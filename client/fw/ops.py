"""What the fw commands do. Training scripts can call these too (see fw.get_version)."""
from __future__ import annotations

import datetime
import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import config
from .api import Api
from .cache import Cache, remove_file
from .checks import IMAGE_EXTS, scan_folder
from .contract import (BATCH_OBJECT_MAX, Entry, batches, diff_entries, manifest_bytes, manifest_hash, parse_manifest,
                       sha256_file)
from .layout import Layout

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

    if workers <= 1:
        out = [run(item) for item in items]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            out = list(pool.map(run, items))
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
    if workers <= 1 or len(jobs) <= 1:
        total = sum(run(job) for job in jobs)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            total = sum(pool.map(run, jobs))
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
    _show(echo, '不能入库的文件', report.bad_paths)
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
                echo(f'补传了 {len(missing)} 个服务器上缺少或损坏的文件')
            if api.missing([(digest, len(data))]):
                api.put_bytes(data)
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
    if confirm is not None and not confirm(f'确认建 {name}@{number} 吗？'):
        echo('没有建版本')
        return None

    uploaded = _upload(api, missing, sources, sizes, workers, '上传')
    api.put_bytes(data)
    t2 = time.perf_counter()
    source = f'fw dataset import {socket.gethostname()}:{report.root.resolve()}'
    version = api.create_version(name, digest, base['id'] if base else None, latest['id'] if latest else None,
                                 source, note)
    t3 = time.perf_counter()
    echo(f'已冻结 {version["ref"]}：{version["file_count"]} 个文件，{size_text(version["total_bytes"])}；'
         f'上传了 {len(missing)} 个文件，{size_text(uploaded)}')
    echo(f'  用时：算哈希 {t1 - t0:.1f} 秒，上传 {t2 - t1:.1f} 秒，建版本 {t3 - t2:.1f} 秒')
    return version


def _empty_dest(dest: Path) -> None:
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise FwError(f'{dest} 已存在且不是空目录')


def get(api: Api, cache: Cache, ref: str, dest, link: str = 'auto', subpath: Optional[str] = None,
        workers: int = 8, echo: Echo = print) -> dict:
    """Lay out one version in an empty folder, downloading only what the local cache lacks.

    link: 'auto' (reflink where the file system can, else copy), 'copy', or 'hardlink' (read-only files
    shared with the cache; only when asked for). Every placed file is hashed and must match the manifest;
    on a mismatch the cache object is downloaded again, and a second mismatch is an error.
    """
    name, number = parse_ref(ref)
    version = api.version(name, number)
    entries = parse_manifest(api.read_object(version['manifest_sha256']))
    if subpath:
        sp = subpath.strip('/')
        entries = [e for e in entries if e[0] == sp or e[0].startswith(sp + '/')]
        if not entries:
            raise FwError(f'{version["ref"]} 里没有 {sp}')
    dest = Path(dest)
    _empty_dest(dest)
    dest.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    objects = {d: size for _, d, size in entries}
    before = cache.downloaded_files
    progress = Progress('下载', len(objects))
    paths = cache.fetch_many(api, list(objects.items()), workers=workers, progress=progress)
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
            # the cache object is damaged, or another fw process dropped it meanwhile: download it again
            with lock:
                if digest not in refetched:
                    refetched.add(digest)
                    cache.drop(digest)
            source = cache.fetch(api, digest)
        raise FwError(f'{rel}：重新下载后仍然和清单的哈希对不上，请检查这台机器的磁盘')

    _parallel(place, entries, _file_workers([size for _, _, size in entries], workers), '摆放并核对')
    t2 = time.perf_counter()
    echo(f'{version["ref"]} 已放到 {dest}：{len(entries)} 个文件，每个都按清单核对过；'
         f'下载 {cache.downloaded_files - before} 个，方式：{layout.describe()}；'
         f'用时 下载 {t1 - t0:.1f} 秒，摆放和核对 {t2 - t1:.1f} 秒')
    if refetched:
        echo(f'  本机缓存里有 {len(refetched)} 个文件和哈希对不上，已重新下载')
    return dict(version, path=str(dest), files=len(entries))


def export(api: Api, cache: Cache, ref: str, dest, workers: int = 8, echo: Echo = print) -> dict:
    """A self-contained copy for machines that cannot reach the platform: data/, fw-manifest.txt and
    fw-version.json. `fw verify` checks it there without the platform."""
    dest = Path(dest)
    _empty_dest(dest)
    version = get(api, cache, ref, dest / 'data', link='copy', workers=workers, echo=echo)
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
