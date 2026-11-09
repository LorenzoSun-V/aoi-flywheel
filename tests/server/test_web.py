"""The web pages (step 4): served by fw-server next to the API, plain files without a build step."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import struct
import zlib

import httpx
import pytest

from test_trainsets import label, pool


def real_png(w: int, h: int, seed: int) -> bytes:
    """A PNG a browser can show: w x h, one gray level per row, seed makes the content unique."""
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    rows = b''.join(b'\x00' + bytes([(y * 7 + seed) % 256]) * (w * 3) for y in range(h))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


WEB_TESTS = Path(__file__).resolve().parent.parent / 'web'


def test_the_pages_are_served_with_a_strict_policy(service):
    r = httpx.get(service.url + '/')
    assert r.status_code == 200 and r.headers['content-type'].startswith('text/html')
    assert '/web/app.js' in r.text
    policy = r.headers['content-security-policy']
    assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy and 'unsafe-inline' not in policy
    assert r.headers['x-content-type-options'] == 'nosniff' and r.headers['cache-control'] == 'no-cache'
    js = httpx.get(service.url + '/web/app.js')
    assert js.status_code == 200 and js.headers['content-type'].startswith('text/javascript')
    assert httpx.get(service.url + '/web/style.css').headers['content-type'].startswith('text/css')
    for bad in ('/web/index.html', '/web/nope.js', '/web/..%2Fapp.py', '/web/%2E%2E/app.py'):
        assert httpx.get(service.url + bad).status_code == 404, bad
    assert 'innerHTML' not in js.text and 'insertAdjacentHTML' not in js.text  # text only, never markup


def node_with_playwright() -> bool:
    node = shutil.which('node')
    if not node:
        return False
    return subprocess.run([node, '-e', "require('playwright')"], cwd=WEB_TESTS, capture_output=True).returncode == 0


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_the_page_helpers():
    r = subprocess.run(['node', '--test', str(WEB_TESTS / 'helpers.test.mjs')], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_walk_through_the_pages(service, tokens, api, tmp_path):
    from test_trainsets import add_version
    seed = int.from_bytes(os.urandom(2), 'big')
    files = {f'A/img{i:03}.png': real_png(200, 100, seed + i) for i in range(4)}
    files.update({f'A/img{i:03}.json': label('scratch') for i in range(3)})  # img003: no label
    files['B/b.png'] = real_png(200, 100, seed + 10)
    name = pool(api, tmp_path, files)
    files['A/img000.json'] = label('scratch', 'dent')
    files['C/c.png'] = real_png(200, 100, seed + 11)
    add_version(api, tmp_path, name, files)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'smoke.cjs'), service.url + '/', tokens['algo'], name, str(shots)],
                       capture_output=True, text=True, timeout=120, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert '凭据不对' in seen['wrong']
    assert name in seen['datasets'] and tokens['algo_name'] in seen['who']
    assert seen['versions'] == [f'{name}@2', f'{name}@1']
    assert [f.split()[1] for f in seen['folders']] == ['A', 'B', 'C']
    assert seen['tiles'] == 4 and seen['labeled'] == 3
    assert 'scratch × 1' in seen['image'] and 'dent × 1' in seen['image'] and '未确认' in seen['image']
    assert seen['canvas'] == [200, 100]
    assert '新增 1' in seen['diff'] and '修改 1' in seen['diff']
    assert seen['errors'] == []
