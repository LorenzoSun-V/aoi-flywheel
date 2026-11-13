"""The service end to end over HTTP: accounts and roles, uploads, versions, diffs, and the two CLIs."""
import hashlib
import json
import uuid

import httpx
import pytest

from helpers import quiet, read_tree, write
from fw import cli as fw_cli
from fw import ops
from fw.api import Api, ApiError
from fw.contract import diff_entries, parse_manifest
from fwserver import auth, db
from fwserver import cli as server_cli


def status_of(call):
    try:
        call()
    except ApiError as e:
        return e.status
    return 200


def test_health_and_me(service, make_api, tokens):
    assert httpx.get(service.url + '/api/v1/health').json()['ok'] is True
    for role in ('admin', 'algo', 'field', 'worker'):
        me = make_api(role).me()
        assert me['role'] == role and me['name'] == tokens[role + '_name']
        assert me['caps'] == auth.caps_of(role) and me['api_level'] >= 2


def test_roles(make_api, dataset, tmp_path):
    algo = make_api('algo')
    write(tmp_path / 'v1', 'a.bmp', b'1')
    v = ops.import_folder(algo, dataset, tmp_path / 'v1', echo=quiet)
    # a field account reads every dataset and downloads its files, and changes nothing
    field = make_api('field')
    assert [d['name'] for d in field.datasets() if d['name'] == dataset] == [dataset]
    assert field.dataset(dataset)['latest']['id'] == v['id'] and field.versions(dataset)[0]['id'] == v['id']
    assert field.read_object(v['manifest_sha256'])
    assert field.diff(v['id'], v['id']) is not None
    for change in (lambda: field.create_dataset(f'X-{uuid.uuid4().hex[:6]}', 'SES', 'TX', 'pool', ''),
                   lambda: field.create_version(dataset, v['manifest_sha256'], None, v['id'], '', ''),
                   lambda: field.preview_change(dataset, {'remove': ['x']})):
        assert status_of(change) == 403
    # it uploads files (for its labelling batches), which by themselves change no dataset
    assert field.missing([('0' * 64, 1)]) == ['0' * 64] and field.put_bytes(b'data')
    assert field.post('/folder-check', {'paths': ['a.png']})['files'] == 1
    # a worker (a GPU machine) fetches what its jobs need and sees no lists
    worker = make_api('worker')
    assert status_of(worker.datasets) == 403 and worker.read_object(v['manifest_sha256'])
    assert status_of(make_api('admin').datasets) == 200


def test_tokens_revoke_and_disable(make_api, database, service):
    assert status_of(make_api(token='').me) == 401
    assert status_of(make_api(token='fw_not-a-token').me) == 401
    with db.connect(database) as c:
        name = f'u-{uuid.uuid4().hex[:6]}'
        auth.add_user(c, name, 'algo')
        t1, id1 = auth.create_token(c, name, 'laptop')
        t2, _ = auth.create_token(c, name, 'trainer-01')
        assert status_of(make_api(token=t1).me) == 200
        auth.revoke_token(c, id1)
        assert status_of(make_api(token=t1).me) == 401
        assert status_of(make_api(token=t2).me) == 200
        assert auth.set_disabled(c, name, True) == 1
        assert status_of(make_api(token=t2).me) == 401
        auth.set_disabled(c, name, False)
        assert status_of(make_api(token=t2).me) == 401  # re-enabling does not bring tokens back
        last = c.execute('SELECT last_used_at FROM tokens WHERE id = %s', (id1,)).fetchone()['last_used_at']
        assert last is not None


def test_upload_is_checked_by_the_server(api, service):
    data = b'some image bytes'
    wrong = hashlib.sha256(b'other').hexdigest()
    r = api.http.put(f'/objects/{wrong}', content=data)
    assert r.status_code == 400
    assert not service.app.state.store.path(wrong).exists()
    assert list(service.app.state.store.tmp.iterdir()) == []
    good = api.put_bytes(data)
    assert api.read_object(good) == data
    assert api.missing([(good, len(data)), (good, len(data) + 1), (wrong, 1)]) == [good, wrong]
    assert api.http.put('/objects/not-a-hash', content=b'x').status_code == 400


def test_damaged_object_on_the_server_is_repaired_by_upload(api, service):
    data = b'precious'
    digest = api.put_bytes(data)
    path = service.app.state.store.path(digest)
    path.chmod(0o644)
    path.write_bytes(b'precioux')  # same size, wrong content
    r = api.http.put(f'/objects/{digest}', content=data)
    assert r.status_code == 200 and r.json()['created'] is True
    assert path.read_bytes() == data


def test_dataset_rules(api, dataset):
    assert status_of(lambda: api.create_dataset(dataset.lower(), 'SES', 'TX', 'pool', '')) == 409
    assert status_of(lambda: api.create_dataset('bad name', 'SES', 'TX', 'pool', '')) == 400
    assert status_of(lambda: api.create_dataset('x@1', 'SES', 'TX', 'pool', '')) == 400
    assert status_of(lambda: api.create_dataset('ok-name', '', '', 'nope', '')) == 400
    assert status_of(lambda: api.version(dataset, 'latest')) == 404
    assert status_of(lambda: api.dataset('does-not-exist')) == 404
    assert api.dataset(dataset.lower())['name'] == dataset  # names are case-insensitive


