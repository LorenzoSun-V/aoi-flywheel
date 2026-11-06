"""Reading labels and image sizes for training-set snapshots, and writing YOLO and COCO (no database)."""
import io
import json
import struct
from collections import Counter
from decimal import Decimal

import pytest

from fwserver import formats

CLASSES = ['scratch', 'dent']


def size(data: bytes):
    return formats.image_size(io.BytesIO(data))


def jpeg(w, h, app1=b''):
    """SOI, an APP0 and optionally a big APP1 (EXIF) segment, then the frame header."""
    out = b'\xff\xd8' + b'\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
    if app1:
        out += b'\xff\xe1' + struct.pack('>H', len(app1) + 2) + app1
    return out + b'\xff\xc2' + struct.pack('>HBHHB', 11, 8, h, w, 1) + b'\x01\x11\x00' + b'\xff\xda\x00\x08rest'


def tiff(w, h, order='<'):
    entries = [(256, 3, 1, struct.pack(order + 'H', w) + b'\0\0'), (257, 4, 1, struct.pack(order + 'I', h))]
    ifd = struct.pack(order + 'H', len(entries)) + b''.join(
        struct.pack(order + 'HHI', t, k, n) + v for t, k, n, v in entries) + b'\0\0\0\0'
    return (b'II*\x00' if order == '<' else b'MM\x00*') + struct.pack(order + 'I', 8) + ifd


