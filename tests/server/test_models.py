"""Models, model versions, evaluation reports, releases, tags and the trace chain: what is checked when they
are registered, who may do what, and what the database keeps frozen."""
import datetime
import json
import uuid

import psycopg
import pytest

from helpers import quiet, read_tree, register_place, write
from fw import cli as fw_cli
from fw import ops
from fw.api import ApiError
from fw.contract import sha256_file
from fwserver import cli as server_cli
from fwserver import datasets as ds
from fwserver import db, models

from test_trainsets import CLASSES, make, pool, sample_files, status_of

COMMIT = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'


def onnx_bytes(tag: str) -> bytes:
    """Enough of an ONNX file for the checks: ir_version (field 1, varint) and producer_name (field 2) holding
    the tag, a well-formed protobuf message without a graph."""
    producer = ('pytorch ' + tag).encode()
    return b'\x08\x07\x12' + bytes([len(producer)]) + producer


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


def test_models_are_listed_by_the_snapshot_they_were_trained_on(api, make_api, snapshot, tmp_path):
    """/models?trainset=<snapshot>: the models a version of which was trained on a version of that snapshot (the
    web proposes the model to register on only when exactly one was)."""
    other = make(api, f'T-{uuid.uuid4().hex[:8]}', api.trainset(snapshot['dataset'], 1)['trainset']['sources'], test_dirs=['2026-10'])
    a, b, c = (f'M-{uuid.uuid4().hex[:8]}' for _ in range(3))
    for m in (a, b, c):
        api.create_model(m, 'SES', 'TX', 'det', '')
    register(api, a, snapshot, files_in(tmp_path, f'by-ts-a-{uuid.uuid4().hex}'))
    register(api, b, other, files_in(tmp_path, f'by-ts-b-{uuid.uuid4().hex}'))
    register(api, c, other, files_in(tmp_path, f'by-ts-c1-{uuid.uuid4().hex}'))
    register(api, c, snapshot, files_in(tmp_path, f'by-ts-c2-{uuid.uuid4().hex}'))   # its second version

    def names(rows):
        return sorted(r['name'] for r in rows)
    ts = snapshot['dataset']
    assert names(api.get('/models', trainset=ts)) == sorted([a, c])
    assert names(api.get('/models', trainset=other['dataset'].lower())) == sorted([b, c])  # names are not case-sensitive
    assert names(api.get('/models', trainset=ts, q=c)) == [c]
    assert api.get('/models', trainset=ts, line='TY') == []
    assert api.get('/models', trainset=f'T-{uuid.uuid4().hex[:8]}') == []          # no such snapshot: none, not an error
    with pytest.raises(ApiError) as e:
        make_api('worker').get('/models', trainset=ts)
    assert e.value.status == 403


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
    for files, message in (([['weight/x.pt', digest, size], ['model.onnx', digest, size]], 'files/'),
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
    out = []
    mv = ops.register_model(api, model, files_in(tmp_path) / 'best.onnx', snapshot['ref'], code_dir=repo,
                            echo=out.append)
    assert mv['code_commit'] == head and mv['code_dirty']
    assert mv['code_repo'] == 'https://git.example.com/algo/train.git'
    assert '没提交的改动' in out[0] and 'token123' not in ''.join(out)
    with pytest.raises(ops.FwError, match='不是 git 仓库'):
        ops.register_model(api, model, files_in(tmp_path, 'c') / 'best.onnx', snapshot['ref'], code_dir=tmp_path,
                           echo=quiet)
    with pytest.raises(ops.FwError, match='只写一个'):
        ops.register_model(api, model, files_in(tmp_path, 'c') / 'best.onnx', snapshot['ref'], code_dir=repo,
                           code_commit=COMMIT, echo=quiet)


def test_the_training_code_is_optional(api, model, snapshot, tmp_path, monkeypatch, cache):
    # The current folder's git is never taken unasked (it may be any repository, e.g. this one).
    monkeypatch.setattr(ops, 'git_info', lambda folder: pytest.fail('git read without --code-dir'))
    run = files_in(tmp_path)
    mv = ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], weights=run / 'best.pt', echo=quiet)
    assert mv['code_commit'] == '' and mv['code_repo'] == '' and not mv['code_dirty']
    assert ops.code_text(mv) == '未记录'
    assert {f['path'] for f in api.model_version(model, str(mv['number']))['files']} == \
        {'model.onnx', 'classes.txt', 'weights/best.pt'}
    again = ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], weights=run / 'best.pt', echo=quiet)
    assert again['existing'] and again['number'] == mv['number']
    out = tmp_path / 'got'
    ops.get_model(api, cache, mv['ref'], out, echo=quiet)
    assert (out / 'weights' / 'best.pt').read_bytes() == (run / 'best.pt').read_bytes()
    with pytest.raises(ops.FwError, match='--code-repo 也要写 --code-commit'):
        ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], code_repo='https://g/x.git', echo=quiet)
    with pytest.raises(ops.FwError, match='--weights 只收一个文件'):
        ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], weights=run, echo=quiet)


