"""Acceptance: a failure while uploading, while committing, or while flushing to disk never leaves a frozen
version that lacks files; the next run of the same import completes it."""
import uuid

import pytest

from helpers import read_tree, write
from fw import ops
from fw.api import ApiError
from fw.cache import Cache
from fwserver import datasets as ds


def quiet(_):
    pass


def folder(tmp_path, n=20):
    """Files no other test has uploaded: the service and its store are shared by the whole session, and a
    file the server already has is never sent again."""
    salt = uuid.uuid4().hex
    files = {}
    for i in range(n):
        files[f'img_{i:03d}.bmp'] = salt.encode() + bytes([i]) * (100 + i)
        files[f'img_{i:03d}.json'] = f'{{"i": {i}, "salt": "{salt}"}}'.encode()
    for rel, data in files.items():
        write(tmp_path / 'src', rel, data)
    return tmp_path / 'src', files


def no_versions(api, dataset):
    return api.dataset(dataset)['latest'] is None


def all_frozen_versions_complete(service, database):
    from fwserver import db
    with db.connect(database) as c:
        return ds.verify(c, service.app.state.store, rehash=True, echo=quiet) == []


@pytest.mark.parametrize('route', ['batch', 'single'])
def test_interrupted_upload(api, dataset, tmp_path, service, database, monkeypatch, route):
    src, files = folder(tmp_path)
    if route == 'batch':  # small files, many per request
        monkeypatch.setattr(ops, 'UPLOAD_COUNT', 5)
        method, digests_of = 'put_objects', lambda items: [d for d, _, _ in items]
    else:  # the way large files go: one per request
        monkeypatch.setattr(ops, 'BATCH_OBJECT_MAX', 0)
        method, digests_of = 'put_file', lambda digest, path: [digest]
    real = getattr(api, method)
    sent = []

    def flaky(*args):
        if len(sent) >= 15:
            raise ApiError(0, 'network gone')
        sent.extend(digests_of(*args))
        return real(*args)

    setattr(api, method, flaky)
    with pytest.raises(ApiError):
        ops.import_folder(api, dataset, src, workers=1, echo=quiet)
    assert no_versions(api, dataset)
    # a version that points at files the server lacks is refused outright
    from fw.contract import manifest_bytes, manifest_hash, sha256_file
    entries = [(rel, *sha256_file(src / rel)) for rel in sorted(files)]
    data = manifest_bytes(entries)
    api.put_bytes(data)
    with pytest.raises(ApiError) as e:
        api.create_version(dataset, manifest_hash(data), None, None, 'test', '')
    assert e.value.status == 400 and no_versions(api, dataset)
    resent = []

    def counting(*args):
        resent.extend(digests_of(*args))
        return real(*args)

    setattr(api, method, counting)
    v = ops.import_folder(api, dataset, src, echo=quiet)
    assert v['ref'].endswith('@1')
    assert len(set(sent)) == 15 and not set(sent) & set(resent)  # the rerun sends only what was missing
    assert len(set(sent) | set(resent)) == len(files)
    assert all_frozen_versions_complete(service, database)


def test_failure_between_rows_and_freeze(api, dataset, tmp_path, service, database, sql, cache):
    src, files = folder(tmp_path)

    def boom():
        raise RuntimeError('injected failure before freezing')

    service.hooks['before_freeze'] = boom
    try:
        with pytest.raises(Exception):
            ops.import_folder(api, dataset, src, echo=quiet)
    finally:
        service.hooks.pop('before_freeze')
    assert no_versions(api, dataset)
    leftovers = sql.execute('SELECT count(*) AS n FROM versions v JOIN datasets d ON d.id = v.dataset_id '
                            'WHERE d.name = %s', (dataset,)).fetchone()['n']
    assert leftovers == 0
    v = ops.import_folder(api, dataset, src, echo=quiet)
    assert v['number'] == 1
    ops.get(api, cache, v['ref'], tmp_path / 'out', echo=quiet)
    assert read_tree(tmp_path / 'out') == files
    assert all_frozen_versions_complete(service, database)


def test_disk_flush_error_blocks_writes_until_verified(api, dataset, tmp_path, service, database, monkeypatch):
    import ctypes
    import errno
    store = service.app.state.store

    def failing_syncfs(fd):
        ctypes.set_errno(errno.EIO)
        return -1

    src, _ = folder(tmp_path)
    monkeypatch.setattr(store, '_syncfs', failing_syncfs)
    with pytest.raises(ApiError) as e:
        ops.import_folder(api, dataset, src, echo=quiet)
    assert e.value.status == 503 and no_versions(api, dataset)
    monkeypatch.undo()
    # flagged: further uploads and versions are refused until verify --rehash passes
    write(tmp_path / 'other', 'x.bmp', b'other')
    with pytest.raises(ApiError) as e:
        ops.import_folder(api, dataset, tmp_path / 'other', echo=quiet)
    assert e.value.status == 503
    from fwserver import cli
    monkeypatch.setenv('FW_DSN', database)
    monkeypatch.setenv('FW_STORE', str(store.root))
    assert cli.main(['verify']) == 1  # without --rehash the flag stays
    assert cli.main(['verify', '--rehash']) == 0
    assert store.flag_text() is None
    assert ops.import_folder(api, dataset, src, echo=quiet)['number'] == 1
    assert all_frozen_versions_complete(service, database)


def test_concurrent_import_into_same_dataset_is_refused(api, dataset, tmp_path):
    src, _ = folder(tmp_path, 3)
    write(tmp_path / 'b', 'b.bmp', b'b')

    def other_import_meanwhile(prompt):
        ops.import_folder(api, dataset, tmp_path / 'b', echo=quiet)
        return True

    with pytest.raises(ApiError) as e:
        ops.import_folder(api, dataset, src, confirm=other_import_meanwhile, echo=quiet)
    assert e.value.status == 409
    assert api.dataset(dataset)['latest']['number'] == 1
