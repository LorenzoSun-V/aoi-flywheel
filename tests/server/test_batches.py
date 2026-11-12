"""Labelling batches: upload as frozen revisions, review, and import into the pool."""
import json
import uuid

import pytest

from helpers import quiet, read_tree, write
from fw import ops
from fw.api import ApiError
from fw.contract import parse_manifest
from fwserver import batches as server_batches


def label(*boxes):
    return json.dumps({'version': '2.3.6', 'shapes': [
        {'label': n, 'shape_type': 'rectangle', 'points': [[x, y], [x + 10, y + 10]]} for n, x, y in boxes]}).encode()


def lines():
    out = []
    return out, out.append


@pytest.fixture
def pool(api, dataset, tmp_path):
    write(tmp_path / 'v1', '20261101/a.bmp', b'batch A')
    write(tmp_path / 'v1', '20261101/a.json', label(('ng', 1, 1)))
    ops.import_folder(api, dataset, tmp_path / 'v1', echo=quiet)
    return dataset


@pytest.fixture
def day(tmp_path):
    """A folder of two images, one labelled."""
    folder = tmp_path / '20261104'
    write(folder, 'c.bmp', b'batch C')
    write(folder, 'c.json', label(('ng', 5, 5)))
    write(folder, 'd.bmp', b'batch D')
    return folder


@pytest.fixture
def field(make_api):
    return make_api('field')


def new_name():
    return f'B-{uuid.uuid4().hex[:8]}'


def upload(api, pool, day, **kw):
    return ops.upload_batch(api, day, pool, kw.pop('labeler', '张三'), name=kw.pop('name', new_name()), echo=quiet, **kw)


def test_field_uploads_a_batch_that_stays_out_of_the_pool(api, field, pool, day, tokens):
    out, echo = lines()
    b = ops.upload_batch(field, day, pool, '张三', name=new_name(), echo=echo)
    assert b['status'] == 'open' and b['revision'] == 1 and b['dir'] == '20261104' and b['pool'] == pool
    assert b['owner'] == tokens['field_name'] and b['labeler'] == '张三' and b['file_count'] == 3
    assert b['actions'] == ['revise', 'submit', 'close']
    assert any('还没有提交复核' in x for x in out)
    # the pool is untouched and the batch is not listed as a dataset
    assert api.dataset(pool)['latest']['number'] == 1
    assert b['name'] not in [d['name'] for d in api.datasets()]
    # a revision is a frozen version: it reads like any version
    v = api.version(b['name'], 1)
    assert v['kind'] == 'batch' and v['file_count'] == 3
    assert {p for p, _, _ in parse_manifest(api.read_object(v['manifest_sha256']))} == {'c.bmp', 'c.json', 'd.bmp'}
    assert [s['step'] for s in b['steps']] == ['upload']
    assert [x['name'] for x in field.batches(mine=True)['batches']] == [b['name']]
    assert b['name'] not in [x['name'] for x in api.batches(mine=True)['batches']]


def test_upload_needs_labeler_a_pool_and_images(api, field, pool, day, tmp_path):
    with pytest.raises(ops.FwError, match='标注人'):
        ops.upload_batch(field, day, pool, '  ', echo=quiet)
    with pytest.raises(ApiError, match='没有数据集'):
        ops.upload_batch(field, day, 'no-such-pool', '张三', echo=quiet)
    write(tmp_path / 'empty', 'readme.txt', b'x')
    with pytest.raises(ops.FwError):
        ops.upload_batch(field, tmp_path / 'empty', pool, '张三', echo=quiet)
    with pytest.raises(ops.FwError, match='批次名称'):
        ops.upload_batch(field, day, pool, '张三', name='批次', echo=quiet)
    # the labeler is checked by the server too
    digest, _, _ = ops._send_folder(field, day, False, False, 2, quiet)
    with pytest.raises(ApiError, match='标注人') as e:
        field.create_batch({'name': new_name(), 'pool': pool, 'dir': 'x', 'manifest_sha256': digest, 'labeler': ' '})
    assert e.value.status == 400


def test_two_unfinished_batches_cannot_aim_at_one_folder(field, pool, day):
    b = upload(field, pool, day)
    with pytest.raises(ApiError, match='还没有结束') as e:
        upload(field, pool, day, to='20261104')
    assert e.value.status == 409
    field.batch_step(b['name'], 'close', {'comment': '重拍'})
    upload(field, pool, day)


