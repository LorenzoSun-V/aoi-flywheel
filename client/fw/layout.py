"""Laying cached objects out as the files of a version. Every placed file is hashed and compared with the
manifest, whatever the mode, so a damaged cache object can never end up in a training folder unnoticed.

Modes:
- reflink: copy-on-write clone (btrfs, XFS with reflink=1). Takes no extra space until a file is edited,
  and editing it never touches the cache.
- copy: an ordinary copy, used where reflink is not available (ext4, NTFS and most others).
- hardlink: only when asked for. The file IS the cache object: read-only, shared with the cache and with
  every other folder linked to it, so writing to it anyway (root ignores read-only) changes all of them.
'auto' (the default) tries reflink and falls back to copy.
- job: a worker's job folder, which is only read and is deleted when the job ends: reflink, else hardlink (no
  second copy of tens of thousands of images on ext4), else copy (another file system, no permission).
"""
from __future__ import annotations

import errno
import hashlib
import os
import sys
import threading
from pathlib import Path

from .contract import CHUNK

FICLONE = 0x40049409  # _IOW(0x94, 9, int) from linux/fs.h
_NO_HARDLINK = {errno.EXDEV, errno.EPERM, errno.EACCES, errno.EMLINK, errno.EOPNOTSUPP, errno.ENOSYS}
_NO_REFLINK = {errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV, errno.EINVAL, errno.ENOSYS}
MODE_NAMES = {'reflink': 'reflink（写时复制，改文件不影响缓存）', 'copy': '复制',
              'hardlink': '硬链接（只读，和缓存共用同一份数据，不要在这个目录里改文件）'}


def hash_file(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                return h.hexdigest()
            h.update(block)


def reflink(src: Path, dst: Path) -> bool:
    """Clone src into the new file dst. False, leaving no dst, when the file system cannot clone."""
    if sys.platform != 'linux':
        return False
    import fcntl
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    done = False
    try:
        with open(src, 'rb') as s:
            fcntl.ioctl(fd, FICLONE, s.fileno())
        done = True
    except OSError as e:
        if e.errno not in _NO_REFLINK:
            raise
    finally:
        os.close(fd)
        if not done:
            os.unlink(dst)
    return done


def copy_hashed(src: Path, dst: Path) -> str:
    """Copy src to the new file dst and return the sha256 of exactly the bytes written."""
    h = hashlib.sha256()
    with open(src, 'rb') as s, open(dst, 'xb') as d:
        while True:
            block = s.read(CHUNK)
            if not block:
                return h.hexdigest()
            h.update(block)
            d.write(block)


class Layout:
    def __init__(self, mode: str = 'auto'):
        if mode not in ('auto', 'copy', 'hardlink', 'job'):
            raise ValueError(mode)
        self.mode = mode
        self.used = set()
        self._lock = threading.Lock()

    def _note(self, mode: str):
        with self._lock:
            self.used.add(mode)

    def place(self, src: Path, dst: Path) -> str:
        """Create dst from the cache object src; returns the sha256 of what dst now holds."""
        if self.mode in ('auto', 'job'):
            if reflink(src, dst):
                self._note('reflink')
                return hash_file(dst)
            with self._lock:  # this file system cannot clone: everything else directly the next way
                # another thread may have switched already: only from the mode asked for, never copy -> hardlink
                self.mode = {'auto': 'copy', 'job': 'job-hardlink'}.get(self.mode, self.mode)
        if self.mode == 'job-hardlink':
            try:
                os.link(src, dst)
                self._note('hardlink')
                return hash_file(dst)
            except OSError as e:
                if e.errno not in _NO_HARDLINK:
                    raise
        if self.mode == 'hardlink':
            try:
                os.link(src, dst)
                self._note('hardlink')
                return hash_file(dst)
            except OSError as e:
                if e.errno != errno.EMLINK:
                    raise  # too many links to one inode (a very common file): copy that one instead
        self._note('copy')
        return copy_hashed(src, dst)

    def describe(self) -> str:
        return '、'.join(MODE_NAMES[m] for m in ('reflink', 'hardlink', 'copy') if m in self.used) or '-'