def test_no_training_code_reads_as_an_empty_string(api, model, snapshot, tmp_path, capsys):
    # Re-review 01:37: a client from before 0004 slices code_commit; null made `fw model versions` exit.
    run = files_in(tmp_path)
    mv = ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], echo=quiet)
    assert mv['code_commit'] == ''
    assert [v['code_commit'] for v in api.model_versions(model)] == ['']
    assert api.model_version(model, str(mv['number']))['code_commit'] == ''
    for v in api.model_versions(model):  # what the client of f5320cd does with each row
        assert (v['code_commit'][:10] + ('*' if v['code_dirty'] else '')) == ''
    again = ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], echo=quiet)
    assert again['existing'] and again['number'] == mv['number']


def test_empty_weights_are_refused(api, model, snapshot, tmp_path):
    # Re-review 01:37: an empty best.pt was frozen, and the retry with the real file was then refused (409).
    run = files_in(tmp_path)
    empty = write(tmp_path / 'empty', 'best.pt', b'')
    with pytest.raises(ops.FwError, match='是空的'):
        ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], weights=empty, echo=quiet)
    rows = ops._send_files(api, [('model.onnx', run / 'best.onnx'), ('weights/best.pt', empty)], 2, quiet)
    with pytest.raises(ApiError, match='是空的'):
        api.register_model_version(model, {'trainset_id': snapshot['id'], 'files': rows})
    assert api.model_versions(model) == []
    mv = ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], weights=run / 'best.pt', echo=quiet)
    assert mv['number'] == 1 and not mv['existing']


def test_git_variables_never_redirect_the_code_dir(api, model, snapshot, tmp_path, monkeypatch):
    # Re-review 01:37: GIT_DIR / GIT_WORK_TREE inherited from the shell made --code-dir B record repository A.
    import subprocess
    env = dict(GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@x')
    heads = {}
    for name in ('a', 'b'):
        repo = tmp_path / name
        repo.mkdir()
        git = lambda *a: subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True,  # noqa: E731
                                        env={**{k: v for k, v in __import__('os').environ.items()
                                                if not k.startswith('GIT_')}, **env})
        git('init', '-q')
        write(repo, 'train.py', name.encode())
        git('add', 'train.py')
        git('commit', '-q', '-m', name)
        git('remote', 'add', 'origin', f'https://git.example.com/{name}.git')
        heads[name] = git('rev-parse', 'HEAD').stdout.decode().strip()
    assert heads['a'] != heads['b']
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'a' / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(tmp_path / 'a'))
    mv = ops.register_model(api, model, files_in(tmp_path) / 'best.onnx', snapshot['ref'], code_dir=tmp_path / 'b',
                            echo=quiet)
    assert (mv['code_commit'], mv['code_repo']) == (heads['b'], 'https://git.example.com/b.git')
    assert ops.git_info(tmp_path) is None  # not a repository, whatever GIT_DIR says


