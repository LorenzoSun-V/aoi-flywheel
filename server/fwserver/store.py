"""Content-addressed file store: objects/ab/cd/<sha256>, each written once, read-only, never edited.

Writing an object: a temporary file in tmp/ (same file system) -> hash checked while it is written ->
read-only -> linked to its final name (an existing object is not replaced unless it is damaged). Nothing
is fsynced per file. Before the database commits any row that references objects, flush() runs one
syncfs(2) on the store's file system, which makes the data AND the directory entries of every object
written so far durable; if syncfs reports an error the commit does not happen. One syncfs instead of a
file fsync plus a directory fsync per object is what keeps imports of 200k small files fast on an HDD.

Writes are allowed only while the store root holds WRITE-OK and no WRITE-ERROR. When syncfs reports an
error, the store stops accepting writes in three ways, so that the block holds even on a full disk:
1. in memory, for the rest of this process;
2. WRITE-OK is deleted (deleting needs no free space), which blocks every server process, also after a
   restart;
3. WRITE-ERROR records what happened, for people (needs a little space; written if it can be).
Only `fw-server verify --rehash` lifts the block: once every referenced object checks out, read back from
the disk, it makes everything durable and writes a new WRITE-OK; every process sees the new one. A new,
empty store gets WRITE-OK when it is first opened; a store with objects but no WRITE-OK (copied from
elsewhere, or the marker was lost) has to be verified first.
If the disk cannot even delete WRITE-OK (read-only or failing), only the in-memory block is left: after
repairing the disk, run verify --rehash before relying on the store again.

A power cut or a crash of the machine reports no error at all, yet loses whatever had not reached the disk:
an object written since the last syncfs can come back missing, short, or the right size full of zeros. So
before an object appears (or is moved aside), UNSYNCED is made durable with the machine's boot id, and the
syncfs that makes everything durable removes it again. An UNSYNCED that holds another boot id means the
machine went down with writes not yet on the disk: writes are refused until verify --rehash passes, as after
a syncfs error. A shared/exclusive file lock keeps the removal from racing a write in another process.
"""
from __future__ import annotations

import ctypes
import datetime
import errno
import fcntl
import hashlib
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from fw.contract import CHUNK

HEX64 = re.compile(r'[0-9a-f]{64}\Z')
HEX2 = re.compile(r'[0-9a-f]{2}\Z')
FLAG = 'WRITE-ERROR'  # present: writes refused; says what went wrong and when
MARKER = 'WRITE-OK'  # absent: writes refused; a new random content each time verify --rehash passes
UNSYNCED = 'UNSYNCED'  # present: objects written since the last syncfs, on the boot it names
LOCK = '.unsynced.lock'
_UNREADABLE = object()
log = logging.getLogger('fwserver.store')


class StoreError(RuntimeError):
    pass


def _boot_id() -> Optional[str]:
    """Changes every time the machine starts (Linux); the same inside and outside containers."""
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip() or None
    except OSError:
        return None


class BadContent(ValueError):
    pass


