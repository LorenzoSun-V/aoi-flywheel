"""Models, model versions, evaluation reports, releases, tags and the trace chain: what is checked when they
are registered, who may do what, and what the database keeps frozen."""
import datetime
import json
import uuid

import psycopg
import pytest

from helpers import quiet, read_tree, write
from fw import cli as fw_cli
from fw import ops
from fw.api import ApiError
from fw.contract import sha256_file
from fwserver import datasets as ds
from fwserver import db, models

from test_trainsets import CLASSES, make, pool, sample_files, status_of

COMMIT = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'


def onnx_bytes(tag: str) -> bytes:
    """Enough of an ONNX file for the check: it starts with ir_version (field 1, varint)."""
    return b'\x08\x07\x12\x07pytorch' + tag.encode()


@pytest.fixture
def snapshot(api, tmp_path):
    src = pool(api, tmp_path, {**sample_files('2026-09', 8), **sample_files('2026-10', 3)})
    return make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'], test_dirs=['2026-10'])


@pytest.fixture
def model(api):
    name = f'M-{uuid.uuid4().hex[:8]}'
    api.create_model(name, 'SES', 'TX', 'det', '')
    return name


def files_in(tmp_path, tag='a', metrics=None, extra=None):
    """A trained model on disk: best.onnx, val.json and a weights file."""
    d = tmp_path / f'run-{uuid.uuid4().hex[:6]}'
    write(d, 'best.onnx', onnx_bytes(tag))
    write(d, 'val.json', json.dumps(metrics if metrics is not None else {'mAP50': 0.81, 'box': {'p': 0.9}}).encode())
    write(d, 'best.pt', b'PK weights ' + tag.encode())
    if extra:
        for rel, data in extra.items():
            write(d, rel, data)
    return d


def register(api, model, snapshot, run, **kw):
    kw.setdefault('code_commit', COMMIT)
    return ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], metrics=run / 'val.json',
                              files=[run / 'best.pt'], echo=quiet, **kw)


# --- model versions ------------------------------------------------------------------------------------------
def test_register_and_get_a_model_version(api, model, snapshot, tmp_path, cache, make_api):
    run = files_in(tmp_path)
    mv = register(api, model, snapshot, run, code_repo='https://user:secret@git.example.com/algo/train.git')
    assert mv['ref'] == f'{model}@1' and not mv['existing']
    assert mv['trainset']['ref'] == snapshot['ref'] and mv['classes'] == CLASSES
    assert mv['code_commit'] == COMMIT and mv['code_repo'] == 'https://git.example.com/algo/train.git'
    assert mv['metrics'] == {'mAP50': 0.81, 'box': {'p': 0.9}}
    assert mv['onnx_sha256'] == sha256_file(run / 'best.onnx')[0]
    detail = api.model_version(model, 1)
    assert [f['path'] for f in detail['files']] == ['classes.txt', 'files/best.pt', 'metrics.json', 'model.onnx']

    # the deploy role fetches it like the training machine would
    deploy = make_api('deploy')
    got = ops.get_model(deploy, cache, f'{model}@1', tmp_path / 'deploy', echo=quiet)
    tree = read_tree(tmp_path / 'deploy')
    assert tree['model.onnx'] == (run / 'best.onnx').read_bytes()
    assert tree['classes.txt'] == b'scratch\ndent\n'
    assert tree['files/best.pt'] == (run / 'best.pt').read_bytes()
    info = json.loads(tree[ops.MODEL_INFO])
    assert info['ref'] == f'{model}@1' and info['trainset']['ref'] == snapshot['ref'] and info['classes'] == CLASSES
    assert got['path'] == str(tmp_path / 'deploy')

    # the same files again (a script run twice, a retry): the version that exists
    again = register(api, model, snapshot, run, code_repo='https://git.example.com/algo/train.git')
    assert again['existing'] and again['id'] == mv['id']
    # a new ONNX is the next version; the same ONNX with other files is refused
    mv2 = register(api, model, snapshot, files_in(tmp_path, 'b'))
    assert mv2['ref'] == f'{model}@2'
    with pytest.raises(ApiError) as e:
        register(api, model, snapshot, run, code_commit='0' * 40)
    assert e.value.status == 409 and f'{model}@1' in e.value.message
    assert [v['ref'] for v in api.model_versions(model)] == [f'{model}@1', f'{model}@2']
    assert api.model(model)['latest'] == f'{model}@2'


