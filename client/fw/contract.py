"""File contracts shared by the client and the server: path rules, hashing and manifest format 1.

A manifest lists every file of one dataset version (never a delta against the parent):

    fw-manifest 1
    <path>\t<sha256>\t<size>
    ...

UTF-8, LF line ends, a final LF, lines sorted by the UTF-8 bytes of the path, no duplicate paths.
The manifest itself is stored by its sha256 like any other file; that hash identifies the version's content.
See docs/manifest-format.md.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple

MANIFEST_HEADER = 'fw-manifest 1'
IMAGE_EXTS = frozenset({'.bmp', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp'})
LABEL_EXTS = frozenset({'.json', '.txt'})  # LiteAnno / X-AnyLabeling JSON, YOLO txt
META_NAMES = frozenset({'classes.txt', 'classes.names', 'obj.names', 'data.yaml', 'dataset.yaml', 'mask_colors.json'})
JUNK_NAMES = frozenset({'thumbs.db', 'desktop.ini', '.ds_store'})  # left out of a folder before it is imported
CHUNK = 1 << 20
MAX_PATH_BYTES = 1024

Entry = Tuple[str, str, int]  # (path, sha256 hex, size in bytes)

# Training-set snapshots (docs/trainset-format.md): the layouts fw get makes from a snapshot's files.
TRAINSET_FORMATS = ('yolo', 'yolo-tree', 'coco', 'json', 'all')
EMPTY_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'  # of no bytes: a made empty label
MIN_BOX_BYTES = 9  # a YOLO label line is five numbers: a file shorter than "0 0 0 0 0" holds no box
NEG_TRAIN = 'neg_train'
TRAINSET_SPLITS = ('train', 'val', 'test')
COCO_DIRS = {'train': 'train', 'val': 'valid', 'test': 'test'}  # the Roboflow / RF-DETR folder names
COCO_FILE = '_annotations.coco.json'

_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_SIZE = re.compile(r'(0|[1-9][0-9]{0,17})\Z')
_BAD_CHARS = frozenset('<>:"|?*\\') | frozenset(chr(i) for i in range(32)) | {'\x7f'}
_WINDOWS_RESERVED = frozenset(
    ['CON', 'PRN', 'AUX', 'NUL'] + [f'COM{i}' for i in range(1, 10)] + [f'LPT{i}' for i in range(1, 10)])


class ContractError(ValueError):
    pass


def path_problem(path: str) -> str | None:
    """Why `path` cannot be stored, or None. Paths must work on both Windows and Linux."""
    if not path:
        return '路径为空'
    if path.startswith('/'):
        return '不能是绝对路径'
    if unicodedata.normalize('NFC', path) != path:
        return '文件名的 Unicode 写法不规范（不是 NFC）'
    if len(path.encode('utf-8')) > MAX_PATH_BYTES:
        return f'路径超过 {MAX_PATH_BYTES} 字节'
    for seg in path.split('/'):
        if seg in ('', '.', '..'):
            return '路径里有空的、. 或 .. 这一级'
        bad = sorted(set(seg) & _BAD_CHARS)
        if bad:
            return f'含有 Windows 不允许的字符 {bad!r}'
        if seg.endswith((' ', '.')):
            return '某一级以空格或点结尾（Windows 会去掉）'
        if seg.split('.')[0].upper() in _WINDOWS_RESERVED:
            return f'{seg} 是 Windows 保留名'
    return None


def check_path(path: str) -> None:
    problem = path_problem(path)
    if problem:
        raise ContractError(f'{path!r}: {problem}')


def fold(path: str) -> str:
    """Key under which two paths collide on a case-insensitive file system (Windows)."""
    return unicodedata.normalize('NFC', path).casefold()


def path_key(path: str) -> bytes:
    return path.encode('utf-8')


def sha256_file(path) -> Tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, 'rb') as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def collisions(paths: Iterable[str]) -> List[str]:
    """Problems that make a set of paths impossible to lay out on disk: case-only twins (they collide on
    Windows) and a name used both as a file and as a folder."""
    problems = []
    files = {}
    dirs = set()
    for p in paths:
        key = fold(p)
        if key in files and files[key] != p:
            problems.append(f'只差大小写：{files[key]} 和 {p}')
        files.setdefault(key, p)
        parts = key.split('/')
        for i in range(1, len(parts)):
            dirs.add('/'.join(parts[:i]))
    for d in sorted(dirs & set(files)):
        problems.append(f'{files[d]} 既是文件又是目录')
    return problems


def manifest_bytes(entries: Iterable[Entry]) -> bytes:
    rows = sorted(entries, key=lambda e: path_key(e[0]))
    out = [MANIFEST_HEADER]
    for path, digest, size in rows:
        out.append(f'{path}\t{digest}\t{size}')
    return ('\n'.join(out) + '\n').encode('utf-8')


def parse_manifest(data: bytes) -> List[Entry]:
    """Parse and fully validate a manifest; any deviation from the canonical form is an error."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as e:
        raise ContractError(f'清单不是 UTF-8：{e}') from None
    if not text.endswith('\n'):
        raise ContractError('清单没有以换行结尾')
    lines = text[:-1].split('\n')
    if lines[0] != MANIFEST_HEADER:
        raise ContractError(f'清单第一行应为 {MANIFEST_HEADER!r}')
    entries: List[Entry] = []
    previous = None
    for n, line in enumerate(lines[1:], start=2):
        fields = line.split('\t')
        if len(fields) != 3:
            raise ContractError(f'清单第 {n} 行应有 3 列')
        path, digest, size = fields
        check_path(path)
        if not _HEX64.match(digest):
            raise ContractError(f'清单第 {n} 行的哈希不是 64 位小写十六进制')
        if not _SIZE.match(size):
            raise ContractError(f'清单第 {n} 行的大小不是非负整数')
        key = path_key(path)
        if previous is not None and key <= previous:
            raise ContractError(f'清单第 {n} 行没有按路径排序或路径重复')
        previous = key
        entries.append((path, digest, int(size)))
    problems = collisions(p for p, _, _ in entries)
    if problems:
        raise ContractError('清单里的路径无法同时放在磁盘上：' + '；'.join(problems[:5]))
    return entries


