"""What the fw commands do. Training scripts can call these too (see fw.get_version)."""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import config
from .api import Api
from .cache import Cache, remove_file
from .checks import IMAGE_EXTS, scan_folder
from .contract import (BATCH_OBJECT_MAX, TRAINSET_FORMATS, Entry, batches, collisions, diff_entries, manifest_bytes,
                       manifest_hash, parse_manifest, path_problem, sha256_file, trainset_layout)
from .layout import Layout
from .pool import run_all

Echo = Callable[[str], None]
Confirm = Callable[[str], bool]

UPLOAD_COUNT = 2000       # objects per batch upload
UPLOAD_BYTES = 32 << 20   # bytes per batch upload
BIG_FILE = 256 << 10      # threads pay off only when the average file is at least this big


class FwError(Exception):
    """A problem the person can act on; the command line prints it without a traceback."""


def connect() -> Api:
    cfg = config.load()
    return Api(cfg.get('server', ''), cfg.get('token'))


def parse_ref(ref: str) -> Tuple[str, str]:
    """'SES-TX' -> latest version, 'SES-TX@3' -> version 3."""
    name, _, number = ref.partition('@')
    number = number or 'latest'
    if not name or not (number == 'latest' or number.isdigit()):
        raise FwError(f'版本写成 数据集名@序号，例如 SES-TX@3（不写 @序号 就是最新版）：{ref}')
    return name, number


def size_text(n: int) -> str:
    if n >= 1024 ** 3:
        return f'{n / 1024 ** 3:.2f} GB'
    return f'{n / 1024 ** 2:.1f} MB'


class Progress:
    """A single status line on stderr; when stderr is not a terminal nothing is printed."""

    def __init__(self, label: str, total: int):
        self.label, self.total, self.count = label, total, 0
        self.tty = sys.stderr.isatty()
        self.last = 0.0
        self.lock = threading.Lock()

    def add(self, n: int = 1):
        if not self.tty:
            return
        with self.lock:
            self.count += n
            now = time.monotonic()
            if now - self.last > 0.5:
                self.last = now
                print(f'\r{self.label} {self.count}/{self.total}', end='', file=sys.stderr, flush=True)

    def done(self):
        if self.total and self.tty:
            print(f'\r{self.label} {self.total}/{self.total}', file=sys.stderr, flush=True)


def _show(echo: Echo, title: str, items: list, limit: int = 5):
    if items:
        examples = f'，例如 {"、".join(str(i) for i in items[:limit])}' if limit else ''
        echo(f'  {title}：{len(items)}{examples}')


def _group(paths: List[str], limit: int = 3) -> str:
    shown = '、'.join(paths[:limit])
    return f'{shown} 等 {len(paths)} 个' if len(paths) > limit else shown


def _local(root: Path, rel: str) -> Path:
    return root.joinpath(*rel.split('/'))


def _parallel(fn, items, workers: int, label: str) -> list:
    progress = Progress(label, len(items))

    def run(item):
        result = fn(item)
        progress.add()
        return result

    out = run_all(run, items, workers)
    progress.done()
    return out


def _file_workers(sizes: List[int], workers: int) -> int:
    """Threads for hashing or copying local files. Large files are hashed with the GIL released, so threads
    help; for small files the switching between threads costs more than it saves (measured: 8 KB files take
    about 50 us each in one thread and over 300 us each in 8 threads), so they are done in one thread."""
    if not sizes or sum(sizes) < BIG_FILE * len(sizes):
        return 1
    return max(1, min(workers, os.cpu_count() or 1))


def _upload(api: Api, digests: List[str], sources: Dict[str, Path], sizes: Dict[str, int], workers: int,
            label: str) -> int:
    """Upload these objects: small ones many per request, large ones one per request. Returns bytes sent."""
    progress = Progress(label, len(digests))
    small = [(d, sizes[d]) for d in digests if sizes[d] <= BATCH_OBJECT_MAX]
    large = [d for d in digests if sizes[d] > BATCH_OBJECT_MAX]

    def run(job):
        kind, arg = job
        if kind == 'batch':
            n = api.put_objects([(d, sources[d], size) for d, size in arg])
            progress.add(len(arg))
        else:
            n = api.put_file(arg, sources[arg])
            progress.add()
        return n

    jobs = [('batch', b) for b in batches(small, UPLOAD_COUNT, UPLOAD_BYTES)] + [('file', d) for d in large]
    total = sum(run_all(run, jobs, workers))
    progress.done()
    return total


def show_changes(echo: Echo, title: str, changes: dict, limit: int = 5):
    echo(f'{title}：新增 {len(changes["added"])}、修改 {len(changes["modified"])}、'
         f'删除 {len(changes["deleted"])}、改名 {len(changes["renamed"])}')
    if limit:
        _show(echo, '修改', changes['modified'], limit)
        _show(echo, '删除', changes['deleted'], limit)
        _show(echo, '改名', [f'{a} → {b}' for a, b in changes['renamed']], limit)


def _hash_local(item) -> Entry:
    rel, path = item
    before = os.stat(path)
    digest, size = sha256_file(path)
    after = os.stat(path)
    if size != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise FwError(f'{rel} 在计算哈希时被修改了，等它不再变化后重试')
    return rel, digest, size


