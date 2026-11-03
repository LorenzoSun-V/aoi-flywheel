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
from typing import Dict, Iterable, List, Tuple

MANIFEST_HEADER = 'fw-manifest 1'
CHUNK = 1 << 20
MAX_PATH_BYTES = 1024

Entry = Tuple[str, str, int]  # (path, sha256 hex, size in bytes)

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
BATCH_OBJECT_MAX = 16 << 20
_FRAME = re.compile(rb'([0-9a-f]{64}) (0|[1-9][0-9]{0,17})\n')
_FRAME_MAX = 64 + 1 + 18 + 1


def frame_header(digest: str, size: int) -> bytes:
    return f'{digest} {size}\n'.encode('ascii')


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
                    raise ContractError(f'传输内容里的文件超过 {self.max_object >> 20} MB，要单独传')
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