def manifest_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def classify_changes(added: Dict[str, str], deleted: Dict[str, str], modified: Iterable[str]) -> dict:
    """Turn raw path differences (path -> sha256) into the reported change lists.

    A deleted path whose content reappears at an added path is a rename; pairs are made one to one in path
    order, so a file copied to two new places counts as one rename plus one addition. The client preview and
    the server diff both use this, so they always agree.
    """
    by_hash: Dict[str, List[str]] = {}
    for path in sorted(added):
        by_hash.setdefault(added[path], []).append(path)
    renamed = []
    for path in sorted(deleted):
        targets = by_hash.get(deleted[path])
        if targets:
            renamed.append([path, targets.pop(0)])
    moved_to = {new for _, new in renamed}
    moved_from = {old for old, _ in renamed}
    return {
        'added': sorted(p for p in added if p not in moved_to),
        'deleted': sorted(p for p in deleted if p not in moved_from),
        'modified': sorted(modified),
        'renamed': renamed,
    }


def diff_entries(old: Iterable[Entry], new: Iterable[Entry]) -> dict:
    """Changes from one full manifest to another (Python's str order equals UTF-8 byte order)."""
    a = {p: d for p, d, _ in old}
    b = {p: d for p, d, _ in new}
    return classify_changes({p: d for p, d in b.items() if p not in a},
                            {p: d for p, d in a.items() if p not in b},
                            [p for p, d in b.items() if p in a and a[p] != d])


# --- batch transfer ------------------------------------------------------------------------------------
# Many small objects in one HTTP body, so that a folder of 200k small files is not 200k requests:
#
#     fw-objects 1\n
#     <sha256> <size>\n<size bytes>
#     <sha256> <size>\n<size bytes>
#     ...
#
# Every object is still checked against its own hash by whoever receives it. Objects larger than
# BATCH_OBJECT_MAX travel one per request instead.
OBJECTS_MAGIC = b'fw-objects 1\n'
OBJECTS_TYPE = 'application/x-fw-objects'

# --- naming a version ---------------------------------------------------------------------------------
# A version is written <name>@<which>, where <which> is its number (SES-TX@3), one of its labels
# (SES-TX@v0.1.0) or nothing (SES-TX: the newest). This is the one place that reads that; the server looks the
# version up (datasets.get_version, models.get_model_version).
LABEL = re.compile(r'v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\Z')
LABEL_HELP = '版本号写成 v主.次.修订，例如 v0.1.0（小写 v，三段数字，不带前导 0）'
BUMPS = ('patch', 'minor', 'major')


