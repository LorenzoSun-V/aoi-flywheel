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


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_snapshot_is_shown_in_the_folders_fw_get_makes(service, tokens, api, tmp_path):
    from test_trainsets import CLASSES, make
    seed = int.from_bytes(os.urandom(2), 'big')
    files = {}
    for i, (folder, labelled) in enumerate([('train', True), ('train', True), ('neg_train', False), ('val', True),
                                            ('neg_val', False), ('field_1108', True)]):
        files[f'{folder}/images/{folder}{i}.png'] = real_png(200, 100, seed + i)
        if labelled:
            files[f'{folder}/images/{folder}{i}.json'] = label('scratch')
    src = pool(api, tmp_path, files)
    split = {f'SES/TX/{p}': 'train' if 'train' in p else 'val' if 'val' in p else 'test'
             for p in files if p.endswith('.png')}
    name = f'T-{os.urandom(4).hex()}'
    make(api, name, [f'{src}@1'], CLASSES, split=split)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'snapshot.cjs'), service.url + '/', tokens['algo'], name, str(shots)],
                       capture_output=True, text=True, timeout=120, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['top'] == ['📁 neg_train 1', '📁 test 1', '📁 train 2', '📁 val 2']  # not images/<划分>/<现场>/…
    assert 'fw get 默认取回的一样' in seen['note']
    assert seen['train'] == ['📁 images 2']
    assert seen['trainTiles'] == ['train0.png', 'train1.png'] and seen['trainLabeled'] == 2
    assert seen['crumbs'].endswith('/ train / images') and 'scratch × 1' in seen['image'] and seen['count'] == '1 / 2'
    assert seen['negTiles'] == ['neg_train2.png'] and seen['valTiles'] == ['neg_val4.png', 'val3.png']
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
    assert seen['nav'] == ['数据集', '标注批次', '模型', '任务', '预测集', '操作记录']
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


