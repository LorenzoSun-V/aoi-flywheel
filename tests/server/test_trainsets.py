"""Training-set snapshots: the split, continuing a snapshot, the checks on labels and classes, and what the
database keeps frozen."""
import json
import uuid

import psycopg
import pytest

from helpers import quiet, read_tree, write
from fw import cli as fw_cli
from fw import ops
from fw.api import ApiError
from fwserver import trainsets
from fwserver.trainsets import Options, Sample

CLASSES = ['scratch', 'dent']


def label(*names) -> bytes:
    return json.dumps({'version': '1', 'shapes': [{'label': n, 'points': [[0, 0], [1, 1]]} for n in names]}).encode()


def status_of(call):
    try:
        call()
    except ApiError as e:
        return e.status
    return 200


def pool(api, tmp_path, files: dict, site='SES', line='TX', name=None) -> str:
    """A pool dataset with one version holding files ({path: bytes}); returns its name."""
    name = name or f'P-{uuid.uuid4().hex[:8]}'
    api.create_dataset(name, site, line, 'pool', '')
    add_version(api, tmp_path, name, files)
    return name


def add_version(api, tmp_path, name, files: dict) -> dict:
    src = tmp_path / f'src-{uuid.uuid4().hex[:6]}'
    for rel, data in files.items():
        write(src, rel, data)
    return ops.import_folder(api, name, src, force=True, echo=quiet)  # force: the pool may hold same-stem images


def sample_files(prefix: str, n: int, names=('scratch',), negatives=0) -> dict:
    """n images under prefix/, each with a JSON label naming `names`, plus `negatives` images without one."""
    files = {}
    for i in range(n):
        files[f'{prefix}/img{i:03}.png'] = f'{prefix}-{i}'.encode()
        files[f'{prefix}/img{i:03}.json'] = label(*names)
    for i in range(negatives):
        files[f'{prefix}/neg{i:03}.png'] = f'{prefix}-neg-{i}'.encode()
    return files


def make(api, name, sources, classes=CLASSES, **kw):
    return ops.create_trainset(api, name, sources, classes, echo=quiet, **kw)


def splits_of(tree: dict) -> dict:
    """{key: split} of every image in a fetched snapshot."""
    out = {}
    for path in tree:
        if path.endswith('.png'):
            split, _, key = path.partition('/')
            out[key] = split
    return out


def fetch(api, cache, ref, dest) -> dict:
    ops.get(api, cache, ref, dest, echo=quiet)
    return read_tree(dest)


# --- the split itself (no database) -------------------------------------------------------------------
def test_cut_is_fixed_by_keys_ratio_and_seed():
    keys = [f'SES/TX/{i}.png' for i in range(1000)]
    a = trainsets._cut(keys, 0.2, 0)
    assert a == trainsets._cut(list(reversed(keys)), 0.2, 0)  # input order does not matter
    assert sum(1 for s in a.values() if s == 'val') == 200
    b = trainsets._cut(keys, 0.2, 1)
    assert a != b and sum(1 for s in b.values() if s == 'val') == 200
    assert set(trainsets._cut(keys, 0.0, 0).values()) == {'train'}
    # adding samples does not move the ones that were there: only their rank among each other matters
    more = trainsets._cut(keys + [f'SES/TX/new{i}.png' for i in range(10)], 0.2, 0)
    assert sum(1 for k in keys if more[k] != a[k]) <= 10


def samples(*keys):
    return {k: Sample(k, 'SES', 'TX', (k.split('/', 2)[2], '0' * 64, 1), None) for k in keys}


def test_assign_continue_merge_and_new_to():
    keys = [f'SES/TX/a/{i}.png' for i in range(10)] + [f'SES/TX/b/{i}.png' for i in range(5)]
    s = samples(*keys)
    rule, problems = trainsets._assign(s, Options(['x'], CLASSES, test_dirs=['b']), None)
    assert problems == [] and rule['mode'] == 'fresh'
    assert all(s[k].split == 'test' for k in keys[10:])
    assert {s[k].split for k in keys[:10]} == {'train', 'val'}
    previous = {k: (v.split, None) for k, v in s.items()}

    s2 = samples(*keys, 'SES/TX/c/0.png', 'SES/TX/c/1.png')
    trainsets._assign(s2, Options(['x'], CLASSES), previous)
    assert all(s2[k].split == previous[k][0] for k in keys)
    assert s2['SES/TX/c/0.png'].split == s2['SES/TX/c/1.png'].split == 'test'

    s3 = samples(*keys, 'SES/TX/c/0.png')
    trainsets._assign(s3, Options(['x'], CLASSES, new_to='trainval'), previous)
    assert s3['SES/TX/c/0.png'].split in ('train', 'val')

    s4 = samples(*keys)
    rule, _ = trainsets._assign(s4, Options(['x'], CLASSES, merge_test=True), previous)
    assert rule['merge_test'] and all(s4[k].split in ('train', 'val') for k in keys)
    assert all(s4[k].split == previous[k][0] for k in keys[:10])  # old train/val samples stay where they were

    _, problems = trainsets._assign(samples(*keys), Options(['x'], CLASSES, test_dirs=['b', 'nope']), None)
    assert any('nope' in p for p in problems)


