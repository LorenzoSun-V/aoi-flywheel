"""Evaluation jobs (step 3, part 2): fw eval run, the worker running aoi-deteval in a runner, the report.

aoi-deteval and conda are stand-ins here: a conda script that prints package listings and runs a fake deteval,
which writes predictions and a summary the way the real one does (one set per folder, group 'eval')."""
import json
import os
import stat
import sys
import uuid

import pytest

from helpers import quiet, write
from fw import cli as fw_cli
from fw import ops, worker
from fw.api import ApiError
from fwserver import db

from test_jobs import make_worker  # noqa: F401  (fixture)
from test_models import model, onnx_bytes, snapshot  # noqa: F401  (fixtures)
from test_trainsets import label, png, pool, sample_files, status_of

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='the worker runs on the Linux GPU machine')

FAKE_DETEVAL = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args else None
with open(os.environ['FAKE_DETEVAL_LOG'], 'a') as f:
    f.write(json.dumps(args) + '\n')
cfg = json.loads(Path(opt('--sets')).read_text())
IMG = {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp'}
def images(d):
    return sorted(p for p in Path(d).iterdir() if p.suffix.lower() in IMG)
assert args[0] == 'evaluate', args
pred = json.loads(Path(opt('--preds')).read_text())
name = pred['name']
rows = {}
for s, d in cfg['sets'].items():
    boxes = sum(len(json.loads(p.with_suffix('.json').read_text())['shapes'])
                for p in images(d) if p.with_suffix('.json').exists())
    boxes += sum(len([x for x in t.read_text().splitlines() if x.strip()]) for t in
                 (Path(str(p.parent).replace('/images', '/labels')) / (p.stem + '.txt') for p in images(d))
                 if t.exists())
    rows[s] = {'model': name, 'set': s, 'images': len(pred['sets'][s]), 'boxes': boxes, 'mAP50': 0.5,
               'mAP50-95': 0.3, 'P': 0.8, 'R': float('nan')}
for g, members in (cfg.get('groups') or {}).items():
    rows[g] = {'model': name, 'set': g, 'images': sum(rows[m]['images'] for m in members),
               'boxes': sum(rows[m]['boxes'] for m in members), 'mAP50': 0.5, 'mAP50-95': 0.3, 'P': 0.8,
               'R': float('nan')}
if os.environ.get('FAKE_DETEVAL_SKIP'):
    rows['eval']['images'] -= 1
out = Path(opt('--out')); out.mkdir(parents=True, exist_ok=True)
summary = {name: {'weights': pred.get('weights'), 'framework': pred['framework'], 'iomin': 0.5, 'agnostic': False,
                  'thresholds': {'f1': 0.3}, 'reached': {}, **rows}}
(out / 'summary.json').write_text(json.dumps(summary))
(out / 'run_info.json').write_text(json.dumps({'deteval_version': '0.3.0'}))
(out / 'metrics.csv').write_text('model,set,mAP50\n')
print('evaluated')
'''

# The deploy code's inference module and OpenCV, as trt_predict.py uses them: every image gets one box of class 0
# and one of a class beyond the model's class table (left out).
FAKE_PY_RFDETR = r'''
import os
class RfdetrDetector:
    def __init__(self, model_path, device_id=0, conf_threshold=0.3, start_class_id=0):
        assert open(model_path, 'rb').read().startswith(b'ENGINE'), model_path
        if os.environ.get('FAKE_DETEVAL_FAIL'):
            raise RuntimeError('[RfdetrDetector] Init failed: CUDA out of memory')
        self.conf = conf_threshold
    def detect(self, image):
        return [[1.0, 2.0, 29.0, 38.0, 0.9, 0], [5.0, 5.0, 10.0, 10.0, 0.8, 99]]
    def release(self):
        pass
'''
FAKE_CV2 = r'''
import os
IMREAD_COLOR = 1
def imread(path, flag):
    return path if os.path.isfile(path) else None
'''


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def gpu(tmp_path, monkeypatch, make_worker):  # noqa: F811
    """A GPU machine as the worker sees it: a fake conda (environments in listings/; `run` runs the command, a
    python one with the fake py_rfdetr and cv2 importable), a fake onnx2engine (writes <onnx>.engine; each
    build is a line of builds.log), a fake nvidia-smi and the fake deteval evaluate. listings/FAIL makes the
    inference fail, listings/SKIP makes deteval miss an image."""
    listings = tmp_path / 'listings'
    listings.mkdir()
    modules = tmp_path / 'modules'
    modules.mkdir()
    (modules / 'py_rfdetr.py').write_text(FAKE_PY_RFDETR)
    (modules / 'cv2.py').write_text(FAKE_CV2)
    (tmp_path / 'deteval.py').write_text(FAKE_DETEVAL)
    executable(tmp_path / 'conda', '#!/bin/sh\n'
               f'd="{listings}"\n'
               '[ "$1" = run ] && [ ! -f "$d/$3.txt" ] && { echo "EnvironmentLocationNotFound: $3" >&2; exit 1; }\n'
               'if [ "$1" = run ] && [ "$4" = python ] && [ "$5" = -m ]; then echo "pip==24.0"; exit 0; fi\n'
               'if [ "$1" = run ] && [ "$4" = python ] && [ "$5" = -c ]; then echo "10.14.1.48"; exit 0; fi\n'
               'if [ "$1" = run ] && [ "$4" = --no-capture-output ]; then\n'
               f'  export FAKE_DETEVAL_LOG="{tmp_path / "deteval.log"}" PYTHONPATH="{modules}"\n'
               '  [ -f "$d/FAIL" ] && export FAKE_DETEVAL_FAIL=1\n'
               '  [ -f "$d/SKIP" ] && export FAKE_DETEVAL_SKIP=1\n'
               '  shift 4\n'
               f'  [ "$1" = python ] && {{ shift; exec "{sys.executable}" "$@"; }}\n'
               f'  [ "$1" = deteval ] && {{ shift; exec "{sys.executable}" "{tmp_path / "deteval.py"}" "$@"; }}\n'
               '  exec "$@"\n'
               'fi\n'
               '[ "$1" = list ] && [ -f "$d/$3.txt" ] && cat "$d/$3.txt"\n')
    onnx2engine = executable(tmp_path / 'onnx2engine', '#!/bin/sh\n'
                             f'echo "$1" >> "{tmp_path / "builds.log"}"\n'
                             '{ printf ENGINE; cat "$1"; } > "${1%.onnx}.engine"\n'
                             'echo "Convert onnx model to TensorRT engine model successfully!"\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    executable(bin_dir / 'nvidia-smi', '#!/bin/sh\necho "NVIDIA GeForce RTX 3070, 550.54.14"\n')
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
    monkeypatch.setenv('FW_CONDA', str(tmp_path / 'conda'))
    monkeypatch.setenv('FW_CACHE', str(tmp_path / 'fw-cache'))
    w = make_worker()
    w.listings = listings
    w.onnx2engine = str(onnx2engine)
    w.calls = lambda: [json.loads(x) for x in (tmp_path / 'deteval.log').read_text().splitlines()]
    w.builds = lambda: (tmp_path / 'builds.log').read_text().splitlines() if (tmp_path / 'builds.log').exists() \
        else []
    return w


def add_runner(w, framework='rfdetr', env=None):
    env = env or f'env-{uuid.uuid4().hex[:6]}'
    (w.listings / f'{env}.txt').write_text(f'@EXPLICIT\nhttps://x/{env}.tar.bz2\n')
    return worker.register_runner(w, env, env, '', framework, w.onnx2engine if framework else '')


def trained(api, model, snapshot, tmp_path, weights='best.pt'):  # noqa: F811
    """A model version with its ONNX (what is evaluated) and weights (never loaded by the platform)."""
    d = tmp_path / f'run-{uuid.uuid4().hex[:6]}'
    write(d, 'best.onnx', onnx_bytes(uuid.uuid4().hex))
    write(d, weights, b'weights ' + uuid.uuid4().hex.encode())
    return ops.register_model(api, model, d / 'best.onnx', snapshot['ref'], weights=d / weights, echo=quiet)


def test_eval_on_folders_of_a_pool(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    mv = trained(api, model, snapshot, tmp_path, weights='best.pth')
    files = {**sample_files('A/x', 3), **sample_files('A/y/z', 2), **sample_files('B', 4)}
    files['A/x/neg.png'] = png(uuid.uuid4().hex)  # no label: a normal image
    data = pool(api, tmp_path, files)
    r = add_runner(gpu)
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{data}@1', 'dirs': ['A']})
    p = job['params']
    assert job['kind'] == 'eval' and job['runner'] == r['ref'] and p['data']['images'] == 6
    assert p['data']['dirs'] == ['A'] and p['warnings'] == []
    assert [f[0] for f in p['model']['files']] == ['model.onnx']  # the weights are never sent to a worker
    assert worker.run_worker(gpu, quiet, once=True) == 1
    done = api.job(job['id'])
    assert done['status'] == 'done', done['error']
    e = api.eval(done['result']['eval_id'])
    assert e['model_version']['ref'] == mv['ref'] and e['testset']['ref'] == f'{data}@1' and e['dirs'] == ['A']
    assert e['job_id'] == job['id'] and e['runner'] == r['ref'] and e['tool'] == 'aoi-deteval'
    assert e['tool_version'] == '0.3.0' and e['metrics']['images'] == 6 and e['metrics']['boxes'] == 5
    assert e['metrics']['R'] is None and e['metrics']['thresholds'] == {'f1': 0.3}  # NaN kept as null
    files = {f['path']: f['sha256'] for f in e['files']}
    name = p['deteval']['name']
    assert {'metrics.json', 'files/summary.json', 'files/run_info.json', 'files/eval_sets.yaml',
            f'files/preds/{name}.json', 'files/engine.json'} <= set(files)
    preds = json.loads(gpu.read_object(files[f'files/preds/{name}.json']))
    assert preds['framework'] == 'rfdetr-tensorrt' and preds['conf'] == 0.001 and preds['nc'] == len(mv['classes'])
    assert all(boxes == [[1.0, 2.0, 30.0, 40.0, 0.9, 0]] for s in preds['sets'].values() for boxes in s.values())
    assert sum(len(s) for s in preds['sets'].values()) == 6
    engine = json.loads(gpu.read_object(files['files/engine.json']))
    assert engine['gpu'] == 'NVIDIA GeForce RTX 3070, 550.54.14' and engine['trt_version'] == '10.14.1.48'
    assert engine['precision'] == 'fp16' and engine['onnx_sha256'] == mv['onnx_sha256']
    [evaluate] = gpu.calls()
    assert evaluate[0] == 'evaluate' and evaluate[evaluate.index('--conf-set') + 1] == 'eval'
    sets = json.loads(gpu.read_object(files['files/eval_sets.yaml']))
    assert sorted(sets['sets']) == ['d1', 'd2'] and sets['groups'] == {'eval': ['d1', 'd2']}
    assert sets['names'] == mv['classes']
    log = ''.join(x['text'] for x in api.job_log(job['id'])['pieces'])
    assert '推理了 6 张图，6 个框；去掉 6 个类别超出模型类别表的框' in log and 'onnx2engine' in log
    assert not list((tmp_path / 'fw-cache' / 'jobs').iterdir())  # the job's folder is gone


def test_an_engine_is_built_once_per_onnx_and_machine(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    """The engine is a cache of this machine: the same ONNX is built once; another ONNX, or another build of
    onnx2engine (another runner version), builds again."""
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    params = {'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'}
    for _ in range(2):
        api.submit_job('eval', runner=r['runner'], params=params)
    assert worker.run_worker(gpu, quiet, once=True) == 2
    assert len(gpu.builds()) == 1
    other = trained(api, model, snapshot, tmp_path)
    api.submit_job('eval', runner=r['runner'], params={**params, 'model': other['ref']})
    worker.run_worker(gpu, quiet, once=True)
    assert len(gpu.builds()) == 2
    with open(gpu.onnx2engine, 'a') as f:
        f.write('# rebuilt\n')
    r2 = worker.register_runner(gpu, r['runner'], r['conda_env'], '', 'rfdetr', gpu.onnx2engine)
    assert r2['created'] and r2['onnx2engine_sha256'] != r['onnx2engine_sha256']
    job = api.submit_job('eval', runner=r['ref'], params=params)  # the old version: the program changed
    worker.run_worker(gpu, quiet, once=True)
    assert api.job(job['id'])['status'] == 'failed' and 'onnx2engine' in ''.join(
        x['text'] for x in api.job_log(job['id'])['pieces'])
    api.submit_job('eval', runner=r2['ref'], params=params)
    worker.run_worker(gpu, quiet, once=True)
    assert len(gpu.builds()) == 3


def test_a_runner_for_evaluations_names_its_onnx2engine(api, gpu):
    env = f'env-{uuid.uuid4().hex[:6]}'
    (gpu.listings / f'{env}.txt').write_text('@EXPLICIT\n')
    with pytest.raises(worker.WorkerError):
        worker.register_runner(gpu, env, env, '', 'rfdetr', '')
    with pytest.raises(worker.WorkerError):
        worker.register_runner(gpu, env, env, '', 'rfdetr', '/no/such/onnx2engine')
    digest = gpu.put_bytes(worker.conda_listing(env))
    for body in ({'framework': 'rfdetr'}, {'framework': 'ultralytics', 'onnx2engine': gpu.onnx2engine,
                                          'onnx2engine_sha256': '0' * 64},
                 {'onnx2engine': gpu.onnx2engine, 'onnx2engine_sha256': '0' * 64}):
        assert status_of(lambda: gpu.register_runner({'name': env, 'conda_env': env, 'env_sha256': digest,
                                                       **body})) == 400


def test_eval_on_a_snapshot_split_warns_about_training_images(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    test = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'})
    assert test['params']['data']['images'] == 3 and test['params']['warnings'] == []
    train = api.submit_job('eval', runner=r['runner'],
                           params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:train'})
    assert 'train' in train['params']['warnings'][0] and snapshot['ref'] in train['params']['warnings'][0]
    worker.run_worker(gpu, quiet, once=True)
    e = api.eval(api.job(test['id'])['result']['eval_id'])
    assert e['testset']['ref'] == f'{snapshot["ref"]}:test' and e['split'] == 'test' and e['dirs'] == []
    assert e['metrics']['images'] == 3 and e['metrics']['boxes'] == 3  # the snapshot's YOLO labels were read
    warned = api.eval(api.job(train['id'])['result']['eval_id'])
    assert '训练集快照' in warned['note']


def test_what_an_eval_needs(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    no_framework = add_runner(gpu, framework='')
    data = pool(api, tmp_path, {**sample_files('A', 2), 'T/images/t.png': png('t'),
                                'T/labels/t.txt': b'0 .5 .5 .1 .1\n', 'S/a.png': png('sa'), 'S/a.jpg': png('sb')})
    onnx_only = ops.register_model(api, model, write(tmp_path / 'o', 'm.onnx', onnx_bytes('only')),
                                   snapshot['ref'], echo=quiet)

    def submit(**params):
        params.setdefault('model', mv['ref'])
        params.setdefault('data', f'{data}@1')
        params.setdefault('dirs', ['A'])
        return status_of(lambda: api.submit_job('eval', runner=params.pop('runner', r['runner']), params=params))

    assert submit() == 200
    assert submit(runner=no_framework['runner']) == 400  # registered without --framework
    assert submit(model=onnx_only['ref']) == 200  # the ONNX is what is evaluated: weights are not needed
    assert submit(dirs=['T']) == 400  # YOLO txt only: the pool's own class numbers
    assert submit(dirs=['S']) == 400  # a.png and a.jpg would share a.json
    assert submit(dirs=['nope']) == 400
    assert submit(dirs=['../A']) == 400
    assert submit(data=snapshot['ref'], dirs=[]) == 400  # a snapshot needs its split
    assert submit(data=f'{snapshot["ref"]}:test') == 400  # and is evaluated whole
    assert submit(data=f'{data}@1:test') == 400  # a pool has no split
    assert submit(imgsz=640) == 400 and submit(batch=1) == 400 and submit(conf=0.1) == 400  # the engine decides
    with pytest.raises(ApiError) as e:
        gpu.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{data}@1'})
    assert e.value.status == 403  # a worker does not submit


def test_an_eval_that_missed_images_registers_nothing(api, gpu, model, snapshot, tmp_path, monkeypatch):  # noqa: F811
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    (gpu.listings / 'SKIP').write_text('')
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'})
    worker.run_worker(gpu, quiet, once=True)
    j = api.job(job['id'])
    assert j['status'] == 'failed' and '有图没评到' in j['error'] and api.evals(model) == []
    (gpu.listings / 'SKIP').unlink()
    (gpu.listings / 'FAIL').write_text('')
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'})
    worker.run_worker(gpu, quiet, once=True)
    j = api.job(job['id'])
    assert j['status'] == 'failed' and 'TensorRT 推理没有成功' in j['error']
    assert 'CUDA out of memory' in ''.join(x['text'] for x in api.job_log(job['id'])['pieces'])


def test_an_eval_in_a_changed_runner_fails(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    (gpu.listings / f'{r["conda_env"]}.txt').write_text('@EXPLICIT\nhttps://x/torch-other.tar.bz2\n')
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'})
    worker.run_worker(gpu, quiet, once=True)
    j = api.job(job['id'])
    assert j['status'] == 'failed' and '推理环境和登记时不一样' in j['error'] and api.evals(model) == []


def test_a_cancelled_or_lost_eval_registers_nothing(api, gpu, model, snapshot, tmp_path, database):  # noqa: F811
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'})
    c = gpu.claim()
    api.cancel_job(job['id'])
    j = gpu.finish(job['id'], c['token'], 'done', {'files': [], 'tool_version': '0.3.0'})
    assert j['status'] == 'cancelled' and api.evals(model) == []
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'})
    c = gpu.claim()
    with db.connect(database) as conn:
        conn.execute("UPDATE jobs SET lease_expires = now() - interval '1 second' WHERE id = %s", (job['id'],))
    assert status_of(lambda: gpu.finish(job['id'], c['token'], 'done', {'files': []})) == 409
    assert api.evals(model) == []


def test_eval_run_on_the_command_line(service, tokens, gpu, model, snapshot, tmp_path, monkeypatch,  # noqa: F811
                                      capsys, api):
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    monkeypatch.setenv('FW_CONFIG', str(tmp_path / 'config.json'))
    assert fw_cli.main(['login', '--server', service.url, '--token', tokens['algo']]) == 0
    assert fw_cli.main(['eval', 'run', mv['ref'], '--data', f'{snapshot["ref"]}:train', '--runner', r['runner']]) == 0
    out = capsys.readouterr().out
    assert '已提交评估任务' in out and '注意' in out and '张图' in out
    job_id = int(out.split('已提交评估任务 ')[1].split('：')[0])
    worker.run_worker(gpu, quiet, once=True)
    assert fw_cli.main(['job', 'show', str(job_id)]) == 0
    out = capsys.readouterr().out
    assert '评估报告 #' in out and 'mAP50 0.5' in out
    eval_id = api.job(job_id)['result']['eval_id']
    assert fw_cli.main(['eval', 'show', str(eval_id)]) == 0
    assert f'由评估任务 {job_id} 在推理环境 {r["ref"]} 里算出' in capsys.readouterr().out
    assert fw_cli.main(['eval', 'run', mv['ref'], '--data', 'nope@1', '--runner', r['runner']]) == 1


def test_the_database_keeps_eval_reports_consistent(sql, api, gpu, model, snapshot, tmp_path):  # noqa: F811
    import psycopg
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'})
    worker.run_worker(gpu, quiet, once=True)
    e = api.eval(api.job(job['id'])['result']['eval_id'])
    row = sql.execute('SELECT * FROM eval_reports WHERE id = %s', (e['id'],)).fetchone()
    for dirs, job_id, why in ((['A'], None, 'without folders'), (['/A'], None, 'without folders'),
                              ([], job['id'], 'not a running evaluation')):
        with pytest.raises(psycopg.errors.RaiseException, match=why):
            with sql.transaction():
                sql.execute('INSERT INTO eval_reports (model_version_id, testset_id, split, fileset_id, metrics, '
                            'created_by, dirs, job_id, runner_version_id) '
                            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
                            (row['model_version_id'], row['testset_id'], row['split'], row['fileset_id'], '{}',
                             row['created_by'], dirs, job_id, row['runner_version_id'] if job_id else None))


def test_labels_next_to_images_are_read(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    """JSON labels of a pool keep their class names: they are read against the model's class table."""
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    data = pool(api, tmp_path, {'C/1.png': png('c1'), 'C/1.json': label(mv['classes'][0], mv['classes'][0])})
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{data}@1'})
    worker.run_worker(gpu, quiet, once=True)
    e = api.eval(api.job(job['id'])['result']['eval_id'])
    assert e['metrics']['boxes'] == 2 and e['dirs'] == []


# --- review of PR #13 ------------------------------------------------------------------------------------
def test_a_snapshot_numbered_by_another_class_table_is_refused(api, gpu, model, tmp_path):  # noqa: F811
    from test_trainsets import make
    src = pool(api, tmp_path, {**sample_files('a', 4, names=('scratch', 'dent')),
                               **sample_files('b', 3, names=('dent',))})
    same = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'], classes=['scratch', 'dent'], test_dirs=['b'])
    swapped = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'], classes=['dent', 'scratch'], test_dirs=['b'])
    mv = trained(api, model, same, tmp_path)
    assert mv['classes'] == ['scratch', 'dent']
    r = add_runner(gpu)
    assert api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{same["ref"]}:test'})
    with pytest.raises(ApiError) as e:
        api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{swapped["ref"]}:test'})
    assert e.value.status == 400 and '类别表' in e.value.message


def test_labels_with_upper_case_extensions(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    """x.JSON is the label of x.png (laid out as x.json, which deteval reads); an image with only labels/x.TXT is
    a YOLO-only image like one with x.txt."""
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    data = pool(api, tmp_path, {'J/1.png': png(uuid.uuid4().hex), 'J/1.JSON': label('scratch', 'scratch'),
                                'T/images/2.png': png(uuid.uuid4().hex), 'T/labels/2.TXT': b'0 .5 .5 .1 .1\n'})
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{data}@1', 'dirs': ['J']})
    worker.run_worker(gpu, quiet, once=True)
    done = api.job(job['id'])
    assert done['status'] == 'done', done['error']
    assert api.eval(done['result']['eval_id'])['metrics']['boxes'] == 2
    with pytest.raises(ApiError) as e:
        api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{data}@1', 'dirs': ['T']})
    assert e.value.status == 400 and 'YOLO txt' in e.value.message


def test_two_jobs_with_the_same_result_keep_a_report_each(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    """Two runners send byte-identical output: each job gets its own report, recording that job and runner (the
    files are stored once); the same job sending it again gets its own report back."""
    mv = trained(api, model, snapshot, tmp_path)
    r1, r2 = add_runner(gpu), add_runner(gpu)
    params = {'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'}
    first = api.submit_job('eval', runner=r1['runner'], params=params)
    worker.run_worker(gpu, quiet, once=True)
    e1 = api.eval(api.job(first['id'])['result']['eval_id'])
    files = [[f['path'], f['sha256'], f['size']] for f in e1['files']]
    second = api.submit_job('eval', runner=r2['runner'], params=params)
    c = gpu.claim()
    j = gpu.finish(second['id'], c['token'], 'done', {'files': files, 'tool_version': '0.3.0'})
    assert j['status'] == 'done'
    e2 = api.eval(j['result']['eval_id'])
    assert e2['id'] != e1['id'] and e2['job_id'] == second['id'] and e2['runner'] == r2['ref']
    assert api.eval(e1['id'])['job_id'] == first['id'] and api.eval(e1['id'])['runner'] == r1['ref']
    again = gpu.finish(second['id'], c['token'], 'done', {'files': files, 'tool_version': '0.3.0'})
    assert again['result']['eval_id'] == e2['id']


def test_a_relative_work_dir(api, gpu, model, snapshot, tmp_path, monkeypatch):  # noqa: F811
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FW_WORK_DIR', 'jobs')
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'})
    worker.run_worker(gpu, quiet, once=True)
    done = api.job(job['id'])
    assert done['status'] == 'done', done['error']
    assert not list((tmp_path / 'jobs').iterdir())


def test_pool_labels_must_name_classes_of_the_model(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    data = pool(api, tmp_path, {'A/1.png': png(uuid.uuid4().hex), 'A/1.json': label('Scratch'),
                                'B/2.png': png(uuid.uuid4().hex), 'B/2.json': label('no-such-class'),
                                'C/3.png': png(uuid.uuid4().hex), 'C/3.json': label(mv['classes'][0])})
    for d in ('A', 'B'):
        with pytest.raises(ApiError) as e:
            api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{data}@1', 'dirs': [d]})
        assert e.value.status == 400 and '类别表' in e.value.message
    assert api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{data}@1', 'dirs': ['C']})