def test_the_server_checks_weights_and_code(api, model, snapshot, tmp_path):
    run = files_in(tmp_path)
    rows = ops._send_files(api, [('model.onnx', run / 'best.onnx'), ('weights/a.pt', run / 'best.pt'),
                                 ('weights/b.pt', run / 'val.json'), ('weights/x/c.pt', run / 'best.pt')], 2, quiet)
    base = {'trainset_id': snapshot['id']}
    for files, match in (([rows[0], rows[1], rows[2]], '只能有一个权重文件'), ([rows[0], rows[3]], '不能有子目录')):
        with pytest.raises(ApiError, match=match):
            api.register_model_version(model, dict(base, files=files))
    for body in ({'code_repo': 'https://g/x.git'}, {'code_dirty': True}):
        with pytest.raises(ApiError, match='就要写训练代码的提交号'):
            api.register_model_version(model, dict(base, files=[rows[0]], **body))


def test_the_server_refuses_an_onnx_with_external_data_too(api, model, snapshot, tmp_path):
    # The web registers a model version without fw, so the server makes the check fw makes before sending.
    from helpers import EXTERNAL, ld, onnx_model, tensor
    bad = write(tmp_path / 'ext', 'model.onnx', onnx_model(ld(5, tensor('w', EXTERNAL))))
    rows = ops._send_files(api, [('model.onnx', bad)], 2, quiet)
    with pytest.raises(ApiError, match='external data'):
        api.register_model_version(model, {'trainset_id': snapshot['id'], 'files': rows})
    broken = write(tmp_path / 'broken', 'model.onnx', b'\x08\x07\x12\xff')  # starts like ONNX, does not parse
    rows = ops._send_files(api, [('model.onnx', broken)], 2, quiet)
    with pytest.raises(ApiError, match='不完整'):
        api.register_model_version(model, {'trainset_id': snapshot['id'], 'files': rows})
    assert api.model_versions(model) == []


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
def deteval_out(tmp_path, models=('model_s0',), sets=(('test', 3),)):
    """An aoi-deteval evaluate output folder; the fixture snapshot has 3 test images and 2 val images."""
    d = tmp_path / f'deteval-{uuid.uuid4().hex[:6]}'
    summary = {}
    for i, m in enumerate(models):
        summary[m] = {'weights': None, 'thresholds': {'f1': 0.31}, 'reached': {}}
        for name, images in sets:
            summary[m][name] = {'model': m, 'set': name, 'images': images, 'mAP50': [0.7, 0.8][i], 'lf1_F1': 0.8}
    write(d, 'summary.json', json.dumps(summary).encode())
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
    assert e['metrics'] == {'weights': None, 'thresholds': {'f1': 0.31}, 'reached': {}, 'model': 'model_s0',
                            'set': 'test', 'images': 3, 'mAP50': 0.7, 'lf1_F1': 0.8}
    assert 'model_s0' in e['note'] and '集合 test' in e['note']
    shown = api.eval(e['id'])
    assert [f['path'] for f in shown['files']] == ['files/metrics.csv', 'files/report.txt', 'files/run_info.json',
                                                   'files/summary.json', 'metrics.json']
    assert ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=out, echo=quiet)['existing']

    two = deteval_out(tmp_path, ('model_s0', 'model_s1'))
    with pytest.raises(ops.FwError, match='--deteval-model'):
        ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=two, echo=quiet)
    e2 = ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=two, deteval_model='model_s1',
                           echo=quiet)
    assert e2['metrics']['mAP50'] == 0.8

    with pytest.raises(ops.FwError, match=':test'):
        ops.register_eval(api, f'{model}@1', snapshot['ref'], deteval=out, echo=quiet)
    with pytest.raises(ApiError, match='没有 val'):
        empty = make(api, f'T-{uuid.uuid4().hex[:8]}',
                     [f'{pool(api, tmp_path, sample_files("x", 1))}@1'], val=0.0)
        ops.register_eval(api, f'{model}@1', f'{empty["ref"]}:val', metrics=write(tmp_path, 'v.json', b'{}'),
                          echo=quiet)
    test_pool = pool(api, tmp_path, sample_files('t', 2))
    metrics = write(tmp_path, 'm.json', b'{"recall": 0.9}')
    e3 = ops.register_eval(api, f'{model}@1', f'{test_pool}@1', metrics=metrics, tool='other', echo=quiet)
    assert e3['testset']['ref'] == f'{test_pool}@1' and e3['metrics'] == {'recall': 0.9}
    with pytest.raises(ApiError, match='不用写划分'):
        ops.register_eval(api, f'{model}@1', f'{test_pool}@1:test', metrics=metrics, echo=quiet)
    with pytest.raises(ops.FwError, match='只写一个'):
        ops.register_eval(api, f'{model}@1', f'{test_pool}@1', echo=quiet)
    assert [x['id'] for x in api.evals(model)] == [e['id'], e2['id'], e3['id']]

    # aoi-deteval writes a bare NaN for a metric with nothing to measure (e.g. no ground truth of a class)
    nan = deteval_out(tmp_path)
    (nan / 'summary.json').write_text('{"model_s0": {"thresholds": {}, "val": {"set": "val", "images": 2, '
                                      '"mAP50": 0.5, "AP_dent": NaN, "x": -Infinity, "y": 1e309}}}', encoding='utf-8')
    lines = []
    e4 = ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:val', deteval=nan, echo=lines.append)
    assert e4['metrics'] == {'thresholds': {}, 'set': 'val', 'images': 2, 'mAP50': 0.5, 'AP_dent': None, 'x': None,
                             'y': None}
    assert any('3 项没有定义的指标' in line for line in lines)
    bad = write(tmp_path, 'nan.json', b'{"recall": NaN}')
    with pytest.raises(ops.FwError, match='NaN'):  # other metrics files: standard JSON only
        ops.register_eval(api, f'{model}@1', f'{test_pool}@1', metrics=bad, echo=quiet)


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