def import_folder(api: Api, name: str, folder, parent: str = 'latest', note: str = '',
                  allow_other_files: bool = False, force: bool = False, dry_run: bool = False,
                  confirm: Optional[Confirm] = None, workers: int = 8, echo: Echo = print) -> Optional[dict]:
    """Check a folder, show what changes against the parent version, upload only what the server lacks
    and freeze the folder as the next version of dataset `name`.

    Every file is hashed afresh (remembered hashes are not trusted). Returns the new version, the parent
    when nothing changed, or None for a dry run or a declined confirmation. `confirm` is asked before
    anything is uploaded; None means no question.
    """
    report = scan_folder(folder)
    echo(f'扫描 {report.root}：{len(report.files)} 个文件，图片 {len(report.images)}，标注 {len(report.labels)}，'
         f'说明文件 {len(report.meta)}，其他 {len(report.others)}')
    _show(echo, '已忽略的系统文件', report.junk)
    _show(echo, '读不了的目录', report.unreadable)
    _show(echo, '链接和特殊文件', report.links)
    _show(echo, '文件名用不了', report.bad_paths)
    _show(echo, '路径冲突', report.collisions)
    _show(echo, '同名不同扩展名的图片', [_group(g) for g in report.same_stem_images])
    _show(echo, '没有标注的图片', report.unlabeled_images)
    _show(echo, '找不到图片的标注', report.orphan_labels)
    _show(echo, '不同目录里的同名图片（训练时把目录摊平会互相覆盖）', [_group(g) for g in report.same_name_in_dirs])
    _show(echo, '说明文件（一起入库）', report.meta)
    _show(echo, '图片和标注以外的文件', report.others)
    reasons = report.blocking(allow_other_files, force)
    if reasons:
        raise FwError('没有入库：\n  ' + '\n  '.join(reasons))
    if not report.files:
        raise FwError('目录里没有文件')

    dataset = api.dataset(name)
    latest = dataset.get('latest')
    if parent == 'latest':
        base = latest
    elif parent == 'none':
        base = None
    elif parent.isdigit():
        base = api.version(name, parent)
    else:
        raise FwError('--parent 写 latest、none 或版本序号')

    t0 = time.perf_counter()
    sample = [os.stat(path).st_size for _, path in report.files[:200]]
    entries: List[Entry] = _parallel(_hash_local, report.files, _file_workers(sample, workers), '算哈希')
    data = manifest_bytes(entries)
    digest = manifest_hash(data)
    total = sum(size for _, _, size in entries)
    t1 = time.perf_counter()
    groups: Dict[str, List[str]] = {}
    for rel, d, _ in entries:
        if Path(rel).suffix.lower() in IMAGE_EXTS:
            groups.setdefault(d, []).append(rel)
    _show(echo, '内容完全相同的图片（只存一份）', [_group(g) for g in groups.values() if len(g) > 1], limit=3)

    sources: Dict[str, Path] = {}
    sizes: Dict[str, int] = {}
    for rel, d, size in entries:
        sources.setdefault(d, _local(report.root, rel))
        sizes[d] = size
    missing = api.missing([(d, sizes[d]) for d in sorted(sources)])
    if base is not None and base['manifest_sha256'] == digest:
        if not dry_run:  # objects the server lost or put in quarantine come back from this folder
            if missing:
                _upload(api, missing, sources, sizes, workers, '补传')
            manifest_missing = bool(api.missing([(digest, len(data))]))
            if manifest_missing:
                api.put_bytes(data)
            api.flush()  # also covers files an interrupted earlier run brought back: on disk before we say so
            if missing:
                echo(f'补传了 {len(missing)} 个服务器上缺少或损坏的文件')
            if manifest_missing:
                echo('补传了这个版本的清单文件')
        echo(f'和 {base["ref"]} 完全一样，不用建新版本')
        return base
    if base is not None:
        old = parse_manifest(api.read_object(base['manifest_sha256']))
        show_changes(echo, f'相比 {base["ref"]}', diff_entries(old, entries))
    else:
        echo('不接父版本：这是一条新的版本线' if latest else f'这是 {name} 的第一个版本')
    number = (latest['number'] if latest else 0) + 1
    echo(f'新版本 {name}@{number}：{len(entries)} 个文件，{size_text(total)}；要上传 {len(missing)} 个文件，'
         f'{size_text(sum(sizes[d] for d in missing))}，其余服务器上已有')
    if dry_run:
        echo('试运行：没有上传，没有建版本')
        return None
    t_check = time.perf_counter()
    if confirm is not None and not confirm(f'确认建 {name}@{number} 吗？'):
        echo('没有建版本')
        return None

    t_upload = time.perf_counter()  # the time spent answering the question above is nobody's speed
    uploaded = _upload(api, missing, sources, sizes, workers, '上传')
    api.put_bytes(data)
    t2 = time.perf_counter()
    source = f'fw dataset import {socket.gethostname()}:{report.root.resolve()}'
    version = api.create_version(name, digest, base['id'] if base else None, latest['id'] if latest else None,
                                 source, note)
    t3 = time.perf_counter()
    echo(f'已冻结 {version["ref"]}：{version["file_count"]} 个文件，{size_text(version["total_bytes"])}；'
         f'上传了 {len(missing)} 个文件，{size_text(uploaded)}')
    echo(f'  用时：算哈希 {t1 - t0:.1f} 秒，查服务器和对比 {t_check - t1:.1f} 秒，上传 {t2 - t_upload:.1f} 秒，'
         f'建版本 {t3 - t2:.1f} 秒（不含等你确认的时间）')
    return version


def _empty_dest(dest: Path) -> None:
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise FwError(f'{dest} 已存在且不是空目录')


def _check_link(link: str) -> None:
    if link == 'hardlink' and os.name == 'nt':
        raise FwError('Windows 上不能用 --hardlink：Windows 的只读属性由一个文件的所有硬链接共用，'
                      '清缓存时取出的文件会一起变成可写；用默认方式或 --copy')


def _lay_out(api: Api, cache: Cache, label: str, entries: List[Entry], dest: Path, link: str, workers: int,
             echo: Echo) -> None:
    """Lay these files out in `dest` (empty or missing), downloading only what the cache lacks; each placed
    file is hashed and must match its entry (see get)."""
    _empty_dest(dest)
    dest.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    objects = {d: size for _, d, size in entries}
    before = cache.downloaded_files
    progress = Progress('下载', 0)
    cached = set()

    def on_cached(digests):
        cached.update(digests)
        n = len(digests)
        progress.total = len(objects) - n
        echo(f'{len(objects)} 种不同内容：本机缓存已有 {n} 个，要下载 {len(objects) - n} 个')

    paths = cache.fetch_many(api, list(objects.items()), workers=workers, progress=progress, on_cached=on_cached)
    progress.done()
    t1 = time.perf_counter()
    for parent in sorted({_local(dest, rel).parent for rel, _, _ in entries}):
        parent.mkdir(parents=True, exist_ok=True)
    layout = Layout(link)
    lock = threading.Lock()
    refetched = set()

    def place(entry):
        rel, digest, _ = entry
        target = _local(dest, rel)
        source = paths[digest]
        for attempt in range(3):
            try:
                if layout.place(source, target) == digest:
                    return
                remove_file(target)
            except FileNotFoundError:
                if target.exists():
                    raise
            # the cache object is damaged, or it was cleared meanwhile: download it again. The new copy
            # replaces the old one atomically, so another fw process reading it never finds it missing.
            with lock:
                refetched.add(digest)
            source = cache.fetch(api, digest, force=True)
        raise FwError(f'{rel}：重新下载后仍然和清单的哈希对不上，请检查这台机器的磁盘')

    _parallel(place, entries, _file_workers([size for _, _, size in entries], workers), '摆放并核对')
    t2 = time.perf_counter()
    echo(f'{label} 已放到 {dest}：{len(entries)} 个文件，每个都按清单核对过；'
         f'缓存命中 {len(cached - refetched)} 个，下载 {cache.downloaded_files - before} 个，'
         f'方式：{layout.describe()}；'
         f'用时 下载 {t1 - t0:.1f} 秒，摆放和核对 {t2 - t1:.1f} 秒')
    if refetched:
        echo(f'  本机缓存里有 {len(refetched)} 个文件和哈希对不上，已重新下载')


