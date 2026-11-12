"""fw dataset add / remove: the server builds the next version from the newest one and the folders given."""
import hashlib
import json

import pytest

from helpers import quiet, read_tree, write
from fw import cli as fw_cli
from fw import ops
from fw.api import ApiError
from fw.contract import manifest_bytes, manifest_hash, parse_manifest
from fwserver import cli as server_cli


def label(*boxes):
    return json.dumps({'version': '2.3.6', 'shapes': [
        {'label': n, 'shape_type': 'rectangle', 'points': [[x, y], [x + 5, y + 5]]} for n, x, y in boxes]}).encode()


def paths(api, version):
    return {p: d for p, d, _ in parse_manifest(api.read_object(version['manifest_sha256']))}


def lines():
    out = []
    return out, out.append


@pytest.fixture
def pool(api, dataset, tmp_path):
    """A dataset whose first version holds two day folders."""
    write(tmp_path / 'v1', '20261101/a.bmp', b'A')
    write(tmp_path / 'v1', '20261101/a.json', label(('ng', 1, 1)))
    write(tmp_path / 'v1', '20261102/b.bmp', b'B')
    write(tmp_path / 'v1', '20261102/b.json', label(('ng', 2, 2)))
    v1 = ops.import_folder(api, dataset, tmp_path / 'v1', echo=quiet)
    return dataset, v1


def test_add_uploads_only_the_folder(api, pool, tmp_path, cache):
    name, v1 = pool
    day = tmp_path / '20261103'
    write(day, 'c.bmp', b'C')
    write(day, 'c.json', label(('ng', 3, 3)))
    out, echo = lines()
    v2 = ops.add_folders(api, name, [day], note='第三天', echo=echo)
    assert v2['number'] == 2 and v2['parent']['id'] == v1['id'] and v2['file_count'] == 6
    assert v2['note'] == '第三天' and v2['source'].startswith('fw dataset add ')
    got = paths(api, v2)
    assert set(got) == set(paths(api, v1)) | {'20261103/c.bmp', '20261103/c.json'}
    assert all(got[p] == d for p, d in paths(api, v1).items())
    assert any('新增 20261103/：2 个文件' in x for x in out)
    assert any('要上传 2 个文件' in x for x in out)
    versions = api.versions(name)
    assert versions[0]['ops'] == []
    assert [(o['op'], o['dir'], o['file_count'], o['removed_count']) for o in versions[1]['ops']] == [
        ('add', '20261103', 2, 0)]
    assert api.version(name, 2)['ops'] == versions[1]['ops']
    # what comes out is the pool with the folder in it
    ops.get(api, cache, f'{name}@2', tmp_path / 'out', echo=quiet)
    assert read_tree(tmp_path / 'out')['20261103/c.bmp'] == b'C'


def test_add_first_version_several_folders_and_to(api, dataset, tmp_path):
    write(tmp_path / 'x' / 'd1', 'a.bmp', b'1')
    write(tmp_path / 'x' / 'd2', 'b.bmp', b'2')
    v1 = ops.add_folders(api, dataset, [tmp_path / 'x' / 'd1', tmp_path / 'x' / 'd2'], echo=quiet)
    assert v1['number'] == 1 and v1['parent'] is None and set(paths(api, v1)) == {'d1/a.bmp', 'd2/b.bmp'}
    assert [o['dir'] for o in api.version(dataset, 1)['ops']] == ['d1', 'd2']
    v2 = ops.add_folders(api, dataset, [tmp_path / 'x' / 'd1'], to='night\\d1', echo=quiet)
    assert set(paths(api, v2)) == {'d1/a.bmp', 'd2/b.bmp', 'night/d1/a.bmp'}
    with pytest.raises(ops.FwError, match='--to 只能'):
        ops.add_folders(api, dataset, [tmp_path / 'x' / 'd1', tmp_path / 'x' / 'd2'], to='z', echo=quiet)
    write(tmp_path / 'y' / 'D1', 'a.bmp', b'1')
    with pytest.raises(ops.FwError, match='同一个目录'):
        ops.add_folders(api, dataset, [tmp_path / 'x' / 'd1', tmp_path / 'y' / 'D1'], echo=quiet)


