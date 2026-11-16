"""The web pages (step 4): served by fw-server next to the API, plain files without a build step."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import struct
import zlib
from datetime import datetime, timedelta, timezone

import helpers
import httpx
import pytest

from test_trainsets import label, pool
from test_evals import gpu  # noqa: F401  (fixture)
from test_jobs import fake_conda, make_worker  # noqa: F401
from test_models import model, snapshot  # noqa: F401


def real_png(w: int, h: int, seed: int) -> bytes:
    """A PNG a browser can show: w x h, one gray level per row, seed makes the content unique."""
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    rows = b''.join(b'\x00' + bytes([(y * 7 + seed) % 256]) * (w * 3) for y in range(h))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


WEB_TESTS = Path(__file__).resolve().parent.parent / 'web'


def test_the_pages_never_open_the_browsers_own_boxes():
    """Questions before a change are asked on the page itself (askFirst), never in window.confirm, prompt or alert."""
    import re
    code = (Path(__file__).resolve().parents[2] / 'server' / 'fwserver' / 'web' / 'app.js').read_text(encoding='utf-8')
    assert not re.findall(r'\b(?:window\.)?(?:confirm|prompt|alert)\s*\(', code)



def test_every_recorded_operation_is_put_in_words_on_the_record_page():
    """The record page says each kind of event in a sentence (eventText) and names each group in its filter: an
    action added to events.ACTIONS without them would show its raw fields."""
    import re
    from fwserver import events
    code = (Path(__file__).resolve().parents[2] / 'server' / 'fwserver' / 'web' / 'app.js').read_text(encoding='utf-8')
    said = code[code.index('export function eventText'):code.index('export function eventHref')]
    assert sorted(set(events.ACTIONS) - set(re.findall(r"case '([a-z_]+\.[a-z_]+)'", said))) == []
    groups = code[code.index('export const EVENT_GROUPS'):code.index('export function eventText')]
    assert sorted({a.split('.')[0] for a in events.ACTIONS} - set(re.findall(r'\b([a-z_]+):', groups))) == []

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
    assert '凭据无效' in seen['wrong']
    assert seen['home'] == '概览'
    assert name in seen['datasets'] and tokens['algo_name'] in seen['who']
    assert seen['versions'] == ['@2', '@1']
    assert [f.split()[0] for f in seen['folders']] == ['A', 'B', 'C']
    assert seen['tiles'] == 4 and seen['labeled'] == 3
    # the tiles show thumbnails made by the server, not the images themselves (a field BMP is a few MB)
    assert seen['tileFetches'].count('thumb') == 4 and 'object' not in seen['tileFetches'][1:]
    assert seen['tileSizes'] == [[200, 100]] * 4  # never enlarged
    assert 'scratch × 1' in seen['image'] and 'dent × 1' in seen['image'] and '未确认' in seen['image']
    assert seen['canvas'] == [200, 100]
    assert seen['hashchanges'] == 1  # one arrow key, one page: not one per image page seen before
    assert '新增 1' in seen['diff'] and '修改 1' in seen['diff']
    assert seen['diffTitle'].startswith(f'{name}@1 → {name}@2') and '删除 1' in seen['diffBack']
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
    assert seen['top'] == ['neg_train 1', 'test 1', 'train 2', 'val 2']  # not images/<划分>/<现场>/…
    assert 'fw get 默认取回的结构一致' in seen['note']  # behind the ⓘ of the title
    assert seen['train'] == ['images 2']
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
    assert seen['nav'] == ['概览', '数据集', '标注批次', '模型', '发布记录', '任务', '预测集', '操作记录']
    assert snapshot['ref'] in seen['info'] and 'scratch、dent' in seen['info']
    assert len(seen['evals']) == 2
    assert seen['refused'] == '最低分数 conf 须在 0 和 1 之间，例如 0.1。'
    assert seen['evalData']['value'] == f'{snapshot["ref"]}:test' and not seen['evalData']['versionShown']
    assert seen['evalData']['groups'][0] == '训练集快照' and '数据池' in seen['evalData']['groups']  # other tests' sets too
    assert seen['poolOffered'] and seen['dirs'] == ['A/（3 张图片）', 'B/（1 张图片）']   # picked, not typed
    assert seen['version'] == ['1', [f'{data}（最新）']]
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
    assert '共 3 个框；3 张图片有框，0 张无框' in seen['summary']
    assert 'scratch × 1' in seen['image'] and '预测（虚线' in seen['image']
    assert seen['imageHash'].startswith('/img/') and f'pred={set_id}' in seen['imageHash']
    assert 'A%2Fimg001.png' in seen['next'] and f'pred={set_id}' in seen['next']
    assert seen['errors'] == []



@pytest.mark.skipif(not node_with_playwright() or os.name != 'posix', reason='Playwright for node is not installed')
def test_jobs_and_prediction_sets_are_browsed_by_line_and_sorted(service, tokens, api, gpu, snapshot, tmp_path):  # noqa: F811
    """The jobs and prediction sets of a line are those of its models (a check of a runner is under 「全部」 only,
    in the last group); under 「全部」 and under a site the rows are grouped by line; the jobs list shows the newest
    100 and loads older ones on request; a click on a header sorts the rows shown (within each group), a second
    click the other way, the order kept when more rows come in."""
    from test_evals import add_runner, trained
    from fw import worker
    from helpers import quiet, register_place
    site = f'J{os.urandom(3).hex().upper()}'
    seed = int.from_bytes(os.urandom(2), 'big')
    files = {f'A/img{i:03}.png': real_png(200, 100, seed + i) for i in range(2)}
    files.update({f'A/img{i:03}.json': label('scratch') for i in range(2)})
    data = f'{pool(api, tmp_path, files)}@1'
    r = add_runner(gpu)
    models, done = {}, []
    for line, kind in (('L1', 'predict'), ('L2', 'eval')):
        register_place(site, line)
        name = f'{line}-{os.urandom(3).hex()}'
        api.create_model(name, site, line, 'det', '')
        models[line] = name
        job = api.submit_job(kind, runner=r['runner'], params={'model': trained(api, name, snapshot, tmp_path)['ref'],
                                                               'data': data, 'dirs': ['A']})
        assert worker.run_worker(gpu, quiet, once=True) == 1
        done.append(api.job(job['id']))
        assert done[-1]['status'] == 'done', done[-1]['error']
    set_id = done[0]['result']['prediction_set']
    other = f'L1-z{os.urandom(3).hex()}'                                   # a second model on L1, to sort (after the first)
    api.create_model(other, site, 'L1', 'det', '')
    cancelled = [api.submit_job('check', runner=r['runner'])['id'] for _ in range(101)]
    for job_id in cancelled:
        api.cancel_job(job_id)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(['node', str(WEB_TESTS / 'jobs.cjs'), service.url + '/', tokens['algo'], site, str(shots)],
                         capture_output=True, text=True, timeout=240, cwd=WEB_TESTS)
    assert out.returncode == 0, out.stdout + out.stderr
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    predicted, evaluated = (f'任务 {j["id"]}' for j in done)
    assert seen['line']['ids'] == [predicted] and seen['line']['here'].startswith('L1')
    assert seen['line']['path'] == f'概览/{site}/L1/任务' and seen['line']['scope'] == f'范围：{site} / L1'
    assert not seen['line']['search']                                  # a list of jobs has no names to search
    assert seen['line']['groups'] == [[None, [predicted]]]
    # by line under a site, L1 first; a check of a runner under 「全部」 only, in the last group
    assert seen['site']['ids'] == [predicted, evaluated]
    assert seen['site']['groups'] == [[f'{site} / L1', [predicted]], [f'{site} / L2', [evaluated]]]
    everything = dict(seen['all']['groups'])
    assert everything[f'{site} / L1'] == [predicted] and everything[f'{site} / L2'] == [evaluated]
    assert f'site={site}' in seen['done']['hash'] and seen['done']['ids'] == [predicted, evaluated]
    assert seen['predictions']['ids'] == [f'#{set_id}'] and seen['predictions']['path'] == f'概览/{site}/L1/预测集'
    assert dict(seen['allPredictions'])[f'{site} / L1'] == [f'#{set_id}']
    assert seen['noPredictions'] == '没有符合条件的预测集。'
    ours = [f'任务 {i}' for i in cancelled]
    assert seen['first'] == {'rows': 100, 'more': True}               # the newest 100: ours but the oldest
    assert seen['up']['ids'] == ours[1:] and seen['up']['sorted'] == [['编号', 'ascending']]
    assert seen['down']['ids'] == ours[1:][::-1] and seen['down']['sorted'] == [['编号', 'descending']]
    shown = seen['more']['ids']
    assert len(shown) >= 101 and shown[-101:] == ours and seen['more']['sorted'] == [['编号', 'ascending']]
    for _, rows in seen['more']['groups']:                                 # the older rows sorted in with the rest
        assert rows == sorted(rows, key=lambda t: int(t.split()[-1]))
    assert seen['more']['groups'][-1][0] == '不属于任何产线'                # the checks of a runner
    assert seen['keyboard'] == [['提交人', 'ascending']]
    on_l1 = [models['L1'], other]
    assert seen['models']['names'] == on_l1[::-1] and seen['models']['sorted'] == [['名称', 'descending']]
    assert seen['modelsBack']['sorted'] == [] and seen['modelsBack']['names'] == on_l1  # the server's order again
    assert seen['siteModels'] == [[f'{site} / L1', on_l1], [f'{site} / L2', [models['L2']]]]


@pytest.mark.skipif(not node_with_playwright() or os.name != 'posix', reason='Playwright for node is not installed')
def test_evaluations_start_on_the_benchmark_and_data_is_picked_not_typed(service, tokens, api, make_api, gpu, snapshot, tmp_path):  # noqa: F811
    """The evaluation form of a model version starts on its line's benchmark test set (the one the overview compares
    on), with its versions to pick, and reads the folders of a version only when they are asked for. A batch's
    pre-labelling form offers its line's release first, even of another line's model, then the model versions of
    every line, the deprecated ones left out; with no release it chooses nothing until one is picked."""
    from test_evals import add_runner, trained
    from test_models import release
    from test_trainsets import add_version
    from fw import ops
    from helpers import quiet, register_place, write
    site = f'K{os.urandom(3).hex().upper()}'
    for line in ('L1', 'L2', 'L3'):
        register_place(site, line)
    seed = int.from_bytes(os.urandom(2), 'big')
    bench = f'{site}-test'
    api.create_dataset(bench, site, 'L1', 'test', '')
    add_version(api, tmp_path, bench, {f'D{d}/img{i}.{x}': (real_png(64, 32, seed + 10 * d + i) if x == 'png' else label('scratch'))
                                       for d in (1, 2) for i in range(d) for x in ('png', 'json')})
    api.set_benchmark(f'{site}/L1', bench)
    name, own = f'{site}-det', f'{site}-L3det'
    api.create_model(name, site, 'L1', 'det', '')
    api.create_model(own, site, 'L3', 'det', '')
    old, mv = trained(api, name, snapshot, tmp_path), trained(api, name, snapshot, tmp_path)
    mv3 = trained(api, own, snapshot, tmp_path)
    api.set_tag(name, old['number'], 'deprecated', True, '漏检较多')
    for line in ('L2', 'L3'):  # the lines deploy the model of L1
        release(make_api('field'), mv['ref'], '2026-11-01T10:00:00+08:00', site=site, line=line)
    r = add_runner(gpu)
    batches = []
    for i, line in enumerate(('L1', 'L2', 'L3')):
        pool_name = pool(api, tmp_path / f'p{i}', {'20261101/a.png': real_png(64, 32, seed + 100 + i)}, site=site, line=line)
        folder = tmp_path / f'f{i}' / '20261102'
        write(folder, 'x.png', real_png(64, 32, seed + 200 + i))
        batches.append(ops.upload_batch(make_api('field'), folder, pool_name, '张三', echo=quiet)['name'])
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(['node', str(WEB_TESTS / 'picks.cjs'), service.url + '/', tokens['algo'], tokens['field'], mv['ref'], bench,
                          r['ref'], ','.join(batches), str(shots)], capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert out.returncode == 0, out.stdout + out.stderr
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['before'] == {'manifests': 0, 'dirs': 0, 'said': '全部图片'}   # nothing read until asked
    assert seen['eval']['data'] == f'@{bench}' and seen['eval']['versions'] == [f'{bench}@1（最新）']
    assert seen['eval']['dirs'] == ['D1/（1 张图片）', 'D2/（2 张图片）'] and seen['eval']['typed'] == ['short']
    assert seen['said'] == '已选 1 个目录'
    job = api.job(seen['job'])
    assert job['kind'] == 'eval' and job['params']['data']['ref'] == f'{bench}@1' and job['params']['data']['dirs'] == ['D2']
    assert job['params']['data']['images'] == 2 and job['params']['model']['ref'] == mv['ref']
    assert seen['split'] == [True, True]
    api.cancel_job(seen['job'])

    groups = lambda form: dict(form['groups'])  # noqa: E731
    l1, l2, l3 = seen['prelabel']
    # no release: the line's versions are offered, nothing chosen, and a click asks for one
    assert l1['value'] == '' and l1['first'] == '请选择模型版本' and l1['hint'] == f'{site}/L1 尚未登记发布，请选择模型版本。'
    assert groups(l1)['本产线'] == [mv['ref']] and mv3['ref'] in groups(l1)['同一现场的其他产线']   # old is deprecated
    assert '当前发布' not in groups(l1) and l1['refused'] == '请选择模型版本。'
    # a release of another line's model: chosen, first, said
    assert l2['value'] == mv['ref'] and l2['chosen'].endswith(' · 当前发布') and l2['hint'] is None and not l2['go']
    assert l2['groups'][0] == ['当前发布', [mv['ref']]] and '本产线' not in groups(l2) and 'refused' not in l2
    assert l3['value'] == mv['ref'] and groups(l3)['本产线'] == [mv3['ref']] and not l3['go']
    assert all(old['ref'] not in refs for form in (l1, l2, l3) for _, refs in form['groups'])

def write_tree(root: Path, files: dict) -> Path:
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def test_the_folder_check_is_the_one_of_fw_dataset_import(service, tokens, tmp_path):
    from fw.checks import scan_folder
    files = {'A/a.png': b'1', 'A/a.bmp': b'2', 'A/a.json': label('scratch'), 'A/b.png': b'3', 'B/b.png': b'4',
             'B/x.json': b'{"shapes": []}', 'notes.docx': b'5', 'A/Thumbs.db': b'6', 'classes.txt': b'scratch\n',
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
    assert allowed['reasons'] == local.blocking(True, True) == ['有仅大小写不同、或同名的文件和目录，在 Windows 上会互相覆盖，请先改名']

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
    api.create_dataset(dataset, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
    ops.import_folder(api, dataset, write_tree(tmp_path / 'd', {'a/x.png': real_png(64, 32, 7)}), echo=quiet)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'accounts.cjs'), service.url + '/', tokens['admin'], name, dataset,
                        str(shots)], capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['nav'] == ['概览', '数据集', '标注批次', '模型', '发布记录', '任务', '预测集', '操作记录', '账号']
    assert seen['made'] == f'已新建账号 {name}' and seen['dialogs'] == []                 # asked on the page itself
    assert '改为现场实施' in seen['asked'] and seen['cancelled'] == [0, 'algo']
    assert seen['repicked'] == [1, 'admin'] and seen['replaced'] == [1, 'algo']  # a question replaced puts its role back
    assert seen['changed'] == f'已将 {name} 的角色改为现场实施' and seen['role'] == 'field'
    assert seen['token'].startswith('fw_') and '只显示这一次' in seen['tokens'] and '现场笔记本' in seen['tokens']
    assert '账号名只能用' in seen['refused']
    admin = tokens['admin_name']
    assert [(e[1], e[2], e[3]) for e in reversed(seen['events'])] == [
        (admin, '新建账号', name), (admin, '修改账号角色', name), (admin, '发放凭据', name)]
    assert [e[4] for e in reversed(seen['events'])][:2] == ['角色为算法工程师', '角色由算法工程师改为现场实施']
    assert seen['events'][0][4].startswith('凭据 ') and seen['events'][0][4].endswith('（现场笔记本）')
    assert seen['eventLinks'] == ['#/users'] * 3                              # an administrator opens the accounts
    assert seen['token'] not in json.dumps(seen['events'])
    # the account it made works, as a field account: it reads, and is offered nothing it may not do
    assert seen['fieldNav'] == ['概览', '数据集', '标注批次', '模型', '发布记录', '预测集'] and seen['fieldWho'] == [name, '现场实施']
    assert seen['fieldButtons'] == [] and '现场实施账号不能管理账号' in seen['fieldUsers']
    assert '现场笔记本' in seen['mine'] and '有效' in seen['mine']
    me = make_api(token=seen['token']).me()
    assert (me['name'], me['role']) == (name, 'field')


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_every_page_keeps_rows_on_one_line_and_forms_to_one_width(service, tokens, api, make_api, tmp_path):
    """Tables: one line per row at 1180 wide, inside their panels with a few words of every text left (a
    version as its number in its dataset's table), a long note cut short with its whole text on pointing, times as
    今天 HH:MM, sources by how and which folder, numbers on the right, version labels not in a state's colour;
    a batch's state as a dot and words; forms of one width with their check boxes under the fields; files chosen
    in a box; the sign-in page without the side bar; the account's name and role on two lines."""
    from fw import ops
    from helpers import quiet
    from test_trainsets import make
    seed = int.from_bytes(os.urandom(2), 'big')
    name = f'W-{os.urandom(4).hex()}'
    api.create_dataset(name, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
    ops.import_folder(api, name, write_tree(tmp_path / 'a' / 'pool', {'20261101/x.png': real_png(64, 32, seed)}), echo=quiet)
    note = '补充十一月夜班采集的数据，' * 12
    ops.import_folder(api, name, write_tree(tmp_path / 'b' / 'pool', {'20261101/x.png': real_png(64, 32, seed),
                                                                     '20261102/y.png': real_png(64, 32, seed + 1)}),
                      note=note, echo=quiet)
    api.add_label(name, 2, 'v0.1.0', '')
    make(api, f'{name}-det', [f'{name}@2'], test_dirs=['20261102'])         # the model register page needs a snapshot
    field = make_api('field')
    batch = ops.upload_batch(field, write_tree(tmp_path / 'c' / '20261103', {'z.png': real_png(64, 32, seed + 2)}), name,
                             '张三', echo=quiet)['name']
    field.batch_step(batch, 'submit', {'reviewer': tokens['algo_name']})
    api.batch_step(batch, 'reject', {'comment': '请补标', 'images': ['z.png'], 'revision': 1})
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'layout.cjs'), service.url + '/', tokens['algo'], name, batch, str(shots)],
                       capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['loginSide'] == 'none' and seen['loginNav'] == 0 and 'aoi-flywheel' in seen['loginBrand']
    assert seen['side'] != 'none' and seen['whoTwoLines']
    assert seen['versionsOneLine'] and seen['fits'] and seen['eventsOneLine'] and seen['eventsFit']
    assert seen['note'] == {'title': note.strip(), 'cut': True, 'overflow': 'ellipsis'}
    assert seen['time'][0].startswith(('今天 ', '昨天 ')) and len(seen['time'][1]) == len('2026-11-14 22:09')
    assert seen['source'][0] == 'fw dataset import · pool' and seen['source'][1].startswith('fw dataset import ')
    assert seen['numbersRight'] == ['right', 'right']
    assert seen['labelChip'] != 'rgb(230, 244, 234)'                       # not the green of 复核通过
    assert seen['versionRefs'] == [['@1', f'{name}@1']]  # the parent: its number, all of it on pointing
    assert seen['tables'] == {page: {'wide': [], 'narrow': []} for page in ('pool', 'events', 'datasets', 'batches', 'batch')}
    assert seen['headActions'] == ['上传新增目录', '建立训练集快照'] and seen['more'] == ['移除目录', '上传完整版本']
    assert seen['tree'].split()[0] == 'TX' and seen['navLine'] == '#/models?site=SES&line=TX'
    assert seen['batchState'] == ['已退回', True]
    assert seen['steps'] == ['时间', '账号', '操作', '意见'] and seen['stepsTitle'] == '流转记录'
    assert seen['formWidth'] <= 720 and seen['checkUnderFields']
    assert seen['fileBoxes'] == 3 and seen['nativeHidden'] == '0' and seen['fileChosen'] == 'best.onnx · 2 B'