def get(api: Api, cache: Cache, ref: str, dest, link: str = 'auto', subpath: Optional[str] = None,
        workers: int = 8, echo: Echo = print, fmt: Optional[str] = None) -> dict:
    """Lay out one version in an empty folder, downloading only what the local cache lacks.

    link: 'auto' (reflink where the file system can, else copy), 'copy', or 'hardlink' (read-only files
    shared with the cache; only when asked for). Every placed file is hashed and must match the manifest;
    on a mismatch the cache object is downloaded again, and a second mismatch is an error.
    fmt: for a training-set snapshot, the layout (yolo by default, coco, json or all; see trainset_layout).
    """
    _check_link(link)
    if fmt is not None and fmt not in TRAINSET_FORMATS:
        raise FwError(f'--format 只能是 {"、".join(TRAINSET_FORMATS)}')
    name, number = parse_ref(ref)
    version = api.version(name, number)
    entries = parse_manifest(api.read_object(version['manifest_sha256']))
    if version.get('kind') == 'trainset':
        fmt = fmt or 'yolo'
        if fmt != 'all':
            held = api.trainset(name, version['number'])['trainset']['formats']
            if fmt not in held:
                raise FwError(f'{version["ref"]} 里没有 {fmt} 格式（有 {"、".join(held)}）；'
                              f'为什么没有，见建快照时的提示或 trainset.json 的 missing_formats')
        entries = trainset_layout(entries, fmt)
    elif fmt not in (None, 'all'):
        raise FwError(f'{version["ref"]} 不是训练集快照，--format 只用于训练集快照')
    if subpath:
        sp = subpath.strip('/')
        entries = [e for e in entries if e[0] == sp or e[0].startswith(sp + '/')]
        if not entries:
            raise FwError(f'{version["ref"]} 里没有 {sp}')
    dest = Path(dest)
    _lay_out(api, cache, version['ref'], entries, dest, link, workers, echo)
    out = dict(version, path=str(dest), files=len(entries))
    if version.get('kind') == 'trainset':
        out['format'] = fmt
    if fmt == 'yolo' and not subpath:
        rels = [rel for rel, _, _ in entries]
        out['data_yaml'] = str(_write_data_yaml(dest, rels))
        empty = [s for s in ('train', 'val') if not any(p.startswith(f'images/{s}/') for p in rels)]
        if empty:
            out['missing_splits'] = empty
            echo(f'  已生成 {out["data_yaml"]}（不在快照里）；这个快照没有 {"、".join(empty)} 样本，'
                 f'不能直接 yolo train（ultralytics 要 train 和 val），只能当测试集用')
        else:
            echo(f'  已生成 {out["data_yaml"]}（不在快照里）：yolo train data={out["data_yaml"]}')
    return out


def _write_data_yaml(dest: Path, paths: List[str]) -> Path:
    """data.yaml for ultralytics, made from classes.txt: the folder's absolute path, the splits it has and
    the class names. It is written after the snapshot is laid out and is not part of it."""
    classes = (dest / 'classes.txt').read_text(encoding='utf-8').splitlines()
    splits = [s for s in ('train', 'val', 'test') if any(p.startswith(f'images/{s}/') for p in paths)]
    q = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731  a JSON string is a valid YAML scalar
    lines = ['# fw get 生成的，不在快照里；ultralytics：yolo train data=<这个文件>',
             f'path: {q(dest.resolve().as_posix())}']
    lines += [f'{s}: images/{s}' for s in splits]
    lines += ['names:'] + [f'  {i}: {q(n)}' for i, n in enumerate(classes)]
    path = dest / 'data.yaml'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def export(api: Api, cache: Cache, ref: str, dest, workers: int = 8, echo: Echo = print) -> dict:
    """A self-contained copy for machines that cannot reach the platform: data/, fw-manifest.txt and
    fw-version.json. `fw verify` checks it there without the platform."""
    dest = Path(dest)
    _empty_dest(dest)
    version = get(api, cache, ref, dest / 'data', link='copy', workers=workers, echo=echo, fmt='all')
    (dest / 'fw-manifest.txt').write_bytes(api.read_object(version['manifest_sha256']))
    info = {k: version[k] for k in ('ref', 'id', 'dataset', 'number', 'manifest_sha256', 'file_count', 'total_bytes',
                                    'created_at', 'created_by')}
    info['exported_at'] = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    (dest / 'fw-version.json').write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    echo(f'已导出到 {dest}；拷到别的机器后用 fw verify {dest} 核对')
    return version


def verify(folder, workers: int = 8, echo: Echo = print) -> bool:
    """Check an exported folder against its manifest, without the platform."""
    folder = Path(folder)
    try:
        data = (folder / 'fw-manifest.txt').read_bytes()
    except FileNotFoundError:
        raise FwError(f'{folder} 里没有 fw-manifest.txt，不是 fw export 导出的目录') from None
    entries = parse_manifest(data)
    problems = []
    meta = folder / 'fw-version.json'
    if meta.is_file():
        info = json.loads(meta.read_text(encoding='utf-8'))
        if info.get('manifest_sha256') != manifest_hash(data):
            problems.append('fw-manifest.txt 和 fw-version.json 里记的清单哈希对不上')
    root = folder / 'data'
    actual = set()
    for dirpath, _, names in os.walk(root):
        for n in names:
            actual.add((Path(dirpath) / n).relative_to(root).as_posix())
    expected = {rel: (d, size) for rel, d, size in entries}
    missing = sorted(set(expected) - actual)
    extra = sorted(actual - set(expected))
    present = sorted(set(expected) & actual)
    results = _parallel(lambda rel: sha256_file(_local(root, rel)) != expected[rel], present, workers, '核对')
    changed = [rel for rel, bad in zip(present, results) if bad]
    _show(echo, '缺少的文件', missing)
    _show(echo, '多出来的文件', extra)
    _show(echo, '内容不一致的文件', changed)
    for p in problems:
        echo(f'  {p}')
    ok = not (problems or missing or extra or changed)
    echo(f'{folder}：' + (f'一致，{len(entries)} 个文件' if ok else '不一致'))
    return ok


def diff(api: Api, new_ref: str, old_ref: Optional[str] = None, show_lists: bool = False,
         echo: Echo = print) -> dict:
    """Changes from old_ref to new_ref; without old_ref, from new_ref's parent."""
    new = api.version(*parse_ref(new_ref))
    if old_ref:
        old = api.version(*parse_ref(old_ref))
    elif new.get('parent'):
        old = new['parent']
    else:
        raise FwError(f'{new["ref"]} 没有父版本，请写出要比较的两个版本')
    d = api.diff(old['id'], new['id'])
    show_changes(echo, f'{old["ref"]} → {new["ref"]}', d, limit=0 if show_lists else 5)
    if show_lists:
        for title, key in (('新增', 'added'), ('修改', 'modified'), ('删除', 'deleted')):
            for p in d[key]:
                echo(f'{title}\t{p}')
        for a, b in d['renamed']:
            echo(f'改名\t{a}\t{b}')
    return d


