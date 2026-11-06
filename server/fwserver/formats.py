"""Label formats a training-set snapshot is frozen in: reading LiteAnno / X-AnyLabeling JSON and YOLO txt, and
writing YOLO detection labels and COCO annotations. Pure functions, no database and no store.

The rules are in docs/trainset-format.md. What a snapshot holds is computed here once, when it is made, and
frozen; changing this code never changes a snapshot that exists.
"""
from __future__ import annotations

import json
import math
import struct
from collections import Counter
from dataclasses import dataclass, field
from typing import BinaryIO, Dict, List, Optional, Tuple

TASK = 'det'  # the only task so far: axis-aligned boxes
BOX_SHAPES = frozenset({'rectangle', 'polygon', 'rotation'})  # their bounding box is the detection box
Size = Tuple[int, int]  # (width, height) in pixels


@dataclass
class Box:
    name: str
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class Label:
    kind: str  # 'json' or 'txt'
    names: List[str]  # the class name of every object, in file order
    boxes: List[Box] = field(default_factory=list)  # json: pixel boxes clipped to the image
    yolo_rows: List[Tuple[int, float, float, float, float]] = field(default_factory=list)  # txt: as written
    size: Optional[Size] = None  # json: imageWidth, imageHeight
    clipped: int = 0  # boxes that reached outside the image and were cut back to it
    outside: int = 0  # boxes with nothing left inside the image
    shapes: Counter = field(default_factory=Counter)  # json: shape types used as boxes, other than rectangle
    refused: Counter = field(default_factory=Counter)  # json: shape types that cannot be a detection box
    data: Optional[bytes] = None  # the label as the snapshot holds it, when that is not the file itself: a txt
    # without the UTF-8 BOM it was written with, or a label whose classes a class map changed (None: the file)
    moved: Counter = field(default_factory=Counter)  # shapes a class map renamed or dropped: (old, new or '') -> n


def _number(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _size(doc: dict) -> Optional[Size]:
    w, h = doc.get('imageWidth'), doc.get('imageHeight')
    if isinstance(w, bool) or isinstance(h, bool) or not isinstance(w, int) or not isinstance(h, int):
        return None
    return (w, h) if w > 0 and h > 0 else None


def parse_json(text: str):
    """A LiteAnno / X-AnyLabeling label, or the reason it cannot be used (a str). Boxes are clipped only once
    the image size is known (see clip)."""
    try:
        doc = json.loads(text)
    except ValueError as e:
        return f'不是合法的 JSON（{e}）'
    shapes = doc.get('shapes') if isinstance(doc, dict) else None
    if not isinstance(shapes, list):
        return '不是 LiteAnno / X-AnyLabeling 标注（没有 shapes 列表）'
    label = Label('json', [], size=_size(doc))
    for shape in shapes:
        if not isinstance(shape, dict):
            return '有一个形状不是 JSON 对象'
        name = shape.get('label')
        if not isinstance(name, str) or not name:
            return '有形状没有类别名'
        label.names.append(name)
        kind = shape.get('shape_type') or 'polygon'  # a missing shape_type means polygon (LiteAnno, labelme)
        if not isinstance(kind, str) or kind not in BOX_SHAPES:
            label.refused[str(kind)] += 1
            continue
        points = shape.get('points')
        if not isinstance(points, list) or len(points) < 2:
            return f'有 {kind} 形状的点数不对'
        xs, ys = [], []
        for p in points:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                return f'有 {kind} 形状的点不是 [x, y]'
            x, y = _number(p[0]), _number(p[1])
            if x is None or y is None:
                return f'有 {kind} 形状的坐标不是数'
            xs.append(x)
            ys.append(y)
        if kind != 'rectangle':
            label.shapes[kind] += 1
        label.boxes.append(Box(name, min(xs), min(ys), max(xs), max(ys)))
    return label


def clip(label: Label, size: Size) -> None:
    """Cut every box back to the image; a box with no area left inside it counts as outside and is dropped
    only from the list of boxes (the caller refuses the label)."""
    w, h = size
    kept = []
    for b in label.boxes:
        x1, y1, x2, y2 = max(b.x1, 0.0), max(b.y1, 0.0), min(b.x2, float(w)), min(b.y2, float(h))
        if x2 <= x1 or y2 <= y1:
            label.outside += 1
            continue
        if (x1, y1, x2, y2) != (b.x1, b.y1, b.x2, b.y2):
            label.clipped += 1
        kept.append(Box(b.name, x1, y1, x2, y2))
    label.boxes = kept


def parse_txt(text: str, classes: List[str]):
    """A YOLO detection label (class cx cy w h, normalised to 0..1), or the reason it cannot be used."""
    label = Label('txt', [])
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
        if len(fields) != 5:
            return f'第 {n} 行应为「类别 中心x 中心y 宽 高」5 个数，有 {len(fields)} 个（分割、旋转框、关键点的标注不能当检测框）'
        try:
            values = [float(v) for v in fields[1:]]
        except ValueError:
            return f'第 {n} 行有不是数的坐标'
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in values):
            return f'第 {n} 行的坐标不在 0 到 1 之间'
        label.names.append(classes[cid])
        label.yolo_rows.append((cid, *values))
    return label