def test_what_a_model_version_must_be(api, model, snapshot, tmp_path):
    run = files_in(tmp_path)
    write(run, 'weights.pt', b'PK not an onnx')
    with pytest.raises(ops.FwError, match='不像 ONNX'):
        ops.register_model(api, model, run / 'weights.pt', snapshot['ref'], code_commit=COMMIT, echo=quiet)
    src = pool(api, tmp_path, {'a.png': b'1'})
    with pytest.raises(ops.FwError, match='不是训练集快照'):
        ops.register_model(api, model, run / 'best.onnx', f'{src}@1', code_commit=COMMIT, echo=quiet)
    write(run, 'list.json', b'[1, 2]')
    with pytest.raises(ops.FwError, match='JSON 对象'):
        ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], metrics=run / 'list.json',
                           code_commit=COMMIT, echo=quiet)
    with pytest.raises(ApiError, match='提交号'):
        ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], code_commit='main', echo=quiet)
    with pytest.raises(ApiError) as e:
        ops.register_model(api, 'no-such-model', run / 'best.onnx', snapshot['ref'], code_commit=COMMIT, echo=quiet)
    assert e.value.status == 404 and 'fw model create' in e.value.message

    # straight to the API: files that are not on the server, paths that are not allowed, a stored file that is not
    # what the request says
    digest, size = sha256_file(run / 'best.onnx')
    body = {'trainset_id': snapshot['id'], 'code_commit': COMMIT}
    with pytest.raises(ApiError, match='还没有'):
        api.register_model_version(model, dict(body, files=[['model.onnx', 'f' * 64, 10]]))
    api.put_file(digest, run / 'best.onnx')
    for files, message in (([['weights/x.pt', digest, size], ['model.onnx', digest, size]], 'files/'),
                           ([['classes.txt', digest, size], ['model.onnx', digest, size]], 'files/'),
                           ([['files/a', digest, size]], '缺 model.onnx'),
                           ([['model.onnx', digest, size], ['model.onnx', digest, size]], '两次'),
                           ([['model.onnx', digest, size], ['files/A', digest, size], ['files/a', digest, size]],
                            '大小写')):
        with pytest.raises(ApiError, match=message):
            api.register_model_version(model, dict(body, files=files))
    bad = json.dumps({'x': float('nan')}, allow_nan=True).encode()
    bad_digest = api.put_bytes(bad)
    with pytest.raises(ApiError, match='JSON'):
        api.register_model_version(model, dict(body, files=[['model.onnx', digest, size],
                                                            ['metrics.json', bad_digest, len(bad)]]))
    with pytest.raises(ApiError, match='令牌'):
        api.register_model_version(model, dict(body, files=[['model.onnx', digest, size]],
                                               code_repo='https://ghp_x@github.example/a.git'))
    assert api.model_versions(model) == []


def test_models_are_created_once(api, model):
    with pytest.raises(ApiError) as e:
        api.create_model(model.lower(), 'SES', 'TX', 'det', '')
    assert e.value.status == 409
    for args in ((f'{model}x', '', 'TX', 'det'), (f'{model}x', 'SES', 'TX', 'seg'), ('bad name', 'SES', 'TX', 'det')):
        assert status_of(lambda: api.create_model(*args, '')) == 400
    assert model in [m['name'] for m in api.models()]