def is_label(text) -> bool:
    return isinstance(text, str) and LABEL.match(text) is not None


def label_key(label: str) -> Tuple[int, int, int]:
    """For sorting labels by what they mean: v0.10.0 after v0.9.0."""
    m = LABEL.match(label)
    if m is None:
        raise ContractError(f'{label!r}：{LABEL_HELP}')
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def split_ref(ref, what: str = '版本', labels: bool = True) -> Tuple[str, str]:
    """'SES-TX' -> ('SES-TX', 'latest'); 'SES-TX@3' -> ('SES-TX', '3'); 'SES-TX@v0.1.0' -> ('SES-TX',
    'v0.1.0'). labels false: for things that are only counted (the revisions of a batch)."""
    if not isinstance(ref, str) or not ref.strip():
        raise ContractError(f'要写{what}')
    name, sep, which = ref.strip().partition('@')
    which = which if sep else 'latest'
    ok = which == 'latest' or (which.isascii() and which.isdigit() and int(which) > 0) or (labels and is_label(which))
    if not name or not ok:
        raise ContractError(f'{what}写法不对：{ref}。写成 名称@序号（SES-TX@3）'
                            + ('、名称@版本号（SES-TX@v0.1.0）' if labels else '') + '，只写名称就是最新版')
    return name, which


def next_labels(labels: Iterable[str]) -> Dict[str, str]:
    """What the next label could be, from the highest one given so far (retired ones count: their strings
    stay taken): {'patch': ..., 'minor': ..., 'major': ...}. With none yet: v0.0.1, v0.1.0, v1.0.0."""
    keys = [label_key(x) for x in labels]
    a, b, c = max(keys) if keys else (0, 0, 0)
    return {'patch': f'v{a}.{b}.{c + 1}', 'minor': f'v{a}.{b + 1}.0', 'major': f'v{a + 1}.0.0'}


# --- which fw works with which server ------------------------------------------------------------------
# Every request of fw carries its interface level and every answer of the server carries the server's, in this
# header. A side that sends none is level 1 (anything before the header existed). Raise API_LEVEL when the
# interface gains something; raise MIN_CLIENT_LEVEL when the server can no longer serve older fw correctly, and
# MIN_SERVER_LEVEL when fw needs something older servers lack. Then the older side is told to update, by
# name, instead of failing somewhere in the middle of a command.
#   2  fw dataset add / remove (POST /datasets/<name>/changes), accounts and events over HTTP, capabilities in /me
#   3  labelling batches (fw batch ..., /batches); field accounts upload files
#   4  pre-labelling a batch (fw batch prelabel, /batches/<name>/prelabel)
#   5  version labels (fw label ..., <name>@v0.1.0 wherever a version is named)
#   6  the register of sites and lines (fw site ..., /sites); lists narrowed by site, line and text
#   7  /sites also says what the lists of datasets show on a line, a site and in all (snapshots, listed, unplaced)
#   8  /trainsets?source=<pool>: the snapshots that took data from that pool
API_HEADER = 'X-Fw-Api'
API_LEVEL = 8
MIN_CLIENT_LEVEL = 1
MIN_SERVER_LEVEL = 6


def peer_level(value: Optional[str]) -> int:
    """The level a header value states; 1 when there is none; 0 when it is not a level at all."""
    if value is None:
        return 1
    return int(value) if value.isdigit() and len(value) < 6 else 0
BATCH_OBJECT_MAX = 16 << 20
_FRAME = re.compile(rb'([0-9a-f]{64}) (0|[1-9][0-9]{0,17})\n')
_FRAME_MAX = 64 + 1 + 18 + 1


def frame_header(digest: str, size: int) -> bytes:
    return f'{digest} {size}\n'.encode('ascii')


class ObjectTooLarge(ContractError):
    """A frame announced an object bigger than the reader accepts."""

    def __init__(self, size: int, limit: int):
        super().__init__(f'传输内容里的文件超过 {limit >> 20} MB，要单独传')
        self.size = size
        self.limit = limit


