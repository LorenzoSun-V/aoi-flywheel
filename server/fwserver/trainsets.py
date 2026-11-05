"""Training-set snapshots: which samples a training run uses, which split each one is in, and the class table.

A snapshot is a version of a dataset of kind 'trainset'. Its files are the tree a training machine gets:

    train/<site>/<line>/<path in the pool>      image, and its label next to it (same stem)
    val/...  test/...
    classes.txt                                 one class name per line; the line number is the class id
    trainset.json                               sources, classes, split rule and counts (docs/trainset-format.md)

so it is frozen, fetched (fw get), exported and verified exactly like any other version, and its manifest hash
identifies it. What is frozen is the RESULT of the split, never the rule: fetching a snapshot lays out the
same files in the same splits every time, whatever code or seed reads it.

Making one (plan, then create):
1. resolve the source pool versions; every source needs a site and a line (they name its folder);
2. pair every image with the label of the same stem in its folder; refuse orphan labels and ambiguous pairs;
3. read every label and refuse class names that are not in the class table (or YOLO ids out of range);
4. assign splits: from a split file; or continuing the previous snapshot (old samples keep their split, new
   ones go to test unless told otherwise, optionally old test merged into train/val); or fresh (chosen test
   folders to test, the rest to train/val). train/val is cut by ratio in an order fixed by the seed;
5. build the tree, store classes.txt, trainset.json and the manifest, then datasets.create_version with
   trainset=True, which writes the trainset rows in the same transaction before the version is frozen.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple

from fw.contract import IMAGE_EXTS, LABEL_EXTS, META_NAMES, collisions, manifest_bytes, path_problem

from . import datasets
from .datasets import DatasetError
from .store import Store

FORMAT = 'fw-trainset 1'
SPLITS = ('train', 'val', 'test')
NEW_TO = ('test', 'trainval')
MAX_CLASSES = 1000
EXAMPLES = 5

File = Tuple[str, str, int]  # (path in its pool version, sha256, size)


@dataclass
class Sample:
    key: str  # <site>/<line>/<image path in its pool version>; the same image keeps its key across snapshots
    site: str
    line: str
    image: File
    label: Optional[File]  # None: a negative sample (an image with no label)
    names: List[str] = field(default_factory=list)  # class name of every shape in the label
    split: str = ''


@dataclass
class Options:
    sources: List[str]
    classes: List[str]
    val: float = 0.2
    seed: int = 0
    test_dirs: List[str] = field(default_factory=list)
    merge_test: bool = False
    new_to: str = 'test'
    split: Optional[Dict[str, str]] = None  # key -> split, from a split file
    fresh: bool = False  # do not continue the previous snapshot
    note: str = ''


def _examples(items, n: int = EXAMPLES) -> str:
    items = list(items)
    more = f' 等 {len(items)} 个' if len(items) > n else ''
    return '、'.join(str(i) for i in items[:n]) + more


def _stem(path: str) -> str:
    suffix = PurePosixPath(path).suffix
    return path[: len(path) - len(suffix)] if suffix else path


def _parse_ref(ref: str) -> Tuple[str, str]:
    name, sep, number = (ref or '').partition('@')
    if not name:
        raise DatasetError(400, f'版本写法不对：{ref!r}，应为 名称@序号')
    return name, (number if sep else 'latest')


def check_options(o: Options) -> None:
    if not o.sources:
        raise DatasetError(400, '至少要一个来源版本（--from）')
    if not o.classes:
        raise DatasetError(400, '类别表是空的')
    if len(o.classes) > MAX_CLASSES:
        raise DatasetError(400, f'类别不能超过 {MAX_CLASSES} 个')
    for name in o.classes:
        if not name or name != name.strip() or any(c in name for c in '\t\r\n'):
            raise DatasetError(400, f'类别名不能为空、不能有首尾空格、制表符或换行：{name!r}')
    dup = [n for n, c in Counter(o.classes).items() if c > 1]
    if dup:
        raise DatasetError(400, f'类别表里有重复的名称：{_examples(dup)}')
    if not 0 <= o.val < 1 or math.isnan(o.val):
        raise DatasetError(400, 'val 比例应在 0 到 1 之间（不含 1）')
    if o.new_to not in NEW_TO:
        raise DatasetError(400, f'新样本的去向只能是 {"、".join(NEW_TO)}')
    if o.split is not None and (o.test_dirs or o.merge_test):
        raise DatasetError(400, '用了划分清单，就不能再用 --test-dirs 或 --merge-test')
    if o.split is not None:
        bad = sorted({s for s in o.split.values() if s not in SPLITS})
        if bad:
            raise DatasetError(400, f'划分清单里只能写 {"、".join(SPLITS)}，有 {_examples(bad)}')


# --- sources and samples ---------------------------------------------------------------------------
def _sources(conn, refs: List[str]) -> List[Tuple[dict, dict]]:
    out = []
    for ref in refs:
        name, number = _parse_ref(ref)
        version = datasets.get_version(conn, name, number)
        dataset = datasets.get_dataset(conn, name)
        if dataset['kind'] == 'trainset':
            raise DatasetError(400, f'{version["ref"]} 是训练集快照，来源要用数据池版本')
        if not dataset['site'] or not dataset['line']:
            raise DatasetError(400, f'{dataset["name"]} 没有填现场或产线，取出时的目录 <现场>/<产线>/ 定不下来；'
                                    f'换一个填了现场和产线的数据集')
        out.append((version, dataset))
    seen = Counter(v['id'] for v, _ in out)
    if any(c > 1 for c in seen.values()):
        raise DatasetError(400, '同一个来源版本写了两次')
    return out


def _files(conn, version_id: int) -> List[File]:
    return [(r['path'], r['d'], r['size']) for r in conn.execute(
        "SELECT path, encode(sha256, 'hex') AS d, size FROM version_files WHERE version_id = %s ORDER BY path",
        (version_id,))]


def _samples(conn, sources) -> Tuple[Dict[str, Sample], List[str]]:
    samples: Dict[str, Sample] = {}
    problems = []
    for version, dataset in sources:
        site, line = dataset['site'], dataset['line']
        images, labels = defaultdict(list), defaultdict(list)
        for f in _files(conn, version['id']):
            name = PurePosixPath(f[0]).name.lower()
            ext = PurePosixPath(f[0]).suffix.lower()
            if name in META_NAMES:
                continue
            if ext in IMAGE_EXTS:
                images[_stem(f[0])].append(f)
            elif ext in LABEL_EXTS:
                labels[_stem(f[0])].append(f)
        orphans = sorted(s for s in labels if s not in images)
        if orphans:
            problems.append(f'{version["ref"]} 有 {len(orphans)} 个标注没有对应的图片：{_examples(orphans)}')
        twins = sorted(s for s, v in images.items() if len(v) > 1)
        if twins:
            problems.append(f'{version["ref"]} 有 {len(twins)} 组同名不同扩展名的图片，标注对不上：{_examples(twins)}')
        double = sorted(s for s, v in labels.items() if len(v) > 1 and s in images)
        if double:
            problems.append(f'{version["ref"]} 有 {len(double)} 张图同时有 JSON 和 txt 标注，不知道用哪个：'
                            f'{_examples(double)}')
        for stem, imgs in images.items():
            if len(imgs) > 1 or len(labels.get(stem, ())) > 1:
                continue
            image = imgs[0]
            key = f'{site}/{line}/{image[0]}'
            if key in samples:
                problems.append(f'两个来源都有 {key}（同一现场和产线的两个数据集里有同一路径）')
                continue
            label = labels[stem][0] if stem in labels else None
            samples[key] = Sample(key, site, line, image, label)
    return samples, problems


def _read_labels(store: Store, samples: Dict[str, Sample], classes: List[str]) -> List[str]:
    """Fill each sample's class names; refuse labels that cannot be read and classes not in the table."""
    known = set(classes)
    parsed: Dict[str, object] = {}  # label sha256 -> list of names, or a problem string
    unreadable, unknown = [], defaultdict(list)
    for s in samples.values():
        if s.label is None:
            continue
        digest = s.label[1]
        if digest not in parsed:
            parsed[digest] = _parse_label(store, s.label, classes)
        result = parsed[digest]
        if isinstance(result, str):
            unreadable.append(f'{s.site}/{s.line}/{s.label[0]}：{result}')
            continue
        s.names = result
        for n in result:
            if n not in known:
                unknown[n].append(f'{s.site}/{s.line}/{s.label[0]}')
    problems = []
    if unreadable:
        problems.append(f'有 {len(unreadable)} 个标注读不了：{_examples(unreadable, 3)}')
    for name, where in sorted(unknown.items()):
        problems.append(f'类别 {name!r} 不在类别表里，{len(where)} 个标注用到，例如 {_examples(where, 3)}')
    return problems


