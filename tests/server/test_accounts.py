"""Accounts over HTTP, what each role may do, the record of who did what, and which fw works with the server."""
import uuid

import httpx
import pytest

from helpers import quiet, write
from fw import ops
from fw.api import Api, ApiError
from fw.contract import API_HEADER, API_LEVEL
from fwserver import auth, db, events
from fwserver import app as fw_app
from fwserver import cli as server_cli


def status_of(call):
    try:
        call()
    except ApiError as e:
        return e.status
    return 200


def new_name(prefix='u'):
    return f'{prefix}-{uuid.uuid4().hex[:8]}'


def last(api, **query):
    return api.events(limit=1, **query)['events'][0]


# --- what each role may do -------------------------------------------------------------------------------
def test_the_table_of_capabilities():
    assert set(auth.ROLES) == {'admin', 'algo', 'field', 'worker'}
    assert all(set(roles) <= set(auth.ROLES) and text for roles, text in auth.CAPS.values())
    # the three roles the user decided on (2026-11-12): admin; algo changes datasets, models, evaluations and
    # jobs, reviews batches; field reads datasets and models, downloads, registers releases, and uploads
    # labelling batches (its own), which needs the upload of files
    assert auth.caps_of('algo') == sorted(['datasets.read', 'datasets.write', 'objects.upload', 'objects.download',
                                           'models.read', 'models.write', 'jobs.read', 'jobs.write', 'events.read',
                                           'batches.read', 'batches.write', 'batches.review', 'sites.read',
                                           'sites.write'])
    assert auth.caps_of('field') == sorted(['datasets.read', 'objects.download', 'objects.upload', 'models.read',
                                            'releases.write', 'batches.read', 'batches.write', 'sites.read'])
    assert auth.caps_of('worker') == sorted(['objects.upload', 'objects.download', 'jobs.read', 'jobs.run'])
    assert set(auth.caps_of('admin')) == set(auth.CAPS) - {'jobs.run'}


def test_every_endpoint_asks_for_a_capability(service):
    """No endpoint is open to an account just because nobody thought of it: each one either names a capability
    or is one of the few every signed-in account has."""
    open_to_all = {'/api/v1/health', '/api/v1/me', '/api/v1/me/tokens', '/api/v1/tokens/{token_id}/revoke',
                   '/api/v1/openapi.json'}
    for route in service.app.routes:
        path = getattr(route, 'path', '')
        if not path.startswith('/api/') or path in open_to_all:
            continue
        deps = [d.call.__qualname__ for d in route.dependant.dependencies]
        assert any('need.<locals>.check' in d for d in deps), f'{path} asks for no capability'


def test_accounts_are_managed_over_http_by_administrators_only(make_api, service):
    admin = make_api('admin')
    for role in ('algo', 'field', 'worker'):
        other = make_api(role)
        for call in (other.users, lambda: other.post('/users', {'name': new_name(), 'role': 'admin'}),
                     lambda: other.post(f'/users/{new_name()}/role', {'role': 'admin'}),
                     lambda: other.post(f'/users/{new_name()}/disable', {}),
                     lambda: other.get(f'/users/{new_name()}/tokens'),
                     lambda: other.post(f'/users/{new_name()}/tokens', {'label': 'x'})):
            assert status_of(call) == 403
    name = new_name()
    assert admin.post('/users', {'name': name, 'role': 'field'}) == {'name': name, 'role': 'field'}
    assert status_of(lambda: admin.post('/users', {'name': name.upper(), 'role': 'algo'})) == 400  # taken
    assert status_of(lambda: admin.post('/users', {'name': 'bad name', 'role': 'algo'})) == 400
    assert status_of(lambda: admin.post('/users', {'name': new_name(), 'role': 'deploy'})) == 400
    made = admin.post(f'/users/{name}/tokens', {'label': '笔记本'})
    person = Api(service.url, made['token'])
    try:
        assert person.me()['role'] == 'field' and status_of(lambda: person.create_dataset(new_name('D'), 'SES', 'TX', 'pool', '')) == 403
        # a role change counts from the next request; the token stays
        assert admin.post(f'/users/{name}/role', {'role': 'algo'})['role'] == 'algo'
        assert person.me()['role'] == 'algo' and 'datasets.write' in person.me()['caps']
        assert status_of(lambda: admin.post(f'/users/{name}/role', {'role': 'worker'})) == 400
        assert status_of(lambda: admin.post(f'/users/{name}/role', {'role': 'boss'})) == 400
        listed = {u['name']: u for u in admin.users()['users']}[name]
        assert (listed['role'], listed['active_tokens'], listed['disabled_at']) == ('algo', 1, None)
        tokens = admin.get(f'/users/{name}/tokens')['tokens']
        assert [(t['id'], t['label'], t['revoked_at']) for t in tokens] == [(made['id'], '笔记本', None)]
        assert 'token' not in tokens[0]  # shown once, when it was made
        assert admin.post(f'/users/{name}/disable', {}) == {'tokens_revoked': 1}
        assert status_of(person.me) == 401
        admin.post(f'/users/{name}/enable', {})
        assert status_of(person.me) == 401  # the old token stays dead
    finally:
        person.close()
    assert status_of(lambda: admin.get(f'/users/{new_name()}/tokens')) == 400


