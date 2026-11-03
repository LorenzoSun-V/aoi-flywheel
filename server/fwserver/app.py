"""HTTP API (/api/v1). Every machine talks to the platform through this one port."""
from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import __version__, auth, datasets, db
from .datasets import DatasetError, check_digest
from .settings import Settings
from .store import BadContent, Store, StoreError

log = logging.getLogger('fwserver')
DATA_ROLES = ('admin', 'algo')  # step 1: only these roles see or change datasets (design doc, roles table)
MISSING_LIMIT = 20000
BUFFER = 1 << 20


class MissingBody(BaseModel):
    objects: List[Tuple[str, int]] = Field(max_length=MISSING_LIMIT)  # (sha256, size)


class DatasetBody(BaseModel):
    name: str
    site: str = ''
    line: str = ''
    kind: str = 'pool'
    note: str = ''


class VersionBody(BaseModel):
    manifest_sha256: str
    parent_id: Optional[int]
    expect_latest_id: Optional[int]
    source: str = ''
    note: str = ''


def create_app(settings: Optional[Settings] = None, hooks: Optional[dict] = None) -> FastAPI:
    """hooks: test-only fault injection, {'before_freeze': callable}."""
    settings = settings or Settings.from_env()
    hooks = hooks if hooks is not None else {}
    store = Store(settings.store)
    state = {}
    touched = {}
    touched_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        pool = db.make_pool(settings.dsn, settings.pool_size)
        with pool.connection() as conn:
            problems = db.schema_problems(conn)
        if problems:
            pool.close()
            raise RuntimeError('；'.join(problems))
        state['pool'] = pool
        store.clean_tmp()
        yield
        pool.close()
        store.close()

    app = FastAPI(title='aoi-flywheel', version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url='/api/v1/openapi.json')
    app.state.store = store

    @app.exception_handler(DatasetError)
    async def dataset_error(request, exc: DatasetError):
        return JSONResponse({'detail': exc.message}, status_code=exc.status)

    @app.exception_handler(StoreError)
    async def store_error(request, exc: StoreError):
        return JSONResponse({'detail': str(exc)}, status_code=503)

    def conn():
        return state['pool'].connection()

    def current_user(request: Request) -> dict:
        scheme, _, token = request.headers.get('authorization', '').partition(' ')
        if scheme.lower() != 'bearer' or not token.strip():
            raise HTTPException(401, '需要凭据：先运行 fw login', headers={'WWW-Authenticate': 'Bearer'})
        with conn() as c:
            user = auth.authenticate(c, token.strip())
            if user is None:
                raise HTTPException(401, '凭据无效、已吊销，或者账号已停用', headers={'WWW-Authenticate': 'Bearer'})
            now = time.monotonic()
            with touched_lock:
                due = now - touched.get(user['token_id'], -1e9) > 60
                if due:
                    touched[user['token_id']] = now
            if due:
                auth.touch(c, user['token_id'])
        return user

    def data_user(user: dict = Depends(current_user)) -> dict:
        if user['role'] not in DATA_ROLES:
            raise HTTPException(403, f'{auth.ROLES[user["role"]]}账号不能查看或修改数据集')
        return user

    # --- service --------------------------------------------------------------------------------
    @app.get('/api/v1/health')
    def health():
        with conn() as c:
            c.execute('SELECT 1')
        problem = store.problem()
        return {'ok': problem is None, 'version': __version__, 'store_error': problem}

    @app.get('/api/v1/me')
    def me(user: dict = Depends(current_user)):
        return {'name': user['name'], 'role': user['role'], 'token_id': user['token_id'],
                'token_label': user['token_label']}

    # --- objects --------------------------------------------------------------------------------
    @app.post('/api/v1/objects/missing')
    def missing(body: MissingBody, user: dict = Depends(data_user)):
        """Which of these (sha256, size) the store lacks; an object with another size counts as missing."""
        return {'missing': [check_digest(d) for d, size in body.objects if store.size(check_digest(d)) != size]}

    @app.post('/api/v1/objects/flush')
    def flush(user: dict = Depends(data_user)):
        """Make every object uploaded so far durable. Creating a version does this itself; a client that only
        brought back files an existing version lacked calls it before saying the repair is done."""
        store.flush()
        return {'ok': True}

    @app.put('/api/v1/objects/{digest}')
    async def put_object(digest: str, request: Request, user: dict = Depends(data_user)):
        check_digest(digest)
        length = request.headers.get('content-length')
        if length is None or not length.isdigit():
            raise HTTPException(411, '上传需要 Content-Length')
        if int(length) > settings.max_object_bytes:
            raise HTTPException(413, f'单个文件不能超过 {settings.max_object_bytes // 1024 ** 3} GB')
        writer = await run_in_threadpool(store.writer, digest)
        try:
            pending, n = [], 0
            async for chunk in request.stream():
                pending.append(chunk)
                n += len(chunk)
                if n >= BUFFER:
                    await run_in_threadpool(writer.write, b''.join(pending))
                    pending, n = [], 0
            if pending:
                await run_in_threadpool(writer.write, b''.join(pending))
            if writer.size != int(length):
                raise HTTPException(400, '上传不完整')
            created = await run_in_threadpool(writer.commit)
        except BadContent as e:
            raise HTTPException(400, str(e)) from None
        finally:
            await run_in_threadpool(writer.abort)
        return {'sha256': digest, 'size': writer.size, 'created': created}

    @app.get('/api/v1/objects/{digest}')
    def get_object(digest: str, user: dict = Depends(data_user)):
        path = store.path(check_digest(digest))
        if not path.is_file():
            raise HTTPException(404, f'服务器上没有文件 {digest[:12]}')
        return FileResponse(path, media_type='application/octet-stream',
                            headers={'Cache-Control': 'private, max-age=31536000, immutable'})

    # --- datasets and versions -------------------------------------------------------------------
    @app.get('/api/v1/datasets')
    def list_datasets(user: dict = Depends(data_user)):
        with conn() as c:
            return datasets.list_datasets(c)

    @app.post('/api/v1/datasets', status_code=201)
    def create_dataset(body: DatasetBody, user: dict = Depends(data_user)):
        with conn() as c:
            d = datasets.create_dataset(c, user['user_id'], body.name, body.site, body.line, body.kind, body.note)
        log.info('dataset %s created by %s', d['name'], user['name'])
        return d

    @app.get('/api/v1/datasets/{name}')
    def get_dataset(name: str, user: dict = Depends(data_user)):
        with conn() as c:
            return datasets.get_dataset(c, name)

    @app.get('/api/v1/datasets/{name}/versions')
    def list_versions(name: str, user: dict = Depends(data_user)):
        with conn() as c:
            return datasets.list_versions(c, name)

    @app.post('/api/v1/datasets/{name}/versions', status_code=201)
    def create_version(name: str, body: VersionBody, user: dict = Depends(data_user)):
        with conn() as c:
            v = datasets.create_version(c, store, user['user_id'], name, body.manifest_sha256, body.parent_id,
                                        body.expect_latest_id, body.source, body.note,
                                        before_freeze=hooks.get('before_freeze'))
        log.info('version %s frozen by %s: %s files, %s bytes', v['ref'], user['name'], v['file_count'],
                 v['total_bytes'])
        return v

    @app.get('/api/v1/datasets/{name}/versions/{number}')
    def get_version(name: str, number: str, user: dict = Depends(data_user)):
        with conn() as c:
            return datasets.get_version(c, name, number)

    @app.get('/api/v1/versions/{version_id}')
    def get_version_by_id(version_id: int, user: dict = Depends(data_user)):
        with conn() as c:
            return datasets.get_version_by_id(c, version_id)

    @app.get('/api/v1/diff')
    def diff(old: int, new: int, user: dict = Depends(data_user)):
        with conn() as c:
            return datasets.diff(c, old, new)

    return app