def _parse_label(store: Store, label: File, classes: List[str]):
    data = store.read(label[1])
    if data is None:
        return '服务器上没有这个文件，先运行 fw-server verify'
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return '不是 UTF-8'
    if label[0].lower().endswith('.json'):
        try:
            doc = json.loads(text)
        except ValueError as e:
            return f'不是合法的 JSON（{e}）'
        shapes = doc.get('shapes') if isinstance(doc, dict) else None
        if not isinstance(shapes, list):
            return '不是 LiteAnno / X-AnyLabeling 标注（没有 shapes 列表）'
        names = []
        for shape in shapes:
            name = shape.get('label') if isinstance(shape, dict) else None
            if not isinstance(name, str) or not name:
                return '有形状没有类别名'
            names.append(name)
        return names
    names = []
    for n, row in enumerate(text.splitlines(), start=1):
        fields = row.split()
        if not fields:
            continue
        try:
            cid = int(fields[0])
        except ValueError:
            return f'第 {n} 行的类别编号不是整数'
        if not 0 <= cid < len(classes):
            return f'第 {n} 行的类别编号 {cid} 超出类别表（0 到 {len(classes) - 1}）'
        names.append(classes[cid])
    return names


# --- the previous snapshot and the split ---------------------------------------------------------------
def _latest_trainset(conn, name: str) -> Optional[dict]:
    row = conn.execute('SELECT id, kind FROM datasets WHERE lower(name) = lower(%s)', (name,)).fetchone()
    if row is None:
        return None
    if row['kind'] != 'trainset':
        raise DatasetError(409, f'{name} 已经是一个数据集的名称（数据集和训练集快照共用名称），换一个名称')
    latest = conn.execute('SELECT id FROM versions WHERE dataset_id = %s ORDER BY number DESC LIMIT 1',
                          (row['id'],)).fetchone()
    return datasets.get_version_by_id(conn, latest['id']) if latest else None