def test_everyone_sees_and_revokes_their_own_tokens_only(make_api, service, database):
    name = new_name()
    with db.connect(database) as c:
        auth.add_user(c, name, 'field')
        t1, id1 = auth.create_token(c, name, 'a')
        t2, id2 = auth.create_token(c, name, 'b')
    person, other = Api(service.url, t1), make_api('field')
    try:
        assert [t['id'] for t in person.get('/me/tokens')['tokens']] == [id1, id2]
        assert status_of(lambda: other.post(f'/tokens/{id2}/revoke', {})) == 404  # not theirs
        assert status_of(lambda: make_api('algo').post(f'/tokens/{id2}/revoke', {})) == 404
        person.post(f'/tokens/{id2}/revoke', {})
        assert status_of(lambda: person.post(f'/tokens/{id2}/revoke', {})) == 404  # already
        assert status_of(Api(service.url, t2).me) == 401
        make_api('admin').post(f'/tokens/{id1}/revoke', {})  # an administrator revokes anyone's
        assert status_of(person.me) == 401
    finally:
        person.close()


def test_the_last_administrator_stays(database):
    """Checked on a database of its own, where the administrators can be counted."""
    with db.connect(database) as c:
        admins = [u['name'] for u in auth.list_users(c) if u['role'] == 'admin' and u['disabled_at'] is None]
        extra = new_name('adm')
        auth.add_user(c, extra, 'admin')
        try:
            c.execute('BEGIN')
            for name in admins:  # inside a transaction that is rolled back: the other tests keep their accounts
                auth.set_disabled(c, name, True)
            with pytest.raises(auth.AuthError, match='唯一在用的管理员'):
                auth.set_disabled(c, extra, True)
            with pytest.raises(auth.AuthError, match='唯一在用的管理员'):
                auth.set_role(c, extra, 'algo')
        finally:
            c.execute('ROLLBACK')
        auth.set_role(c, extra, 'algo')  # with the others back it may go
        assert auth.get_user(c, extra)['role'] == 'algo'


# --- the record --------------------------------------------------------------------------------------------
def test_changes_are_recorded_with_who_made_them(api, make_api, tokens, dataset, tmp_path, database):
    admin = make_api('admin')
    e = last(admin, action='dataset.create', q=dataset)
    assert (e['actor'], e['object'], e['by_server'], e['action_text']) == (tokens['algo_name'], dataset, False, '新建数据集')
    assert e['detail']['kind'] == 'pool'
    write(tmp_path / 'd', 'a.bmp', b'1')
    v = ops.import_folder(api, dataset, tmp_path / 'd', echo=quiet)
    e = last(admin, action='version', q=dataset)
    assert (e['action'], e['object'], e['actor']) == ('version.create', v['ref'], tokens['algo_name'])
    assert e['detail']['file_count'] == 1 and e['detail']['source'].startswith('fw dataset import')
    # accounts and tokens, by whom; from the server command line there is no account
    name = new_name()
    admin.post('/users', {'name': name, 'role': 'field'})
    made = admin.post(f'/users/{name}/tokens', {'label': 'x'})
    admin.post(f'/users/{name}/role', {'role': 'algo'})
    admin.post(f'/tokens/{made["id"]}/revoke', {})
    admin.post(f'/users/{name}/disable', {})
    admin.post(f'/users/{name}/disable', {})  # again: nothing changed, nothing recorded
    admin.post(f'/users/{name}/enable', {})
    with db.connect(database) as c:
        auth.set_role(c, name, 'field')
    got = admin.events(q=name, limit=20)['events']
    assert [(x['action'], x['actor'], x['by_server']) for x in reversed(got)] == [
        ('user.create', tokens['admin_name'], False), ('token.create', tokens['admin_name'], False),
        ('user.role', tokens['admin_name'], False), ('token.revoke', tokens['admin_name'], False),
        ('user.disable', tokens['admin_name'], False), ('user.enable', tokens['admin_name'], False),
        ('user.role', events.SERVER, True)]
    assert got[-3]['detail'] == {'from': 'field', 'to': 'algo'} and got[-2]['detail']['token_id'] == made['id']
    assert made['token'] not in str(got)  # the token itself is never written down
    # filters and paging
    assert all(x['action'].startswith('user.') for x in admin.events(action='user', q=name)['events'])
    assert [x['action'] for x in admin.events(action='user.role', q=name)['events']] == ['user.role', 'user.role']
    assert admin.events(actor=tokens['admin_name'].upper(), q=name)['events'][0]['action'] == 'user.enable'
    page = admin.events(q=name, limit=2)
    assert len(page['events']) == 2 and page['next_before'] == page['events'][-1]['id']
    rest = admin.events(q=name, limit=20, before=page['next_before'])
    assert len(rest['events']) == 5 and rest['next_before'] is None
    # algorithm engineers read the record; field accounts and workers do not
    assert status_of(lambda: api.events(limit=1)) == 200
    for role in ('field', 'worker'):
        assert status_of(lambda: make_api(role).events(limit=1)) == 403