# --- found in the review of 2026-11-05 13:12: each reproduced first --------------------------------------------
def test_a_retry_stays_on_the_version_it_meant(api, make_api, model, snapshot, tmp_path, monkeypatch):
    """The first request succeeds, its answer is lost, a new version is registered, then the retry goes out:
    both must land on the version the call meant, not on the newest one."""
    register(api, model, snapshot, files_in(tmp_path, 'one'))
    out = deteval_out(tmp_path)
    deploy = make_api('deploy')
    calls = []

    def lose_the_first_answer(real):
        return lambda url, **kw: lose(real, url, **kw)

    def lose(real, url, **kw):
        r = real(url, **kw)
        if not calls and ('/evals' in url or url.endswith('/releases')):
            calls.append(url)
            register(api, model, snapshot, files_in(tmp_path, f'meanwhile-{len(calls)}-{uuid.uuid4().hex[:4]}'))
            import httpx
            raise httpx.ReadTimeout('the answer was lost', request=r.request)
        return r

    monkeypatch.setattr(api.http, 'post', lose_the_first_answer(api.http.post))
    monkeypatch.setattr(deploy.http, 'post', lose_the_first_answer(deploy.http.post))
    e = ops.register_eval(api, model, f'{snapshot["ref"]}:test', deteval=out, echo=quiet)
    assert e['model_version']['ref'] == f'{model}@1' and e['existing']
    assert api.model_version(model, 2)['evals'] == []
    calls.clear()
    site = f'S{uuid.uuid4().hex[:6]}'
    r = release(deploy, model, '2026-10-20T10:00+08:00', site=site)
    assert r['model_version']['ref'] == f'{model}@2' and r['existing']
    assert api.current_release(site, 'TX')['model_version']['ref'] == f'{model}@2'
    assert api.model_version(model, 3)['releases'] == []
    for path, body in ((f'/models/{model}/versions/latest/evals', {'testset_id': snapshot['id'], 'split': 'test',
                                                                    'files': []}),
                       ('/releases', {'model': model, 'number': 'latest', 'deploy_repo': 'x', 'deploy_commit': 'abcdef1',
                                      'deployed_at': '2026-10-20T10:00+08:00'})):
        with pytest.raises(ApiError, match='具体的模型版本'):
            (deploy if path == '/releases' else api).post(path, body)


