"""Version labels (v0.1.0): another name for a version of a dataset, a snapshot or a model."""
import uuid

import pytest

from helpers import quiet, write
from fw import cli as fw_cli
from fw import ops
from fw.contract import ContractError, label_key, next_labels, split_ref

from test_evals import trained
from test_models import model, snapshot  # noqa: F401  (fixtures)
from test_trainsets import make, pool, sample_files, status_of


def versions(api, dataset, tmp_path, n):
    out = []
    for i in range(n):
        write(tmp_path / 'data', f'd{i}/x.bmp', f'label test {uuid.uuid4().hex}'.encode())
        out.append(ops.import_folder(api, dataset, tmp_path / 'data', echo=quiet))
    return out


def test_refs_are_read_in_one_place():
    assert split_ref('SES-TX') == ('SES-TX', 'latest')
    assert split_ref(' SES-TX@3 ') == ('SES-TX', '3')
    assert split_ref('SES-TX@v0.1.0') == ('SES-TX', 'v0.1.0')
    for bad in ('', '@3', 'SES-TX@', 'SES-TX@0', 'SES-TX@-1', 'SES-TX@v1', 'SES-TX@v01.0.0', 'SES-TX@V1.0.0',
                'SES-TX@1.0.0', 'SES-TX@v1.0.0-rc1', 'SES-TX@v1.0.0 x', 'SES-TX@３', None, 3):
        with pytest.raises(ContractError):
            split_ref(bad)
    with pytest.raises(ContractError):
        split_ref('B-1@v0.1.0', labels=False)
    assert split_ref('B-1@2', labels=False) == ('B-1', '2')
    assert sorted(['v0.10.0', 'v0.9.0', 'v1.0.0', 'v0.9.12'], key=label_key) == ['v0.9.0', 'v0.9.12', 'v0.10.0', 'v1.0.0']
    assert next_labels([]) == {'patch': 'v0.0.1', 'minor': 'v0.1.0', 'major': 'v1.0.0'}
    assert next_labels(['v0.1.0', 'v0.9.3', 'v0.2.0']) == {'patch': 'v0.9.4', 'minor': 'v0.10.0', 'major': 'v1.0.0'}