@pytest.mark.parametrize('data, expected', [
    (b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR' + struct.pack('>II', 768, 512) + b'\x08\x00\x00\x00\x00', (768, 512)),
    (jpeg(2048, 1536), (2048, 1536)),
    (jpeg(640, 480, app1=b'Exif\x00\x00' + bytes(60000)), (640, 480)),
    (b'BM' + bytes(12) + struct.pack('<Iii', 40, 1280, -1024), (1280, 1024)),  # top-down BMP
    (b'BM' + bytes(12) + struct.pack('<IHH', 12, 300, 200), (300, 200)),
    (b'GIF89a' + struct.pack('<HH', 32, 16) + bytes(8), (32, 16)),
    (tiff(4096, 3000), (4096, 3000)),
    (tiff(100, 70, '>'), (100, 70)),
    (b'RIFF\0\0\0\0WEBPVP8X' + bytes(8) + (799).to_bytes(3, 'little') + (599).to_bytes(3, 'little') + bytes(4),
     (800, 600)),
    (b'not an image at all, just bytes', None),
    (b'\xff\xd8\xff\xe0\x00\x10JFIF', None),  # cut off before the frame header
    (b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR' + struct.pack('>II', 0, 5), None),
])
def test_image_size_from_header(data, expected):
    assert size(data) == expected


def test_image_sizes_match_pillow(tmp_path):
    PIL = pytest.importorskip('PIL.Image')
    for fmt, ext in (('PNG', 'png'), ('JPEG', 'jpg'), ('BMP', 'bmp'), ('GIF', 'gif'), ('TIFF', 'tif'),
                     ('WEBP', 'webp')):
        for w, h in ((7, 5), (640, 480), (1, 1)):
            path = tmp_path / f'{w}x{h}.{ext}'
            PIL.new('L' if fmt != 'WEBP' else 'RGB', (w, h)).save(path, fmt)
            with open(path, 'rb') as f:
                assert formats.image_size(f) == (w, h), (fmt, w, h)


def test_json_boxes():
    doc = {'imageWidth': 100, 'imageHeight': 50, 'shapes': [
        {'label': 'dent', 'shape_type': 'rectangle', 'points': [[10, 5], [30, 25]]},  # labelme: two corners
        {'label': 'scratch', 'shape_type': None, 'points': [[0, 0], [4, 1], [2, 8]]},  # null: a polygon
        {'label': 'dent', 'shape_type': 'circle', 'points': [[5, 5], [6, 6]]},
    ]}
    label = formats.parse_json(json.dumps(doc))
    assert label.size == (100, 50) and label.names == ['dent', 'scratch', 'dent']
    assert [(b.x1, b.y1, b.x2, b.y2) for b in label.boxes] == [(10, 5, 30, 25), (0, 0, 4, 8)]
    assert label.refused == {'circle': 1} and label.shapes == {'polygon': 1}
    assert formats.yolo_bytes(label, CLASSES, (100, 50)) == (b'1 0.200000 0.300000 0.200000 0.400000\n'
                                                            b'0 0.020000 0.080000 0.040000 0.160000\n')
    for bad, message in (('[]', 'shapes'), ('{"shapes": [{"label": "a", "points": [[1, "x"], [2, 2]]}]}', '不是数'),
                         ('{"shapes": [{"label": "a", "points": [[1, 2]]}]}', '点数'),
                         ('{"shapes": [{"label": "a", "points": [[1, true], [2, 2]]}]}', '不是数'),
                         ('{"shapes": [1]}', 'JSON 对象'), ('{"shapes": [{"points": []}]}', '没有类别名')):
        assert message in formats.parse_json(bad)
    no_size = formats.parse_json('{"imageWidth": 0, "imageHeight": 5, "shapes": []}')
    assert no_size.size is None and no_size.names == []


def test_clip():
    label = formats.parse_json(json.dumps({'shapes': [
        {'label': 'a', 'shape_type': 'rectangle', 'points': [[-5, -5], [10, 10]]},
        {'label': 'a', 'shape_type': 'rectangle', 'points': [[20, 20], [30, 30]]},
        {'label': 'a', 'shape_type': 'rectangle', 'points': [[100, 0], [120, 10]]},  # on the right edge: outside
    ]}))
    formats.clip(label, (100, 50))
    assert [(b.x1, b.y1, b.x2, b.y2) for b in label.boxes] == [(0, 0, 10, 10), (20, 20, 30, 30)]
    assert label.clipped == 1 and label.outside == 1


def test_txt_labels():
    label = formats.parse_txt('0 0.5 0.5 0.2 0.4\n\n1  0.1\t0.1 0.1 0.1\n', CLASSES)
    assert label.names == ['scratch', 'dent'] and label.yolo_rows[1] == (1, 0.1, 0.1, 0.1, 0.1)
    # a txt keeps its coordinates as written (one space between them, LF): rewriting one never moves a box
    assert formats.yolo_bytes(label, CLASSES, None) == b'0 0.5 0.5 0.2 0.4\n1 0.1 0.1 0.1 0.1\n'
    assert formats.pixel_boxes(label, CLASSES, (200, 100))[0] == (0, 80.0, 30.0, 40.0, 40.0)
    for bad, message in (('2 0.5 0.5 0.1 0.1', '超出类别表'), ('a 0.5 0.5 0.1 0.1', '不是整数'),
                         ('0 0.5 0.5 0.1 0.1 0.3 0.3', '5 个数'), ('0 0.5 0.5 0.1 nan', '0 到 1'),
                         ('0 0.5 0.5 0.1 x', '不是数')):
        assert message in formats.parse_txt(bad, CLASSES)


def test_coco_document():
    label = formats.parse_json(json.dumps({'shapes': [
        {'label': 'dent', 'shape_type': 'rectangle', 'points': [[1.25, 2], [3.5, 4]]}]}))
    data = formats.coco_bytes([('SES/TX/a.png', (10, 8), formats.pixel_boxes(label, CLASSES, (10, 8))),
                               ('SES/TX/neg.png', (10, 8), [])], CLASSES)
    doc = json.loads(data)
    assert doc['images'] == [{'id': 1, 'file_name': 'SES/TX/a.png', 'width': 10, 'height': 8},
                             {'id': 2, 'file_name': 'SES/TX/neg.png', 'width': 10, 'height': 8}]
    assert doc['annotations'] == [{'id': 1, 'image_id': 1, 'category_id': 1, 'bbox': [1.25, 2.0, 2.25, 2.0],
                                   'area': 4.5, 'iscrowd': 0, 'segmentation': []}]
    assert [c['id'] for c in doc['categories']] == [0, 1]


def test_remap_json_and_txt():
    assert formats.remap_json('{not json', {'a': ''}) == (None, Counter())  # parse_json says why
    assert formats.remap_json('{"shapes": [1, {"label": 2}]}', {'a': ''}) == (None, Counter())
    text = json.dumps({'shapes': [{'label': 'a', 'x': 1}, {'label': 'b'}, 'odd'], 'k': 'v'})
    new, moved = formats.remap_json(text, {'a': 'b', 'b': 'a'})  # one step: a swap stays a swap
    assert json.loads(new) == {'shapes': [{'label': 'b', 'x': 1}, {'label': 'a'}, 'odd'], 'k': 'v'}
    assert moved == Counter({('a', 'b'): 1, ('b', 'a'): 1}) and new.endswith('}\n')
    label = formats.parse_txt('0 0.5 0.5 0.1 0.1\n1 0.5 0.5 0.2 0.2\n2 0.5 0.5 0.3 0.3\n', ['x', 'y', 'z'])
    assert formats.remap_txt(label, {'x': '', 'z': 'y'}, ['y'])
    assert label.names == ['y', 'y'] and [r[0] for r in label.yolo_rows] == [0, 0]
    assert label.moved == Counter({('x', ''): 1, ('z', 'y'): 1})
    same = formats.parse_txt('1 0.5 0.5 0.1 0.1\n', ['x', 'y'])
    assert not formats.remap_txt(same, {}, ['x', 'y'])  # nothing changed: the file is frozen as it is
    other = formats.parse_txt('0 0.5 0.5 0.1 0.1\n', ['y', 'x'])  # its own table: renumbered
    assert formats.remap_txt(other, {}, ['x', 'y']) and other.yolo_rows[0][0] == 1


def test_remapping_keeps_numbers_and_coordinates_as_written():
    # review of #10: rewriting must not move a box or change any number in the file
    text = ('{"calibration": 0.1234567890123456789, "big": 12345678901234567890123, "e": 1E400, "shapes": '
            '[{"label": "A", "points": [[1e1, 20.000000000000000001], [50.5, 60]], "shape_type": "rectangle"}]}')
    new, _ = formats.remap_json(text, {'A': 'B'})
    doc = json.loads(new, parse_float=Decimal)
    assert doc['calibration'] == Decimal('0.1234567890123456789') and doc['big'] == 12345678901234567890123
    assert '1E400' in new and '[\n          1e1,\n          20.000000000000000001\n        ]' in new
    assert doc['shapes'][0]['label'] == 'B' and formats.remap_json(new, {'B': 'A'})[0] is not None
    label = formats.parse_txt('1 0.5 0.5 0.0000004 0.2\n', ['B', 'A'])
    assert formats.remap_txt(label, {}, ['A', 'B'])
    assert formats.yolo_bytes(label, ['A', 'B'], None) == b'0 0.5 0.5 0.0000004 0.2\n'
    assert formats.pixel_boxes(label, ["A", "B"], (200, 100))[0][3] > 0  # COCO keeps the same width
