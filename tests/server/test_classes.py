"""The class table of a data pool: labels that go into a pool may only name its classes."""
import json
import re
import uuid
from unittest import mock

import psycopg
import pytest

from helpers import quiet, write
from fw import ops
from fw.api import ApiError
from fwserver import datasets as server_datasets
from fwserver import formats
from test_trainsets import old_pool, status_of

CLASSES = ['scratch', 'dent']


def label(*names):
    return json.dumps({'version': '2.3.6', 'shapes': [
        {'label': n, 'shape_type': 'rectangle', 'points': [[1, 1], [9, 9]]} for n in names]}).encode()


def new_pool(api, classes=CLASSES):
    name = f'C-{uuid.uuid4().hex[:8]}'
    api.create_dataset(name, 'SES', 'TX', 'pool', '', classes=classes)
    return name


def folder(tmp_path, files):
    root = tmp_path / f'src-{uuid.uuid4().hex[:6]}'
    for rel, data in files.items():
        write(root, rel, data)
    return root


def image():
    return uuid.uuid4().bytes  # content no other test has


# --- uploads ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize('files, message', [
    ({'a.json': label('scratch', 'crack')}, 'crack（1 个文件，例如 a.json）'),
    ({'a.json': label('Scratch')}, 'Scratch（1 个文件，例如 a.json，类别表里是 scratch）'),
    ({'a.txt': b'0 0.5 0.5 0.1 0.1\n5 0.5 0.5 0.1 0.1\n'}, '编号 5（1 个文件，例如 a.txt）'),
    ({'a.txt': b'x 0.5 0.5 0.1 0.1\n'}, 'a.txt（第 1 行的类别编号不是整数）'),
    ({'a.json': b'{not json'}, 'a.json（不是合法的 JSON'),
    ({'a.json': b'{"shapes": [{"points": []}]}'}, 'a.json（有形状没有类别名）'),
    ({'a.json': b'[]'}, '没有 shapes 列表'),
    ({'a.json': '{"shapes": []}'.encode('utf-16')}, 'a.json（不是 UTF-8 文本）'),
    ({'a.json': label('dent'), 'classes.txt': b'dent\nscratch\n'}, 'classes.txt（第 1 行是 dent，数据池类别表的第 1 个是 scratch）'),
    ({'a.json': label('dent'), 'classes.txt': b'scratch\ndent\ncrack\n'}, '有 3 个类别，数据池的类别表只有 2 个'),
])
def test_a_pool_refuses_labels_outside_its_class_table(api, tmp_path, files, message):
    name = new_pool(api)
    root = folder(tmp_path, dict(files, **{'a.png': image()}))
    with pytest.raises(ApiError) as e:
        ops.import_folder(api, name, root, echo=quiet)
    assert e.value.status == 400 and message in e.value.message, e.value.message
    assert f'不符合数据池 {name} 的类别表（2 类：scratch、dent）' in e.value.message
    assert api.dataset(name)['latest'] is None


def test_labels_of_the_class_table_go_in(api, tmp_path):
    name = new_pool(api)
    root = folder(tmp_path, {'a.png': image(), 'a.json': label('scratch', 'dent', 'scratch'), 'b.png': image(),
                             'b.json': b'{"shapes": []}', 'images/c.png': image(), 'labels/c.txt': b'1 0.5 0.5 0.2 0.2\n\n',
                             'd.png': image(), 'classes.txt': b'scratch\n'})  # the beginning of the table
    v = ops.import_folder(api, name, root, echo=quiet)
    assert v['number'] == 1 and api.dataset(name)['classes'] == CLASSES


def test_a_pool_without_a_class_table_takes_no_upload(api, make_api, tmp_path):
    name = f'C-{uuid.uuid4().hex[:8]}'
    api.create_dataset(name, 'SES', 'TX', 'pool', '')  # as an fw from before the tables still can
    assert api.dataset(name)['classes'] == []
    root = folder(tmp_path, {'a.png': image(), 'a.json': label('scratch')})
    with pytest.raises(ApiError, match=f'数据池 {name} 尚无类别表；请先在网页的数据池页面填写类别表') as e:
        ops.import_folder(api, name, root, echo=quiet)
    assert e.value.status == 400
    with pytest.raises(ApiError, match='尚无类别表'):
        ops.upload_batch(make_api('field'), root, name, '张三', name=f'B-{uuid.uuid4().hex[:8]}', echo=quiet)
    api.add_classes(name, CLASSES)
    assert ops.import_folder(api, name, root, echo=quiet)['number'] == 1