def test_existing_folder_is_refused_unless_replaced(api, pool, tmp_path):
    name, v1 = pool
    day = tmp_path / 'new' / '20261102'
    write(day, 'b2.bmp', b'B2')
    with pytest.raises(ops.FwError, match=r'池里已有 20261102/（.*@1，2 个文件）.*--replace'):
        ops.add_folders(api, name, [day], echo=quiet)
    assert api.dataset(name)['latest']['id'] == v1['id']
    # the API refuses too, whatever the client checked
    data = manifest_bytes([('b2.bmp', '0' * 64, 1)])
    api.put_bytes(data)
    body = {'add': [{'dir': '20261102', 'manifest_sha256': manifest_hash(data)}], 'expect_latest_id': v1['id']}
    with pytest.raises(ApiError) as e:
        api.create_change(name, body)
    assert e.value.status == 409
    out, echo = lines()
    v2 = ops.add_folders(api, name, [day], replace=True, echo=echo)
    assert set(paths(api, v2)) == {'20261101/a.bmp', '20261101/a.json', '20261102/b2.bmp'}
    assert any('替换 20261102/：原有 2 个文件换成这 1 个' in x for x in out)
    assert any('删除：例如 20261102/b.bmp' in x for x in out)
    assert [(o['op'], o['removed_count']) for o in api.version(name, 2)['ops']] == [('replace', 2)]
    # a folder that differs only in case would be the same folder on Windows
    other = tmp_path / 'case' / 'ABC'
    write(other, 'x.bmp', b'X')
    ops.add_folders(api, name, [other], echo=quiet)
    with pytest.raises(ops.FwError, match='只差大小写'):
        ops.add_folders(api, name, [other], to='abc', replace=True, echo=quiet)
    # also when it is a folder above the new one that differs in case
    with pytest.raises(ops.FwError, match='只差大小写的目录 ABC/'):
        ops.add_folders(api, name, [other], to='abc/sub', echo=quiet)
    m = manifest_bytes([('x.bmp', '0' * 64, 1)])
    api.put_bytes(m)
    two = api.preview_change(name, {'add': [{'dir': 'Night/a', 'manifest_sha256': manifest_hash(m)},
                                            {'dir': 'night/b', 'manifest_sha256': manifest_hash(m)}]})
    assert any('只差大小写的目录 Night/' in r for r in two['reasons'])
    # replacing with the same content makes no version
    with pytest.raises(ops.FwError, match='完全一样'):
        ops.add_folders(api, name, [other], replace=True, echo=quiet)


def test_remove(api, pool):
    name, v1 = pool
    out, echo = lines()
    v2 = ops.remove_dirs(api, name, ['20261101/'], note='拍糊了', echo=echo)
    assert set(paths(api, v2)) == {'20261102/b.bmp', '20261102/b.json'} and v2['note'] == '拍糊了'
    assert any('去掉 20261101/：2 个文件' in x for x in out)
    assert [(o['op'], o['dir'], o['file_count'], o['removed_count'], o['content_sha256'])
            for o in api.version(name, 2)['ops']] == [('remove', '20261101', 0, 2, None)]
    assert set(paths(api, api.version(name, 1))) == set(paths(api, v1))  # the old version is as it was
    with pytest.raises(ApiError, match='没有目录 20261101/'):
        ops.remove_dirs(api, name, ['20261101'], echo=quiet)
    with pytest.raises(ApiError, match='就没有文件了'):
        ops.remove_dirs(api, name, ['20261102'], echo=quiet)


def test_dry_run_and_declined(api, pool, tmp_path):
    name, v1 = pool
    day = tmp_path / '20261103'
    write(day, 'c.bmp', b'C' * 100)
    out, echo = lines()
    assert ops.add_folders(api, name, [day], dry_run=True, echo=echo) is None
    assert any('试运行' in x for x in out)
    digest = hashlib.sha256(b'C' * 100).hexdigest()
    assert api.missing([(digest, 100)]) == [digest]  # the image was not uploaded
    assert ops.add_folders(api, name, [day], confirm=lambda q: False, echo=quiet) is None
    assert ops.remove_dirs(api, name, ['20261101'], dry_run=True, echo=quiet) is None
    assert ops.remove_dirs(api, name, ['20261101'], confirm=lambda q: False, echo=quiet) is None
    assert api.dataset(name)['latest']['id'] == v1['id']


def test_duplicate_images_and_labels_warn(api, pool, tmp_path):
    name, _ = pool
    day = tmp_path / '20261103'
    write(day, 'a_again.bmp', b'A')                           # same image as 20261101/a.bmp
    write(day, 'a_again.json', label(('ng', 1, 1), ('ok', 9, 9)))  # labelled differently
    write(day, 'b_again.bmp', b'B')                           # same image, same boxes, other formatting
    write(day, 'b_again.json', label(('ng', 2, 2)) + b'\n')
    write(day, 'n1.bmp', b'N')
    write(day, 'n2.bmp', b'N')                                # twice in the new folder
    write(day, 'n1.json', label(('ng', 4, 4)))
    out, echo = lines()
    assert ops.add_folders(api, name, [day], dry_run=True, echo=echo) is None
    text = '\n'.join(out)
    assert '有 2 张图片和池里已有的内容完全相同' in text and '20261103/a_again.bmp = 20261101/a.bmp' in text
    assert '有 1 组内容完全相同的图片' in text
    assert '标注要等上传以后才能比较' in text
    out, echo = lines()
    v2 = ops.add_folders(api, name, [day], echo=echo)  # warnings only: the version is made
    text = '\n'.join(out)
    assert v2['number'] == 2
    assert '其中 2 对图片相同、标注不一样' in text
    assert '20261103/a_again.bmp（2 个目标）和 20261101/a.bmp（1 个目标）' in text
    assert '20261103/n2.bmp（没有标注）和 20261103/n1.bmp（1 个目标）' in text
    assert 'b_again.bmp（' not in text


