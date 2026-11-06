"""Training-set snapshots: which samples a training run uses, which split each one is in, and the class table.

A snapshot is a version of a dataset of kind 'trainset'. Its files (docs/trainset-format.md):

    classes.txt                                  one class name per line; the line number is the class id
    trainset.json                                sources, classes, split rule, counts, formats
    images/<split>/<site>/<line>/<path in pool>  every image
    labels/<split>/<site>/<line>/<stem>.txt      YOLO detection label (made from the JSON, or the pool's txt)
    json/<split>/<site>/<line>/<stem>.json       the pool's LiteAnno / X-AnyLabeling label, unchanged (made from
                                                 the boxes when the pool has a YOLO txt)
    coco/<split>.json                            COCO detection annotations of the split

so it is frozen, fetched, exported and verified exactly like any other version, and its manifest hash
identifies it; fw get lays it out per format (fw.contract.trainset_layout). What is frozen is the RESULT: the
split of every sample and the converted labels, never the rule: the same files come back every time, whatever
code or seed reads them.

Making one (plan, then create):
1. resolve the source pool versions; every source needs a site and a line (they name its folder);
2. pair every image with its label: the same stem in its folder, or the .txt YOLO finds (images/ -> labels/);
   refuse orphan labels and ambiguous pairs (label_source picks json or txt for an image with both);
3. read every label (.txt ids by label_classes, else by the class table) and apply class_map (rename, or drop
   a class: background); refuse class names that are not in the class table, map sources no label has, YOLO
   ids out of range, shapes that cannot be a detection box and boxes outside the image;
4. assign splits: from a split file; or continuing the previous snapshot (old samples keep their split, new
   ones go to test unless told otherwise, optionally old test merged into train/val); or fresh (chosen test
   folders to test, the rest to train/val). train/val is cut by ratio in an order fixed by the seed;
5. build the tree with the converted labels, store them, classes.txt, trainset.json and the manifest, then
   datasets.create_version with trainset=True, which writes the trainset rows in the same transaction before
   the version is frozen.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple

from fw.contract import (IMAGE_EXTS, LABEL_EXTS, META_NAMES, TRAINSET_FORMATS, collisions, manifest_bytes,
                         path_problem, trainset_layout)

from . import datasets, formats
from .datasets import DatasetError
from .formats import Label, Size
from .store import Store

FORMAT = 'fw-trainset 1'
SPLITS = ('train', 'val', 'test')
NEW_TO = ('test', 'trainval')
MAX_CLASSES = 1000
EXAMPLES = 5
BOM = b'\xef\xbb\xbf'

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
    parsed: Optional[Label] = None
    size: Optional[Size] = None  # (width, height): from the JSON label, else from the image header


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
    require_labels: bool = False  # refuse images without a label instead of keeping them as negatives
    label_source: Optional[str] = None  # 'json' or 'txt': which label to use for an image that has both
    label_classes: Optional[List[str]] = None  # the class table the .txt labels are numbered by (None: classes)
    class_map: Optional[Dict[str, str]] = None  # class in the labels -> class in classes, or '' (drop the box)


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


def _bad_name(name) -> bool:
    return not isinstance(name, str) or not name or name != name.strip() or any(c in name for c in '\t\r\n')


def _check_table(names: List[str], what: str) -> None:
    if not names:
        raise DatasetError(400, f'{what}是空的')
    if len(names) > MAX_CLASSES:
        raise DatasetError(400, f'{what}不能超过 {MAX_CLASSES} 个类别')
    for name in names:
        if _bad_name(name):
            raise DatasetError(400, f'类别名不能为空、不能有首尾空格、制表符或换行：{name!r}')
    dup = [n for n, c in Counter(names).items() if c > 1]
    if dup:
        raise DatasetError(400, f'{what}里有重复的名称：{_examples(dup)}')


def check_options(o: Options) -> None:
    if not o.sources:
        raise DatasetError(400, '至少要一个来源版本（--from）')
    _check_table(o.classes, '类别表')
    if o.label_classes is not None:
        _check_table(o.label_classes, 'txt 标注的类别表（--label-classes）')
    if o.class_map:
        if len(o.class_map) > MAX_CLASSES:
            raise DatasetError(400, f'类别映射不能超过 {MAX_CLASSES} 条')
        for old, new in o.class_map.items():
            if _bad_name(old):
                raise DatasetError(400, f'映射的源类别名不能为空、不能有首尾空格、制表符或换行：{old!r}')
            if new == old:
                raise DatasetError(400, f'映射 {old}={new} 的源和目标一样，不用写')
            if new and new not in o.classes:
                raise DatasetError(400, f'映射 {old}={new} 的目标 {new!r} 不在类别表里（要去掉这类框就写 {old}=）')
    if not 0 <= o.val < 1 or math.isnan(o.val):
        raise DatasetError(400, 'val 比例应在 0 到 1 之间（不含 1）')
    if o.new_to not in NEW_TO:
        raise DatasetError(400, f'新样本的去向只能是 {"、".join(NEW_TO)}')
    if o.label_source is not None and o.label_source not in LABEL_SOURCES:
        raise DatasetError(400, f'标注来源只能是 {"、".join(LABEL_SOURCES)}，不是 {o.label_source!r}')
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


def _yolo_label_stem(image_path: str) -> Optional[str]:
    """Where ultralytics (and aoi-deteval) look for an image's label: its path with the LAST folder named images
    turned into labels, e.g. b1/images/x/a.png -> b1/labels/x/a (then .txt). None without such a folder."""
    parts = image_path.split('/')
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == 'images':
            return _stem('/'.join(parts[:i] + ['labels'] + parts[i + 1:]))
    return None


LABEL_SOURCES = ('json', 'txt')


def _samples(conn, sources, label_source: Optional[str] = None) -> Tuple[Dict[str, Sample], List[str]]:
    """Pair every image with its label: a .json or .txt of the same name in its folder, or the .txt where YOLO
    looks for it (images/ -> labels/). An image with more than one: label_source ('json' or 'txt') picks the
    kind; still more than one, or none given, is refused. Labels no image claims are refused (orphans)."""
    samples: Dict[str, Sample] = {}
    stems: Dict[str, str] = {}  # <site>/<line>/<image path without extension> -> key: their labels share a path
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
        candidates: Dict[str, List[File]] = {}
        claimed = set()
        for stem in images:
            found = list(labels.get(stem, ()))
            yolo = _yolo_label_stem(images[stem][0][0])
            if yolo is not None:
                found += [f for f in labels.get(yolo, ()) if f[0].lower().endswith('.txt')]
            candidates[stem] = found
            claimed.update(f[0] for f in found)
        orphans = sorted(f[0] for group in labels.values() for f in group if f[0] not in claimed)
        if orphans:
            problems.append(f'{version["ref"]} 有 {len(orphans)} 个标注没有对应的图片（同目录同名，或 YOLO 的 '
                            f'images/ 对应 labels/）：{_examples(orphans)}')
        twins = sorted(s for s, v in images.items() if len(v) > 1)
        if twins:
            problems.append(f'{version["ref"]} 有 {len(twins)} 组同名不同扩展名的图片，标注对不上：{_examples(twins)}')
        mixed, double = [], []
        chosen: Dict[str, Optional[File]] = {}
        for stem, found in candidates.items():
            kinds = {PurePosixPath(f[0]).suffix.lower().lstrip('.') for f in found}
            pick = found
            if len(kinds) > 1:
                if label_source is None:
                    mixed.append(images[stem][0][0])
                    continue
                pick = [f for f in found if f[0].lower().endswith('.' + label_source)]
            if len(pick) > 1:
                double.append(images[stem][0][0])
                continue
            chosen[stem] = pick[0] if pick else None
        if mixed:
            problems.append(f'{version["ref"]} 有 {len(mixed)} 张图同时有 JSON 和 txt 标注，不知道用哪个；'
                            f'用 --label-source json 或 --label-source txt 选一种：{_examples(mixed)}')
        if double:
            problems.append(f'{version["ref"]} 有 {len(double)} 张图有两个同类的标注（同目录一个、labels/ 下一个），'
                            f'不知道用哪个：{_examples(double)}')
        for stem, imgs in images.items():
            if len(imgs) > 1 or stem not in chosen:
                continue
            image = imgs[0]
            key = f'{site}/{line}/{image[0]}'
            if key in samples:
                problems.append(f'两个来源都有 {key}（同一现场和产线的两个数据集里有同一路径）')
                continue
            twin = stems.setdefault(f'{site}/{line}/{stem}', key)
            if twin != key:
                problems.append(f'两个来源有同名不同扩展名的图片 {twin} 和 {key}，YOLO 标注会是同一个文件')
                continue
            samples[key] = Sample(key, site, line, image, chosen[stem])
    return samples, problems


def _header_size(store: Store, image: File) -> Optional[Size]:
    try:
        with open(store.path(image[1]), 'rb') as f:
            return formats.image_size(f)
    except OSError:
        return None


def _parse(store: Store, label: File, o: Options):
    """A label with o.class_map applied and, for a .txt, numbered by o.classes; or why it cannot be used."""
    data = store.read(label[1])
    if data is None:
        return '服务器上没有这个文件，先运行 fw-server verify'
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return '不是 UTF-8'
    class_map = o.class_map or {}
    if label[0].lower().endswith('.json'):
        mapped, moved = formats.remap_json(text, class_map)
        parsed = formats.parse_json(text if mapped is None else mapped)
        if not isinstance(parsed, str) and mapped is not None:
            parsed.data, parsed.moved = mapped.encode('utf-8'), moved
        return parsed
    parsed = formats.parse_txt(text, o.label_classes if o.label_classes is not None else o.classes)
    if isinstance(parsed, str):
        return parsed
    if data.startswith(BOM):
        parsed.data = data[len(BOM):]  # YOLO readers take the BOM for part of the first class id
    if formats.remap_txt(parsed, class_map, o.classes):
        parsed.data = formats.yolo_bytes(parsed, o.classes, None)
    return parsed


def _read_labels(store: Store, samples: Dict[str, Sample], o: Options) -> Tuple[List[str], List[str]]:
    """Read every label (each content once), apply the class map and check it can be a YOLO detection label:
    (problems, notices)."""
    known = set(o.classes)
    parsed: Dict[str, object] = {}  # label sha256 -> Label, or a problem string
    clipped_by: Dict[Tuple[str, Size], Label] = {}  # one clipped copy per label content and image size
    unreadable, no_size, outside = [], [], []
    unknown, refused = defaultdict(list), defaultdict(list)
    clipped, as_box, moved, emptied, txt = 0, Counter(), Counter(), [], 0
    for s in samples.values():
        if s.label is None:
            continue
        where = f'{s.site}/{s.line}/{s.label[0]}'
        digest = s.label[1]
        if digest not in parsed:
            parsed[digest] = _parse(store, s.label, o)
        label = parsed[digest]
        if isinstance(label, str):
            unreadable.append(f'{where}：{label}')
            continue
        moved.update(label.moved)
        if label.moved and not label.names:
            emptied.append(where)
        txt += label.kind == 'txt'
        if label.kind == 'json':
            size = label.size or _header_size(store, s.image)
            if size is None:
                no_size.append(where)
                continue
            key = (digest, size)
            if key not in clipped_by:
                copy = Label('json', label.names, list(label.boxes), size=size, shapes=label.shapes,
                             refused=label.refused, data=label.data, moved=label.moved)
                formats.clip(copy, size)
                clipped_by[key] = copy
            label = clipped_by[key]
            s.size = size
            if label.outside:
                outside.append(where)
            clipped += label.clipped
            as_box.update(label.shapes)
            for kind, n in label.refused.items():
                refused[kind].append(where)
        s.parsed = label
        s.names = label.names
        for n in label.names:
            if n not in known:
                unknown[n].append(where)
    problems = []
    if unreadable:
        problems.append(f'有 {len(unreadable)} 个标注读不了：{_examples(unreadable, 3)}')
    if no_size:
        problems.append(f'有 {len(no_size)} 个标注没有 imageWidth / imageHeight，图片尺寸也读不出来，'
                        f'转不成 YOLO：{_examples(no_size, 3)}')
    for name, where in sorted(unknown.items()):
        problems.append(f'类别 {name!r} 不在类别表里，{len(where)} 个标注用到，例如 {_examples(where, 3)}')
    for kind, where in sorted(refused.items()):
        problems.append(f'{len(where)} 个标注里有 {kind} 形状，不能当检测框（只收 rectangle、polygon、rotation），'
                        f'例如 {_examples(where, 3)}')
    if outside:
        problems.append(f'{len(outside)} 个标注里有框完全在图片外面：{_examples(outside, 3)}')
    unused = sorted(set(o.class_map or ()) - {old for old, _ in moved})
    if unused:
        problems.append(f'映射的源类别 {_examples(unused)} 在标注里一个框也没有（写错了？类别名区分大小写）')
    notices = []
    for (old, new), n in sorted(moved.items()):
        notices.append(f'映射：{old} 的 {n} 个框算作 {new}' if new else f'映射：{old} 的 {n} 个框去掉，当背景')
    if emptied:
        notices.append(f'{len(emptied)} 张图的框全被映射去掉了，当没有框的背景图收进来：{_examples(emptied, 3)}')
    if o.label_classes is not None and not txt:
        notices.append('没有用到 txt 标注，--label-classes 不起作用')
    if clipped:
        notices.append(f'{clipped} 个框超出图片边界，已裁到图片内')
    if as_box:
        kinds = '、'.join(f'{k} {n} 个' for k, n in sorted(as_box.items()))
        notices.append(f'这些形状按外接矩形当检测框：{kinds}')
    return problems, notices


def _formats(store: Store, samples: Dict[str, Sample]) -> Dict[str, Optional[str]]:
    """Which formats the snapshot can hold: format -> None, or why it is left out."""
    unsized = []
    for s in samples.values():
        if s.size is None:
            s.size = _header_size(store, s.image)
            if s.size is None:
                unsized.append(s.key)
    out: Dict[str, Optional[str]] = {'yolo': None, 'coco': None, 'json': None}
    if unsized:
        out['coco'] = f'{len(unsized)} 张图读不出尺寸（只认 PNG、JPEG、BMP、GIF、TIFF、WebP 的文件头）：{_examples(unsized, 3)}'
    txt = [k for k in unsized if samples[k].parsed is not None and samples[k].parsed.kind == 'txt']
    if txt:  # their JSON is made from the YOLO boxes, which needs the image size
        out['json'] = f'{len(txt)} 张图的标注是 YOLO txt，图片尺寸又读不出来，生成不了 LiteAnno JSON：{_examples(txt, 3)}'
    return out


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
    """key -> (split, sha256 of its label files) of every sample in a snapshot, read back from its tree: the
    files under json/ and labels/ of its split and stem (one of them is the pool's label, the other made from
    it). The key keeps the image's extension."""
    images: Dict[str, str] = {}
    found: Dict[str, Dict[str, str]] = {'json': {}, 'labels': {}}
    for path, digest, _ in _files(conn, previous['id']):
        top, _, rest = path.partition('/')
        split, sep, key = rest.partition('/')
        if not sep or split not in SPLITS:
            continue
        if top == 'images':
            images[key] = split
        elif top in found:
            found[top][f'{split}/{_stem(key)}'] = digest
    out = {}
    for key, split in images.items():
        where = f'{split}/{_stem(key)}'
        out[key] = (split, frozenset(d for d in (found['json'].get(where), found['labels'].get(where)) if d))
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
    def relabeled_(k: str, s: Sample) -> bool:
        if s.label is None:
            return bool(previous[k][1])
        mine = {s.label[1]}
        if s.parsed is not None and s.parsed.data is not None:  # frozen without its BOM
            mine.add(hashlib.sha256(s.parsed.data).hexdigest())
        return not mine & previous[k][1]

    relabeled = sum(1 for k, s in samples.items() if k in previous and relabeled_(k, s))
    added = sum(1 for k in samples if k not in previous)
    removed = sum(1 for k in previous if k not in samples)
    return {'added': added, 'removed': removed, 'moved': len(moved), 'moved_examples': moved[:EXAMPLES],
            'relabeled': relabeled}


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _tree(samples: Dict[str, Sample], classes: List[str], available: Dict[str, Optional[str]],
          classes_data: bytes, info_data: bytes) -> Tuple[List[File], Dict[str, bytes], List[str]]:
    """The snapshot's files (path, sha256, size), the converted contents to store (sha256 -> bytes), problems."""
    blobs: Dict[str, bytes] = {}

    def made(path: str, data: bytes) -> File:
        digest = hashlib.sha256(data).hexdigest()
        blobs[digest] = data
        return path, digest, len(data)

    entries: List[File] = [made('classes.txt', classes_data), made('trainset.json', info_data)]
    coco = defaultdict(list)
    for s in sorted(samples.values(), key=lambda s: s.key):
        rest = f'{s.split}/{s.site}/{s.line}/'
        stem = _stem(s.image[0])
        entries.append(('images/' + rest + s.image[0], s.image[1], s.image[2]))
        if s.parsed is not None and s.parsed.kind == 'txt':
            if s.parsed.data is not None:  # without its BOM, or with the classes a class map changed
                entries.append(made('labels/' + rest + stem + '.txt', s.parsed.data))
            else:  # already a YOLO detection label: as it is
                entries.append(('labels/' + rest + stem + '.txt', s.label[1], s.label[2]))
            if available['json'] is None:  # for LiteAnno: rectangles from the boxes
                name = PurePosixPath(s.image[0]).name
                entries.append(made('json/' + rest + stem + '.json',
                                    formats.liteanno_bytes(s.parsed, classes, s.size, name)))
        elif s.parsed is not None:
            entries.append(made('labels/' + rest + stem + '.txt', formats.yolo_bytes(s.parsed, classes, s.size)))
            if s.parsed.data is not None:  # the classes a class map changed
                entries.append(made('json/' + rest + stem + '.json', s.parsed.data))
            else:
                entries.append(('json/' + rest + stem + '.json', s.label[1], s.label[2]))
        if available['coco'] is None:
            boxes = formats.pixel_boxes(s.parsed, classes, s.size) if s.parsed is not None else []
            coco[s.split].append((s.key, s.size, boxes))
    for split in SPLITS:
        if coco.get(split):
            entries.append(made(f'coco/{split}.json', formats.coco_bytes(coco[split], classes)))
    problems = [f'{p}：{why}' for p, why in ((e[0], path_problem(e[0])) for e in entries) if why]
    for fmt in TRAINSET_FORMATS:  # every layout fw get can make must work on Windows too
        problems += collisions(e[0] for e in trainset_layout(entries, fmt))
    return entries, blobs, sorted(set(problems))


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
    notices: List[str] = field(default_factory=list)
    formats: Dict[str, Optional[str]] = field(default_factory=dict)  # format -> None, or why it is left out
    entries: List[File] = field(default_factory=list)
    blobs: Dict[str, bytes] = field(default_factory=dict)  # contents made for the snapshot, by sha256

    def summary(self) -> dict:
        return {'name': self.name, 'sources': [v['ref'] for v, _ in self.sources],
                'based_on': self.previous['ref'] if self.previous else None,
                'latest': self.latest['ref'] if self.latest else None,
                'expect_latest_id': self.latest['id'] if self.latest else None,
                'rule': self.rule, 'counts': self.counts, 'changes': self.changes, 'problems': self.problems,
                'notices': self.notices, 'task': formats.TASK,
                'formats': [f for f, why in self.formats.items() if why is None],
                'missing_formats': {f: why for f, why in self.formats.items() if why is not None},
                'samples': len(self.samples), 'file_count': len(self.entries),
                'total_bytes': sum(e[2] for e in self.entries)}