def test_batch_names_share_the_dataset_namespace(api, field, pool, day):
    with pytest.raises(ApiError, match='被别人建了|已经存在') as e:
        upload(field, pool, day, name=pool)
    assert e.value.status == 409


def test_notice_when_the_pool_has_the_folder(field, pool, day):
    b = upload(field, pool, day, to='20261101')
    assert b['notices'] and '20261101/' in b['notices'][0]


def test_review_round_trip(api, field, make_api, pool, day, tokens, cache, tmp_path):
    b = upload(field, pool, day)
    name = b['name']
    # only an account that may review can be asked
    with pytest.raises(ApiError, match='不能当复核人'):
        field.batch_step(name, 'submit', {'reviewer': tokens['deploy_name']})
    with pytest.raises(ApiError, match='要写复核人'):
        field.batch_step(name, 'submit', {})
    b = field.batch_step(name, 'submit', {'reviewer': tokens['algo_name']})
    assert b['status'] == 'review' and b['reviewer'] == tokens['algo_name'] and b['actions'] == ['close']
    assert [x['name'] for x in api.batches(todo=True)['batches']] == [name]
    assert api.batch(name)['actions'] == ['approve', 'reject', 'close']
    # while it is in review the owner cannot change it
    write(day, 'd.json', label(('ng', 7, 7)))
    with pytest.raises(ops.FwError, match='不能上传新的一版'):
        ops.update_batch(field, name, day, '张三', echo=quiet)
    # the reviewer fetches it like any version
    ops.get(api, cache, f'{name}@1', tmp_path / 'look', echo=quiet)
    assert set(read_tree(tmp_path / 'look')) == {'c.bmp', 'c.json', 'd.bmp'}
    # reject: a comment is required, images must exist in the revision
    with pytest.raises(ApiError, match='退回意见'):
        api.batch_step(name, 'reject', {'comment': ' '})
    with pytest.raises(ApiError, match='没有这些文件'):
        api.batch_step(name, 'reject', {'comment': '漏标', 'images': ['zz.bmp']})
    b = api.batch_step(name, 'reject', {'comment': 'd.bmp 漏标', 'images': ['d.bmp'], 'revision': 1})
    assert b['status'] == 'rejected'
    # a rejected batch is not submitted again as it is
    with pytest.raises(ApiError, match='先上传改好的一版'):
        field.batch_step(name, 'submit', {})
    out, echo = lines()
    b = ops.update_batch(field, name, day, '李四', reviewer='-', note='补了 d', echo=echo)
    assert b['revision'] == 2 and b['status'] == 'review' and b['reviewer'] == tokens['algo_name']
    assert b['labeler'] == '李四'
    assert any('标注有变化的图片 1 张：加框 1' in x for x in out)
    b = api.batch_step(name, 'approve', {'comment': '可以', 'revision': 2})
    assert b['status'] == 'approved' and b['actions'] == ['reject', 'import', 'close']
    steps = [(s['step'], s['actor'], s['revision']) for s in b['steps']]
    f, a = tokens['field_name'], tokens['algo_name']
    assert steps == [('upload', f, 1), ('submit', f, 1), ('reject', a, 1), ('upload', f, 2), ('submit', f, 2),
                     ('approve', a, 2)]
    assert b['steps'][2]['comment'] == 'd.bmp 漏标' and b['steps'][2]['images'] == ['d.bmp']
    assert [(r['number'], r['labeler'], r['created_by']) for r in b['revisions']] == [(1, '张三', f), (2, '李四', f)]


def test_same_content_is_not_a_new_revision(field, pool, day):
    b = upload(field, pool, day)
    with pytest.raises(ApiError, match='完全一样') as e:
        ops.update_batch(field, b['name'], day, '张三', echo=quiet)
    assert e.value.status == 409
    assert field.batch(b['name'])['revision'] == 1


