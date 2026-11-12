"""Changing a dataset by folders: the next version = the newest one with folders added, replaced or removed
(fw dataset add / remove, and the same buttons on the web).

The server builds the new version itself. A client uploads only the new folder: its files, and its own file
list (a manifest whose paths are inside the folder). The server takes the rows of the newest version, drops
the folders to remove or replace, adds the new folders' files under their names, writes the full manifest of
the result and freezes it like any other version (datasets.create_version: every version is still a full
manifest). What was done is recorded in version_changes in the same transaction, so "this version is its
parent plus that folder" is something the platform did, not something a client said.

plan() is what both the preview and the creation run, so what is frozen is what was shown:

- a folder that is already in the pool is refused by name unless it is to be replaced;
- the checks of fw dataset import (contract.classify_paths) on the resulting file list: names that do not
  work, clashes, and, for the new folders only, other files and images that share a stem;
- warnings, never refusals: new images whose content (sha256, from the manifests, no file is read) is
  already in the pool or appears twice in what is added, and those among them whose labels differ.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from fw.contract import (IMAGE_EXTS, LABEL_EXTS, ContractError, blocking_reasons, classify_paths, diff_entries,
                         fold, manifest_bytes, manifest_hash, parse_manifest, path_problem, yolo_label_stem)

from . import datasets, formats
from .datasets import DatasetError, check_digest
from .store import Store

Entry = Tuple[str, str, int]
MAX_DIRS = 100       # folders added or removed in one version
EXAMPLES = 50        # how many of each finding a plan lists
MAX_LABEL_READS = 20000  # labels read to compare duplicate images; beyond this the rest are not compared
OPS = {'add': '新增', 'replace': '替换', 'remove': '去掉'}


def clean_dir(value: Any) -> str:
    """A folder of the pool as a relative path with / between its levels."""
    if not isinstance(value, str):
        raise DatasetError(400, '目录写成文本')
    d = value.replace('\\', '/').strip().strip('/')
    if not d:
        raise DatasetError(400, '目录不能是空的')
    problem = path_problem(d)
    if problem:
        raise DatasetError(400, f'目录 {d} 用不了：{problem}')
    return d


def _under(path: str, d: str) -> bool:
    return path.startswith(d + '/')


def _stem(path: str) -> str:
    suffix = PurePosixPath(path).suffix
    return path[: len(path) - len(suffix)] if suffix else path


def _is_image(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in IMAGE_EXTS


@dataclass
class Op:
    op: str            # add, replace, remove
    dir: str
    file_count: int = 0
    total_bytes: int = 0
    removed_count: int = 0
    content_sha256: Optional[str] = None

    def row(self) -> dict:
        return {'op': self.op, 'op_text': OPS[self.op], 'dir': self.dir, 'file_count': self.file_count,
                'total_bytes': self.total_bytes, 'removed_count': self.removed_count,
                'content_sha256': self.content_sha256}


@dataclass
class Plan:
    dataset: dict
    latest: Optional[dict]
    ops: List[Op]
    entries: List[Entry]                 # the full file list of the new version
    inherited: List[Entry]               # those of them taken unchanged from the parent
    new: List[Entry]                     # those that come from the added folders
    conflicts: List[dict] = field(default_factory=list)   # folders already in the pool (not to be replaced)
    reasons: List[str] = field(default_factory=list)      # why the version must not be made as it stands
    checks: Dict[str, list] = field(default_factory=dict)
    duplicates: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)

    def summary(self) -> dict:
        number = (self.latest['number'] if self.latest else 0) + 1
        return {'dataset': self.dataset['name'], 'parent': self.latest['ref'] if self.latest else None,
                'expect_latest_id': self.latest['id'] if self.latest else None,
                'ref': f'{self.dataset["name"]}@{number}', 'number': number, 'file_count': len(self.entries),
                'total_bytes': sum(s for _, _, s in self.entries), 'ops': [o.row() for o in self.ops],
                'conflicts': self.conflicts, 'reasons': self.reasons,
                'checks': {k: {'count': len(v), 'examples': v[:EXAMPLES]} for k, v in self.checks.items()},
                'duplicates': self.duplicates,
                'changes': {k: {'count': len(v), 'examples': v[:EXAMPLES]} for k, v in self.changes.items()}}


# --- labels of duplicate images ------------------------------------------------------------------------
def _label_index(entries: List[Entry]) -> Dict[str, Entry]:
    """Labels by their path with the extension in lower case."""
    out: Dict[str, Entry] = {}
    for e in entries:
        suffix = PurePosixPath(e[0]).suffix
        if suffix.lower() in LABEL_EXTS:
            out.setdefault(e[0][: len(e[0]) - len(suffix)] + suffix.lower(), e)
    return out


def _label_of(image: str, labels: Dict[str, Entry]) -> Optional[Entry]:
    """The label an image is paired with (as trainsets._samples pairs them): the .json or .txt of its name in
    its folder, or the .txt where YOLO looks for it; a JSON wins over a txt."""
    stem = _stem(image)
    yolo = yolo_label_stem(image)
    for key in (stem + '.json', stem + '.txt', (yolo + '.txt') if yolo is not None else None):
        if key is not None and key in labels:
            return labels[key]
    return None


def _signature(store: Store, label: Entry) -> Optional[Tuple[str, int, Any]]:
    """(kind, number of objects, what to compare) of a label file: its boxes, whatever the order or the
    formatting. A file that cannot be read as a label is compared by its bytes. None: the file is not in the
    store (a new folder's label that has not been uploaded yet)."""
    kind = 'json' if label[0].lower().endswith('.json') else 'txt'
    data = store.read(label[1])
    if data is None:
        return None
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return kind, 0, ('bytes', label[1])
    if kind == 'json':
        parsed = formats.parse_json(text)
        if isinstance(parsed, str):
            return kind, 0, ('bytes', label[1])
        boxes = sorted((b.name, round(b.x1, 2), round(b.y1, 2), round(b.x2, 2), round(b.y2, 2)) for b in parsed.boxes)
        return kind, len(parsed.names), (tuple(boxes), tuple(sorted(parsed.refused.items())))
    rows = []
    for row in text.splitlines():
        fields = row.split()
        if not fields:
            continue
        try:
            rows.append((fields[0],) + tuple(round(float(v), 6) for v in fields[1:]))
        except ValueError:
            rows.append(tuple(fields))
    return kind, len(rows), tuple(sorted(rows, key=repr))


def _duplicates(store: Store, kept: List[Entry], new: List[Entry], entries: List[Entry]) -> dict:
    """New images whose content the pool already has (kept: the parent's files that stay) or that appear more
    than once in what is added, and the pairs among them whose labels differ."""
    in_pool: Dict[str, List[str]] = {}
    for path, digest, _ in kept:
        if _is_image(path):
            in_pool.setdefault(digest, []).append(path)
    in_new: Dict[str, List[str]] = {}
    for path, digest, _ in new:
        if _is_image(path):
            in_new.setdefault(digest, []).append(path)
    pool_pairs = [(path, in_pool[digest][0], len(in_pool[digest])) for digest, paths in sorted(in_new.items())
                  if digest in in_pool for path in paths]
    pool_pairs.sort()
    groups = sorted(sorted(paths) for paths in in_new.values() if len(paths) > 1)
    pairs = [(a, b) for a, b, _ in pool_pairs] + [(p, g[0]) for g in groups for p in g[1:]]

    labels = _label_index(entries)
    seen: Dict[str, Optional[Tuple[str, int, Any]]] = {}
    reads = 0
    differ, uncompared, not_uploaded = [], 0, 0
    for new_image, other in pairs:
        a, b = _label_of(new_image, labels), _label_of(other, labels)
        if a is None and b is None:
            continue
        if a is not None and b is not None and a[1] == b[1]:
            continue
        sigs = []
        for label in (a, b):
            if label is None:
                sigs.append(None)
                continue
            if label[1] not in seen:
                if reads >= MAX_LABEL_READS:
                    sigs.append(False)
                    continue
                reads += 1
                seen[label[1]] = _signature(store, label)
            sigs.append(seen[label[1]] if seen[label[1]] is not None else True)
        if True in sigs:
            not_uploaded += 1
            continue
        if False in sigs:
            uncompared += 1
            continue
        sa, sb = sigs
        if sa is not None and sb is not None:
            if sa[0] != sb[0]:  # a JSON against a YOLO txt: class names against numbers, only the counts compare
                if sa[1] == sb[1]:
                    continue
            elif sa[2] == sb[2]:
                continue
        differ.append({'new': new_image, 'existing': other,
                       'new_boxes': sa[1] if sa is not None else None,
                       'existing_boxes': sb[1] if sb is not None else None})
    return {'in_pool': {'count': len(pool_pairs),
                        'examples': [{'new': a, 'existing': b, 'copies': n} for a, b, n in pool_pairs[:EXAMPLES]]},
            'in_new': {'count': len(groups), 'examples': groups[:EXAMPLES]},
            'labels_differ': {'count': len(differ), 'examples': differ[:EXAMPLES]},
            'uncompared': uncompared, 'not_uploaded': not_uploaded}


# --- the plan ------------------------------------------------------------------------------------------
def plan(conn, store: Store, name: str, add: List[dict], remove: List[str], allow_other_files: bool = False,
         force: bool = False) -> Plan:
    """add: [{dir, manifest_sha256, replace}], the manifest being the folder's own file list in the store."""
    dataset = datasets.get_dataset(conn, name)
    if dataset['kind'] == 'trainset':
        raise DatasetError(400, f'{dataset["name"]} 是训练集快照，只能用 fw trainset create 建新版本')
    if not add and not remove:
        raise DatasetError(400, '要写新增或去掉哪个目录')
    if len(add) + len(remove) > MAX_DIRS:
        raise DatasetError(400, f'一次最多新增、去掉 {MAX_DIRS} 个目录')
    latest = dataset['latest']
    parent: List[Entry] = datasets.version_files(conn, latest['id']) if latest else []

    removes = [clean_dir(d) for d in remove]
    adds = [(clean_dir(a.get('dir')), check_digest(a.get('manifest_sha256') or ''), bool(a.get('replace')))
            for a in add]
    dirs = removes + [d for d, _, _ in adds]
    folded = sorted(fold(d) for d in dirs)
    for a, b in zip(folded, folded[1:]):
        if a == b or b.startswith(a + '/'):
            raise DatasetError(400, f'目录 {a} 和 {b} 重复或一个在另一个里面，一次只能各动一处')

    ops: List[Op] = []
    conflicts: List[dict] = []
    gone = set()  # paths of the parent that are not in the new version
    for d in removes:
        files = [p for p, _, _ in parent if _under(p, d)]
        if not files:
            raise DatasetError(400, f'{latest["ref"] if latest else dataset["name"]} 里没有目录 {d}/'
                                    f'（写成版本里的相对路径，注意大小写）')
        gone.update(files)
        ops.append(Op('remove', d, removed_count=len(files)))
    new: List[Entry] = []
    for d, digest, replace in adds:
        data = store.read(digest)
        if data is None:
            raise DatasetError(400, f'目录 {d} 的文件清单还没有上传')
        try:
            content = parse_manifest(data)
        except ContractError as e:
            raise DatasetError(400, f'目录 {d} 的文件清单不合格：{e}') from None
        if not content:
            raise DatasetError(400, f'目录 {d} 里没有文件')
        exact = [p for p, _, _ in parent if _under(p, d)]
        other_case = sorted({p[: len(d)] for p, _, _ in parent
                             if fold(p).startswith(fold(d) + '/') and not _under(p, d)})
        if other_case:
            conflicts.append({'dir': d, 'files': 0, 'ref': latest['ref'], 'other_case': other_case[0]})
        elif exact and not replace:
            conflicts.append({'dir': d, 'files': len(exact), 'ref': latest['ref'], 'other_case': None})
        gone.update(exact)
        size = sum(s for _, _, s in content)
        ops.append(Op('replace' if exact else 'add', d, len(content), size, len(exact), digest))
        new += [(f'{d}/{p}', h, s) for p, h, s in content]

    kept = [e for e in parent if e[0] not in gone]
    entries = sorted(kept + new)
    p = Plan(dataset, latest, ops, entries, kept, new, conflicts)
    if not entries:
        raise DatasetError(400, f'去掉这些目录以后 {dataset["name"]} 就没有文件了；版本不能是空的')
    for c in conflicts:
        if c['other_case']:
            p.reasons.append(f'池里已有只差大小写的目录 {c["other_case"]}/（{c["ref"]}），在 Windows 上会和 {c["dir"]}/ '
                             f'混在一起；用同样的写法，或换一个名称')
        else:
            p.reasons.append(f'池里已有 {c["dir"]}/（{c["ref"]}，{c["files"]:,} 个文件）；要整个换成这份就加 --replace'
                             f'（网页上勾选「替换已有目录」），要并存就换一个名称（--to）')
    # the checks of fw dataset import, on the whole result; what an import may allow with a flag counts only
    # where a new folder is involved (the rest of the pool was accepted when it came in)
    found = classify_paths(path for path, _, _ in entries)
    fresh = {path for path, _, _ in new}
    fresh_stems = {_stem(path) for path in fresh}
    found['others'] = [x for x in found['others'] if x in fresh]
    found['same_stem_images'] = [g for g in found['same_stem_images'] if any(x in fresh for x in g)]
    p.reasons += blocking_reasons(found, allow_other_files, force)
    p.checks = {
        'bad_paths': found['bad_paths'], 'collisions': found['collisions'],
        'same_stem_images': found['same_stem_images'], 'others': found['others'],
        'unlabeled_images': [s for s in found['unlabeled_images'] if s in fresh_stems],
        'orphan_labels': [s for s in found['orphan_labels'] if s in fresh_stems],
        'meta': [x for x in found['meta'] if x in fresh]}
    if not new:  # only removals: labels that lose their images by it are worth a look
        before = set(classify_paths(path for path, _, _ in parent)['orphan_labels'])
        p.checks['orphan_labels'] = [s for s in found['orphan_labels'] if s not in before]
    p.duplicates = _duplicates(store, kept, new, entries)
    p.changes = {k: [f'{a} → {b}' for a, b in v] if k == 'renamed' else v
                 for k, v in diff_entries(parent, entries).items()}
    if latest is not None and not p.reasons and manifest_hash(manifest_bytes(entries)) == latest['manifest_sha256']:
        p.reasons.append(f'结果和 {latest["ref"]} 完全一样，不用建新版本')
    return p


def create(conn, store: Store, user_id: int, name: str, add: List[dict], remove: List[str],
           expect_latest_id: Optional[int], allow_other_files: bool = False, force: bool = False,
           source: str = '', note: str = '', before_freeze=None) -> dict:
    """Make the version that plan() describes. expect_latest_id: the newest version the caller saw in its
    preview; if the dataset moved on meanwhile nothing is made (409) and the caller previews again."""
    p = plan(conn, store, name, add, remove, allow_other_files, force)
    if (p.latest['id'] if p.latest else None) != expect_latest_id:
        now = p.latest['ref'] if p.latest else '没有版本'
        raise DatasetError(409, f'{p.dataset["name"]} 在这期间有了变化（最新是 {now}）；请重新运行，以最新版本为基础')
    if p.reasons:
        raise DatasetError(409 if p.conflicts else 400, '没有建版本：' + '；'.join(p.reasons))
    digest, _ = store.put_bytes(manifest_bytes(p.entries))

    def record(c, version_id: int) -> None:
        for i, o in enumerate(p.ops):
            c.execute('INSERT INTO version_changes (version_id, position, op, dir, file_count, total_bytes, '
                      'removed_count, content_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                      (version_id, i, o.op, o.dir, o.file_count, o.total_bytes, o.removed_count,
                       bytes.fromhex(o.content_sha256) if o.content_sha256 else None))

    version = datasets.create_version(conn, store, user_id, p.dataset['name'], digest,
                                      p.latest['id'] if p.latest else None, expect_latest_id, source, note,
                                      before_freeze=before_freeze, record=record, inherited=set(p.inherited))
    out = p.summary()
    out['version'] = version
    return out


