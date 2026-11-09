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
from test_evals import gpu  # noqa: F401  (fixture)
from test_jobs import make_worker  # noqa: F401
from test_models import model, snapshot  # noqa: F401


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
    assert seen['hashchanges'] == 1  # one arrow key, one page: not one per image page seen before
    assert '新增 1' in seen['diff'] and '修改 1' in seen['diff']
    assert seen['errors'] == []


@pytest.mark.skipif(not node_with_playwright() or os.name != 'posix', reason='Playwright for node is not installed')
def test_a_walk_through_models_jobs_and_predictions(service, tokens, api, gpu, model, snapshot, tmp_path):  # noqa: F811
    from test_evals import add_runner, trained
    from fw import worker
    from helpers import quiet
    seed = int.from_bytes(os.urandom(2), 'big')
    files = {f'A/img{i:03}.png': real_png(200, 100, seed + i) for i in range(2)}
    files.update({f'A/img{i:03}.json': label('scratch') for i in range(2)})
    files['A/new.png'] = real_png(200, 100, seed + 5)
    files['B/b.png'] = real_png(200, 100, seed + 6)
    data = f'{pool(api, tmp_path, files)}@1'
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)

    def run(job_id):
        assert worker.run_worker(gpu, quiet, once=True) == 1
        j = api.job(job_id)
        assert j['status'] == 'done', j['error']
        return j

    for _ in range(2):  # two reports to compare
        run(api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': data, 'dirs': ['A']})['id'])
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)

    def walk(phase, *args):
        p = subprocess.run(['node', str(WEB_TESTS / 'models.cjs'), service.url + '/', tokens['algo'], phase, mv['ref'],
                           str(shots), *args], capture_output=True, text=True, timeout=120, cwd=WEB_TESTS)
        assert p.returncode == 0, p.stdout + p.stderr
        return json.loads(p.stdout.strip().splitlines()[-1])

    seen = walk('submit', data, r['ref'])  # other tests' runners are listed too
    assert seen['nav'] == ['数据集', '模型', '任务', '预测集']
    assert snapshot['ref'] in seen['info'] and 'scratch、dent' in seen['info']
    assert len(seen['evals']) == 2
    assert 'nope' in seen['refused']
    job_id = int(seen['job'].split('/')[-1])
    assert seen['job'] == f'/job/{job_id}' and '排队' in seen['jobText'] and data in seen['jobText'] and seen['cancel']
    job = api.job(job_id)
    assert job['kind'] == 'predict' and job['params']['data']['dirs'] == ['A'] and job['params']['conf'] == 0.1
    assert job['runner'] == r['ref']
    assert job['submitted_by'] == tokens['algo_name'] and job['status'] == 'queued', job
    set_id = run(job_id)['result']['prediction_set']

    seen = walk('view', str(set_id))
    assert 'images' in seen['compare'] and 'boxes' in seen['compare']
    assert seen['jobs'][0] == '完成' and len(seen['jobs']) >= 3
    assert f'预测集 #{set_id}' in seen['result'] and '推理了' in seen['log']
    assert [t.split()[-1] for t in seen['tiles']] == ['A/img000.png', 'A/img001.png', 'A/new.png']
    assert all(t.startswith('1 框') for t in seen['tiles'])
    assert '3 个框，3 张图有框，0 张没有' in seen['summary']
    assert 'scratch × 1' in seen['image'] and '预测（虚线' in seen['image']
    assert seen['imageHash'].startswith('/img/') and f'pred={set_id}' in seen['imageHash']
    assert 'A%2Fimg001.png' in seen['next'] and f'pred={set_id}' in seen['next']
    assert seen['errors'] == []