def test_other_datasets_have_no_class_table(api, tmp_path):
    name = f'C-{uuid.uuid4().hex[:8]}'
    assert status_of(lambda: api.create_dataset(name, '', '', 'test', '', classes=CLASSES)) == 400
    api.create_dataset(name, '', '', 'test', '')
    with pytest.raises(ApiError, match='不是数据池；只有数据池有类别表'):
        api.add_classes(name, CLASSES)
    root = folder(tmp_path, {'a.png': image(), 'a.json': label('anything')})
    # a test set follows the table of a pool (test_a_test_set_follows_the_class_table_of_a_pool); without one
    # it takes no upload
    with pytest.raises(ApiError, match='尚未指定沿用哪个数据池的类别表'):
        ops.import_folder(api, name, root, echo=quiet)
    regression = f'C-{uuid.uuid4().hex[:8]}'
    api.create_dataset(regression, '', '', 'regression', '')
    assert ops.import_folder(api, regression, root, echo=quiet)['number'] == 1  # a regression set is labelled as it is


def test_added_folders_are_checked_and_the_preview_says_why(api, tmp_path):
    name = new_pool(api)
    ops.import_folder(api, name, folder(tmp_path, {'a/1.png': image(), 'a/1.json': label('scratch')}), echo=quiet)
    day = folder(tmp_path, {'2.png': image(), '2.json': label('crack'), '3.png': image(), '3.json': label('crack')})
    with pytest.raises(ApiError) as e:  # the preview had not the labels yet: refused when the version is made
        ops.add_folders(api, name, [day], to='b', echo=quiet)
    assert e.value.status == 400 and 'crack（2 个文件，例如 b/2.json、b/3.json）' in e.value.message
    digest, _, _ = ops._send_folder(api, day, False, False, 2, quiet)
    preview = api.preview_change(name, {'add': [{'dir': 'b', 'manifest_sha256': digest}], 'remove': []})
    assert any('类别表以外的类别：crack' in r for r in preview['reasons']), preview['reasons']
    assert api.dataset(name)['latest']['number'] == 1
    api.add_classes(name, ['crack'])  # a new class indeed: at the end of the table
    assert ops.add_folders(api, name, [day], to='b', echo=quiet)['number'] == 2


def test_batches_are_checked_against_the_pool_they_are_for(api, make_api, tokens, tmp_path):
    field = make_api('field')
    name = new_pool(api)
    ops.import_folder(api, name, folder(tmp_path, {'a/1.png': image(), 'a/1.json': label('scratch')}), echo=quiet)
    bad = folder(tmp_path, {'2.png': image(), '2.json': label('Dent')})
    good = folder(tmp_path, {'2.png': image(), '2.json': label('dent')})
    batch = f'B-{uuid.uuid4().hex[:8]}'
    with pytest.raises(ApiError, match='没有上传批次：不符合数据池 .*：Dent（1 个文件，例如 2.json，类别表里是 dent）') as e:
        ops.upload_batch(field, bad, name, '张三', name=batch, echo=quiet)
    assert e.value.status == 400 and status_of(lambda: api.batch(batch)) == 404
    ops.upload_batch(field, good, name, '张三', name=batch, echo=quiet)
    with pytest.raises(ApiError, match='没有上传批次：.*Dent'):  # the owner's next revision
        ops.update_batch(field, batch, bad, '张三', echo=quiet)
    field.batch_step(batch, 'submit', {'reviewer': tokens['algo_name']})
    digest, _, _ = ops._send_folder(api, bad, False, False, 2, quiet)  # the reviewer's own corrections
    with pytest.raises(ApiError, match='没有上传批次：.*Dent'):
        api.batch_step(batch, 'approve', {'manifest_sha256': digest, 'labeler': '赵工', 'revision': 1})
    api.batch_step(batch, 'approve', {})
    assert ops.import_batch(api, batch, echo=quiet)['number'] == 2