def test_code_commit_from_git(api, model, snapshot, tmp_path, monkeypatch):
    import subprocess
    repo = tmp_path / 'train'
    repo.mkdir()
    env = dict(GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@x')
    run = lambda *a: subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True,  # noqa: E731
                                    env={**__import__('os').environ, **env})
    run('init', '-q')
    write(repo, 'train.py', b'print(1)\n')
    run('add', 'train.py')
    run('commit', '-q', '-m', 'x')
    run('remote', 'add', 'origin', 'https://bot:token123@git.example.com/algo/train.git')
    head = run('rev-parse', 'HEAD').stdout.decode().strip()
    write(repo, 'train.py', b'print(2)\n')  # a change not committed
    monkeypatch.chdir(repo)
    out = []
    mv = ops.register_model(api, model, files_in(tmp_path) / 'best.onnx', snapshot['ref'], echo=out.append)
    assert mv['code_commit'] == head and mv['code_dirty']
    assert mv['code_repo'] == 'https://git.example.com/algo/train.git'
    assert '没提交的改动' in out[0] and 'token123' not in ''.join(out)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ops.FwError, match='--code-commit'):
        ops.register_model(api, model, files_in(tmp_path, 'c') / 'best.onnx', snapshot['ref'], echo=quiet)


def test_tags(api, model, snapshot, tmp_path, sql):
    mv = register(api, model, snapshot, files_in(tmp_path))
    assert mv['tags'] == []
    assert api.set_tag(model, 1, 'candidate', True)['tags'] == ['candidate']
    assert not api.set_tag(model, 1, 'candidate', True)['changed']
    assert api.set_tag(model, 1, 'deprecated', True)['tags'] == ['candidate', 'deprecated']
    assert api.set_tag(model, 1, 'candidate', False, 'replaced')['tags'] == ['deprecated']
    assert status_of(lambda: api.set_tag(model, 1, 'production', True)) == 400
    rows = sql.execute('SELECT tag, added FROM model_tag_events WHERE model_version_id = %s ORDER BY id',
                       (mv['id'],)).fetchall()
    assert [(r['tag'], r['added']) for r in rows] == [('candidate', True), ('deprecated', True), ('candidate', False)]


# --- evaluation reports ---------------------------------------------------------------------------------------
def deteval_out(tmp_path, models=('model_s0',)):
    d = tmp_path / f'deteval-{uuid.uuid4().hex[:6]}'
    write(d, 'summary.json', json.dumps({m: {'f1': {'conf': 0.31}, 'test': {'mAP50': [0.7, 0.8][i], 'lf1': 0.8}}
                                         for i, m in enumerate(models)}).encode())
    write(d, 'run_info.json', json.dumps({'deteval_version': '0.2.0', 'metric_version': 3}).encode())
    write(d, 'metrics.csv', b'model,set,mAP50\n')
    write(d, 'report.txt', '对比表\n'.encode())
    return d


def test_eval_reports(api, model, snapshot, tmp_path):
    register(api, model, snapshot, files_in(tmp_path))
    out = deteval_out(tmp_path)
    e = ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=out, echo=quiet)
    assert e['testset']['ref'] == f'{snapshot["ref"]}:test' and e['split'] == 'test'
    assert e['tool'] == 'aoi-deteval' and e['tool_version'] == '0.2.0'
    assert e['metrics'] == {'f1': {'conf': 0.31}, 'test': {'mAP50': 0.7, 'lf1': 0.8}}
    assert 'model_s0' in e['note']
    shown = api.eval(e['id'])
    assert [f['path'] for f in shown['files']] == ['files/metrics.csv', 'files/report.txt', 'files/run_info.json',
                                                   'files/summary.json', 'metrics.json']
    assert ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=out, echo=quiet)['existing']

    two = deteval_out(tmp_path, ('model_s0', 'model_s1'))
    with pytest.raises(ops.FwError, match='--deteval-model'):
        ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=two, echo=quiet)
    e2 = ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=two, deteval_model='model_s1',
                           echo=quiet)
    assert e2['metrics']['test']['mAP50'] == 0.8

    with pytest.raises(ops.FwError, match=':test'):
        ops.register_eval(api, f'{model}@1', snapshot['ref'], deteval=out, echo=quiet)
    with pytest.raises(ApiError, match='没有 val'):
        empty = make(api, f'T-{uuid.uuid4().hex[:8]}',
                     [f'{pool(api, tmp_path, sample_files("x", 1))}@1'], val=0.0)
        ops.register_eval(api, f'{model}@1', f'{empty["ref"]}:val', deteval=out, echo=quiet)
    test_pool = pool(api, tmp_path, sample_files('t', 2))
    metrics = write(tmp_path, 'm.json', b'{"recall": 0.9}')
    e3 = ops.register_eval(api, f'{model}@1', f'{test_pool}@1', metrics=metrics, tool='other', echo=quiet)
    assert e3['testset']['ref'] == f'{test_pool}@1' and e3['metrics'] == {'recall': 0.9}
    with pytest.raises(ApiError, match='不用写划分'):
        ops.register_eval(api, f'{model}@1', f'{test_pool}@1:test', metrics=metrics, echo=quiet)
    with pytest.raises(ops.FwError, match='只写一个'):
        ops.register_eval(api, f'{model}@1', f'{test_pool}@1', echo=quiet)
    assert [x['id'] for x in api.evals(model)] == [e['id'], e2['id'], e3['id']]