@pytest.mark.skipif(not node_with_playwright() or os.name != 'posix', reason='Playwright for node is not installed')
def test_a_failed_job_is_submitted_again_or_marked_handled_in_the_browser(service, tokens, api, make_api, gpu, model, snapshot,  # noqa: F811
                                                                          tmp_path):
    """A failed job's page says why and offers 「重新提交」 (on the newest version of its runner by default) and
    「标为已处理…」 (asked on the page, with a note); after either the home page's 「待我处理」 lists it no more, and the
    list of jobs says what became of it. A failed pre-label of a labelling batch another account uploaded is not
    offered for resubmission: the page names the uploader."""
    from test_evals import add_runner, trained
    from fw import ops, worker
    from helpers import quiet
    r = add_runner(gpu)
    (gpu.listings / f'{r["conda_env"]}.txt').write_text('@EXPLICIT\nhttps://x/torch-other.tar.bz2\n')  # changed
    failed = []
    for _ in range(2):   # two model versions: two pieces of work
        mv = trained(api, model, snapshot, tmp_path)
        job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'})
        worker.run_worker(gpu, quiet, once=True)
        assert api.job(job['id'])['status'] == 'failed'
        failed.append(job['id'])
    field = make_api('field')
    batch = ops.upload_batch(field, write_tree(tmp_path / 'day', {'y.png': real_png(64, 32, 8)}),
                             pool(api, tmp_path, {'A/x.png': real_png(64, 32, 7)}), '张三', echo=quiet)['name']
    pre = ops.prelabel_batch(field, batch, model=mv['ref'], runner=r['runner'], echo=quiet)['job_id']
    worker.run_worker(gpu, quiet, once=True)
    assert api.job(pre)['status'] == 'failed'
    newer = worker.register_runner(gpu, r['runner'], r['conda_env'], '', 'rfdetr', gpu.onnx2engine)  # registered anew
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(['node', str(WEB_TESTS / 'failed.cjs'), service.url + '/', tokens['algo'], *map(str, failed), str(pre), str(shots)],
                         capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert out.returncode == 0, out.stdout + out.stderr
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    a, b = (f'任务 {i}' for i in failed)
    assert {a, b} <= set(seen['before'])
    assert '推理环境和登记时不一样' in seen['card']['error'] and seen['card']['runner'] == f'{newer["ref"]}（同名推理环境的最新版本）'
    assert seen['card']['buttons'] == ['重新提交', '标为已处理…']
    again = api.job(int(seen['retried']['hash'].split('/')[-1]))
    assert again['runner'] == newer['ref'] and again['status'] == 'queued' and seen['retried']['title'].startswith(f'任务 {again["id"]}')
    assert seen['firstAfter'] == {'retried': f'已重新提交为任务 {again["id"]}（排队）。', 'actions': 0}
    assert a not in seen['afterRetry'] and b in seen['afterRetry']
    assert '此标记不能撤销' in seen['ask']
    assert seen['handled']['text'].startswith(f'已由 {tokens["algo_name"]} 于 ') and seen['handled']['text'].endswith('标为已处理：模型版本选错，无需重跑')
    assert seen['handled']['actions'] == 0 and b not in seen['afterHandled']
    assert seen['list'] == [[b, '失败（已处理）'], [a, f'失败（已重新提交为任务 {again["id"]}）']]
    assert api.job(failed[1])['handled']['note'] == '模型版本选错，无需重跑'
    assert seen['batchJob'] == {'owner': f'该任务是标注批次 {batch} 的预标注，仅上传该批次的账号（{field.get("/me")["name"]}）'
                                         '或管理员可以重新提交，上传人可在批次页重新运行预标注。',
                                'link': f'#/b/{batch}', 'retry': 0, 'runner': 0, 'actions': 0}


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_the_lists_are_browsed_by_site_and_line(service, tokens, api, tmp_path):
    """A site or line chosen in the tree narrows the lists; under 「全部」 and under a site the rows are grouped by
    line (a title row each, in the order of the tree), under a line they are listed plainly."""
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
    titles = [t for t, _ in seen['all']['poolGroups']]
    tx, ty = f'{site} / TX', f'{site} / TY'
    assert dict(seen['all']['poolGroups'])[tx] == [a] and dict(seen['all']['poolGroups'])[ty] == [b]
    assert titles.index(tx) + 1 == titles.index(ty) and dict(seen['all']['snapshotGroups'])[tx] == [ts]
    assert seen['tasks']['head'][:2] == ['名称', '任务'] and dict(seen['tasks']['rows'])[ts] == '检测'  # the snapshot's task
    assert any(x.startswith(site) for x in seen['tree']) and seen['tree'][0].startswith('全部')
    assert seen['site']['pools'] == [a, b] and seen['site']['snapshots'] == [ts]
    assert seen['site']['poolGroups'] == [[tx, [a]], [ty, [b]]] and seen['site']['snapshotGroups'] == [[tx, [ts]]]
    assert seen['ty'] == {'pools': [b], 'snapshots': [], 'poolGroups': [[None, [b]]], 'snapshotGroups': []}
    assert f'site={site}' in seen['tyHash']
    # the snapshot took its data from TX
    assert seen['tx'] == {'pools': [a], 'snapshots': [ts], 'poolGroups': [[None, [a]]], 'snapshotGroups': [[None, [ts]]]}
    assert seen['newHref'] == f'#/dataset-new?site={site}&line=TX'    # a new pool starts on the line being browsed
    assert '没有符合条件的数据集' in seen['none']
    assert seen['navModels'] == f'#/models?site={site}&line=TX' and seen['models'] == [model] and seen['navOn'] == '模型'
    assert seen['navAfter'] == '#/datasets'
    assert batch in seen['batches'] and seen['batchGroups'] == [[ty, [batch]]]


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_line_is_seen_at_a_glance(service, tokens, api, make_api, make_worker, tmp_path):  # noqa: F811
    """The overview: every line with how much it holds; one line with its release, its benchmark test set and
    its models evaluated on it, snapshots, pools and the batches under way; said plainly when the line is
    unknown or the account may not see it."""
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
    ops.register_eval(api, f'{model}@1', f'{snap["ref"]}:test', deteval=deteval_out(tmp_path), echo=quiet)  # not on the benchmark
    bench = f'B-{os.urandom(4).hex()}'
    api.create_dataset(bench, site, 'TX', 'test', '')
    ops.import_folder(api, bench, write_tree(tmp_path / 'bench', {'x.png': real_png(64, 32, 7)}), echo=quiet)
    api.set_benchmark(f'{site}/TX', bench)
    e = _on(api, tmp_path, f'{model}@1', f'{bench}@1', {'mAP50': 0.7, 'images': 1})
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
    assert seen['lineRow'] == [f'{site} / TX', '2', '1', '1', '1', '1']   # pool and test set, snapshot, model, batch, release
    assert rel['ref'] in seen['release'] and f'{model}@1' in seen['release']
    assert seen['models'] == [model, '@1', '0.7', '—', '—', '—', '']      # the numbers the report has
    assert seen['menu'] == [['查看版本', f'#/mv/{model}/1'], ['查看评估', f'#/eval/{e["id"]}'], ['复制版本号', None]]
    assert seen['data'] == [f'{a}@1', f'{snap["ref"]}', f'{bench}@1']        # the pool, the snapshot, the test set
    assert seen['benchmark'].startswith(f'基准测试集{bench}@1') and seen['benchmarkChange'] == '更换'
    assert '基准测试集尚未设定' in seen['ty']
    assert seen['batches'] == [batch, '待提交复核']                            # the closed one is not under way
    assert seen['treeOn'].startswith('TX')
    assert seen['navModels'] == f'#/models?site={site}&line=TX'               # the lists keep the line
    assert seen['moreBatches'] == f'#/batches?site={site}&line=TX&view=all'
    assert '尚未登记发布' in seen['ty'] and '该产线暂无模型' in seen['ty'] and seen['tyPools'] == [f'{b}@1']
    assert '不是已登记的产线' in seen['unknown']
    assert seen['siteLines'] == [f'{site} / TX', f'{site} / TY']
    # a field account reads every part of it
    assert seen['field']['sections'] == ['models', 'data', 'batches'] and seen['field']['refused'] == []
    assert seen['field']['benchmark'].startswith(f'基准测试集{bench}@1') and seen['field']['change'] == 0  # read, not set
    assert rel['ref'] in seen['field']['release'] and seen['field']['model'] == model
    assert '概览' not in seen['workerNav'] and '请使用有权限的账号' in seen['refused']


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_models_of_a_line_are_compared_on_its_benchmark_test_set(service, tokens, api, make_api, tmp_path):
    """The overview sets and changes a line's benchmark test set (a test set of that line), keeping every setting;
    the models' numbers there and on their page are those measured on its newest version as a whole, the
    released version marked and the others with their change from it; evaluation tables show mAP50,
    mAP50-95, P and R and fit at 1180; a field account reads it and sets nothing; a new batch is offered data
    pools only, by line, none chosen unless the line has one."""
    from fw import ops
    from helpers import quiet, register_place
    from test_models import files_in, register, release
    from test_trainsets import make, pool, sample_files
    site = f'S{os.urandom(4).hex()}'
    register_place(site, 'TY')
    a = pool(api, tmp_path / 'a', sample_files('2026-09', 4), site=site, line='TX')
    ty = [pool(api, tmp_path / f'ty{i}', sample_files('2026-09', 1), site=site, line='TY') for i in range(2)]
    first, second, other, stray = (f'{p}-{os.urandom(4).hex()}' for p in 'ABCD')
    for name, line in ((first, 'TX'), (second, 'TX'), (other, 'TY')):
        api.create_dataset(name, site, line, 'test', '')
        ops.import_folder(api, name, write_tree(tmp_path / name, {'x.png': real_png(64, 32, len(name) + ord(name[0]))}), echo=quiet)
    api.create_dataset(stray, site, 'TX', 'regression', '')                      # not a test set: not offered
    snap = make(api, f'T-{os.urandom(4).hex()}', [f'{a}@1'])
    model = f'M-{os.urandom(4).hex()}'
    api.create_model(model, site, 'TX', 'det', '')
    for i in range(3):
        register(api, model, snap, files_in(tmp_path, f'b{i}'))
    for n, numbers in ((1, {'mAP50': 0.9, 'P': 0.8}), (2, {'mAP50': 0.92, 'mAP50-95': 0.7, 'P': 0.85, 'R': 0.8}),
                       (3, {'mAP50': 0.95, 'mAP50-95': 0.72, 'P': 0.83, 'R': 0.81})):
        _on(api, tmp_path, f'{model}@{n}', f'{second}@1', numbers)
    _on(api, tmp_path, f'{model}@1', f'{first}@1', {'mAP50': 0.5})
    release(make_api('deploy'), f'{model}@2', '2026-11-10T10:00+08:00', site=site, line='TX')
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'benchmark.cjs'), service.url + '/', tokens['algo'], tokens['field'], site, model,
                        first, second, str(shots)], capture_output=True, text=True, timeout=240, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    # none set: the latest version, no numbers
    assert '尚未设定' in seen['none']['bar'] and seen['none']['models'] == ['@3', '—', '—']
    assert seen['offered'] == ['', first, second] and seen['unchosen'] == '请选择测试集。'
    assert seen['setToast'] == f'已将 {first} 设为 {site}/TX 的基准测试集'
    assert f'{first}@1' in seen['first']['bar'] and seen['first']['models'] == ['@1', '0.5', '—']   # its only report there
    assert seen['offeredNow'] == ['', second]
    assert f'{second}@1' in seen['second']['bar'] and seen['second']['models'] == ['@3', '0.95', '0.72', '0.83', '0.81']
    assert seen['history'] == [[api.me()['name'], second, ''], [api.me()['name'], first, '首个基准']]
    assert [h['dataset'] for h in api.benchmark(site, 'TX')['history']] == [second, first]
    # the model's page: its versions on the benchmark, the release marked, the change from it
    assert seen['benchRef'] == f'基准测试集{second}@1'
    assert seen['bench'] == [['@3', '0.95 +0.03', '0.72 +0.02', '0.83 −0.02', '0.81 +0.01'],
                             ['@2当前发布', '0.92', '0.7', '0.85', '0.8'],
                             ['@1', '0.9 −0.02', '—', '0.8 −0.05', '—']]
    assert seen['evalHead'] == ['', '编号', '模型版本', '测试集', '目录', 'mAP50', 'mAP50-95', 'P', 'R', '时间', '备注']
    assert seen['evalRows'][0][1:] == ['@1', f'{first}@1', '全部', '0.5', '—', '—', '—']
    assert seen['evalRows'][1][1:] == ['@3', f'{second}@1', '全部', '0.95', '0.72', '0.83', '0.81']
    assert seen['evalWho'].startswith(api.me()['name'])
    assert seen['versionEvalHead'] == ['', '编号', '测试集', '目录', 'mAP50', 'mAP50-95', 'P', 'R', '时间', '备注']
    assert seen['modelWide'] == [] and seen['versionWide'] == []
    # a new batch: pools only, by line, none chosen unless the line has only one
    groups = dict(seen['pools'])
    assert groups[f'{site} / TX'] == [a] and sorted(groups[f'{site} / TY']) == sorted(ty)
    assert not {first, second, other, stray, snap['dataset']} & {n for g in groups.values() for n in g}
    assert seen['poolFirst'] == ['', '请选择数据池'] and seen['poolOfLine'] == a and seen['poolOfTwo'] == ''
    assert f'{second}@1' in seen['field']['bar'] and seen['field']['change'] == 0


