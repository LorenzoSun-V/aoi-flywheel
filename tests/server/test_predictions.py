"""Prediction sets and pre-labels (step 3, part 3): fw predict run / get, the worker's predict job, reuse.

The GPU machine is the fake one of test_evals (fake conda, onnx2engine, nvidia-smi, py_rfdetr and cv2): every
image gets a box of class 0 at (1, 2)-(30, 40) scoring 0.9 and one of a class beyond the model's (left out)."""
import json
import os

import pytest

from helpers import quiet
from fw import cli as fw_cli
from fw import ops, worker
from fw.cache import Cache

from test_evals import add_runner, gpu, trained  # noqa: F401  (fixtures)
from test_jobs import make_worker  # noqa: F401
from test_models import model, snapshot  # noqa: F401
from test_trainsets import CLASSES, H, W, png, pool, sample_files, status_of

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='the worker runs on the Linux GPU machine')


def unlabeled(prefix, n):
    return {f'{prefix}/new{i}.png': png(f'{prefix}-new-{i}-{os.urandom(4).hex()}') for i in range(n)}


@pytest.fixture
def setup(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    mv = trained(api, model, snapshot, tmp_path)
    files = {**sample_files('A', 2), **unlabeled('A/new', 2), **sample_files('B', 2)}
    data = pool(api, tmp_path, files)
    return mv, f'{data}@1', add_runner(gpu), files


def run_job(api, gpu, job):  # noqa: F811
    worker.run_worker(gpu, quiet, once=True)
    j = api.job(job['id'])
    assert j['status'] == 'done', j['error']
    return j


def get(api, set_id, dest, **kw):
    cache = Cache()
    try:
        return ops.get_predictions(api, cache, set_id, dest, echo=quiet, **kw)
    finally:
        cache.close()


def test_pre_labels_from_a_prediction_set(api, gpu, setup, tmp_path):  # noqa: F811
    mv, data, r, files = setup
    job = api.submit_job('predict', runner=r['runner'], params={'model': mv['ref'], 'data': data, 'dirs': ['A']})
    assert job['kind'] == 'predict' and not job['reused'] and job['params']['data']['images'] == 4
    assert job['params']['conf'] == 0.1 and [f[0] for f in job['params']['model']['files']] == ['model.onnx']
    j = run_job(api, gpu, job)
    s = api.prediction_set(j['result']['prediction_set'])
    e = api.events(action='prediction_set', limit=1)['events'][0]  # recorded, in the name of who asked for it
    assert e['object'] == f'预测集 {s["id"]}' and e['detail']['job_id'] == j['id'] and not e['by_server']
    assert s['images'] == 4 and s['boxes'] == 4 and s['job_id'] == job['id'] and s['dirs'] == ['A']
    assert s['data']['ref'] == data and s['model_version']['ref'] == mv['ref'] and s['runner'] == r['ref']
    assert {f['path'] for f in s['files']} == {'predictions.json', 'engine.json'}
    assert [x['id'] for x in api.prediction_sets(mv['model'])] == [s['id']]

    out = get(api, s['id'], tmp_path / 'out')
    assert out == {'path': str(tmp_path / 'out'), 'images': 4, 'prelabeled': 2, 'boxes': 2, 'kept': 2,
                   'replaced': 0}
    for i in range(2):  # labeled images keep their label as it is
        assert (tmp_path / 'out' / f'A/img{i:03}.json').read_bytes() == files[f'A/img{i:03}.json']
        doc = json.loads((tmp_path / 'out' / f'A/new/new{i}.json').read_text(encoding='utf-8'))
        assert doc['checked'] is False and doc['imagePath'] == f'new{i}.png' and doc['imageData'] is None
        assert (doc['imageWidth'], doc['imageHeight']) == (W, H)
        assert doc['flywheel'] == {'prediction_set': s['id'], 'model': mv['ref'], 'runner': r['ref'],
                                   'threshold': 0.3}
        [shape] = doc['shapes']
        assert shape['label'] == CLASSES[0] and shape['score'] == 0.9 and shape['shape_type'] == 'rectangle'
        assert shape['points'] == [[1.0, 2.0], [30.0, 2.0], [30.0, 40.0], [1.0, 40.0]]
    assert not (tmp_path / 'out' / 'B').exists()
    assert (tmp_path / 'out' / 'classes.txt').read_text(encoding='utf-8') == ''.join(c + '\n' for c in CLASSES)

    out = get(api, s['id'], tmp_path / 'all', replace=True)
    assert out['prelabeled'] == 4 and out['kept'] == 0
    assert json.loads((tmp_path / 'all' / 'A/img000.json').read_text())['shapes'][0]['score'] == 0.9
    assert get(api, s['id'], tmp_path / 'high', threshold=0.95)['prelabeled'] == 0
    with pytest.raises(ops.FwError, match='低于预测集'):
        get(api, s['id'], tmp_path / 'low', threshold=0.05)


def test_the_same_prediction_is_not_run_twice(api, gpu, setup):  # noqa: F811
    mv, data, r, _ = setup
    params = {'model': mv['ref'], 'data': data, 'dirs': ['A/new']}
    first = api.submit_job('predict', runner=r['runner'], params=params)
    again = api.submit_job('predict', runner=r['runner'], params=params)
    assert again['reused'] and again['id'] == first['id']  # still queued: the same job
    run_job(api, gpu, first)
    done = api.submit_job('predict', runner=r['runner'], params=params)
    assert done['reused'] and done['id'] == first['id'] and done['status'] == 'done'
    assert worker.run_worker(gpu, quiet, once=True) == 0 and len(gpu.builds()) == 1
    other = api.submit_job('predict', runner=r['runner'], params={**params, 'conf': 0.2})
    assert not other['reused']
    j = run_job(api, gpu, other)
    assert j['result']['prediction_set'] != api.job(first['id'])['result']['prediction_set']
    assert len(gpu.builds()) == 1  # the engine is the same


def test_what_a_prediction_needs(api, gpu, setup, snapshot, tmp_path):  # noqa: F811
    mv, data, r, _ = setup
    no_framework = add_runner(gpu, framework='')
    same_stem = pool(api, tmp_path, {'S/a.png': png('sa'), 'S/a.jpg': png('sb')})

    def submit(**params):
        params.setdefault('model', mv['ref'])
        params.setdefault('data', data)
        return status_of(lambda: api.submit_job('predict', runner=params.pop('runner', r['runner']),
                                                params=params))

    assert submit() == 200
    assert submit(runner=no_framework['runner']) == 400
    assert submit(data=f'{snapshot["ref"]}') == 400  # a snapshot: pre-labels are for pool folders
    assert submit(dirs=['nope']) == 400
    assert submit(data=f'{same_stem}@1') == 400  # a.png and a.jpg would share a.json
    assert submit(conf=0) == 400 and submit(conf=1) == 400 and submit(conf='0.5') == 400
    assert submit(threshold=0.5) == 400


def test_predictions_that_do_not_match_the_input_list_register_nothing(api, gpu, setup):  # noqa: F811
    mv, data, r, _ = setup
    job = api.submit_job('predict', runner=r['runner'], params={'model': mv['ref'], 'data': data, 'dirs': ['B']})
    claimed = gpu.claim()
    assert claimed['id'] == job['id']
    images = sorted(f'B/img{i:03}.png' for i in range(2))

    def finish_with(doc):
        data = json.dumps(doc).encode()
        files = [['predictions.json', gpu.put_bytes(data), len(data)]]
        return status_of(lambda: gpu.finish(job['id'], claimed['token'], 'done', {'files': files}))

    good = {'size': [W, H], 'boxes': [[1, 2, 30, 40, 0.9, 0]]}
    assert finish_with({'schema_version': 1, 'images': {images[0]: good}}) == 400  # one image missing
    assert finish_with({'schema_version': 1, 'images': {p: good for p in images + ['B/x.png']}}) == 400
    bad_boxes = ([1, 2, 30, 40, 0.05, 0], [1, 2, 30, 40, 0.9, 2], [30, 2, 1, 40, 0.9, 0], [1, 2, 30, 40, 0.9])
    for box in bad_boxes:
        assert finish_with({'schema_version': 1,
                            'images': {p: {'size': [W, H], 'boxes': [box]} for p in images}}) == 400
    assert api.job(job['id'])['status'] == 'running' and api.prediction_sets(mv['model']) == []
    assert finish_with({'schema_version': 1, 'images': {p: good for p in images}}) == 200
    assert api.prediction_sets(mv['model'])[0]['boxes'] == 2


def test_fw_predict_commands(service, tokens, api, gpu, setup, tmp_path, monkeypatch, capsys):  # noqa: F811
    mv, data, r, _ = setup
    monkeypatch.setenv('FW_CONFIG', str(tmp_path / 'config.json'))
    assert fw_cli.main(['login', '--server', service.url, '--token', tokens['algo']]) == 0
    run = ['predict', 'run', mv['ref'], '--data', data, '--dirs', 'A/new', '--runner', r['runner']]
    assert fw_cli.main(run) == 0
    assert '已提交推理任务' in capsys.readouterr().out
    worker.run_worker(gpu, quiet, once=True)
    assert fw_cli.main(['predict', 'list']) == 0
    assert mv['ref'] in capsys.readouterr().out
    set_id = api.prediction_sets(mv['model'])[-1]['id']
    assert fw_cli.main(['predict', 'show', str(set_id)]) == 0
    assert 'predictions.json' in capsys.readouterr().out
    assert fw_cli.main(['predict', 'get', str(set_id), str(tmp_path / 'got')]) == 0
    assert '2 张写了预标注' in capsys.readouterr().out
    assert fw_cli.main(run) == 0
    out = capsys.readouterr().out
    assert '已经有了' in out and f'fw predict get {set_id}' in out
    assert fw_cli.main(['predict', 'get', str(set_id), str(tmp_path / 'low'), '--threshold', '0.01']) == 1


def test_the_database_keeps_prediction_sets(sql, api, gpu, setup):  # noqa: F811
    """Written once by a running prediction job, never changed; one file set per record."""
    import psycopg
    mv, data, r, _ = setup
    job = api.submit_job('predict', runner=r['runner'], params={'model': mv['ref'], 'data': data, 'dirs': ['B']})
    s = api.prediction_set(run_job(api, gpu, job)['result']['prediction_set'])
    row = sql.execute('SELECT * FROM prediction_sets WHERE id = %s', (s['id'],)).fetchone()
    with pytest.raises(psycopg.errors.RaiseException, match='cannot be changed'):
        with sql.transaction():
            sql.execute('UPDATE prediction_sets SET boxes = 0 WHERE id = %s', (s['id'],))
    with pytest.raises(psycopg.errors.RaiseException, match='cannot be changed'):
        with sql.transaction():
            sql.execute('DELETE FROM prediction_sets WHERE id = %s', (s['id'],))
    with pytest.raises(psycopg.errors.RaiseException, match='not a running prediction'):
        with sql.transaction():
            sql.execute('INSERT INTO prediction_sets (model_version_id, runner_version_id, dataset_version_id, '
                        'inputs_sha256, conf, images, boxes, fileset_id, job_id, created_by) '
                        'VALUES (%s, %s, %s, %s, 0.5, 1, 0, %s, %s, %s)',
                        (row['model_version_id'], row['runner_version_id'], row['dataset_version_id'],
                         row['inputs_sha256'], row['fileset_id'], row['job_id'], row['created_by']))


# --- review of PR #14, round 1 ---------------------------------------------------------------------------
def test_boxes_below_conf_from_the_deploy_code_are_left_out(api, gpu, setup, tmp_path):  # noqa: F811
    """The deploy code applies conf its own way (> instead of >=, float16 scores): trt_predict filters again,
    so one box just under conf does not throw the whole GPU run away."""
    mv, data, r, _ = setup
    m = tmp_path / 'modules' / 'py_rfdetr.py'
    m.write_text(m.read_text().replace("return [[1.0, 2.0, 29.0, 38.0, 0.9, 0], [5.0, 5.0, 10.0, 10.0, 0.8, 99]]",
                                       "return [[1.0, 2.0, 29.0, 38.0, 0.9, 0], [3.0, 3.0, 9.0, 9.0, 0.0999999, 0]]"))
    job = api.submit_job('predict', runner=r['runner'], params={'model': mv['ref'], 'data': data, 'dirs': ['A']})
    s = api.prediction_set(run_job(api, gpu, job)['result']['prediction_set'])
    assert s['images'] == 4 and s['boxes'] == 4


def test_replace_keeps_a_label_where_the_model_finds_nothing(api, gpu, setup, tmp_path):  # noqa: F811
    mv, data, r, files = setup
    job = api.submit_job('predict', runner=r['runner'], params={'model': mv['ref'], 'data': data, 'dirs': ['A']})
    s = run_job(api, gpu, job)['result']['prediction_set']
    out = get(api, s, tmp_path / 'out', threshold=0.95, replace=True)
    assert out['prelabeled'] == 0 and out['kept'] == 2 and out['replaced'] == 0
    for i in range(2):
        assert (tmp_path / 'out' / f'A/img{i:03}.json').read_bytes() == files[f'A/img{i:03}.json']
    out = get(api, s, tmp_path / 'all', replace=True)
    assert out['prelabeled'] == 4 and out['replaced'] == 2 and out['kept'] == 0


def test_replace_does_not_write_into_the_cache_or_leave_two_labels(api, gpu, model, snapshot, tmp_path):  # noqa: F811
    mv = trained(api, model, snapshot, tmp_path)
    old = json.dumps({'version': '3.1', 'flags': {}, 'shapes': [], 'imagePath': 'u.png', 'imageData': None}).encode()
    data = pool(api, tmp_path, {'C/u.png': png('u-' + os.urandom(4).hex()), 'C/u.JSON': old})
    r = add_runner(gpu)
    job = api.submit_job('predict', runner=r['runner'], params={'model': mv['ref'], 'data': f'{data}@1'})
    s = run_job(api, gpu, job)['result']['prediction_set']
    out = get(api, s, tmp_path / 'linked', replace=True, link='hardlink')
    assert out['replaced'] == 1
    assert sorted(p.name for p in (tmp_path / 'linked' / 'C').iterdir()) == ['u.json', 'u.png']
    assert json.loads((tmp_path / 'linked' / 'C/u.json').read_text())['shapes'][0]['score'] == 0.9
    get(api, s, tmp_path / 'again')  # the cache still has the old label as it was
    assert (tmp_path / 'again' / 'C/u.JSON').read_bytes() == old


def test_a_job_being_cancelled_is_not_reused(api, gpu, setup):  # noqa: F811
    mv, data, r, _ = setup
    params = {'model': mv['ref'], 'data': data, 'dirs': ['B']}
    first = api.submit_job('predict', runner=r['runner'], params=params)
    assert gpu.claim()['id'] == first['id']
    api.cancel_job(first['id'])
    again = api.submit_job('predict', runner=r['runner'], params=params)
    assert not again['reused'] and again['id'] != first['id']


def test_verify_names_prediction_sets(sql, api, gpu, setup):  # noqa: F811
    from fwserver import models
    mv, data, r, _ = setup
    job = api.submit_job('predict', runner=r['runner'], params={'model': mv['ref'], 'data': data, 'dirs': ['B']})
    s = api.prediction_set(run_job(api, gpu, job)['result']['prediction_set'])
    _, wanted, _ = models.verify_inputs(sql)
    assert wanted[s['inputs_sha256']] == (None, f'预测集 #{s["id"]} 的图片清单')
    assert all(wanted[f['sha256']][1] == f'预测集 #{s["id"]}' for f in s['files'])


# --- pre-labelling a labelling batch ---------------------------------------------------------------------
def raw_folder(tmp_path, name='20261105'):
    from helpers import write
    folder = tmp_path / name
    for i in range(3):
        write(folder, f'raw{i}.png', png(f'batch-raw-{i}-{os.urandom(4).hex()}'))
    return folder


def test_a_batch_is_pre_labelled_by_its_owner(api, make_api, gpu, setup, tmp_path, tokens, monkeypatch, capsys):  # noqa: F811
    mv, data, r, _ = setup
    field, other = make_api('field'), make_api('deploy')
    folder = raw_folder(tmp_path)
    name = f'B-{os.urandom(4).hex()}'
    b = ops.upload_batch(field, folder, data.split('@')[0], '张三', name=name, echo=quiet)
    assert 'prelabel' in b['actions']
    # a field account cannot submit jobs in general, only the pre-label of its own batch
    assert status_of(lambda: field.submit_job('predict', runner=r['runner'], params={'model': mv['ref'],
                                                                                    'data': f'{name}@1'})) == 403
    assert status_of(lambda: other.prelabel_batch(name, {'model': mv['ref'], 'runner': r['runner']})) == 403
    assert status_of(lambda: field.prelabel_batch(name, {'model': mv['ref'], 'runner': r['runner'],
                                                        'revision': 2})) == 409
    assert status_of(lambda: field.prelabel_batch(name, {'model': 'no-such@1', 'runner': r['runner']})) == 404
    assert field.batch_prelabels(name)['prelabels'] == []
    out = []
    row = ops.prelabel_batch(field, name, model=mv['ref'], runner=r['runner'], echo=out.append)
    assert row['status'] == 'queued' and row['revision'] == 1 and row['model'] == mv['ref'] and row['images'] == 3
    assert any('已提交预标注' in x for x in out)
    again = field.prelabel_batch(name, {'model': mv['ref'], 'runner': r['runner']})
    assert again['reused'] and again['id'] == row['job_id']
    b = field.batch(name)
    steps = [s for s in b['steps'] if s['step'] == 'prelabel']
    assert len(steps) == 1 and steps[0]['job_id'] == row['job_id'] and mv['ref'] in steps[0]['comment']
    assert steps[0]['actor'] == tokens['field_name'] and steps[0]['revision'] == 1
    e = api.events(action='batch.prelabel', q=name)['events']
    assert len(e) == 1 and e[0]['detail']['job_id'] == row['job_id'] and e[0]['actor'] == tokens['field_name']
    cache = Cache(tmp_path / 'cache')
    try:
        with pytest.raises(ops.FwError, match='还没跑完'):
            ops.get_batch_prelabels(field, cache, name, tmp_path / 'early', echo=quiet)
        run_job(api, gpu, {'id': row['job_id']})
        [done] = field.batch_prelabels(name)['prelabels']
        assert done['status'] == 'done' and done['boxes'] == 3 and done['prediction_set']
        got = ops.get_batch_prelabels(field, cache, name, tmp_path / 'work', echo=quiet)
        assert got['prelabeled'] == 3 and got['kept'] == 0
        doc = json.loads((tmp_path / 'work' / 'raw0.json').read_text(encoding='utf-8'))
        assert doc['checked'] is False and doc['flywheel']['model'] == mv['ref']
        # the files are plain copies: correcting them and uploading the folder is the next revision
        doc['checked'] = True
        (tmp_path / 'work' / 'raw0.json').write_text(json.dumps(doc), encoding='utf-8')
        b = ops.update_batch(field, name, tmp_path / 'work', '张三', reviewer=tokens['algo_name'], echo=quiet)
        assert b['revision'] == 2 and b['status'] == 'review' and b['file_count'] == 7
        # in review it is no longer the owner's to pre-label, and revision 2 has no pre-labels of its own
        assert 'prelabel' not in b['actions']
        assert status_of(lambda: field.prelabel_batch(name, {'model': mv['ref'], 'runner': r['runner']})) == 409
        with pytest.raises(ops.FwError, match='还没有预标注'):
            ops.get_batch_prelabels(field, cache, name, tmp_path / 'none', echo=quiet)
        # rejected: revision 2 has the same images as revision 1, so the same job answers, noted for revision 2
        api.batch_step(name, 'reject', {'comment': '再看看'})
        second = field.prelabel_batch(name, {'model': mv['ref'], 'runner': r['runner']})
        assert second['reused'] and second['id'] == row['job_id']
        assert field.prelabel_batch(name, {'model': mv['ref'], 'runner': r['runner']})['id'] == row['job_id']
        assert [(x['revision'], x['status'], x['job_id']) for x in field.batch_prelabels(name)['prelabels']] \
            == [(2, 'done', row['job_id']), (1, 'done', row['job_id'])]
        got = ops.get_batch_prelabels(field, cache, name, tmp_path / 'two', echo=quiet)
        assert got['images'] == 3 and got['kept'] == 3  # laid out from revision 2, whose images all have labels
        assert json.loads((tmp_path / 'two' / 'raw0.json').read_text(encoding='utf-8'))['checked'] is True
        # one unfinished pre-label per batch
        more = field.prelabel_batch(name, {'model': mv['ref'], 'runner': r['runner'], 'conf': 0.2})
        assert not more['reused']
        assert status_of(lambda: field.prelabel_batch(name, {'model': mv['ref'], 'runner': r['runner'],
                                                            'conf': 0.3})) == 409
        assert field.prelabel_batch(name, {'model': mv['ref'], 'runner': r['runner'], 'conf': 0.2})['id'] == more['id']
        assert not [j for j in api.jobs(status='queued') if j['params'].get('conf') == 0.3
                    and j['params'].get('data', {}).get('ref') == f'{name}@2']
        api.cancel_job(more['id'])
    finally:
        cache.close()
    # the command line
    monkeypatch.setattr(ops, 'connect', lambda: field)
    monkeypatch.setattr(field, 'close', lambda: None)
    assert fw_cli.main(['batch', 'show', name]) == 0
    assert f'预标注第 1 版（任务 {row["job_id"]}，{done["status_text"]}）' in capsys.readouterr().out


def test_pre_label_defaults(api, make_api, gpu, setup, tmp_path):  # noqa: F811
    mv, data, r, _ = setup
    field = make_api('field')
    # a line without a release: the model must be named
    lonely = pool(api, tmp_path, sample_files('A', 1), line=f'L{os.urandom(3).hex()}')
    b = ops.upload_batch(field, raw_folder(tmp_path, 'x1'), lonely, '张三', name=f'B-{os.urandom(4).hex()}', echo=quiet)
    d = field.batch_prelabels(b['name'])['defaults']
    assert d['model'] is None and r['runner'] in d['runners']
    with pytest.raises(Exception, match='还没有登记过发布'):
        field.prelabel_batch(b['name'], {'runner': r['runner']})
    # with a release on the pool's line, that model version is the default
    from test_models import release
    import datetime
    release(make_api('deploy'), mv['ref'], datetime.datetime.now().astimezone().isoformat())
    b = ops.upload_batch(field, raw_folder(tmp_path, 'x2'), data.split('@')[0], '张三',
                         name=f'B-{os.urandom(4).hex()}', echo=quiet)
    d = field.batch_prelabels(b['name'])['defaults']
    assert d['model']['ref'] and d['site'] == 'SES' and d['line'] == 'TX'
    job = field.prelabel_batch(b['name'], {'runner': r['runner']})
    assert job['params']['model']['ref'] == d['model']['ref'] and job['params']['data']['ref'] == f'{b["name"]}@1'
    api.cancel_job(job['id'])