# --- over HTTP ------------------------------------------------------------------------------------------
def test_first_snapshot_layout_and_fetch(api, cache, tmp_path):
    files = {**sample_files('2026-09', 20, ('scratch', 'dent'), negatives=3), **sample_files('2026-10', 5)}
    files['2026-09/extra.txt'] = b'0 0.5 0.5 0.1 0.1\n1 0.2 0.2 0.1 0.1\n'
    files['2026-09/extra.bmp'] = b'yolo image'
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    v = make(api, name, [f'{src}@1'], val=0.25, test_dirs=['2026-10'], note='first')
    assert v['ref'] == f'{name}@1' and v['kind'] == 'trainset'
    ts = v['trainset']
    assert ts['classes'] == CLASSES and ts['sources'] == [f'{src}@1'] and ts['based_on'] is None
    counts = ts['counts']
    assert counts['test']['samples'] == 5 and counts['test']['boxes'] == {'scratch': 5, 'dent': 0}
    assert counts['train']['samples'] + counts['val']['samples'] == 24
    assert counts['val']['samples'] == 6  # round(24 * 0.25)
    assert counts['train']['negatives'] + counts['val']['negatives'] == 3
    assert sum(counts[s]['boxes']['dent'] for s in ('train', 'val')) == 21

    tree = fetch(api, cache, v['ref'], tmp_path / 'got')
    assert tree['classes.txt'] == b'scratch\ndent\n'
    info = json.loads(tree['trainset.json'])
    assert info['format'] == trainsets.FORMAT and info['counts'] == counts and info['classes'] == CLASSES
    assert info['sources'][0]['ref'] == f'{src}@1'
    assert info['rule'] == {'mode': 'fresh', 'val': 0.25, 'seed': 0, 'test_dirs': ['2026-10'], 'merge_test': False}
    for path, data in files.items():  # every pool file is in exactly one split, unchanged, label next to image
        hits = [p for p in tree if '/' in p and p.split('/', 1)[1] == f'SES/TX/{path}']
        assert len(hits) == 1 and tree[hits[0]] == data, path
    assert len(tree) == len(files) + 2
    splits = splits_of(tree)
    assert all(s == 'test' for k, s in splits.items() if k.startswith('SES/TX/2026-10/'))
    for k, split in splits.items():  # the label is in the same split as its image
        stem = k.rsplit('.', 1)[0]
        assert not any(p.startswith(f'{other}/{stem}.') for other in ('train', 'val', 'test') if other != split
                       for p in tree)
    assert api.trainset(name)['trainset'] == ts
    assert any(d['name'] == name for d in api.trainsets())
    assert not any(d['name'] == src for d in api.trainsets())

    # the same inputs again would make the same snapshot: refused as unchanged
    preview = api.preview_trainset(name, {'sources': [f'{src}@1'], 'classes': CLASSES, 'val': 0.25,
                                          'test_dirs': ['2026-10'], 'fresh': True})
    assert any('完全一样' in p for p in preview['problems'])
    assert preview['counts'] == counts