def plan(conn, store: Store, name: str, o: Options) -> Plan:
    if not datasets.NAME.match(name or ''):
        raise DatasetError(400, '名称只能用字母、数字和 . _ -，以字母或数字开头，最多 64 个字符')
    check_options(o)
    sources = _sources(conn, o.sources)
    latest = _latest_trainset(conn, name)
    previous = None if o.fresh or o.split is not None else latest  # a split file continues nothing
    samples, problems = _samples(conn, sources, o.label_source)
    label_problems, notices = _read_labels(store, samples, o)
    problems += label_problems
    prev_splits = _previous_splits(conn, previous) if previous is not None else None
    rule, more = _assign(samples, o, prev_splits)
    problems += more
    if o.label_source is not None:  # how the labels were read: kept with the split rule, for people
        rule['label_source'] = o.label_source
    if o.label_classes is not None:
        rule['label_classes'] = o.label_classes
    if o.class_map:
        rule['class_map'] = o.class_map
    if not samples:
        problems.append('来源版本里没有图片')
    if o.require_labels:
        bare = sorted(k for k, s in samples.items() if s.label is None)
        if bare:
            problems.append(f'要求每张图都有标注（--require-labels），有 {len(bare)} 张没有：{_examples(bare, 3)}')
    counts = _counts(samples, o.classes)
    p = Plan(name, sources, latest, previous, samples, rule, counts, _changes(samples, prev_splits), problems,
             notices)
    if previous is not None and _previous_classes(conn, previous) != o.classes:
        p.notices.append(f'类别表和 {previous["ref"]} 不一样，类别编号可能变了')
    empty = [split for split in ('train', 'val') if samples and not counts[split]['samples']]
    if empty:
        p.notices.append(f'没有 {"、".join(empty)} 样本：只能当测试集用，不能直接训练（ultralytics 要 train 和 val）')
    if problems:
        return p
    p.formats = _formats(store, samples)
    summary = p.summary()
    info = {'format': FORMAT, 'name': name, 'task': formats.TASK,
            'sources': [{'ref': v['ref'], 'manifest_sha256': v['manifest_sha256'], 'site': d['site'],
                         'line': d['line']} for v, d in sources],
            'based_on': previous['ref'] if previous else None, 'classes': o.classes, 'rule': rule, 'counts': counts,
            'formats': summary['formats'], 'missing_formats': summary['missing_formats']}
    classes_data = ('\n'.join(o.classes) + '\n').encode('utf-8')
    p.entries, p.blobs, p.problems = _tree(samples, o.classes, p.formats, classes_data, _json_bytes(info))
    if latest is not None and not p.problems and _same_content(conn, latest, p.entries):
        p.problems.append(f'和 {latest["ref"]} 完全一样（样本、划分、标注和类别表都没变），不用建新快照')
    return p