def _previous_splits(conn, previous: dict) -> Dict[str, Tuple[str, Optional[str]]]:
    """key -> (split, label sha256) of every sample in a snapshot, read back from its tree."""
    out: Dict[str, Tuple[str, Optional[str]]] = {}
    labels: Dict[str, str] = {}
    images: Dict[str, Tuple[str, str]] = {}
    for path, digest, _ in _files(conn, previous['id']):
        split, sep, rest = path.partition('/')
        if not sep or split not in SPLITS:
            continue
        ext = PurePosixPath(rest).suffix.lower()
        if ext in IMAGE_EXTS:
            images[_stem(rest)] = (split, rest)
        elif ext in LABEL_EXTS:
            labels[_stem(rest)] = digest
    for stem, (split, rest) in images.items():
        out[rest] = (split, labels.get(stem))
    return out


def _order(seed: int, key: str) -> bytes:
    return hashlib.sha256(f'{seed}\n{key}'.encode('utf-8')).digest()


def _cut(keys: List[str], val: float, seed: int) -> Dict[str, str]:
    """train/val for these keys: the same keys, ratio and seed always give the same cut."""
    ordered = sorted(keys, key=lambda k: _order(seed, k))
    n_val = round(len(ordered) * val)
    return {k: ('val' if i < n_val else 'train') for i, k in enumerate(ordered)}


def _in_dirs(s: Sample, dirs: List[str]) -> Optional[str]:
    for d in dirs:
        if s.image[0].startswith(d + '/') or s.key.startswith(d + '/'):
            return d
    return None


def _assign(samples: Dict[str, Sample], o: Options, previous: Optional[Dict[str, Tuple[str, Optional[str]]]]
            ) -> Tuple[dict, List[str]]:
    problems = []
    dirs = [d.strip('/') for d in o.test_dirs if d.strip('/')]
    used = {d: 0 for d in dirs}
    if o.split is not None:
        missing = sorted(k for k in samples if k not in o.split)
        extra = sorted(k for k in o.split if k not in samples)
        if missing:
            problems.append(f'划分清单漏了 {len(missing)} 个样本：{_examples(missing, 3)}')
        if extra:
            problems.append(f'划分清单里有 {len(extra)} 个路径不在来源版本里：{_examples(extra, 3)}')
        for k, s in samples.items():
            s.split = o.split.get(k, '')
        return {'mode': 'split-file', 'split_count': len(o.split)}, problems
    pool = []
    for k, s in samples.items():
        hit = _in_dirs(s, dirs)
        if hit is not None:
            used[hit] += 1
        if previous is not None and k in previous:
            old = previous[k][0]
            if old == 'test' and o.merge_test and hit is None:
                pool.append(k)
            else:
                s.split = old
        elif hit is not None or (previous is not None and o.new_to == 'test'):
            s.split = 'test'
        else:
            pool.append(k)
    for k, split in _cut(pool, o.val, o.seed).items():
        samples[k].split = split
    unused = [d for d, n in used.items() if n == 0]
    if unused:
        problems.append(f'--test-dirs 里的 {_examples(unused)} 没有任何样本（写错了？）')
    rule = {'mode': 'continue' if previous is not None else 'fresh', 'val': o.val, 'seed': o.seed,
            'test_dirs': dirs, 'merge_test': o.merge_test}
    if previous is not None:
        rule['new_to'] = o.new_to
    return rule, problems