def test_compare(api, model, snapshot, tmp_path, monkeypatch, service, tokens, capsys):
    register(api, model, snapshot, files_in(tmp_path, 'a', {'mAP50': 0.8, 'recall': 0.7}))
    register(api, model, snapshot, files_in(tmp_path, 'b', {'mAP50': 0.85, 'precision': 0.9}))
    a, b = api.model_version(model, 1), api.model_version(model, 2)
    assert ops.compare_metrics(a['metrics'], b['metrics']) == [('mAP50', 0.8, 0.85), ('recall', 0.7, None),
                                                               ('precision', None, 0.9)]
    login(monkeypatch, tmp_path, service, tokens['algo'])
    capsys.readouterr()
    assert fw_cli.main(['model', 'compare', f'{model}@1', f'{model}@2']) == 0
    out = capsys.readouterr().out
    assert '+0.05' in out and 'precision' in out
    assert fw_cli.main(['model', 'compare', f'{model}@1', f'{model}@2', '--testset', f'{snapshot["ref"]}:test']) == 1
    assert '没有在' in capsys.readouterr().err


# --- releases ---------------------------------------------------------------------------------------------
def release(api, model_ref, when, commit='abcdef1', **kw):
    return ops.register_release(api, model_ref, 'https://git.example.com/deploy/trt.git', commit, when, echo=quiet,
                                **kw)


def test_releases_and_the_current_model(api, make_api, model, snapshot, tmp_path):
    register(api, model, snapshot, files_in(tmp_path, 'a'))
    register(api, model, snapshot, files_in(tmp_path, 'b'))
    deploy = make_api('deploy')
    site, line = f'S{uuid.uuid4().hex[:6]}', 'TX'
    engine = write(tmp_path, 'model_fp16.engine', b'engine bytes')
    r1 = release(deploy, f'{model}@2', '2026-10-20T10:30+08:00', site=site, line=line, gpu='RTX3070', trt_version='10.3',
                 precision='fp16', engine_file=engine)
    assert r1['ref'] == f'{site}-{line}-r1' and r1['current']['ref'] == r1['ref']
    assert r1['engine_sha256'] == sha256_file(engine)[0] and r1['precision'] == 'fp16'
    # registered later, deployed earlier: it does not become the current one
    r2 = release(deploy, f'{model}@1', '2026-10-19T09:00:00+08:00', site=site, line=line)
    assert r2['ref'] == f'{site}-{line}-r2' and r2['current']['ref'] == r1['ref']
    assert deploy.current_release(site, line)['model_version']['ref'] == f'{model}@2'
    # a retry of the same registration is the same release; the same deployment with other details is refused
    assert release(deploy, f'{model}@2', '2026-10-20T02:30Z', site=site, line=line, gpu='RTX3070', trt_version='10.3',
                   precision='fp16', engine_file=engine)['existing']
    with pytest.raises(ApiError) as e:
        release(deploy, f'{model}@2', '2026-10-20T02:30Z', site=site, line=line, gpu='RTX3080')
    assert e.value.status == 409
    assert [r['ref'] for r in deploy.releases(site, line)] == [r2['ref'], r1['ref']]  # by deployment time
    assert deploy.release(f'{site.lower()}-tx-r1')['id'] == r1['id']
    # the model's own site and line when none is given
    r3 = release(make_api('admin'), f'{model}@1', None)
    assert (r3['site'], r3['line']) == ('SES', 'TX')

    future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()
    for kw, message in ((dict(when=future), '还晚'), (dict(commit='v1.2'), '提交号'),
                        (dict(when='2026-10-20T10:30', precision='int8'), '精度')):
        when = kw.pop('when', '2026-10-20T10:30+08:00')
        with pytest.raises(ApiError, match=message):
            release(deploy, f'{model}@1', when, site=site, line=line, **kw)
    with pytest.raises(ApiError, match='令牌'):
        deploy.register_release({'model': model, 'number': '1', 'deploy_repo': 'https://x:y@git.example/d.git',
                                 'deploy_commit': 'abcdef1', 'deployed_at': '2026-10-20T10:30+08:00'})
    with pytest.raises(ApiError, match='时区'):
        deploy.register_release({'model': model, 'number': '1', 'deploy_repo': 'd', 'deploy_commit': 'abcdef1',
                                 'deployed_at': '2026-10-20T10:30'})
    assert status_of(lambda: deploy.current_release(site, 'other')) == 404