def test_a_checked_file_is_read_again_as_another_kind_of_file(api, tmp_path):
    """Contents checked as one kind of file count as checked for that kind only (review of #41): the bytes of
    a YOLO label already in the pool are no JSON label, and those of a JSON label are no YOLO label."""
    name = new_pool(api)
    yolo, doc = b'0 0.5 0.5 0.1 0.1\n', label('dent', 'scratch')
    ops.import_folder(api, name, folder(tmp_path, {'A/x.png': image(), 'A/x.txt': yolo,
                                                   'A/y.png': image(), 'A/y.json': doc}), echo=quiet)
    for files, message in (({'b.png': image(), 'b.json': yolo}, 'B/b.json（不是合法的 JSON'),
                           ({'b.png': image(), 'b.txt': doc}, 'B/b.txt（第 1 行的类别编号不是整数）')):
        with pytest.raises(ops.FwError, match=re.escape(message)):  # said in the preview
            ops.add_folders(api, name, [folder(tmp_path, files)], to='B', echo=quiet)
        whole = folder(tmp_path, {'A/x.png': image(), 'A/x.txt': yolo, **{f'B/{k}': v for k, v in files.items()}})
        with pytest.raises(ApiError, match=re.escape(message)):  # and by a full upload
            ops.import_folder(api, name, whole, echo=quiet)
    assert api.dataset(name)['latest']['number'] == 1


# --- the table ------------------------------------------------------------------------------------------
def test_classes_are_only_added_at_the_end_of_the_table(api, make_api, tokens):
    name = new_pool(api)
    out = api.add_classes(name, ['crack', '异物'])
    assert out['added'] == ['crack', '异物'] and out['latest_check'] is None
    assert out['dataset']['classes'] == api.dataset(name)['classes'] == ['scratch', 'dent', 'crack', '异物']
    for names, message in ((['Crack'], '类别表中已有 crack，不能再添加 Crack'), (['a', 'A'], 'a 和 A 重复'),
                           ([], '请填写类别'), ([' a'], '首尾空格'), (['a\tb'], '制表符'), (['x' * 129], '最多 128'),
                           ([1], '类别名不能为空'), ([f'c{i}' for i in range(997)], '最多 1000 个')):
        with pytest.raises(ApiError, match=message) as e:
            api.add_classes(name, names)
        assert e.value.status == 400
    assert api.dataset(name)['classes'] == ['scratch', 'dent', 'crack', '异物']
    assert status_of(lambda: api.add_classes(f'no-{uuid.uuid4().hex[:6]}', ['a'])) == 404
    for role in ('field', 'deploy'):
        assert status_of(lambda: make_api(role).add_classes(name, ['a'])) == 403
        assert status_of(lambda: make_api(role).create_dataset(f'C-{uuid.uuid4().hex[:8]}', 'SES', 'TX', 'pool', '',
                                                               classes=CLASSES)) == 403
    e = api.events(limit=1, action='dataset.classes', q=name)['events'][0]
    assert e['actor'] == tokens['algo_name'] and e['detail'] == {'added': ['crack', '异物'], 'count': 4}
    e = api.events(limit=1, action='dataset.create', q=name)['events'][0]
    assert e['detail']['classes'] == CLASSES