def test_continue_keeps_every_split(api, cache, tmp_path):
    files = sample_files('2026-09', 30)
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    v1 = make(api, name, [f'{src}@1'], val=0.2)
    t1 = splits_of(fetch(api, cache, v1['ref'], tmp_path / 'g1'))
    assert set(t1.values()) == {'train', 'val'}

    files2 = dict(files)
    files2.update(sample_files('2026-11', 4))
    del files2['2026-09/img000.png'], files2['2026-09/img000.json']
    files2['2026-09/img001.json'] = label('scratch', 'dent')
    add_version(api, tmp_path, src, files2)
    lines = []
    v2 = ops.create_trainset(api, name, [f'{src}@2'], CLASSES, echo=lines.append)
    assert v2['parent']['ref'] == v1['ref'] and v2['trainset']['based_on'] == v1['ref']
    assert v2['summary']['changes'] == {'added': 4, 'removed': 1, 'moved': 0, 'moved_examples': [], 'relabeled': 1}
    assert any('接着' in line for line in lines)
    t2 = splits_of(fetch(api, cache, v2['ref'], tmp_path / 'g2'))
    assert all(t2[k] == t1[k] for k in t2 if k in t1)
    assert [t2[k] for k in t2 if k.startswith('SES/TX/2026-11/')] == ['test'] * 4

    v3 = make(api, name, [f'{src}@2'], merge_test=True)
    t3 = splits_of(fetch(api, cache, v3['ref'], tmp_path / 'g3'))
    assert set(t3.values()) <= {'train', 'val'}
    assert v3['summary']['changes']['moved'] == 4
    assert all(t3[k] == t2[k] for k in t2 if t2[k] != 'test')
    d = api.diff(v2['id'], v3['id'])  # moving a sample between splits is a rename, nothing is uploaded again
    assert len(d['renamed']) == 8 and d['added'] == [] and d['deleted'] == []

    v4 = make(api, name, [f'{src}@2'], fresh=True, seed=7)
    assert v4['trainset']['based_on'] is None and v4['parent']['ref'] == v3['ref']
    assert v4['trainset']['rule']['mode'] == 'fresh'


def test_split_file(api, cache, tmp_path):
    files = sample_files('a', 4)
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    keys = [f'SES/TX/a/img{i:03}.png' for i in range(4)]
    split_file = tmp_path / 'split.txt'
    split_file.write_text('# my split\n' + '\n'.join(f'{k}\t{s}' for k, s in zip(keys, ['train', 'val', 'test',
                                                                                        'train'])) + '\n',
                          encoding='utf-8')
    split = ops.read_split_file(split_file)
    with pytest.raises(ops.FwError, match='漏了'):
        make(api, name, [f'{src}@1'], split={k: split[k] for k in keys[:3]})
    with pytest.raises(ops.FwError, match='不在来源版本里'):
        make(api, name, [f'{src}@1'], split=dict(split, **{'SES/TX/b.png': 'val'}))
    with pytest.raises(ApiError, match='划分清单'):
        make(api, name, [f'{src}@1'], split=split, merge_test=True)
    v = make(api, name, [f'{src}@1'], split=split)
    assert splits_of(fetch(api, cache, v['ref'], tmp_path / 'g')) == split
    assert v['trainset']['rule'] == {'mode': 'split-file', 'split_count': 4}


@pytest.mark.parametrize('files, message', [
    ({'a.png': b'1', 'a.json': label('crack')}, "类别 'crack' 不在类别表里"),
    ({'a.png': b'1', 'a.txt': b'5 0.5 0.5 0.1 0.1\n'}, '超出类别表'),
    ({'a.png': b'1', 'a.txt': b'x 0.5 0.5 0.1 0.1\n'}, '不是整数'),
    ({'a.png': b'1', 'a.json': b'{not json'}, '不是合法的 JSON'),
    ({'a.png': b'1', 'a.json': b'{"shapes": [{"points": []}]}'}, '没有类别名'),
    ({'a.png': b'1', 'a.json': b'[]'}, '没有 shapes'),
    ({'a.png': b'1', 'a.json': '{"shapes": []}'.encode('utf-16')}, '不是 UTF-8'),
    ({'a.png': b'1', 'b.json': label('scratch')}, '没有对应的图片'),
    ({'a.png': b'1', 'a.bmp': b'2', 'a.json': label('scratch')}, '同名不同扩展名'),
    ({'a.png': b'1', 'a.json': label('scratch'), 'a.txt': b'0 0.5 0.5 0.1 0.1\n'}, '同时有 JSON 和 txt'),
])
def test_bad_labels_are_refused(api, tmp_path, files, message):
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    body = {'sources': [f'{src}@1'], 'classes': CLASSES}
    preview = api.preview_trainset(name, body)
    assert any(message in p for p in preview['problems']), preview['problems']
    with pytest.raises(ops.FwError, match='没有建训练集快照'):
        make(api, name, [f'{src}@1'])
    assert status_of(lambda: api.create_trainset(name, body)) == 400
    assert status_of(lambda: api.trainset(name)) == 404
    assert not any(d['name'] == name for d in api.datasets())


