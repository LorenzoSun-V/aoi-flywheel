"""The register of sites and lines: what pools, models and releases may name, and lists narrowed by them."""
import uuid

import psycopg
import pytest

from helpers import quiet, write
from fw import cli as fw_cli
from fw import ops

from test_models import files_in, model, register, snapshot  # noqa: F401  (fixtures)
from test_trainsets import make, pool, sample_files, status_of


def new_site():
    return f'S{uuid.uuid4().hex[:8]}'


def line_of(tree, site, line):
    [s] = [s for s in tree['sites'] if s['code'] == site]
    [x] = [x for x in s['lines'] if x['code'] == line]
    return x


def test_sites_and_lines_are_registered_once(api, make_api):
    site = new_site()
    field, admin = make_api('field'), make_api('admin')
    assert status_of(lambda: field.add_site(site)) == 403
    tree = api.add_site(site, '苏州厂')
    [s] = [s for s in tree['sites'] if s['code'] == site]
    assert s == {'code': site, 'name': '苏州厂', 'lines': []}
    assert status_of(lambda: api.add_site(site.lower())) == 409       # one site in any case of letters
    api.add_site(f'{site.lower()}/TX', '贴片线')                       # a line of it, however the site is typed
    assert status_of(lambda: api.add_site(f'{site}/tx')) == 409
    other = new_site()
    admin.add_site(f'{other}/L1')                                      # a new site comes with its first line
    tree = field.sites()
    assert line_of(tree, site, 'TX') == {'code': 'TX', 'name': '贴片线', 'datasets': 0, 'trainsets': 0, 'models': 0,
                                         'batches': 0, 'releases': 0}
    assert line_of(tree, other, 'L1')['name'] == ''
    for bad in ('', 'A-B', 'A/B-C', 'A/B/C', 'A B', 'a@b', '/TX', 'A/', 'x' * 65):
        assert status_of(lambda: api.add_site(bad)) == 400, bad
    # only the shown name changes
    api.rename_site(f'{site}/tx', '贴片一线')
    api.rename_site(site, '')
    tree = api.sites()
    assert line_of(tree, site, 'TX')['name'] == '贴片一线' and [s['name'] for s in tree['sites'] if s['code'] == site] == ['']
    assert status_of(lambda: api.rename_site(f'{site}/none', 'x')) == 404
    assert status_of(lambda: field.rename_site(site, 'x')) == 403
    acts = [(e['action'], e['object']) for e in api.events(q=site)['events']]
    assert acts == [('site.rename', site), ('site.rename', f'{site}/TX'), ('line.add', f'{site}/TX'), ('site.add', site)]


def test_pools_models_and_releases_take_registered_lines_only(api, make_api, snapshot, tmp_path):  # noqa: F811
    site = new_site()
    name = f'D-{uuid.uuid4().hex[:8]}'
    with pytest.raises(ops.ApiError, match=f'fw site add {site}/TX') as e:
        api.create_dataset(name, site, 'TX', 'pool', '')
    assert e.value.status == 400
    assert status_of(lambda: api.create_dataset(name, '', '', 'pool', '')) == 400      # a pool belongs to a line
    assert status_of(lambda: api.create_dataset(name, 'SES', '', 'pool', '')) == 400
    assert status_of(lambda: api.create_model(f'M-{name}', site, 'TX', 'det', '')) == 400
    api.add_site(f'{site}/TX')
    assert status_of(lambda: api.create_dataset(name, site, 'TY', 'pool', '')) == 400  # that line is not registered
    d = api.create_dataset(name, site.lower(), 'tx', 'pool', '')                       # stored as registered
    assert (d['site'], d['line']) == (site, 'TX')
    m = api.create_model(f'M-{name}', site.lower(), 'tx', 'det', '')
    assert (m['site'], m['line']) == (site, 'TX')
    # a test set may belong to no line, or to a registered one
    assert api.create_dataset(f'{name}-t', '', '', 'test', '')['site'] == ''
    assert status_of(lambda: api.create_dataset(f'{name}-u', new_site(), 'TX', 'test', '')) == 400
    # a release: the model's line, or another registered one
    register(api, f'M-{name}', snapshot, files_in(tmp_path))
    deploy = make_api('deploy')
    body = {'model': f'M-{name}', 'number': '1', 'deploy_repo': 'git@x:deploy.git', 'deploy_commit': 'a' * 40,
            'deployed_at': '2026-11-01T08:00:00+08:00'}
    assert status_of(lambda: deploy.register_release(dict(body, site=new_site(), line='TX'))) == 400
    assert deploy.register_release(dict(body, site=site.lower(), line='tx'))['ref'] == f'{site}-TX-r1'
    assert line_of(api.sites(), site, 'TX') == {'code': 'TX', 'name': '', 'datasets': 1, 'trainsets': 0, 'models': 1,
                                                'batches': 0, 'releases': 1}