def test_a_deteval_report_must_be_the_test_set(api, model, snapshot, tmp_path):
    register(api, model, snapshot, files_in(tmp_path))
    val_report = deteval_out(tmp_path, sets=(('val', 2),))
    with pytest.raises(ops.FwError, match='--deteval-set'):  # a val report is not a test result
        ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=val_report, echo=quiet)
    with pytest.raises(ops.FwError, match='评了 2 张图.*有 3 张'):
        ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=val_report, deteval_set='val',
                          echo=quiet)
    with pytest.raises(ops.FwError, match='没有集合 nope'):
        ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=val_report, deteval_set='nope',
                          echo=quiet)
    renamed = deteval_out(tmp_path, sets=(('TX_v2_test', 3), ('val', 2)))
    e = ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=renamed, deteval_set='TX_v2_test',
                          echo=quiet)
    assert e['metrics']['set'] == 'TX_v2_test' and e['metrics']['images'] == 3
    test_pool = pool(api, tmp_path, sample_files('t', 2))  # a plain dataset version: its only set, same count
    assert ops.register_eval(api, f'{model}@1', f'{test_pool}@1', deteval=deteval_out(tmp_path, sets=(('x', 2),)),
                             echo=quiet)['metrics']['set'] == 'x'
    with pytest.raises(ops.FwError, match='评了 3 张图'):
        ops.register_eval(api, f'{model}@1', f'{test_pool}@1', deteval=deteval_out(tmp_path), echo=quiet)


def test_a_token_in_the_repository_is_never_printed(api, model, snapshot, tmp_path, monkeypatch):
    run = files_in(tmp_path)
    lines = []
    mv = ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], code_commit=COMMIT,
                            code_repo='https://bot:ghp_SECRET123@git.example.com/train.git', echo=lines.append)
    assert mv['code_repo'] == 'https://git.example.com/train.git'
    assert lines and not any('SECRET' in line for line in lines)
    lines.clear()  # blanks around the address (re-review 13:47): the token is still caught before it is shown
    mv = ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], code_commit=COMMIT,
                            code_repo='  https://bot:ghp_SECRET456@git.example.com/train.git\t', echo=lines.append)
    assert mv['code_repo'] == 'https://git.example.com/train.git' and mv['existing']
    assert lines and not any('SECRET' in line for line in lines)
    assert ops.strip_credentials(' HTTPS://u:t@h/x ') == 'HTTPS://h/x'


def test_two_lines_never_share_a_release_ref(api, make_api, snapshot, tmp_path):
    name = f'M-{uuid.uuid4().hex[:8]}'
    with pytest.raises(ApiError, match='只能用汉字'):
        api.create_model(name, 'A-B', 'C', 'det', '')
    register_place('A', 'B')
    api.create_model(name, 'A', 'B', 'det', '')
    register(api, name, snapshot, files_in(tmp_path))
    for site, line in (('A-B', 'C'), ('A', 'B-C'), ('SES/A', 'TX'), ('SES', 'T X'), ('S%2F', 'TX'), ('S?', 'TX'),
                       ('SES@A', 'TX'), ('现场／1', 'TX'), ('现场 1', 'TX')):
        with pytest.raises(ApiError, match='只能用汉字、英文字母、数字'):
            release(make_api('deploy'), f'{name}@1', '2026-10-20T10:00+08:00', site=site, line=line)
    r = release(make_api('deploy'), f'{name}@1', '2026-10-20T10:00+08:00', site='现场1', line='线_2.a')
    assert api.release(r['ref'])['ref'] == r['ref'] == '现场1-线_2.a-r1'
    assert api.chain(r['ref'])['release']['ref'] == r['ref']