def test_roles(api, make_api, model, snapshot, tmp_path, cache):
    mv = register(api, model, snapshot, files_in(tmp_path))
    deploy, field, algo = make_api('deploy'), make_api('field'), make_api('algo')
    # the deploy role sees models and releases, downloads model files and nothing else, and registers releases
    assert deploy.model_version(model, 1)['id'] == mv['id']
    assert deploy.chain(f'{model}@1')['trainset']['ref'] == snapshot['ref']
    assert status_of(lambda: release(deploy, f'{model}@1', '2026-10-20T10:30+08:00', site='D', line='L')) == 200
    assert status_of(lambda: deploy.create_model('X', 'S', 'L', 'det', '')) == 403
    assert status_of(lambda: deploy.set_tag(model, 1, 'candidate', True)) == 403
    assert status_of(lambda: deploy.register_model_version(model, {'files': [], 'trainset_id': 1,
                                                                   'code_commit': COMMIT})) == 403
    image = next(f for f in ops.parse_manifest(api.read_object(snapshot['manifest_sha256']))
                 if f[0].startswith('images/'))
    assert status_of(lambda: deploy.read_object(image[1])) == 403
    assert status_of(lambda: deploy.read_object(snapshot['manifest_sha256'])) == 403
    assert status_of(lambda: deploy.fetch_objects([(image[1], image[2])], lambda d, b: None)) == 403
    assert status_of(lambda: deploy.version(*ops.parse_ref(snapshot['ref']))) == 403
    assert status_of(lambda: ops.get_model(deploy, cache, f'{model}@1', tmp_path / 'd', echo=quiet)) == 200
    # algorithm engineers register models and evaluations but not releases
    assert status_of(lambda: release(algo, f'{model}@1', '2026-10-20T10:30+08:00')) == 403
    # field accounts see none of it
    for call in (field.models, lambda: field.model_version(model, 1), lambda: field.chain(f'{model}@1'),
                 lambda: field.releases(), lambda: field.evals(), lambda: field.read_object(mv['onnx_sha256']),
                 lambda: field.create_model('X', 'S', 'L', 'det', '')):
        assert status_of(call) == 403