def test_checks_on_the_result(api, pool, tmp_path):
    name, v1 = pool
    day = tmp_path / '20261103'
    write(day, 'c.bmp', b'C')
    write(day, 'notes.docx', b'x')
    with pytest.raises(ops.FwError, match='--allow-other-files'):
        ops.add_folders(api, name, [day], echo=quiet)
    v2 = ops.add_folders(api, name, [day], allow_other_files=True, echo=quiet)
    assert '20261103/notes.docx' in paths(api, v2)
    # later changes are not blocked by what an earlier version was allowed to hold
    v3 = ops.remove_dirs(api, name, ['20261101'], echo=quiet)
    assert v3['number'] == 3
    # the API checks names itself
    for bad in ('', '../x', 'a/../b', 'con', 'a//b'):
        data = manifest_bytes([('x.bmp', '0' * 64, 1)])
        api.put_bytes(data)
        with pytest.raises(ApiError) as e:
            api.preview_change(name, {'add': [{'dir': bad, 'manifest_sha256': manifest_hash(data)}]})
        assert e.value.status == 400, bad
    with pytest.raises(ApiError, match='重复或一个在另一个里面'):
        api.preview_change(name, {'remove': ['20261102', '20261102/sub']})
    with pytest.raises(ApiError, match='还没有上传'):
        api.preview_change(name, {'add': [{'dir': 'z', 'manifest_sha256': 'f' * 64}]})
    with pytest.raises(ApiError, match='新增或去掉哪个目录'):
        api.preview_change(name, {})


def test_stale_preview_is_refused(api, pool, tmp_path):
    name, v1 = pool
    data = manifest_bytes([('x.bmp', hashlib.sha256(b'X').hexdigest(), 1)])
    api.put_bytes(data)
    api.put_bytes(b'X')
    body = {'add': [{'dir': 'x', 'manifest_sha256': manifest_hash(data)}]}
    preview = api.preview_change(name, body)
    assert preview['expect_latest_id'] == v1['id'] and preview['ref'] == f'{name}@2'
    ops.remove_dirs(api, name, ['20261101'], echo=quiet)
    with pytest.raises(ApiError) as e:
        api.create_change(name, dict(body, expect_latest_id=v1['id']))
    assert e.value.status == 409 and '有了变化' in str(e.value)
    # the files of an added folder must be in the store
    data = manifest_bytes([('y.bmp', 'a' * 64, 1)])
    api.put_bytes(data)
    latest = api.dataset(name)['latest']['id']
    with pytest.raises(ApiError):
        api.create_change(name, {'add': [{'dir': 'y', 'manifest_sha256': manifest_hash(data)}],
                                 'expect_latest_id': latest})
    assert api.dataset(name)['latest']['id'] == latest


def test_roles(make_api, pool):
    name, _ = pool
    for role in ('field', 'deploy', 'worker'):
        with pytest.raises(ApiError) as e:
            make_api(role).preview_change(name, {'remove': ['20261101']})
        assert e.value.status == 403


def test_history_cannot_be_rewritten(api, pool, sql):
    name, _ = pool
    v2 = ops.remove_dirs(api, name, ['20261101'], echo=quiet)
    for statement in ("UPDATE version_changes SET dir = 'x' WHERE version_id = %s",
                      'DELETE FROM version_changes WHERE version_id = %s',
                      "INSERT INTO version_changes (version_id, position, op, dir, file_count, total_bytes, "
                      "removed_count) VALUES (%s, 5, 'remove', 'x', 0, 0, 1)"):
        with pytest.raises(Exception):
            with sql.transaction():
                sql.execute(statement, (v2['id'],))
    with pytest.raises(Exception):
        with sql.transaction():
            sql.execute('TRUNCATE version_changes')


def test_cli_and_verify(api, pool, tmp_path, service, monkeypatch, capsys):
    name, _ = pool
    day = tmp_path / '20261103'
    write(day, 'c.bmp', b'C')
    monkeypatch.setattr(ops, 'connect', lambda: api)
    monkeypatch.setattr(api, 'close', lambda: None)
    assert fw_cli.main(['dataset', 'add', name, str(day), '--yes', '--note', 'n']) == 0
    assert fw_cli.main(['dataset', 'remove', name, '20261101', '--yes']) == 0
    assert fw_cli.main(['dataset', 'versions', name]) == 0
    out = capsys.readouterr().out
    assert '+20261103/' in out and '-20261101/' in out and '整份导入' in out
    with pytest.raises(SystemExit):
        fw_cli.main(['dataset', 'remove', name])
    # fw-server verify knows the uploaded file list of the added folder
    from fwserver import db
    with db.connect(service.settings.dsn) as c:
        _, wanted, _ = server_cli.verify_inputs(c)
    digest = api.version(name, 2)['ops'][0]['content_sha256']
    assert digest in wanted and '20261103' in wanted[digest][1]
