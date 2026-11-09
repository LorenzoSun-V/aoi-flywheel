"""HTTP client of the platform service. Every machine talks to the service through this one port."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import BinaryIO, Callable, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .contract import BATCH_OBJECT_MAX, CHUNK, OBJECTS_MAGIC, OBJECTS_TYPE, ContractError, FrameParser, frame_header

SMALL_BODY = 32 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f'{message}（HTTP {status}）' if status else message)
        self.status = status
        self.message = message


def _retry(call, attempts: int = 4):
    """Retry idempotent calls on network errors (not on HTTP error responses)."""
    delay = 1.0
    for i in range(attempts):
        try:
            return call()
        except httpx.TransportError:
            if i == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


class Api:
    def __init__(self, server: str, token: Optional[str], timeout: float = 300.0, transport=None):
        if not server:
            raise ApiError(0, '还没有配置服务器地址，先运行 fw login')
        self.server = server.rstrip('/')
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        self.http = httpx.Client(
            base_url=self.server + '/api/v1', headers=headers,
            timeout=httpx.Timeout(timeout, connect=15.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=32),
            trust_env=False,  # the platform is on the local network: never route it through a proxy
            transport=transport)

    def close(self):
        self.http.close()

    @staticmethod
    def _json(r: httpx.Response):
        if r.status_code >= 400:
            try:
                detail = r.json().get('detail', r.text)
            except ValueError:
                detail = r.text
            if isinstance(detail, list):  # request validation errors
                detail = '；'.join(str(d.get('msg', d)) if isinstance(d, dict) else str(d) for d in detail)
            raise ApiError(r.status_code, str(detail))
        return r.json()

    def get(self, path: str, **params):
        return self._json(_retry(lambda: self.http.get(path, params=params)))

    def post(self, path: str, body: dict):
        return self._json(self.http.post(path, json=body))  # not retried: creating twice is not harmless

    # --- objects -------------------------------------------------------------------------------
    def missing(self, objects: List[Tuple[str, int]]) -> List[str]:
        """Which of these (sha256, size) the server lacks (an object of another size counts as missing)."""
        out: List[str] = []
        for i in range(0, len(objects), 5000):
            part = [[d, s] for d, s in objects[i:i + 5000]]
            out += self._json(_retry(lambda: self.http.post('/objects/missing', json={'objects': part})))['missing']
        return out

    def put_file(self, digest: str, path) -> int:
        size = os.path.getsize(path)
        headers = {'Content-Type': 'application/octet-stream'}

        def send():
            with open(path, 'rb') as f:
                if size <= SMALL_BODY:
                    return self.http.put(f'/objects/{digest}', content=f.read(), headers=headers)
                return self.http.put(f'/objects/{digest}', headers=dict(headers, **{'Content-Length': str(size)}),
                                     content=iter(lambda: f.read(CHUNK), b''))
        return self._json(_retry(send))['size']

    def put_objects(self, items: List[Tuple[str, Path, int]]) -> int:
        """Upload several small objects (sha256, path, size) in one request; the server checks each one
        against its own hash. Returns the bytes sent."""
        parts = [OBJECTS_MAGIC]
        total = 0
        for digest, path, size in items:
            if size > BATCH_OBJECT_MAX:
                raise ValueError('large objects go through put_file')
            with open(path, 'rb') as f:
                data = f.read(size + 1)
            if len(data) != size:
                raise ApiError(0, f'{path} 在算完哈希以后被改过（大小变了），等它不再变化后重新导入')
            parts += [frame_header(digest, size), data]
            total += size
        body = b''.join(parts)
        self._json(_retry(lambda: self.http.post('/objects/batch', content=body,
                                                 headers={'Content-Type': OBJECTS_TYPE})))
        return total

    def fetch_objects(self, objects: List[Tuple[str, int]], on_object: Callable[[str, bytes], None]) -> None:
        """Download several small objects (sha256, size) in one streamed response. on_object(sha256, data)
        is called once per object, only after its size and hash were checked; after a network error the
        objects not yet received are asked for again."""
        expected = dict(objects)
        remaining = [d for d, _ in objects]
        delay = 1.0
        for attempt in range(4):
            parser = FrameParser(BATCH_OBJECT_MAX)
            done = 0
            try:
                with self.http.stream('POST', '/objects/fetch', json={'objects': remaining}) as r:
                    if r.status_code >= 400:
                        r.read()
                        self._json(r)
                    for chunk in r.iter_bytes(CHUNK):
                        for digest, data in parser.feed(chunk):
                            if done >= len(remaining) or digest != remaining[done] \
                                    or len(data) != expected[digest] or hashlib.sha256(data).hexdigest() != digest:
                                raise ApiError(0, f'下载的文件 {digest[:12]} 和它的哈希对不上')
                            on_object(digest, data)
                            done += 1
                parser.finish()
            except httpx.TransportError:
                remaining = remaining[done:]
                if not remaining:
                    return
                if attempt == 3:
                    raise
                time.sleep(delay)
                delay *= 2
                continue
            except ContractError as e:
                raise ApiError(0, f'下载内容格式不对：{e}') from None
            if done != len(remaining):
                raise ApiError(0, f'下载不完整：还差 {len(remaining) - done} 个文件')
            return

    def put_bytes(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self._json(_retry(lambda: self.http.put(f'/objects/{digest}', content=data,
                                                headers={'Content-Type': 'application/octet-stream'})))
        return digest

    def flush(self) -> None:
        """Ask the server to make every object uploaded so far durable (creating a version does it anyway)."""
        self._json(_retry(lambda: self.http.post('/objects/flush')))

    def download(self, digest: str, out: BinaryIO) -> int:
        """Stream one object into `out` and check its hash; returns its size."""
        def run():
            out.seek(0)
            out.truncate()
            h = hashlib.sha256()
            n = 0
            with self.http.stream('GET', f'/objects/{digest}') as r:
                if r.status_code >= 400:
                    r.read()
                    self._json(r)
                for block in r.iter_bytes(CHUNK):
                    h.update(block)
                    out.write(block)
                    n += len(block)
            if h.hexdigest() != digest:
                raise ApiError(0, f'下载的文件 {digest[:12]} 和它的哈希对不上')
            return n
        return _retry(run)

    def read_object(self, digest: str) -> bytes:
        def run():
            r = self.http.get(f'/objects/{digest}')
            if r.status_code >= 400:
                self._json(r)
            return r.content
        data = _retry(run)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ApiError(0, f'下载的文件 {digest[:12]} 和它的哈希对不上')
        return data

    # --- datasets and versions -----------------------------------------------------------------
    def me(self):
        return self.get('/me')

    def datasets(self):
        return self.get('/datasets')

    def dataset(self, name: str):
        return self.get(f'/datasets/{name}')

    def create_dataset(self, name: str, site: str, line: str, kind: str, note: str):
        return self.post('/datasets', {'name': name, 'site': site, 'line': line, 'kind': kind, 'note': note})

    def version(self, name: str, number='latest'):
        return self.get(f'/datasets/{name}/versions/{number}')

    def versions(self, name: str):
        return self.get(f'/datasets/{name}/versions')

    def create_version(self, name: str, manifest_sha256: str, parent_id: Optional[int],
                       expect_latest_id: Optional[int], source: str, note: str):
        """expect_latest_id: the dataset's latest version as this client saw it; the server refuses the new
        version if someone else added one in the meantime (each version is a full snapshot, so silently
        building on a stale latest would drop their changes)."""
        return self.post(f'/datasets/{name}/versions', {
            'manifest_sha256': manifest_sha256, 'parent_id': parent_id, 'expect_latest_id': expect_latest_id,
            'source': source, 'note': note})

    def diff(self, old_id: int, new_id: int):
        return self.get('/diff', old=old_id, new=new_id)

    # --- training-set snapshots ----------------------------------------------------------------
    def trainsets(self):
        return self.get('/trainsets')

    def preview_trainset(self, name: str, body: dict):
        """What creating this snapshot would make (counts, changes, problems); nothing is written."""
        return self._json(_retry(lambda: self.http.post(f'/trainsets/{name}/preview', json=body)))

    def create_trainset(self, name: str, body: dict):
        """body as for the preview plus expect_latest_id from it: refused if a snapshot was made meanwhile."""
        return self.post(f'/trainsets/{name}/versions', body)

    def trainset(self, name: str, number='latest'):
        return self.get(f'/trainsets/{name}/versions/{number}')

    # --- models, evaluations, releases -------------------------------------------------------------
    def models(self):
        return self.get('/models')

    def model(self, name: str):
        return self.get(f'/models/{name}')

    def create_model(self, name: str, site: str, line: str, task: str, note: str):
        return self.post('/models', {'name': name, 'site': site, 'line': line, 'task': task, 'note': note})

    def model_versions(self, name: str):
        return self.get(f'/models/{name}/versions')

    def model_version(self, name: str, number='latest'):
        """The version with its files, evaluations and releases."""
        return self.get(f'/models/{name}/versions/{number}')

    def register_model_version(self, name: str, body: dict):
        """Registering the same files again returns the existing version (existing: true), so a retry after
        a network error is harmless."""
        return self._json(_retry(lambda: self.http.post(f'/models/{name}/versions', json=body)))

    def set_tag(self, name: str, number, tag: str, added: bool, note: str = ''):
        return self.post(f'/models/{name}/versions/{number}/tags', {'tag': tag, 'added': added, 'note': note})

    def register_eval(self, name: str, number, body: dict):
        """Like register_model_version: the same report again returns the existing one."""
        return self._json(_retry(lambda: self.http.post(f'/models/{name}/versions/{number}/evals', json=body)))

    def prediction_sets(self, model: str = ''):
        return self.get('/predictions', model=model)['prediction_sets']

    def prediction_set(self, set_id: int):
        return self.get(f'/predictions/{int(set_id)}')

    def evals(self, model: str = ''):
        return self.get('/evals', model=model)

    def eval(self, eval_id: int):
        return self.get(f'/evals/{eval_id}')

    def releases(self, site: str = '', line: str = ''):
        return self.get('/releases', site=site, line=line)

    def register_release(self, body: dict):
        """The same release again (same line, model version, deploy commit and time) returns the existing one."""
        return self._json(_retry(lambda: self.http.post('/releases', json=body)))

    def current_release(self, site: str, line: str):
        return self.get('/releases/current', site=site, line=line)

    def release(self, ref: str):
        return self.get(f'/releases/{quote(ref, safe="")}')

    def chain(self, ref: str, check: bool = False):
        return self.get('/chain', ref=ref, check=str(check).lower())

    # --- runners and jobs ------------------------------------------------------------------------
    def post_retried(self, path: str, body: dict):
        """For calls the server makes safe to repeat (renewing, logging by sequence number, finishing)."""
        return self._json(_retry(lambda: self.http.post(path, json=body)))

    def runners(self):
        return self.get('/runners')['runners']

    def runner(self, name: str):
        return self.get(f'/runners/{quote(name, safe="")}')['versions']

    def register_runner(self, body: dict):
        return self.post_retried('/runners', body)  # registering the same thing again returns it

    def jobs(self, status: str = '', limit: int = 50):
        return self.get('/jobs', status=status, limit=limit)['jobs']

    def job(self, job_id: int):
        return self.get(f'/jobs/{int(job_id)}')

    def submit_job(self, kind: str, runner: Optional[str] = None, params: Optional[dict] = None):
        return self.post('/jobs', {'kind': kind, 'runner': runner, 'params': params})

    def cancel_job(self, job_id: int):
        return self.post_retried(f'/jobs/{int(job_id)}/cancel', {})

    def job_log(self, job_id: int, attempt: int = 0, after: int = -1):
        return self.get(f'/jobs/{int(job_id)}/log', attempt=attempt, after=after)

    def claim(self):
        return self.post('/worker/claim', {})['job']  # not retried: a lost answer leaves a claim nobody renews

    def renew(self, job_id: int, token: str):
        return self.post_retried(f'/jobs/{int(job_id)}/renew', {'token': token})

    def add_log(self, job_id: int, token: str, seq: int, text: str):
        return self.post_retried(f'/jobs/{int(job_id)}/log', {'token': token, 'seq': seq, 'text': text})

    def finish(self, job_id: int, token: str, outcome: str, result: Optional[dict] = None, error: str = ''):
        return self.post_retried(f'/jobs/{int(job_id)}/finish', {'token': token, 'outcome': outcome,
                                                                 'result': result, 'error': error})
