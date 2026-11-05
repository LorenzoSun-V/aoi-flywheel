"""Checks run on a folder before it becomes a dataset version (same rules as the pilot's intake script)."""
from __future__ import annotations

import os
import stat
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Tuple

from .contract import IMAGE_EXTS, LABEL_EXTS, META_NAMES, collisions, path_problem
JUNK_NAMES = {'thumbs.db', 'desktop.ini', '.ds_store'}
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
        if self.bad_paths:
            reasons.append('有文件名在 Windows 或 Linux 上用不了，先改名')
        if self.collisions:
            reasons.append('有只差大小写、或同名的文件和目录，在 Windows 上会互相覆盖，先改名')
        if self.same_stem_images and not force:
            reasons.append('有同名不同扩展名的图片（比如 a.bmp 和 a.png），标注对不上；先清理，或确认无妨后加 --force')
        if self.others and not allow_other_files:
            reasons.append('有图片和标注以外的文件；先清理，或确认要一起入库后加 --allow-other-files')
        return reasons


def _stem(rel: str) -> str:
    suffix = PurePosixPath(rel).suffix
    return rel[: len(rel) - len(suffix)] if suffix else rel


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
    image_stems: Dict[str, List[str]] = defaultdict(list)
    label_stems = set()
    for rel, _ in report.files:
        problem = path_problem(rel)
        if problem:
            report.bad_paths.append(f'{rel}：{problem}')
        name = PurePosixPath(rel).name.lower()
        ext = PurePosixPath(rel).suffix.lower()
        if ext in IMAGE_EXTS:
            report.images.append(rel)
            image_stems[_stem(rel)].append(rel)
        elif name in META_NAMES:
            report.meta.append(rel)
        elif ext in LABEL_EXTS:
            report.labels.append(rel)
            label_stems.add(_stem(rel))
        else:
            report.others.append(rel)
    report.collisions = collisions(rel for rel, _ in report.files)
    report.same_stem_images = [v for v in image_stems.values() if len(v) > 1]
    report.unlabeled_images = sorted(s for s in image_stems if s not in label_stems)
    report.orphan_labels = sorted(s for s in label_stems if s not in image_stems)
    by_name: Dict[str, List[str]] = defaultdict(list)
    for rel in report.images:
        by_name[PurePosixPath(_stem(rel)).name.lower()].append(rel)
    report.same_name_in_dirs = [v for _, v in sorted(by_name.items()) if len({PurePosixPath(r).parent for r in v}) > 1]
    return report
