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
print('environment:', ' '.join(sorted(os.environ)))
cfg = json.loads(Path(opt('--sets')).read_text())
IMG = {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp'}
def images(d):
    return sorted(p for p in Path(d).iterdir() if p.suffix.lower() in IMG)
if args[0] == 'predict':
    if os.environ.get('FAKE_DETEVAL_FAIL'):
        print('CUDA out of memory'); sys.exit(3)
    weights = Path(opt('--weights'))
    assert weights.is_file(), weights
    print('training_config beside weights:', (weights.parent / 'training_config.json').is_file())
    sets = {s: {p.name: [[1, 2, 30, 40, 0.9, 0]] for p in images(d)} for s, d in cfg['sets'].items()}
    out = Path(opt('--out')); out.mkdir(parents=True, exist_ok=True)
    (out / (opt('--name') + '.json')).write_text(json.dumps({'schema_version': 1, 'name': opt('--name'),
                                                             'framework': opt('--framework'), 'sets': sets}))
    print('predicted', sum(len(v) for v in sets.values()), 'images')
else:
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
    summary = {name: {'weights': None, 'framework': pred['framework'], 'iomin': 0.5, 'agnostic': False,
                      'thresholds': {'f1': 0.3}, 'reached': {}, **rows}}
    (out / 'summary.json').write_text(json.dumps(summary))
    (out / 'run_info.json').write_text(json.dumps({'deteval_version': '0.3.0'}))
    (out / 'metrics.csv').write_text('model,set,mAP50\n')
    print('evaluated')
'''


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def gpu(tmp_path, monkeypatch, make_worker):  # noqa: F811
    """A worker machine with a fake conda (environments in listings/) that runs the fake deteval, and a fake sudo
    (FW_RUN_AS=nobody) that runs the command as it is, with the empty environment the worker gives it; it writes
    its arguments to sudo.log. The fake deteval fails with listings/FAIL, misses an image with listings/SKIP."""
    listings = tmp_path / 'listings'
    listings.mkdir()
    (tmp_path / 'deteval.py').write_text(FAKE_DETEVAL)
    executable(tmp_path / 'conda', '#!/bin/sh\n'
               f'd="{listings}"\n'
               'if [ "$1" = run ] && [ "$4" = python ]; then echo "pip==24.0"; exit 0; fi\n'
               'if [ "$1" = env ] && [ "$2" = list ]; then\n'
               '  printf \'{"envs": [\'; sep=; for f in "$d"/*.txt; do\n'
               '    printf \'%s"%s/envs/%s"\' "$sep" "$d" "$(basename "$f" .txt)"; sep=,; done; echo \']}\'; exit 0\n'
               'fi\n'
               'if [ "$1" = run ] && [ "$5" = deteval ]; then\n'
               '  [ -f "$d/$3.txt" ] || exit 1\n'
               f'  export FAKE_DETEVAL_LOG="{tmp_path / "deteval.log"}"\n'
               '  [ -f "$d/FAIL" ] && export FAKE_DETEVAL_FAIL=1\n'
               '  [ -f "$d/SKIP" ] && export FAKE_DETEVAL_SKIP=1\n'
               f'  shift 5; exec "{sys.executable}" "{tmp_path / "deteval.py"}" "$@"\n'
               'fi\n'
               '[ "$1" = list ] && [ -f "$d/$3.txt" ] && cat "$d/$3.txt"\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    executable(bin_dir / 'sudo', '#!/bin/sh\n'
               f'echo "$*" >> "{tmp_path / "sudo.log"}"\n'
               'while [ "$1" != -- ]; do shift; done; shift\n'
               'case " $* " in *" kill -KILL -1 "*) exit 0;;\n'  # not every process of the test's account
               '  *fw-probe*) exit 0;;\n'  # the test's account owns everything here: nothing to probe
               '  *" crontab -l "*|*" atq "*) echo "You are not allowed to use this program" >&2; exit 1;;\n'
               'esac\n'               'exec "$@"\n')
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
    monkeypatch.setenv('FW_RUN_AS', 'nobody')
    monkeypatch.setattr(worker.RunAs, 'owner', lambda self: os.getuid())  # the fake sudo does not switch
    monkeypatch.setenv('FW_CONDA', str(tmp_path / 'conda'))
    monkeypatch.setenv('FW_CACHE', str(tmp_path / 'fw-cache'))
    w = make_worker()
    w.listings = listings
    w.calls = lambda: [json.loads(x) for x in (tmp_path / 'deteval.log').read_text().splitlines()]
    return w


def add_runner(w, framework='ultralytics', env=None):
    env = env or f'env-{uuid.uuid4().hex[:6]}'
    (w.listings / f'{env}.txt').write_text(f'@EXPLICIT\nhttps://x/{env}.tar.bz2\n')
    return worker.register_runner(w, env, env, '', framework)


def trained(api, model, snapshot, tmp_path, weights='best.pt', config=False):  # noqa: F811
    d = tmp_path / f'run-{uuid.uuid4().hex[:6]}'
    write(d, 'best.onnx', onnx_bytes(uuid.uuid4().hex))
    write(d, weights, b'weights ' + uuid.uuid4().hex.encode())
    files = []
    if config:
        write(d, 'training_config.json', b'{"resolution": 768}')
        files.append(d / 'training_config.json')
    return ops.register_model(api, model, d / 'best.onnx', snapshot['ref'], weights=d / weights, files=files,
                              echo=quiet)


def test_eval_on_folders_of_a_pool(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    mv = trained(api, model, snapshot, tmp_path, weights='best.pth', config=True)
    files = {**sample_files('A/x', 3), **sample_files('A/y/z', 2), **sample_files('B', 4)}
    files['A/x/neg.png'] = png(uuid.uuid4().hex)  # no label: a normal image
    data = pool(api, tmp_path, files)
    r = add_runner(gpu, 'rfdetr')
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{data}@1', 'dirs': ['A']})
    p = job['params']
    assert job['kind'] == 'eval' and job['runner'] == r['ref'] and p['data']['images'] == 6
    assert p['data']['dirs'] == ['A'] and p['warnings'] == []
    assert worker.run_worker(gpu, quiet, once=True) == 1
    done = api.job(job['id'])
    assert done['status'] == 'done', done['error']
    e = api.eval(done['result']['eval_id'])
    assert e['model_version']['ref'] == mv['ref'] and e['testset']['ref'] == f'{data}@1' and e['dirs'] == ['A']
    assert e['job_id'] == job['id'] and e['runner'] == r['ref'] and e['tool'] == 'aoi-deteval'
    assert e['tool_version'] == '0.3.0' and e['metrics']['images'] == 6 and e['metrics']['boxes'] == 5
    assert e['metrics']['R'] is None and e['metrics']['thresholds'] == {'f1': 0.3}  # NaN kept as null
    paths = {f['path'] for f in e['files']}
    assert {'metrics.json', 'files/summary.json', 'files/run_info.json', 'files/eval_sets.yaml',
            f'files/preds/{p["deteval"]["name"]}.json'} <= paths
    predict, evaluate = gpu.calls()
    assert predict[0] == 'predict' and predict[predict.index('--framework') + 1] == 'rfdetr'
    assert evaluate[0] == 'evaluate' and evaluate[evaluate.index('--conf-set') + 1] == 'eval'
    sets = json.loads(gpu.read_object(next(f['sha256'] for f in e['files'] if f['path'] == 'files/eval_sets.yaml')))
    assert sorted(sets['sets']) == ['d1', 'd2'] and sets['groups'] == {'eval': ['d1', 'd2']}
    assert sets['names'] == mv['classes']
    log = ''.join(x['text'] for x in api.job_log(job['id'])['pieces'])
    assert 'training_config beside weights: True' in log and 'predicted 6 images' in log
    assert not list((tmp_path / 'fw-cache' / 'jobs').iterdir())  # the job's folder is gone


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
    assert submit(model=onnx_only['ref']) == 400  # no weights to evaluate
    assert submit(dirs=['T']) == 400  # YOLO txt only: the pool's own class numbers
    assert submit(dirs=['S']) == 400  # a.png and a.jpg would share a.json
    assert submit(dirs=['nope']) == 400
    assert submit(dirs=['../A']) == 400
    assert submit(data=snapshot['ref'], dirs=[]) == 400  # a snapshot needs its split
    assert submit(data=f'{snapshot["ref"]}:test') == 400  # and is evaluated whole
    assert submit(data=f'{data}@1:test') == 400  # a pool has no split
    assert submit(imgsz=0) == 400 and submit(batch=True) == 400 and submit(conf=0.1) == 400
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
    assert j['status'] == 'failed' and 'deteval predict 没有成功' in j['error']
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
def test_model_code_runs_under_its_own_account_with_nothing_of_the_workers(  # noqa: F811
        api, gpu, model, snapshot, tmp_path, monkeypatch):  # noqa: F811
    """deteval loads the uploaded weights (a pickle: code): it runs through sudo as FW_RUN_AS, with an empty
    environment; a worker without FW_RUN_AS does not run it at all."""
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    monkeypatch.setenv('FW_SECRET_FOR_TEST', 'do-not-pass')
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'})
    worker.run_worker(gpu, quiet, once=True)
    assert api.job(job['id'])['status'] == 'done'
    log = ''.join(x['text'] for x in api.job_log(job['id'])['pieces'])
    shown = [line for line in log.splitlines() if line.startswith('environment:')]
    assert shown and all('FW_' not in line and 'FAKE_DETEVAL_LOG' in line for line in shown)
    sudo = (tmp_path / 'sudo.log').read_text().splitlines()
    runs = [line for line in sudo if ' deteval ' in line]
    assert len(runs) == 2 and all(line.startswith('-n -H -u nobody -- /usr/bin/env -i ') for line in runs)
    assert any('kill -KILL -1' in line for line in sudo)  # what it left running is killed
    monkeypatch.delenv('FW_RUN_AS')
    job = api.submit_job('eval', runner=r['runner'], params={'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'})
    worker.run_worker(gpu, quiet, once=True)
    j = api.job(job['id'])
    assert j['status'] == 'failed' and 'FW_RUN_AS' in j['error']
    assert len([line for line in (tmp_path / 'sudo.log').read_text().splitlines() if ' deteval ' in line]) == 2


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


def test_a_link_among_the_model_outputs_uploads_nothing(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    """evaluate puts a link to the worker's config among its outputs, or swaps the predictions it was given for
    one: the job fails, or the predictions read before evaluate ran are what is uploaded; the token never is."""
    secret = tmp_path / 'worker-config.json'
    secret.write_text('{"token": "WORKER-TOKEN-123"}')
    secret.chmod(0o600)
    with open(tmp_path / 'deteval.py', 'a') as f:
        f.write('\nif args[0] == "evaluate" and os.path.exists(%r):\n'
                '    os.symlink(%r, Path(opt("--out")) / "leak.json")\n'
                'if args[0] == "evaluate":\n'
                '    p = Path(opt("--preds")); p.unlink(); p.symlink_to(%r)\n'
                % (str(tmp_path / 'LINK'), str(secret), str(secret)))
    mv = trained(api, model, snapshot, tmp_path)
    r = add_runner(gpu)
    params = {'model': mv['ref'], 'data': f'{snapshot["ref"]}:test'}
    (tmp_path / 'LINK').write_text('')
    job = api.submit_job('eval', runner=r['runner'], params=params)
    worker.run_worker(gpu, quiet, once=True)
    j = api.job(job['id'])
    assert j['status'] == 'failed' and '链接' in j['error'] and api.evals(model) == []
    (tmp_path / 'LINK').unlink()
    job = api.submit_job('eval', runner=r['runner'], params=params)
    worker.run_worker(gpu, quiet, once=True)
    done = api.job(job['id'])
    assert done['status'] == 'done', done['error']
    for f in api.eval(done['result']['eval_id'])['files']:
        assert b'WORKER-TOKEN-123' not in api.read_object(f['sha256'])