@pytest.mark.parametrize('value', ['SES', 'L_2.a', '现场1', '线_2.a', '\u3400', '\u9fff', 'SES@A', 'A-B', 'a/b', ' a',
                                   'a\tb', '现场／1', '현장', '😀', '\u4dc0', 'é', 'x%2F', 'a#b', ''])
def test_the_database_takes_the_names_the_server_takes(sql, api, model, snapshot, tmp_path, value):
    """Re-review 14:05: one rule for sites and lines, in models.SITE_LINE and in the CHECKs of 0003."""
    mv = api.model_version(model, 1) if api.model_versions(model) else register(api, model, snapshot, files_in(tmp_path))
    user = sql.execute('SELECT created_by FROM model_versions WHERE id = %s', (mv['id'],)).fetchone()['created_by']
    server_takes = bool(models.SITE_LINE.match(value))
    for statement in ("INSERT INTO releases (site, line, number, model_version_id, deploy_repo, deploy_commit, "
                      "deployed_at, created_by) VALUES (%(v)s, 'TX', 1, %(mv)s, 'x', 'abcdef1', now(), %(u)s)",
                      "INSERT INTO releases (site, line, number, model_version_id, deploy_repo, deploy_commit, "
                      "deployed_at, created_by) VALUES ('SES', %(v)s, 1, %(mv)s, 'x', 'abcdef1', now(), %(u)s)",
                      "INSERT INTO models (name, site, line, task, created_by) VALUES ('db-' || md5(random()::text), "
                      "%(v)s, 'TX', 'det', %(u)s)"):
        try:
            with sql.transaction():
                sql.execute(statement, {'v': value, 'mv': mv['id'], 'u': user})
                raise RuntimeError('roll back')
        except RuntimeError:
            database_takes = True
        except psycopg.Error:
            database_takes = False
        assert database_takes == server_takes, (statement, value)


def test_the_chain_check_covers_the_test_sets(api, model, snapshot, tmp_path, service):
    register(api, model, snapshot, files_in(tmp_path))
    unique = f'only-here-{uuid.uuid4().hex}'
    test_pool = pool(api, tmp_path, {'t/a.png': unique.encode(), 't/b.png': b'b' + unique.encode()})
    ops.register_eval(api, f'{model}@1', f'{test_pool}@1', deteval=deteval_out(tmp_path, sets=(('t', 2),)),
                      echo=quiet)
    digest = sha256_file(write(tmp_path, 'x.png', unique.encode()))[0]
    path = service.app.state.store.path(digest)
    moved = tmp_path / 'aside'
    path.rename(moved)
    try:
        c = api.chain(f'{model}@1', check=True)
        assert c['check']['problem_count'] == 1 and f'{test_pool}@1：t/a.png' in c['check']['problems'][0]
    finally:
        moved.rename(path)


@pytest.mark.parametrize('change', ['rewrite', 'delete', 'appear'])
def test_a_report_changed_while_it_is_registered(api, cache, model, snapshot, tmp_path, monkeypatch, change):
    """The report folder changes after the metrics were read (summary.json rewritten or deleted, run_info.json
    appearing late): the metrics and the archive still come from one copy of the folder, taken first."""
    register(api, model, snapshot, files_in(tmp_path))
    out = deteval_out(tmp_path)
    if change == 'appear':
        (out / 'run_info.json').unlink()
    real = ops._send_files

    def meanwhile(api_, items, workers, echo):
        if change == 'rewrite':
            doc = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
            doc['model_s0']['test']['mAP50'] = 0.95
            (out / 'summary.json').write_text(json.dumps(doc), encoding='utf-8')
        elif change == 'delete':
            (out / 'summary.json').unlink()
        else:
            write(out, 'run_info.json', json.dumps({'deteval_version': '9.9'}).encode())
        return real(api_, items, workers, echo)

    monkeypatch.setattr(ops, '_send_files', meanwhile)
    e = ops.register_eval(api, f'{model}@1', f'{snapshot["ref"]}:test', deteval=out, echo=quiet)
    assert e['metrics']['mAP50'] == 0.7
    archived = {f['path']: f for f in api.eval(e['id'])['files']}
    summary = json.loads(api.read_object(archived['files/summary.json']['sha256']))
    assert summary['model_s0']['test']['mAP50'] == 0.7
    if change == 'appear':  # not there when the copy was taken: neither archived nor used for the version
        assert 'files/run_info.json' not in archived and e['tool_version'] == ''
    else:
        assert 'files/run_info.json' in archived and e['tool_version'] == '0.2.0'


