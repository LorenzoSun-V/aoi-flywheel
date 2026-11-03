"""Local cache of one machine: downloaded objects, stored by sha256 under objects/ab/cd/<sha256>.

An object enters the cache only after its download matched its hash. The index keeps each object's size and
mtime from that moment; an object whose size or mtime has changed since is downloaded again, and the new copy
replaces it under the same name. Nothing in the cache is deleted because of what the index says: several fw
processes may share one cache, and the row one of them has just read may already be out of date (another
process may have installed a fresh copy since). The size and mtime check is only a shortcut: every file
`fw get` lays out is hashed again against the manifest, so a cache object that was changed some other way is
still caught (see layout.py).
"""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path
from typing import Optional, Tuple

from .config import cache_dir
from .layout import hash_file


def remove_file(path: Path) -> None:
    """Delete a cache object (or a file just laid out). The file's mode is left alone except on Windows,
    which cannot delete a read-only file: elsewhere a hardlinked copy in a fetched folder shares the mode,
    and must stay read-only (fw get --hardlink is refused on Windows for this reason)."""
    try:
        if os.name == 'nt':
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        os.unlink(path)
    except FileNotFoundError:
        pass


def install_file(tmp: str, path: Path, digest: str) -> None:
    """Give a verified download its name in the cache. Another fw process sharing the cache may be reading
    or installing the same object at this moment, so the name never goes missing on the way: no delete
    before the rename."""
    os.chmod(tmp, stat.S_IREAD)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(tmp, path)  # atomic; outside Windows it replaces a read-only file as well
        return
    except PermissionError:
        if os.name != 'nt':
            raise
    # Windows does not replace a read-only (or open) file. An intact copy that is already there will do.
    try:
        intact = hash_file(path) == digest
    except FileNotFoundError:
        intact = False
    if intact:
        remove_file(Path(tmp))
        return
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    except FileNotFoundError:
        pass
    try:
        os.replace(tmp, path)
    except BaseException:
        try:
            os.chmod(path, stat.S_IREAD)
        except OSError:
            pass
        raise


class Cache:
    def __init__(self, root=None):
        self.root = Path(root) if root else cache_dir()
        (self.root / 'tmp').mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.root / 'index.sqlite'), timeout=120, check_same_thread=False,
                                   isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('CREATE TABLE IF NOT EXISTS objects '
                             '(sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL)')
        self.downloaded_files = 0
        self.downloaded_bytes = 0

    def close(self):
        with self._lock:
            self._db.close()

    def object_path(self, digest: str) -> Path:
        return self.root / 'objects' / digest[:2] / digest[2:4] / digest

    def stats(self) -> Tuple[int, int]:
        with self._lock:
            count, total = self._db.execute('SELECT count(*), coalesce(sum(size), 0) FROM objects').fetchone()
        return count, total

    def clear(self) -> int:
        """Drop every cached object; folders already laid out keep their files."""
        removed = 0
        objects = self.root / 'objects'
        if objects.is_dir():
            for dirpath, _, names in os.walk(objects, topdown=False):
                for n in names:
                    remove_file(Path(dirpath) / n)
                    removed += 1
                os.rmdir(dirpath)
        with self._lock:
            self._db.execute('DELETE FROM objects')
        return removed

    def cached(self, digest: str) -> Optional[Path]:
        """The cached object, if present and its size and mtime are as downloaded. Otherwise None, and the
        file is left where it is: by the time it is looked at, another fw process may have replaced it with a
        fresh copy (and the row read here is out of date), so a mismatch is a reason to download again, never
        to delete."""
        path = self.object_path(digest)
        with self._lock:
            row = self._db.execute('SELECT size, mtime_ns FROM objects WHERE sha256 = ?', (digest,)).fetchone()
        if row is None:
            return None
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        if (st.st_size, st.st_mtime_ns) == (row[0], row[1]):
            return path
        return None

    def fetch(self, api, digest: str, force: bool = False) -> Path:
        """Return the cached object, downloading it (hash checked before it enters the cache) if needed.
        force: download it even if the cache seems to have it (the cached copy did not match the manifest).
        Either way the new copy replaces the old one atomically, so the name is never missing for a process
        that is reading it."""
        if not force:
            path = self.cached(digest)
            if path is not None:
                return path
        path = self.object_path(digest)
        fd, tmp = tempfile.mkstemp(dir=str(self.root / 'tmp'))
        try:
            with os.fdopen(fd, 'w+b') as f:
                size = api.download(digest, f)
            install_file(tmp, path, digest)
        except BaseException:
            remove_file(Path(tmp))
            raise
        st = path.stat()
        with self._lock:
            self._db.execute('INSERT OR REPLACE INTO objects VALUES (?, ?, ?)', (digest, st.st_size, st.st_mtime_ns))
            self.downloaded_files += 1
            self.downloaded_bytes += size
        return path