# --- the tree ---------------------------------------------------------------------------------------
def _counts(samples: Dict[str, Sample], classes: List[str]) -> dict:
    counts = {}
    for split in SPLITS:
        members = [s for s in samples.values() if s.split == split]
        boxes = Counter(n for s in members for n in s.names)
        counts[split] = {'samples': len(members), 'negatives': sum(1 for s in members if s.label is None),
                         'boxes': {c: boxes.get(c, 0) for c in classes}}
    return counts


def _changes(samples: Dict[str, Sample], previous: Optional[Dict[str, Tuple[str, Optional[str]]]]) -> dict:
    if previous is None:
        return {}
    moved = sorted(f'{k}：{previous[k][0]} → {s.split}' for k, s in samples.items()
                   if k in previous and previous[k][0] != s.split)
    relabeled = sum(1 for k, s in samples.items()
                    if k in previous and previous[k][1] != (s.label[1] if s.label else None))
    added = sum(1 for k in samples if k not in previous)
    removed = sum(1 for k in previous if k not in samples)
    return {'added': added, 'removed': removed, 'moved': len(moved), 'moved_examples': moved[:EXAMPLES],
            'relabeled': relabeled}


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _tree(samples: Dict[str, Sample], classes_data: bytes, info_data: bytes) -> Tuple[List[File], List[str]]:
    entries: List[File] = [('classes.txt', hashlib.sha256(classes_data).hexdigest(), len(classes_data)),
                           ('trainset.json', hashlib.sha256(info_data).hexdigest(), len(info_data))]
    for s in samples.values():
        base = f'{s.split}/{s.site}/{s.line}/'
        entries.append((base + s.image[0], s.image[1], s.image[2]))
        if s.label is not None:
            entries.append((base + s.label[0], s.label[1], s.label[2]))
    problems = [f'{p}：{why}' for p, why in ((e[0], path_problem(e[0])) for e in entries) if why]
    problems += collisions(e[0] for e in entries)
    return entries, problems


@dataclass
class Plan:
    name: str
    sources: list
    latest: Optional[dict]  # the newest snapshot of this name: the new one's parent
    previous: Optional[dict]  # the snapshot whose splits are continued (None with fresh)
    samples: Dict[str, Sample]
    rule: dict
    counts: dict
    changes: dict
    problems: List[str]
    classes_data: bytes = b''
    info_data: bytes = b''
    entries: List[File] = field(default_factory=list)

    def summary(self) -> dict:
        return {'name': self.name, 'sources': [v['ref'] for v, _ in self.sources],
                'based_on': self.previous['ref'] if self.previous else None,
                'expect_latest_id': self.latest['id'] if self.latest else None,
                'rule': self.rule, 'counts': self.counts, 'changes': self.changes, 'problems': self.problems,
                'samples': len(self.samples), 'file_count': len(self.entries),
                'total_bytes': sum(e[2] for e in self.entries)}


def plan(conn, store: Store, name: str, o: Options) -> Plan:
    if not datasets.NAME.match(name or ''):
        raise DatasetError(400, '名称只能用字母、数字和 . _ -，以字母或数字开头，最多 64 个字符')
    check_options(o)
    sources = _sources(conn, o.sources)
    latest = _latest_trainset(conn, name)
    previous = None if o.fresh else latest
    samples, problems = _samples(conn, sources)
    problems += _read_labels(store, samples, o.classes)
    prev_splits = _previous_splits(conn, previous) if previous is not None else None
    rule, more = _assign(samples, o, prev_splits)
    problems += more
    if not samples:
        problems.append('来源版本里没有图片')
    counts = _counts(samples, o.classes)
    p = Plan(name, sources, latest, previous, samples, rule, counts, _changes(samples, prev_splits), problems)
    if problems:
        return p
    p.classes_data = ('\n'.join(o.classes) + '\n').encode('utf-8')
    info = {'format': FORMAT, 'name': name,
            'sources': [{'ref': v['ref'], 'manifest_sha256': v['manifest_sha256'], 'site': d['site'],
                         'line': d['line']} for v, d in sources],
            'based_on': previous['ref'] if previous else None, 'classes': o.classes, 'rule': rule, 'counts': counts}
    p.info_data = _json_bytes(info)
    p.entries, p.problems = _tree(samples, p.classes_data, p.info_data)
    if latest is not None and not p.problems and _same_content(conn, latest, p.entries):
        p.problems.append(f'和 {latest["ref"]} 完全一样（样本、划分、标注和类别表都没变），不用建新快照')
    return p