def test_a_file_set_has_one_owner(sql, api, model, snapshot, tmp_path):
    mv = register(api, model, snapshot, files_in(tmp_path))
    row = sql.execute('SELECT fileset_id, created_by FROM model_versions WHERE id = %s', (mv['id'],)).fetchone()
    with pytest.raises(psycopg.Error, match='belongs to a model version'):
        with sql.transaction():
            sql.execute("INSERT INTO eval_reports (model_version_id, testset_id, split, fileset_id, metrics, created_by) "
                        "VALUES (%s, %s, 'test', %s, '{}', %s)",
                        (mv['id'], snapshot['id'], row['fileset_id'], row['created_by']))


def test_an_unreadable_folder_is_never_left_out(api, model, snapshot, tmp_path, monkeypatch):
    run = files_in(tmp_path)
    att = tmp_path / 'att'
    write(att, 'ok.txt', b'ok')
    write(att, 'locked/secret.txt', b'x')
    real = ops.os.scandir

    def scandir(path='.'):
        if str(path).endswith('locked'):
            raise PermissionError(13, 'Permission denied', str(path))
        return real(path)

    monkeypatch.setattr(ops.os, 'scandir', scandir)
    with pytest.raises(ops.FwError, match='读不了目录 .*locked'):
        ops.register_model(api, model, run / 'best.onnx', snapshot['ref'], files=[att], code_commit=COMMIT,
                           echo=quiet)
    register(api, model, snapshot, run)
    att2 = deteval_out(tmp_path)
    write(att2, 'locked/per_image.csv', b'x')
    with pytest.raises(ops.FwError, match='读不了目录'):  # --deteval keeps its whole folder the same way
        ops.register_eval(api, model, f'{snapshot["ref"]}:test', deteval=att2, echo=quiet)


def test_numbers_out_of_range(api, model, snapshot, tmp_path):
    run = files_in(tmp_path, metrics={'recall': 0.5})
    (run / 'val.json').write_text('{"recall": 1e309}', encoding='utf-8')
    with pytest.raises(ops.FwError, match='inf'):
        register(api, model, snapshot, run)
    data = b'{"recall": 1e309}'  # straight to the server: 400, not 500
    digest = api.put_bytes(data)
    onnx = onnx_bytes(uuid.uuid4().hex)
    body = {'files': [['model.onnx', api.put_bytes(onnx), len(onnx)], ['metrics.json', digest, len(data)]],
            'trainset_id': snapshot['id'], 'code_commit': COMMIT, 'code_repo': '', 'code_dirty': False, 'note': ''}
    with pytest.raises(ApiError, match='超出范围') as e:
        api.register_model_version(model, body)
    assert e.value.status == 400


def test_creating_the_same_model_at_once(api, make_api, sql):
    """Both requests pass the look-up and wait at the INSERT; the second gets 409, not 500."""
    import threading
    import time
    name = f'M-{uuid.uuid4().hex[:8]}'
    results = []
    clients = [make_api('algo'), make_api('algo')]
    with sql.transaction():
        sql.execute('LOCK TABLE models IN SHARE ROW EXCLUSIVE MODE')
        threads = [threading.Thread(target=lambda c=c: results.append(
            status_of(lambda: c.create_model(name, 'SES', 'TX', 'det', ''))), daemon=True) for c in clients]
        for t in threads:
            t.start()
        time.sleep(1.5)
    for t in threads:
        t.join(30)
    assert sorted(results) == [200, 409]