def remap_json(text: str, class_map: Dict[str, str]) -> Tuple[Optional[str], Counter]:
    """A LiteAnno / X-AnyLabeling label with its class names changed by class_map (old -> new; an empty new name
    drops the shape, which makes it background): the new text, or None when no shape is in the map, and how
    many shapes each (old, new) moved. Everything else in the file stays as it is; what is not a label is left
    for parse_json to refuse."""
    moved: Counter = Counter()
    if not class_map:
        return None, moved
    try:
        doc = json.loads(text)
    except ValueError:
        return None, moved
    shapes = doc.get('shapes') if isinstance(doc, dict) else None
    if not isinstance(shapes, list):
        return None, moved
    kept = []
    for shape in shapes:
        name = shape.get('label') if isinstance(shape, dict) else None
        if isinstance(name, str) and name in class_map:
            new = class_map[name]
            moved[(name, new)] += 1
            if not new:
                continue
            shape = dict(shape, label=new)  # the same keys in the same order
        kept.append(shape)
    if not moved:
        return None, moved
    doc['shapes'] = kept
    return json.dumps(doc, ensure_ascii=False, indent=2) + '\n', moved


def remap_txt(label: Label, class_map: Dict[str, str], classes: List[str]) -> bool:
    """In place: rename the boxes of a YOLO label by class_map (an empty new name drops the box) and number
    every row by classes, the snapshot's class table (a name not in it gets -1; the caller refuses those
    labels). True when a row was dropped or got another class id, i.e. the file has to be written again."""
    index = {n: i for i, n in enumerate(classes)}
    names, rows, changed = [], [], False
    for name, (cid, *box) in zip(label.names, label.yolo_rows):
        new = class_map.get(name, name)
        if new != name:
            label.moved[(name, new)] += 1
        if not new:
            changed = True
            continue
        nid = index.get(new, -1)
        changed = changed or nid != cid
        names.append(new)
        rows.append((nid, *box))
    label.names, label.yolo_rows = names, rows
    return changed


def _f(v: float) -> str:
    return f'{v:.6f}'


def yolo_bytes(label: Label, classes: List[str], size: Size) -> bytes:
    """YOLO detection label: one line per box, 'class cx cy w h' normalised to the image, 6 decimals."""
    if label.kind == 'txt':
        rows = label.yolo_rows
    else:
        w, h = size
        index = {n: i for i, n in enumerate(classes)}
        rows = [(index[b.name], (b.x1 + b.x2) / 2 / w, (b.y1 + b.y2) / 2 / h, (b.x2 - b.x1) / w, (b.y2 - b.y1) / h)
                for b in label.boxes]
    return ''.join(f'{c} {_f(cx)} {_f(cy)} {_f(bw)} {_f(bh)}\n' for c, cx, cy, bw, bh in rows).encode('ascii')


def pixel_boxes(label: Label, classes: List[str], size: Size) -> List[Tuple[int, float, float, float, float]]:
    """(class id, x, y, width, height) in pixels, for COCO."""
    w, h = size
    if label.kind == 'txt':
        out = []
        for c, cx, cy, bw, bh in label.yolo_rows:
            x1, y1 = max((cx - bw / 2) * w, 0.0), max((cy - bh / 2) * h, 0.0)
            x2, y2 = min((cx + bw / 2) * w, float(w)), min((cy + bh / 2) * h, float(h))
            out.append((c, x1, y1, x2 - x1, y2 - y1))
        return out
    index = {n: i for i, n in enumerate(classes)}
    return [(index[b.name], b.x1, b.y1, b.x2 - b.x1, b.y2 - b.y1) for b in label.boxes]