@pytest.mark.skipif(not node_with_playwright() or os.name != 'posix', reason='Playwright for node is not installed')
def test_the_home_page_lists_what_waits_for_the_account(service, tokens, api, make_api, make_worker, fake_conda, database, tmp_path):  # noqa: F811
    """The home page's 「待我处理」: for an algorithm engineer the batches it is asked to review, the approved ones
    to import and the job it submitted that failed (not another account's); for a field account its rejected
    batches and the ones not yet sent to review, and nothing it may not act on; for a new account, nothing. The
    batch list's tabs carry how many each holds."""
    from fw import ops
    from fwserver import auth, db
    from helpers import quiet, write
    from test_jobs import runner
    site = f'S{os.urandom(4).hex()}'
    pool_name = pool(api, tmp_path / 'p', {'20261101/a.bmp': b'pool A'}, site=site, line='TX')
    field, algo_name = make_api('field'), tokens['algo_name']
    made = {}
    for i, state in enumerate(('review', 'approved', 'rejected', 'open')):
        folder = tmp_path / f'2026120{i + 1}'
        write(folder, 'x.bmp', f'{site} {state}'.encode())
        b = ops.upload_batch(field, folder, pool_name, '张三', name=f'{site}-{state}', echo=quiet)
        if state != 'open':
            field.batch_step(b['name'], 'submit', {'reviewer': algo_name})
        if state == 'approved':
            api.batch_step(b['name'], 'approve', {})
        if state == 'rejected':
            api.batch_step(b['name'], 'reject', {'comment': '划痕漏标'})
        made[state] = b['name']
    w = make_worker()
    r = runner(w, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    c = w.claim()
    assert c['id'] == job['id']
    w.finish(job['id'], c['token'], 'failed', error='读不了 conda 环境\n更多信息')
    with db.connect(database) as k:
        fresh = f'new-{os.urandom(3).hex()}'
        auth.add_user(k, fresh, 'field')
        fresh_token = auth.create_token(k, fresh, 'test')[0]
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(['node', str(WEB_TESTS / 'todo.cjs'), service.url + '/', tokens['algo'], tokens['field'], fresh_token, site,
                          str(shots)], capture_output=True, text=True, timeout=240, cwd=WEB_TESTS)
    assert out.returncode == 0, out.stdout + out.stderr
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    ours = set(made.values()) | {f'任务 {job["id"]}'}
    mine = lambda rows: [row for row in rows if row[2] in ours]  # noqa: E731  (other tests' rows are there too)
    where = f'入库到 {pool_name} / 2026120'
    algo = mine(seen['algo']['rows'])
    assert [(k, o) for k, _, o, _ in algo] == [('review', made['review']), ('import', made['approved']), ('failed', f'任务 {job["id"]}')]
    assert algo[0][1] == '待我复核' and algo[0][3] == f'第 1 版，上传账号 {tokens["field_name"]}，{where}1/'
    assert algo[1][1] == '待入库' and algo[1][3] == f'第 1 版已由 {algo_name} 复核通过，{where}2/'
    assert algo[2][1] == '任务失败' and algo[2][3] == '检查推理环境：读不了 conda 环境'
    assert seen['algo']['head'].startswith('待我处理') and seen['algo']['lines']
    assert seen['algoLink'] == f'#/b/{seen["algo"]["rows"][0][2]}'  # the row leads to its batch
    # the tabs of the batch list count what each holds, here on the site of this test
    assert seen['algoTabs']['todo'] == {'tabs': [['todo', '1'], ['mine', '0'], ['all', None]], 'on': 'todo', 'rows': 1}
    assert seen['algoTabs']['mine']['rows'] == 0 and seen['algoTabs']['all']['rows'] == 4
    field_rows = mine(seen['field']['rows'])
    assert [(k, s, o) for k, s, o, _ in field_rows] == [('rejected', '已退回', made['rejected']), ('unsent', '待提交复核', made['open'])]
    assert field_rows[0][3] == f'第 1 版已被 {algo_name} 退回，请修改后上传新版次' and field_rows[1][3] == '第 1 版尚未提交复核'
    assert not [row for row in seen['field']['rows'] if row[0] in ('review', 'import', 'failed')]  # what a field account may not do
    assert seen['fieldTabs'] == {'tabs': [['mine', '4'], ['all', None]], 'on': 'mine', 'rows': 4}
    assert seen['fresh']['rows'] == [] and seen['fresh']['none'] == '暂无待处理的事项。' and seen['fresh']['head'] == '待我处理'
    assert seen['fits'][0] <= seen['fits'][1] + 1


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_release_is_registered_and_read_on_the_web(service, tokens, api, make_api, tmp_path):
    """A field account downloads the ONNX of its line's current release from the overview, is told of a newer
    candidate, registers that version's deployment from its page (a refusal shown where it was asked; the engine
    hashed in the browser, never uploaded) and finds it marked current on the line's record of releases; an
    earlier deployment registered later leaves the current release, and the same one again is that release. An
    algorithm engineer reads the record and is offered nothing to register."""
    import datetime
    import hashlib
    from helpers import register_place
    from test_models import files_in, register, release
    from test_trainsets import make, pool, sample_files
    site = f'S{os.urandom(4).hex()}'
    register_place(site, 'TX')
    snap = make(api, f'T-{os.urandom(4).hex()}', [f'{pool(api, tmp_path / "a", sample_files("2026-09", 2), site=site, line="TX")}@1'])
    model = f'M-{os.urandom(4).hex()}'
    api.create_model(model, site, 'TX', 'det', '')
    for i in range(3):
        register(api, model, snap, files_in(tmp_path, f'r{i}'))
    for n, tag in ((2, 'candidate'), (2, 'deprecated'), (3, 'candidate')):  # @2: a candidate no more
        api.set_tag(model, n, tag, True)
    release(make_api('deploy'), f'{model}@1', '2026-11-01T10:00+08:00', site=site, line='TX')
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'releases.cjs'), service.url + '/', tokens['field'], tokens['algo'], site, model,
                        str(shots)], capture_output=True, text=True, timeout=240, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    r1, r2, r3 = (f'{site}-TX-r{n}' for n in (1, 2, 3))
    # the card of a field account: the ONNX of what runs there, the newer candidate (not the deprecated one)
    assert seen['card']['ref'] == r1 and seen['card']['acts'] == '下载 ONNX登记发布发布记录'
    assert seen['downloaded'] == f'{model}@1.onnx' and seen['card']['newer'] == f'比当前发布更新的候选版本：{model}@3'
    assert seen['versionActions'] == ['下载 ONNX', '登记发布']
    # the form: the version of the page it came from, the line, the address of the line's last release
    assert seen['form']['model'].startswith(f'{model}@3') and '候选' in seen['form']['model']
    assert seen['form']['repo'] == 'https://git.example.com/deploy/trt.git' and (seen['form']['site'], seen['form']['line']) == (site, 'TX')
    assert seen['missing'] == '请填写部署仓库的提交号。' and 'git 提交号' in seen['refused']
    first = seen['first']
    assert first['asked'].startswith(f'将登记 {model}@3 在 {site}/TX 的发布') and '引擎 model_fp16.engine' in first['asked']
    assert '不能修改或删除' in first['asked'] and first['toast'] == f'已登记发布 {r2}'
    assert first['picked'] == r2 and first['puts'] == 0                       # the engine is not uploaded
    assert first['rows'][0][:2] == [f'{r2} 当前发布', f'{model}@3'] and first['rows'][0][4:] == ['RTX3070 · TensorRT 10.3 · fp16', first['rows'][0][5], '网页登记']
    assert first['rows'][1][:2] == [r1, f'{model}@1']
    engine = f'engine of {site}'.encode()
    got = api.release(r2)
    assert (got['engine_sha256'], got['engine_size']) == (hashlib.sha256(engine).hexdigest(), len(engine))
    assert first['rows'][0][5] == got['engine_sha256'][:8] + '…'
    assert (got['model_version']['ref'], got['deploy_commit'], got['gpu'], got['trt_version'], got['precision']) == (
        f'{model}@3', '0123456789abcdef0123456789abcdef01234567', 'RTX3070', '10.3', 'fp16')
    assert abs(datetime.datetime.fromisoformat(got['deployed_at']) - datetime.datetime.now(datetime.timezone.utc)) < datetime.timedelta(minutes=5)
    assert api.missing([(got['engine_sha256'], len(engine))]) == [got['engine_sha256']]
    # an earlier deployment of a deprecated version: registered, not current; the same again is that one
    assert '已标记为弃用' in seen['deprecated']
    assert seen['earlier']['toast'] == f'已登记发布 {r3}；该产线当前发布仍为 {r2}（部署时间更晚）'
    assert seen['again']['toast'] == f'该次部署已登记为 {r3}'
    assert seen['rows'] == [[f'{r2} 当前发布', f'{model}@3', seen['rows'][0][2]], [r3, f'{model}@2', '11月2日 09:00'], [r1, f'{model}@1', '11月1日 10:00']]
    assert datetime.datetime.fromisoformat(api.release(r3)['deployed_at']) == datetime.datetime(2026, 11, 2, 1, 0, tzinfo=datetime.timezone.utc)
    assert api.current_release(site, 'TX')['ref'] == r2 and len(api.releases(site, 'TX')) == 3
    assert seen['after'] == {'ref': r2, 'newer': 0}
    assert seen['count'] == ['3', f'#/releases?site={site}&line=TX'] and seen['fits'][0] <= seen['fits'][1] + 1, seen['fits']
    # an algorithm engineer
    assert seen['algo']['rows'] == 3 and seen['algo']['add'] == 0 and seen['algo']['register'] == 0
    assert seen['algo']['acts'] == '查看模型版本发布记录' and seen['algo']['download'] == 0
    assert seen['algo']['releases'] == [r2, f'#/releases?site={site}&line=TX&ref={r2}']
    assert '当前账号不能登记发布' in seen['algo']['refused']


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
    assert seen['lineOfHiddenSiteOff'] is True and '未显示' in seen['hiddenNote']
    assert not any(other in h for h in seen['listTree']) and seen['hiddenPoolListed'] is True   # the list hides nothing
    assert seen['listCrumbs'].replace(' ', '') == '概览/数据集'
    assert seen['headers'] and all(h == ['nowrap', True] for h in seen['headers'])
    assert seen['browsedHidden'] == [ty]
    assert (seen['siteModels'], seen['siteModelRows']) == ('3', 3)     # TY is not shown, its two models still count
    assert seen['backFromBatch'] == '/datasets'                         # not caught between the two addresses of a batch
    assert '没有可显示的产线' in seen['allLinesHidden'] and '显示设置' in seen['allLinesHidden']
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