def test_a_refused_change_leaves_no_record(api, make_api, dataset):
    admin = make_api('admin')
    before = admin.events(limit=1)['events'][0]['id']
    assert status_of(lambda: api.create_dataset(dataset, 'SES', 'TX', 'pool', '')) == 409
    assert status_of(lambda: admin.post('/users', {'name': 'bad name', 'role': 'algo'})) == 400
    assert status_of(lambda: api.cancel_job(10 ** 9)) == 404
    assert admin.events(limit=1)['events'][0]['id'] == before


def test_the_record_cannot_be_rewritten(sql, api, dataset):
    for statement in ("UPDATE events SET actor = 'x'", 'DELETE FROM events', 'TRUNCATE events'):
        with pytest.raises(Exception, match='cannot be changed or removed|TRUNCATE|truncate'):
            with sql.transaction():
                sql.execute(statement)
    with pytest.raises(ValueError):
        events.record(sql, None, 'made.up', 'x')


def test_fw_server_user_role(database, monkeypatch, capsys):
    name = new_name()
    monkeypatch.setenv('FW_DSN', database)
    monkeypatch.setenv('FW_STORE', '/tmp')
    assert server_cli.main(['user', 'add', name, '--role', 'field']) == 0
    assert server_cli.main(['user', 'role', name, 'algo']) == 0
    assert '现在是算法工程师' in capsys.readouterr().out
    assert server_cli.main(['user', 'role', name, 'worker']) == 1
    with pytest.raises(SystemExit):
        server_cli.main(['user', 'add', new_name(), '--role', 'deploy'])


# --- which fw works with which server ------------------------------------------------------------------------
def test_every_answer_states_the_interface_level(service, tokens):
    r = httpx.get(service.url + '/api/v1/health')
    assert r.headers[API_HEADER] == str(API_LEVEL)
    r = httpx.get(service.url + '/api/v1/me')  # also a refusal
    assert r.status_code == 401 and r.headers[API_HEADER] == str(API_LEVEL)
    r = httpx.get(service.url + '/api/v1/me', headers={'Authorization': f'Bearer {tokens["algo"]}', API_HEADER: 'x'})
    assert r.status_code == 426  # not a level at all


def test_an_fw_older_than_the_server_accepts_is_told_to_update(service, tokens, monkeypatch):
    monkeypatch.setattr(fw_app, 'MIN_CLIENT_LEVEL', API_LEVEL + 1)
    api = Api(service.url, tokens['algo'])
    try:
        with pytest.raises(ApiError) as e:
            api.me()
        assert e.value.status == 426 and 'pip install ./client' in str(e.value)
    finally:
        api.close()
    # a request without the header is one from before the header existed: level 1
    assert httpx.get(service.url + '/api/v1/health').status_code == 426
    assert httpx.get(service.url + '/').status_code == 200  # the web page is the server's own


def test_a_server_older_than_fw_needs_is_named(service, tokens, monkeypatch):
    from fw import api as fw_api
    monkeypatch.setattr(fw_api, 'MIN_SERVER_LEVEL', API_LEVEL + 1)
    api = Api(service.url, tokens['algo'])
    try:
        with pytest.raises(ApiError, match='平台服务比这台机器的 fw 旧'):
            api.me()
        with pytest.raises(ApiError, match='平台服务比这台机器的 fw 旧'):
            api.read_object('0' * 64)
    finally:
        api.close()


def test_an_address_that_is_not_the_platform_is_called_that():
    """A wrong address or a proxy's error page has no interface level: that is not an old server."""
    def not_the_platform(request):
        return httpx.Response(502, text='<html>Bad Gateway</html>', headers={'content-type': 'text/html'})

    proxied = Api('http://proxy.example', 'x', transport=httpx.MockTransport(not_the_platform))
    try:
        with pytest.raises(ApiError, match='回答的不是平台服务（HTTP 502）') as e:
            proxied.me()
        assert e.value.status == 502
    finally:
        proxied.close()