# --- reading it back -------------------------------------------------------------------------------------
def attach(conn, versions: List[dict]) -> List[dict]:
    """Add 'ops' (how the server made it from its parent; [] for a version imported whole) to version dicts."""
    by_id: Dict[int, List[dict]] = {v['id']: [] for v in versions}
    if by_id:
        rows = conn.execute("SELECT version_id, op, dir, file_count, total_bytes, removed_count, "
                            "encode(content_sha256, 'hex') AS content_sha256 FROM version_changes "
                            'WHERE version_id = ANY(%s) ORDER BY version_id, position', (list(by_id),)).fetchall()
        for r in rows:
            by_id[r['version_id']].append(Op(r['op'], r['dir'], r['file_count'], r['total_bytes'],
                                             r['removed_count'], r['content_sha256']).row())
    for v in versions:
        v['ops'] = by_id[v['id']]
    return versions


def verify_inputs(conn) -> Tuple[List[str], Dict[str, Tuple[Optional[int], str]], int]:
    """For datasets.verify (fw-server verify): the uploaded file lists of added folders."""
    wanted: Dict[str, Tuple[Optional[int], str]] = {}
    rows = conn.execute("SELECT encode(c.content_sha256, 'hex') AS d, d.name || '@' || v.number AS ref, c.dir "
                        'FROM version_changes c JOIN versions v ON v.id = c.version_id '
                        'JOIN datasets d ON d.id = v.dataset_id WHERE c.content_sha256 IS NOT NULL '
                        'ORDER BY c.version_id, c.position').fetchall()
    for r in rows:
        wanted.setdefault(r['d'], (None, f'{r["ref"]} 新增目录 {r["dir"]} 的文件清单'))
    return [], wanted, 0