def get_version(ref: str, dest, link: str = 'auto') -> dict:
    """For training scripts: lay out a version in `dest` (empty or missing) and return its record, with
    'id' (to register with a model later) and 'path'. Progress goes to stderr."""
    api = connect()
    cache = Cache()
    try:
        return get(api, cache, ref, dest, link=link, echo=lambda s: print(s, file=sys.stderr))
    finally:
        cache.close()
        api.close()


# --- training-set snapshots ------------------------------------------------------------------------
SPLITS = ('train', 'val', 'test')
FORMAT_NAMES = {'yolo': 'YOLO', 'coco': 'COCO', 'json': 'LiteAnno JSON'}


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


def read_classes(path) -> List[str]:
    """classes.txt: one class name per line, the line number (from 0) is the class id."""
    try:
        text = Path(path).read_text(encoding='utf-8-sig')
    except OSError as e:
        raise FwError(f'读不了类别表 {path}：{e.strerror or e}') from None
    except UnicodeDecodeError:
        raise FwError(f'类别表 {path} 不是 UTF-8') from None
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    names = [line.strip() for line in lines]
    empty = [str(i + 1) for i, n in enumerate(names) if not n]
    if empty:
        raise FwError(f'类别表 {path} 第 {"、".join(empty[:5])} 行是空的；行号就是类别编号，中间不能有空行')
    if not names:
        raise FwError(f'类别表 {path} 是空的')
    return names


def read_split_file(path) -> Dict[str, str]:
    """A split made elsewhere: one sample per line, '<site>/<line>/<image path>' and train, val or test,
    separated by a tab or spaces; empty lines and lines starting with # are skipped."""
    out: Dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding='utf-8-sig').splitlines()
    except OSError as e:
        raise FwError(f'读不了划分清单 {path}：{e.strerror or e}') from None
    for n, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, _, split = line.rstrip().rpartition('\t') if '\t' in line else line.rstrip().rpartition(' ')
        key, split = key.strip().replace('\\', '/'), split.strip()  # paths copied from Windows Explorer
        if not key or split not in SPLITS:
            raise FwError(f'划分清单 {path} 第 {n} 行应为「<现场>/<产线>/<图片路径> train|val|test」：{line!r}')
        if key in out:
            raise FwError(f'划分清单 {path} 第 {n} 行：{key} 出现了两次')
        out[key] = split
    if not out:
        raise FwError(f'划分清单 {path} 是空的')
    return out


def show_trainset_plan(echo: Echo, preview: dict, classes: List[str]) -> None:
    counts = preview['counts']
    parts = []
    for split in SPLITS:
        c = counts[split]
        parts.append(f'{split} {c["samples"]} 张' + (f'（无标注 {c["negatives"]}）' if c['negatives'] else ''))
    echo('划分：' + '，'.join(parts))
    rows = [(name, [counts[s]['boxes'].get(name, 0) for s in SPLITS]) for name in classes]
    width = max(_width(n) for n in classes + ['类别'])

    def pad(text):
        return text + ' ' * (width - _width(text))
    echo(f'  {pad("类别")}  ' + '  '.join(f'{s:>6}' for s in SPLITS) + '  （框数）')
    for name, nums in rows:
        echo(f'  {pad(name)}  ' + '  '.join(f'{n:>6}' for n in nums))
    unused = [n for n, nums in rows if not any(nums)]
    if unused:
        echo(f'  注意：类别表里有 {len(unused)} 个类别一个框都没有：{"、".join(unused[:5])}')
    ch = preview.get('changes') or {}
    if ch:
        echo(f'相比 {preview["based_on"]}：新增 {ch["added"]}、移除 {ch["removed"]}、换了划分 {ch["moved"]}、'
             f'标注改了 {ch["relabeled"]}')
        _show(echo, '换了划分', ch.get('moved_examples', []))


def create_trainset(api: Api, name: str, sources: List[str], classes: List[str], val: float = 0.2, seed: int = 0,
                    test_dirs=(), merge_test: bool = False, new_to: str = 'test',
                    split: Optional[Dict[str, str]] = None, fresh: bool = False, note: str = '',
                    require_labels: bool = False, dry_run: bool = False, confirm: Optional[Confirm] = None,
                    echo: Echo = print, label_source: Optional[str] = None) -> Optional[dict]:
    """Preview a training-set snapshot, ask, then freeze it. The server builds both from the same inputs (the
    source versions the preview resolved, e.g. SES-TX@4 for SES-TX), so what is frozen is what was shown; if
    someone made another snapshot of this name meanwhile, it refuses."""
    body = {'sources': list(sources), 'classes': list(classes), 'val': val, 'seed': seed,
            'test_dirs': list(test_dirs), 'merge_test': merge_test, 'new_to': new_to, 'split': split,
            'fresh': fresh, 'note': note, 'require_labels': require_labels}
    if label_source is not None:  # only when given: a server from before this option still takes the request
        body['label_source'] = label_source
    preview = api.preview_trainset(name, body)
    if preview['based_on']:
        how = f'接着 {preview["based_on"]} 的划分'
    elif preview.get('latest'):
        how = f'{"用划分清单" if split is not None else "重新划分"}（不接着 {preview["latest"]}）'
    else:
        how = '第一版'
    echo(f'{name}：{how}；来源 {"、".join(preview["sources"])}；类别 {len(classes)} 个')
    show_trainset_plan(echo, preview, classes)
    for notice in preview.get('notices', []):
        echo(f'  注意：{notice}')
    if preview['problems']:
        raise FwError('没有建训练集快照：\n  ' + '\n  '.join(preview['problems']))
    echo(f'标注格式：{"、".join(FORMAT_NAMES[f] for f in preview["formats"])}（检测框，类别编号即 classes.txt 的行号）')
    for fmt, why in preview['missing_formats'].items():
        echo(f'  没有 {FORMAT_NAMES[fmt]}：{why}')
    echo(f'新快照：{preview["samples"]} 个样本，{preview["file_count"]} 个文件，{size_text(preview["total_bytes"])}')
    if dry_run:
        return None
    if confirm is not None and not confirm(f'确认建 {name} 的新快照吗？'):
        echo('没有建快照')
        return None
    body['expect_latest_id'] = preview['expect_latest_id']
    body['sources'] = preview['sources']  # the versions shown, not whatever is latest by now
    version = api.create_trainset(name, body)
    echo(f'已冻结 {version["ref"]}：{preview["samples"]} 个样本，{version["file_count"]} 个文件；'
         f'取到本地：fw get {version["ref"]} --to <目录>')
    return version