def _previous_classes(conn, previous: dict) -> Optional[list]:
    row = conn.execute('SELECT classes FROM trainset_versions WHERE version_id = %s', (previous['id'],)).fetchone()
    return row['classes'] if row else None


def _same_content(conn, latest: dict, entries: List[File]) -> bool:
    """Everything but trainset.json (which names the snapshot it is based on) is the same as in latest."""
    def files(items):
        return {(path, digest) for path, digest, _ in items if path != 'trainset.json'}
    return files(entries) == files(_files(conn, latest['id']))


def create(conn, store: Store, user_id: int, name: str, o: Options, expect_latest_id: Optional[int],
           before_freeze=None) -> dict:
    """Build the snapshot again exactly as planned and freeze it. expect_latest_id is the newest snapshot of
    this name the person saw in the preview: if another one was made meanwhile, nothing is created (409)."""
    loose = [ref for ref in o.sources if not _parse_ref(ref)[1].isdigit()]
    if loose:
        raise DatasetError(400, f'建快照时来源要写成具体版本（预览时定下的那个），例如 名称@3：{_examples(loose)}')
    p = plan(conn, store, name, o)
    if (p.latest['id'] if p.latest else None) != expect_latest_id:  # the preview was based on another snapshot
        now = p.latest['ref'] if p.latest else '没有版本'
        raise DatasetError(409, f'{name} 在这期间有了变化（最新是 {now}）；请重新运行 fw trainset create')
    if p.problems:
        raise DatasetError(400, '没有建训练集快照：\n  ' + '\n  '.join(p.problems))
    for data in p.blobs.values():
        store.put_bytes(data)
    manifest = manifest_bytes(p.entries)
    digest, _ = store.put_bytes(manifest)
    new = None
    taken = conn.execute('SELECT 1 FROM datasets WHERE lower(name) = lower(%s)', (name,)).fetchone()
    if taken is None:  # the name is taken together with the first version, or not at all
        sites = {(d['site'], d['line']) for _, d in p.sources}
        site, line = next(iter(sites)) if len(sites) == 1 else ('', '')
        new = {'site': site, 'line': line, 'kind': 'trainset', 'note': o.note}

    def record(c, version_id):
        summary = p.summary()
        c.execute('INSERT INTO trainset_versions (version_id, based_on_id, task, classes, rule, counts, formats) '
                  'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                  (version_id, p.previous['id'] if p.previous else None, formats.TASK,
                   json.dumps(o.classes, ensure_ascii=False), json.dumps(p.rule, ensure_ascii=False),
                   json.dumps(p.counts, ensure_ascii=False), json.dumps(summary['formats'])))
        for i, (v, _) in enumerate(p.sources):
            c.execute('INSERT INTO trainset_sources (version_id, position, source_id) VALUES (%s, %s, %s)',
                      (version_id, i, v['id']))

    version = datasets.create_version(conn, store, user_id, name, digest, p.latest['id'] if p.latest else None,
                                      expect_latest_id, source='fw trainset create', note=o.note,
                                      before_freeze=before_freeze, trainset=True, record=record, new_dataset=new)
    version.update(info(conn, version['id']))
    version['summary'] = p.summary()
    return version


def info(conn, version_id: int) -> dict:
    row = conn.execute('SELECT based_on_id, task, classes, rule, counts, formats FROM trainset_versions '
                       'WHERE version_id = %s', (version_id,)).fetchone()
    if row is None:
        raise DatasetError(404, '这个版本不是训练集快照')
    sources = [datasets.get_version_by_id(conn, r['source_id'])['ref'] for r in conn.execute(
        'SELECT source_id FROM trainset_sources WHERE version_id = %s ORDER BY position', (version_id,))]
    based_on = datasets.get_version_by_id(conn, row['based_on_id'])['ref'] if row['based_on_id'] else None
    return {'trainset': {'task': row['task'], 'classes': row['classes'], 'rule': row['rule'],
                         'counts': row['counts'], 'formats': row['formats'], 'sources': sources,
                         'based_on': based_on}}


def get(conn, name: str, number: str) -> dict:
    version = datasets.get_version(conn, name, number)
    if version['kind'] != 'trainset':
        raise DatasetError(400, f'{version["ref"]} 不是训练集快照')
    version.update(info(conn, version['id']))
    return version


def list_trainsets(conn) -> List[dict]:
    return [d for d in datasets.list_datasets(conn) if d['kind'] == 'trainset']