def test_bad_sources_and_names(api, tmp_path):
    ok = pool(api, tmp_path, {'a.png': b'1'})
    no_site = pool(api, tmp_path, {'a.png': b'1'}, site='', line='')
    name = f'T-{uuid.uuid4().hex[:8]}'
    assert make(api, name, [f'{ok}@1'])['number'] == 1
    for sources, message in (([f'{no_site}@1'], '没有填现场或产线'), ([f'{name}@1'], '训练集快照'),
                             ([f'{ok}@1', f'{ok}@1'], '写了两次'), ([f'{ok}@9'], None)):
        with pytest.raises(ApiError, match=message):
            make(api, f'T-{uuid.uuid4().hex[:8]}', sources)
    with pytest.raises(ApiError, match='共用名称') as e:
        make(api, ok, [f'{ok}@1'])
    assert e.value.status == 409
    same = pool(api, tmp_path, {'a.png': b'2'})  # also SES/TX: the same key twice
    with pytest.raises(ops.FwError, match='两个来源都有 SES/TX/a.png'):
        make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{ok}@1', f'{same}@1'])
    other = pool(api, tmp_path, {'a.png': b'3'}, site='SES', line='TY')
    v = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{ok}@1', f'{other}@1'], val=0)
    assert v['trainset']['counts']['train']['samples'] == 2
    assert status_of(lambda: make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{ok}@1'], classes=[])) == 422
    for classes, message in ((['a', 'a'], '重复'), ([' a'], '首尾空格'), (['a\tb'], '制表符')):
        with pytest.raises(ApiError, match=message):
            make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{ok}@1'], classes=classes)
    assert status_of(lambda: make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{ok}@1'], val=1.0)) == 400
    assert status_of(lambda: make(api, 'bad name', [f'{ok}@1'])) == 400


def test_trainsets_are_made_only_by_trainset_create(api, tmp_path):
    src = pool(api, tmp_path, {'a.png': b'1'})
    name = f'T-{uuid.uuid4().hex[:8]}'
    make(api, name, [f'{src}@1'])
    write(tmp_path / 'f', 'b.png', b'2')
    with pytest.raises(ApiError, match='只能用 fw trainset create') as e:
        ops.import_folder(api, name, tmp_path / 'f', echo=quiet)
    assert e.value.status == 400
    assert status_of(lambda: api.create_dataset(f'X-{uuid.uuid4().hex[:6]}', 'SES', 'TX', 'trainset', '')) in (400,
                                                                                                                 422)
    with pytest.raises(ApiError, match='不是训练集快照'):
        api.trainset(src)


def test_someone_else_made_one_meanwhile(api, tmp_path):
    src = pool(api, tmp_path, sample_files('a', 6))
    name = f'T-{uuid.uuid4().hex[:8]}'

    def other_person_first(_prompt):
        make(api, name, [f'{src}@1'], seed=1)
        return True

    with pytest.raises(ApiError, match='在这期间有了变化') as e:
        ops.create_trainset(api, name, [f'{src}@1'], CLASSES, confirm=other_person_first, echo=quiet)
    assert e.value.status == 409
    assert [v['number'] for v in api.versions(name)] == [1]


def test_failure_before_freeze_leaves_nothing(api, service, sql, tmp_path):
    src = pool(api, tmp_path, sample_files('a', 3))
    name = f'T-{uuid.uuid4().hex[:8]}'

    def boom():
        raise RuntimeError('injected failure before freezing')

    service.hooks['before_freeze'] = boom
    try:
        with pytest.raises(Exception):
            make(api, name, [f'{src}@1'])
    finally:
        service.hooks.pop('before_freeze')
    assert sql.execute('SELECT count(*) AS n FROM versions v JOIN datasets d ON d.id = v.dataset_id '
                       'WHERE d.name = %s', (name,)).fetchone()['n'] == 0
    assert make(api, name, [f'{src}@1'])['number'] == 1


def test_roles(make_api, tmp_path):
    algo = make_api('algo')
    src = pool(algo, tmp_path, {'a.png': b'1'})
    name = f'T-{uuid.uuid4().hex[:8]}'
    make(make_api('admin'), name, [f'{src}@1'])
    body = {'sources': [f'{src}@1'], 'classes': CLASSES}
    for role in ('field', 'deploy'):
        api = make_api(role)
        assert status_of(api.trainsets) == 403
        assert status_of(lambda: api.trainset(name)) == 403
        assert status_of(lambda: api.preview_trainset(name, body)) == 403
        assert status_of(lambda: api.create_trainset(name, body)) == 403


# --- what the database keeps ----------------------------------------------------------------------------
@pytest.fixture
def snapshot(api, tmp_path):
    src = pool(api, tmp_path, {'a.png': b'1', 'a.json': label('scratch')})
    return make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'])


@pytest.mark.parametrize('statement', [
    "UPDATE trainset_versions SET classes = '[]' WHERE version_id = %(v)s",
    "UPDATE trainset_versions SET counts = '{}' WHERE version_id = %(v)s",
    "DELETE FROM trainset_versions WHERE version_id = %(v)s",
    "UPDATE trainset_sources SET source_id = source_id WHERE version_id = %(v)s",
    "DELETE FROM trainset_sources WHERE version_id = %(v)s",
    "INSERT INTO trainset_sources VALUES (%(v)s, 5, %(v)s)",
    "TRUNCATE trainset_versions",
    "TRUNCATE trainset_sources",
    "UPDATE datasets SET kind = 'pool' WHERE id = (SELECT dataset_id FROM versions WHERE id = %(v)s)",
])
def test_snapshot_record_cannot_change(sql, snapshot, statement):
    with pytest.raises(psycopg.Error):
        with sql.transaction():
            sql.execute(statement, {'v': snapshot['id']})
    row = sql.execute('SELECT classes FROM trainset_versions WHERE version_id = %s', (snapshot['id'],)).fetchone()
    assert row['classes'] == CLASSES


def test_only_trainset_versions_have_a_record(sql, snapshot, api, tmp_path):
    src = pool(api, tmp_path, {'a.png': b'1'})
    pool_version = api.version(src, 'latest')['id']
    with pytest.raises(psycopg.Error, match='not a trainset'):
        with sql.transaction():
            sql.execute("INSERT INTO trainset_versions VALUES (%s, NULL, '[]', '{}', '{}')", (pool_version,))
    with pytest.raises(psycopg.Error, match='has versions'):
        with sql.transaction():
            sql.execute("UPDATE datasets SET kind = 'trainset' WHERE name = %s", (src,))


# --- the command line and the training-script call -----------------------------------------------------------
def test_command_line_and_get_trainset(service, tokens, api, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('FW_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setenv('FW_CACHE', str(tmp_path / 'cache'))
    monkeypatch.delenv('FW_SERVER', raising=False)
    monkeypatch.delenv('FW_TOKEN', raising=False)
    assert fw_cli.main(['login', '--server', service.url, '--token', tokens['algo']]) == 0
    src = pool(api, tmp_path, {**sample_files('2026-09', 10), **sample_files('2026-10', 2, ('dent',))})
    (tmp_path / 'classes.txt').write_text('scratch\ndent\n', encoding='utf-8')
    name = f'T-{uuid.uuid4().hex[:8]}'
    args = ['trainset', 'create', name, '--from', f'{src}@1', '--classes', str(tmp_path / 'classes.txt'),
            '--test-dirs', '2026-10']
    assert fw_cli.main(args + ['--dry-run']) == 0
    assert fw_cli.main(args) == 1  # no terminal to confirm in
    capsys.readouterr()
    assert fw_cli.main(args + ['--yes']) == 0
    out = capsys.readouterr().out
    assert f'已冻结 {name}@1' in out and 'test 2 张' in out
    assert fw_cli.main(args + ['--yes']) == 1  # unchanged
    assert '完全一样' in capsys.readouterr().err
    assert fw_cli.main(['trainset', 'list']) == 0
    assert name in capsys.readouterr().out
    assert fw_cli.main(['trainset', 'show', name]) == 0
    out = capsys.readouterr().out
    assert '重新划分' in out and 'test 目录 2026-10' in out and 'dent' in out
    assert fw_cli.main(['dataset', 'list']) == 0
    assert '训练集快照' in capsys.readouterr().out
    assert fw_cli.main(['trainset', 'show', src]) == 1

    import fw
    ts = fw.get_trainset(name, tmp_path / 'train')
    assert ts['ref'] == f'{name}@1' and ts['classes'] == CLASSES and ts['sources'] == [f'{src}@1']
    assert ts['path'] == str(tmp_path / 'train') and (tmp_path / 'train' / 'classes.txt').is_file()
    assert ts['counts']['test']['boxes']['dent'] == 2