class FrameParser:
    """Incremental reader of an fw-objects body: feed() it the bytes as they arrive, get back the objects
    completed so far as (sha256, data); finish() raises if the body stopped in the middle of an object."""

    def __init__(self, max_object: int = BATCH_OBJECT_MAX):
        self.max_object = max_object
        self.buf = bytearray()
        self.pos = 0
        self.magic = False
        self.digest = None
        self.need = 0
        self.count = 0

    def feed(self, data: bytes) -> List[Tuple[str, bytes]]:
        if self.pos:
            del self.buf[:self.pos]  # compact once per call, so parsing stays linear in the body size
            self.pos = 0
        self.buf += data
        out: List[Tuple[str, bytes]] = []
        buf = self.buf
        while True:
            if not self.magic:
                if len(buf) - self.pos < len(OBJECTS_MAGIC):
                    break
                if bytes(buf[self.pos:self.pos + len(OBJECTS_MAGIC)]) != OBJECTS_MAGIC:
                    raise ContractError('传输内容不是 fw-objects 1 格式')
                self.pos += len(OBJECTS_MAGIC)
                self.magic = True
            elif self.digest is None:
                end = buf.find(b'\n', self.pos, self.pos + _FRAME_MAX)
                if end < 0:
                    if len(buf) - self.pos >= _FRAME_MAX:
                        raise ContractError(f'传输内容第 {self.count + 1} 个文件的头格式不对')
                    break
                m = _FRAME.fullmatch(bytes(buf[self.pos:end + 1]))
                if m is None:
                    raise ContractError(f'传输内容第 {self.count + 1} 个文件的头格式不对')
                self.need = int(m.group(2))
                if self.need > self.max_object:
                    raise ObjectTooLarge(self.need, self.max_object)
                self.digest = m.group(1).decode('ascii')
                self.pos = end + 1
            else:
                if len(buf) - self.pos < self.need:
                    break
                out.append((self.digest, bytes(buf[self.pos:self.pos + self.need])))
                self.pos += self.need
                self.digest = None
                self.count += 1
        return out

    def finish(self) -> None:
        if not self.magic or self.digest is not None or len(self.buf) > self.pos:
            raise ContractError('传输内容不完整')


def batches(objects: Iterable[Tuple[str, int]], max_count: int, max_bytes: int) -> List[List[Tuple[str, int]]]:
    """Split (sha256, size) pairs into lists of at most max_count objects and about max_bytes."""
    out: List[List[Tuple[str, int]]] = []
    current: List[Tuple[str, int]] = []
    total = 0
    for digest, size in objects:
        if current and (len(current) >= max_count or total + size > max_bytes):
            out.append(current)
            current, total = [], 0
        current.append((digest, size))
        total += size
    if current:
        out.append(current)
    return out


def yolo_label_stem(image_path: str) -> Optional[str]:
    """Where ultralytics (and aoi-deteval) look for an image's label: its path with the LAST folder named images
    turned into labels, e.g. b1/images/x/a.png -> b1/labels/x/a (then .txt). None without such a folder."""
    parts = image_path.split('/')
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == 'images':
            path = '/'.join(parts[:i] + ['labels'] + parts[i + 1:])
            suffix = PurePosixPath(path).suffix
            return path[: len(path) - len(suffix)] if suffix else path
    return None


_IMAGES_DIR = re.compile(r'images_*\Z')


def yolo_safe(sub: str) -> str:
    """A path inside images/<split>/ or labels/<split>/ with every folder named images (or images_, images__…)
    given one more underscore. ultralytics and aoi-deteval find a label by replacing the LAST /images/ of the
    image path with /labels/; an images folder in the pool's own path would send them to the wrong place. One
    more underscore keeps different paths different."""
    parts = sub.split('/')
    return '/'.join([p + '_' if _IMAGES_DIR.match(p) else p for p in parts[:-1]] + parts[-1:])


