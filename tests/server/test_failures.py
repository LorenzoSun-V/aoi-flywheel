"""Acceptance: a failure while uploading, while committing, or while flushing to disk never leaves a frozen
version that lacks files; the next run of the same import completes it."""
import ctypes
import errno
import os
import uuid
from pathlib import Path

import pytest

from helpers import read_tree, write
from fw import ops
from fw.api import ApiError
from fw.cache import Cache
from fw.contract import sha256_file
from fwserver import cli
from fwserver import datasets as ds
from fwserver.store import FLAG, MARKER, Store


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


def failing_syncfs(fd):
    ctypes.set_errno(errno.EIO)
    return -1


def disk_full(*args, **kwargs):
    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def verify_rehash(monkeypatch, database, store) -> int:
    monkeypatch.setenv('FW_DSN', database)
    monkeypatch.setenv('FW_STORE', str(store.root))
    return cli.main(['verify', '--rehash'])


def test_disk_flush_error_blocks_writes_until_verified(api, dataset, tmp_path, service, database, monkeypatch):
    store = service.app.state.store
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


def test_block_holds_when_the_disk_is_too_full_for_the_flag(api, dataset, tmp_path, service, database, monkeypatch):
    """syncfs fails and WRITE-ERROR cannot be created (disk full): writes stay refused, in this process and in
    any other or restarted one, until verify --rehash passes."""
    store = service.app.state.store
    src, _ = folder(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(store, '_syncfs', failing_syncfs)
        m.setattr(store, '_write_flag', disk_full)
        with pytest.raises(ApiError) as e:
            ops.import_folder(api, dataset, src, echo=quiet)
        assert e.value.status == 503
    try:
        assert not (store.root / FLAG).exists() and not (store.root / MARKER).exists()
        with pytest.raises(ApiError) as e:  # the disk works again, but nothing has been checked
            ops.import_folder(api, dataset, src, echo=quiet)
        assert e.value.status == 503 and no_versions(api, dataset)
        restarted = Store(store.root)
        try:
            assert restarted.problem() is not None
        finally:
            restarted.close()
    finally:
        assert verify_rehash(monkeypatch, database, store) == 0
    assert ops.import_folder(api, dataset, src, echo=quiet)['number'] == 1
    assert all_frozen_versions_complete(service, database)


def test_block_holds_in_memory_when_the_disk_refuses_everything(api, dataset, tmp_path, service, database,
                                                                monkeypatch):
    """Not even WRITE-OK can be deleted (read-only file system): the running service still refuses writes,
    and lets them through again as soon as verify --rehash has passed."""
    store = service.app.state.store
    src, _ = folder(tmp_path)
    real_unlink = os.unlink

    def read_only_marker(path, *args, **kwargs):
        if Path(path).name == MARKER:
            raise OSError(errno.EROFS, os.strerror(errno.EROFS))
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(store, '_syncfs', failing_syncfs)
        m.setattr(store, '_write_flag', disk_full)
        m.setattr(os, 'unlink', read_only_marker)
        with pytest.raises(ApiError) as e:
            ops.import_folder(api, dataset, src, echo=quiet)
        assert e.value.status == 503
    try:
        assert (store.root / MARKER).exists() and not (store.root / FLAG).exists()
        with pytest.raises(ApiError) as e:
            ops.import_folder(api, dataset, src, echo=quiet)
        assert e.value.status == 503 and no_versions(api, dataset)
    finally:
        assert verify_rehash(monkeypatch, database, store) == 0
    assert ops.import_folder(api, dataset, src, echo=quiet)['number'] == 1


def test_bringing_back_lost_files_of_an_unchanged_version_is_flushed(api, dataset, tmp_path, service,
                                                                     monkeypatch):
    """Re-importing an unchanged folder brings back files the server lost; they must be on disk before the
    command says the old version is complete again."""
    src, _ = folder(tmp_path, 3)
    v = ops.import_folder(api, dataset, src, echo=quiet)
    store = service.app.state.store
    lost, _ = sha256_file(src / 'img_001.bmp')
    store.path(lost).unlink()
    events = []
    real_install, real_sync = store.install, store.sync
    monkeypatch.setattr(store, 'install', lambda digest, tmp: events.append('install') or real_install(digest, tmp))
    monkeypatch.setattr(store, 'sync', lambda: events.append('sync') or real_sync())
    assert ops.import_folder(api, dataset, src, echo=quiet)['id'] == v['id']
    assert events == ['install', 'sync'] and store.path(lost).is_file()


def test_rehash_after_a_write_error_checks_files_no_version_uses_yet(api, dataset, tmp_path, service, database,
                                                                    monkeypatch):
    """The failed import leaves its uploaded files behind, and after the write error one of them is damaged
    (same size). verify --rehash must catch it before it lifts the block: the retry would otherwise reuse it
    and freeze a version that cannot be fetched."""
    store = service.app.state.store
    src, files = folder(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(store, '_syncfs', failing_syncfs)
        with pytest.raises(ApiError) as e:
            ops.import_folder(api, dataset, src, echo=quiet)
        assert e.value.status == 503 and no_versions(api, dataset)
    damaged, _ = sha256_file(src / 'img_003.bmp')
    path = store.path(damaged)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.chmod(0o644)
    path.write_bytes(bytes(data))
    assert verify_rehash(monkeypatch, database, store) == 0
    assert not path.exists()  # moved to quarantine/: the server no longer claims to have it
    v = ops.import_folder(api, dataset, src, echo=quiet)  # so the retry sends it again
    assert v['number'] == 1 and all_frozen_versions_complete(service, database)
    cache = Cache(tmp_path / 'cache')
    try:
        ops.get(api, cache, v['ref'], tmp_path / 'out', echo=quiet)
    finally:
        cache.close()
    assert read_tree(tmp_path / 'out') == files


def test_rehash_fails_when_a_folder_of_the_store_cannot_be_listed(api, dataset, tmp_path, service, database,
                                                                   monkeypatch):
    """Same as above, but the folder holding the damaged file cannot be listed while verify runs (EIO). A
    check that skipped it would pass without having seen the file: verify must fail and keep the block."""
    store = service.app.state.store
    src, files = folder(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(store, '_syncfs', failing_syncfs)
        with pytest.raises(ApiError) as e:
            ops.import_folder(api, dataset, src, echo=quiet)
        assert e.value.status == 503 and no_versions(api, dataset)
    damaged, _ = sha256_file(src / 'img_005.bmp')
    path = store.path(damaged)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.chmod(0o644)
    path.write_bytes(bytes(data))
    real_scandir = os.scandir

    def scandir(p='.'):
        if os.fspath(p) == str(path.parent):
            raise OSError(errno.EIO, os.strerror(errno.EIO), os.fspath(p))
        return real_scandir(p)

    with monkeypatch.context() as m:
        m.setattr(os, 'scandir', scandir)
        assert verify_rehash(monkeypatch, database, store) == 1
    assert path.exists() and store.problem() is not None  # nothing moved, still refusing writes
    with pytest.raises(ApiError) as e:
        ops.import_folder(api, dataset, src, echo=quiet)
    assert e.value.status == 503
    assert verify_rehash(monkeypatch, database, store) == 0  # the folder reads again: now it is caught
    assert not path.exists()
    v = ops.import_folder(api, dataset, src, echo=quiet)
    assert all_frozen_versions_complete(service, database)
    cache = Cache(tmp_path / 'cache')
    try:
        ops.get(api, cache, v['ref'], tmp_path / 'out', echo=quiet)
    finally:
        cache.close()
    assert read_tree(tmp_path / 'out') == files


def _power_cut_during_an_import(api, dataset, tmp_path, service, monkeypatch):
    """An import uploads part of its files, then the machine loses power: the files sent so far were never
    made durable, and one comes back damaged (same size). Then the machine and the server start again.
    Returns (folder, files, path of the damaged object)."""
    from fwserver import store as store_module
    store = service.app.state.store
    src, files = folder(tmp_path)
    real = api.put_objects
    sent = []

    def cut(items):
        if len(sent) >= 15:
            raise ApiError(0, 'power gone')
        sent.extend(d for d, _, _ in items)
        return real(items)

    with monkeypatch.context() as m:
        m.setattr(ops, 'UPLOAD_COUNT', 5)
        m.setattr(api, 'put_objects', cut)
        with pytest.raises(ApiError):
            ops.import_folder(api, dataset, src, workers=1, echo=quiet)
    assert no_versions(api, dataset)
    path = store.path(sent[3])
    data = bytearray(path.read_bytes())
    data[:] = bytes(len(data))  # what an unsynced file can look like after a power cut: same size, zeros
    path.chmod(0o644)
    path.write_bytes(bytes(data))
    # the restart: a new boot of the machine, a new server process
    monkeypatch.setattr(store_module, '_boot_id', lambda: 'boot-after-the-power-cut')
    monkeypatch.setattr(store, '_boot', 'boot-after-the-power-cut')
    return src, files, path


def test_power_loss_during_an_import_blocks_writes_until_verified(api, dataset, tmp_path, service, database,
                                                                  monkeypatch):
    """Nothing reported an error, but after the restart the store must refuse writes until verify --rehash has
    checked it; otherwise the retry reuses the damaged file and freezes a version that cannot be fetched."""
    store = service.app.state.store
    src, files, path = _power_cut_during_an_import(api, dataset, tmp_path, service, monkeypatch)
    with pytest.raises(ApiError) as e:
        ops.import_folder(api, dataset, src, echo=quiet)
    assert e.value.status == 503 and no_versions(api, dataset)
    assert verify_rehash(monkeypatch, database, store) == 0
    assert not path.exists()  # moved to quarantine/, so the retry sends it again
    v = ops.import_folder(api, dataset, src, echo=quiet)
    assert all_frozen_versions_complete(service, database)
    cache = Cache(tmp_path / 'cache')
    try:
        ops.get(api, cache, v['ref'], tmp_path / 'out', echo=quiet)
    finally:
        cache.close()
    assert read_tree(tmp_path / 'out') == files


def test_verify_fails_when_it_cannot_lift_the_block_after_a_power_loss(api, dataset, tmp_path, service, database,
                                                                       monkeypatch, capsys):
    """Everything checks out, but the record of the old boot cannot be removed: uploads stay refused, so verify
    must fail instead of reporting the block as lifted."""
    from fwserver.store import UNSYNCED
    store = service.app.state.store
    src, files, path = _power_cut_during_an_import(api, dataset, tmp_path, service, monkeypatch)
    record = os.fspath(store.root / UNSYNCED)
    real_unlink = os.unlink

    def unlink(p, *args, **kwargs):
        if os.fspath(p) == record:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_unlink(p, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(os, 'unlink', unlink)
        assert verify_rehash(monkeypatch, database, store) == 1
    out = capsys.readouterr()
    assert '已解除' not in out.out and '没能解除写入阻断' in out.err
    with pytest.raises(ApiError) as e:
        ops.import_folder(api, dataset, src, echo=quiet)
    assert e.value.status == 503 and no_versions(api, dataset)
    assert verify_rehash(monkeypatch, database, store) == 0  # the disk works again
    assert '已解除' in capsys.readouterr().out
    ops.import_folder(api, dataset, src, echo=quiet)
    assert all_frozen_versions_complete(service, database)