def liteanno_bytes(label: Label, classes: List[str], size: Size, image_name: str) -> bytes:
    """A LiteAnno / X-AnyLabeling JSON for a YOLO txt label, so that the snapshot can be opened in LiteAnno:
    one rectangle per box, in pixels (clipped to the image like the COCO boxes)."""
    w, h = size
    shapes = []
    for c, x, y, bw, bh in pixel_boxes(label, classes, size):
        x1, y1, x2, y2 = (round(v, 4) for v in (x, y, x + bw, y + bh))
        shapes.append({'label': classes[c], 'points': [[x1, y1], [x2, y1], [x2, y2], [x1, y2]], 'group_id': None,
                       'description': '', 'shape_type': 'rectangle', 'flags': {}})
    doc = {'version': '3.1', 'flags': {}, 'shapes': shapes, 'imagePath': image_name, 'imageData': None,
           'imageHeight': h, 'imageWidth': w}
    return (json.dumps(doc, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def coco_bytes(images: List[Tuple[str, Size, list]], classes: List[str]) -> bytes:
    """COCO detection annotations for one split. images: (file_name, size, pixel boxes), file_name relative to
    the folder that holds the split's images. Category ids are the class ids of classes.txt (from 0)."""
    doc = {'images': [], 'annotations': [],
           'categories': [{'id': i, 'name': n, 'supercategory': 'none'} for i, n in enumerate(classes)]}
    ann = 0
    for image_id, (name, (w, h), boxes) in enumerate(images, start=1):
        doc['images'].append({'id': image_id, 'file_name': name, 'width': w, 'height': h})
        for c, x, y, bw, bh in boxes:
            ann += 1
            box = [round(x, 4), round(y, 4), round(bw, 4), round(bh, 4)]
            doc['annotations'].append({'id': ann, 'image_id': image_id, 'category_id': c, 'bbox': box,
                                       'area': round(box[2] * box[3], 4), 'iscrowd': 0, 'segmentation': []})
    return (json.dumps(doc, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


# --- image sizes from the file header (no decoding, no extra libraries) ------------------------------------
def image_size(f: BinaryIO) -> Optional[Size]:
    """(width, height) as stored in a PNG, JPEG, BMP, GIF, TIFF or WebP header; None if unknown. EXIF
    orientation is not applied (images from industrial cameras do not carry it)."""
    head = f.read(32)
    try:
        if head.startswith(b'\x89PNG\r\n\x1a\n') and head[12:16] == b'IHDR':
            return _positive(*struct.unpack('>II', head[16:24]))
        if head.startswith(b'\xff\xd8'):
            return _jpeg(f)
        if head.startswith(b'BM'):
            header = struct.unpack('<I', head[14:18])[0]
            if header == 12:
                return _positive(*struct.unpack('<HH', head[18:22]))
            w, h = struct.unpack('<ii', head[18:26])
            return _positive(w, abs(h))
        if head[:6] in (b'GIF87a', b'GIF89a'):
            return _positive(*struct.unpack('<HH', head[6:10]))
        if head[:4] in (b'II*\x00', b'MM\x00*'):
            return _tiff(f, '<' if head[:2] == b'II' else '>')
        if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
            return _webp(head + f.read(32))
    except struct.error:
        return None
    return None


def _positive(w: int, h: int) -> Optional[Size]:
    return (w, h) if w > 0 and h > 0 else None


_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _jpeg(f: BinaryIO) -> Optional[Size]:
    f.seek(2)
    while True:
        b = f.read(1)
        while b and b != b'\xff':
            b = f.read(1)
        while b == b'\xff':
            b = f.read(1)
        if not b:
            return None
        marker = b[0]
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            continue  # markers without a length
        if marker in (0xD9, 0xDA):
            return None  # end of image, or the image data started before any frame header
        length = struct.unpack('>H', f.read(2))[0]
        if marker in _SOF:
            h, w = struct.unpack('>xHH', f.read(5))
            return _positive(w, h)
        f.seek(length - 2, 1)


def _tiff(f: BinaryIO, order: str) -> Optional[Size]:
    f.seek(4)
    f.seek(struct.unpack(order + 'I', f.read(4))[0])
    count = struct.unpack(order + 'H', f.read(2))[0]
    found: Dict[int, int] = {}
    for _ in range(count):
        tag, kind, n, value = struct.unpack(order + 'HHI4s', f.read(12))
        if tag in (256, 257) and n == 1 and kind in (3, 4):
            found[tag] = struct.unpack(order + ('H' if kind == 3 else 'I'), value[:2 if kind == 3 else 4])[0]
    if 256 in found and 257 in found:
        return _positive(found[256], found[257])
    return None


def _webp(head: bytes) -> Optional[Size]:
    chunk = head[12:16]
    if chunk == b'VP8X':
        w = int.from_bytes(head[24:27], 'little') + 1
        h = int.from_bytes(head[27:30], 'little') + 1
        return _positive(w, h)
    if chunk == b'VP8L' and head[20] == 0x2F:
        bits = int.from_bytes(head[21:25], 'little')
        return _positive((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b'VP8 ' and head[23:26] == b'\x9d\x01\x2a':
        w, h = struct.unpack('<HH', head[26:30])
        return _positive(w & 0x3FFF, h & 0x3FFF)
    return None