def short_paths(subs: Iterable[str]) -> Dict[str, str]:
    """The shorter path fw get uses for each <site>/<line>/<path in pool> of a snapshot made with short paths
    (rule 'paths': 'short'): <site>/<line>/ goes, and so does the LAST folder named images in the pool path (the
    pool's YOLO images/ folder, whose labels/ the snapshot already paired), e.g. SES/TX/b1/images/x/1.png ->
    b1/x/1.png. The server refuses to make a snapshot whose shorter paths collide (two sources with the same
    path, or a pool with both images/a.png and a.png)."""
    subs = list(subs)
    split = []
    for sub in subs:
        parts = sub.split('/')
        if len(parts) < 3 or not all(parts):
            raise ContractError(f'训练集快照里有不认识的路径 {sub}')
        split.append(parts)
    out = {}
    for sub, parts in zip(subs, split):
        pool = parts[2:]
        for i in range(len(pool) - 2, -1, -1):
            if pool[i] == 'images':
                pool = pool[:i] + pool[i + 1:]
                break
        out[sub] = '/'.join(pool)
    return out


def trainset_flat(entries: Iterable[Entry]) -> Tuple[List[Tuple[str, str, int, Optional[str]]], List[str]]:
    """The flat YOLO layout of a snapshot, the default of fw get: (rows, problems). A row is (path, sha256, size,
    the stored path it comes from, or None for an empty label made here).

        train/images/<name>, train/labels/<stem>.txt   train images with boxes
        neg_train/<name>                               train images without boxes (no label, or one without a
                                                       box), no label files: what a trainer adds as negatives
        val/images/<name>, val/labels/<stem>.txt       every val image; one without a label gets an empty .txt
        test/images/<name>, test/labels/<stem>.txt     the same for test

    <name> is the image's file name alone: no folders of the pool are kept, so trainers that list a folder
    without going into subfolders find every image. Two files that land on one path (the same file name from two
    folders or sources; in one images/ folder also the same name with another extension, whose labels would be
    one file; names that differ only in case count as one, see fold) are problems: such a snapshot cannot be
    laid out flat (yolo-tree keeps the folders). Whether a label has a box is read from its size, see
    MIN_BOX_BYTES."""
    keep: List[Tuple[str, str, int, Optional[str]]] = []
    images: List[Tuple[str, str, Entry]] = []
    labels: Dict[Tuple[str, str], Entry] = {}
    for entry in entries:
        path = entry[0]
        top, _, rest = path.partition('/')
        split, _, sub = rest.partition('/')
        if not rest:
            keep.append((path, entry[1], entry[2], path))
        elif top in ('images', 'labels', 'json') and split in TRAINSET_SPLITS and sub:
            if top == 'images':
                images.append((split, sub, entry))
            elif top == 'labels':
                labels[(split, sub)] = entry
        elif not (top == 'coco' and rest.endswith('.json') and rest[:-5] in TRAINSET_SPLITS):
            raise ContractError(f'训练集快照里有不认识的路径 {path}')
    rows = list(keep)
    for split, sub, (path, digest, size) in images:
        name = sub.rsplit('/', 1)[-1]
        suffix = PurePosixPath(name).suffix
        stem = name[: len(name) - len(suffix)] if suffix else name
        sub_suffix = PurePosixPath(sub).suffix
        label = labels.get((split, (sub[: len(sub) - len(sub_suffix)] if sub_suffix else sub) + '.txt'))
        if split == 'train' and (label is None or label[2] < MIN_BOX_BYTES):
            rows.append((f'{NEG_TRAIN}/{name}', digest, size, path))
            continue
        rows.append((f'{split}/images/{name}', digest, size, path))
        if label is None:
            rows.append((f'{split}/labels/{stem}.txt', EMPTY_SHA256, 0, None))
        else:
            rows.append((f'{split}/labels/{stem}.txt', label[1], label[2], label[0]))
    by_path: Dict[str, List[str]] = {}
    for new, _, _, stored in rows:
        if stored is not None and stored.startswith('images/'):  # labels are named after their image
            by_path.setdefault(fold(new), []).append(stored)
    by_label: Dict[str, List[str]] = {}
    for new, _, _, stored in rows:
        if '/images/' in new:
            suffix = PurePosixPath(new).suffix
            by_label.setdefault(fold(new[: len(new) - len(suffix)] if suffix else new), []).append(stored)
    problems = [f'{"、".join(sorted(group))} 平铺后是同一个文件' for group in by_path.values() if len(group) > 1]
    problems += [f'{"、".join(sorted(group))} 平铺后同名不同扩展名，会共用一份标注' for group in by_label.values()
                 if len({fold(g.rsplit("/", 1)[-1]) for g in group}) > 1]
    return rows, sorted(problems)


