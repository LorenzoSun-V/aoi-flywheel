"""Checks run on a folder before it becomes a dataset version (same rules as the pilot's intake script)."""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Tuple

from .contract import collisions, path_problem

IMAGE_EXTS = {'.bmp', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp'}
LABEL_EXTS = {'.json', '.txt'}  # LiteAnno / X-AnyLabeling JSON, YOLO txt
META_NAMES = {'classes.txt', 'classes.names', 'obj.names', 'data.yaml', 'dataset.yaml', 'mask_colors.json'}
JUNK_NAMES = {'thumbs.db', 'desktop.ini', '.ds_store'}


@dataclass
class FolderReport:
    root: Path
    files: List[Tuple[str, Path]] = field(default_factory=list)  # (relative posix path, absolute path)
    junk: List[str] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    meta: List[str] = field(default_factory=list)
    others: List[str] = field(default_factory=list)
    bad_paths: List[str] = field(default_factory=list)
    collisions: List[str] = field(default_factory=list)
    same_stem_images: List[List[str]] = field(default_factory=list)
    unlabeled_images: List[str] = field(default_factory=list)
    orphan_labels: List[str] = field(default_factory=list)
    same_name_in_dirs: List[List[str]] = field(default_factory=list)  # warning only: one name in two folders

    def blocking(self, allow_other_files: bool, force: bool) -> List[str]:
        """Reasons the folder must not become a version as it stands."""
        reasons = []
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


def scan_folder(folder) -> FolderReport:
    root = Path(folder)
    if not root.is_dir():
        raise FileNotFoundError(f'目录不存在：{root}')
    report = FolderReport(root=root)
    for dirpath, dirnames, names in os.walk(root):
        dirnames.sort()
        for name in sorted(names):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if name.lower() in JUNK_NAMES:
                report.junk.append(rel)
                continue
            if path.is_symlink() or not path.is_file():
                report.bad_paths.append(f'{rel}：是链接或特殊文件，先换成普通文件')
                continue
            report.files.append((rel, path))
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