def test_a_label_names_a_version_for_good(api, make_api, dataset, tmp_path, cache):
    v1, v2, v3 = versions(api, dataset, tmp_path, 3)
    assert v1['labels'] == [] and api.labels(dataset)['labels'] == []
    assert api.labels(dataset)['suggest']['minor'] == 'v0.1.0'
    out = []
    v = ops.add_label(api, f'{dataset}@1', 'v0.0.1', note='冷启动', echo=out.append)
    assert v['id'] == v1['id'] and v['labels'] == ['v0.0.1'] and f'{dataset}@v0.0.1' in out[0]
    # the label is the version, wherever a version is named
    assert api.version(dataset, 'v0.0.1')['id'] == v1['id']
    got = ops.get(api, cache, f'{dataset}@v0.0.1', tmp_path / 'out', echo=quiet)
    assert got['ref'] == f'{dataset}@1' and (tmp_path / 'out' / 'd0' / 'x.bmp').is_file()
    # several labels on one version; asked twice is fine; a label never moves
    ops.add_label(api, f'{dataset}@2', bump='minor', echo=quiet)
    ops.add_label(api, f'{dataset}@2', 'v1.0.0', echo=quiet)
    assert api.version(dataset, 2)['labels'] == ['v0.1.0', 'v1.0.0']
    assert api.add_label(dataset, 2, 'v1.0.0')['labels'] == ['v0.1.0', 'v1.0.0']
    assert api.add_label(dataset, 'v0.1.0', 'v1.0.0')['id'] == v2['id']
    with pytest.raises(ops.ApiError, match=f'已经用在 {dataset}@2 上') as e:
        api.add_label(dataset, 3, 'v1.0.0')
    assert e.value.status == 409
    assert [v['labels'] for v in api.versions(dataset)] == [['v0.0.1'], ['v0.1.0', 'v1.0.0'], []]
    assert api.dataset(dataset)['latest']['labels'] == []
    assert status_of(lambda: api.version(dataset, 'v9.9.9')) == 404
    for bad in ('1.0.0', 'v1.0', 'v01.0.0', 'latest', 'v1.0.0 '):
        assert status_of(lambda: api.add_label(dataset, 3, bad)) == 400
    # who may: algo and admin give labels, only admin retires, field only reads
    field, admin = make_api('field'), make_api('admin')
    assert field.version(dataset, 'v1.0.0')['id'] == v2['id'] and len(field.labels(dataset)['labels']) == 3
    assert status_of(lambda: field.add_label(dataset, 3, 'v2.0.0')) == 403
    assert status_of(lambda: api.retire_label(dataset, 'v1.0.0', '起错了')) == 403
    assert status_of(lambda: admin.retire_label(dataset, 'v1.0.0', '')) == 400
    assert status_of(lambda: admin.retire_label(dataset, 'v3.0.0', '起错了')) == 404
    assert admin.retire_label(dataset, 'v1.0.0', '起错了')['id'] == v2['id']
    assert status_of(lambda: admin.retire_label(dataset, 'v1.0.0', '起错了')) == 409
    # retired: names nothing any more, and the string stays taken
    with pytest.raises(ops.ApiError, match='已撤销（起错了）') as e:
        api.version(dataset, 'v1.0.0')
    assert e.value.status == 404
    assert api.version(dataset, 2)['labels'] == ['v0.1.0']
    with pytest.raises(ops.ApiError, match='撤销的版本号不能再用'):
        api.add_label(dataset, 3, 'v1.0.0')
    rows = api.labels(dataset)
    assert [(r['label'], r['ref'], r['retired']) for r in rows['labels']] == [
        ('v1.0.0', f'{dataset}@2', True), ('v0.1.0', f'{dataset}@2', False), ('v0.0.1', f'{dataset}@1', False)]
    assert rows['labels'][0]['retired_note'] == '起错了' and rows['labels'][2]['note'] == '冷启动'
    assert rows['suggest'] == {'patch': 'v1.0.1', 'minor': 'v1.1.0', 'major': 'v2.0.0'}  # after the retired one too
    e = api.events(q=dataset, action='version')['events']
    assert [(x['action'], x['object'], x['detail'].get('label')) for x in e if 'label' in x['action']] == [
        ('version.unlabel', f'{dataset}@2', 'v1.0.0'), ('version.label', f'{dataset}@2', 'v1.0.0'),
        ('version.label', f'{dataset}@2', 'v0.1.0'), ('version.label', f'{dataset}@1', 'v0.0.1')]


def test_label_rows_cannot_be_changed(api, dataset, tmp_path, sql):
    v1, v2 = versions(api, dataset, tmp_path, 2)
    api.add_label(dataset, 1, 'v0.1.0')
    import psycopg
    for statement in ("UPDATE version_labels SET version_id = %(v2)s WHERE version_id = %(v1)s",
                      "UPDATE version_labels SET label = 'v0.2.0' WHERE version_id = %(v1)s",
                      "UPDATE version_labels SET note = 'x' WHERE version_id = %(v1)s",
                      "DELETE FROM version_labels WHERE version_id = %(v1)s",
                      "TRUNCATE version_labels",
                      "INSERT INTO version_labels (dataset_id, version_id, label, created_by) "
                      "SELECT dataset_id + 1, %(v1)s, 'v5.0.0', created_by FROM versions WHERE id = %(v1)s",
                      "INSERT INTO version_labels (dataset_id, version_id, label, created_by) "
                      "SELECT dataset_id, %(v1)s, 'v5.0', created_by FROM versions WHERE id = %(v1)s"):
        with pytest.raises(psycopg.Error):
            sql.execute(statement, {'v1': v1['id'], 'v2': v2['id']})
    assert api.version(dataset, 'v0.1.0')['id'] == v1['id']