def write_tree(root: Path, files: dict) -> Path:
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def test_the_folder_check_is_the_one_of_fw_dataset_import(service, tokens, tmp_path):
    from fw.checks import scan_folder
    files = {'A/a.png': b'1', 'A/a.bmp': b'2', 'A/a.json': label('scratch'), 'A/b.png': b'3', 'B/b.png': b'4',
             'B/x.json': b'{}', 'notes.docx': b'5', 'A/Thumbs.db': b'6', 'classes.txt': b'scratch\n',
             'C/images/c.png': b'7', 'C/labels/c.txt': b'0 0.5 0.5 0.1 0.1\n', 'D/x.png': b'8', 'd/x.png': b'9'}
    root = write_tree(tmp_path / 'top', files)
    local = scan_folder(root)
    r = httpx.post(service.url + '/api/v1/folder-check', headers={'Authorization': f'Bearer {tokens["algo"]}'},
                   json={'paths': sorted(files)}, timeout=30).json()
    assert r['junk'] == ['A/Thumbs.db'] and r['files'] == len(files) - 1
    for key in ('images', 'labels', 'meta', 'others', 'bad_paths', 'collisions', 'same_stem_images',
                'unlabeled_images', 'orphan_labels', 'same_name_in_dirs'):
        assert r[key]['count'] == len(getattr(local, key)), key
        assert sorted(map(str, r[key]['examples'])) == sorted(map(str, getattr(local, key))), key
    assert r['reasons'] == local.blocking(False, False) and len(r['reasons']) == 3
    allowed = httpx.post(service.url + '/api/v1/folder-check', headers={'Authorization': f'Bearer {tokens["algo"]}'},
                         json={'paths': sorted(files), 'allow_other_files': True, 'force': True}).json()
    assert allowed['reasons'] == local.blocking(True, True) == ['有只差大小写、或同名的文件和目录，在 Windows 上会互相覆盖，先改名']

    def post(paths):
        return httpx.post(service.url + '/api/v1/folder-check', json={'paths': paths}, timeout=60,
                          headers={'Authorization': f'Bearer {tokens["algo"]}'})

    assert post(['a/' + 'x' * 1025]).status_code == 422  # one path longer than a path may be
    assert post([f'{i}.png' for i in range(300_001)]).status_code == 422  # more than the web page checks


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_accounts_and_the_record_in_the_browser(service, tokens, api, make_api, tmp_path):
    from fw import ops
    from helpers import quiet
    name = f'web-{os.urandom(4).hex()}'
    dataset = f'W-{os.urandom(4).hex()}'
    api.create_dataset(dataset, 'SES', 'TX', 'pool', '')
    ops.import_folder(api, dataset, write_tree(tmp_path / 'd', {'a/x.png': real_png(64, 32, 7)}), echo=quiet)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'accounts.cjs'), service.url + '/', tokens['admin'], name, dataset,
                        str(shots)], capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['nav'] == ['数据集', '标注批次', '模型', '任务', '预测集', '操作记录', '账号']
    assert seen['token'].startswith('fw_') and '只显示这一次' in seen['tokens'] and '现场笔记本' in seen['tokens']
    assert '账号名只能用' in seen['refused']
    admin = tokens['admin_name']
    assert [(e[1], e[2], e[3]) for e in reversed(seen['events'])] == [
        (admin, '新建账号', name), (admin, '改账号角色', name), (admin, '发凭据', name)]
    assert 'from=algo' in seen['events'][1][4] and 'to=field' in seen['events'][1][4]
    assert seen['token'] not in json.dumps(seen['events'])
    # the account it made works, as a field account: it reads, and is offered nothing it may not do
    assert seen['fieldNav'] == ['数据集', '标注批次', '模型', '预测集'] and seen['fieldWho'] == f'{name}（现场实施）'
    assert seen['fieldButtons'] == [] and '现场实施账号不能管理账号' in seen['fieldUsers']
    assert '现场笔记本' in seen['mine'] and '有效' in seen['mine']
    me = make_api(token=seen['token']).me()
    assert (me['name'], me['role']) == (name, 'field')


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_folder_added_and_removed_from_the_browser(service, tokens, api, tmp_path):
    from fw import ops
    from helpers import quiet
    seed = int.from_bytes(os.urandom(2), 'big')
    same = real_png(64, 32, seed)
    base = write_tree(tmp_path / 'base', {'20261101/a.png': same, '20261101/a.json': label('scratch'),
                                          '20261102/b.png': real_png(64, 32, seed + 1)})
    day = write_tree(tmp_path / '20261103', {'c.png': real_png(64, 32, seed + 2), 'c.json': label('dent'),
                                             'a_again.png': same, 'a_again.json': label('scratch', 'dent')})
    name = f'W-{os.urandom(4).hex()}'
    api.create_dataset(name, 'SES', 'TX', 'pool', '')
    v1 = ops.import_folder(api, name, base, echo=quiet)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'change.cjs'), service.url + '/', tokens['algo'], name, str(shots),
                        str(day), '20261101'], capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['target'] == '20261103'
    assert '新增 20261103/：4 个文件' in seen['plan'] and f'新版本 {name}@2：7 个文件' in seen['plan']
    assert '和池里已有图片内容完全相同' in seen['plan'] and '20261103/a_again.png = 20261101/a.png' in seen['plan']
    assert '图片相同、标注不一样' in seen['plan'] and '（2 个目标） 和 20261101/a.png（1 个目标）' in seen['plan']
    assert seen['added'] == f'/v/{name}/2'
    assert '池里已有 20261103/' in seen['again'] and seen['offered'] == 0
    assert '去掉 20261101/：2 个文件' in seen['removal'] and f'新版本 {name}@3：5 个文件' in seen['removal']
    assert seen['removed'] == f'/v/{name}/3'
    assert '新增 20261103/' in seen['versions'] and '去掉 20261101/' in seen['versions'] and '整份导入' in seen['versions']
    v2, v3 = api.version(name, 2), api.version(name, 3)
    assert v2['parent']['id'] == v1['id'] and v2['note'] == '第三天' and v2['source'] == '网页上传新增目录 20261103'
    assert [o['op'] for o in v2['ops']] == ['add'] and [o['op'] for o in v3['ops']] == ['remove']
    assert v3['note'] == '拍糊了' and v3['file_count'] == 5


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_folder_uploaded_from_the_browser(service, tokens, api, tmp_path):
    from fw import ops
    from helpers import quiet
    seed = int.from_bytes(os.urandom(2), 'big')
    first = {'A/img0.png': real_png(64, 32, seed), 'A/img0.json': label('scratch'),
             'A/img1.png': real_png(64, 32, seed + 1), 'A/中文 名.png': real_png(64, 32, seed + 2),
             'B/b.png': real_png(64, 32, seed + 3), 'readme.docx': b'notes',
             'A/Thumbs.db': b'junk'}
    second = {k: v for k, v in first.items() if k not in ('B/b.png', 'A/Thumbs.db')}
    second['A/img0.json'] = label('scratch', 'dent')
    second['C/new.png'] = real_png(64, 32, seed + 4)
    one = write_tree(tmp_path / 'one', first)
    two = write_tree(tmp_path / 'two', second)
    name = f'W-{os.urandom(4).hex()}'
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'upload.cjs'), service.url + '/', tokens['algo'], name, str(shots),
                        str(one), str(two)], capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert '图片和标注以外的文件' in seen['refused'] and '系统文件 1 个不入库' in seen['report']
    assert f'这是 {name} 的第一个版本' in seen['plan'] and '6 个文件' in seen['plan']
    assert seen['first'] == f'/v/{name}/1'
    assert '新增 1，删除 1，修改 1' in seen['plan2'] and '会删掉' in seen['plan2'] and 'B/b.png' in seen['plan2']
    assert seen['second'] == f'/v/{name}/2'
    assert seen['errors'] == []

    v1, v2 = api.version(name, 1), api.version(name, 2)
    assert v1['note'] == '第一批' and v1['source'] == '网页上传 one' and v2['parent']['ref'] == v1['ref']
    d = api.dataset(name)
    assert (d['site'], d['line'], d['kind']) == ('SES', 'TX', 'pool')
    # the same version fw dataset import makes of the same folder: same manifest, every file readable
    other = f'{name}-cli'
    api.create_dataset(other, 'SES', 'TX', 'pool', '')
    for folder, v in ((one, v1), (two, v2)):
        mine = ops.import_folder(api, other, folder, allow_other_files=True, echo=quiet)
        assert mine['manifest_sha256'] == v['manifest_sha256']


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_an_older_folder_check_answer_is_not_used(service, tokens, api, tmp_path):
    """Folder A's check answers late, after folder B was picked: B (an other file, no check box) must not be
    offered for upload with A's report (review of #17)."""
    seed = int.from_bytes(os.urandom(2), 'big')
    a = write_tree(tmp_path / 'a', {'x/a.png': real_png(32, 32, seed)})
    b = write_tree(tmp_path / 'b', {'y/b.png': real_png(32, 32, seed + 1), 'readme.docx': b'notes',
                                    'y/Thumbs.db': b'junk'})
    name = f'R-{os.urandom(4).hex()}'
    api.create_dataset(name, 'SES', 'TX', 'pool', '')
    r = subprocess.run(['node', str(WEB_TESTS / 'race.cjs'), service.url + '/', tokens['algo'], name, str(a), str(b)],
                       capture_output=True, text=True, timeout=120, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert not seen['go'] and seen['puts'] == 0
    assert '图片和标注以外的文件' in seen['shown'] and '2 个文件' in seen['shown']


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_batch_from_upload_to_the_pool_in_the_browser(service, tokens, api, tmp_path):
    from fw import ops
    from helpers import quiet
    seed = int.from_bytes(os.urandom(2), 'big')
    a, b = real_png(64, 32, seed), real_png(64, 32, seed + 1)
    base = write_tree(tmp_path / 'base', {'20261101/x.png': real_png(64, 32, seed + 2)})
    day = write_tree(tmp_path / 'one' / '20261104', {'a.png': a, 'a.json': label('scratch'), 'b.png': b})
    fixed = write_tree(tmp_path / 'two' / '20261104', {'a.png': a, 'a.json': label('scratch'), 'b.png': b,
                                                       'b.json': label('dent')})
    pool = f'W-{os.urandom(4).hex()}'
    name = f'B-{os.urandom(4).hex()}'
    api.create_dataset(pool, 'SES', 'TX', 'pool', '')
    ops.import_folder(api, pool, base, echo=quiet)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'batch.cjs'), service.url + '/', tokens['field'], tokens['algo'],
                        tokens['algo_name'], pool, name, str(shots), str(day), str(fixed)],
                       capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert '标注批次' in seen['fieldNav'] and '操作记录' not in seen['fieldNav']
    assert seen['dir'] == '20261104' and '标注人姓名' in seen['noLabeler']
    assert seen['uploaded'] == '待复核' and name in seen['todo']
    assert '退回意见' in seen['noComment']
    assert 'b.png 漏标' in seen['rejected'] and seen['sameReviewer'] == tokens['algo_name']
    assert seen['prelabel'] == 1 and '预标注：让模型先给第 1 版的图画框' in seen['rejected']
    assert '标注有变化的图片 1 张：加框 1' in seen['changes'] and seen['resubmitted'] == '待复核'
    assert '新增 20261104/：4 个文件' in seen['plan'] and f'建 {pool}@2' in seen['plan']
    assert f'已入库为 {pool}@2' in seen['done'] and seen['buttons'] == 0
    b = api.batch(name)
    assert b['status'] == 'imported' and [r['labeler'] for r in b['revisions']] == ['张三', '李四']
    assert [s['step'] for s in b['steps']] == ['upload', 'submit', 'reject', 'upload', 'submit', 'approve', 'import']
    assert api.version(pool, 2)['source'] == f'batch:{name}@2'