def test_the_database_keeps_a_class_table_growing_at_its_end(api, sql):
    name = new_pool(api)
    d = sql.execute('SELECT id, created_by FROM datasets WHERE name = %s', (name,)).fetchone()
    for statement in ("UPDATE dataset_classes SET name = 'x' WHERE dataset_id = %s",
                      'DELETE FROM dataset_classes WHERE dataset_id = %s'):
        with pytest.raises(psycopg.errors.RaiseException, match='only grows at its end'):
            sql.execute(statement, (d['id'],))
    with pytest.raises(psycopg.errors.RaiseException):
        sql.execute('TRUNCATE dataset_classes')
    with pytest.raises(psycopg.errors.RaiseException, match='not added at the end'):
        sql.execute('INSERT INTO dataset_classes (dataset_id, position, name, created_by) VALUES (%s, 5, %s, %s)',
                    (d['id'], 'gap', d['created_by']))
    with pytest.raises(psycopg.errors.UniqueViolation):
        sql.execute('INSERT INTO dataset_classes (dataset_id, position, name, created_by) VALUES (%s, 2, %s, %s)',
                    (d['id'], 'DENT', d['created_by']))
    other = f'C-{uuid.uuid4().hex[:8]}'
    api.create_dataset(other, '', '', 'test', '')
    t = sql.execute('SELECT id FROM datasets WHERE name = %s', (other,)).fetchone()['id']
    with pytest.raises(psycopg.errors.RaiseException, match='is not a data pool'):
        sql.execute('INSERT INTO dataset_classes (dataset_id, position, name, created_by) VALUES (%s, 0, %s, %s)',
                    (t, 'a', d['created_by']))
    assert api.dataset(name)['classes'] == CLASSES


# --- pools from before the tables ---------------------------------------------------------------------------
def test_the_first_table_of_an_old_pool_is_checked_on_its_newest_version(api, tmp_path):
    name = old_pool(api, tmp_path, {'A/1.png': image(), 'A/1.json': label('scratch'),
                                    'B/2.png': image(), 'B/2.json': label('Scratch', 'dirt')})
    out = api.add_classes(name, CLASSES)
    check = out['latest_check']
    assert check['ref'] == f'{name}@1'
    assert 'Scratch（1 个文件，例如 B/2.json，类别表里是 scratch）' in check['problem'] and 'dirt（1 个文件' in check['problem']
    assert api.add_classes(name, ['crack'])['latest_check'] is None  # only the first table is checked so
    # until a version is made against the table, the next one is checked in full: B/ is in the way of C/, and
    # the preview says so (B/'s labels are on the server)
    day = folder(tmp_path, {'3.png': image(), '3.json': label('dent')})
    with pytest.raises(ops.FwError, match='没有入库：\n  不符合数据池 .*B/2.json'):
        ops.add_folders(api, name, [day], to='C', echo=quiet)
    assert ops.remove_dirs(api, name, ['B'], echo=quiet)['number'] == 2
    reads = []
    real = formats.label_classes

    def counted(kind, data, classes):
        reads.append(data)
        return real(kind, data, classes)

    with mock.patch.object(server_datasets.formats, 'label_classes', counted):
        assert ops.add_folders(api, name, [day], to='C', echo=quiet)['number'] == 3
    assert reads and set(reads) == {label('dent')}  # @2 was checked: A/1.json is not read again


def test_an_old_pool_whose_labels_fit_is_said_to_fit(api, tmp_path):
    name = old_pool(api, tmp_path, {'A/1.png': image(), 'A/1.json': label('dent')})
    assert api.add_classes(name, CLASSES)['latest_check'] == {'ref': f'{name}@1', 'problem': None, 'checked': 1,
                                                              'total': 1, 'complete': True}
    empty = f'C-{uuid.uuid4().hex[:8]}'
    api.create_dataset(empty, 'SES', 'TX', 'pool', '')
    assert api.add_classes(empty, CLASSES)['latest_check'] is None  # no version to check


def test_saving_the_first_table_of_a_large_pool_checks_only_part_of_it(api, tmp_path, monkeypatch):
    """Saving the table answers in time however large the pool (review of #41): its newest version is looked
    at only so far, the answer says how far, and the next upload checks all of it."""
    files = {f'A/{i}.png': image() for i in range(4)}
    files.update({f'A/{i}.json': label('dent') for i in range(3)})
    files['A/3.json'] = label('dirt')  # past what is looked at
    name = old_pool(api, tmp_path, files)
    monkeypatch.setattr(server_datasets, 'LATEST_CHECK_FILES', 2)
    check = api.add_classes(name, CLASSES)['latest_check']
    assert check == {'ref': f'{name}@1', 'problem': None, 'checked': 2, 'total': 4, 'complete': False}
    day = folder(tmp_path, {'4.png': image(), '4.json': label('dent')})
    with pytest.raises(ops.FwError, match=re.escape('dirt（1 个文件，例如 A/3.json）')):
        ops.add_folders(api, name, [day], to='B', echo=quiet)
    monkeypatch.setattr(server_datasets, 'LATEST_CHECK_FILES', 5000)
    monkeypatch.setattr(server_datasets, 'LATEST_CHECK_SECONDS', 0.0)  # out of time before the first file
    other = old_pool(api, tmp_path, {'x.png': image(), 'x.json': label('dent')})
    assert api.add_classes(other, CLASSES)['latest_check'] == {'ref': f'{other}@1', 'problem': None, 'checked': 0,
                                                               'total': 1, 'complete': False}


