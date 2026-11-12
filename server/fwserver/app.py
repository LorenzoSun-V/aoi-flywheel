"""HTTP API (/api/v1). Every machine talks to the platform through this one port."""
from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from fw.contract import (API_HEADER, API_LEVEL, MIN_CLIENT_LEVEL, peer_level)
from fw.contract import (BATCH_OBJECT_MAX, JUNK_NAMES, MAX_PATH_BYTES, OBJECTS_MAGIC, OBJECTS_TYPE, ContractError,
                         FrameParser, ObjectTooLarge, blocking_reasons, classify_paths, frame_header)

from . import __version__, auth, changes, datasets, db, evals, events, jobs, models, predictions, trainsets
from .datasets import DatasetError, check_digest
from .settings import Settings
from .store import BadContent, Store, StoreError

log = logging.getLogger('fwserver')
MISSING_LIMIT = 20000
FOLDER_LIMIT = 300_000     # files of one folder checked from the web page (bigger ones: fw dataset import)
FOLDER_EXAMPLES = 50       # how many of each problem the check lists
BUFFER = 1 << 20
BATCH_BODY_MAX = 512 << 20   # one batch upload request
FETCH_LIMIT = 5000           # objects per batch download request
FETCH_BYTES_MAX = 512 << 20
STORE_GROUP = 4 << 20        # batch uploads are written to the store in groups of about this many bytes


class FolderCheckBody(BaseModel):
    paths: List[Annotated[str, Field(max_length=MAX_PATH_BYTES)]] = Field(max_length=FOLDER_LIMIT)  # every file's path
    allow_other_files: bool = False
    force: bool = False


class MissingBody(BaseModel):
    objects: List[Tuple[str, int]] = Field(max_length=MISSING_LIMIT)  # (sha256, size)


class RunnerBody(BaseModel):
    name: str
    conda_env: str
    env_sha256: str
    src_dir: str = ''
    src_commit: Optional[str] = None
    src_dirty: bool = False
    src_state_sha256: Optional[str] = None
    framework: str = ''
    onnx2engine: str = ''
    onnx2engine_sha256: Optional[str] = None


class JobBody(BaseModel):
    kind: str
    runner: Optional[str] = None
    params: Optional[dict] = None


class LeaseBody(BaseModel):
    token: str = Field(max_length=200)


class LogBody(LeaseBody):
    seq: int
    text: str


class FinishBody(LeaseBody):
    outcome: str
    result: Optional[dict] = None
    error: str = ''


WEB = Path(__file__).with_name('web')
WEB_FILES = {'app.js': 'text/javascript; charset=utf-8', 'sha256.js': 'text/javascript; charset=utf-8',
             'style.css': 'text/css; charset=utf-8'}
# Pages load only their own scripts and styles; images come from the API as blobs. no-cache: a new fw-server
# version is picked up on the next load.
WEB_HEADERS = {'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
               'Content-Security-Policy': "default-src 'self'; img-src 'self' blob: data:; object-src 'none'; "
                                          "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"}


def _iso(t) -> Optional[str]:
    return t.isoformat() if t else None