def trainset_layout(entries: Iterable[Entry], fmt: str, short: bool = False) -> List[Entry]:
    """The files of a training-set snapshot as fw get lays them out in one format (docs/trainset-format.md):

    yolo       flat, see trainset_flat: train/images + train/labels, neg_train, val/…, test/… (refused with
               ContractError when file names collide)
    yolo-tree  images/<split>/..., labels/<split>/...txt with the pool's folders kept (ultralytics finds a label
               by images/ -> labels/; folders named images inside are renamed, see yolo_safe)
    coco       train|valid|test/... images, with <folder>/_annotations.coco.json (Roboflow / RF-DETR layout)
    json       train|val|test/... images with the LiteAnno JSON next to each (open the folder in LiteAnno)
    all        the snapshot as it is stored

    "..." is <site>/<line>/<path in pool>; with short (snapshots whose rule has 'paths': 'short') it is the
    shorter path from short_paths. classes.txt and trainset.json are in every layout. Only paths change;
    contents are the frozen ones (yolo also makes empty labels, EMPTY_SHA256)."""
    if fmt not in TRAINSET_FORMATS:
        raise ContractError(f'格式只能是 {"、".join(TRAINSET_FORMATS)}，不是 {fmt!r}')
    entries = list(entries)
    if fmt == 'yolo':
        rows, problems = trainset_flat(entries)
        if problems:
            more = f'\n  …共 {len(problems)} 处' if len(problems) > 10 else ''
            raise ContractError('这个快照不能按默认的平铺目录（--format yolo）摆，有文件会重名；'
                                '用 --format yolo-tree 保留原来的目录：\n  ' + '\n  '.join(problems[:10]) + more)
        return [row[:3] for row in rows]
    tree = fmt == 'yolo-tree'
    names: Dict[str, str] = {}
    if short and fmt != 'all':
        subs = set()
        for path, _, _ in entries:
            top, _, rest = path.partition('/')
            split, _, sub = rest.partition('/')
            if top in ('images', 'labels', 'json') and split in TRAINSET_SPLITS and sub:
                subs.add(sub)
        names = short_paths(sorted(subs))
    out = []
    for path, digest, size in entries:
        top, _, rest = path.partition('/')
        split, _, sub = rest.partition('/')
        sub = names.get(sub, sub)
        if not rest or fmt == 'all':
            new = path
        elif top == 'images' and split in TRAINSET_SPLITS and sub:
            new = {'yolo-tree': f'images/{split}/{yolo_safe(sub)}', 'coco': f'{COCO_DIRS[split]}/{sub}',
                   'json': f'{split}/{sub}'}[fmt]
        elif top == 'labels':
            new = f'labels/{split}/{yolo_safe(sub)}' if tree and sub else (path if tree else None)
        elif top == 'json':
            new = f'{split}/{sub}' if fmt == 'json' else None
        elif top == 'coco' and rest.endswith('.json') and rest[:-5] in TRAINSET_SPLITS:
            new = f'{COCO_DIRS[rest[:-5]]}/{COCO_FILE}' if fmt == 'coco' else None
        else:
            raise ContractError(f'训练集快照里有不认识的路径 {path}')
        if new is not None:
            out.append((new, digest, size))
    return out


# --- evaluation inputs (fw eval run) ---------------------------------------------------------------------
MAX_EVAL_DIRS = 200


def eval_dirs(dirs: Iterable[str]) -> List[str]:
    """The chosen folders of a version as clean relative paths, without repeats; [] = the whole version."""
    out: List[str] = []
    for d in dirs:
        d = (d or '').replace('\\', '/').strip().strip('/')
        if not d:
            raise ContractError('目录不能是空的；要评整个版本就不写 --dirs')
        if any(p in ('', '.', '..') for p in d.split('/')):
            raise ContractError(f'目录写成版本里的相对路径，例如 SES/TX/test_1015：{d}')
        if d not in out:
            out.append(d)
    if len(out) > MAX_EVAL_DIRS:
        raise ContractError(f'目录不能超过 {MAX_EVAL_DIRS} 个')
    return out