def _same_content(conn, latest: dict, entries: List[File]) -> bool:
    """Everything but trainset.json (which names the snapshot it is based on) is the same as in latest."""
    def files(items):
        return {(path, digest) for path, digest, _ in items if path != 'trainset.json'}
    return files(entries) == files(_files(conn, latest['id']))


def create(conn, store: Store, user_id: int, name: str, o: Options, expect_latest_id: Optional[int],
           before_freeze=None) -> dict:
    """Build the snapshot again exactly as planned and freeze it. expect_latest_id is the newest snapshot of
    this name the person saw in the preview: if another one was made meanwhile, nothing is created (409)."""
    p = plan(conn, store, name, o)
    if (p.latest['id'] if p.latest else None) != expect_latest_id:  # the preview was based on another snapshot
        now = p.latest['ref'] if p.latest else '没有版本'
        raise DatasetError(409, f'{name} 在这期间有了变化（最新是 {now}）；请重新运行 fw trainset create')
    if p.problems:
        raise DatasetError(400, '没有建训练集快照：\n  ' + '\n  '.join(p.problems))
    for data in (p.classes_data, p.info_data):
        store.put_bytes(data)
    manifest = manifest_bytes(p.entries)
    digest, _ = store.put_bytes(manifest)
    if p.latest is None:
        sites = {(d['site'], d['line']) for _, d in p.sources}
        site, line = next(iter(sites)) if len(sites) == 1 else ('', '')
        try:
            datasets.create_dataset(conn, user_id, name, site, line, 'trainset', note=o.note, internal=True)
        except DatasetError as e:
            if e.status != 409:  # made by someone else meanwhile: create_version below finds out what they did
                raise

    def record(c, version_id):
        c.execute('INSERT INTO trainset_versions (version_id, based_on_id, classes, rule, counts) '
                  'VALUES (%s, %s, %s, %s, %s)',
                  (version_id, p.previous['id'] if p.previous else None, json.dumps(o.classes, ensure_ascii=False),
                   json.dumps(p.rule, ensure_ascii=False), json.dumps(p.counts, ensure_ascii=False)))
        for i, (v, _) in enumerate(p.sources):
            c.execute('INSERT INTO trainset_sources (version_id, position, source_id) VALUES (%s, %s, %s)',
                      (version_id, i, v['id']))

    version = datasets.create_version(conn, store, user_id, name, digest, p.latest['id'] if p.latest else None,
                                      expect_latest_id, source='fw trainset create', note=o.note,
                                      before_freeze=before_freeze, trainset=True, record=record)
    version.update(info(conn, version['id']))
    version['summary'] = p.summary()
    return version


def info(conn, version_id: int) -> dict:
    row = conn.execute('SELECT based_on_id, classes, rule, counts FROM trainset_versions WHERE version_id = %s',
                       (version_id,)).fetchone()
    if row is None:
        raise DatasetError(404, '这个版本不是训练集快照')
    sources = [datasets.get_version_by_id(conn, r['source_id'])['ref'] for r in conn.execute(
        'SELECT source_id FROM trainset_sources WHERE version_id = %s ORDER BY position', (version_id,))]
    based_on = datasets.get_version_by_id(conn, row['based_on_id'])['ref'] if row['based_on_id'] else None
    return {'trainset': {'classes': row['classes'], 'rule': row['rule'], 'counts': row['counts'],
                         'sources': sources, 'based_on': based_on}}


def get(conn, name: str, number: str) -> dict:
    version = datasets.get_version(conn, name, number)
    if version['kind'] != 'trainset':
        raise DatasetError(400, f'{version["ref"]} 不是训练集快照')
    version.update(info(conn, version['id']))
    return version


def list_trainsets(conn) -> List[dict]:
    return [d for d in datasets.list_datasets(conn) if d['kind'] == 'trainset']