def test_only_owner_and_asked_reviewer_act(api, field, make_api, pool, day, tokens, sql):
    other_field = make_api('deploy')
    admin = make_api('admin')
    b = upload(field, pool, day)
    name = b['name']
    write(day, 'd.json', label(('ng', 7, 7)))
    with pytest.raises(ops.FwError, match='不是你的批次'):
        ops.update_batch(other_field, name, day, '王五', echo=quiet)
    with pytest.raises(ApiError, match='不是你的批次') as e:
        other_field.batch_step(name, 'submit', {'reviewer': tokens['algo_name']})
    assert e.value.status == 403
    with pytest.raises(ApiError) as e:
        other_field.batch_step(name, 'close', {})
    assert e.value.status == 403
    # field accounts cannot review at all
    for step in ('reject', 'approve'):
        with pytest.raises(ApiError, match='不能复核') as e:
            field.batch_step(name, step, {'comment': 'x'})
        assert e.value.status == 403
    field.batch_step(name, 'submit', {'reviewer': tokens['admin_name']})
    # an algo account that was not asked cannot answer; the administrator who was asked can
    with pytest.raises(ApiError, match='复核人不是你') as e:
        api.batch_step(name, 'approve', {})
    assert e.value.status == 403
    assert admin.batch_step(name, 'approve', {})['status'] == 'approved'
    # field cannot import
    with pytest.raises(ApiError) as e:
        field.batch_step(name, 'import/preview', {})
    assert e.value.status == 403


def test_stale_revision_is_refused(api, field, pool, day, tokens):
    b = upload(field, pool, day, reviewer=tokens['algo_name'])
    assert b['status'] == 'review'
    with pytest.raises(ApiError, match='最新是第 1 版') as e:
        api.batch_step(b['name'], 'approve', {'revision': 2})
    assert e.value.status == 409


def test_self_review_is_recorded(api, pool, day, tokens):
    b = upload(api, pool, day, reviewer=tokens['algo_name'])
    submit = [s for s in b['steps'] if s['step'] == 'submit'][0]
    assert submit['self_review'] is True and submit['reviewer'] == tokens['algo_name']
    assert api.batch_step(b['name'], 'approve', {})['status'] == 'approved'


def test_reviewer_approves_with_own_changes(api, field, pool, day, tokens, tmp_path):
    b = upload(field, pool, day, reviewer=tokens['algo_name'])
    name = b['name']
    fixed = tmp_path / 'fixed'
    write(fixed, 'c.bmp', b'batch C')
    write(fixed, 'c.json', label(('ok', 5, 5)))
    write(fixed, 'd.bmp', b'batch D')
    write(fixed, 'd.json', label(('ng', 1, 1)))
    digest, _, _ = ops._send_folder(api, fixed, False, False, 2, quiet)
    with pytest.raises(ApiError, match='标注人'):
        api.batch_step(name, 'approve', {'manifest_sha256': digest, 'revision': 1})
    b = api.batch_step(name, 'approve', {'manifest_sha256': digest, 'labeler': '赵工', 'revision': 1})
    assert b['status'] == 'approved' and b['revision'] == 2
    r = b['revisions'][1]
    assert r['by_reviewer'] is True and r['labeler'] == '赵工' and r['created_by'] == tokens['algo_name']
    assert [(s['step'], s['revision']) for s in b['steps']][-2:] == [('upload', 2), ('approve', 2)]
    c = api.compare_batch(name)
    assert c['boxes'] == {'added': 1, 'removed': 0, 'moved': 0, 'relabeled': 1}
    assert [(x['image'], x['added'], x['relabeled']) for x in c['images']['examples']] == [
        ('c.bmp', 0, 1), ('d.bmp', 1, 0)]


def test_import_adds_the_folder_to_the_pool(api, field, pool, day, tokens, cache, tmp_path):
    b = upload(field, pool, day, reviewer=tokens['algo_name'])
    name = b['name']
    with pytest.raises(ApiError, match='复核通过以后才能入库') as e:
        api.batch_step(name, 'import/preview', {})
    assert e.value.status == 409
    api.batch_step(name, 'approve', {})
    out, echo = lines()
    assert ops.import_batch(api, name, dry_run=True, echo=echo) is None
    assert api.dataset(pool)['latest']['number'] == 1
    v = ops.import_batch(api, name, echo=echo)
    assert v['ref'] == f'{pool}@2' and v['file_count'] == 5 and v['source'] == f'batch:{name}@1'
    assert any('新增 20261104/：3 个文件' in x for x in out)
    assert [(o['op'], o['dir']) for o in api.version(pool, 2)['ops']] == [('add', '20261104')]
    b = api.batch(name)
    assert b['status'] == 'imported' and b['imported'] == f'{pool}@2' and b['actions'] == []
    assert b['steps'][-1]['step'] == 'import' and b['steps'][-1]['pool_version'] == f'{pool}@2'
    ops.get(api, cache, f'{pool}@2', tmp_path / 'out', echo=quiet)
    assert set(read_tree(tmp_path / 'out')) == {'20261101/a.bmp', '20261101/a.json', '20261104/c.bmp',
                                                 '20261104/c.json', '20261104/d.bmp'}
    # the end: nothing more can be done with it
    for step, body in (('import', {'expect_latest_id': v['id']}), ('close', {}), ('reject', {'comment': 'x'})):
        with pytest.raises(ApiError) as e:
            api.batch_step(name, step, body)
        assert e.value.status == 409