def eval_selection(entries: Iterable[Entry], snapshot: bool, split: str = '', dirs: Iterable[str] = ()
                   ) -> Tuple[List[Entry], List[Entry], List[str]]:
    """What an evaluation of a version reads: (files to lay out, the images among them, problems).

    A training-set snapshot: one split, in the yolo-tree layout (images/<split>/..., labels/<split>/...txt): the labels
    are already in the snapshot's class table, which is the model's. Any other version: the images under the
    chosen folders (every folder below them too) with the LiteAnno / X-AnyLabeling JSON next to each, read by
    class name. An image with only a YOLO txt is a problem: its class numbers are the pool's own, which need
    not be the model's (make a snapshot with --label-classes and evaluate its test split instead). So is an
    image without a label next to another image of the same name with another extension (they would share
    one label). Label extensions are matched in any case (x.JSON is laid out as x.json). Images without any
    label are normal images (no defects)."""
    entries = list(entries)
    if snapshot:
        if split not in TRAINSET_SPLITS:
            raise ContractError(f'训练集快照要写划分（{"、".join(TRAINSET_SPLITS)}）')
        laid = trainset_layout(entries, 'yolo-tree')
        images = [e for e in laid if e[0].startswith(f'images/{split}/')
                  and PurePosixPath(e[0]).suffix.lower() in IMAGE_EXTS]
        wanted = {yolo_label_stem(p) + '.txt' for p, _, _ in images}
        labels = [e for e in laid if e[0] in wanted]
        return sorted(images + labels), images, []
    chosen = eval_dirs(dirs)
    # labels by their path with the extension in lower case: deteval only looks for .json / .txt, so x.JSON is
    # laid out as x.json (and x.TXT counts as a txt label)
    by_label: Dict[str, List[Entry]] = {}
    for e in entries:
        suffix = PurePosixPath(e[0]).suffix
        if suffix.lower() in LABEL_EXTS:
            by_label.setdefault(e[0][: len(e[0]) - len(suffix)] + suffix.lower(), []).append(e)

    def inside(path: str) -> bool:
        return not chosen or any(path.startswith(d + '/') for d in chosen)

    images = [e for e in entries if inside(e[0]) and PurePosixPath(e[0]).suffix.lower() in IMAGE_EXTS]
    problems: List[str] = []
    found = {d for d in chosen if any(p.startswith(d + '/') for p, _, _ in images)}
    problems += [f'{d} 下没有图片（目录写成版本里的相对路径，注意大小写）' for d in chosen if d not in found]
    files, txt_only, stems, twice = [], [], {}, []
    for path, digest, size in images:
        stem = path[: len(path) - len(PurePosixPath(path).suffix)]
        stems.setdefault(stem, []).append(path)
        files.append((path, digest, size))
        json_labels = by_label.get(stem + '.json', [])
        if len(json_labels) > 1:
            twice.append('、'.join(sorted(e[0] for e in json_labels)))
        elif json_labels:
            files.append((stem + '.json', json_labels[0][1], json_labels[0][2]))
        else:
            yolo = yolo_label_stem(path)
            if stem + '.txt' in by_label or (yolo is not None and yolo + '.txt' in by_label):
                txt_only.append(path)
    if twice:
        problems.append(f'{len(twice)} 张图的 JSON 标注有几份（只差扩展名大小写），不知道用哪份：{"；".join(twice[:3])}')
    if txt_only:
        problems.append(f'{len(txt_only)} 张图只有 YOLO txt 标注，类别编号是数据池自己的，不一定和模型的类别表一致：'
                        f'{"、".join(txt_only[:3])}；先用 fw trainset create --label-classes 建快照，再评快照的划分')
    shared = [ps for ps in stems.values() if len(ps) > 1]
    if shared:
        problems.append(f'有 {len(shared)} 组图片同名不同扩展名，会共用一份标注：'
                        + '；'.join('、'.join(ps) for ps in shared[:3]))
    return sorted(set(files)), images, problems