def test_lists_are_narrowed_by_site_line_and_text(api, tmp_path, monkeypatch, capsys):
    site = new_site()
    a = pool(api, tmp_path / 'a', {**sample_files('2026-09', 4), **sample_files('2026-10', 2)}, site=site, line='TX')
    b = pool(api, tmp_path / 'b', sample_files('2026-09', 2), site=site, line='TY')
    ts = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{a}@1'], test_dirs=['2026-10'])['dataset']
    api.create_model(f'M-{a}', site, 'TX', 'det', '')
    write(tmp_path / 'day', 'x.png', f'site batch {uuid.uuid4().hex}'.encode())
    batch = ops.upload_batch(api, tmp_path / 'day', b, '张三', echo=quiet)['name']

    def names(rows):
        return sorted(r['name'] for r in rows)
    assert names(api.datasets(site=site)) == sorted([a, b, ts])
    assert names(api.datasets(site=site.lower(), line='tx')) == sorted([a, ts])   # the snapshot took data from TX
    assert names(api.datasets(site=site, line='TY')) == [b]
    assert names(api.datasets(site=site, kind='pool')) == sorted([a, b])
    assert names(api.datasets(q=a[2:].upper())) == [a] and api.datasets(site=new_site()) == []
    assert names(api.trainsets(site=site, line='TX')) == [ts] and api.trainsets(site=site, line='TY') == []
    assert names(api.models(site=site)) == [f'M-{a}'] and api.models(site=site, line='TY') == []
    assert names(api.models(q=a.lower())) == [f'M-{a}']
    assert names(api.batches(site=site)['batches']) == [batch] and api.batches(site=site, line='TX')['batches'] == []
    assert names(api.batches(q=batch[:6])['batches']) == [batch]
    assert names(api.datasets()) == names(api.datasets(site='', line='', q=''))
    tree = api.sites()
    assert line_of(tree, site, 'TX')['datasets'] == 1 and line_of(tree, site, 'TX')['trainsets'] == 1
    assert line_of(tree, site, 'TY')['batches'] == 1
    # the command line
    monkeypatch.setattr(ops, 'connect', lambda: api)
    monkeypatch.setattr(api, 'close', lambda: None)
    assert fw_cli.main(['dataset', 'list', '--site', site, '--line', 'TY']) == 0
    out = capsys.readouterr().out
    assert b in out and a not in out
    assert fw_cli.main(['model', 'list', '--search', a]) == 0
    assert f'M-{a}' in capsys.readouterr().out
    assert fw_cli.main(['trainset', 'list', '--site', site]) == 0 and ts in capsys.readouterr().out
    assert fw_cli.main(['batch', 'list', '--site', site]) == 0 and batch in capsys.readouterr().out
    other = new_site()
    assert fw_cli.main(['site', 'add', f'{other}/L1', '--name', '一线']) == 0
    assert fw_cli.main(['site', 'name', other, '新厂']) == 0
    capsys.readouterr()
    assert fw_cli.main(['site', 'list']) == 0
    out = capsys.readouterr().out
    assert f'{other}（新厂）' in out and 'L1（一线）' in out and site in out


def test_a_code_is_for_good(api, sql):
    site = new_site()
    api.add_site(f'{site}/TX')
    for statement in ("UPDATE sites SET code = code || 'x' WHERE code = %(s)s",
                      "DELETE FROM sites WHERE code = %(s)s",
                      "UPDATE lines SET code = 'TZ' WHERE site_id = (SELECT id FROM sites WHERE code = %(s)s)",
                      "UPDATE lines SET site_id = site_id + 1 WHERE site_id = (SELECT id FROM sites WHERE code = %(s)s)",
                      "DELETE FROM lines WHERE site_id = (SELECT id FROM sites WHERE code = %(s)s)",
                      "TRUNCATE lines", "TRUNCATE sites CASCADE",
                      "INSERT INTO sites (code) VALUES (lower(%(s)s))",
                      "INSERT INTO sites (code) VALUES ('a-b')",
                      "INSERT INTO lines (site_id, code) SELECT id, 'tx' FROM sites WHERE code = %(s)s"):
        with pytest.raises(psycopg.Error):
            sql.execute(statement, {'s': site})
    assert [x['code'] for s in api.sites()['sites'] if s['code'] == site for x in s['lines']] == ['TX']


def test_a_label_refused_after_the_creation_says_what_exists(api, model, snapshot, tmp_path, monkeypatch, capsys):  # noqa: F811
    """--label on fw model register: when the label is taken between the check and the registration, the
    version exists all the same and the message says so."""
    register(api, model, snapshot, files_in(tmp_path, 'first'))
    d = tmp_path / 'run2'
    write(d, 'best.onnx', __import__('test_models').onnx_bytes(uuid.uuid4().hex))
    monkeypatch.setattr(ops, 'connect', lambda: api)
    monkeypatch.setattr(api, 'close', lambda: None)
    monkeypatch.setattr(ops, 'check_new_label', lambda *a, **k: api.add_model_label(model, 1, 'v0.1.0'))  # taken meanwhile
    assert fw_cli.main(['model', 'register', model, '--onnx', str(d / 'best.onnx'), '--trainset', snapshot['ref'],
                        '--label', 'v0.1.0']) == 1
    err = capsys.readouterr().err
    assert f'{model}@2 已经建好' in err and f'fw label add {model}@2 <版本号> --model' in err
    assert api.model_version(model, 2)['labels'] == []