# --- reading a file for its classes (pure) ------------------------------------------------------------------
def test_what_a_file_says_about_classes():
    assert [formats.class_kind(p) for p in ('a/b.JSON', 'x.txt', 'classes.txt', 'A/Classes.TXT', 'obj.names',
                                            'mask_colors.json', 'a.png', 'data.yaml')] == [
        'json', 'txt', 'list', 'list', 'list', None, None, None]
    table = ['scratch', 'dent']
    assert formats.label_classes('json', label('crack', 'dent', 'crack', 'pit'), table) == (None, ['crack', 'pit'])
    assert formats.label_classes('txt', b'\xef\xbb\xbf1 0 0 0 0\r\n7 1 1 1 1\n7 1\n-1 1\n', table) == (
        None, ['编号 7', '编号 -1'])
    assert formats.label_classes('list', b'scratch\ndent\n\n', table) == (None, [])
    assert formats.label_classes('list', b'scratch\n\ndent\n', table)[0] == '第 2 行是 空行，数据池类别表的第 2 个是 dent'
    assert formats.label_classes('json', b'{"shapes": [1]}', table) == ('有形状没有类别名', [])


# --- test sets follow the table of a pool (0020) -----------------------------------------------------------
def test_a_test_set_follows_the_class_table_of_a_pool(api, make_api, sql, tmp_path):
    """A test set names the pool of its line whose table it follows, when it is made: uploads are checked
    against that table (it grows with the pool), txt included; the table is the pool's, not a copy. Only a data
    pool of the same line (any pool for a test set of no line), with a table; only a test set; field cannot."""
    pool = new_pool(api)
    other_line = f'C-{uuid.uuid4().hex[:8]}'
    from test_trainsets import register_place
    register_place('SES', 'TY')
    api.create_dataset(other_line, 'SES', 'TY', 'pool', '', classes=CLASSES)
    empty = f'C-{uuid.uuid4().hex[:8]}'
    sql.execute("INSERT INTO datasets (name, site, line, kind, note, created_by) "
                "SELECT %s, 'SES', 'TX', 'pool', '', created_by FROM datasets WHERE name = %s", (empty, pool))
    regression = f'C-{uuid.uuid4().hex[:8]}'
    api.create_dataset(regression, 'SES', 'TX', 'regression', '')
    name = f'T-{uuid.uuid4().hex[:8]}'
    for source, why in ((other_line, '请选择同一产线的数据池'), (regression, '不是数据池'), (empty, '尚无类别表'),
                        ('nope-' + name, '不存在'), ('  ', '请选择沿用哪个数据池的类别表')):
        with pytest.raises(ApiError, match=why):
            api.create_dataset(name, 'SES', 'TX', 'test', '', classes_from=source)
    with pytest.raises(ApiError, match='只有测试集沿用数据池的类别表'):
        api.create_dataset(name, 'SES', 'TX', 'regression', '', classes_from=pool)
    with pytest.raises(ApiError) as e:
        make_api('field').create_dataset(name, 'SES', 'TX', 'test', '', classes_from=pool)
    assert e.value.status == 403
    with pytest.raises(ApiError):
        api.dataset(name)                                                 # nothing was left behind
    d = api.create_dataset(name, 'SES', 'TX', 'test', '', classes_from=pool)
    assert d['classes_from'] == pool and d['classes'] == CLASSES
    assert [x for x in api.datasets(kind='test') if x['name'] == name][0]['classes_from'] == pool
    # uploads: a class the table lacks is refused, naming the pool; a txt label is read by the pool's numbering
    bad = folder(tmp_path, {'a.png': image(), 'a.json': label('crack')})
    with pytest.raises(ApiError, match=f'不符合数据池 {pool} 的类别表'):
        ops.import_folder(api, name, bad, echo=quiet)
    with pytest.raises(ApiError, match=f'不符合数据池 {pool} 的类别表'):
        ops.import_folder(api, name, folder(tmp_path, {'b.png': image(), 'b.txt': b'5 0.5 0.5 0.1 0.1\n'}), echo=quiet)
    good = folder(tmp_path, {'a.png': image(), 'a.json': label('dent'), 'b.png': image(), 'b.txt': b'0 0.5 0.5 0.1 0.1\n'})
    assert ops.import_folder(api, name, good, echo=quiet)['number'] == 1
    # the pool's table grows: the test set takes the new class too
    api.add_classes(pool, ['crack'])
    assert api.dataset(name)['classes'] == CLASSES + ['crack']
    assert ops.import_folder(api, name, bad, force=True, echo=quiet)['number'] == 2
    # it never changes; the database holds the same rules
    with pytest.raises(ApiError, match=f'已沿用数据池 {pool} 的类别表，不能更改') as e:
        api.follow_classes(name, pool)
    assert e.value.status == 409
    for statement, args in (('UPDATE datasets SET classes_from = (SELECT id FROM datasets WHERE name = %s) WHERE name = %s', (empty, name)),
                            ('UPDATE datasets SET classes_from = NULL WHERE name = %s', (name,)),
                            ('UPDATE datasets SET classes_from = (SELECT id FROM datasets WHERE name = %s) WHERE name = %s', (pool, regression))):
        with pytest.raises(psycopg.errors.RaiseException):
            sql.execute(statement, args)
    # a test set of no line may follow the pool of any line
    loose = f'T-{uuid.uuid4().hex[:8]}'
    assert api.create_dataset(loose, '', '', 'test', '', classes_from=other_line)['classes_from'] == other_line
    ev = [e for e in api.events(q=name, limit=20)['events'] if e['action'] == 'dataset.create']
    assert ev[0]['detail']['classes_from'] == pool