def predict_selection(entries: Iterable[Entry], dirs: Iterable[str] = ()) -> Tuple[List[Entry], List[str]]:
    """What a prediction job over a version reads: (the images under the chosen folders, every folder below
    them too; problems). Labels are not read. Two images of the same name with other extensions are a problem:
    their pre-labels would be the same JSON file."""
    chosen = eval_dirs(dirs)

    def inside(path: str) -> bool:
        return not chosen or any(path.startswith(d + '/') for d in chosen)

    images = sorted(e for e in entries if inside(e[0]) and PurePosixPath(e[0]).suffix.lower() in IMAGE_EXTS)
    problems = [f'{d} 下没有图片（目录写成版本里的相对路径，注意大小写）' for d in chosen
                if not any(p.startswith(d + '/') for p, _, _ in images)]
    stems: Dict[str, List[str]] = {}
    for path, _, _ in images:
        stems.setdefault(path[: len(path) - len(PurePosixPath(path).suffix)].lower(), []).append(path)
    shared = [ps for ps in stems.values() if len(ps) > 1]
    if shared:
        problems.append(f'有 {len(shared)} 组图片同名不同扩展名（或只差大小写），预标注会写进同一个 JSON：'
                        + '；'.join('、'.join(ps) for ps in shared[:3]))
    return images, problems


# --- the checks on a folder before it becomes a dataset version (fw dataset import and the web upload) -------
def _stem(rel: str) -> str:
    suffix = PurePosixPath(rel).suffix
    return rel[: len(rel) - len(suffix)] if suffix else rel


def classify_paths(paths: Iterable[str]) -> dict:
    """The files of a folder (relative posix paths, junk names already left out) sorted into images, labels,
    meta files and others, with what is wrong with them. Same rules as the pilot's intake script."""
    rels = list(paths)
    out: dict = {'images': [], 'labels': [], 'meta': [], 'others': [], 'bad_paths': []}
    image_stems: Dict[str, List[str]] = {}
    label_stems = set()
    txt_stems = set()
    for rel in rels:
        problem = path_problem(rel)
        if problem:
            out['bad_paths'].append(f'{rel}：{problem}')
        name = PurePosixPath(rel).name.lower()
        ext = PurePosixPath(rel).suffix.lower()
        if ext in IMAGE_EXTS:
            out['images'].append(rel)
            image_stems.setdefault(_stem(rel), []).append(rel)
        elif name in META_NAMES:
            out['meta'].append(rel)
        elif ext in LABEL_EXTS:
            out['labels'].append(rel)
            label_stems.add(_stem(rel))
            if ext == '.txt':
                txt_stems.add(_stem(rel))
        else:
            out['others'].append(rel)
    out['collisions'] = collisions(rels)
    out['same_stem_images'] = [v for v in image_stems.values() if len(v) > 1]
    # a label pairs like on the server (trainsets._samples): the same stem in the image's folder, or the .txt
    # where YOLO looks for it (images/ -> labels/)
    yolo = {s: yolo_label_stem(s + '.x') for s in image_stems}
    out['unlabeled_images'] = sorted(s for s in image_stems if s not in label_stems and yolo[s] not in txt_stems)
    yolo_found = {y for y in yolo.values() if y in txt_stems}  # only the .txt there is YOLO's
    out['orphan_labels'] = sorted({_stem(rel) for rel in out['labels'] if _stem(rel) not in image_stems and not (
        _stem(rel) in yolo_found and rel.lower().endswith('.txt'))})
    by_name: Dict[str, List[str]] = {}
    for rel in out['images']:
        by_name.setdefault(PurePosixPath(_stem(rel)).name.lower(), []).append(rel)
    out['same_name_in_dirs'] = [v for _, v in sorted(by_name.items())
                                if len({PurePosixPath(r).parent for r in v}) > 1]
    return out


def blocking_reasons(found: dict, allow_other_files: bool, force: bool) -> List[str]:
    """Reasons a classified folder (classify_paths) must not become a version as it stands. The flags are
    fw dataset import's --allow-other-files and --force (the same check boxes on the web)."""
    reasons = []
    if found['bad_paths']:
        reasons.append('有文件名在 Windows 或 Linux 上用不了，先改名')
    if found['collisions']:
        reasons.append('有只差大小写、或同名的文件和目录，在 Windows 上会互相覆盖，先改名')
    if found['same_stem_images'] and not force:
        reasons.append('有同名不同扩展名的图片（比如 a.bmp 和 a.png），标注对不上；先清理，或确认无妨后加 --force')
    if found['others'] and not allow_other_files:
        reasons.append('有图片和标注以外的文件；先清理，或确认要一起入库后加 --allow-other-files')
    return reasons