# --- the trace chain ---------------------------------------------------------------------------------------
def test_chain_from_a_release_down_to_the_pool(api, make_api, model, snapshot, tmp_path, service):
    mv = register(api, model, snapshot, files_in(tmp_path))
    e = ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=deteval_out(tmp_path), echo=quiet)
    site = f'S{uuid.uuid4().hex[:6]}'
    r = release(make_api('deploy'), f'{model}@1', '2026-10-20T10:30+08:00', site=site, line='TX', gpu='RTX3070')
    c = api.chain(r['ref'], check=True)
    assert c['release']['id'] == r['id'] and c['model_version']['id'] == mv['id']
    assert [x['id'] for x in c['evals']] == [e['id']]
    assert c['trainset']['ref'] == snapshot['ref'] and c['trainset']['trainset']['classes'] == CLASSES
    assert [s['ref'] for s in c['sources']] == snapshot['trainset']['sources']
    assert c['check']['problem_count'] == 0 and c['check']['objects'] > 20
    lines = []
    ops.show_chain(lines.append, c)
    text = '\n'.join(lines)
    for part in (r['ref'], 'RTX3070', mv['ref'], COMMIT, f'#{e["id"]}', snapshot['ref'],
                 snapshot['trainset']['sources'][0], '都在文件库里'):
        assert part in text
    from_version = api.chain(f'{model}@1')
    assert from_version['release'] is None and [x['ref'] for x in from_version['releases']] == [r['ref']]

    # a file of the chain is lost: --check names it (and the store is put back for the other tests)
    store = service.app.state.store
    path = store.path(mv['onnx_sha256'])
    aside = tmp_path / 'aside'
    path.rename(aside)
    try:
        check = api.chain(r['ref'], check=True)['check']
        assert check['problem_count'] == 1 and 'model.onnx' in check['problems'][0]
        with db.connect(service.settings.dsn) as conn:
            problems = ds.verify(conn, store, echo=quiet, more=models.verify_inputs)
        assert any(mv['onnx_sha256'][:12] in p for p in problems)
    finally:
        aside.rename(path)


def test_command_line(api, model, snapshot, tmp_path, monkeypatch, service, tokens, capsys):
    login(monkeypatch, tmp_path, service, tokens['algo'])
    run = files_in(tmp_path)
    args = ['model', 'register', model, '--onnx', str(run / 'best.onnx'), '--trainset', snapshot['ref'],
            '--metrics', str(run / 'val.json'), '--file', str(run / 'best.pt'), '--code-commit', COMMIT]
    assert fw_cli.main(args) == 0
    assert f'已登记 {model}@1' in capsys.readouterr().out
    assert fw_cli.main(args) == 0
    assert '已经登记过' in capsys.readouterr().out
    assert fw_cli.main(['model', 'list']) == 0
    assert model in capsys.readouterr().out
    assert fw_cli.main(['model', 'versions', model]) == 0
    assert snapshot['ref'] in capsys.readouterr().out
    assert fw_cli.main(['model', 'tag', f'{model}@1', 'candidate']) == 0
    assert '加上了标签「候选」' in capsys.readouterr().out
    assert fw_cli.main(['eval', 'register', f'{model}@1', '--testset', f'{snapshot["ref"]}:test',
                        '--deteval', str(deteval_out(tmp_path))]) == 0
    assert '已登记评估报告' in capsys.readouterr().out
    assert fw_cli.main(['eval', 'list', model]) == 0
    assert 'aoi-deteval 0.2.0' in capsys.readouterr().out
    assert fw_cli.main(['model', 'show', model]) == 0
    out = capsys.readouterr().out
    assert 'mAP50: 0.81' in out and 'box.p: 0.9' in out and '候选' in out and 'files/best.pt' in out
    assert fw_cli.main(['model', 'get', f'{model}@1', '--to', str(tmp_path / 'm')]) == 0
    assert (tmp_path / 'm' / 'model.onnx').is_file()
    assert fw_cli.main(['release', 'register', '--model', f'{model}@1', '--deploy-repo', 'git@git.example.com:d.git',
                        '--deploy-commit', 'abcdef1']) == 1
    assert '部署账号' in capsys.readouterr().err

    login(monkeypatch, tmp_path, service, tokens['deploy'])
    site = f'S{uuid.uuid4().hex[:6]}'
    assert fw_cli.main(['release', 'register', '--model', f'{model}@1', '--site', site, '--line', 'TX',
                        '--deploy-repo', 'git@git.example.com:d.git', '--deploy-commit', 'abcdef1',
                        '--deployed-at', '2026-10-20T10:30+08:00', '--gpu', 'RTX3070', '--trt', '10.3',
                        '--precision', 'fp16']) == 0
    assert f'已登记发布 {site}-TX-r1' in capsys.readouterr().out
    assert fw_cli.main(['release', 'current', '--site', site, '--line', 'TX']) == 0
    assert f'{model}@1' in capsys.readouterr().out
    assert fw_cli.main(['release', 'list', '--site', site]) == 0
    assert 'RTX3070，TRT 10.3，FP16' in capsys.readouterr().out
    assert fw_cli.main(['model', 'chain', f'{site}-TX-r1', '--check']) == 0
    out = capsys.readouterr().out
    assert snapshot['ref'] in out and '都在文件库里' in out
    assert fw_cli.main(['release', 'show', f'{site}-TX-r1']) == 0
    assert fw_cli.main(['get', snapshot['ref'], '--to', str(tmp_path / 'x')]) == 1  # deploy cannot fetch data

    import fw
    m = fw.get_model(f'{model}@1', tmp_path / 'script')
    assert m['onnx_sha256'] == sha256_file(run / 'best.onnx')[0] and (tmp_path / 'script' / 'classes.txt').is_file()