class ApiLevel:
    """Says which interface level this server has on every answer, and refuses an fw that is too old for it
    (fw.contract: API_LEVEL, MIN_CLIENT_LEVEL). Plain ASGI, so uploads and downloads still stream."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        stated = dict(scope['headers']).get(API_HEADER.lower().encode())
        level = peer_level(stated.decode('latin-1') if stated is not None else None)
        if scope['path'].startswith('/api/') and level < MIN_CLIENT_LEVEL:
            answer = JSONResponse({'detail': f'这台机器的 fw 太旧（接口第 {level} 版，平台要求至少第 {MIN_CLIENT_LEVEL} 版）：'
                                             '在仓库目录 git pull 后重新运行 python -m pip install ./client'},
                                  status_code=426, headers={API_HEADER: str(API_LEVEL)})
            await answer(scope, receive, send)
            return

        async def with_level(message):
            if message['type'] == 'http.response.start':
                message = dict(message, headers=list(message.get('headers') or [])
                               + [(API_HEADER.lower().encode(), str(API_LEVEL).encode())])
            await send(message)

        await self.app(scope, receive, with_level)


def size_text(n: int) -> str:
    for unit, scale in (('GB', 1 << 30), ('MB', 1 << 20), ('KB', 1 << 10)):
        if n >= scale:
            return f'{n / scale:g} {unit}'
    return f'{n} 字节'


class FetchBody(BaseModel):
    objects: List[str] = Field(max_length=FETCH_LIMIT)


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


class AddDirBody(BaseModel):
    dir: str
    manifest_sha256: str  # the folder's own file list (paths inside the folder), uploaded like any object
    replace: bool = False


class ChangeBody(BaseModel):
    """The next version = the newest one with folders added, replaced or removed (changes.py)."""
    expect_latest_id: Optional[int] = None
    add: List[AddDirBody] = Field(default_factory=list, max_length=changes.MAX_DIRS)
    remove: List[str] = Field(default_factory=list, max_length=changes.MAX_DIRS)
    allow_other_files: bool = False
    force: bool = False
    source: str = ''
    note: str = ''


class UserBody(BaseModel):
    name: str
    role: str


class RoleBody(BaseModel):
    role: str


class TokenBody(BaseModel):
    label: str = ''


class TrainsetBody(BaseModel):
    sources: List[str] = Field(min_length=1, max_length=100)  # pool versions, e.g. SES-TX@4
    classes: List[str] = Field(min_length=1, max_length=trainsets.MAX_CLASSES)
    val: float = 0.2
    seed: int = 0
    test_dirs: List[str] = Field(default_factory=list, max_length=1000)
    merge_test: bool = False
    new_to: str = 'test'
    split: Optional[Dict[str, str]] = None  # key (<site>/<line>/<image path>) -> train, val or test
    fresh: bool = False
    note: str = ''
    require_labels: bool = False  # refuse images without a label (default: they are negative samples)
    label_source: Optional[str] = None  # json or txt: the label of an image that has both
    label_classes: Optional[List[str]] = Field(default=None, max_length=trainsets.MAX_CLASSES)  # .txt numbering
    class_map: Optional[Dict[str, str]] = None  # class in the labels -> class in classes, '' drops the box
    test: float = 0.0  # the share cut off as test, next to val
    expect_latest_id: Optional[int] = None  # create only: the newest snapshot seen in the preview

    def options(self) -> trainsets.Options:
        return trainsets.Options(self.sources, self.classes, self.val, self.seed, self.test_dirs, self.merge_test,
                                 self.new_to, self.split, self.fresh, self.note, self.require_labels,
                                 self.label_source, self.label_classes, self.class_map, self.test)


class ModelBody(BaseModel):
    name: str
    site: str
    line: str
    task: str = 'det'
    note: str = ''


FileRow = Tuple[str, str, int]  # (path, sha256, size)


class ModelVersionBody(BaseModel):
    files: List[FileRow] = Field(max_length=models.MAX_FILES)
    trainset_id: int
    code_commit: str = ''  # optional: no training code recorded
    code_repo: str = ''
    code_dirty: bool = False
    note: str = ''


class TagBody(BaseModel):
    tag: str
    added: bool = True
    note: str = ''


class EvalBody(BaseModel):
    testset_id: int
    split: str = ''
    files: List[FileRow] = Field(max_length=models.MAX_FILES)
    tool: str = ''
    tool_version: str = ''
    note: str = ''


class ReleaseBody(BaseModel):
    model: str            # model name
    number: str           # its version number
    site: str = ''        # default: the model's
    line: str = ''
    deploy_repo: str
    deploy_commit: str
    deployed_at: str      # ISO 8601 with a time zone
    gpu: str = ''
    trt_version: str = ''
    precision: str = ''
    engine_sha256: Optional[str] = None  # the engine the site built from the ONNX (the file stays there)
    engine_size: Optional[int] = None
    note: str = ''


def create_app(settings: Optional[Settings] = None, hooks: Optional[dict] = None) -> FastAPI:
    """hooks: test-only fault injection, {'before_freeze': callable}."""
    settings = settings or Settings.from_env()
    hooks = hooks if hooks is not None else {}
    store = Store(settings.store)
    too_large = f'单个文件不能超过 {size_text(settings.max_object_bytes)}（FW_MAX_OBJECT_GB）'
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
    app.add_middleware(ApiLevel)

    @app.exception_handler(DatasetError)
    async def dataset_error(request, exc: DatasetError):
        return JSONResponse({'detail': exc.message}, status_code=exc.status)

    @app.exception_handler(auth.AuthError)
    async def auth_error(request, exc: auth.AuthError):
        return JSONResponse({'detail': str(exc)}, status_code=400)

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

    def need(cap: str):
        """A dependency that lets through the accounts whose role has this capability (auth.CAPS)."""
        def check(user: dict = Depends(current_user)) -> dict:
            if not auth.allowed(user['role'], cap):
                raise HTTPException(403, f'{auth.ROLES[user["role"]]}账号不能{auth.CAPS[cap][1]}')
            return user
        return check

    data_reader = need('datasets.read')
    data_user = need('datasets.write')
    uploader = need('objects.upload')
    downloader = need('objects.download')
    job_reader = need('jobs.read')
    job_writer = need('jobs.write')
    worker = need('jobs.run')
    model_reader = need('models.read')
    model_writer = need('models.write')
    release_writer = need('releases.write')
    event_reader = need('events.read')
    account_admin = need('users.manage')

    # --- service --------------------------------------------------------------------------------
    @app.get('/api/v1/health')
    def health():
        with conn() as c:
            c.execute('SELECT 1')
        problem = store.problem()
        return {'ok': problem is None, 'version': __version__, 'store_error': problem}

    @app.get('/api/v1/me')
    def me(user: dict = Depends(current_user)):
        return {'name': user['name'], 'role': user['role'], 'role_text': auth.ROLES[user['role']],
                'caps': auth.caps_of(user['role']), 'token_id': user['token_id'],
                'token_label': user['token_label'], 'server_version': __version__, 'api_level': API_LEVEL}

    # --- accounts, tokens, the record of who did what -------------------------------------------
    def token_row(t: dict) -> dict:
        return {'id': t['id'], 'account': t['name'], 'label': t['label'], 'created_at': _iso(t['created_at']),
                'last_used_at': _iso(t['last_used_at']), 'revoked_at': _iso(t['revoked_at'])}

    @app.get('/api/v1/users')
    def list_users(user: dict = Depends(account_admin)):
        with conn() as c:
            return {'roles': auth.ROLES, 'users': [
                {'name': u['name'], 'role': u['role'], 'role_text': auth.ROLES[u['role']],
                 'created_at': _iso(u['created_at']), 'disabled_at': _iso(u['disabled_at']),
                 'active_tokens': u['active_tokens']} for u in auth.list_users(c)]}

    @app.post('/api/v1/users', status_code=201)
    def create_user(body: UserBody, user: dict = Depends(account_admin)):
        with conn() as c:
            u = auth.add_user(c, body.name, body.role, actor_id=user['user_id'])
        return {'name': u['name'], 'role': u['role']}

    @app.post('/api/v1/users/{name}/role')
    def set_user_role(name: str, body: RoleBody, user: dict = Depends(account_admin)):
        with conn() as c:
            u = auth.set_role(c, name, body.role, actor_id=user['user_id'])
        return {'name': u['name'], 'role': u['role']}

    @app.post('/api/v1/users/{name}/disable')
    def disable_user(name: str, user: dict = Depends(account_admin)):
        with conn() as c:
            return {'tokens_revoked': auth.set_disabled(c, name, True, actor_id=user['user_id'])}

    @app.post('/api/v1/users/{name}/enable')
    def enable_user(name: str, user: dict = Depends(account_admin)):
        with conn() as c:
            auth.set_disabled(c, name, False, actor_id=user['user_id'])
        return {}

    @app.get('/api/v1/users/{name}/tokens')
    def list_user_tokens(name: str, user: dict = Depends(account_admin)):
        with conn() as c:
            auth.get_user(c, name)
            return {'tokens': [token_row(t) for t in auth.list_tokens(c, name)]}

    @app.post('/api/v1/users/{name}/tokens', status_code=201)
    def create_user_token(name: str, body: TokenBody, user: dict = Depends(account_admin)):
        """The token is in this answer and nowhere else: only its hash is kept."""
        with conn() as c:
            token, token_id = auth.create_token(c, name, body.label, actor_id=user['user_id'])
        return {'id': token_id, 'token': token}

    @app.post('/api/v1/tokens/{token_id}/revoke')
    def revoke_token(token_id: int, user: dict = Depends(current_user)):
        """An administrator revokes any token; everyone else only their own."""
        mine = None if auth.allowed(user['role'], 'users.manage') else user['user_id']
        with conn() as c:
            if not auth.revoke_token(c, token_id, actor_id=user['user_id'], owner_id=mine):
                raise HTTPException(404, f'凭据 {token_id} 不存在、已吊销，或者不是你的')
        return {}

    @app.get('/api/v1/me/tokens')
    def my_tokens(user: dict = Depends(current_user)):
        with conn() as c:
            return {'tokens': [token_row(t) for t in auth.list_tokens(c, user['name'])]}

    @app.get('/api/v1/events')
    def list_events(limit: int = 100, before: Optional[int] = None, actor: str = '', action: str = '', q: str = '',
                    user: dict = Depends(event_reader)):
        with conn() as c:
            return events.list_events(c, limit, before, actor, action, q)

    # --- objects --------------------------------------------------------------------------------
    @app.post('/api/v1/objects/missing')
    def missing(body: MissingBody, user: dict = Depends(uploader)):
        """Which of these (sha256, size) the store lacks; an object with another size counts as missing."""
        return {'missing': [check_digest(d) for d, size in body.objects if store.size(check_digest(d)) != size]}

    @app.post('/api/v1/folder-check')
    def folder_check(body: FolderCheckBody, user: dict = Depends(data_user)):
        """The checks of fw dataset import on a folder the web page picked (only its paths: the browser does
        the hashing). junk: system files to leave out; reasons: why it must not be imported as it stands."""
        junk = [p for p in body.paths if p.rpartition('/')[2].lower() in JUNK_NAMES]
        rels = [p for p in body.paths if p.rpartition('/')[2].lower() not in JUNK_NAMES]
        found = classify_paths(rels)
        reasons = blocking_reasons(found, body.allow_other_files, body.force)
        if not rels:
            reasons.append('目录里没有文件')
        out = {'files': len(rels), 'junk': junk, 'reasons': reasons}
        for key, value in found.items():
            out[key] = {'count': len(value), 'examples': value[:FOLDER_EXAMPLES]}
        return out

    @app.post('/api/v1/objects/flush')
    def flush(user: dict = Depends(uploader)):
        """Make every object uploaded so far durable. Creating a version does this itself; a client that only
        brought back files an existing version lacked calls it before saying the repair is done."""
        store.flush()
        return {'ok': True}

    @app.put('/api/v1/objects/{digest}')
    async def put_object(digest: str, request: Request, user: dict = Depends(uploader)):
        check_digest(digest)
        length = request.headers.get('content-length')
        if length is None or not length.isdigit():
            raise HTTPException(411, '上传需要 Content-Length')
        if int(length) > settings.max_object_bytes:
            raise HTTPException(413, too_large)
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

    @app.post('/api/v1/objects/batch')
    async def put_objects(request: Request, user: dict = Depends(uploader)):
        """Many small objects in one fw-objects body (fw.contract); each one is checked against its own hash.
        Objects before a bad one stay stored (they are content-addressed, so that is harmless)."""
        length = request.headers.get('content-length')
        if length is None or not length.isdigit():
            raise HTTPException(411, '上传需要 Content-Length')
        if int(length) > BATCH_BODY_MAX:
            raise HTTPException(413, f'一次批量上传不能超过 {BATCH_BODY_MAX >> 20} MB')
        store.check_writable()
        parser = FrameParser(min(BATCH_OBJECT_MAX, settings.max_object_bytes))  # the same limit as one by one
        group, group_bytes = [], 0
        stored = created = received = 0

        def put_group(objects):
            return sum(store.put_verified(d, data) for d, data in objects)

        try:
            async for chunk in request.stream():
                received += len(chunk)
                for digest, data in parser.feed(chunk):
                    group.append((digest, data))
                    group_bytes += len(data)
                    if group_bytes >= STORE_GROUP or len(group) >= 512:
                        created += await run_in_threadpool(put_group, group)
                        stored += len(group)
                        group, group_bytes = [], 0
            if received != int(length):
                raise HTTPException(400, '上传不完整')
            parser.finish()
            if group:
                created += await run_in_threadpool(put_group, group)
                stored += len(group)
        except ObjectTooLarge as e:
            if e.size > settings.max_object_bytes:
                raise HTTPException(413, too_large) from None
            raise HTTPException(400, str(e)) from None
        except ContractError as e:
            raise HTTPException(400, str(e)) from None
        except BadContent as e:
            raise HTTPException(400, str(e)) from None
        return {'stored': stored, 'created': created}

    @app.post('/api/v1/objects/fetch')
    def fetch_objects(body: FetchBody, user: dict = Depends(downloader)):
        """Many small objects in one streamed fw-objects body, in the order asked for."""
        digests = [check_digest(d) for d in body.objects]
        sizes = [store.size(d) for d in digests]
        missing = [d for d, s in zip(digests, sizes) if s is None]
        if missing:
            raise HTTPException(404, f'服务器上没有 {len(missing)} 个文件，例如 {missing[0][:12]}')
        if any(s > BATCH_OBJECT_MAX for s in sizes):
            raise HTTPException(400, f'超过 {BATCH_OBJECT_MAX >> 20} MB 的文件要单独下载')
        total = sum(sizes)
        if total > FETCH_BYTES_MAX:
            raise HTTPException(413, f'一次批量下载不能超过 {FETCH_BYTES_MAX >> 20} MB')
        headers = [frame_header(d, s) for d, s in zip(digests, sizes)]
        length = len(OBJECTS_MAGIC) + sum(len(h) for h in headers) + total

        def stream():
            out = bytearray(OBJECTS_MAGIC)
            for digest, size, head in zip(digests, sizes, headers):
                data = store.read(digest)
                if data is None or len(data) != size:  # quarantined or repaired meanwhile: cut the response short
                    raise RuntimeError(f'object {digest} changed while it was being sent')
                out += head
                out += data
                if len(out) >= BUFFER:
                    yield bytes(out)
                    out = bytearray()
            if out:
                yield bytes(out)

        return StreamingResponse(stream(), media_type=OBJECTS_TYPE, headers={'Content-Length': str(length)})

    @app.get('/api/v1/objects/{digest}')
    def get_object(digest: str, user: dict = Depends(downloader)):
        check_digest(digest)
        path = store.path(digest)
        if not path.is_file():
            raise HTTPException(404, f'服务器上没有文件 {digest[:12]}')
        return FileResponse(path, media_type='application/octet-stream',
                            headers={'Cache-Control': 'private, max-age=31536000, immutable'})

    # --- datasets and versions -------------------------------------------------------------------
    @app.get('/api/v1/datasets')
    def list_datasets(user: dict = Depends(data_reader)):
        with conn() as c:
            return datasets.list_datasets(c)

    @app.post('/api/v1/datasets', status_code=201)
    def create_dataset(body: DatasetBody, user: dict = Depends(data_user)):
        with conn() as c:
            d = datasets.create_dataset(c, user['user_id'], body.name, body.site, body.line, body.kind, body.note)
        log.info('dataset %s created by %s', d['name'], user['name'])
        return d

    @app.get('/api/v1/datasets/{name}')
    def get_dataset(name: str, user: dict = Depends(data_reader)):
        with conn() as c:
            return datasets.get_dataset(c, name)

    @app.get('/api/v1/datasets/{name}/versions')
    def list_versions(name: str, user: dict = Depends(data_reader)):
        with conn() as c:
            return changes.attach(c, datasets.list_versions(c, name))

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
    def get_version(name: str, number: str, user: dict = Depends(data_reader)):
        with conn() as c:
            return changes.attach(c, [datasets.get_version(c, name, number)])[0]

    @app.post('/api/v1/datasets/{name}/changes/preview')
    def preview_change(name: str, body: ChangeBody, user: dict = Depends(data_user)):
        with conn() as c:
            return changes.plan(c, store, name, [a.model_dump() for a in body.add], body.remove,
                                body.allow_other_files, body.force).summary()

    @app.post('/api/v1/datasets/{name}/changes', status_code=201)
    def create_change(name: str, body: ChangeBody, user: dict = Depends(data_user)):
        with conn() as c:
            out = changes.create(c, store, user['user_id'], name, [a.model_dump() for a in body.add], body.remove,
                                 body.expect_latest_id, body.allow_other_files, body.force, body.source, body.note,
                                 before_freeze=hooks.get('before_freeze'))
        log.info('version %s frozen by %s: %s', out['version']['ref'], user['name'],
                 ', '.join(f'{o["op"]} {o["dir"]}' for o in out['ops']))
        return out

    @app.get('/api/v1/versions/{version_id}')
    def get_version_by_id(version_id: int, user: dict = Depends(data_reader)):
        with conn() as c:
            return datasets.get_version_by_id(c, version_id)

    # --- training-set snapshots ------------------------------------------------------------------
    @app.get('/api/v1/trainsets')
    def list_trainsets(user: dict = Depends(data_reader)):
        with conn() as c:
            return trainsets.list_trainsets(c)

    @app.post('/api/v1/trainsets/{name}/preview')
    def preview_trainset(name: str, body: TrainsetBody, user: dict = Depends(data_user)):
        """What `create` would make, without making it: counts, changes against the previous snapshot, problems."""
        with conn() as c:
            return trainsets.plan(c, store, name, body.options()).summary()

    @app.post('/api/v1/trainsets/{name}/versions', status_code=201)
    def create_trainset(name: str, body: TrainsetBody, user: dict = Depends(data_user)):
        with conn() as c:
            v = trainsets.create(c, store, user['user_id'], name, body.options(), body.expect_latest_id,
                                 before_freeze=hooks.get('before_freeze'))
        log.info('trainset %s frozen by %s: %s samples', v['ref'], user['name'], v['summary']['samples'])
        return v

    @app.get('/api/v1/trainsets/{name}/versions/{number}')
    def get_trainset(name: str, number: str, user: dict = Depends(data_reader)):
        with conn() as c:
            return trainsets.get(c, name, number)

    @app.get('/api/v1/trainsets/{name}/versions/{number}/layout')
    def trainset_layout(name: str, number: str, user: dict = Depends(data_reader)):
        """The folders fw get makes by default, so the web page shows a snapshot the way people get it."""
        with conn() as c:
            return trainsets.layout(c, name, number)

    # --- models, evaluations, releases -------------------------------------------------------------
    @app.get('/api/v1/models')
    def list_models(user: dict = Depends(model_reader)):
        with conn() as c:
            return models.list_models(c)

    @app.post('/api/v1/models', status_code=201)
    def create_model(body: ModelBody, user: dict = Depends(model_writer)):
        with conn() as c:
            m = models.create_model(c, user['user_id'], body.name, body.site, body.line, body.task, body.note)
        log.info('model %s created by %s', m['name'], user['name'])
        return m

    @app.get('/api/v1/models/{name}')
    def get_model(name: str, user: dict = Depends(model_reader)):
        with conn() as c:
            return models.get_model(c, name)

    @app.get('/api/v1/models/{name}/versions')
    def list_model_versions(name: str, user: dict = Depends(model_reader)):
        with conn() as c:
            return models.list_model_versions(c, name)

    @app.post('/api/v1/models/{name}/versions', status_code=201)
    def register_model_version(name: str, body: ModelVersionBody, user: dict = Depends(model_writer)):
        with conn() as c:
            mv = models.register_model_version(c, store, user['user_id'], name, body.files, body.trainset_id,
                                               body.code_repo, body.code_commit, body.code_dirty, body.note)
        if not mv['existing']:
            log.info('model version %s registered by %s: onnx %s', mv['ref'], user['name'], mv['onnx_sha256'][:12])
        return mv

    @app.get('/api/v1/models/{name}/versions/{number}')
    def get_model_version(name: str, number: str, user: dict = Depends(model_reader)):
        with conn() as c:
            return models.model_version_detail(c, name, number)

    @app.post('/api/v1/models/{name}/versions/{number}/tags')
    def set_tag(name: str, number: str, body: TagBody, user: dict = Depends(model_writer)):
        with conn() as c:
            return models.set_tag(c, user['user_id'], name, number, body.tag, body.added, body.note)

    @app.post('/api/v1/models/{name}/versions/{number}/evals', status_code=201)
    def register_eval(name: str, number: str, body: EvalBody, user: dict = Depends(model_writer)):
        with conn() as c:
            e = models.register_eval(c, store, user['user_id'], name, number, body.testset_id, body.split,
                                     body.files, body.tool, body.tool_version, body.note)
        if not e['existing']:
            log.info('evaluation #%s of %s registered by %s', e['id'], e['model_version']['ref'], user['name'])
        return e

    @app.get('/api/v1/evals')
    def list_evals(model: str = '', user: dict = Depends(model_reader)):
        with conn() as c:
            return models.list_evals(c, model=model or None)

    @app.get('/api/v1/evals/{eval_id}')
    def get_eval(eval_id: int, user: dict = Depends(model_reader)):
        with conn() as c:
            return models.get_eval(c, eval_id)

    @app.get('/api/v1/predictions')
    def list_prediction_sets(model: str = '', user: dict = Depends(data_reader)):
        with conn() as c:
            return {'prediction_sets': predictions.list_sets(c, model)}

    @app.get('/api/v1/predictions/{set_id}')
    def get_prediction_set(set_id: int, user: dict = Depends(data_reader)):
        with conn() as c:
            return predictions.get_set(c, set_id)

    @app.get('/api/v1/releases')
    def list_releases(site: str = '', line: str = '', user: dict = Depends(model_reader)):
        with conn() as c:
            return models.list_releases(c, site, line)

    @app.post('/api/v1/releases', status_code=201)
    def register_release(body: ReleaseBody, user: dict = Depends(release_writer)):
        with conn() as c:
            r = models.register_release(c, user['user_id'], body.model, body.number, body.site, body.line,
                                        body.deploy_repo, body.deploy_commit, body.deployed_at, body.gpu,
                                        body.trt_version, body.precision, body.engine_sha256, body.engine_size,
                                        body.note)
        if not r['existing']:
            log.info('release %s (%s) registered by %s', r['ref'], r['model_version']['ref'], user['name'])
        return r

    @app.get('/api/v1/releases/current')
    def current_release(site: str, line: str, user: dict = Depends(model_reader)):
        with conn() as c:
            return models.current_release(c, site, line)

    @app.get('/api/v1/releases/{ref}')
    def get_release(ref: str, user: dict = Depends(model_reader)):
        with conn() as c:
            return models.get_release(c, ref)

    @app.get('/api/v1/chain')
    def chain(ref: str, check: bool = False, user: dict = Depends(model_reader)):
        with conn() as c:
            return models.chain(c, store, ref, check)

    # --- runners and jobs ------------------------------------------------------------------------
    @app.get('/api/v1/runners')
    def list_runners(user: dict = Depends(job_reader)):
        with conn() as c:
            return {'runners': jobs.list_runners(c)}

    @app.post('/api/v1/runners', status_code=201)
    def register_runner(body: RunnerBody, user: dict = Depends(worker)):
        with conn() as c:
            r = jobs.register_runner(c, store, user, body.name, body.conda_env, body.env_sha256, body.src_dir,
                                     body.src_commit, body.src_dirty, body.src_state_sha256, body.framework,
                                     body.onnx2engine, body.onnx2engine_sha256)
        if r['created']:
            log.info('runner %s registered by %s', r['ref'], user['name'])
        return r

    @app.get('/api/v1/runners/{name}')
    def get_runner(name: str, user: dict = Depends(job_reader)):
        with conn() as c:
            return {'versions': jobs.runner_versions(c, name)}

    @app.get('/api/v1/jobs')
    def list_jobs(status: str = '', limit: int = 50, user: dict = Depends(job_reader)):
        with conn() as c:
            return {'jobs': jobs.list_jobs(c, status, limit)}

    @app.post('/api/v1/jobs', status_code=201)
    def submit_job(body: JobBody, user: dict = Depends(job_writer)):
        with conn() as c:
            if body.kind == 'eval':
                job = evals.submit(c, store, user, body.runner, body.params)
            elif body.kind == 'predict':
                job = predictions.submit(c, store, user, body.runner, body.params)
            else:
                job = jobs.submit(c, user, body.kind, body.runner, body.params)
        log.info('job %s (%s) submitted by %s', job['id'], job['kind'], user['name'])
        return job

    @app.get('/api/v1/jobs/{job_id}')
    def get_job(job_id: int, user: dict = Depends(job_reader)):
        with conn() as c:
            return jobs.get_job(c, job_id)

    @app.post('/api/v1/jobs/{job_id}/cancel')
    def cancel_job(job_id: int, user: dict = Depends(job_writer)):
        with conn() as c:
            return jobs.cancel(c, job_id, user['user_id'])

    @app.get('/api/v1/jobs/{job_id}/log')
    def get_job_log(job_id: int, attempt: int = 0, after: int = -1, user: dict = Depends(job_reader)):
        with conn() as c:
            return jobs.job_log(c, job_id, attempt or None, after)

    @app.post('/api/v1/worker/claim')
    def claim_job(user: dict = Depends(worker)):
        with conn() as c:
            job = jobs.claim(c, user, settings.job_lease_seconds)
        if job is not None:
            log.info('job %s attempt %s claimed by %s', job['id'], job['attempt'], user['name'])
        return {'job': job}

    @app.post('/api/v1/jobs/{job_id}/renew')
    def renew_job(job_id: int, body: LeaseBody, user: dict = Depends(worker)):
        with conn() as c:
            return jobs.renew(c, job_id, body.token, settings.job_lease_seconds)

    @app.post('/api/v1/jobs/{job_id}/log')
    def add_job_log(job_id: int, body: LogBody, user: dict = Depends(worker)):
        with conn() as c:
            return jobs.add_log(c, job_id, body.token, body.seq, body.text)

    @app.post('/api/v1/jobs/{job_id}/finish')
    def finish_job(job_id: int, body: FinishBody, user: dict = Depends(worker)):
        with conn() as c:
            job = evals.finish(c, store, job_id, body.token, body.outcome, body.result, body.error)
        log.info('job %s finished: %s', job_id, job['status'])
        return job

    # --- web pages (step 4): static files, no build step; they read the API above with the account's token ----
    @app.get('/', include_in_schema=False)
    def web_index():
        return FileResponse(WEB / 'index.html', media_type='text/html; charset=utf-8', headers=WEB_HEADERS)

    @app.get('/web/{name}', include_in_schema=False)
    def web_file(name: str):
        kind = WEB_FILES.get(name)
        if kind is None:
            raise HTTPException(404, '没有这个文件')
        return FileResponse(WEB / name, media_type=kind, headers=WEB_HEADERS)

    @app.get('/api/v1/diff')
    def diff(old: int, new: int, user: dict = Depends(data_reader)):
        with conn() as c:
            return datasets.diff(c, old, new)

    return app