def test_import_refuses_an_existing_folder_unless_replaced(api, field, pool, day, tokens):
    b = upload(field, pool, day, to='20261101', reviewer=tokens['algo_name'])
    api.batch_step(b['name'], 'approve', {})
    with pytest.raises(ops.FwError, match='池里已有 20261101/'):
        ops.import_batch(api, b['name'], echo=quiet)
    assert api.batch(b['name'])['status'] == 'approved'
    v = ops.import_batch(api, b['name'], replace=True, echo=quiet)
    assert [(o['op'], o['dir']) for o in api.version(pool, v['number'])['ops']] == [('replace', '20261101')]


def test_import_fails_whole_when_the_freeze_fails(api, field, pool, day, tokens, service):
    b = upload(field, pool, day, reviewer=tokens['algo_name'])
    api.batch_step(b['name'], 'approve', {})
    preview = api.batch_step(b['name'], 'import/preview', {})

    def boom():
        raise RuntimeError('boom')
    service.hooks['before_freeze'] = boom
    try:
        with pytest.raises(ApiError):
            api.batch_step(b['name'], 'import', {'expect_latest_id': preview['expect_latest_id']})
    finally:
        service.hooks.clear()
    assert api.batch(b['name'])['status'] == 'approved' and api.dataset(pool)['latest']['number'] == 1


def test_approved_batch_can_still_be_rejected(api, field, pool, day, tokens):
    b = upload(field, pool, day, reviewer=tokens['algo_name'])
    api.batch_step(b['name'], 'approve', {})
    assert api.batch_step(b['name'], 'reject', {'comment': '再看发现错标'})['status'] == 'rejected'
    with pytest.raises(ApiError, match='复核通过以后才能入库'):
        api.batch_step(b['name'], 'import/preview', {})


def test_batches_are_refused_where_pools_are_meant(api, field, pool, day, tmp_path):
    b = upload(field, pool, day)
    name = b['name']
    with pytest.raises(ApiError, match='是标注批次'):
        ops.add_folders(api, name, [day], to='x', echo=quiet)
    write(day, 'z.bmp', b'batch Z')
    with pytest.raises((ApiError, ops.FwError), match='标注批次'):
        ops.import_folder(api, name, day, echo=quiet)
    with pytest.raises(ApiError, match='标注批次'):
        api.preview_trainset('TS-' + uuid.uuid4().hex[:6], {'sources': [f'{name}@1'], 'classes': ['ng']})
    with pytest.raises(ApiError, match='批次要进的是数据池'):
        upload(field, name, day, to='zz')


def test_database_guards(field, pool, day, sql):
    b = upload(field, pool, day)
    import psycopg
    for statement in ('DELETE FROM batches WHERE id = %s', "UPDATE batches SET dir = 'x' WHERE id = %s",
                      'DELETE FROM batch_steps WHERE batch_id = %s', "UPDATE batch_steps SET comment = 'x' WHERE batch_id = %s",
                      'DELETE FROM batch_revisions WHERE batch_id = %s'):
        with pytest.raises(psycopg.errors.RaiseException):
            sql.execute(statement, (b['id'],))
    field.batch_step(b['name'], 'close', {})
    with pytest.raises(psycopg.errors.RaiseException):
        sql.execute("UPDATE batches SET status = 'open' WHERE id = %s", (b['id'],))


def test_every_step_is_an_event(api, field, pool, day, tokens):
    b = upload(field, pool, day, reviewer=tokens['algo_name'])
    name = b['name']
    api.batch_step(name, 'reject', {'comment': '不行'})
    write(day, 'd.json', label(('ng', 7, 7)))
    ops.update_batch(field, name, day, '张三', reviewer='-', echo=quiet)
    api.batch_step(name, 'approve', {})
    ops.import_batch(api, name, echo=quiet)
    got = [(e['action'], e['object'], e['actor']) for e in api.events(q=name, limit=50)['events']][::-1]
    f, a = tokens['field_name'], tokens['algo_name']
    assert [g for g in got if g[0].startswith('batch.')] == [
        ('batch.upload', f'{name}@1', f), ('batch.submit', f'{name}@1', f), ('batch.reject', f'{name}@1', a),
        ('batch.upload', f'{name}@2', f), ('batch.submit', f'{name}@2', f), ('batch.approve', f'{name}@2', a),
        ('batch.import', f'{name}@2', a)]