def test_training_script_call(api, model, snapshot, tmp_path, monkeypatch, service, tokens):
    login(monkeypatch, tmp_path, service, tokens['algo'])
    import fw
    ts = fw.get_trainset(snapshot['ref'], tmp_path / 'train')
    run = files_in(tmp_path)
    mv = fw.register_model(model, onnx=run / 'best.onnx', trainset=ts, metrics=run / 'val.json', code_commit=COMMIT)
    assert mv['ref'] == f'{model}@1' and mv['trainset']['id'] == ts['id']
    assert fw.register_model(model, onnx=run / 'best.onnx', trainset=ts['id'], metrics=run / 'val.json',
                             code_commit=COMMIT)['existing']


def login(monkeypatch, tmp_path, service, token):
    monkeypatch.setenv('FW_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setenv('FW_CACHE', str(tmp_path / 'cache'))
    monkeypatch.delenv('FW_SERVER', raising=False)
    monkeypatch.delenv('FW_TOKEN', raising=False)
    assert fw_cli.main(['login', '--server', service.url, '--token', token]) == 0


# --- what the database keeps -------------------------------------------------------------------------------
@pytest.fixture
def registered(api, make_api, model, snapshot, tmp_path):
    mv = register(api, model, snapshot, files_in(tmp_path))
    e = ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=deteval_out(tmp_path), echo=quiet)
    api.set_tag(model, 1, 'candidate', True)
    r = release(make_api('deploy'), f'{model}@1', '2026-10-20T10:30+08:00', site=f'S{uuid.uuid4().hex[:6]}',
                line='TX')
    return {'mv': mv['id'], 'fs': mv['fileset_id'], 'e': e['id'], 'efs': e['fileset_id'], 'r': r['id'],
            'm': mv['model']}


@pytest.mark.parametrize('statement', [
    "UPDATE models SET note = 'x' WHERE lower(name) = lower(%(m)s)",
    "DELETE FROM models WHERE lower(name) = lower(%(m)s)",
    "UPDATE model_versions SET metrics = '{}' WHERE id = %(mv)s",
    "UPDATE model_versions SET trainset_id = trainset_id WHERE id = %(mv)s",
    "DELETE FROM model_versions WHERE id = %(mv)s",
    "UPDATE filesets SET frozen = false WHERE id = %(fs)s",
    "DELETE FROM filesets WHERE id = %(fs)s",
    "UPDATE fileset_files SET size = size + 1 WHERE fileset_id = %(fs)s",
    "DELETE FROM fileset_files WHERE fileset_id = %(fs)s",
    "INSERT INTO fileset_files VALUES (%(fs)s, 'files/new', '\\x" + '00' * 32 + "', 1)",
    "UPDATE eval_reports SET metrics = '{}' WHERE id = %(e)s",
    "DELETE FROM eval_reports WHERE id = %(e)s",
    "UPDATE releases SET deployed_at = now() WHERE id = %(r)s",
    "DELETE FROM releases WHERE id = %(r)s",
    "UPDATE model_tag_events SET added = false WHERE model_version_id = %(mv)s",
    "DELETE FROM model_tag_events WHERE model_version_id = %(mv)s",
    "TRUNCATE models CASCADE",
    "TRUNCATE model_versions CASCADE",
    "TRUNCATE filesets CASCADE",
    "TRUNCATE fileset_files",
    "TRUNCATE eval_reports",
    "TRUNCATE releases",
    "TRUNCATE model_tag_events",
])
def test_records_cannot_change(sql, registered, statement):
    with pytest.raises(psycopg.Error):
        with sql.transaction():
            sql.execute(statement, registered)
    assert sql.execute('SELECT count(*) AS n FROM fileset_files WHERE fileset_id = %s',
                       (registered['fs'],)).fetchone()['n'] == 4


