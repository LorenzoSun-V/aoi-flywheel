"""The file store on its own (no database): when it accepts writes."""
import ctypes
import errno
import os

import pytest

from fwserver.store import FLAG, MARKER, UNSYNCED, Store, StoreError


def failing_syncfs(fd):
    ctypes.set_errno(errno.EIO)
    return -1


def test_new_store_is_writable_but_copied_in_objects_need_verify(tmp_path):
    s = Store(tmp_path / 's')
    try:
        assert s.problem() is None
        s.put_bytes(b'x')
    finally:
        s.close()
    os.unlink(tmp_path / 's' / MARKER)  # for example objects copied in from elsewhere, never checked here
    s = Store(tmp_path / 's')
    try:
        with pytest.raises(StoreError, match=MARKER):
            s.check_writable()
        s.mark_verified()
        s.check_writable()
    finally:
        s.close()


def test_every_process_sees_the_block_and_its_end(tmp_path):
    a, b = Store(tmp_path / 's'), Store(tmp_path / 's')
    try:
        a.put_bytes(b'x')
        a._syncfs = failing_syncfs
        with pytest.raises(StoreError):
            a.flush()
        assert (tmp_path / 's' / FLAG).exists() and not (tmp_path / 's' / MARKER).exists()
        for s in (a, b):
            with pytest.raises(StoreError):
                s.check_writable()
        with pytest.raises(StoreError):
            b.put_bytes(b'y')
        c = Store(tmp_path / 's')  # a restarted process
        try:
            assert c.problem() is not None
            c.mark_verified()  # what fw-server verify --rehash does once everything checks out
        finally:
            c.close()
        a._syncfs = b._syncfs
        for s in (a, b):
            s.check_writable()
        a.flush()
        assert not (tmp_path / 's' / FLAG).exists()
    finally:
        a.close()
        b.close()


def test_rehash_reads_from_the_disk(tmp_path, monkeypatch):
    from fwserver import store as store_module
    advice = []
    monkeypatch.setattr(os, 'posix_fadvise', lambda fd, offset, length, how: advice.append(how), raising=False)
    path = tmp_path / 'f'
    path.write_bytes(b'abc')
    store_module.hash_path(path)
    assert advice == []
    store_module.hash_path(path, from_disk=True)
    assert advice == [os.POSIX_FADV_DONTNEED]


@pytest.mark.parametrize('level', [0, 1, 2])
def test_listing_objects_fails_on_a_folder_it_cannot_read(tmp_path, monkeypatch, level):
    s = Store(tmp_path / 's')
    try:
        digests = {s.put_bytes(bytes([i]) * 10)[0] for i in range(3)}
        (s.objects / 'README').write_text('not an object')  # entries outside objects/ab/cd/<sha256> are no objects
        assert set(s.iter_objects()) == digests
        d = min(digests)
        target = [s.objects, s.objects / d[:2], s.objects / d[:2] / d[2:4]][level]
        real_scandir = os.scandir

        def scandir(p='.'):
            if os.fspath(p) == str(target):
                raise OSError(errno.EIO, os.strerror(errno.EIO), os.fspath(p))
            return real_scandir(p)

        monkeypatch.setattr(os, 'scandir', scandir)
        with pytest.raises(StoreError, match='核对没有完成'):
            list(s.iter_objects())
    finally:
        s.close()


def _store_on_boot(monkeypatch, root, boot):
    from fwserver import store as store_module
    monkeypatch.setattr(store_module, '_boot_id', lambda: boot)
    return Store(root)


def _lose_power(s):
    os.close(s._fd)  # the process is gone without a clean stop; the page cache with it


def test_a_write_is_on_record_before_the_object_appears(tmp_path, monkeypatch):
    s = _store_on_boot(monkeypatch, tmp_path / 's', 'boot-1')
    real_link = os.link
    seen = []

    def link(src, dst, *args, **kwargs):
        seen.append((tmp_path / 's' / UNSYNCED).read_text().strip())
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, 'link', link)
    s.put_bytes(b'abc')
    assert seen == ['boot-1']
    s.sync()  # now nothing can be lost: the record goes
    assert not (tmp_path / 's' / UNSYNCED).exists()
    s.close()


def test_a_restart_with_writes_not_yet_on_disk_blocks_until_verified(tmp_path, monkeypatch):
    s = _store_on_boot(monkeypatch, tmp_path / 's', 'boot-1')
    digest, _ = s.put_bytes(b'abc')
    _lose_power(s)
    s = _store_on_boot(monkeypatch, tmp_path / 's', 'boot-2')
    try:
        assert UNSYNCED in s.problem()
        with pytest.raises(StoreError, match='verify --rehash'):
            s.writer(digest)
        s.move_to_quarantine(digest)  # what verify does with a damaged file: allowed, the record stays
        assert (tmp_path / 's' / UNSYNCED).read_text().strip() == 'boot-1'
        s.mark_verified()
        assert s.problem() is None and not (tmp_path / 's' / UNSYNCED).exists()
        s.put_bytes(b'abc')
    finally:
        s.close()


def test_a_restart_after_everything_reached_the_disk_needs_no_check(tmp_path, monkeypatch):
    s = _store_on_boot(monkeypatch, tmp_path / 's', 'boot-1')
    s.put_bytes(b'abc')
    s.sync()
    _lose_power(s)
    s = _store_on_boot(monkeypatch, tmp_path / 's', 'boot-2')
    assert s.problem() is None
    s.close()


def test_a_clean_stop_makes_writes_durable(tmp_path, monkeypatch):
    s = _store_on_boot(monkeypatch, tmp_path / 's', 'boot-1')
    s.put_bytes(b'abc')
    s.close()
    assert not (tmp_path / 's' / UNSYNCED).exists()
    s = _store_on_boot(monkeypatch, tmp_path / 's', 'boot-2')
    assert s.problem() is None
    s.close()


def test_the_record_is_not_removed_while_another_process_writes(tmp_path, monkeypatch):
    import threading
    a = _store_on_boot(monkeypatch, tmp_path / 's', 'boot-1')
    b = Store(tmp_path / 's')
    writing = a._writing()
    writing.__enter__()  # a is between "on record" and "object installed"
    done = threading.Event()
    t = threading.Thread(target=lambda: (b.sync(), done.set()))
    t.start()
    try:
        assert not done.wait(0.3)  # b's sync cannot clear the record under a's write
        assert (tmp_path / 's' / UNSYNCED).exists()
    finally:
        writing.__exit__(None, None, None)
    t.join(10)
    assert done.is_set() and not (tmp_path / 's' / UNSYNCED).exists()
    a.close()
    b.close()
