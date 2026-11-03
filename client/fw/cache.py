"""Local cache of one machine: downloaded objects, stored by sha256 under objects/ab/cd/<sha256>.

An object enters the cache only after its download matched its hash. The index keeps each object's size and
mtime from that moment; an object whose size or mtime has changed since is dropped and downloaded again.
That check is only a shortcut: every file `fw get` lays out is hashed again against the manifest, so a
cache object that was changed some other way is still caught (see layout.py).
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


def remove_file(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)  # Windows cannot delete a read-only file
        os.unlink(path)
    except FileNotFoundError:
        pass


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
        self.dropped = 0

    def close(self):
        with self._lock:
            self._db.close()

    def object_path(self, digest: str) -> Path:
        return self.root / 'objects' / digest[:2] / digest[2:4] / digest

    def stats(self) -> Tuple[int, int]:
        with self._lock:
            count, total = self._db.execute('SELECT count(*), coalesce(sum(size), 0) FROM objects').fetchone()
        return count, total

    def drop(self, digest: str) -> None:
        remove_file(self.object_path(digest))
        with self._lock:
            self.dropped += 1
            self._db.execute('DELETE FROM objects WHERE sha256 = ?', (digest,))

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
        """The cached object, if present and its size and mtime are as downloaded."""
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
        self.drop(digest)
        return None

    def fetch(self, api, digest: str) -> Path:
        """Return the cached object, downloading it (hash checked before it enters the cache) if needed."""
        path = self.cached(digest)
        if path is not None:
            return path
        path = self.object_path(digest)
        fd, tmp = tempfile.mkstemp(dir=str(self.root / 'tmp'))
        try:
            with os.fdopen(fd, 'w+b') as f:
                size = api.download(digest, f)
            os.chmod(tmp, stat.S_IREAD)
            path.parent.mkdir(parents=True, exist_ok=True)
            remove_file(path)
            os.replace(tmp, path)
        except BaseException:
            remove_file(Path(tmp))
            raise
        st = path.stat()
        with self._lock:
            self._db.execute('INSERT OR REPLACE INTO objects VALUES (?, ?, ?)', (digest, st.st_size, st.st_mtime_ns))
            self.downloaded_files += 1
            self.downloaded_bytes += size
        return path
