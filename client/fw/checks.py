"""Checks run on a folder before it becomes a dataset version (same rules as the pilot's intake script)."""
from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from .contract import IMAGE_EXTS, JUNK_NAMES, blocking_reasons, classify_paths  # noqa: F401  (IMAGE_EXTS: ops)
_NAME_SURROGATE = 0x20000000  # Windows reparse tags with this bit lead somewhere else: symlinks, junctions


@dataclass
class FolderReport:
    root: Path
    files: List[Tuple[str, Path]] = field(default_factory=list)  # (relative posix path, absolute path)
    junk: List[str] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    meta: List[str] = field(default_factory=list)
    others: List[str] = field(default_factory=list)
    unreadable: List[str] = field(default_factory=list)  # folders (or entries) the scan could not read
    links: List[str] = field(default_factory=list)  # symlinks, junctions and special files, never followed
    bad_paths: List[str] = field(default_factory=list)  # names that do not work on Windows or Linux
    collisions: List[str] = field(default_factory=list)
    same_stem_images: List[List[str]] = field(default_factory=list)
    unlabeled_images: List[str] = field(default_factory=list)
    orphan_labels: List[str] = field(default_factory=list)
    same_name_in_dirs: List[List[str]] = field(default_factory=list)  # warning only: one name in two folders

    def blocking(self, allow_other_files: bool, force: bool) -> List[str]:
        """Reasons the folder must not become a version as it stands."""
        reasons = []
        if self.unreadable:
            reasons.append('有目录读不了，里面的文件会漏掉，新版本里就成了删除；先修好权限或磁盘')
        if self.links:
            reasons.append('有链接或特殊文件，只收普通文件和目录；先换成普通文件和目录')
        return reasons + blocking_reasons(vars(self), allow_other_files, force)


def _is_link(entry: os.DirEntry) -> bool:
    """A symlink, or on Windows any reparse point that leads to another path (junctions as well)."""
    if entry.is_symlink():
        return True
    if sys.platform == 'win32':
        st = entry.stat(follow_symlinks=False)
        return bool(getattr(st, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    and getattr(st, 'st_reparse_tag', 0) & _NAME_SURROGATE)
    return False


def _reason(e: OSError) -> str:
    return e.strerror or str(e)


def _walk(root: Path, report: FolderReport) -> None:
    """Collect every regular file under root. Nothing is skipped quietly: a folder that cannot be read, a
    link (to a folder or a file) and a special file each go into the report, because a file the scan
    missed would look deleted in the new version."""
    pending = ['']
    while pending:
        folder = pending.pop()
        try:
            with os.scandir(root / folder if folder else root) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            report.unreadable.append(f'{folder or "."}/：读不了（{_reason(e)}）')
            continue
        subfolders = []
        for entry in entries:
            rel = f'{folder}/{entry.name}' if folder else entry.name
            try:
                if _is_link(entry):
                    kind = '目录' if entry.is_dir() else '文件'
                    report.links.append(f'{rel}：是链接（指向{kind}），先换成普通的{kind}')
                elif entry.is_dir(follow_symlinks=False):
                    subfolders.append(rel)
                elif not entry.is_file(follow_symlinks=False):
                    report.links.append(f'{rel}：是特殊文件，先换成普通文件')
                elif entry.name.lower() in JUNK_NAMES:
                    report.junk.append(rel)
                else:
                    report.files.append((rel, Path(entry.path)))
            except OSError as e:
                report.unreadable.append(f'{rel}：读不了（{_reason(e)}）')
        pending.extend(reversed(subfolders))  # depth first, in name order


def scan_folder(folder) -> FolderReport:
    root = Path(folder)
    if not root.is_dir():
        raise FileNotFoundError(f'目录不存在：{root}')
    report = FolderReport(root=root)
    _walk(root, report)
    found = classify_paths(rel for rel, _ in report.files)
    for key, value in found.items():
        setattr(report, key, value)
    return report
