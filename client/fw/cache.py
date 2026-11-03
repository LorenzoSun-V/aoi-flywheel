"""Local cache of one machine: downloaded objects, stored by sha256 under objects/ab/cd/<sha256>.

An object enters the cache only after its download matched its hash. The index keeps each object's size and
mtime from that moment; an object whose size or mtime has changed since is dropped and downloaded again.
That check is only a shortcut: every file `fw get` lays out is hashed again against the manifest, so a
cache object that was changed some other way is still caught (see layout.py). For the same reason the
index is not synced to disk on every write (synchronous=NORMAL): after a power cut it may be missing the
last few objects (they are downloaded again) or describe an object whose data was lost (the hash check
when it is laid out catches that).
"""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional, Tuple

from .config import cache_dir
from .contract import BATCH_OBJECT_MAX, batches
from .layout import hash_file

FETCH_COUNT = 2000          # objects per batch download
FETCH_BYTES = 32 << 20      # bytes per batch download


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
            self._db.execute('PRAGMA synchronous=NORMAL')
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

    def cached_many(self, digests: Iterable[str]) -> Dict[str, Path]:
        """cached() for many objects at once: one index query per 500 objects instead of one per object."""
        digests = list(digests)
        rows = {}
        with self._lock:
            for i in range(0, len(digests), 500):
                part = digests[i:i + 500]
                rows.update((r[0], (r[1], r[2])) for r in self._db.execute(
                    f'SELECT sha256, size, mtime_ns FROM objects WHERE sha256 IN ({",".join("?" * len(part))})', part))
        out = {}
        for digest in digests:
            row = rows.get(digest)
            if row is None:
                continue
            path = self.object_path(digest)
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if (st.st_size, st.st_mtime_ns) == row:
                out[digest] = path
            else:
                self.drop(digest)
        return out

    def _install(self, digest: str, data: bytes) -> Tuple[str, int, int]:
        """Put verified bytes in the cache under their hash; returns the index row."""
        path = self.object_path(digest)
        fd, tmp = tempfile.mkstemp(dir=str(self.root / 'tmp'))
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            install_file(tmp, path, digest)
        except BaseException:
            remove_file(Path(tmp))
            raise
        st = path.stat()
        return digest, st.st_size, st.st_mtime_ns

    def fetch_many(self, api, objects: List[Tuple[str, int]], workers: int = 4, progress=None) -> Dict[str, Path]:
        """Make sure every (sha256, size) is in the cache and return sha256 -> cached path. Small objects come
        in batches of up to FETCH_COUNT per request, large ones one by one; every object is checked against
        its hash before it enters the cache."""
        sizes = dict(objects)
        paths = self.cached_many(sizes)
        if progress:
            progress.add(len(paths))
        todo = sorted(d for d in sizes if d not in paths)
        small = [(d, sizes[d]) for d in todo if sizes[d] <= BATCH_OBJECT_MAX]
        large = [d for d in todo if sizes[d] > BATCH_OBJECT_MAX]

        def run_batch(batch):
            rows = []

            def on_object(digest, data):
                rows.append(self._install(digest, data))
                if progress:
                    progress.add()

            try:
                api.fetch_objects(batch, on_object)
            finally:  # objects already received stay in the cache even if the batch failed later
                with self._lock:
                    self._db.executemany('INSERT OR REPLACE INTO objects VALUES (?, ?, ?)', rows)
                    self.downloaded_files += len(rows)
                    self.downloaded_bytes += sum(r[1] for r in rows)
                    for digest, _, _ in rows:
                        paths[digest] = self.object_path(digest)

        def run_large(digest):
            paths[digest] = self.fetch(api, digest)
            if progress:
                progress.add()

        jobs = [(run_batch, b) for b in batches(small, FETCH_COUNT, FETCH_BYTES)] + [(run_large, d) for d in large]
        if workers <= 1 or len(jobs) <= 1:
            for fn, arg in jobs:
                fn(arg)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for _ in pool.map(lambda job: job[0](job[1]), jobs):
                    pass
        return paths

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