def test_labels_of_snapshots_and_models(api, make_api, model, snapshot, tmp_path, monkeypatch, capsys):  # noqa: F811
    ts = snapshot['dataset']
    api.add_label(ts, 1, 'v0.1.0')
    assert api.trainset(ts, 'v0.1.0')['id'] == snapshot['id'] and api.trainset(ts)['labels'] == ['v0.1.0']
    # a model version is registered on the snapshot by its label, and gets a label of its own
    d = tmp_path / 'run'
    write(d, 'best.onnx', __import__('test_models').onnx_bytes(uuid.uuid4().hex))
    mv = ops.register_model(api, model, d / 'best.onnx', f'{ts}@v0.1.0', echo=quiet)
    assert mv['trainset']['ref'] == f'{ts}@1' and mv['labels'] == []
    assert api.model_labels(model)['suggest']['patch'] == 'v0.0.1'
    mv = ops.add_label(api, mv['ref'], 'v1.0.0', model=True, echo=quiet)
    assert mv['labels'] == ['v1.0.0'] and api.model_version(model, 'v1.0.0')['id'] == mv['id']
    assert api.model_versions(model)[0]['labels'] == ['v1.0.0']
    # labels of the dataset namespace and of the model namespace do not meet
    assert status_of(lambda: api.version(ts, 'v1.0.0')) == 404
    other = trained(api, model, snapshot, tmp_path)
    with pytest.raises(ops.ApiError, match=f'已经用在 {model}@1 上'):
        api.add_model_label(model, other['number'], 'v1.0.0')
    field, admin = make_api('field'), make_api('admin')
    assert status_of(lambda: field.add_model_label(model, 2, 'v1.1.0')) == 403
    assert status_of(lambda: api.retire_model_label(model, 'v1.0.0', '错了')) == 403
    # a release names the model version by its label: it is as fixed as the number
    rel = field.register_release({'model': model, 'number': 'v1.0.0', 'site': f'S{uuid.uuid4().hex[:6]}', 'line': 'L1',
                                  'deploy_repo': 'git@x:deploy.git',
                                  'deploy_commit': 'a' * 40, 'deployed_at': '2026-11-01T08:00:00+08:00'})
    assert rel['model_version']['ref'] == f'{model}@1'
    assert admin.retire_model_label(model, 'v1.0.0', '错了')['id'] == mv['id']
    assert status_of(lambda: api.model_version(model, 'v1.0.0')) == 404
    assert [(r['label'], r['retired']) for r in api.model_labels(model)['labels']] == [('v1.0.0', True)]
    acts = [x['action'] for x in api.events(q=model, action='model_version')['events']]
    assert acts.count('model_version.label') == 1 and acts.count('model_version.unlabel') == 1
    # the command line
    monkeypatch.setattr(ops, 'connect', lambda: api)
    monkeypatch.setattr(api, 'close', lambda: None)
    assert fw_cli.main(['label', 'add', f'{model}@2', '--bump', 'minor', '--model', '-m', '第二版']) == 0
    assert f'{model}@2 的版本号是 v1.1.0' in capsys.readouterr().out
    assert fw_cli.main(['label', 'list', model, '--model']) == 0
    out = capsys.readouterr().out
    assert 'v1.1.0' in out and '已撤销：错了' in out and 'v1.1.1' in out
    assert fw_cli.main(['label', 'add', f'{ts}@1']) == 1
    assert '二选一' in capsys.readouterr().err
    assert fw_cli.main(['model', 'versions', model]) == 0
    assert 'v1.1.0' in capsys.readouterr().out
    assert fw_cli.main(['trainset', 'list']) == 0
    assert '@1 v0.1.0' in capsys.readouterr().out


def test_a_batch_revision_takes_no_label(api, dataset, tmp_path):
    write(tmp_path / 'b', 'x.bmp', f'batch label {uuid.uuid4().hex}'.encode())
    name = f'B-{uuid.uuid4().hex[:8]}'
    ops.upload_batch(api, tmp_path / 'b', dataset, '张三', name=name, echo=quiet)
    with pytest.raises(ops.ApiError, match='不起版本号') as e:
        api.add_label(name, 1, 'v0.1.0')
    assert e.value.status == 400


def test_a_snapshot_from_a_labelled_pool_version(api, tmp_path):
    src = pool(api, tmp_path, {**sample_files('2026-09', 8), **sample_files('2026-10', 3)})
    api.add_label(src, 1, 'v0.1.0')
    ts = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@v0.1.0'], test_dirs=['2026-10'])
    assert api.trainset(ts['dataset'])['trainset']['sources'] == [f'{src}@1']