def test_box_changes():
    box = server_batches.box_changes
    a = ('ng', 0.0, 0.0, 10.0, 10.0)
    assert box([a], [a]) == {'added': 0, 'removed': 0, 'moved': 0, 'relabeled': 0}
    assert box([a], [('ng', 1.0, 1.0, 11.0, 11.0)]) == {'added': 0, 'removed': 0, 'moved': 1, 'relabeled': 0}
    assert box([a], [('ok', 0.0, 0.0, 10.0, 10.0)]) == {'added': 0, 'removed': 0, 'moved': 0, 'relabeled': 1}
    assert box([a], [('ng', 50.0, 50.0, 60.0, 60.0)]) == {'added': 1, 'removed': 1, 'moved': 0, 'relabeled': 0}
    assert box([a, a], [a]) == {'added': 0, 'removed': 1, 'moved': 0, 'relabeled': 0}
    assert box([], [a, a]) == {'added': 2, 'removed': 0, 'moved': 0, 'relabeled': 0}


def test_compare_ignores_reformatted_labels(field, pool, day):
    b = upload(field, pool, day)
    doc = json.loads(label(('ng', 5, 5)))
    doc['flags'] = {}
    write(day, 'c.json', json.dumps(doc, indent=2).encode())
    write(day, 'e.bmp', b'batch E')
    ops.update_batch(field, b['name'], day, '张三', echo=quiet)
    c = field.compare_batch(b['name'])
    assert c['files']['modified']['count'] == 1 and c['files']['added']['count'] == 1
    assert c['images']['changed'] == 0 and c['images']['added'] == 1
    with pytest.raises(ApiError, match='两个不同的版次'):
        field.compare_batch(b['name'], old=1, new=1)


def test_cli_from_upload_to_import(api, pool, day, tokens, tmp_path, monkeypatch, capsys):
    from fw import cli as fw_cli
    monkeypatch.setattr(ops, 'connect', lambda: api)
    monkeypatch.setattr(api, 'close', lambda: None)
    monkeypatch.setenv('FW_CACHE', str(tmp_path / 'cache'))
    name = new_name()
    me = tokens['algo_name']
    assert fw_cli.main(['batch', 'upload', str(day), '--pool', pool, '--labeler', '张三', '--name', name]) == 0
    assert fw_cli.main(['batch', 'submit', name, '--reviewer', me]) == 0
    assert fw_cli.main(['batch', 'list', '--todo']) == 0
    assert fw_cli.main(['batch', 'get', name, '--to', str(tmp_path / 'got')]) == 0
    assert set(read_tree(tmp_path / 'got')) == {'c.bmp', 'c.json', 'd.bmp'}
    assert fw_cli.main(['batch', 'reject', name, '-m', '漏标 d', '--image', 'd.bmp']) == 0
    write(day, 'd.json', label(('ng', 7, 7)))
    assert fw_cli.main(['batch', 'update', name, str(day), '--labeler', '张三', '--reviewer', '-']) == 0
    assert fw_cli.main(['batch', 'diff', name]) == 0
    fixed = tmp_path / 'fixed'
    for rel, data in read_tree(day).items():
        write(fixed, rel, data)
    write(fixed, 'c.json', label(('ok', 5, 5)))
    assert fw_cli.main(['batch', 'approve', name, '--with', str(fixed)]) == 1   # who changed it must be said
    assert fw_cli.main(['batch', 'approve', name, '--with', str(fixed), '--labeler', '赵工', '-m', '改了 c']) == 0
    assert fw_cli.main(['batch', 'import', name, '--dry-run']) == 0
    assert fw_cli.main(['batch', 'import', name, '--yes']) == 0
    assert fw_cli.main(['batch', 'show', name]) == 0
    assert fw_cli.main(['batch', 'close', name]) == 1
    out = capsys.readouterr().out
    assert f'已提交复核：{name} 第 1 版，复核人 {me}' in out and '（自审）' in out
    assert '问题图片 d.bmp' in out and '加框 1' in out and '赵工（复核人修改）' in out
    assert f'已冻结 {pool}@2' in out and f'成为 {pool}@2' in out
    assert api.batch(name)['status'] == 'imported'