def test_versions_diff_and_chain(api, dataset, tmp_path, cache):
    v1 = {'a.bmp': b'1', 'a.json': b'j1', 'b.bmp': b'2', 'b.json': b'j2', 'c.bmp': b'3', 'c.json': b'j3'}
    v2 = {'a.bmp': b'1', 'a.json': b'j1-new', 'copy/b.bmp': b'2', 'copy/b.json': b'j2', 'moved.bmp': b'3',
          'c.json': b'j3', 'd.bmp': b'4'}
    for n, files in ((1, v1), (2, v2)):
        for rel, data in files.items():
            write(tmp_path / f'v{n}', rel, data)
    a = ops.import_folder(api, dataset, tmp_path / 'v1', note='first', echo=quiet)
    b = ops.import_folder(api, dataset, tmp_path / 'v2', echo=quiet)
    assert b['parent']['id'] == a['id'] and b['changes'] == {'added': 1, 'modified': 1, 'deleted': 0, 'renamed': 3}
    d = api.diff(a['id'], b['id'])
    expected = diff_entries(parse_manifest(api.read_object(a['manifest_sha256'])),
                            parse_manifest(api.read_object(b['manifest_sha256'])))
    assert {k: d[k] for k in expected} == expected
    assert d['renamed'] == [['b.bmp', 'copy/b.bmp'], ['b.json', 'copy/b.json'], ['c.bmp', 'moved.bmp']]
    assert d['added'] == ['d.bmp'] and d['modified'] == ['a.json'] and d['deleted'] == []
    assert [v['number'] for v in api.versions(dataset)] == [1, 2]
    assert api.get(f'/versions/{b["id"]}')['ref'] == f'{dataset}@2'
    # identical content and stale "latest" are refused by the server itself
    assert status_of(lambda: api.create_version(dataset, b['manifest_sha256'], b['id'], b['id'], '', '')) == 409
    assert status_of(lambda: api.create_version(dataset, a['manifest_sha256'], b['id'], a['id'], '', '')) == 409
    # an older version as parent (going back to v1's content)
    c = ops.import_folder(api, dataset, tmp_path / 'v1', echo=quiet)
    assert c['number'] == 3 and c['manifest_sha256'] == a['manifest_sha256']
    for n, files in ((1, v1), (2, v2), (3, v1)):
        ops.get(api, cache, f'{dataset}@{n}', tmp_path / f'out{n}', echo=quiet)
        assert read_tree(tmp_path / f'out{n}') == files


def test_fw_command_line(service, tokens, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('FW_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setenv('FW_CACHE', str(tmp_path / 'cache'))
    monkeypatch.delenv('FW_SERVER', raising=False)
    monkeypatch.delenv('FW_TOKEN', raising=False)
    assert fw_cli.main(['login', '--server', service.url, '--token', tokens['algo']]) == 0
    assert json.loads((tmp_path / 'config.json').read_text())['server'] == service.url
    name = f'CLI-{uuid.uuid4().hex[:6]}'
    files = {'a.bmp': b'1', 'a.json': b'{}', '图/b.bmp': b'2', '图/b.json': b'{}'}
    for rel, data in files.items():
        write(tmp_path / 'src', rel, data)
    assert fw_cli.main(['whoami']) == 0
    assert fw_cli.main(['dataset', 'create', name, '--site', 'SES', '--line', 'TX']) == 0
    assert fw_cli.main(['dataset', 'import', name, str(tmp_path / 'src'), '--dry-run']) == 0
    assert fw_cli.main(['dataset', 'import', name, str(tmp_path / 'src')]) == 1  # no terminal to confirm in
    assert fw_cli.main(['dataset', 'import', name, str(tmp_path / 'src'), '--yes', '--note', 'v1']) == 0
    write(tmp_path / 'src', 'c.bmp', b'3')
    assert fw_cli.main(['dataset', 'import', name, str(tmp_path / 'src'), '--yes']) == 0
    assert fw_cli.main(['dataset', 'list']) == 0
    assert fw_cli.main(['dataset', 'versions', name, '--source']) == 0
    assert fw_cli.main(['dataset', 'diff', f'{name}@2']) == 0
    assert fw_cli.main(['dataset', 'diff', f'{name}@1', f'{name}@2', '--list']) == 0
    assert fw_cli.main(['get', f'{name}@1', '--to', str(tmp_path / 'got')]) == 0
    assert read_tree(tmp_path / 'got') == files
    assert fw_cli.main(['export', name, '--to', str(tmp_path / 'exp')]) == 0
    assert fw_cli.main(['verify', str(tmp_path / 'exp')]) == 0
    (tmp_path / 'exp' / 'data' / 'c.bmp').write_bytes(b'4')
    assert fw_cli.main(['verify', str(tmp_path / 'exp')]) == 1
    assert fw_cli.main(['cache', 'info']) == 0
    assert fw_cli.main(['cache', 'clear']) == 0
    assert fw_cli.main(['get', 'no-such-dataset', '--to', str(tmp_path / 'x')]) == 1
    out = capsys.readouterr()
    assert '新增 1' in out.out and '没有数据集' in out.err


def test_fw_server_command_line(database, service, monkeypatch, capsys):
    monkeypatch.setenv('FW_DSN', database)
    monkeypatch.setenv('FW_STORE', str(service.app.state.store.root))
    name = f'srv-{uuid.uuid4().hex[:6]}'
    assert server_cli.main(['migrate']) == 0
    assert server_cli.main(['user', 'add', name, '--role', 'algo']) == 0
    assert server_cli.main(['user', 'add', name, '--role', 'algo']) == 1
    assert server_cli.main(['token', 'create', name, '--label', 'trainer-01']) == 0
    token = [line for line in capsys.readouterr().out.splitlines() if line.startswith('fw_')][0]
    api = Api(service.url, token)
    try:
        assert api.me()['name'] == name
        assert server_cli.main(['token', 'list', name]) == 0
        assert server_cli.main(['user', 'list']) == 0
        assert server_cli.main(['user', 'disable', name]) == 0
        assert status_of(api.me) == 401
    finally:
        api.close()
    assert server_cli.main(['token', 'revoke', '999999']) == 1
    assert server_cli.main(['verify']) == 0