def _libc_syncfs():
    try:
        fn = ctypes.CDLL(None, use_errno=True).syncfs
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int]
    return fn


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def hash_path(path: Path, from_disk: bool = False) -> Tuple[str, int]:
    """sha256 and size of a file. from_disk: drop its pages from the page cache first, so that the bytes
    really come from the disk; after a write-back error the cache can still hold data the disk never got."""
    h = hashlib.sha256()
    size = 0
    with open(path, 'rb') as f:
        if from_disk and hasattr(os, 'posix_fadvise'):
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        while True:
            block = f.read(CHUNK)
            if not block:
                return h.hexdigest(), size
            h.update(block)
            size += len(block)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.objects = self.root / 'objects'
        self.tmp = self.root / 'tmp'
        self.quarantine = self.root / 'quarantine'
        for d in (self.objects, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        self._syncfs = _libc_syncfs()
        # Kept open for the life of the process: syncfs reports write-back errors that happened since this
        # descriptor was opened, so a descriptor opened just before the call could miss an earlier error.
        self._fd = os.open(self.root, os.O_RDONLY)
        self._lock = threading.Lock()
        self._failed: Optional[Tuple[str, object]] = None  # (what happened, WRITE-OK as it was then)
        self._boot = _boot_id()
        if self._marker() is None and not (self.root / FLAG).exists() and not any(self.objects.iterdir()):
            self._write_marker()  # a new, empty store: nothing has been written that could be lost

    def close(self):
        """A clean stop: make what was written durable, so that a restart of the machine afterwards needs no
        check. If that fails, the store is marked as after any syncfs error."""
        try:
            if self._unsynced() is not None:
                self.sync()
        except StoreError:
            pass
        finally:
            os.close(self._fd)

    # --- objects -------------------------------------------------------------------------------
    def path(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest[2:4] / digest

    def size(self, digest: str) -> Optional[int]:
        try:
            return self.path(digest).stat().st_size
        except FileNotFoundError:
            return None

    def read(self, digest: str) -> Optional[bytes]:
        try:
            return self.path(digest).read_bytes()
        except FileNotFoundError:
            return None

    def writer(self, digest: str) -> 'ObjectWriter':
        self.check_writable()
        return ObjectWriter(self, digest)

    def put_bytes(self, data: bytes) -> Tuple[str, bool]:
        digest = hashlib.sha256(data).hexdigest()
        w = self.writer(digest)
        w.write(data)
        return digest, w.commit()

    def put_verified(self, digest: str, data: bytes) -> bool:
        """Store one object received whole (batch uploads). The hash is checked before anything is written;
        returns True if the object is new or repaired. The caller checks check_writable() once per batch."""
        if hashlib.sha256(data).hexdigest() != digest:
            raise BadContent(f'上传的内容和哈希 {digest[:12]} 对不上（文件在传输中损坏，或上传期间被改过）')
        fd, tmp = tempfile.mkstemp(dir=self.tmp, prefix=digest[:16] + '.')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            return self.install(digest, Path(tmp))
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def install(self, digest: str, tmp: Path) -> bool:
        """Give the verified temporary file `tmp` its final name. Returns False if an intact object with
        that hash was already there; a damaged one (wrong size or content) is replaced."""
        os.chmod(tmp, 0o444)
        dst = self.path(digest)
        dst.parent.mkdir(parents=True, exist_ok=True)
        with self._writing():
            try:
                os.link(tmp, dst)  # never replaces: two uploads of the same object race safely
                return True
            except FileExistsError:
                pass
            st = dst.stat()
            if st.st_size == tmp.stat().st_size and hash_path(dst)[0] == digest:
                return False
            os.replace(tmp, dst)  # the existing copy was damaged: the verified upload repairs it
            return True

    # --- durability ----------------------------------------------------------------------------
    @contextmanager
    def _locked(self, exclusive: bool):
        # A descriptor of its own each time: flock is per open file, so threads of one process exclude each
        # other this way as well as processes do.
        fd = os.open(self.root / LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    def _unsynced(self) -> Optional[str]:
        """The boot id UNSYNCED names, None if there is none, '?' if it cannot be read."""
        try:
            return (self.root / UNSYNCED).read_text(encoding='utf-8').strip() or '?'
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            return '?'

    @contextmanager
    def _writing(self):
        """Around anything that makes an object appear or move: UNSYNCED is on the disk first."""
        with self._locked(exclusive=False):
            # One left by an earlier boot (or unreadable) stays as it is: it already blocks uploads, and only
            # verify --rehash, whose repairs and moves come through here too, may remove it.
            if self._boot is not None and self._unsynced() is None:
                self._write_unsynced()
            yield

    def _write_unsynced(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=f'.{UNSYNCED}.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(f'{self._boot}\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.root / UNSYNCED)
            os.fsync(self._fd)
        except OSError as e:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise StoreError(f'文件库记不下 {UNSYNCED}（{e}），不能写入') from None

    def _lost_writes(self) -> str:
        return (f'机器上次断电或死机时，文件库里还有没落盘的写入（{UNSYNCED}），那时写进来的文件可能已经坏了')

    def _clear_unsynced(self, any_boot: bool = False) -> None:
        """After a syncfs that succeeded: nothing written so far can be lost any more. Under the exclusive lock
        no object appears meanwhile, and one more syncfs covers what appeared since the first."""
        seen = self._unsynced()
        if seen is None or (seen != self._boot and not any_boot):
            return  # another boot's UNSYNCED stays until verify --rehash
        with self._locked(exclusive=True):
            self._syncfs_or_fail()
            try:
                os.unlink(self.root / UNSYNCED)
                os.fsync(self._fd)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning('could not remove %s (%s): the next restart of the machine will ask for a check',
                            UNSYNCED, e)

    def _marker(self) -> Optional[str]:
        try:
            return (self.root / MARKER).read_text(encoding='utf-8').strip() or None
        except (OSError, UnicodeDecodeError):
            return None  # missing or unreadable: writes are refused

    def _write_marker(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=f'.{MARKER}.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(f'{_now()} {secrets.token_hex(16)}\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.root / MARKER)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        os.fsync(self._fd)

    def _write_flag(self, text: str) -> None:
        with open(self.root / FLAG, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.fsync(self._fd)

    def _mark_failed(self, message: str) -> None:
        """Refuse writes from now on: in this process for sure, and for good as far as the disk still lets us."""
        message = f'{_now()} {message}'
        try:
            seen = (self.root / MARKER).read_text(encoding='utf-8').strip() or None
        except FileNotFoundError:
            seen = None
        except (OSError, UnicodeDecodeError):
            seen = _UNREADABLE  # a new marker could not be told from this one
        with self._lock:
            self._failed = (message, seen)
        log.critical('store write error, writes refused until fw-server verify --rehash passes: %s', message)
        try:
            os.unlink(self.root / MARKER)  # needs no free space: works on a full disk too
            removed = True
        except FileNotFoundError:
            removed = True
        except OSError as e:
            removed = False
            log.critical('could not delete %s (%s): the block holds only while this process runs', MARKER, e)
        if removed:
            with self._lock:
                if self._failed is not None and self._failed[0] == message:
                    self._failed = (message, None)  # any WRITE-OK from now on is a new one
            try:
                os.fsync(self._fd)
            except OSError as e:
                log.critical('could not make the removal of %s durable (%s)', MARKER, e)
        try:
            self._write_flag(message)
        except OSError as e:
            log.critical('could not write %s (%s)', FLAG, e)

    def problem(self) -> Optional[str]:
        """Why writes are refused right now, or None."""
        marker = self._marker()
        with self._lock:
            if self._failed is not None:
                message, seen = self._failed
                if seen is _UNREADABLE:
                    return f'文件库写盘出过错（{message}），连 {MARKER} 都读不了也删不掉；verify --rehash 通过后还要重启服务'
                if marker is None or marker == seen:
                    return f'文件库写盘出过错（{message}）'
                self._failed = None  # verify --rehash passed after the error: it wrote a new WRITE-OK
        flag = self.flag_text()
        if flag is not None:
            return f'文件库写盘出过错（{flag or "原因没能写下来"}）'
        seen = self._unsynced()
        if seen is not None and self._boot is not None and seen != self._boot:
            return self._lost_writes()
        if marker is None:
            return f'文件库缺少 {MARKER} 标记（写盘出过错，或者文件库是从别处拷来的，还没核对过）'
        return None

    def check_writable(self) -> None:
        problem = self.problem()
        if problem is not None:
            raise StoreError(f'{problem}；先检查磁盘，再在服务器上运行 fw-server verify --rehash，通过后才能继续写入')

    def sync(self) -> None:
        """Make every object written so far durable (data and directory entries), or refuse writes from now
        on and raise."""
        self._syncfs_or_fail()
        self._clear_unsynced()

    def _syncfs_or_fail(self) -> None:
        if self._syncfs is not None:
            if self._syncfs(self._fd) == 0:
                return
            message = f'syncfs: {os.strerror(ctypes.get_errno())}'
        elif hasattr(os, 'sync'):
            os.sync()  # not Linux: development only
            return
        else:
            raise StoreError('文件库只支持 Linux')
        self._mark_failed(message)
        raise StoreError(f'文件库写盘出错（{message}），没有提交；已标记为异常')

    def flush(self) -> None:
        """The barrier before anything that relies on the objects written so far (a version commit)."""
        self.check_writable()
        self.sync()

    def mark_verified(self) -> None:
        """Every referenced object checked out (fw-server verify --rehash): make everything durable, then let
        every server process write again."""
        self.sync()
        try:
            self._clear_unsynced(any_boot=True)  # also one left by an earlier boot: everything checked out
            self._write_marker()
            try:
                os.unlink(self.root / FLAG)
            except FileNotFoundError:
                pass
            os.fsync(self._fd)
        except OSError as e:
            raise StoreError(f'核对通过了，但没能解除写入阻断（{e}）；检查文件库所在的盘') from None
        with self._lock:
            self._failed = None

    def flag_text(self) -> Optional[str]:
        try:
            return (self.root / FLAG).read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as e:
            return f'{FLAG} 读不了：{e}'

    # --- maintenance ---------------------------------------------------------------------------
    def clean_tmp(self, older_than: float = 86400) -> int:
        """Remove temporary files left by interrupted uploads."""
        removed = 0
        limit = time.time() - older_than
        for p in self.tmp.iterdir():
            try:
                if p.stat().st_mtime < limit:
                    p.unlink()
                    removed += 1
            except FileNotFoundError:
                pass
        return removed

    def move_to_quarantine(self, digest: str) -> None:
        src = self.path(digest)
        self.quarantine.mkdir(exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        with self._writing():  # undone by a power cut before the next syncfs, the damaged file would be back
            try:
                os.replace(src, self.quarantine / f'{digest}.{stamp}')
            except FileNotFoundError:
                pass

    def repair_from(self, digest: str, other_root: Path) -> bool:
        """Copy an intact copy of `digest` from another store (for example the backup) into this one."""
        src = Path(other_root) / 'objects' / digest[:2] / digest[2:4] / digest
        if not src.is_file():
            return False
        fd, tmp = tempfile.mkstemp(dir=self.tmp)
        os.close(fd)
        try:
            shutil.copyfile(src, tmp)
            if hash_path(Path(tmp))[0] != digest:
                return False
            self.install(digest, Path(tmp))
            return True
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def iter_objects(self) -> Iterator[str]:
        """Every object in the store, walking objects/ab/cd/<sha256>. A folder that cannot be listed (or an
        entry whose type cannot be read) raises StoreError: a check that skipped it would pass without having
        seen the objects in it."""
        for a in self._subdirs(self.objects):
            for b in self._subdirs(a.path):
                for e in self._list(b.path):
                    if HEX64.match(e.name) and e.name[:2] == a.name and e.name[2:4] == b.name:
                        yield e.name

    @staticmethod
    def _list(path: str) -> List[os.DirEntry]:
        try:
            with os.scandir(path) as it:
                return list(it)
        except OSError as e:
            raise StoreError(f'读不了文件库目录 {path}（{e.strerror or e}），核对没有完成') from None

    def _subdirs(self, path) -> List[os.DirEntry]:
        out = []
        for e in self._list(path):
            if not HEX2.match(e.name):
                continue  # objects are only ever looked up under objects/ab/cd/
            try:
                if e.is_dir():  # follows a link: whatever is behind it is served, so it is checked too
                    out.append(e)
            except OSError as err:
                raise StoreError(f'读不了文件库目录 {e.path}（{err.strerror or err}），核对没有完成') from None
        return out


class ObjectWriter:
    """One upload: data goes to a temporary file while it is hashed; commit() checks the hash and installs."""

    def __init__(self, store: Store, digest: str):
        self.store = store
        self.digest = digest
        fd, tmp = tempfile.mkstemp(dir=store.tmp, prefix=digest[:16] + '.')
        self.tmp = Path(tmp)
        self.file = os.fdopen(fd, 'wb')
        self.hash = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> None:
        self.hash.update(data)
        self.file.write(data)
        self.size += len(data)

    def commit(self) -> bool:
        try:
            self.file.close()
            if self.hash.hexdigest() != self.digest:
                raise BadContent(f'上传的内容和哈希 {self.digest[:12]} 对不上（文件在传输中损坏，或上传期间被改过）')
            return self.store.install(self.digest, self.tmp)
        finally:
            self.abort()

    def abort(self) -> None:
        if not self.file.closed:
            self.file.close()
        try:
            os.unlink(self.tmp)
        except FileNotFoundError:
            pass
        except OSError as e:  # pragma: no cover
            if e.errno != errno.ENOENT:
                raise