def get_trainset(ref: str, dest, fmt: str = 'yolo', link: str = 'auto') -> dict:
    """For training scripts: lay out a training-set snapshot in `dest` (empty or missing) in one format (yolo,
    coco, json or all; see docs/trainset-format.md) and return its record: 'id' (pass it when registering the
    model), 'ref', 'path', 'format', 'classes' (the index is the class id), 'counts', 'sources', and for yolo
    'data_yaml'. Progress goes to stderr."""
    name, number = parse_ref(ref)
    api = connect()
    cache = Cache()
    try:
        info = api.trainset(name, number)
        version = get(api, cache, info['ref'], dest, link=link, echo=lambda s: print(s, file=sys.stderr), fmt=fmt)
    finally:
        cache.close()
        api.close()
    ts = info['trainset']
    return dict(version, task=ts['task'], classes=ts['classes'], counts=ts['counts'], sources=ts['sources'],
                based_on=ts['based_on'], rule=ts['rule'], formats=ts['formats'])


# --- models, evaluations, releases -----------------------------------------------------------------------
TAG_NAMES = {'candidate': '候选', 'deprecated': '弃用'}
MODEL_INFO = 'fw-model.json'  # written by get_model next to the files; not part of the model version


def parse_model_ref(ref: str) -> Tuple[str, str]:
    """'SES-TX-detr' -> latest version, 'SES-TX-detr@3' -> version 3."""
    name, _, number = (ref or '').partition('@')
    number = number or 'latest'
    if not name or not (number == 'latest' or number.isdigit()):
        raise FwError(f'模型版本写成 模型名@序号，例如 SES-TX-detr@3（不写 @序号 就是最新版）：{ref}')
    return name, number


def strip_credentials(url: str) -> str:
    """https://user:token@host/x -> https://host/x: a token in a repository URL must never be recorded or shown.
    Surrounding blanks are removed first (the server removes them too), so ' https://u:t@h/x' is caught."""
    url = (url or '').strip()
    m = re.match(r'(https?://)[^/]*@(.*)\Z', url, re.I | re.S)
    return m.group(1) + m.group(2) if m else url


