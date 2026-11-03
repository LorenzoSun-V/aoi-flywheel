"""Content-addressed file store: objects/ab/cd/<sha256>, each written once, read-only, never edited.

Writing an object: a temporary file in tmp/ (same file system) -> hash checked while it is written ->
read-only -> linked to its final name (an existing object is not replaced unless it is damaged). Nothing
is fsynced per file. Before the database commits any row that references objects, flush() runs one
syncfs(2) on the store's file system, which makes the data AND the directory entries of every object
written so far durable; if syncfs reports an error the commit does not happen. One syncfs instead of a
file fsync plus a directory fsync per object is what keeps imports of 200k small files fast on an HDD.

A write error reported by syncfs leaves WRITE-ERROR in the store root. While it is there no upload and no
new version is accepted; `fw-server verify --rehash` removes it once every referenced object checks out.
"""
from __future__ import annotations

import ctypes
import datetime
import errno
import hashlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional, Tuple

from fw.contract import CHUNK

HEX64 = re.compile(r'[0-9a-f]{64}\Z')
FLAG = 'WRITE-ERROR'


class StoreError(RuntimeError):
    pass


class BadContent(ValueError):
    pass


def _libc_syncfs():
    try:
        fn = ctypes.CDLL(None, use_errno=True).syncfs
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int]
    return fn


def hash_path(path: Path) -> Tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, 'rb') as f:
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

    def close(self):
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

    def install(self, digest: str, tmp: Path) -> bool:
        """Give the verified temporary file `tmp` its final name. Returns False if an intact object with
        that hash was already there; a damaged one (wrong size or content) is replaced."""
        os.chmod(tmp, 0o444)
        dst = self.path(digest)
        dst.parent.mkdir(parents=True, exist_ok=True)
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
    def check_writable(self) -> None:
        flag = self.root / FLAG
        if flag.exists():
            raise StoreError(f'文件库写盘出过错（{flag.read_text(encoding="utf-8").strip()}）；先检查磁盘，'
                             f'再在服务器上运行 fw-server verify --rehash，通过后才能继续写入')

    def flush(self) -> None:
        """Make every object written so far durable (data and directory entries), or raise."""
        self.check_writable()
        if self._syncfs is not None:
            if self._syncfs(self._fd) == 0:
                return
            err = ctypes.get_errno()
            message = f'syncfs: {os.strerror(err)}'
        elif hasattr(os, 'sync'):
            os.sync()  # not Linux: development only
            return
        else:
            raise StoreError('文件库只支持 Linux')
        stamp = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        (self.root / FLAG).write_text(f'{stamp} {message}\n', encoding='utf-8')
        raise StoreError(f'文件库写盘出错（{message}），没有提交；已标记为异常')

    def clear_flag(self) -> bool:
        flag = self.root / FLAG
        if flag.exists():
            flag.unlink()
            return True
        return False

    def flag_text(self) -> Optional[str]:
        flag = self.root / FLAG
        return flag.read_text(encoding='utf-8').strip() if flag.exists() else None

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
        for dirpath, _, names in os.walk(self.objects):
            for n in names:
                if HEX64.match(n):
                    yield n


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
