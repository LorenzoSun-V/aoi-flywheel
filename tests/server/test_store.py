"""The file store on its own (no database): when it accepts writes."""
import ctypes
import errno
import os

import pytest

from fwserver.store import FLAG, MARKER, Store, StoreError


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
