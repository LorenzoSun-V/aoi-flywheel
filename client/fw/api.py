"""HTTP client of the platform service. Every machine talks to the service through this one port."""
from __future__ import annotations

import hashlib
import os
import time
from typing import BinaryIO, List, Optional, Tuple

import httpx

from .contract import CHUNK

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
