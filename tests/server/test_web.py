"""The web pages (step 4): served by fw-server next to the API, plain files without a build step."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import struct
import zlib
from datetime import datetime, timedelta, timezone

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
    assert seen['home'] == '概览'
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
    assert seen['crumbs'].endswith('/train/images') and 'scratch × 1' in seen['image'] and seen['count'] == '1 / 2'
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
    assert seen['nav'] == ['概览', '数据集', '标注批次', '模型', '任务', '预测集', '操作记录']
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
    assert seen['nav'] == ['概览', '数据集', '标注批次', '模型', '任务', '预测集', '操作记录', '账号']
    assert seen['token'].startswith('fw_') and '只显示这一次' in seen['tokens'] and '现场笔记本' in seen['tokens']
    assert '账号名只能用' in seen['refused']
    admin = tokens['admin_name']
    assert [(e[1], e[2], e[3]) for e in reversed(seen['events'])] == [
        (admin, '新建账号', name), (admin, '改账号角色', name), (admin, '发凭据', name)]
    assert 'from=algo' in seen['events'][1][4] and 'to=field' in seen['events'][1][4]
    assert seen['token'] not in json.dumps(seen['events'])
    # the account it made works, as a field account: it reads, and is offered nothing it may not do
    assert seen['fieldNav'] == ['概览', '数据集', '标注批次', '模型', '预测集'] and seen['fieldWho'] == f'{name}（现场实施）'
    assert seen['fieldButtons'] == [] and '现场实施账号不能管理账号' in seen['fieldUsers']
    assert '现场笔记本' in seen['mine'] and '有效' in seen['mine']
    me = make_api(token=seen['token']).me()
    assert (me['name'], me['role']) == (name, 'field')


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_the_lists_are_browsed_by_site_and_line(service, tokens, api, tmp_path):
    from fw import ops
    from helpers import quiet, register_place
    from test_trainsets import make, pool, sample_files
    site = f'S{os.urandom(4).hex()}'
    register_place(site, 'TY')
    a = pool(api, tmp_path / 'a', {**sample_files('2026-09', 4), **sample_files('2026-10', 2)}, site=site, line='TX')
    b = pool(api, tmp_path / 'b', sample_files('2026-09', 2), site=site, line='TY')
    ts = make(api, f'T-{os.urandom(4).hex()}', [f'{a}@1'], test_dirs=['2026-10'])['dataset']
    model = f'M-{os.urandom(4).hex()}'
    api.create_model(model, site, 'TX', 'det', '')
    batch = ops.upload_batch(api, write_tree(tmp_path / 'day', {'x.png': real_png(64, 32, 5)}), b, '张三', echo=quiet)['name']
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'browse.cjs'), service.url + '/', tokens['algo'], site, a, b, ts, model,
                        str(shots)], capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert {a, b} <= set(seen['all']['pools']) and ts in seen['all']['snapshots']
    assert any(x.startswith(site) for x in seen['tree']) and seen['tree'][0].startswith('全部')
    assert sorted(seen['site']['pools']) == sorted([a, b]) and seen['site']['snapshots'] == [ts]
    assert seen['ty'] == {'pools': [b], 'snapshots': []} and f'site={site}' in seen['tyHash']
    assert seen['tx'] == {'pools': [a], 'snapshots': [ts]}           # the snapshot took its data from TX
    assert seen['newHref'] == f'#/dataset-new?site={site}&line=TX'    # a new pool starts on the line being browsed
    assert '这里没有符合的数据池' in seen['none']
    assert seen['navModels'] == f'#/models?site={site}&line=TX' and seen['models'] == [model] and seen['navOn'] == '模型'
    assert seen['navAfter'] == '#/datasets'
    assert batch in seen['batches']


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_line_is_seen_at_a_glance(service, tokens, api, make_api, make_worker, tmp_path):  # noqa: F811
    """The overview: every line with how much it holds; one line with its release, models and their last
    evaluation, snapshots, pools and the batches under way; said plainly when the line is unknown or the
    account may not see it."""
    from fw import ops
    from helpers import quiet, register_place
    from test_models import deteval_out, files_in, register, release
    from test_trainsets import make, pool, sample_files
    site = f'S{os.urandom(4).hex()}'
    register_place(site, 'TY')
    a = pool(api, tmp_path / 'a', {**sample_files('2026-09', 8), **sample_files('2026-10', 3)}, site=site, line='TX')
    b = pool(api, tmp_path / 'b', sample_files('2026-09', 2), site=site, line='TY')
    snap = make(api, f'T-{os.urandom(4).hex()}', [f'{a}@1'], test_dirs=['2026-10'])
    model = f'M-{os.urandom(4).hex()}'  # noqa: F811
    api.create_model(model, site, 'TX', 'det', '')
    register(api, model, snap, files_in(tmp_path))
    e = ops.register_eval(api, f'{model}@1', f'{snap["ref"]}:test', deteval=deteval_out(tmp_path), echo=quiet)
    rel = release(make_api('deploy'), f'{model}@1', '2026-10-20T10:00+08:00', site=site, line='TX')
    batch = ops.upload_batch(api, write_tree(tmp_path / 'day', {'x.png': real_png(64, 32, 9)}), a, '张三', echo=quiet)['name']
    done = ops.upload_batch(api, write_tree(tmp_path / 'old', {'y.png': real_png(64, 32, 10)}), a, '张三', echo=quiet)['name']
    api.batch_step(done, 'close', {})
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    w = make_worker()
    r = subprocess.run(['node', str(WEB_TESTS / 'overview.cjs'), service.url + '/', tokens['algo'],
                        w.http.headers['authorization'].split(' ', 1)[1], tokens['field'], site, str(shots)],
                       capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['nav'][0] == '概览'
    assert seen['lineRow'] == [f'{site} / TX', '1', '1', '1', '1', '1']   # pool, snapshot, model, batch under way, release
    assert rel['ref'] in seen['release'] and f'{model}@1' in seen['release']
    assert seen['models'] == [model, '@1', '0.7', '—', '—', '—', '']      # the numbers the report has
    assert seen['menu'] == [['查看版本', f'#/mv/{model}/1'], ['查看评估', f'#/eval/{e["id"]}'], ['复制版本号', None]]
    assert seen['data'] == [f'{a}@1', f'{snap["ref"]}']                       # the pool, then the snapshot
    assert seen['batches'] == [batch, '待提交复核']                            # the closed one is not under way
    assert seen['treeOn'].startswith('TX')
    assert seen['navModels'] == f'#/models?site={site}&line=TX'               # the lists keep the line
    assert seen['moreBatches'] == f'#/batches?site={site}&line=TX&view=all'
    assert '还没有登记过发布' in seen['ty'] and '这条产线还没有模型' in seen['ty'] and seen['tyPools'] == [f'{b}@1']
    assert '不是登记过的产线' in seen['unknown']
    assert seen['siteLines'] == [f'{site} / TX', f'{site} / TY']
    # a field account reads every part of it
    assert seen['field']['sections'] == ['models', 'data', 'batches'] and seen['field']['refused'] == []
    assert rel['ref'] in seen['field']['release'] and seen['field']['model'] == model
    assert '概览' not in seen['workerNav'] and '换一个有权限的账号' in seen['refused']


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_one_finds_the_way_around(service, tokens, api, tmp_path):
    """The home page is the overview; a site folds up and only the ticked sites and lines are shown, which the
    browser remembers and which hides nothing from the lists; every page says where it is and its back button
    leads to where one came from, or one step up on a page opened directly; table headers stay on one line."""
    from fw import ops
    from helpers import quiet, register_place
    from test_trainsets import pool, sample_files
    site, other = f'S{os.urandom(4).hex()}', f'S{os.urandom(4).hex()}'
    register_place(site, 'TY')
    register_place(other, 'TX')
    a = pool(api, tmp_path / 'a', sample_files('2026-09', 2), site=site, line='TX')
    b = pool(api, tmp_path / 'b', sample_files('2026-09', 2), site=other, line='TX')
    for line in ('TX', 'TY', 'TY'):
        api.create_model(f'M-{os.urandom(4).hex()}', site, line, 'det', '')
    batch = ops.upload_batch(api, write_tree(tmp_path / 'day', {'x.png': real_png(64, 32, 7)}), a, '张三', echo=quiet)['name']
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'nav.cjs'), service.url + '/', tokens['algo'], site, other, a, b, str(shots), batch],
                       capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['home'] == {'h1': '概览', 'on': '概览', 'crumbs': 0} and seen['brand'] == '#/'
    # folding
    assert (seen['linesBefore'], seen['linesFolded'], seen['linesFoldedAfterReload'], seen['linesOpened']) == (2, 0, 0, 2)
    assert seen['foldSays'] == 'false'
    # choosing: the line and the site that were unticked are gone from the tree and the table, and stay gone
    tx, ty = f'#/?site={site}&line=TX', f'#/?site={site}&line=TY'
    for shown in (seen['chosen'], seen['chosenAfterReload']):
        assert tx in shown['tree'] and ty not in shown['tree'] and not any(other in h for h in shown['tree'])
        assert f'{site} / TX' in shown['table'] and f'{site} / TY' not in shown['table']
        assert not any(x.startswith(other) for x in shown['table'])
    assert seen['lineOfHiddenSiteOff'] is True and '没显示' in seen['hiddenNote']
    assert not any(other in h for h in seen['listTree']) and seen['hiddenPoolListed'] is True   # the list hides nothing
    assert seen['listCrumbs'].replace(' ', '') == '概览/数据集'
    assert seen['headers'] and all(h == ['nowrap', True] for h in seen['headers'])
    assert seen['browsedHidden'] == [ty]
    assert (seen['siteModels'], seen['siteModelRows']) == ('3', 3)     # TY is not shown, its two models still count
    assert seen['backFromBatch'] == '/datasets'                         # not caught between the two addresses of a batch
    assert '没有要显示的产线' in seen['allLinesHidden'] and '选择' in seen['allLinesHidden']
    # the path and the way back
    assert seen['lineCrumbs'] == ['概览', '/', site, '/', 'TX']
    assert seen['crumbs'] == [['概览', '#/'], [site, f'#/?site={site}'], ['TX', tx],
                              ['数据集', f'#/datasets?site={site}&line=TX'], [a, None]]
    assert seen['onInDataset'] == '数据集'
    assert seen['versionCrumbs'] == ['概览', site, 'TX', '数据集', a, '@1']
    assert seen['backTo'] == f'/?site={site}&line=TX' and seen['homeHash'] == '/'
    assert seen['upTo'] == f'/datasets?site={site}&line=TX' and seen['upPools'] == [a]


def _deteval(tmp_path, numbers):
    """An aoi-deteval output whose test set has these numbers (mAP50, mAP50-95, P, R)."""
    d = tmp_path / f'deteval-{os.urandom(3).hex()}'
    summary = {'model_s0': {'weights': None, 'thresholds': {}, 'reached': {},
                            'test': {'model': 'model_s0', 'set': 'test', 'images': 3, **numbers}}}
    write_tree(d, {'summary.json': json.dumps(summary).encode(),
                   'run_info.json': json.dumps({'deteval_version': '0.3.0', 'metric_version': 3}).encode(),
                   'metrics.csv': b'model,set,mAP50\n', 'report.txt': b'table\n'})
    return d


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_the_overview_of_a_line_reads_at_a_glance_and_its_rows_have_a_menu(service, tokens, api, make_api, database, tmp_path):
    """SES / TX 贴片线: the release first, models with the evaluation each row shows, data, batches on the right;
    the ⋯ of a row by mouse and keys, its links, copying the reference (or showing it selected where the
    browser has no clipboard); a line with nothing on it; the layout at 1440, 1920 and a narrow window."""
    from fw import ops
    from fwserver import datasets, db
    from helpers import quiet
    from test_models import files_in, register, release
    from test_trainsets import make, pool, sample_files
    empty = 'ASM'
    with db.connect(database) as c:
        try:
            datasets.add_place(c, None, f'SES/{empty}', '组装线')
        except datasets.DatasetError:
            pass  # a second run on the same database
    tag = os.urandom(2).hex()
    data = pool(api, tmp_path / 'a', {**sample_files('2026-09', 6), **sample_files('2026-10', 3)}, name=f'SES-TX-{tag}')
    snap = make(api, f'SES-TX-det-{tag}', [f'{data}@1'], test_dirs=['2026-10'])
    test = f'SES-TX-test-{tag}'
    api.create_dataset(test, 'SES', 'TX', 'test', '')
    from test_trainsets import add_version
    add_version(api, tmp_path / 't', test, sample_files('2026-11', 2))
    detr, yolo, seg = 'SES-TX-detr', 'SES-TX-yolo', 'SES-TX-seg'
    for m in (detr, yolo, seg):
        api.create_model(m, 'SES', 'TX', 'det', '')
    for i in range(3):
        register(api, detr, snap, files_in(tmp_path, f'detr{i}'))
    register(api, yolo, snap, files_in(tmp_path, 'yolo'))
    register(api, seg, snap, files_in(tmp_path, 'seg'))
    where = f'{snap["ref"]}:test'
    ops.register_eval(api, f'{detr}@1', where, deteval=_deteval(tmp_path, {'mAP50': 0.9}), echo=quiet)
    shown = ops.register_eval(api, f'{detr}@2', where, echo=quiet,
                              deteval=_deteval(tmp_path, {'mAP50': 0.962, 'mAP50-95': 0.846, 'P': 0.951, 'R': 0.933}))
    ops.register_eval(api, f'{yolo}@1', where, echo=quiet,
                      deteval=_deteval(tmp_path, {'mAP50': 0.928, 'mAP50-95': 0.791, 'P': 0.94, 'R': 0.9}))
    rel = release(make_api('deploy'), f'{detr}@2', '2026-11-13T17:25+08:00', site='SES', line='TX')
    field = make_api('field')
    batches = []
    for step in ('review', 'open', 'approved'):
        name = ops.upload_batch(field, write_tree(tmp_path / f'b-{step}' / f'2026111{len(batches)}', {'x.png': real_png(64, 32, len(batches))}),
                                data, '张三', echo=quiet)['name']
        if step != 'open':
            field.batch_step(name, 'submit', {'reviewer': tokens['algo_name']})
        if step == 'approved':
            api.batch_step(name, 'approve', {'comment': '可以', 'revision': 1})
        batches.append(name)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'look.cjs'), service.url + '/', tokens['algo'], str(shots), empty],
                       capture_output=True, text=True, timeout=240, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['crumbs'] == ['概览', 'SES', 'TX'] and seen['navOn'] == '概览' and seen['lineOn'] == 'TX 贴片线'
    # the line's current release (other tests in the same run may deploy on SES / TX later than this one)
    current = api.current_release('SES', 'TX')
    at = datetime.fromisoformat(current['deployed_at']).astimezone(timezone(timedelta(hours=8)))
    mv_name, mv_number = current['model_version']['ref'].split('@')
    assert seen['release']['ref'] == current['ref'] and current['model_version']['ref'] in seen['release']['sub']
    assert f'{at.month}月{at.day}日 {at:%H:%M}' in seen['release']['sub']
    assert '已部署' in seen['release']['state'] and seen['release']['go'] == f'#/mv/{mv_name}/{mv_number}'
    assert rel['ref'].startswith('SES-TX-r')
    rows = {r[0]: r[1:] for r in seen['models']}
    assert rows[detr] == ['@2', '0.962', '0.846'] and rows[yolo] == ['@1', '0.928', '0.791']   # the evaluated version
    assert rows[seg] == ['@1', '—', '—']                                                       # no evaluation yet
    kinds = {name.split('@')[0]: kind for name, kind in seen['data']}
    assert kinds[data] == '数据池' and kinds[snap['dataset']] == '训练集快照' and kinds[test] == '测试集'
    mine = {name: state for name, state in seen['batches'] if name in batches}   # other tests add batches on SES/TX too
    assert mine == dict(zip(batches, ['待复核', '待提交复核', '复核通过']))
    for at in (seen['at1440'], seen['at1920']):
        assert at['releaseFirst'] and at['dataUnderModels'] and at['sideBySide']
    assert seen['at1920']['main'] > 1600 and seen['at1920']['fills'] > 1500                  # no 1480 cap
    assert seen['at1180']['stacked'] and not seen['at1180']['sideBySide']
    # the ⋯: hidden until the row is pointed at or focused; the numbers do not move
    assert seen['dotsHidden'] == '0' and seen['dotsHovered'] == ['1', '0'] and seen['numbersStay']
    assert seen['menu'] == [['查看版本', f'#/mv/{detr}/2'], ['查看评估', f'#/eval/{shown["id"]}'], ['复制版本号', None]]
    assert seen['expanded'] == 'true' and seen['closedByOutside'] and seen['menuNoEval'] == ['查看版本', '复制版本号']
    assert seen['tabToDots'] and seen['dotsFocused'] == '1'
    assert (seen['keyFirst'], seen['keySecond']) == ('查看版本', '查看评估') and seen['focusBack'] == f'{detr}@2 的更多操作'
    assert seen['toast'] == f'已复制 {detr}@2' and seen['clipboard'] == f'{detr}@2'
    assert seen['evalPage'] == [f'/eval/{shown["id"]}', f'评估 #{shown["id"]}']
    assert seen['versionPage'] == f'/mv/{detr}/2' and seen['releasePage'] == f'/mv/{mv_name}/{mv_number}'
    assert '还没有登记过发布' in seen['empty']['release'] and '这条产线还没有模型' in seen['empty']['models']
    assert seen['empty']['lineOn'] == f'{empty} 组装线'
    assert seen['fallback'] == {'value': f'{detr}@2', 'focused': True, 'selected': True,
                                'text': '浏览器不让这个页面写剪贴板，按 Ctrl+C 复制：'}
    assert seen['fallbackFocusBack']
    assert seen['byCommand'] == {'toast': f'已复制 {detr}@2', 'copied': f'{detr}@2', 'menu': 0, 'focusBack': True}
    assert seen['refsOneLine']
    assert seen['downloaded'] == 'classes.txt' and seen['downloadButtons'] == 0
    assert seen['calm'] == '0s' and seen['normal'] != '0s'


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_version_gets_its_label_in_the_browser(service, tokens, api, tmp_path):
    from fw import ops
    from helpers import quiet
    seed = int.from_bytes(os.urandom(2), 'big')
    name = f'W-{os.urandom(4).hex()}'
    api.create_dataset(name, 'SES', 'TX', 'pool', '')
    ops.import_folder(api, name, write_tree(tmp_path / 'one', {'a.png': real_png(64, 32, seed)}), echo=quiet)
    ops.import_folder(api, name, write_tree(tmp_path / 'two', {'a.png': real_png(64, 32, seed + 1)}), echo=quiet)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'labels.cjs'), service.url + '/', tokens['algo'], name, str(shots)],
                       capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['proposed'] == 'v0.1.0' and seen['next'] == 'v0.2.0'
    assert f'{name}@1' in seen['row'] and 'v0.1.0' in seen['row']
    assert f'已经用在 {name}@1 上' in seen['taken']
    assert seen['title'].startswith(f'{name}@1') and 'v0.1.0' in seen['title']
    assert api.version(name, 'v0.1.0')['number'] == 1 and api.labels(name)['labels'][0]['note'] == '冷启动'


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_what_only_the_command_line_did_is_done_in_the_browser(service, tokens, api, model, snapshot, database, tmp_path):  # noqa: F811
    """A test set of no line, registering and renaming a line, retiring a label, candidate and deprecated, every
    change of a large comparison; refused cases say why; a field account gets none of the buttons."""
    from fw import ops
    from helpers import quiet
    from test_models import files_in, register
    seed = int.from_bytes(os.urandom(2), 'big')
    labelled = f'W-{os.urandom(4).hex()}'
    api.create_dataset(labelled, 'SES', 'TX', 'pool', '')
    ops.import_folder(api, labelled, write_tree(tmp_path / 'one', {'a.png': real_png(8, 8, seed)}), echo=quiet)
    api.add_label(labelled, 1, 'v0.1.0', '冷启动')
    mv = register(api, model, snapshot, files_in(tmp_path, 'web-tags'))
    big = f'W-{os.urandom(4).hex()}'
    api.create_dataset(big, 'SES', 'TX', 'pool', '')
    ops.import_folder(api, big, write_tree(tmp_path / 'b1', {'old.png': real_png(3, 3, seed)}), echo=quiet)
    many = {f'd/{i:04d}.png': real_png(4, 4 + i // 256, seed + i) for i in range(510)}  # all different: no renames
    ops.import_folder(api, big, write_tree(tmp_path / 'b2', many), echo=quiet)
    site = f'S{os.urandom(3).hex().upper()}'
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'admin.cjs'), service.url + '/', tokens['algo'], tokens['admin'], tokens['field'],
                        str(shots), labelled, mv['ref'], big, site], capture_output=True, text=True, timeout=240, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['newHref'] == '#/dataset-new?site=SES&line=TX'
    assert seen['poolNoneOff'] and seen['poolAt'] == ['SES', 'TX'] and seen['noLine']
    made = api.dataset(f'T-{site}')
    assert made['kind'] == 'test' and made['site'] == '' and made['line'] == ''
    assert '已经存在' in seen['taken']
    assert seen['badCode'].strip() and f'{site}/L1' not in json.dumps(api.sites())
    lines = {(s['code'], x['code']): x['name'] for s in api.sites()['sites'] for x in s['lines']}
    assert lines[(site, 'L1')] == '一号线' and 'L1 一号线' in seen['tree']
    assert seen['algoRetire'] == 0 and '原因' in seen['noReason']
    assert '已撤销' in seen['retired'] and '起错版本了' in seen['retired']
    assert [x['label'] for x in api.labels(labelled)['labels'] if x['retired']] == ['v0.1.0']
    assert seen['metricsShown']
    assert '候选' in seen['candidate'] and '候选' in seen['both'] and '弃用' in seen['both']
    assert seen['history'] == ['标成「候选」', '标成「弃用」：漏检多', '去掉「候选」']
    assert api.model_version(model, mv['number'])['tags'] == ['deprecated'] and '弃用' in seen['listTags']
    assert seen['firstRows'] == 500 and seen['more'] == '全部列出（510 个）' and seen['allRows'] == 510
    assert seen['lines'] == {'新增': 510, '删除': 1} and seen['firstLine'] == '新增\td/0000.png'
    assert seen['file'] == f'{big}-1_{big}-2.tsv'
    page = seen['field'].pop('newPage')
    assert seen['field'] == {'rename': 0, 'register': 0, 'manage': 0, 'tags': 0, 'newDataset': 0}
    assert '不能新建数据集' in page[0] and page[1] == 0                      # said on opening, not on submitting


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_pool_is_filled_from_folders_the_way_they_were_collected(service, tokens, api, tmp_path):
    """A new pool starts from its line; two folders dragged in at once, or the folders inside a "pool" folder
    without that level, each go in under their own names; a loose file there is refused; the whole-version
    upload says what it makes of a "pool" folder and of folders dragged in; a field account gets no new-pool links."""
    seed = int.from_bytes(os.urandom(2), 'big')
    img = lambda k: real_png(16, 8 + k // 256, seed + k)  # noqa: E731  (all different)
    parent = write_tree(tmp_path / 'p' / 'pool', {'20261101/a.png': img(1), '20261101/a.json': label('scratch'),
                                                  '20261102/b.png': img(2)})
    day3 = write_tree(tmp_path / 'd' / '20261103', {'c.png': img(3)})
    day4 = write_tree(tmp_path / 'd' / '20261104', {'d.png': img(4), 'night/e.png': img(5)})
    loose = write_tree(tmp_path / 'l' / 'pool', {'x.png': img(6), '20261105/f.png': img(7)})
    pool = f'W-{os.urandom(4).hex()}'
    api.create_dataset(pool, 'SES', 'TX', 'pool', '')
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'folders.cjs'), service.url + '/', tokens['algo'], pool, str(shots),
                        str(parent), str(day3), str(day4), str(loose), tokens['field']],
                       capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['overviewNew'] == '#/dataset-new?site=SES&line=TX' and '#/dataset-new?site=SES&line=TX' in seen['placesNew']
    assert seen['fieldOverview'] == {'newData': 0, 'links': 0} and seen['fieldPlaces'] == 0  # a field account cannot start one
    two = seen['two']
    assert two['box'] == '2 个目录 · 3 个文件' and '20261103/、20261104/' in two['dirs']
    assert two['target'] == ['', True] and two['inside']
    assert '新增 20261103/' in two['plan']['text'] and '新增 20261104/' in two['plan']['text'] and two['plan']['at'] == f'/v/{pool}/1'
    inside = seen['inside']
    assert inside['target'] == 'pool' and '只加 pool/ 里面的子目录' in inside['label']
    assert '20261101/、20261102/' in inside['dirs'] and inside['targetAfter'] == ['', True]
    assert inside['plan']['at'] == f'/v/{pool}/2' and 'pool/' not in inside['plan']['text']
    assert '最上层有 1 个文件不在子目录里' in seen['loose'] and 'x.png' in seen['loose']
    assert '20261101/、20261102/目录原样保留，pool 这一层不进版本' in seen['wholeParent']
    assert seen['wholeTwo'].startswith('这 2 个目录（20261103/、20261104/）成为新版本的全部内容')
    versions = {v['number']: v for v in api.versions(pool)}
    assert [(o['op'], o['dir']) for o in versions[1]['ops']] == [('add', '20261103'), ('add', '20261104')]
    assert [(o['op'], o['dir']) for o in versions[2]['ops']] == [('add', '20261101'), ('add', '20261102')]
    assert versions[2]['file_count'] == 6 and versions[1]['source'] == '网页上传新增目录 20261103、20261104'


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
    assert seen['reading'] == '正在读取目录…' and seen['picked'] == {'text': '20261104/ · 3 个文件', 'reading': 0}
    assert '要是目录' in seen['droppedFile'] and seen['dropped'] == '20261104/ · 4 个文件'
    assert '一次只能拖一个目录' in seen['droppedTwo']
    assert seen['cancelled'] == {'text': seen['droppedTwo'], 'reading': 0}   # choosing, then cancelling, mid-read
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