def test_what_the_database_refuses(sql, registered, snapshot):
    mv = sql.execute('SELECT * FROM model_versions WHERE id = %s', (registered['mv'],)).fetchone()

    def fileset(paths, frozen=True):
        rows = [(p, b'\x01' * 32, 1) for p in paths]
        manifest = __import__('hashlib').sha256(
            ('fw-manifest 1\n' + ''.join(f'{p}\t{"01" * 32}\t1\n' for p in sorted(paths))).encode()).digest()
        fid = sql.execute('INSERT INTO filesets (manifest_sha256, file_count, total_bytes) VALUES (%s, %s, %s) '
                          'RETURNING id', (manifest, len(rows), len(rows))).fetchone()['id']
        for p, d, s in rows:
            sql.execute('INSERT INTO fileset_files VALUES (%s, %s, %s, %s)', (fid, p, d, s))
        if frozen:
            sql.execute('UPDATE filesets SET frozen = true WHERE id = %s', (fid,))
        return fid

    def insert_version(fid, onnx=b'\x01' * 32, trainset=snapshot['id']):
        sql.execute('INSERT INTO model_versions (model_id, number, fileset_id, onnx_sha256, trainset_id, code_commit, '
                    "metrics, created_by) VALUES (%s, 99, %s, %s, %s, 'abcdef1', '{}', %s)",
                    (mv['model_id'], fid, onnx, trainset, mv['created_by']))

    with pytest.raises(psycopg.Error, match='belong to exactly one'):  # a file set nobody holds
        with sql.transaction():
            fileset(['model.onnx'])
    with pytest.raises(psycopg.Error, match='must be frozen'):
        with sql.transaction():
            insert_version(fileset(['model.onnx'], frozen=False))
    with pytest.raises(psycopg.Error, match='hold model.onnx'):  # the ONNX hash is not the file set's model.onnx
        with sql.transaction():
            insert_version(fileset(['model.onnx']), onnx=b'\x02' * 32)
    pool_version = sql.execute("SELECT v.id FROM versions v JOIN datasets d ON d.id = v.dataset_id "
                               "WHERE d.kind = 'pool' LIMIT 1").fetchone()['id']
    with pytest.raises(psycopg.Error, match='not a training-set snapshot'):
        with sql.transaction():
            insert_version(fileset(['model.onnx']), trainset=pool_version)
    with pytest.raises(psycopg.Error, match='do not match'):  # rows that do not rebuild the manifest
        with sql.transaction():
            fid = fileset(['model.onnx'], frozen=False)
            sql.execute("UPDATE fileset_files SET size = 2 WHERE fileset_id = %s", (fid,))
            sql.execute('UPDATE filesets SET frozen = true WHERE id = %s', (fid,))
    with pytest.raises(psycopg.Error, match='a split is named'):
        with sql.transaction():
            sql.execute("INSERT INTO eval_reports (model_version_id, testset_id, split, fileset_id, metrics, created_by) "
                        "VALUES (%s, %s, '', %s, '{}', %s)",
                        (mv['id'], snapshot['id'], fileset(['metrics.json']), mv['created_by']))
    with pytest.raises(psycopg.Error, match='releases_deploy_repo_check'):
        with sql.transaction():
            sql.execute("INSERT INTO releases (site, line, number, model_version_id, deploy_repo, deploy_commit, "
                        "deployed_at, created_by) VALUES ('S', 'L', 1, %s, 'https://tok@host/r', 'abcdef1', now(), %s)",
                        (mv['id'], mv['created_by']))
