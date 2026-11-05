"""fw-server verify: finds missing and damaged objects, repairs from a backup copy, quarantines the rest."""
import ctypes
import errno
import shutil
import uuid

import pytest

from helpers import quiet, write
from fw import ops
from fw.contract import sha256_file
from fwserver import cli
from fwserver import datasets as ds
from fwserver import db, models
from fwserver.store import Store, StoreError


@pytest.fixture
def version(api, tmp_path):
    name = f'V-{uuid.uuid4().hex[:6]}'
    api.create_dataset(name, '', '', 'pool', '')
    for rel, data in {'a.bmp': b'aaaa', 'a.json': b'{"a": 1}', 'b.bmp': b'bbbb'}.items():
        write(tmp_path / 'src', rel, data)
    return ops.import_folder(api, name, tmp_path / 'src', echo=quiet)


def run_verify(database, store, **kw):
    with db.connect(database) as c:
        return ds.verify(c, store, echo=quiet, more=models.verify_inputs, **kw)


def test_verify_finds_and_repairs(api, version, service, database, tmp_path):
    store = service.app.state.store
    assert run_verify(database, store, rehash=True) == []
    from fw.contract import sha256_file
    digest_a, _ = sha256_file(tmp_path / 'src' / 'a.bmp')
    digest_b, _ = sha256_file(tmp_path / 'src' / 'b.bmp')
    backup = tmp_path / 'backup'
    for d in (digest_a, digest_b):
        dst = backup / 'objects' / d[:2] / d[2:4] / d
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(store.path(d), dst)
    # damage one in place (same size) and lose another
    pa = store.path(digest_a)
    pa.chmod(0o644)
    pa.write_bytes(b'aaaX')
    store.path(digest_b).unlink()
    assert len(run_verify(database, store)) == 1  # without rehash only the missing one shows
    assert run_verify(database, store, rehash=True, repair_from=backup) == []
    assert store.path(digest_a).read_bytes() == b'aaaa' and store.path(digest_b).read_bytes() == b'bbbb'
    # no backup: the damaged object is quarantined and the next import of the folder brings it back
    pa = store.path(digest_a)
    pa.chmod(0o644)
    pa.write_bytes(b'aaaY')
    problems = run_verify(database, store, rehash=True)
    assert len(problems) == 1 and 'quarantine' in problems[0]
    assert api.missing([(digest_a, 4)]) == [digest_a]
    again = ops.import_folder(api, version['dataset'], tmp_path / 'src', echo=quiet)
    assert again['id'] == version['id']
    assert run_verify(database, store, rehash=True) == []


def damaged_with_backup(store, tmp_path):
    """Damage a.bmp's object in place (same size) and keep an intact copy in a backup store."""
    digest, _ = sha256_file(tmp_path / 'src' / 'a.bmp')
    backup = tmp_path / 'backup'
    dst = backup / 'objects' / digest[:2] / digest[2:4] / digest
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(store.path(digest), dst)
    store.path(digest).chmod(0o644)
    store.path(digest).write_bytes(b'aaaZ')
    return digest, backup


def test_repairs_are_on_disk_before_verify_reports(version, service, database, tmp_path, monkeypatch):
    store = Store(service.app.state.store.root)  # like fw-server verify: its own process
    try:
        digest, backup = damaged_with_backup(store, tmp_path)
        events = []
        real_install, real_sync = store.install, store.sync
        monkeypatch.setattr(store, 'install', lambda d, tmp: events.append('install') or real_install(d, tmp))
        monkeypatch.setattr(store, 'sync', lambda: events.append('sync') or real_sync())
        assert run_verify(database, store, rehash=True, repair_from=backup) == []
        # write back what was pending, rehash from the disk, repair, and make the repair durable
        assert events == ['sync', 'install', 'sync']
        assert store.path(digest).read_bytes() == b'aaaa'
    finally:
        store.close()


def test_repair_that_does_not_reach_the_disk_fails_and_blocks(version, service, database, tmp_path, monkeypatch):
    store = Store(service.app.state.store.root)
    try:
        digest, backup = damaged_with_backup(store, tmp_path)
        calls = []
        real_syncfs = store._syncfs

        def syncfs(fd):  # the second syncfs, after the repair, reports a write-back error
            calls.append(fd)
            if len(calls) == 2:
                ctypes.set_errno(errno.EIO)
                return -1
            return real_syncfs(fd)

        monkeypatch.setattr(store, '_syncfs', syncfs)
        with pytest.raises(StoreError):
            run_verify(database, store, rehash=True, repair_from=backup)
        assert service.app.state.store.problem() is not None  # the running service refuses writes too
    finally:
        store.close()
    monkeypatch.setenv('FW_DSN', database)
    monkeypatch.setenv('FW_STORE', str(service.app.state.store.root))
    assert cli.main(['verify', '--rehash']) == 0
    assert service.app.state.store.problem() is None