# --- releases ---------------------------------------------------------------------------------------------
def release(api, model_ref, when, commit='abcdef1', **kw):
    register_place(kw.get('site', 'SES'), kw.get('line', 'TX'))
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
    # a field account sees models and releases, downloads their files and registers releases; it changes
    # nothing else (deploy here is a second field account)
    assert deploy.model_version(model, 1)['id'] == mv['id']
    assert deploy.chain(f'{model}@1')['trainset']['ref'] == snapshot['ref']
    assert status_of(lambda: release(deploy, f'{model}@1', '2026-10-20T10:30+08:00', site='D', line='L')) == 200
    assert status_of(lambda: deploy.create_model('X', 'S', 'L', 'det', '')) == 403
    assert status_of(lambda: deploy.set_tag(model, 1, 'candidate', True)) == 403
    assert status_of(lambda: deploy.register_model_version(model, {'files': [], 'trainset_id': 1,
                                                                   'code_commit': COMMIT})) == 403
    image = next(f for f in ops.parse_manifest(api.read_object(snapshot['manifest_sha256']))
                 if f[0].startswith('images/'))
    assert status_of(lambda: deploy.read_object(image[1])) == 200  # it reads datasets too
    assert status_of(lambda: deploy.version(*ops.parse_ref(snapshot['ref']))) == 200
    assert status_of(lambda: deploy.register_eval(model, 1, {})) in (403, 422)
    assert status_of(lambda: deploy.submit_job('check')) == 403 and status_of(deploy.jobs) == 403
    assert status_of(lambda: ops.get_model(deploy, cache, f'{model}@1', tmp_path / 'd', echo=quiet)) == 200
    # algorithm engineers register models and evaluations but not releases
    assert status_of(lambda: release(algo, f'{model}@1', '2026-10-20T10:30+08:00')) == 403
    for call in (field.models, lambda: field.model_version(model, 1), lambda: field.chain(f'{model}@1'),
                 lambda: field.releases(), lambda: field.evals(), lambda: field.read_object(mv['onnx_sha256'])):
        assert status_of(call) == 200
    # a worker sees none of it
    worker = make_api('worker')
    for call in (worker.models, lambda: worker.model_version(model, 1), lambda: worker.releases(),
                 lambda: release(worker, f'{model}@1', '2026-10-20T10:30+08:00')):
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
            problems = ds.verify(conn, store, echo=quiet, more=server_cli.verify_inputs)
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
    assert '现场实施账号' in capsys.readouterr().err

    login(monkeypatch, tmp_path, service, tokens['deploy'])
    site = f'S{uuid.uuid4().hex[:6]}'
    register_place(site, 'TX')
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
    assert fw_cli.main(['get', snapshot['ref'], '--to', str(tmp_path / 'x')]) == 0  # a field account reads data too

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
    for path, size in (('weights/best.pt', 0), ('weights/x/best.pt', 5)):  # what models._entries refuses (0004)
        with pytest.raises(psycopg.Error, match='fileset_files_weights_file'):
            with sql.transaction():
                fid = sql.execute('INSERT INTO filesets (manifest_sha256, file_count, total_bytes) VALUES (%s, 1, 1) '
                                  'RETURNING id', (b'\x01' * 32,)).fetchone()['id']
                sql.execute('INSERT INTO fileset_files VALUES (%s, %s, %s, %s)', (fid, path, b'\x01' * 32, size))
    for repo, dirty in (('https://git.example.com/t.git', False), ('', True)):  # code without a commit (0004)
        with pytest.raises(psycopg.Error, match='model_versions_code_needs_commit'):
            with sql.transaction():
                sql.execute('INSERT INTO model_versions (model_id, number, fileset_id, onnx_sha256, trainset_id, '
                            "code_repo, code_dirty, metrics, created_by) VALUES (%s, 99, %s, %s, %s, %s, %s, '{}', %s)",
                            (mv['model_id'], fileset(['model.onnx']), b'\x01' * 32, snapshot['id'], repo, dirty,
                             mv['created_by']))
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