def _on(api, tmp_path, model_ref, testset_ref, numbers):
    """An evaluation registered by hand with these numbers."""
    from fw import ops
    from helpers import quiet
    tag = os.urandom(3).hex()
    run = write_tree(tmp_path / f'm-{tag}', {'metrics.json': json.dumps(numbers).encode()})
    return ops.register_eval(api, model_ref, testset_ref, metrics=run / 'metrics.json', echo=quiet)


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_the_overview_of_a_line_reads_at_a_glance_and_its_rows_have_a_menu(service, tokens, api, make_api, database, tmp_path):
    """SES / TX 贴片线: the release first, models with their evaluation on the line's benchmark test set, data,
    batches on the right;
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
    api.set_benchmark('SES/TX', test)                     # the models of the line are compared on its newest version
    bench = f'{test}@1'
    _on(api, tmp_path, f'{detr}@1', bench, {'mAP50': 0.9})
    shown = _on(api, tmp_path, f'{detr}@2', bench, {'mAP50': 0.962, 'mAP50-95': 0.846, 'P': 0.951, 'R': 0.933})
    _on(api, tmp_path, f'{yolo}@1', bench, {'mAP50': 0.928, 'mAP50-95': 0.791, 'P': 0.94, 'R': 0.9})
    # measured elsewhere, not on the benchmark: not shown next to the others
    ops.register_eval(api, f'{seg}@1', f'{snap["ref"]}:test', deteval=_deteval(tmp_path, {'mAP50': 0.99}), echo=quiet)
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
    assert seen['release']['acts'] == '查看模型版本发布记录' and seen['release']['go'] == f'#/mv/{mv_name}/{mv_number}'
    assert rel['ref'].startswith('SES-TX-r')
    rows = {r[0]: r[1:] for r in seen['models']}
    assert rows[detr] == ['@2', '0.962', '0.846'] and rows[yolo] == ['@1', '0.928', '0.791']   # the evaluated version
    assert rows[seg] == ['未评估', '—', '—']                                                    # none on the benchmark
    kinds = {name.split('@')[0]: kind for name, kind in seen['data']}
    assert kinds[data] == '数据池' and kinds[snap['dataset']] == '训练集快照（检测）' and kinds[test] == '测试集'
    mine = {name: state for name, state in seen['batches'] if name in batches}   # other tests add batches on SES/TX too
    assert mine == dict(zip(batches, ['待复核', '待提交复核', '复核通过']))
    for at in (seen['at1440'], seen['at1920']):
        assert at['releaseFirst'] and at['dataUnderModels'] and at['sideBySide']
    assert seen['at1920']['main'] > 1600 and seen['at1920']['fills'] > 1500                  # no 1480 cap
    assert seen['at1180']['stacked'] and not seen['at1180']['sideBySide']
    # the ⋯: hidden until the row is pointed at or focused; the numbers do not move
    assert seen['dotsHidden'] == '0.4' and seen['dotsHovered'] == ['1', '0.4'] and seen['numbersStay']   # faint until pointed at
    assert seen['menu'] == [['查看版本', f'#/mv/{detr}/2'], ['查看评估', f'#/eval/{shown["id"]}'], ['复制版本号', None]]
    assert seen['expanded'] == 'true' and seen['closedByOutside'] and seen['menuNoEval'] == ['查看版本', '复制版本号']
    assert seen['tabToDots'] and seen['dotsFocused'] == '1'
    assert (seen['keyFirst'], seen['keySecond']) == ('查看版本', '查看评估') and seen['focusBack'] == f'{detr}@2 的更多操作'
    assert seen['toast'] == f'已复制 {detr}@2' and seen['clipboard'] == f'{detr}@2'
    assert seen['evalPage'] == [f'/eval/{shown["id"]}', f'评估 #{shown["id"]}']
    assert seen['versionPage'] == f'/mv/{detr}/2' and seen['releasePage'] == f'/mv/{mv_name}/{mv_number}'
    assert '尚未登记发布' in seen['empty']['release'] and '该产线暂无模型' in seen['empty']['models']
    assert seen['empty']['lineOn'] == f'{empty} 组装线'
    assert seen['fallback'] == {'value': f'{detr}@2', 'focused': True, 'selected': True,
                                'text': '浏览器不允许本页面写入剪贴板，请按 Ctrl+C 复制：'}
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
    api.create_dataset(name, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
    ops.import_folder(api, name, write_tree(tmp_path / 'one', {'a.png': real_png(64, 32, seed)}), echo=quiet)
    ops.import_folder(api, name, write_tree(tmp_path / 'two', {'a.png': real_png(64, 32, seed + 1)}), echo=quiet)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'labels.cjs'), service.url + '/', tokens['algo'], name, str(shots)],
                       capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['menu'] == ['与父版本比较', '设定版本号', '复制引用'] and seen['closed'] is True
    assert seen['form'] == f'为 {name}@1 设定版本号'
    assert seen['proposed'] == 'v0.1.0' and seen['next'] == 'v0.2.0'
    assert f'{name}@1' in seen['row'] and 'v0.1.0' in seen['row']
    assert seen['toast'] == f'已设定版本号 v0.1.0（{name}@1）' and seen['stayed'] == [120, 120]
    assert f'已用于 {name}@1' in seen['taken']
    assert seen['title'].startswith(f'{name}@1') and 'v0.1.0' in seen['title']
    assert api.version(name, 'v0.1.0')['number'] == 1 and api.labels(name)['labels'][0]['note'] == '冷启动'


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_the_class_table_of_a_pool_is_set_and_grown_in_the_browser(service, tokens, api, tmp_path, monkeypatch):
    from fwserver import datasets as server_datasets
    from test_trainsets import old_pool
    monkeypatch.setattr(server_datasets, 'LATEST_CHECK_FILES', 1)  # as for a large pool: only A/a.json is looked at
    seed = int.from_bytes(os.urandom(2), 'big')
    name = old_pool(api, tmp_path, {'A/a.png': real_png(64, 32, seed), 'A/a.json': label('Scratch'),
                                    'B/b.png': real_png(64, 32, seed + 1), 'B/b.json': label('dent')})
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'classes.cjs'), service.url + '/', tokens['algo'], tokens['field'], name,
                        str(shots)], capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    for page in ('add', 'upload'):  # refused before a folder is picked, not after it is uploaded
        assert '尚未填写类别表，无法上传' in seen[page], seen[page]
    assert '尚未填写类别表' in seen['none'] and seen['uploadButtons'] == [] and seen['formOpen'] is True
    assert seen['button'] == '保存类别表' and '类别表里已有' not in seen['duplicate'] and '重复' in seen['duplicate']
    assert seen['saved'] == ['scratch', 'dent'] and seen['noneAfter'] is False and seen['formOpenAfter'] is False
    assert seen['savedToast'] == '已保存类别表'
    assert f'{name}@1 中有不符合类别表的标注（已核对其中 1 个标注文件，共 2 个；其余的将在下一次上传时核对）' in seen['check']
    assert 'Scratch' in seen['check'] and 'A/a.json' in seen['check']
    assert seen['buttonAfter'] == '添加到类别表末尾'
    assert seen['added'] == ['scratch', 'dent', 'crack'] and seen['checkAfter'] is False
    assert seen['fieldTabs'] == ['版本1', '类别表3']
    assert seen['fieldRows'] == ['scratch', 'dent', 'crack'] and seen['fieldForm'] is False
    assert api.dataset(name)['classes'] == ['scratch', 'dent', 'crack']


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_what_only_the_command_line_did_is_done_in_the_browser(service, tokens, api, model, snapshot, database, tmp_path):  # noqa: F811
    """A test set of no line, registering and renaming a line, retiring a label, candidate and deprecated, every
    change of a large comparison; refused cases say why; a field account gets none of the buttons."""
    from fw import ops
    from helpers import quiet
    from test_models import files_in, register
    seed = int.from_bytes(os.urandom(2), 'big')
    labelled = f'W-{os.urandom(4).hex()}'
    api.create_dataset(labelled, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
    ops.import_folder(api, labelled, write_tree(tmp_path / 'one', {'a.png': real_png(8, 8, seed)}), echo=quiet)
    api.add_label(labelled, 1, 'v0.1.0', '冷启动')
    mv = register(api, model, snapshot, files_in(tmp_path, 'web-tags'))
    big = f'W-{os.urandom(4).hex()}'
    api.create_dataset(big, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
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
    assert '已存在' in seen['taken']
    assert seen['badCode'].strip() and f'{site}/L1' not in json.dumps(api.sites())
    lines = {(s['code'], x['code']): x['name'] for s in api.sites()['sites'] for x in s['lines']}
    assert lines[(site, 'L1')] == '一号线' and 'L1 一号线' in seen['tree']
    assert seen['algoRetire'] == 0 and '原因' in seen['noReason']
    assert '已撤销' in seen['retired'] and '起错版本了' in seen['retired']
    assert [x['label'] for x in api.labels(labelled)['labels'] if x['retired']] == ['v0.1.0']
    assert seen['metricsShown']
    assert '候选' in seen['candidate'] and '候选' in seen['both'] and '弃用' in seen['both']
    assert seen['history'] == ['标记为「候选」', '标记为「弃用」：漏检多', '取消「候选」']
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
    api.create_dataset(pool, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
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
    assert inside['target'] == 'pool' and '仅添加 pool/ 下的子目录' in inside['label']
    assert '20261101/、20261102/' in inside['dirs'] and inside['targetAfter'] == ['', True]
    assert inside['plan']['at'] == f'/v/{pool}/2' and 'pool/' not in inside['plan']['text']
    assert '顶层有 1 个文件不在任何子目录中' in seen['loose'] and 'x.png' in seen['loose']
    assert '20261101/、20261102/目录保留原名，不包含 pool 这一层' in seen['wholeParent']
    assert seen['wholeTwo'].startswith('新版本的内容为这 2 个目录（20261103/、20261104/），各目录保留原名')
    versions = {v['number']: v for v in api.versions(pool)}
    assert [(o['op'], o['dir']) for o in versions[1]['ops']] == [('add', '20261103'), ('add', '20261104')]
    assert [(o['op'], o['dir']) for o in versions[2]['ops']] == [('add', '20261101'), ('add', '20261102')]
    assert versions[2]['file_count'] == 6 and versions[1]['source'] == '网页上传新增目录 20261103、20261104'


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_snapshot_is_built_and_continued_from_the_pool_pages(service, tokens, api, tmp_path):
    """The web builds a snapshot the way fw trainset create does: from a pool version, refusing a class outside
    the table, then the next version from the pool's next version, on the first one's split and classes."""
    from test_trainsets import add_version
    seed = int.from_bytes(os.urandom(2), 'big')
    first = {}
    for i in range(10):
        first[f'A/img{i}.png'] = real_png(200, 100, seed + i)
        first[f'A/img{i}.json'] = label('scratch' if i % 2 else 'dent')
    first['A/neg0.png'] = real_png(200, 100, seed + 20)
    first['A/neg1.png'] = real_png(200, 100, seed + 21)
    line = f'R{os.urandom(3).hex()}'  # a line of its own: no pool of another test is the line's newest
    older = pool(api, tmp_path / 'older', {'A/old.png': real_png(200, 100, seed + 40)}, site='WEB', line=line,
                 name=f'0{os.urandom(3).hex()}')
    name = pool(api, tmp_path, first, site='WEB', line=line)  # the line's newest pool, though not the first by name
    both = dict(first, **{f'B/b{i}.png': real_png(200, 100, seed + 30 + i) for i in range(3)},
                **{f'B/b{i}.json': label('scratch') for i in range(3)})
    add_version(api, tmp_path, name, both)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'trainset.cjs'), service.url + '/', tokens['algo'], tokens['field'], name, str(shots),
                        'WEB', line], capture_output=True, text=True, timeout=240, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    snap = f'{name}-det'
    assert seen['overview'] == f'#/trainset-new?site=WEB&line={line}'
    assert seen['first'] == {'name': snap, 'mode': f'将建立 {snap}@1', 'sources': [f'{name}@1'], 'classes': '',
                             'val': '0.2', 'test': '0.1', 'newTo': None}
    assert "'dent' 不在类别表中" in seen['missing']['error'] and seen['missing']['create'] == 0  # refused, nothing to make
    assert seen['cleared'] == 0
    counts = {row[0]: row[1:] for row in seen['preview']['counts'][1:]}
    assert [sum(map(int, counts['图片'])), sum(map(int, counts['其中无标注框']))] == [12, 2]
    assert counts['0 scratch（框）'] and counts['1 dent（框）']
    assert seen['preview']['create'] == f'建立 {snap}@1' and '12 个样本' in seen['preview']['total']
    assert seen['made']['command'] == f'fw get {snap}@1 --to {snap}@1'
    assert '0 scratch、1 dent' in seen['made']['info'] and f'{name}@1' in seen['made']['info'] and seen['made']['task'] == '检测'
    assert '按比例划分（val 20%，test 10%，随机种子 0）' in seen['made']['info']
    assert seen['made']['next'] == f'#/trainset-new?name={snap}'
    assert '复制' in seen['copied']                                          # copied, or selected for Ctrl+C
    one = api.trainset(snap, 1)['trainset']
    assert one['classes'] == ['scratch', 'dent'] and one['sources'] == [f'{name}@1'] and one['based_on'] is None
    assert (one['rule']['val'], one['rule']['test']) == (0.2, 0.1)
    assert [one['counts'][s]['samples'] for s in ('train', 'val', 'test')] == [9, 2, 1]  # 7:2:1 of the 10 labelled, the 2 bare cut apart

    assert seen['second'] == {'name': snap, 'mode': f'将接着 {snap}@1 的划分建立 {snap}@2', 'sources': [f'{name}@2'],
                              'classes': 'scratch\ndent', 'val': '0.2', 'test': '0.1', 'newTo': 'test'}
    assert '新增 3 个样本' in seen['secondPreview']['changes'] and seen['secondPreview']['create'] == f'建立 {snap}@2'
    two = api.trainset(snap, 2)['trainset']
    assert two['based_on'] == f'{snap}@1' and two['sources'] == [f'{name}@2']
    assert two['counts']['test']['samples'] == one['counts']['test']['samples'] + 3  # new samples go to test
    assert f'接着 {snap}@1' in seen['secondMade']
    assert seen['next']['mode'] == f'将接着 {snap}@2 的划分建立 {snap}@3' and seen['next']['sources'] == [f'{name}@latest']
    assert seen['line']['sources'] == [f'{name}@latest'] and seen['line']['name'] == snap and older < name
    # previewed as the next version of the name typed, on its classes: the same as its @2, so nothing to build
    assert seen['typed']['mode'] == f'将接着 {snap}@2 的划分建立 {snap}@3' and seen['typed']['classes'] == 'scratch\ndent'
    assert f'和 {snap}@2 完全相同' in seen['typed']['error'] and seen['typed']['create'] == ''
    assert '已是一个数据集的名称' in seen['taken']
    assert seen['fieldButtons'] == 0 and '不能建立训练集快照' in seen['fieldPage']


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_model_version_is_registered_from_the_browser(service, tokens, api, tmp_path):
    """The web registers a model version the way fw model register does: the ONNX with its metrics and weights,
    on the snapshot it was trained on; a weights file or an ONNX with external data is refused."""
    from helpers import EXTERNAL, ld, onnx_model, tensor
    from test_models import onnx_bytes
    from test_trainsets import CLASSES, make
    seed = int.from_bytes(os.urandom(2), 'big')
    line = f'R{os.urandom(3).hex()}'  # a line of its own: no model of another test is proposed
    files = {f'A/img{i}.png': real_png(200, 100, seed + i) for i in range(4)}
    files.update({f'A/img{i}.json': label('scratch', 'dent') for i in range(4)})
    src = pool(api, tmp_path, files, site='WEB', line=line)
    snap = f'{src}-det'
    make(api, snap, [f'{src}@1'], CLASSES)
    other = f'{src}-b'
    make(api, other, [f'{src}@1'], CLASSES)                                    # no model is trained on this one
    run = write_tree(tmp_path / 'run', {'best.onnx': onnx_bytes(f'web {seed}'), 'best.pt': b'PK\x03\x04 weights',
                                        'metrics.json': json.dumps({'mAP50': 0.91}).encode(),
                                        'external.onnx': onnx_model(ld(5, tensor('w', EXTERNAL)))})
    name = f'WEB-{line}-detr'
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'register.cjs'), service.url + '/', tokens['algo'], tokens['field'], snap, other,
                        name, str(run), str(shots)], capture_output=True, text=True, timeout=240, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['first'] == {'trainset': f'{snap}@1', 'model': '+', 'newShown': True, 'place': f'WEB/{line}'}  # no model on the line
    assert '不是 ONNX 文件' in seen['notOnnx']
    assert 'external data' in seen['external'] and seen['afterRefusal'] == name
    assert (seen['kept']['trainset'], seen['kept']['model'], seen['kept']['newShown']) == (f'{other}@1', name, False)
    assert seen['sent'] is None and seen['page']['hash'] == f'#/mv/{name}/1' and seen['page']['title'].startswith(f'{name}@1')
    assert f'{snap}@1' in seen['page']['info'] and 'scratch、dent' in seen['page']['info'] and 'a' * 12 in seen['page']['info']
    assert 'a' * 40 in seen['page']['hashes']                              # the whole commit on pointing and on 复制
    assert {'model.onnx', 'metrics.json', 'weights/best.pt', 'classes.txt'} <= set(seen['page']['files'])
    m = api.model(name)
    assert (m['site'], m['line'], m['task']) == ('WEB', line, 'det')
    mv = api.model_version(name, 1)
    assert mv['trainset']['ref'] == f'{snap}@1' and mv['code_commit'] == 'a' * 40 and mv['note'] == '网页登记'
    assert mv['metrics'] == {'mAP50': 0.91} and api.model_versions(name)[-1]['number'] == 1
    # no snapshot unless the link names one: a version on the wrong snapshot cannot be re-pointed
    assert (seen['again']['trainset'], seen['again']['model']) == ('@', name) and '请选择训练所用的训练集快照' in seen['noSnapshot']
    assert seen['models'] == '#/model-register' and (seen['bare']['trainset'], seen['bare']['model']) == ('@', '')
    assert (seen['other']['trainset'], seen['other']['model']) == (f'{other}@1', '')   # not the line's only model
    assert (seen['picked']['trainset'], seen['picked']['model']) == (f'{snap}@1', name)  # the model trained on it
    assert seen['againSent'] is None and f'已登记为 {name}@1' in seen['toast'] and seen['againHash'] == f'#/mv/{name}/1'
    assert len(api.model_versions(name)) == 1                                  # the same ONNX: no second version
    assert seen['fieldButtons'] == 0 and '不能登记模型版本' in seen['fieldPage']


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_the_onnx_is_saved_under_the_name_of_its_version(service, tokens, api, make_api, model, snapshot, tmp_path):  # noqa: F811
    """A model version's ONNX is saved as <model>@<number>.onnx, with its highest 版本号 in use when it has one, so
    a file on a field machine says which version it is; the bytes are the version's; a field account saves it."""
    from test_models import files_in, register
    one = register(api, model, snapshot, files_in(tmp_path, f'onnx-1-{os.urandom(4).hex()}'))
    register(api, model, snapshot, files_in(tmp_path, f'onnx-2-{os.urandom(4).hex()}'))
    register(api, model, snapshot, files_in(tmp_path, f'onnx-3-{os.urandom(4).hex()}'))
    api.add_model_label(model, 2, 'v0.9.0')
    api.add_model_label(model, 2, 'v1.0.0')
    api.add_model_label(model, 3, 'v1.1.0')
    make_api('admin').retire_model_label(model, 'v1.1.0', '起错版本号')
    weights = next(f for f in api.model_version(model, 1)['files'] if f['path'] == 'files/best.pt')
    stored = service.app.state.store.path(weights['sha256'])  # unique bytes: no other test's file goes missing
    aside = tmp_path / 'aside'
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    stored.rename(aside)
    try:
        r = subprocess.run(['node', str(WEB_TESTS / 'onnx.cjs'), service.url + '/', tokens['field'], tokens['algo'], model, str(shots)],
                           capture_output=True, text=True, timeout=240, cwd=WEB_TESTS)
    finally:
        aside.rename(stored)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['one'] == {'name': f'{model}@1.onnx', 'sha256': one['onnx_sha256']} and seen['oneRow'] == seen['one']
    assert seen['classes'] == 'classes.txt' and seen['tags'] == 0 and seen['icon'] == 1
    assert '下载失败' in seen['missing']
    assert seen['two'] == f'{model}@2_v1.0.0.onnx'                          # the highest of its 版本号
    assert seen['three'] == f'{model}@3.onnx'                               # a retired 版本号 names nothing
    assert seen['algo'] == ['下载 ONNX', '标记为候选', '标记为弃用…']


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_the_details_of_a_page_sit_behind_its_info_marks(service, tokens, api, tmp_path):
    """No page carries long grey hints; they are behind an ⓘ, which shows its text while pointed at or focused,
    keeps it after a click until Esc, a second click or a click elsewhere, and never ticks the box it sits by."""
    from fw import ops
    from helpers import quiet
    seed = int.from_bytes(os.urandom(2), 'big')
    name = f'W-{os.urandom(4).hex()}'
    api.create_dataset(name, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
    ops.import_folder(api, name, write_tree(tmp_path / 'base', {'20261101/a.png': real_png(64, 32, seed),
                                                                '20261101/a.json': label('scratch')}), echo=quiet)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'tips.cjs'), service.url + '/', tokens['algo'], tokens['admin'], name, str(shots)],
                       capture_output=True, text=True, timeout=240, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['long'] == {}                                                  # no long hint left on any page
    for hash in ('#/', f'#/add/{name}', f'#/upload/{name}', f'#/remove/{name}', '#/dataset-new', f'#/trainset-new?from={name}@1',
                 '#/places', '#/model-register', '#/batches',
                 '#/batch-new', '#/predictions', '#/events', '#/me', '#/users'):
        assert seen['marks'][hash] >= 1, hash
    assert 'fw dataset add' in seen['hovered'] and seen['label'] == '上传新增目录说明'
    assert seen['left'] is None                                                # pointed away: gone
    assert 'fw dataset add' in seen['clicked']['text'] and seen['clicked']['expanded'] == 'true'  # clicked: stays
    assert seen['escaped'] is None and seen['secondClick'] is None and seen['elsewhere'] is None
    assert 'fw dataset add' in seen['focused'] and seen['blurred'] is None
    assert seen['box'] == {'ticked': False, 'text': seen['box']['text']} and '20261103/' in seen['box']['text']
    assert not [x for x in seen['controls'] if '的说明' in x], seen['controls']  # the mark does not join their names
    assert '- checkbox "替换已有目录"' in seen['controls'], seen['controls']
    assert '- checkbox "仅添加所选目录下的子目录"' in seen['controls'], seen['controls']
    assert any(x.startswith('- textbox "池中目录"') for x in seen['controls']), seen['controls']


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
    api.create_dataset(name, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
    v1 = ops.import_folder(api, name, base, echo=quiet)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'change.cjs'), service.url + '/', tokens['algo'], name, str(shots),
                        str(day), '20261101'], capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['target'] == '20261103'
    assert seen['steps'] == ['2检查', '3计算哈希并预览', 2] and seen['card'] is True
    assert '新增 20261103/：4 个文件' in seen['plan'] and f'新版本 {name}@2：7 个文件' in seen['plan']
    assert '与数据池中已有图片内容相同' in seen['plan'] and '20261103/a_again.png = 20261101/a.png' in seen['plan']
    assert '图片相同但标注不同' in seen['plan'] and '（2 个目标） 和 20261101/a.png（1 个目标）' in seen['plan']
    assert seen['added'] == f'/v/{name}/2'
    assert '数据池中已有 20261103/' in seen['again'] and seen['offered'] == 0
    assert '移除 20261101/：2 个文件' in seen['removal'] and f'新版本 {name}@3：5 个文件' in seen['removal']
    assert seen['removed'] == f'/v/{name}/3'
    assert '新增 20261103/' in seen['versions'] and '移除 20261101/' in seen['versions'] and '整份导入' in seen['versions']
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
    assert '请填写数据池的类别表' in seen['noClasses']
    assert '图片和标注以外的文件' in seen['refused'] and '系统文件 1 个不入库' in seen['report']
    assert f'这是 {name} 的第一个版本' in seen['plan'] and '6 个文件' in seen['plan']
    assert seen['first'] == f'/v/{name}/1'
    assert '新增 1，删除 1，修改 1' in seen['plan2'] and '将删除' in seen['plan2'] and 'B/b.png' in seen['plan2']
    assert seen['second'] == f'/v/{name}/2'
    assert seen['errors'] == []

    v1, v2 = api.version(name, 1), api.version(name, 2)
    assert v1['note'] == '第一批' and v1['source'] == '网页上传 one' and v2['parent']['ref'] == v1['ref']
    d = api.dataset(name)
    assert (d['site'], d['line'], d['kind'], d['classes']) == ('SES', 'TX', 'pool', ['scratch', 'dent'])
    # the same version fw dataset import makes of the same folder: same manifest, every file readable
    other = f'{name}-cli'
    api.create_dataset(other, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
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
    api.create_dataset(name, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
    r = subprocess.run(['node', str(WEB_TESTS / 'race.cjs'), service.url + '/', tokens['algo'], name, str(a), str(b)],
                       capture_output=True, text=True, timeout=120, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert not seen['go'] and seen['puts'] == 0
    assert '图片和标注以外的文件' in seen['shown'] and '2 个文件' in seen['shown']


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_the_pages_ask_on_the_page_and_say_what_was_done(service, tokens, api, make_api, tmp_path):
    """Closing a batch and revoking a token are asked on the page (no browser box), each change made says so,
    a page that cannot be shown keeps its path and the way back, a slow page shows the bar on top."""
    from fw import ops
    from helpers import quiet
    seed = int.from_bytes(os.urandom(2), 'big')
    pool = f'W-{os.urandom(4).hex()}'
    api.create_dataset(pool, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
    ops.import_folder(api, pool, write_tree(tmp_path / 'p', {'20261101/x.png': real_png(64, 32, seed)}), echo=quiet)
    batch = ops.upload_batch(make_api('field'), write_tree(tmp_path / 'b' / '20261104', {'a.png': real_png(64, 32, seed + 1)}),
                             pool, '张三', echo=quiet)['name']
    r = httpx.post(f'{service.url}/api/v1/users/{tokens["field_name"]}/tokens', json={'label': '旧笔记本'},
                   headers={'Authorization': f'Bearer {tokens["admin"]}'})
    assert r.status_code == 201, r.text
    old = r.json()['id']
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'feedback.cjs'), service.url + '/', tokens['field'], batch, pool, str(shots)],
                       capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == [] and seen['dialogs'] == []
    assert '各版内容保留' in seen['closeAsk'] and seen['escaped'] == 0
    assert seen['closeToast'] == f'已关闭 {batch}'
    assert api.batch(batch)['status'] == 'closed' and api.batch(batch)['steps'][-1]['comment'] == '重复上传'
    assert f'吊销凭据 {old}（旧笔记本）' in seen['revokeAsk'] and seen['kept'] == [0, 1]
    assert seen['revokeToast'] == f'已吊销凭据 {old}'
    assert seen['errorPath'] == ['概览', '数据集', 'no-such-pool'] and seen['errorBack'] == 1
    assert seen['busy'] is True and seen['idle'] is True


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_a_reviewer_marks_problem_images_in_the_viewer_and_rejects_with_their_notes(service, tokens, api, make_api, tmp_path):
    """Marked in the viewer, kept across a reload and across two tabs of one browser (each change is made to the
    marks as kept now, so the batch page left open in a second tab does not undo the viewer's marks; review of #53),
    and sent with the rejection."""
    from fw import ops
    from helpers import quiet
    seed = int.from_bytes(os.urandom(2), 'big')
    a, b, c = (real_png(64, 32, seed + i) for i in range(3))
    pool = f'W-{os.urandom(4).hex()}'
    name = f'B-{os.urandom(4).hex()}'
    api.create_dataset(pool, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
    ops.import_folder(api, pool, write_tree(tmp_path / 'base', {'20261101/x.png': real_png(64, 32, seed + 9)}), echo=quiet)
    day = {'a.png': a, 'a.json': label('scratch'), 'b.png': b, 'c.png': c, 'c.json': label('dent')}
    field = make_api('field')
    ops.upload_batch(field, write_tree(tmp_path / 'one' / '20261104', day), pool, '张三', name=name,
                     reviewer=tokens['algo_name'], echo=quiet)
    api.batch_step(name, 'reject', {'comment': '再看一遍'})
    day.update({'b.json': label('dent'), 'c.json': label('scratch')})   # b labelled, c relabelled
    ops.update_batch(field, name, write_tree(tmp_path / 'two' / '20261104', day), '张三', reviewer='-', echo=quiet)
    shots = Path(os.environ.get('FW_WEB_SHOTS') or tmp_path / 'shots')
    shots.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', str(WEB_TESTS / 'review.cjs'), service.url + '/', tokens['algo'], tokens['field'], name, str(shots)],
                       capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['start'] == {'button': '开始复核', 'seen': '已看 0 / 3'}
    first = seen['first']
    assert first['title'] == 'a.png' and first['progress'] == '已看 1 / 3 · 有问题 0'
    assert (first['shots'], first['on']) == (3, 1) and first['filters'] == ['全部3', '有改动2', '有问题0', '未看2']
    z = seen['zoom']
    pct = lambda t: int(t.rstrip('%'))
    assert pct(z['zoomedIn']) > pct(z['fit']) and pct(z['wheeled']) > pct(z['zoomedIn']) and z['moved'] and z['back'] == z['fit']
    assert z['one'] == '100%'
    assert seen['hidden'] == 'false' and seen['shownAgain'] == 'true'
    assert seen['noteBefore'] is False
    assert seen['marked'] == {'button': '取消标记', 'progress': '已看 1 / 3 · 有问题 1', 'flagged': 1}
    assert seen['changed']['shots'] == 3                     # b, c, and a (shown now) in the strip
    assert seen['changed']['last'] is True and seen['changed']['progress'] == '已看 3 / 3 · 有问题 1'
    assert seen['kept'] == {'button': '取消标记', 'note': '右下角漏标一个划痕', 'progress': '已看 3 / 3 · 有问题 1'}
    tabs = seen['tabs']   # the batch page in a second tab: neither tab's older copy of the marks undoes the other's
    assert (tabs['before'], tabs['told'], tabs['added']) == ('问题图片（1）', '缺少凹坑框', '问题图片（3）')
    assert tabs['typed'] == {'a.png': '右下角漏标一个划痕', 'b.png': '缺少凹坑框，在左上角', 'c.png': ''}
    assert tabs['removed'] == {'marks': {'a.png': '右下角漏标一个划痕'}, 'list': '问题图片（1）', 'button': '标为有问题', 'shots': 1}
    assert '附 1 张问题图片' in seen['ask'] and '退回意见' in seen['noComment']
    assert 'a.png' in seen['steps'] and '右下角漏标一个划痕' in seen['steps'] and seen['left'] == []
    assert '右下角漏标一个划痕' in seen['owner']['note'] and '请补标划痕' in seen['owner']['note'] and seen['owner']['panel'] == 0
    step = api.batch(name)['steps'][-1]
    assert (step['step'], step['comment'], step['images'], step['notes']) == (
        'reject', '请补标划痕', ['a.png'], {'a.png': '右下角漏标一个划痕'})


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
    api.create_dataset(pool, 'SES', 'TX', 'pool', '', classes=helpers.CLASSES)
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
    assert '请拖放目录' in seen['droppedFile'] and seen['dropped'] == '20261104/ · 4 个文件'
    assert '每次只能拖放一个目录' in seen['droppedTwo']
    assert seen['cancelled'] == {'text': seen['droppedTwo'], 'reading': 0}   # choosing, then cancelling, mid-read
    assert seen['dir'] == '20261104' and '标注人姓名' in seen['noLabeler']
    assert seen['uploaded'] == '待复核' and name in seen['todo']
    assert '退回意见' in seen['noComment']
    assert seen['reviewStart'] == '开始复核' and seen['wrongPath'] == '第 1 版中不存在图片 zz.png。'
    assert seen['marks'] == '问题图片（1）'
    assert 'b.png 漏标' in seen['rejected'] and 'b.png：缺少划痕框' in seen['rejected']
    assert '第 1 版被退回，请修改后上传第 2 版' in seen['rejectedCard'] and '缺少划痕框' in seen['rejectedCard']
    assert seen['revise'] == '上传第 2 版'
    assert [c for c, _ in seen['flowRejected']] == ['on', 'todo', 'bad', 'todo'] and '第 1 版已退回' in seen['flowRejected'][2][1]
    assert seen['fieldMore'] == ['运行预标注…', '复制取回命令', '关闭批次…']   # the owner may have a rejected batch pre-labelled
    assert seen['sameReviewer'] == tokens['algo_name']
    assert '标注有变化的图片 1 张：加框 1' in seen['changes'] and seen['resubmitted'] == '待复核'
    assert seen['since']['fixed'] == '1 / 1' and '缺少划痕框' in seen['since']['rows'] and '已修改' in seen['since']['rows']
    assert seen['since']['changed'] == '只看改动的 1 张' and seen['since']['start'] == '开始复核'
    assert '新增 20261104/：4 个文件' in seen['plan'] and f'建立 {pool}@2' in seen['plan']
    assert '版本建立后不能修改或删除' in seen['importAsk'] and f'建立 {pool}@2' in seen['importAsk']
    assert f'已入库，建立 {pool}@2' in seen['imported'] and f'用 {pool}@2 建立训练集快照' in seen['imported']
    assert seen['flowDone'] == ['done'] * 4 and seen['buttons'] == 0
    b = api.batch(name)
    assert b['status'] == 'imported' and [r['labeler'] for r in b['revisions']] == ['张三', '李四']
    assert [s['step'] for s in b['steps']] == ['upload', 'submit', 'reject', 'upload', 'submit', 'approve', 'import']
    assert (b['steps'][2]['images'], b['steps'][2]['notes']) == (['b.png'], {'b.png': '缺少划痕框'})
    assert api.version(pool, 2)['source'] == f'batch:{name}@2'


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_the_reviewer_of_a_batch_is_changed_in_the_browser(service, tokens, api, make_api, tmp_path):
    """The owner of a batch sent to the wrong reviewer asks another one from 更多; the trail says so and the
    first reviewer is no longer offered the review."""
    from fw import ops
    from helpers import quiet
    from test_trainsets import pool, sample_files
    data = pool(api, tmp_path / 'p', sample_files('2026-09', 2))
    field = make_api('field')
    name = ops.upload_batch(field, write_tree(tmp_path / 'b' / '20261116', {'x.png': real_png(64, 32, 3)}), data, '张三',
                            echo=quiet)['name']
    field.batch_step(name, 'submit', {'reviewer': tokens['algo_name']})
    r = subprocess.run(['node', str(WEB_TESTS / 'reassign.cjs'), service.url + '/', tokens['field'], tokens['algo'], name,
                        tokens['admin_name']], capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert '更换复核人…' in seen['more'] and seen['title'] == '更换第 1 版的复核人'
    assert tokens['algo_name'] not in seen['choices'] and tokens['admin_name'] in seen['choices']
    assert seen['unpicked'] == '请选择新的复核人。'
    assert seen['toast'] == f'已更换复核人为 {tokens["admin_name"]}'
    assert f'复核人 {tokens["admin_name"]}' in seen['facts']
    assert len(seen['trail']) == 1 and tokens['admin_name'] in seen['trail'][0] and '原复核人出差' in seen['trail'][0]
    assert seen['oldReview'] == 0
    assert api.batch(name)['reviewer'] == tokens['admin_name']


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_the_jobs_pages_say_whether_a_gpu_machine_is_there(service, tokens, api, make_worker, fake_conda, tmp_path):  # noqa: F811
    """The jobs page lists each GPU machine as 在线 or 离线; a queued job whose machine has never asked for work
    says that it waits for fw worker to start there."""
    from test_jobs import runner
    a = make_worker()
    r = runner(a, fake_conda)
    job = api.submit_job('check', runner=r['runner'])
    other = make_worker()
    assert other.claim() is None                     # another machine is there; this job is not for it
    out = subprocess.run(['node', str(WEB_TESTS / 'machines.cjs'), service.url + '/', tokens['algo'], str(job['id'])],
                         capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert out.returncode == 0, out.stdout + out.stderr
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    machines = {text.split(' ')[0]: (dot, text) for dot, text in seen['machines']}
    assert machines[a.name] == ('dot gray', f'{a.name} 离线（尚未连接过平台）')
    assert machines[other.name] == ('dot green', f'{other.name} 在线')
    assert seen['waits'] == f'推理环境 {r["runner"]}@1 所在的 GPU 机 {a.name} 离线（尚未连接过平台），任务将在该机器启动 fw worker 后运行。'


@pytest.mark.skipif(not node_with_playwright(), reason='Playwright for node is not installed')
def test_folders_are_added_from_the_newest_version_and_from_the_overview(service, tokens, api, make_api, tmp_path):
    """上传新增目录 sits beside the title of the newest version of a pool or test set (not an older one) and in
    the ⋯ of their rows on the line's overview; a snapshot's row has no ⋯; a field account sees none of them."""
    from test_trainsets import add_version, pool, sample_files
    tag = os.urandom(2).hex().upper()
    line = f'AF{tag}'
    data = pool(api, tmp_path / 'a', sample_files('2026-09', 3), line=line, name=f'SES-{line}-pool')
    add_version(api, tmp_path / 'b', data, sample_files('2026-10', 2))
    test = f'SES-{line}-test'
    api.create_dataset(test, 'SES', line, 'test', '')
    add_version(api, tmp_path / 't', test, sample_files('2026-11', 2))
    r = subprocess.run(['node', str(WEB_TESTS / 'addfrom.cjs'), service.url + '/', tokens['algo'], tokens['field'], data, test,
                        'SES', line], capture_output=True, text=True, timeout=180, cwd=WEB_TESTS)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(r.stdout.strip().splitlines()[-1])
    assert seen['errors'] == []
    assert seen['newest'] == [['add-folders', '上传新增目录'], ['trainset-from', '建立训练集快照']]
    assert seen['older'] == [['trainset-from', '建立训练集快照']]
    assert seen['testNewest'] == [['add-folders', '上传新增目录']]
    assert dict(seen['rows']) == {data: 1, test: 1}
    assert seen['poolMenu'] == [['上传新增目录', f'#/add/{data}'], ['建立训练集快照', f'#/trainset-new?from={data}%402']]
    assert seen['testMenu'] == [['上传新增目录', f'#/add/{test}']]
    assert seen['opened'] == f'#/add/{data}'
    assert seen['fieldNewest'] == [] and dict(seen['fieldRows']) == {data: 0, test: 0}