def test_a_test_set_made_before_names_its_pool_once_and_its_newest_version_is_checked(api, make_api, tmp_path):
    """A test set from before (no table) takes no upload; an administrator or algorithm engineer names its pool
    once, and is told what in its newest version does not fit, to be put right with the next upload."""
    pool = new_pool(api)
    name = f'T-{uuid.uuid4().hex[:8]}'
    api.create_dataset(name, 'SES', 'TX', 'test', '')
    files = {'a.png': image(), 'a.json': label('dent'), 'b.png': image(), 'b.json': label('crack')}
    with pytest.raises(ApiError, match='尚未指定沿用哪个数据池的类别表'):
        ops.import_folder(api, name, folder(tmp_path, files), echo=quiet)
    with mock.patch.object(server_datasets, 'class_problems', return_value=None):   # a version from before 0020
        ops.import_folder(api, name, folder(tmp_path, files), echo=quiet)
    assert api.dataset(name)['classes'] == [] and api.dataset(name)['classes_from'] is None
    with pytest.raises(ApiError) as e:
        make_api('field').follow_classes(name, pool)
    assert e.value.status == 403
    with pytest.raises(ApiError, match='不是测试集'):
        api.follow_classes(pool, pool)
    out = api.follow_classes(name, pool)
    assert out['dataset']['classes_from'] == pool and out['dataset']['classes'] == CLASSES
    check = out['latest_check']
    assert check['ref'] == f'{name}@1' and check['complete'] and 'crack' in check['problem'] and pool in check['problem']
    # the next upload is checked in full, the files it keeps included
    with pytest.raises(ApiError, match='crack'):
        ops.import_folder(api, name, folder(tmp_path, {**files, 'c.png': image()}), echo=quiet)
    assert [e for e in api.events(q=name, limit=20)['events'] if e['action'] == 'dataset.classes_from'][0]['detail'] == {'pool': pool}