def _git(folder, *args) -> Optional[str]:
    # GIT_DIR, GIT_WORK_TREE and the like inherited from the shell would point git at another repository than
    # the folder asked for: run without any GIT_* variable.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    try:
        r = subprocess.run(['git', '-C', str(folder), *args], capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def code_text(mv: dict) -> str:
    """A model version's training code for printing: '<repo> @ <commit>', or that none was recorded."""
    if not mv.get('code_commit'):
        return '未记录'
    return f'{mv["code_repo"] or "（没有登记仓库地址）"} @ {mv["code_commit"]}'


def git_info(folder) -> Optional[dict]:
    """The commit checked out in `folder`, its origin URL (without credentials) and whether tracked files
    have changes not committed; None when it is not a git working copy (or git is not installed)."""
    commit = _git(folder, 'rev-parse', 'HEAD')
    if not commit:
        return None
    status = _git(folder, 'status', '--porcelain', '--untracked-files=no')
    return {'commit': commit.lower(), 'repo': strip_credentials(_git(folder, 'config', '--get', 'remote.origin.url')
                                                                or ''),
            'dirty': bool(status)}


def _read_json_object(path, what: str, undefined: Optional[list] = None) -> dict:
    """undefined: read NaN and ±Infinity as null and append each to it (the platform keeps only standard JSON)."""
    def constant(name):
        if undefined is None:
            raise ValueError(f'有 {name}，不是标准 JSON')
        undefined.append(name)
        return None
    def finite(v):  # 1e309 is not NaN to the parser but becomes inf
        if isinstance(v, float) and not math.isfinite(v):
            return constant(repr(v))
        if isinstance(v, dict):
            return {k: finite(x) for k, x in v.items()}
        if isinstance(v, list):
            return [finite(x) for x in v]
        return v
    try:
        data = Path(path).read_bytes()
        doc = finite(json.loads(data.decode('utf-8-sig'), parse_constant=constant))
    except OSError as e:
        raise FwError(f'读不了{what} {path}（{e.strerror or e}）') from None
    except ValueError as e:
        raise FwError(f'{what} {path} 不是合法的 UTF-8 JSON（{e}）') from None
    if not isinstance(doc, dict):
        raise FwError(f'{what} {path} 应该是一个 JSON 对象（{{…}}）')
    return doc


def _stage(folder: Path, dest: Path) -> Path:
    """A private copy of a folder (every file, or an error): what is read from it and what is archived from it
    are the same bytes."""
    if not folder.is_dir():
        raise FwError(f'{folder} 不是目录')
    for rel, path in _folder_files(folder, ''):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(path, target)
        except OSError as e:
            raise FwError(f'读不了 {path}（{e.strerror or e}），没有登记') from None
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _folder_files(folder: Path, prefix: str) -> List[Tuple[str, Path]]:
    out = []

    def unreadable(e: OSError):  # never leave out a folder that cannot be listed
        raise FwError(f'读不了目录 {e.filename}（{e.strerror or e}），附件不完整，没有登记')

    for root, dirs, names in os.walk(folder, onerror=unreadable, followlinks=False):
        dirs.sort()
        for d in dirs:
            if os.path.islink(os.path.join(root, d)):
                raise FwError(f'{os.path.join(root, d)} 是链接，不能作为附件；把真正的文件放进来')
        for n in sorted(names):
            path = Path(root) / n
            if path.is_symlink() or not path.is_file():
                raise FwError(f'{path} 不是普通文件，不能作为附件')
            out.append((prefix + path.relative_to(folder).as_posix(), path))
    return out


def _attachments(paths: Sequence, prefix: str = 'files/') -> List[Tuple[str, Path]]:
    """files/<name> for a file, files/<folder name>/<path inside it> for a folder."""
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out += _folder_files(p, f'{prefix}{p.resolve().name}/')
        elif p.is_file():
            out.append((prefix + p.name, p))
        else:
            raise FwError(f'附件 {p} 不存在')
    return out


def _check_names(items: List[Tuple[str, Path]]) -> None:
    seen = {}
    for rel, path in items:
        problem = path_problem(rel)
        if problem:
            raise FwError(f'{path} 不能登记：{problem}')
        if rel in seen:
            raise FwError(f'{seen[rel]} 和 {path} 登记后同名（{rel}），改一个名字')
        seen[rel] = path
    problems = collisions(seen)
    if problems:
        raise FwError('附件里有放不进同一个目录的文件：' + '；'.join(problems[:5]))


def _send_files(api: Api, items: List[Tuple[str, Path]], workers: int, echo: Echo) -> List[list]:
    """Hash these files, upload the ones the server lacks, and return them as [path, sha256, size]."""
    _check_names(items)
    hashed = _parallel(_hash_local, items, _file_workers([os.path.getsize(p) for _, p in items], workers),
                       '计算哈希')
    sizes = {d: size for _, d, size in hashed}
    sources = {d: path for (_, path), (_, d, _) in zip(items, hashed)}
    missing = api.missing(list(sizes.items()))
    if missing:
        sent = _upload(api, missing, sources, sizes, workers, '上传')
        echo(f'上传了 {len(missing)} 个文件，{size_text(sent)}')
    return [[rel, d, size] for rel, d, size in hashed]


def _trainset(api: Api, trainset) -> dict:
    """A training-set snapshot given as its id (from get_trainset) or as a reference such as SES-TX-det@2."""
    if isinstance(trainset, dict):
        trainset = trainset.get('id')
    if isinstance(trainset, int) and not isinstance(trainset, bool):
        version = api.get(f'/versions/{trainset}')
    else:
        version = api.version(*parse_ref(str(trainset)))
    if version.get('kind') != 'trainset':
        raise FwError(f'{version["ref"]} 不是训练集快照；模型版本要引用训练用的快照（fw trainset create 建的）')
    return version


def register_model(api: Api, name: str, onnx, trainset, metrics=None, files: Sequence = (),
                   code_commit: Optional[str] = None, code_repo: Optional[str] = None, note: str = '',
                   workers: int = 8, echo: Echo = print, weights=None, code_dir=None) -> dict:
    """Upload the ONNX (and the weights, the metrics file and attachments) and register them as the model's
    next version. The training code is optional: code_commit (and code_repo), or code_dir to take the commit
    checked out there; neither records none. Registering the same ONNX with the same snapshot, code and files
    again returns the version that exists (existing: True)."""
    onnx = Path(onnx)
    try:
        with open(onnx, 'rb') as f:
            head = f.read(1)
    except OSError as e:
        raise FwError(f'读不了 ONNX 文件 {onnx}（{e.strerror or e}）') from None
    if head != b'\x08':
        raise FwError(f'{onnx} 不像 ONNX 文件；权重（.pt、.pth）用 --weights 登记')
    version = _trainset(api, trainset)
    items = [('model.onnx', onnx)]
    if weights is not None:
        weights = Path(weights)
        if not weights.is_file():
            raise FwError(f'权重文件 {weights} 不存在（--weights 只收一个文件）')
        if weights.stat().st_size == 0:
            raise FwError(f'权重文件 {weights} 是空的，没有登记')
        items.append(('weights/' + weights.name, weights))
    if metrics is not None:
        _read_json_object(metrics, '验证指标文件')
        items.append(('metrics.json', Path(metrics)))
    items += _attachments(files)
    dirty = False
    from_git = code_dir is not None
    if from_git:
        if code_commit is not None:
            raise FwError('--code-dir 和 --code-commit 只写一个')
        info = git_info(Path(code_dir))
        if info is None:
            raise FwError(f'{code_dir} 不是 git 仓库（或者没装 git），取不到训练代码的提交号')
        code_commit, dirty = info['commit'], info['dirty']
        code_repo = info['repo'] if code_repo is None else code_repo
    code_repo = strip_credentials(code_repo or '')  # before it is shown or sent: a token is never printed
    if code_repo and not code_commit:
        raise FwError('写了 --code-repo 也要写 --code-commit（或者用 --code-dir）')
    code_commit = code_commit or ''
    if from_git:
        echo(f'训练代码：{code_repo or "（没有 origin 地址）"} @ {code_commit[:12]}'
             + ('；有没提交的改动，会记在这个版本上' if dirty else ''))
    rows = _send_files(api, items, workers, echo)
    mv = api.register_model_version(name, {'files': rows, 'trainset_id': version['id'], 'code_commit': code_commit,
                                           'code_repo': code_repo, 'code_dirty': dirty, 'note': note})
    if mv['existing']:
        echo(f'{mv["ref"]} 已经登记过（同一个 ONNX、快照、代码和文件），没有重复登记')
    else:
        echo(f'已登记 {mv["ref"]}：ONNX {mv["onnx_sha256"][:12]}（{size_text(mv["onnx_size"])}），'
             f'训练集快照 {mv["trainset"]["ref"]}，{len(mv["classes"])} 个类别；'
             f'取到部署机器：fw model get {mv["ref"]} --to <目录>')
    return mv


def get_model(api: Api, cache: Cache, ref: str, dest, link: str = 'auto', workers: int = 8,
              echo: Echo = print) -> dict:
    """Lay out a model version's files (model.onnx, classes.txt, metrics.json, files/...) in an empty folder and
    write fw-model.json next to them (which version, snapshot, classes, hashes)."""
    _check_link(link)
    name, number = parse_model_ref(ref)
    mv = api.model_version(name, number)
    entries = parse_manifest(api.read_object(mv['manifest_sha256']))
    dest = Path(dest)
    _lay_out(api, cache, mv['ref'], entries, dest, link, workers, echo)
    info = {k: mv[k] for k in ('ref', 'id', 'model', 'number', 'site', 'line', 'task', 'onnx_sha256', 'manifest_sha256',
                               'trainset', 'classes', 'code_repo', 'code_commit', 'code_dirty', 'tags', 'created_by',
                               'created_at')}
    info['fetched_at'] = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    (dest / MODEL_INFO).write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    echo(f'  已生成 {dest / MODEL_INFO}（不在模型版本里）：版本、快照、类别表和哈希')
    return dict(mv, path=str(dest), files=len(entries))


def _testset(api: Api, testset: str) -> Tuple[dict, str]:
    ref, _, split = (testset or '').partition(':')
    version = api.version(*parse_ref(ref))
    if version.get('kind') == 'trainset' and not split:
        raise FwError(f'{version["ref"]} 是训练集快照，要写明评的是哪个划分，例如 {version["ref"]}:test')
    return version, split


DETEVAL_META = ('weights', 'framework', 'iomin', 'agnostic', 'thresholds', 'reached')  # summary.json, per model


def _pin(api: Api, model_ref: str) -> dict:
    """The model version a reference names now (SES-TX-detr -> SES-TX-detr@3): registrations send its number,
    so a retry can never land on a version registered meanwhile."""
    name, number = parse_model_ref(model_ref)
    return api.model_version(name, number)


def _image_count(api: Api, version: dict, split: str) -> int:
    """How many images the test set has: under images/<split>/ of a snapshot, else every image of the version."""
    entries = parse_manifest(api.read_object(version['manifest_sha256']))
    top = f'images/{split}/' if split else ''
    return sum(1 for path, _, _ in entries if path.startswith(top) and Path(path).suffix.lower() in IMAGE_EXTS)


def _deteval_metrics(api: Api, folder: Path, model: Optional[str], set_name: Optional[str], version: dict,
                     split: str, echo: Echo) -> Tuple[dict, str, str]:
    """(metrics, model, set) from an aoi-deteval output folder: the set of the report that IS this test set.
    It must be named (the split's name, or set_name) and must have evaluated as many images as the test set
    has, or nothing is registered: a val report must never become a test result."""
    undefined: List[str] = []
    summary = _read_json_object(folder / 'summary.json', 'aoi-deteval 的结果', undefined)
    names = sorted(summary)
    if model is None:
        if len(names) != 1:
            raise FwError(f'{folder / "summary.json"} 里有 {len(names)} 个模型（{"、".join(names)}），'
                          '用 --deteval-model 指定是哪一个')
        model = names[0]
    if not isinstance(summary.get(model), dict):
        raise FwError(f'{folder / "summary.json"} 里没有模型 {model}（有 {"、".join(names)}）')
    result = summary[model]
    sets = sorted(k for k, v in result.items() if k not in DETEVAL_META and isinstance(v, dict) and 'images' in v)
    if set_name is None:
        if split and split in sets:
            set_name = split
        elif not split and len(sets) == 1:
            set_name = sets[0]
        else:
            raise FwError(f'aoi-deteval 的结果里评了 {"、".join(sets) or "（没有）"}，不知道哪个是 '
                          f'{version["ref"]}{":" + split if split else ""}；用 --deteval-set 指明')
    if set_name not in sets:
        raise FwError(f'aoi-deteval 的结果里没有集合 {set_name}（有 {"、".join(sets) or "（没有）"}）')
    row = result[set_name]
    want = _image_count(api, version, split)
    if row['images'] != want:
        raise FwError(f'aoi-deteval 的 {set_name} 评了 {row["images"]} 张图，'
                      f'{version["ref"]}{":" + split if split else ""} 有 {want} 张：评的不是这个测试集，没有登记')
    if undefined:  # e.g. a class with no ground truth in this test set: its AP is not defined
        echo(f'  summary.json 里有 {len(undefined)} 项没有定义的指标（NaN），登记为 null；原文件照样保存在 files/')
    metrics = {k: result[k] for k in DETEVAL_META if k in result}
    metrics.update(row)
    return metrics, model, set_name


def register_eval(api: Api, model_ref: str, testset: str, metrics=None, deteval=None,
                  deteval_model: Optional[str] = None, deteval_set: Optional[str] = None, files: Sequence = (),
                  tool: Optional[str] = None, tool_version: Optional[str] = None, note: str = '', workers: int = 8,
                  echo: Echo = print) -> dict:
    """Register an evaluation of a model version on a test set (a dataset version, or a split of a snapshot
    such as SES-TX-det@2:test). Either metrics (any JSON object) or deteval (an aoi-deteval evaluate output
    folder: the metrics are its result for deteval_model on the set that is this test set, see
    _deteval_metrics; the whole folder is kept)."""
    if (metrics is None) == (deteval is None):
        raise FwError('--metrics 和 --deteval 要写一个，只写一个')
    mv = _pin(api, model_ref)
    version, split = _testset(api, testset)
    items: List[Tuple[str, Path]] = []
    rows: List[list] = []
    staging = tempfile.TemporaryDirectory(prefix='fw-eval-')
    try:
        if deteval is not None:
            # Work on a private copy of the report folder: the metrics are read from the very files that are
            # archived, whatever the evaluation still writes to (or deletes from) its folder meanwhile.
            folder = _stage(Path(deteval), Path(staging.name) / 'report')
            doc, deteval_model, deteval_set = _deteval_metrics(api, folder, deteval_model, deteval_set, version,
                                                               split, echo)
            data = json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
            rows.append(['metrics.json', api.put_bytes(data), len(data)])
            items += _folder_files(folder, 'files/')
            if tool is None:
                tool = 'aoi-deteval'
            if tool_version is None and (folder / 'run_info.json').is_file():
                tool_version = str(_read_json_object(folder / 'run_info.json', 'aoi-deteval 的 run_info', []).get(
                    'deteval_version', ''))
            note = '；'.join(t for t in (note, f'aoi-deteval 结果里的模型 {deteval_model}、集合 {deteval_set}') if t)
        else:
            _read_json_object(metrics, '指标文件')  # the server reads the metrics from the copy it stores
            items.append(('metrics.json', Path(metrics)))
        items += _attachments(files)
        rows += _send_files(api, items, workers, echo) if items else []
    finally:
        staging.cleanup()
    e = api.register_eval(mv['model'], mv['number'], {
        'testset_id': version['id'], 'split': split, 'files': rows, 'tool': tool or '',
        'tool_version': tool_version or '', 'note': note})
    if e['existing']:
        echo(f'评估报告 #{e["id"]} 已经登记过（同一个模型版本、测试集和文件），没有重复登记')
    else:
        echo(f'已登记评估报告 #{e["id"]}：{e["model_version"]["ref"]} 在 {e["testset"]["ref"]} 上，'
             f'{e["file_count"]} 个文件')
    return e


def parse_time(value: Optional[str]) -> datetime.datetime:
    """An ISO 8601 time; without a time zone it is this machine's local time; None is now."""
    if value is None or value == 'now':
        return datetime.datetime.now().astimezone()
    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        t = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise FwError(f'时间写成 2026-11-20T10:30+08:00 这样（不写时区就按这台机器的时区）：{value}') from None
    return t if t.tzinfo is not None else t.astimezone()


def register_release(api: Api, model_ref: str, deploy_repo: str, deploy_commit: str,
                     deployed_at: Optional[str] = None, site: str = '', line: str = '', gpu: str = '',
                     trt_version: str = '', precision: str = '', engine_file=None, note: str = '',
                     echo: Echo = print) -> dict:
    """Register that a model version was deployed to a production line. The engine the site built from the
    ONNX stays there; with engine_file only its hash and size are recorded."""
    mv = _pin(api, model_ref)
    when = parse_time(deployed_at)
    body = {'model': mv['model'], 'number': str(mv['number']), 'site': site, 'line': line, 'deploy_repo': strip_credentials(deploy_repo),
            'deploy_commit': deploy_commit, 'deployed_at': when.isoformat(timespec='seconds'), 'gpu': gpu,
            'trt_version': trt_version, 'precision': precision, 'note': note}
    if engine_file is not None:
        try:
            body['engine_sha256'], body['engine_size'] = sha256_file(engine_file)
        except OSError as e:
            raise FwError(f'读不了引擎文件 {engine_file}（{e.strerror or e}）') from None
    r = api.register_release(body)
    if r['existing']:
        echo(f'{r["ref"]} 已经登记过（同一条产线、模型版本、部署提交号和部署时间），没有重复登记')
    else:
        echo(f'已登记发布 {r["ref"]}：{r["site"]}/{r["line"]} 部署了 {r["model_version"]["ref"]}，'
             f'部署时间 {local_time(r["deployed_at"])}')
    current = r['current']
    if current['ref'] == r['ref']:
        echo(f'{r["site"]}/{r["line"]} 现在用的模型就是 {r["model_version"]["ref"]}')
    else:
        echo(f'它的部署时间早于 {current["ref"]}（{local_time(current["deployed_at"])}），'
             f'{r["site"]}/{r["line"]} 现在用的仍是 {current["model_version"]["ref"]}')
    return r


def flatten_metrics(doc: dict, prefix: str = '') -> List[Tuple[str, float]]:
    """The numbers in a metrics JSON as (dotted key, value), in file order; lists and text are left out."""
    out: List[Tuple[str, float]] = []
    for k, v in doc.items():
        key = f'{prefix}{k}'
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append((key, v))
        elif isinstance(v, dict):
            out += flatten_metrics(v, key + '.')
    return out


def compare_metrics(old: dict, new: dict) -> List[Tuple[str, Optional[float], Optional[float]]]:
    a, b = dict(flatten_metrics(old)), dict(flatten_metrics(new))
    keys = list(a) + [k for k in b if k not in a]
    return [(k, a.get(k), b.get(k)) for k in keys]


def latest_eval(mv: dict, testset: str) -> Optional[dict]:
    found = [e for e in mv['evals'] if e['testset']['ref'].lower() == testset.lower()]
    return found[-1] if found else None


def local_time(stamp: Optional[str]) -> str:
    """A time from the server (ISO 8601) in this machine's time zone, to the minute."""
    if not stamp:
        return '-'
    return datetime.datetime.fromisoformat(stamp).astimezone().strftime('%Y-%m-%d %H:%M')


def show_chain(echo: Echo, c: dict) -> None:
    """The trace chain, one level after another, from the release down to the pool versions."""
    t_ = local_time
    r = c['release']
    mv = c['model_version']
    if r:
        echo(f'发布 {r["ref"]}：{r["site"]}/{r["line"]}，部署于 {t_(r["deployed_at"])}，'
             f'{r["created_by"]} 登记于 {t_(r["created_at"])}')
        echo(f'  部署仓库 {r["deploy_repo"]} @ {r["deploy_commit"]}')
        built = [x for x in (r['gpu'], f'TensorRT {r["trt_version"]}' if r['trt_version'] else '',
                             r['precision'].upper()) if x]
        engine = f'；引擎 sha256 {r["engine_sha256"][:12]}（{size_text(r["engine_size"])}，文件在现场）' \
            if r['engine_sha256'] else ''
        echo(f'  现场从 ONNX 构建引擎：{"，".join(built) or "没有登记 GPU、TensorRT 和精度"}{engine}')
        if r['note']:
            echo(f'  备注：{r["note"]}')
    elif c['releases']:
        echo(f'发布过 {len(c["releases"])} 次：' + '、'.join(f'{x["ref"]}（{t_(x["deployed_at"])}）' for x in c['releases']))
    else:
        echo('还没有发布过')
    echo(f'模型版本 {mv["ref"]}：ONNX {mv["onnx_sha256"][:12]}（{size_text(mv["onnx_size"])}），'
         f'文件清单 {mv["manifest_sha256"][:12]}，{mv["created_by"]} 登记于 {t_(mv["created_at"])}')
    echo('  训练代码 ' + code_text(mv) + ('，训练时有没提交的改动' if mv['code_dirty'] else ''))
    if mv['tags']:
        echo('  标签：' + '、'.join(TAG_NAMES.get(t, t) for t in mv['tags']))
    for e in c['evals']:
        tool = f'，{e["tool"]} {e["tool_version"]}'.rstrip() if e['tool'] else ''
        echo(f'  评估报告 #{e["id"]}：{e["testset"]["ref"]}{tool}，{e["created_by"]} 登记于 {t_(e["created_at"])}')
    t = c['trainset']
    ts = t['trainset']
    counts = '，'.join(f'{s} {ts["counts"][s]["samples"]}' for s in SPLITS if ts['counts'].get(s, {}).get('samples'))
    echo(f'训练集快照 {t["ref"]}：清单 {t["manifest_sha256"][:12]}，{len(ts["classes"])} 个类别，{counts}；'
         f'{t["created_by"]} 建于 {t_(t["created_at"])}' + (f'，接着 {ts["based_on"]} 的划分' if ts['based_on'] else ''))
    for v in c['sources']:
        parent = f'，父版本 {v["parent"]["ref"]}' if v.get('parent') else ''
        echo(f'数据池版本 {v["ref"]}：清单 {v["manifest_sha256"][:12]}，{v["file_count"]} 个文件{parent}；'
             f'{v["created_by"]} 导入于 {t_(v["created_at"])}')
        if v['source']:
            echo(f'  来源：{v["source"]}')
        if v['note']:
            echo(f'  备注：{v["note"]}')
    check = c.get('check')
    if check is not None:
        if check['problem_count']:
            echo(f'核对：{check["objects"]} 个文件里有 {check["problem_count"]} 个问题')
            for p in check['problems']:
                echo(f'  {p}')
        else:
            echo(f'核对：链上 {check["objects"]} 个文件都在文件库里，大小都对')


def _connected(fn):
    api = connect()
    try:
        return fn(api)
    finally:
        api.close()


def script_register_model(name: str, onnx, trainset, metrics=None, files: Sequence = (),
                          code_commit: Optional[str] = None, code_repo: Optional[str] = None,
                          note: str = '', weights=None, code_dir=None) -> dict:
    """fw.register_model, for training scripts: register the trained model as the next version of `name` (see
    register_model).
    trainset is what get_trainset returned, its id, or a reference such as 'SES-TX-det@2'. Returns the
    version's record with 'ref' (e.g. SES-TX-detr@3). Progress goes to stderr."""
    return _connected(lambda api: register_model(api, name, onnx, trainset, metrics, files, code_commit, code_repo,
                                                 note, echo=lambda s: print(s, file=sys.stderr),
                                                 weights=weights, code_dir=code_dir))


def script_get_model(ref: str, dest, link: str = 'auto') -> dict:
    """fw.get_model, for deploy scripts: lay out a model version in `dest` (empty or missing) and return its
    record with 'path', 'onnx_sha256' and 'classes'. Progress goes to stderr."""
    cache = Cache()
    try:
        return _connected(lambda api: get_model(api, cache, ref, dest, link=link,
                                                echo=lambda s: print(s, file=sys.stderr)))
    finally:
        cache.close()
