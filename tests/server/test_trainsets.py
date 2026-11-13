"""Training-set snapshots: the split, continuing a snapshot, the checks on labels and classes, and what the
database keeps frozen."""
import json
import struct
import uuid

import psycopg
import pytest

from helpers import register_place, quiet, read_tree, write
from fw import cli as fw_cli
from fw import ops
from fw.api import ApiError
from fwserver import trainsets
from fwserver.trainsets import Options, Sample

CLASSES = ['scratch', 'dent']
W, H = 200, 100


def label(*names, size=(W, H), shapes=None) -> bytes:
    """A LiteAnno label: one rectangle per name (10,20)-(50,60), unless shapes are given."""
    doc = {'version': '3.1', 'flags': {}, 'imagePath': 'x.png', 'imageData': None,
           'shapes': shapes if shapes is not None else
           [{'label': n, 'points': [[10, 20], [50, 20], [50, 60], [10, 60]], 'shape_type': 'rectangle'}
            for n in names]}
    if size:
        doc['imageWidth'], doc['imageHeight'] = size
    return json.dumps(doc).encode()


def png(tag: str, w=W, h=H) -> bytes:
    """Enough of a PNG for its size to be read from the header; tag makes the content unique."""
    return b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR' + struct.pack('>II', w, h) + b'\x08\x02\x00\x00\x00' + tag.encode()


def status_of(call):
    try:
        call()
    except ApiError as e:
        return e.status
    return 200


def pool(api, tmp_path, files: dict, site='SES', line='TX', name=None) -> str:
    """A pool dataset with one version holding files ({path: bytes}); returns its name."""
    name = name or f'P-{uuid.uuid4().hex[:8]}'
    register_place(site, line)
    api.create_dataset(name, site, line, 'pool' if site else 'other', '')  # a pool always has a line
    add_version(api, tmp_path, name, files)
    return name


def add_version(api, tmp_path, name, files: dict) -> dict:
    src = tmp_path / f'src-{uuid.uuid4().hex[:6]}'
    for rel, data in files.items():
        write(src, rel, data)
    return ops.import_folder(api, name, src, force=True, echo=quiet)  # force: the pool may hold same-stem images


def sample_files(prefix: str, n: int, names=('scratch',), negatives=0) -> dict:
    """n images under prefix/, each with a JSON label naming `names`, plus `negatives` images without one."""
    files = {}
    for i in range(n):
        files[f'{prefix}/img{i:03}.png'] = png(f'{prefix}-{i}-{uuid.uuid4().hex}')
        files[f'{prefix}/img{i:03}.json'] = label(*names)
    for i in range(negatives):
        files[f'{prefix}/neg{i:03}.png'] = png(f'{prefix}-neg-{i}-{uuid.uuid4().hex}')
    return files


def make(api, name, sources, classes=CLASSES, **kw):
    return ops.create_trainset(api, name, sources, classes, echo=quiet, **kw)


def splits_of(tree: dict) -> dict:
    """{key: split} of every image in a snapshot fetched as it is stored (fetch's default, --format all)."""
    out = {}
    for path in tree:
        if path.startswith('images/'):
            split, _, key = path[len('images/'):].partition('/')
            out[key] = split
    return out


def fetch(api, cache, ref, dest, fmt='all') -> dict:
    """The snapshot as stored (paths keep <site>/<line>/<path in pool>); fmt=None is fw get's default, the flat
    yolo layout."""
    ops.get(api, cache, ref, dest, echo=quiet, fmt=fmt)
    return read_tree(dest)


def yolo_line(cid, x1=10, y1=20, x2=50, y2=60, w=W, h=H) -> str:
    return f'{cid} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} {(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}\n'


# --- the split itself (no database) -------------------------------------------------------------------
def test_cut_is_fixed_by_keys_ratio_and_seed():
    keys = [f'SES/TX/{i}.png' for i in range(1000)]
    a = trainsets._cut(keys, 0.2, 0)
    assert a == trainsets._cut(list(reversed(keys)), 0.2, 0)  # input order does not matter
    assert sum(1 for s in a.values() if s == 'val') == 200
    b = trainsets._cut(keys, 0.2, 1)
    assert a != b and sum(1 for s in b.values() if s == 'val') == 200
    assert set(trainsets._cut(keys, 0.0, 0).values()) == {'train'}
    # adding samples does not move the ones that were there: only their rank among each other matters
    more = trainsets._cut(keys + [f'SES/TX/new{i}.png' for i in range(10)], 0.2, 0)
    assert sum(1 for k in keys if more[k] != a[k]) <= 10


def samples(*keys):
    return {k: Sample(k, 'SES', 'TX', (k.split('/', 2)[2], '0' * 64, 1), None) for k in keys}


def test_assign_continue_merge_and_new_to():
    keys = [f'SES/TX/a/{i}.png' for i in range(10)] + [f'SES/TX/b/{i}.png' for i in range(5)]
    s = samples(*keys)
    rule, problems = trainsets._assign(s, Options(['x'], CLASSES, test_dirs=['b']), None)
    assert problems == [] and rule['mode'] == 'fresh'
    assert all(s[k].split == 'test' for k in keys[10:])
    assert {s[k].split for k in keys[:10]} == {'train', 'val'}
    previous = {k: (v.split, None) for k, v in s.items()}

    s2 = samples(*keys, 'SES/TX/c/0.png', 'SES/TX/c/1.png')
    trainsets._assign(s2, Options(['x'], CLASSES), previous)
    assert all(s2[k].split == previous[k][0] for k in keys)
    assert s2['SES/TX/c/0.png'].split == s2['SES/TX/c/1.png'].split == 'test'

    s3 = samples(*keys, 'SES/TX/c/0.png')
    trainsets._assign(s3, Options(['x'], CLASSES, new_to='trainval'), previous)
    assert s3['SES/TX/c/0.png'].split in ('train', 'val')

    s4 = samples(*keys)
    rule, _ = trainsets._assign(s4, Options(['x'], CLASSES, merge_test=True), previous)
    assert rule['merge_test'] and all(s4[k].split in ('train', 'val') for k in keys)
    assert all(s4[k].split == previous[k][0] for k in keys[:10])  # old train/val samples stay where they were

    _, problems = trainsets._assign(samples(*keys), Options(['x'], CLASSES, test_dirs=['b', 'nope']), None)
    assert any('nope' in p for p in problems)


# --- over HTTP ------------------------------------------------------------------------------------------
def test_first_snapshot_layout_and_formats(api, cache, tmp_path):
    files = {**sample_files('2026-09', 20, ('scratch', 'dent'), negatives=3), **sample_files('2026-10', 5)}
    files['2026-09/extra.txt'] = b'0 0.5 0.5 0.1 0.1\n1 0.2 0.2 0.1 0.1\n'  # a YOLO label from the pool
    files['2026-09/extra.bmp'] = b'BM' + bytes(12) + struct.pack('<Iii', 40, W, -H) + b'yolo image'
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    v = make(api, name, [f'{src}@1'], val=0.25, test_dirs=['2026-10'], note='first')
    assert v['ref'] == f'{name}@1' and v['kind'] == 'trainset'
    ts = v['trainset']
    assert ts['classes'] == CLASSES and ts['sources'] == [f'{src}@1'] and ts['based_on'] is None
    assert ts['task'] == 'det' and ts['formats'] == ['yolo', 'coco', 'json']  # JSON made for the YOLO txt too
    assert v['summary']['missing_formats'] == {}
    counts = ts['counts']
    assert counts['test']['samples'] == 5 and counts['test']['boxes'] == {'scratch': 5, 'dent': 0}
    assert counts['train']['samples'] + counts['val']['samples'] == 24
    assert counts['val']['samples'] == 6  # round(24 * 0.25)
    assert counts['train']['negatives'] + counts['val']['negatives'] == 3
    assert sum(counts[s]['boxes']['dent'] for s in ('train', 'val')) == 21

    tree = fetch(api, cache, v['ref'], tmp_path / 'yolo', fmt='yolo-tree')
    assert tree['classes.txt'] == b'scratch\ndent\n'
    info = json.loads(tree['trainset.json'])
    assert info['format'] == trainsets.FORMAT and info['counts'] == counts and info['classes'] == CLASSES
    assert info['task'] == 'det' and info['formats'] == ['yolo', 'coco', 'json'] and info['missing_formats'] == {}
    assert info['sources'][0]['ref'] == f'{src}@1'
    assert info['rule'] == {'mode': 'fresh', 'val': 0.25, 'test': 0.0, 'seed': 0, 'stratify': 'boxes',
                            'test_dirs': ['2026-10'], 'merge_test': False, 'paths': 'short'}
    splits = splits_of(tree)
    assert len(splits) == 29 and all(s == 'test' for k, s in splits.items() if k.startswith('2026-10/'))
    for path, data in files.items():
        key = path  # fw get drops <site>/<line>/ (short paths)
        stem = key.rsplit('.', 1)[0]
        if path.endswith(('.png', '.bmp')):
            assert tree[f'images/{splits[key]}/{key}'] == data
        elif path.endswith('.json'):
            split = splits[stem + '.png']
            names = json.loads(data)['shapes']
            assert tree[f'labels/{split}/{stem}.txt'].decode() == ''.join(
                yolo_line(CLASSES.index(n['label'])) for n in names)
        else:
            assert tree[f'labels/{splits[stem + ".bmp"]}/{stem}.txt'] == data  # YOLO from the pool: as it is
    assert not any(p.startswith(f'labels/{s}/2026-09/neg') for p in tree for s in ('train', 'val'))
    yaml = tree['data.yaml'].decode()
    assert f'path: "{(tmp_path / "yolo").resolve().as_posix()}"' in yaml
    assert 'train: images/train\nval: images/val\ntest: images/test\n' in yaml and '  1: "dent"' in yaml
    assert set(tree) == {'classes.txt', 'trainset.json', 'data.yaml'} | {p for p in tree if p.startswith(
        ('images/', 'labels/'))}

    coco = fetch(api, cache, v['ref'], tmp_path / 'coco', fmt='coco')
    assert {p.split('/')[0] for p in coco} == {'classes.txt', 'trainset.json', 'train', 'valid', 'test'}
    test = json.loads(coco['test/_annotations.coco.json'])
    assert test['categories'] == [{'id': 0, 'name': 'scratch', 'supercategory': 'none'},
                                  {'id': 1, 'name': 'dent', 'supercategory': 'none'}]
    assert len(test['images']) == 5 and all(coco['test/' + i['file_name']] for i in test['images'])
    assert test['images'][0] == {'id': 1, 'file_name': '2026-10/img000.png', 'width': W, 'height': H}
    assert test['annotations'][0] == {'id': 1, 'image_id': 1, 'category_id': 0, 'bbox': [10, 20, 40, 40],
                                      'area': 1600, 'iscrowd': 0, 'segmentation': []}
    trainval = [json.loads(coco[f'{d}/_annotations.coco.json']) for d in ('train', 'valid')]
    assert sum(len(d['images']) for d in trainval) == 24 and sum(len(d['annotations']) for d in trainval) == 42
    extra = [(d, i) for d in trainval for i in d['images'] if i['file_name'] == '2026-09/extra.bmp'][0]
    assert extra[1]['width'] == W and extra[1]['height'] == H  # size from the BMP header
    boxes = [a['bbox'] for a in extra[0]['annotations'] if a['image_id'] == extra[1]['id']]
    assert boxes == [[90.0, 45.0, 20.0, 10.0], [30.0, 15.0, 20.0, 10.0]]

    js = fetch(api, cache, v['ref'], tmp_path / 'json', fmt='json')
    made = json.loads(js[f'{splits["2026-09/extra.bmp"]}/2026-09/extra.json'])
    assert made['imagePath'] == 'extra.bmp' and (made['imageWidth'], made['imageHeight']) == (W, H)
    assert [(sh['label'], sh['shape_type'], sh['points'][0], sh['points'][2]) for sh in made['shapes']] == [
        ('scratch', 'rectangle', [90.0, 45.0], [110.0, 55.0]), ('dent', 'rectangle', [30.0, 15.0], [50.0, 25.0])]
    every = fetch(api, cache, v['ref'], tmp_path / 'all', fmt='all')
    assert {p.split('/')[0] for p in every} == {'classes.txt', 'trainset.json', 'images', 'labels', 'coco', 'json'}
    assert len([p for p in every if p.startswith('json/')]) == 26  # 25 from the pool, 1 made from the YOLO txt
    with pytest.raises(ops.FwError, match='不是训练集快照'):
        fetch(api, cache, f'{src}@1', tmp_path / 'pool', fmt='yolo')
    assert api.trainset(name)['trainset'] == ts
    assert any(d['name'] == name for d in api.trainsets())
    assert not any(d['name'] == src for d in api.trainsets())

    # the same inputs again would make the same snapshot: refused as unchanged
    preview = api.preview_trainset(name, {'sources': [f'{src}@1'], 'classes': CLASSES, 'val': 0.25,
                                          'test_dirs': ['2026-10'], 'fresh': True})
    assert any('完全一样' in p for p in preview['problems'])
    assert preview['counts'] == counts


def test_json_layout_opens_in_liteanno(api, cache, tmp_path):
    files = sample_files('a', 3, negatives=1)
    src = pool(api, tmp_path, files)
    v = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'], val=0)
    assert v['trainset']['formats'] == ['yolo', 'coco', 'json']
    tree = fetch(api, cache, v['ref'], tmp_path / 'json', fmt='json')
    assert tree == {'classes.txt': b'scratch\ndent\n', 'trainset.json': tree['trainset.json'],
                    **{f'train/{p}': d for p, d in files.items()}}
    ops.get(api, cache, v['ref'], tmp_path / 'sub', fmt='json', subpath='train/a', echo=quiet)
    assert read_tree(tmp_path / 'sub') == {k: d for k, d in tree.items() if k.startswith('train/')}


def test_shapes_sizes_and_clipping(api, cache, tmp_path):
    rot = {'label': 'dent', 'shape_type': 'rotation', 'points': [[100, 10], [120, 30], [100, 50], [80, 30]]}
    poly = {'label': 'scratch', 'points': [[0, 0], [20, 5], [5, 30]]}  # no shape_type: a polygon
    over = {'label': 'scratch', 'shape_type': 'rectangle', 'points': [[-10, 90], [30, 130]]}
    files = {'a.png': png('a'), 'a.json': label(shapes=[rot, poly, over]),
             'b.png': png('b', 400, 300), 'b.json': label('dent', size=None)}  # size from the PNG header
    src = pool(api, tmp_path, files)
    lines = []
    v = ops.create_trainset(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'], CLASSES, val=0, echo=lines.append)
    text = '\n'.join(lines)
    assert '1 个框超出图片边界' in text and 'polygon 1 个、rotation 1 个' in text
    tree = fetch(api, cache, v['ref'], tmp_path / 'y')
    assert tree['labels/train/SES/TX/a.txt'].decode() == (yolo_line(1, 80, 10, 120, 50) + yolo_line(0, 0, 0, 20, 30)
                                                         + yolo_line(0, 0, 90, 30, 100))
    assert tree['labels/train/SES/TX/b.txt'].decode() == yolo_line(1, w=400, h=300)


@pytest.mark.parametrize('files, message', [
    ({'a.png': png('1'), 'a.json': label(shapes=[{'label': 'scratch', 'shape_type': 'point', 'points': [[1, 1]]}])},
     '有 point 形状，不能当检测框'),
    ({'a.png': png('1'), 'a.json': label(shapes=[{'label': 'scratch', 'shape_type': 'rectangle',
                                                  'points': [[300, 10], [320, 20]]}])}, '完全在图片外面'),
    ({'a.png': b'not an image', 'a.json': label('scratch', size=None)}, '没有 imageWidth'),
    ({'a.png': png('1'), 'a.txt': b'0 0.5 0.5 0.1\n'}, '5 个数'),
    ({'a.png': png('1'), 'a.txt': b'0 0.5 0.5 0.1 1.2\n'}, '不在 0 到 1 之间'),
])
def test_labels_that_cannot_be_detection_boxes(api, tmp_path, files, message):
    src = pool(api, tmp_path, files)
    preview = api.preview_trainset(f'T-{uuid.uuid4().hex[:8]}', {'sources': [f'{src}@1'], 'classes': CLASSES})
    assert any(message in p for p in preview['problems']), preview['problems']


def test_json_from_yolo_needs_the_image_size(api, cache, tmp_path):
    src = pool(api, tmp_path, {'a.png': png(uuid.uuid4().hex), 'a.txt': yolo_line(0).encode(),
                               'b.png': b'raw bytes', 'b.txt': yolo_line(1).encode()})
    v = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'])
    assert v['trainset']['formats'] == ['yolo']
    assert 'SES/TX/b.png' in v['summary']['missing_formats']['json']
    # continuing it: the YOLO labels did not change, whatever was made from them
    files = {'a.png': png(uuid.uuid4().hex), 'a.txt': yolo_line(0).encode()}
    src2 = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    make(api, name, [f'{src2}@1'])
    add_version(api, tmp_path, src2, {**files, 'c.png': png(uuid.uuid4().hex)})
    v2 = make(api, name, [f'{src2}@2'])
    assert v2['summary']['changes']['relabeled'] == 0 and v2['summary']['changes']['added'] == 1


def test_coco_needs_every_image_size(api, cache, tmp_path):
    src = pool(api, tmp_path, {'a.png': png('a'), 'a.json': label('dent'), 'b.png': b'raw bytes'})
    v = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'])
    assert v['trainset']['formats'] == ['yolo', 'json']
    assert '读不出尺寸' in v['summary']['missing_formats']['coco']
    with pytest.raises(ops.FwError, match='没有 coco 格式'):
        fetch(api, cache, v['ref'], tmp_path / 'c', fmt='coco')


def test_continue_keeps_every_split(api, cache, tmp_path):
    files = sample_files('2026-09', 30)
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    v1 = make(api, name, [f'{src}@1'], val=0.2)
    t1 = splits_of(fetch(api, cache, v1['ref'], tmp_path / 'g1'))
    assert set(t1.values()) == {'train', 'val'}

    files2 = dict(files)
    files2.update(sample_files('2026-11', 4))
    del files2['2026-09/img000.png'], files2['2026-09/img000.json']
    files2['2026-09/img001.json'] = label('scratch', 'dent')
    add_version(api, tmp_path, src, files2)
    lines = []
    v2 = ops.create_trainset(api, name, [f'{src}@2'], CLASSES, echo=lines.append)
    assert v2['parent']['ref'] == v1['ref'] and v2['trainset']['based_on'] == v1['ref']
    assert v2['summary']['changes'] == {'added': 4, 'removed': 1, 'moved': 0, 'moved_examples': [], 'relabeled': 1}
    assert any('接着' in line for line in lines)
    t2 = splits_of(fetch(api, cache, v2['ref'], tmp_path / 'g2'))
    assert all(t2[k] == t1[k] for k in t2 if k in t1)
    assert [t2[k] for k in t2 if k.startswith('SES/TX/2026-11/')] == ['test'] * 4

    v3 = make(api, name, [f'{src}@2'], merge_test=True)
    t3 = splits_of(fetch(api, cache, v3['ref'], tmp_path / 'g3'))
    assert set(t3.values()) <= {'train', 'val'}
    assert v3['summary']['changes']['moved'] == 4
    assert all(t3[k] == t2[k] for k in t2 if t2[k] != 'test')
    d = api.diff(v2['id'], v3['id'])  # moving a sample between splits is a rename, nothing is uploaded again
    assert len(d['renamed']) == 4 * 3 and d['added'] == []  # image, YOLO label and JSON of each moved sample
    assert d['deleted'] == ['coco/test.json']  # test is empty now
    assert sorted(d['modified']) == ['coco/train.json', 'coco/val.json', 'trainset.json']

    v4 = make(api, name, [f'{src}@2'], fresh=True, seed=7)
    assert v4['trainset']['based_on'] is None and v4['parent']['ref'] == v3['ref']
    assert v4['trainset']['rule']['mode'] == 'fresh'


def test_split_file(api, cache, tmp_path):
    files = sample_files('a', 4)
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    keys = [f'SES/TX/a/img{i:03}.png' for i in range(4)]
    split_file = tmp_path / 'split.txt'
    split_file.write_text('# my split\n' + '\n'.join(f'{k}\t{s}' for k, s in zip(keys, ['train', 'val', 'test',
                                                                                        'train'])) + '\n',
                          encoding='utf-8')
    split = ops.read_split_file(split_file)
    with pytest.raises(ops.FwError, match='漏了'):
        make(api, name, [f'{src}@1'], split={k: split[k] for k in keys[:3]})
    with pytest.raises(ops.FwError, match='不在来源版本里'):
        make(api, name, [f'{src}@1'], split=dict(split, **{'SES/TX/b.png': 'val'}))
    with pytest.raises(ApiError, match='划分清单'):
        make(api, name, [f'{src}@1'], split=split, merge_test=True)
    v = make(api, name, [f'{src}@1'], split=split)
    assert splits_of(fetch(api, cache, v['ref'], tmp_path / 'g')) == split
    assert v['trainset']['rule'] == {'mode': 'split-file', 'split_count': 4, 'paths': 'short'}
    lines = []
    v2 = ops.create_trainset(api, name, [f'{src}@1'], CLASSES, split=dict(split, **{keys[3]: 'val'}),
                             echo=lines.append)
    assert v2['trainset']['based_on'] is None and v2['parent']['ref'] == v['ref']  # a split file continues nothing
    assert any(f'用划分清单（不接着 {v["ref"]}）' in line for line in lines)


@pytest.mark.parametrize('files, message', [
    ({'a.png': b'1', 'a.json': label('crack')}, "类别 'crack' 不在类别表里"),
    ({'a.png': b'1', 'a.json': label('scratch')[:-1] + b', "imageWidth": true}'}, '没有 imageWidth'),
    ({'a.png': b'1', 'a.txt': b'5 0.5 0.5 0.1 0.1\n'}, '超出类别表'),
    ({'a.png': b'1', 'a.txt': b'x 0.5 0.5 0.1 0.1\n'}, '不是整数'),
    ({'a.png': b'1', 'a.json': b'{not json'}, '不是合法的 JSON'),
    ({'a.png': b'1', 'a.json': b'{"shapes": [{"points": []}]}'}, '没有类别名'),
    ({'a.png': b'1', 'a.json': b'[]'}, '没有 shapes'),
    ({'a.png': b'1', 'a.json': '{"shapes": []}'.encode('utf-16')}, '不是 UTF-8'),
    ({'a.png': b'1', 'b.json': label('scratch')}, '没有对应的图片'),
    ({'a.png': b'1', 'a.bmp': b'2', 'a.json': label('scratch')}, '同名不同扩展名'),
    ({'a.png': b'1', 'a.json': label('scratch'), 'a.txt': b'0 0.5 0.5 0.1 0.1\n'}, '同时有 JSON 和 txt'),
])
def test_bad_labels_are_refused(api, tmp_path, files, message):
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    body = {'sources': [f'{src}@1'], 'classes': CLASSES}
    preview = api.preview_trainset(name, body)
    assert any(message in p for p in preview['problems']), preview['problems']
    with pytest.raises(ops.FwError, match='没有建训练集快照'):
        make(api, name, [f'{src}@1'])
    assert status_of(lambda: api.create_trainset(name, body)) == 400
    assert status_of(lambda: api.trainset(name)) == 404
    assert not any(d['name'] == name for d in api.datasets())


def test_bad_sources_and_names(api, tmp_path):
    ok = pool(api, tmp_path, {'a.png': b'1'})
    no_site = pool(api, tmp_path, {'a.png': b'1'}, site='', line='')
    name = f'T-{uuid.uuid4().hex[:8]}'
    assert make(api, name, [f'{ok}@1'])['number'] == 1
    for sources, message in (([f'{no_site}@1'], '没有填现场或产线'), ([f'{name}@1'], '训练集快照'),
                             ([f'{ok}@1', f'{ok}@1'], '写了两次'), ([f'{ok}@9'], None)):
        with pytest.raises(ApiError, match=message):
            make(api, f'T-{uuid.uuid4().hex[:8]}', sources)
    with pytest.raises(ApiError, match='共用名称') as e:
        make(api, ok, [f'{ok}@1'])
    assert e.value.status == 409
    same = pool(api, tmp_path, {'a.png': b'2'})  # also SES/TX: the same key twice
    with pytest.raises(ops.FwError, match='两个来源都有 SES/TX/a.png'):
        make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{ok}@1', f'{same}@1'])
    other = pool(api, tmp_path, {'a.png': b'3'}, site='SES', line='TY')  # fw get drops SES/TX/ and SES/TY/
    with pytest.raises(ops.FwError, match='去掉现场/产线.*images/train/a.png'):
        make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{ok}@1', f'{other}@1'], val=0)
    other = pool(api, tmp_path, {'b.png': b'3'}, site='SES', line='TY')
    v = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{ok}@1', f'{other}@1'], val=0)
    assert v['trainset']['counts']['train']['samples'] == 2
    assert status_of(lambda: make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{ok}@1'], classes=[])) == 422
    for classes, message in ((['a', 'a'], '重复'), ([' a'], '首尾空格'), (['a\tb'], '制表符')):
        with pytest.raises(ApiError, match=message):
            make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{ok}@1'], classes=classes)
    assert status_of(lambda: make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{ok}@1'], val=1.0)) == 400
    assert status_of(lambda: make(api, 'bad name', [f'{ok}@1'])) == 400


def test_trainsets_are_made_only_by_trainset_create(api, tmp_path):
    src = pool(api, tmp_path, {'a.png': b'1'})
    name = f'T-{uuid.uuid4().hex[:8]}'
    make(api, name, [f'{src}@1'])
    write(tmp_path / 'f', 'b.png', b'2')
    with pytest.raises(ApiError, match='只能用 fw trainset create') as e:
        ops.import_folder(api, name, tmp_path / 'f', echo=quiet)
    assert e.value.status == 400
    assert status_of(lambda: api.create_dataset(f'X-{uuid.uuid4().hex[:6]}', 'SES', 'TX', 'trainset', '')) in (400,
                                                                                                                 422)
    with pytest.raises(ApiError, match='不是训练集快照'):
        api.trainset(src)


def test_someone_else_made_one_meanwhile(api, tmp_path):
    src = pool(api, tmp_path, sample_files('a', 6))
    name = f'T-{uuid.uuid4().hex[:8]}'

    def other_person_first(_prompt):
        make(api, name, [f'{src}@1'], seed=1)
        return True

    with pytest.raises(ApiError, match='在这期间有了变化') as e:
        ops.create_trainset(api, name, [f'{src}@1'], CLASSES, confirm=other_person_first, echo=quiet)
    assert e.value.status == 409
    assert [v['number'] for v in api.versions(name)] == [1]


def test_failure_before_freeze_leaves_nothing(api, service, sql, tmp_path):
    src = pool(api, tmp_path, sample_files('a', 3))
    name = f'T-{uuid.uuid4().hex[:8]}'

    def boom():
        raise RuntimeError('injected failure before freezing')

    service.hooks['before_freeze'] = boom
    try:
        with pytest.raises(Exception):
            make(api, name, [f'{src}@1'])
    finally:
        service.hooks.pop('before_freeze')
    assert sql.execute('SELECT count(*) AS n FROM versions v JOIN datasets d ON d.id = v.dataset_id '
                       'WHERE d.name = %s', (name,)).fetchone()['n'] == 0
    assert not any(d['name'] == name for d in api.datasets())  # the name was not taken either
    assert not any(d['name'] == name for d in api.trainsets())
    assert make(api, name, [f'{src}@1'])['number'] == 1


def test_roles(make_api, tmp_path):
    algo = make_api('algo')
    src = pool(algo, tmp_path, {'a.png': b'1'})
    name = f'T-{uuid.uuid4().hex[:8]}'
    make(make_api('admin'), name, [f'{src}@1'])
    body = {'sources': [f'{src}@1'], 'classes': CLASSES}
    field = make_api('field')  # reads snapshots, makes none
    assert status_of(field.trainsets) == 200 and status_of(lambda: field.trainset(name)) == 200
    assert status_of(lambda: field.preview_trainset(name, body)) == 403
    assert status_of(lambda: field.create_trainset(name, body)) == 403
    assert status_of(make_api('worker').trainsets) == 403


# --- found in review (2026-11-05): each reproduced first ---------------------------------------------------
def test_what_was_previewed_is_what_is_frozen(api, tmp_path):
    """A bare name in --from is resolved at the preview; a pool version imported before the confirmation is
    not what gets frozen."""
    files = sample_files('a', 3)
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'

    def import_meanwhile(_prompt):
        add_version(api, tmp_path, src, {**files, **sample_files('b', 1)})
        return True

    v = ops.create_trainset(api, name, [src], CLASSES, confirm=import_meanwhile, echo=quiet)
    assert v['summary']['samples'] == 3 and v['trainset']['sources'] == [f'{src}@1']
    with pytest.raises(ApiError, match='具体版本') as e:
        api.create_trainset(f'T-{uuid.uuid4().hex[:8]}', {'sources': [src], 'classes': CLASSES})
    assert e.value.status == 400


def test_previous_splits_keep_the_extension(monkeypatch):
    """Two images with the same stem in different splits: each keeps its own split and label."""
    rows = [('images/train/SES/TX/same/a.png', 'i1', 1), ('labels/train/SES/TX/same/a.txt', 'l1', 1),
            ('json/train/SES/TX/same/a.json', 'j1', 1),
            ('images/val/SES/TX/same/a.jpg', 'i2', 1), ('labels/val/SES/TX/same/a.txt', 'l2', 1),
            ('images/test/SES/TX/b.png', 'i3', 1), ('classes.txt', 'c', 1)]
    monkeypatch.setattr(trainsets, '_files', lambda conn, version_id: rows)
    assert trainsets._previous_splits(None, {'id': 1}) == {
        'SES/TX/same/a.png': ('train', {'json': 'j1', 'labels': 'l1'}),
        'SES/TX/same/a.jpg': ('val', {'labels': 'l2'}), 'SES/TX/b.png': ('test', {})}


def test_same_stem_in_two_sources_is_refused(api, tmp_path):
    one = pool(api, tmp_path, {'same/a.png': png(uuid.uuid4().hex), 'same/a.json': label('scratch')})
    two = pool(api, tmp_path, {'same/a.jpg': png(uuid.uuid4().hex), 'same/a.json': label('dent')})
    with pytest.raises(ops.FwError, match='同名不同扩展名的图片 SES/TX/same/a.png 和 SES/TX/same/a.jpg'):
        make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{one}@1', f'{two}@1'])


def ultralytics_label(image: str) -> str:
    """ultralytics.data.utils.img2label_paths (8.x): the last /images/ becomes /labels/, the suffix .txt."""
    sa, sb = '/images/', '/labels/'
    return sb.join(image.rsplit(sa, 1)).rsplit('.', 1)[0] + '.txt'


def test_images_folders_inside_the_pool_path(api, cache, tmp_path):
    files = {'batch/images/one.png': png(uuid.uuid4().hex), 'batch/images/one.json': label('scratch'),
             'images_/two.png': png(uuid.uuid4().hex), 'images_/two.json': label('dent')}
    src = pool(api, tmp_path, files, line='images')
    v = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'], val=0)
    dest = tmp_path / 'g'
    tree = fetch(api, cache, v['ref'], dest, fmt='yolo-tree')
    images = sorted(p for p in tree if p.startswith('images/'))
    assert images == ['images/train/batch/one.png', 'images/train/images__/two.png']  # the last images/ goes
    for image in images:  # where ultralytics (and aoi-deteval) look for the label is where it is
        assert ultralytics_label('/data/' + image)[len('/data/'):] in tree
    assert tree['labels/train/batch/one.txt'].startswith(b'0 ')
    json_tree = fetch(api, cache, v['ref'], tmp_path / 'j', fmt='json')  # only the YOLO layout renames
    assert 'train/batch/one.json' in json_tree and 'train/images_/two.json' in json_tree


def test_a_yolo_pool_pairs_images_with_labels(api, cache, tmp_path):
    # 2026-11-06, the SES pool: <batch>/images/a.png with <batch>/labels/a.txt (and often a.json next to the image).
    files = {'b1/images/a.png': png(uuid.uuid4().hex), 'b1/labels/a.txt': yolo_line(1).encode(),
             'b1/images/sub/c.png': png(uuid.uuid4().hex), 'b1/labels/sub/c.txt': yolo_line(0).encode(),
             'b1/images/neg.png': png(uuid.uuid4().hex),
             'images/top.png': png(uuid.uuid4().hex), 'labels/top.txt': yolo_line(0).encode()}
    src = pool(api, tmp_path, files)
    v = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'], val=0)
    train = v['trainset']['counts']['train']
    assert (train['samples'], train['negatives']) == (4, 1)
    tree = fetch(api, cache, v['ref'], tmp_path / 'g', fmt='yolo-tree')
    assert tree['labels/train/b1/a.txt'] == yolo_line(1).encode()  # tx_v8/images/1.bmp -> images/train/1.bmp
    assert tree['labels/train/b1/sub/c.txt'] == yolo_line(0).encode()
    assert tree['labels/train/top.txt'] == yolo_line(0).encode() and tree['images/train/top.png']
    assert not any(p.endswith('neg.txt') for p in tree)
    for image in (p for p in tree if p.startswith('images/') and not p.endswith('neg.png')):
        assert ultralytics_label('/data/' + image)[len('/data/'):] in tree
    # a label in labels/ whose image is not where YOLO would look is still an orphan
    src = pool(api, tmp_path, {'b1/pics/a.png': png(uuid.uuid4().hex), 'b1/labels/a.txt': yolo_line(0).encode()})
    preview = api.preview_trainset(f'T-{uuid.uuid4().hex[:8]}', {'sources': [f'{src}@1'], 'classes': CLASSES})
    assert any('没有对应的图片' in p and 'b1/labels/a.txt' in p for p in preview['problems']), preview['problems']


def test_short_paths_that_collide_are_refused(api, tmp_path):
    # fw get drops <site>/<line>/ and the pool's images/ folder: two pools with images/1.png, or one pool with
    # both images/2.png and 2.png, would land on the same path
    tx = pool(api, tmp_path, {'images/1.png': png(uuid.uuid4().hex), 'labels/1.txt': yolo_line(0).encode()})
    rx = pool(api, tmp_path, {'images/1.png': png(uuid.uuid4().hex), 'labels/1.txt': yolo_line(1).encode()},
              line='RX')
    preview = api.preview_trainset(f'T-{uuid.uuid4().hex[:8]}', {'sources': [f'{tx}@1', f'{rx}@1'],
                                                                  'classes': CLASSES, 'val': 0})
    assert any('去掉现场/产线' in p and 'images/train/1.png' in p for p in preview['problems']), preview['problems']
    one = pool(api, tmp_path, {'images/2.png': png(uuid.uuid4().hex), '2.png': png(uuid.uuid4().hex)})
    preview = api.preview_trainset(f'T-{uuid.uuid4().hex[:8]}', {'sources': [f'{one}@1'], 'classes': CLASSES})
    assert any('去掉现场/产线' in p for p in preview['problems']), preview['problems']
    other = pool(api, tmp_path, {'images/3.png': png(uuid.uuid4().hex)}, line='RX')
    ok = api.preview_trainset(f'T-{uuid.uuid4().hex[:8]}', {'sources': [f'{tx}@1', f'{other}@1'],
                                                             'classes': CLASSES})
    assert ok['problems'] == []


def test_short_paths_never_share_a_label(api, tmp_path):
    # review of #11: images/1.jpg with labels/1.txt and an unlabeled 1.png would both read labels/train/1.txt
    src = pool(api, tmp_path, {'images/1.jpg': png(uuid.uuid4().hex), 'labels/1.txt': yolo_line(0).encode(),
                               '1.png': png(uuid.uuid4().hex)})
    preview = api.preview_trainset(f'T-{uuid.uuid4().hex[:8]}', {'sources': [f'{src}@1'], 'classes': CLASSES,
                                                                  'val': 0})
    assert any('共用一份标注' in p and 'images/train/1.jpg' in p and 'images/train/1.png' in p
               for p in preview['problems']), preview['problems']
    # 2nd review: only the case differs (one file on Windows), and many such pairs are grouped in one pass
    files = {'images/A.jpg': png(uuid.uuid4().hex), 'labels/A.txt': yolo_line(0).encode(),
             'a.png': png(uuid.uuid4().hex)}
    src = pool(api, tmp_path, files)
    preview = api.preview_trainset(f'T-{uuid.uuid4().hex[:8]}', {'sources': [f'{src}@1'], 'classes': CLASSES,
                                                                  'val': 0})
    assert any('共用一份标注' in p and 'images/train/A.jpg' in p and 'images/train/a.png' in p
               for p in preview['problems']), preview['problems']


def test_shared_label_check_is_one_pass(monkeypatch):
    # 2nd review of #11: every shared stem rescanned the whole list (500 pairs: 1,000,000 _stem calls)
    calls = []
    real = trainsets._stem
    monkeypatch.setattr(trainsets, '_stem', lambda p: calls.append(p) or real(p))
    entries = []
    for i in range(500):
        entries += [(f'images/train/SES/TX/images/{i}.jpg', 'a', 1), (f'images/train/SES/TX/{i}.png', 'b', 1)]
    problems = trainsets._shared_labels(entries)
    assert len(problems) == 2 * 500  # every pair, in the yolo and the json layout
    assert len(calls) == 2 * len(entries)


def test_an_old_layout_snapshot_can_be_made_again_with_short_paths(api, tmp_path, monkeypatch):
    # review of #11: no COCO (size unreadable), so only the layout differs from the snapshot made before 0.2.3
    src = pool(api, tmp_path, {'a.png': b'no header'})
    name = f'T-{uuid.uuid4().hex[:8]}'
    monkeypatch.setattr(trainsets, 'PATHS', None)
    old = make(api, name, [f'{src}@1'])
    assert 'paths' not in old['trainset']['rule'] and 'coco' not in old['trainset']['formats']
    monkeypatch.setattr(trainsets, 'PATHS', 'short')
    new = make(api, name, [f'{src}@1'])
    assert new['number'] == 2 and new['trainset']['rule']['paths'] == 'short'
    preview = api.preview_trainset(name, {'sources': [f'{src}@1'], 'classes': CLASSES})
    assert any('完全一样' in p for p in preview['problems'])  # the same layout again is unchanged


def test_one_label_is_never_given_to_two_images(api, tmp_path):
    # review of #10: labels/a.txt is next to an image labels/a.png and where YOLO looks for images/a.png
    files = {'b/images/a.png': png(uuid.uuid4().hex), 'b/labels/a.png': png(uuid.uuid4().hex),
             'b/labels/a.txt': yolo_line(0).encode(), 'b/images/c.png': png(uuid.uuid4().hex),
             'b/labels/c.txt': yolo_line(1).encode()}
    src = pool(api, tmp_path, files)
    preview = api.preview_trainset(f'T-{uuid.uuid4().hex[:8]}', {'sources': [f'{src}@1'], 'classes': CLASSES})
    shared = [p for p in preview['problems'] if '同时配到了两张图' in p]
    assert len(shared) == 1 and 'b/labels/a.txt' in shared[0] and 'c.txt' not in shared[0], preview['problems']


def test_label_source_picks_json_or_txt(api, cache, tmp_path):
    files = {'b1/images/a.png': png(uuid.uuid4().hex), 'b1/images/a.json': label('scratch'),
             'b1/labels/a.txt': yolo_line(1).encode(),
             'b1/images/only_json.png': png(uuid.uuid4().hex), 'b1/images/only_json.json': label('dent'),
             'b1/images/only_txt.png': png(uuid.uuid4().hex), 'b1/labels/only_txt.txt': yolo_line(0).encode()}
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    preview = api.preview_trainset(name, {'sources': [f'{src}@1'], 'classes': CLASSES})
    assert any('同时有 JSON 和 txt' in p and '--label-source' in p for p in preview['problems']), preview['problems']
    for source, first in (('json', b'0 '), ('txt', b'1 ')):  # a.json says scratch (0), a.txt says dent (1)
        v = make(api, f'{name}-{source}', [f'{src}@1'], val=0, label_source=source)
        tree = fetch(api, cache, v['ref'], tmp_path / source)
        assert tree['labels/train/SES/TX/b1/images/a.txt'].startswith(first)
        assert tree['labels/train/SES/TX/b1/images/only_json.txt'].startswith(b'1 ')  # each still has its own
        assert tree['labels/train/SES/TX/b1/images/only_txt.txt'].startswith(b'0 ')
        assert json.loads(tree['trainset.json'])['rule']['label_source'] == source
        assert api.trainset(*v['ref'].split('@'))['trainset']['rule']['label_source'] == source
    two = pool(api, tmp_path, {'b/images/a.png': png(uuid.uuid4().hex), 'b/images/a.txt': yolo_line(0).encode(),
                               'b/labels/a.txt': yolo_line(1).encode()})
    preview = api.preview_trainset(name, {'sources': [f'{two}@1'], 'classes': CLASSES, 'label_source': 'txt'})
    assert any('两个同一种的标注' in p for p in preview['problems']), preview['problems']
    # review of #10: picking json must not hide two txt labels of one image (one next to it, one in labels/)
    three = pool(api, tmp_path, {'b/images/a.png': png(uuid.uuid4().hex), 'b/images/a.json': label('scratch'),
                                 'b/images/a.txt': yolo_line(0).encode(), 'b/labels/a.txt': yolo_line(1).encode()})
    for source in ('json', 'txt'):
        preview = api.preview_trainset(name, {'sources': [f'{three}@1'], 'classes': CLASSES, 'label_source': source})
        assert any('两个同一种的标注' in p for p in preview['problems']), (source, preview['problems'])
    assert status_of(lambda: api.preview_trainset(name, {'sources': [f'{src}@1'], 'classes': CLASSES,
                                                         'label_source': 'xml'})) == 400


SES5 = ['Dirty', 'Scratch', 'Crack', 'Solder', 'others']  # the SES pool's txt ids; others is background
SES4 = SES5[:4]


def test_class_map_drops_others_and_reads_txt_by_its_own_table(api, cache, tmp_path):
    # 2026-11-06, the SES pool: txt numbered by 5 classes, others (4) is background; the snapshot has 4 classes.
    kept = '3 0.15 0.4 0.2 0.4\n'  # Solder only, written by hand: nothing to change, so frozen byte for byte
    files = {'b1/images/a.png': png(uuid.uuid4().hex), 'b1/labels/a.txt': (yolo_line(4) + yolo_line(1)).encode(),
             'b1/images/b.png': png(uuid.uuid4().hex), 'b1/labels/b.txt': yolo_line(4).encode(),
             'b1/images/c.png': png(uuid.uuid4().hex), 'b1/labels/c.txt': kept.encode()}
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    body = {'sources': [f'{src}@1'], 'classes': SES4, 'val': 0}
    preview = api.preview_trainset(name, body)  # without --label-classes, id 4 is past the 4-class table
    assert any('超出类别表' in p for p in preview['problems']), preview['problems']
    preview = api.preview_trainset(name, dict(body, label_classes=SES5))
    assert any("'others' 不在类别表里" in p for p in preview['problems']), preview['problems']
    v = make(api, name, [f'{src}@1'], SES4, val=0, label_classes=SES5, class_map={'others': ''})
    ts = v['trainset']
    assert ts['counts']['train']['boxes'] == {'Dirty': 0, 'Scratch': 1, 'Crack': 0, 'Solder': 1}
    assert ts['counts']['train']['negatives'] == 0  # b has a label: an image with no boxes, not a missing label
    assert ts['rule']['label_classes'] == SES5 and ts['rule']['class_map'] == {'others': ''}
    tree = fetch(api, cache, v['ref'], tmp_path / 'g', fmt='all')
    lab = 'labels/train/SES/TX/b1/images/'  # fmt all: the snapshot as it is stored
    assert tree[lab + 'a.txt'] == yolo_line(1).encode()
    assert tree[lab + 'b.txt'] == b''
    assert tree[lab + 'c.txt'] == kept.encode()
    assert tree['classes.txt'] == ('\n'.join(SES4) + '\n').encode()
    a = json.loads(tree['json/train/SES/TX/b1/images/a.json'])
    assert [s['label'] for s in a['shapes']] == ['Scratch']
    coco = json.loads(tree['coco/train.json'])
    assert sorted(x['category_id'] for x in coco['annotations']) == [1, 3]
    assert json.loads(tree['trainset.json'])['rule']['class_map'] == {'others': ''}
    # the notices say what the map did
    preview = api.preview_trainset(f'{name}-again', dict(body, label_classes=SES5, class_map={'others': ''}))
    assert '映射：others 的 2 个框去掉，当背景' in preview['notices'], preview['notices']
    assert any(n.startswith('1 张图的框全被映射去掉了') and 'b1/labels/b.txt' in n for n in preview['notices'])


def test_class_map_on_json_keeps_the_rest_of_the_file(api, cache, tmp_path):
    poly = {'label': 'others', 'points': [[0, 0], [5, 0], [5, 5]], 'shape_type': 'polygon', 'group_id': 3}
    point = {'label': 'others', 'points': [[1, 1]], 'shape_type': 'point'}  # dropped with its class, not refused
    keep = {'label': 'DL1', 'points': [[10, 20], [50, 20], [50, 60], [10, 60]], 'shape_type': 'rectangle',
            'description': '划痕', 'flags': {'x': True}}
    doc = json.loads(label(shapes=[poly, keep, point]))
    doc['custom'] = {'kept': [1, 2]}
    raw = json.dumps(doc, ensure_ascii=False).encode()
    files = {'d.png': png(uuid.uuid4().hex), 'd.json': raw,
             'e.png': png(uuid.uuid4().hex), 'e.json': label('DL2', 'DL'),
             'f.png': png(uuid.uuid4().hex), 'f.json': label('DL')}
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    v = make(api, name, [f'{src}@1'], ['DL'], val=0, class_map={'others': '', 'DL1': 'DL', 'DL2': 'DL'})
    assert v['trainset']['counts']['train']['boxes'] == {'DL': 4}
    tree = fetch(api, cache, v['ref'], tmp_path / 'g', fmt='all')
    d = json.loads(tree['json/train/SES/TX/d.json'])
    assert d['shapes'] == [dict(keep, label='DL')] and d['custom'] == {'kept': [1, 2]}
    assert list(d) == list(doc)  # the same keys in the same order
    assert tree['json/train/SES/TX/f.json'] == label('DL')  # nothing to change: the file as it is
    assert tree['labels/train/SES/TX/e.txt'] == (yolo_line(0) * 2).encode()
    # a map source that no label has is refused: a typo would otherwise leave the boxes as they are
    preview = api.preview_trainset(f'{name}-x', {'sources': [f'{src}@1'], 'classes': ['DL', 'DL1', 'DL2'],
                                                 'class_map': {'others': '', 'dl1': 'DL'}})
    assert any("映射的源类别 dl1" in p for p in preview['problems']), preview['problems']


def test_class_map_snapshot_continues_without_relabeling(api, tmp_path):
    files = {'b1/images/a.png': png(uuid.uuid4().hex), 'b1/labels/a.txt': (yolo_line(4) + yolo_line(0)).encode(),
             'b1/images/j.png': png(uuid.uuid4().hex), 'b1/images/j.json': label('others', 'Crack')}
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    kw = dict(label_classes=SES5, class_map={'others': '', 'Crack': 'Scratch'})
    make(api, name, [f'{src}@1'], SES4, val=0, **kw)
    add_version(api, tmp_path, src, dict(files, **{'b1/images/new.png': png(uuid.uuid4().hex)}))
    body = {'sources': [f'{src}@2'], 'classes': SES4, **kw}
    preview = api.preview_trainset(name, body)  # the mapped labels frozen last time are the same: not relabeled
    assert (preview['changes']['added'], preview['changes']['relabeled']) == (1, 0), preview['changes']
    v = make(api, name, [f'{src}@2'], SES4, **kw)
    boxes = {split: c['boxes'] for split, c in v['trainset']['counts'].items() if c['samples']}
    assert boxes == {'train': {'Dirty': 1, 'Scratch': 1, 'Crack': 0, 'Solder': 0},
                     'test': {'Dirty': 0, 'Scratch': 0, 'Crack': 0, 'Solder': 0}}


def test_a_new_class_map_on_a_continued_snapshot_counts_as_relabeled(api, tmp_path):
    # review of #10: the labels the snapshot freezes are compared, not the pool's files before the map
    files = {'x.png': png(uuid.uuid4().hex), 'x.txt': yolo_line(0).encode(),
             'y.png': png(uuid.uuid4().hex), 'y.json': label('A')}
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    make(api, name, [f'{src}@1'], ['A', 'B'], val=0)
    add_version(api, tmp_path, src, dict(files, **{'new.png': png(uuid.uuid4().hex)}))
    body = {'sources': [f'{src}@2'], 'classes': ['A', 'B']}
    assert api.preview_trainset(name, body)['changes']['relabeled'] == 0
    changes = api.preview_trainset(name, dict(body, class_map={'A': 'B'}))['changes']
    assert (changes['added'], changes['relabeled']) == (1, 2), changes


def test_a_txt_with_the_bytes_of_a_json_is_read_as_txt(api, tmp_path):
    # review 2 of #10: the parsed labels were cached by content alone, so b.txt took a.json's boxes
    files = {'a.png': png(uuid.uuid4().hex), 'a.json': label('scratch'),
             'b.png': png(uuid.uuid4().hex), 'b.txt': label('scratch')}
    src = pool(api, tmp_path, files)
    preview = api.preview_trainset(f'T-{uuid.uuid4().hex[:8]}', {'sources': [f'{src}@1'], 'classes': CLASSES,
                                                                'label_source': 'txt'})
    assert any('b.txt' in p and '不是整数' in p for p in preview['problems']), preview['problems']


def test_switching_the_label_source_counts_as_relabeled(api, tmp_path):
    # review 2 of #10: the txt is byte for byte the YOLO made from the JSON, whose size (200x100) is not the
    # image's (400x200), so the JSON and COCO frozen for it change: that is a relabeled sample
    yolo = yolo_line(0).encode()  # what the JSON's box (10,20)-(50,60) on 200x100 becomes
    files = {'b/images/x.png': png(uuid.uuid4().hex, 400, 200), 'b/images/x.json': label('scratch'),
             'b/labels/x.txt': yolo}
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    make(api, name, [f'{src}@1'], val=0, label_source='json')
    add_version(api, tmp_path, src, dict(files, **{'b/images/new.png': png(uuid.uuid4().hex)}))
    body = {'sources': [f'{src}@2'], 'classes': CLASSES}
    assert api.preview_trainset(name, dict(body, label_source='json'))['changes']['relabeled'] == 0
    changes = api.preview_trainset(name, dict(body, label_source='txt'))['changes']
    assert (changes['added'], changes['relabeled']) == (1, 1), changes


def test_a_json_that_comes_or_goes_counts_as_relabeled(api, tmp_path):
    # review 3 of #10: bad.png has no readable size, so a txt-labelled snapshot has no LiteAnno JSON; x.txt is
    # byte for byte the YOLO made from x.json. json -> txt -> json: x's json/ goes, then comes back
    files = {'bad.png': b'not an image ' + uuid.uuid4().hex.encode(), 'bad.txt': yolo_line(1).encode(),
             'b/images/x.png': png(uuid.uuid4().hex), 'b/images/x.json': label('scratch'),
             'b/labels/x.txt': yolo_line(0).encode()}
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    body = {'sources': [f'{src}@1'], 'classes': CLASSES, 'val': 0}
    make(api, name, [f'{src}@1'], val=0, label_source='json')
    for source in ('txt', 'json'):
        changes = api.preview_trainset(name, dict(body, label_source=source))['changes']
        assert changes['relabeled'] == 1, (source, changes)
        make(api, name, [f'{src}@1'], val=0, label_source=source)


def test_a_class_map_on_a_json_with_a_lone_surrogate(api, cache, tmp_path):
    # review 2 of #10: "\udc80" has no UTF-8; rewriting the file gave HTTP 500
    doc = json.loads(label('A'))
    doc['shapes'][0]['description'] = '\udc80'
    raw = json.dumps(doc).encode()  # ensure_ascii: the file holds the escape \udc80
    src = pool(api, tmp_path, {'a.png': png(uuid.uuid4().hex), 'a.json': raw})
    v = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'], ['B'], val=0, class_map={'A': 'B'})
    tree = fetch(api, cache, v['ref'], tmp_path / 'g', fmt='json')
    shape = json.loads(tree['train/a.json'])['shapes'][0]
    assert (shape['label'], shape['description']) == ('B', '\udc80')


def test_class_map_options_are_checked(api, tmp_path):
    src = pool(api, tmp_path, {'a.png': png(uuid.uuid4().hex), 'a.json': label('scratch')})
    name = f'T-{uuid.uuid4().hex[:8]}'
    body = {'sources': [f'{src}@1'], 'classes': CLASSES}
    for extra in ({'class_map': {'scratch': 'nope'}}, {'class_map': {'scratch': 'scratch'}},
                  {'class_map': {' scratch': ''}}, {'class_map': {'': 'dent'}},
                  {'label_classes': []}, {'label_classes': ['a', 'a']}, {'label_classes': ['a\tb']}):
        assert status_of(lambda: api.preview_trainset(name, dict(body, **extra))) == 400, extra
    preview = api.preview_trainset(name, dict(body, label_classes=['x']))  # only JSON labels: has no effect
    assert '没有用到 txt 标注，--label-classes 不起作用' in preview['notices']
    assert ops.parse_class_map(['others=', 'DL1 = DL']) == {'others': '', 'DL1': 'DL'}
    assert ops.parse_class_map([]) is None
    for bad in (['others'], ['=DL'], ['a=', 'a=b']):
        with pytest.raises(ops.FwError):
            ops.parse_class_map(bad)


def test_require_labels(api, tmp_path):
    src = pool(api, tmp_path, {'a.png': png(uuid.uuid4().hex), 'a.json': label('scratch'),
                               'neg.png': png(uuid.uuid4().hex)})
    name = f'T-{uuid.uuid4().hex[:8]}'
    preview = api.preview_trainset(name, {'sources': [f'{src}@1'], 'classes': CLASSES, 'require_labels': True})
    assert any('--require-labels' in p and 'SES/TX/neg.png' in p for p in preview['problems'])
    with pytest.raises(ops.FwError, match='没有建训练集快照'):
        make(api, name, [f'{src}@1'], require_labels=True)
    assert make(api, name, [f'{src}@1'])['trainset']['counts']['train']['negatives'] + \
        api.trainset(name)['trainset']['counts']['val']['negatives'] == 1


def test_a_snapshot_without_val_says_it_cannot_train(api, cache, tmp_path):
    src = pool(api, tmp_path, {'a.png': png(uuid.uuid4().hex), 'a.json': label('scratch')})
    lines = []
    v = ops.create_trainset(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'], CLASSES, echo=lines.append)
    assert any('没有 val 样本' in line for line in lines)
    lines.clear()
    out = ops.get(api, cache, v['ref'], tmp_path / 'g', echo=lines.append)
    assert out['missing_splits'] == ['val'] and any('不能直接训练' in line for line in lines)
    assert not any('交给训练代码' in line for line in lines)


def test_yolo_label_with_a_bom(api, cache, tmp_path):
    text = yolo_line(1)
    src = pool(api, tmp_path, {'a.png': png(uuid.uuid4().hex), 'a.txt': b'\xef\xbb\xbf' + text.encode()})
    v = make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'], val=0)
    tree = fetch(api, cache, v['ref'], tmp_path / 'g')
    assert tree['labels/train/SES/TX/a.txt'] == text.encode()
    assert int(tree['labels/train/SES/TX/a.txt'].split()[0]) == 1


def test_two_first_versions_at_once(api, make_api, service, tmp_path):
    """The name is taken together with the first version: the second request gets 409, not 500."""
    import threading
    import time
    src = pool(api, tmp_path, sample_files('a', 3))
    name = f'T-{uuid.uuid4().hex[:8]}'
    other = make_api('algo')
    body = {'sources': [f'{src}@1'], 'classes': CLASSES, 'seed': 1}
    result = {}

    def second():
        result['status'] = status_of(lambda: other.create_trainset(name, body))

    t = threading.Thread(target=second)

    def start_the_other_and_wait():
        t.start()
        time.sleep(1.5)  # it plans, then waits on the name this transaction has not committed yet

    service.hooks['before_freeze'] = start_the_other_and_wait
    try:
        v = make(api, name, [f'{src}@1'])
    finally:
        service.hooks.pop('before_freeze')
    t.join(30)
    assert v['number'] == 1 and result['status'] == 409
    assert [x['number'] for x in api.versions(name)] == [1]


# --- what the database keeps ----------------------------------------------------------------------------
@pytest.fixture
def snapshot(api, tmp_path):
    src = pool(api, tmp_path, {'a.png': b'1', 'a.json': label('scratch')})
    return make(api, f'T-{uuid.uuid4().hex[:8]}', [f'{src}@1'])


@pytest.mark.parametrize('statement', [
    "UPDATE trainset_versions SET classes = '[]' WHERE version_id = %(v)s",
    "UPDATE trainset_versions SET counts = '{}' WHERE version_id = %(v)s",
    "DELETE FROM trainset_versions WHERE version_id = %(v)s",
    "UPDATE trainset_sources SET source_id = source_id WHERE version_id = %(v)s",
    "DELETE FROM trainset_sources WHERE version_id = %(v)s",
    "INSERT INTO trainset_sources VALUES (%(v)s, 5, %(v)s)",
    "TRUNCATE trainset_versions",
    "TRUNCATE trainset_sources",
    "UPDATE datasets SET kind = 'pool' WHERE id = (SELECT dataset_id FROM versions WHERE id = %(v)s)",
])
def test_snapshot_record_cannot_change(sql, snapshot, statement):
    with pytest.raises(psycopg.Error):
        with sql.transaction():
            sql.execute(statement, {'v': snapshot['id']})
    row = sql.execute('SELECT classes FROM trainset_versions WHERE version_id = %s', (snapshot['id'],)).fetchone()
    assert row['classes'] == CLASSES


def test_only_trainset_versions_have_a_record(sql, snapshot, api, tmp_path):
    src = pool(api, tmp_path, {'a.png': b'1'})
    pool_version = api.version(src, 'latest')['id']
    with pytest.raises(psycopg.Error, match='not a trainset'):
        with sql.transaction():
            sql.execute("INSERT INTO trainset_versions VALUES (%s, NULL, '[]', '{}', '{}')", (pool_version,))
    with pytest.raises(psycopg.Error, match='has versions'):
        with sql.transaction():
            sql.execute("UPDATE datasets SET kind = 'trainset' WHERE name = %s", (src,))


# --- the command line and the training-script call -----------------------------------------------------------
def test_an_old_server_that_ignores_the_label_options_is_refused():
    class OldServer:  # a server from before these options: takes the request, leaves them out of the rule
        def preview_trainset(self, name, body):
            return {'rule': {'mode': 'fresh'}, 'based_on': None, 'sources': body['sources'], 'problems': []}
    for kw, option in ((dict(label_source='txt'), '--label-source'), (dict(label_classes=SES5), '--label-classes'),
                       (dict(class_map={'others': ''}), '--map')):
        with pytest.raises(ops.FwError, match=f'服务器版本太旧，不认 {option}'):
            ops.create_trainset(OldServer(), 'T', ['P@1'], SES4, echo=quiet, dry_run=True, **kw)


def test_command_line_class_map(service, tokens, api, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('FW_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setenv('FW_CACHE', str(tmp_path / 'cache'))
    monkeypatch.delenv('FW_SERVER', raising=False)
    monkeypatch.delenv('FW_TOKEN', raising=False)
    assert fw_cli.main(['login', '--server', service.url, '--token', tokens['algo']]) == 0
    src = pool(api, tmp_path, {'b/images/a.png': png(uuid.uuid4().hex), 'b/images/a.json': label('others'),
                               'b/labels/a.txt': (yolo_line(4) + yolo_line(2)).encode()})
    (tmp_path / 'c4.txt').write_text('\n'.join(SES4) + '\n', encoding='utf-8')
    (tmp_path / 'c5.txt').write_text('\n'.join(SES5) + '\n', encoding='utf-8')
    name = f'T-{uuid.uuid4().hex[:8]}'
    args = ['trainset', 'create', name, '--from', f'{src}@1', '--classes', str(tmp_path / 'c4.txt'), '--val', '0',
            '--label-source', 'txt', '--label-classes', str(tmp_path / 'c5.txt'), '--map', 'others=', '--yes']
    assert fw_cli.main(args) == 0
    out = capsys.readouterr().out
    assert '标注：同时有 JSON 和 txt 的图用 txt；txt 按自己的 5 类编号读' in out and '映射 others→去掉' in out, out
    assert '注意：映射：others 的 1 个框去掉，当背景' in out
    assert fw_cli.main(['trainset', 'show', name]) == 0
    assert '映射 others→去掉' in capsys.readouterr().out
    assert fw_cli.main(args[:-3] + ['--map', 'others', '--dry-run']) == 1
    assert '--map 的写法' in capsys.readouterr().err


def test_command_line_and_get_trainset(service, tokens, api, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('FW_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setenv('FW_CACHE', str(tmp_path / 'cache'))
    monkeypatch.delenv('FW_SERVER', raising=False)
    monkeypatch.delenv('FW_TOKEN', raising=False)
    assert fw_cli.main(['login', '--server', service.url, '--token', tokens['algo']]) == 0
    src = pool(api, tmp_path, {**sample_files('2026-09', 10), **sample_files('2026-10', 2, ('dent',))})
    (tmp_path / 'classes.txt').write_text('scratch\ndent\n', encoding='utf-8')
    name = f'T-{uuid.uuid4().hex[:8]}'
    args = ['trainset', 'create', name, '--from', f'{src}@1', '--classes', str(tmp_path / 'classes.txt'),
            '--test-dirs', '2026-10']
    assert fw_cli.main(args + ['--dry-run']) == 0
    assert fw_cli.main(args) == 1  # no terminal to confirm in
    capsys.readouterr()
    assert fw_cli.main(args + ['--yes']) == 0
    out = capsys.readouterr().out
    assert f'已冻结 {name}@1' in out and 'test 2 张' in out
    assert fw_cli.main(args + ['--yes']) == 1  # unchanged
    assert '完全一样' in capsys.readouterr().err
    assert fw_cli.main(['trainset', 'list']) == 0
    assert name in capsys.readouterr().out
    assert fw_cli.main(['trainset', 'show', name]) == 0
    out = capsys.readouterr().out
    assert '重新划分' in out and 'test 目录 2026-10' in out and 'dent' in out and 'YOLO、COCO、LiteAnno JSON' in out
    assert fw_cli.main(['dataset', 'list']) == 0
    assert '训练集快照' in capsys.readouterr().out
    assert fw_cli.main(['trainset', 'show', src]) == 1

    assert fw_cli.main(['get', name, '--to', str(tmp_path / 'coco'), '--format', 'coco']) == 0
    assert (tmp_path / 'coco' / 'valid' / '_annotations.coco.json').is_file()
    assert fw_cli.main(['get', src, '--to', str(tmp_path / 'p'), '--format', 'coco']) == 1
    assert '不是训练集快照' in capsys.readouterr().err
    assert fw_cli.main(['export', name, '--to', str(tmp_path / 'exp')]) == 0  # exported as stored
    assert (tmp_path / 'exp' / 'data' / 'coco').is_dir()
    assert fw_cli.main(['verify', str(tmp_path / 'exp')]) == 0

    import fw
    ts = fw.get_trainset(name, tmp_path / 'train')
    assert ts['ref'] == f'{name}@1' and ts['classes'] == CLASSES and ts['sources'] == [f'{src}@1']
    assert ts['path'] == str(tmp_path / 'train') and (tmp_path / 'train' / 'classes.txt').is_file()
    assert ts['counts']['test']['boxes']['dent'] == 2 and ts['format'] == 'yolo' and ts['task'] == 'det'
    assert ts['data_yaml'] == str(tmp_path / 'train' / 'data.yaml')
    assert ts['formats'] == ['yolo', 'coco', 'json']
    js = fw.get_trainset(name, tmp_path / 'json', fmt='json')
    assert 'data_yaml' not in js and (tmp_path / 'json' / 'test' / '2026-10' / 'img000.json').is_file()


# --- the flat layout of fw get, the three-way split ----------------------------------------------------
def flat_pool(api, tmp_path) -> str:
    """A pool laid out like the field's: train, neg_train, val, neg_val and a field folder, each with images/
    and labels/ (YOLO txt; the negatives have an empty label, a blank one, or none)."""
    files = {}
    for i in range(3):
        files[f'train/images/t{i}.bmp'] = png(uuid.uuid4().hex)
        files[f'train/labels/t{i}.txt'] = yolo_line(i % 2).encode()
    files.update({'neg_train/images/n0.bmp': png(uuid.uuid4().hex), 'neg_train/labels/n0.txt': b'',
                  'neg_train/images/n1.bmp': png(uuid.uuid4().hex), 'neg_train/labels/n1.txt': b'\n \n\n\n\n\n\n\n\n\n',
                  'neg_train/images/n2.bmp': png(uuid.uuid4().hex),
                  'val/images/v0.bmp': png(uuid.uuid4().hex), 'val/labels/v0.txt': yolo_line(1).encode(),
                  'neg_val/images/w0.bmp': png(uuid.uuid4().hex),
                  'neg_val/images/w1.bmp': png(uuid.uuid4().hex), 'neg_val/labels/w1.txt': b'',
                  'field_1108/images/f0.bmp': png(uuid.uuid4().hex), 'field_1108/labels/f0.txt': yolo_line(0).encode(),
                  'field_1108/images/f1.bmp': png(uuid.uuid4().hex)})
    return pool(api, tmp_path, files), files


def cli_login(service, tokens, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv('FW_CONFIG', str(tmp_path / 'config.json'))
    monkeypatch.setenv('FW_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setenv('FW_SERVER', service.url)
    monkeypatch.setenv('FW_TOKEN', tokens['algo'])


def flat_split(files: dict) -> dict:
    split = {}
    for path in files:
        if '/images/' in path:
            top = path.split('/')[0]
            split[f'SES/TX/{path}'] = 'train' if 'train' in top else 'val' if 'val' in top else 'test'
    return split


def test_fw_get_lays_a_snapshot_out_flat_with_train_negatives_apart(service, tokens, api, cache, tmp_path,
                                                                    monkeypatch):
    cli_login(service, tokens, tmp_path, monkeypatch)
    src, files = flat_pool(api, tmp_path)
    name = f'T-{uuid.uuid4().hex[:8]}'
    lines = []
    v = ops.create_trainset(api, name, [f'{src}@1'], CLASSES, split=flat_split(files), echo=lines.append)
    counts = v['trainset']['counts']
    assert (counts['train']['samples'], counts['train']['negatives'], counts['train']['no_boxes']) == (6, 1, 3)
    assert (counts['val']['no_boxes'], counts['test']['no_boxes']) == (2, 1)
    assert any('train 里没有框的 3 张放在 neg_train/' in line for line in lines)
    assert not any('平铺后会重名' in line for line in lines)

    lines.clear()
    out = ops.get(api, cache, name, tmp_path / 'g', echo=lines.append)  # the default
    tree = read_tree(tmp_path / 'g')
    assert sorted(tree) == sorted(
        ['classes.txt', 'trainset.json', 'data.yaml'] +
        [f'train/images/t{i}.bmp' for i in range(3)] + [f'train/labels/t{i}.txt' for i in range(3)] +
        ['neg_train/n0.bmp', 'neg_train/n1.bmp', 'neg_train/n2.bmp'] +
        ['val/images/v0.bmp', 'val/images/w0.bmp', 'val/images/w1.bmp',
         'val/labels/v0.txt', 'val/labels/w0.txt', 'val/labels/w1.txt'] +
        ['test/images/f0.bmp', 'test/images/f1.bmp', 'test/labels/f0.txt', 'test/labels/f1.txt'])
    assert tree['train/images/t1.bmp'] == files['train/images/t1.bmp']
    assert tree['train/labels/t1.txt'] == yolo_line(1).encode()
    assert tree['neg_train/n1.bmp'] == files['neg_train/images/n1.bmp']
    assert tree['val/labels/w0.txt'] == b'' and tree['val/labels/w1.txt'] == b'' and tree['test/labels/f1.txt'] == b''
    assert tree['data.yaml'].decode().split('\n', 1)[1] == (
        f'path: "{(tmp_path / "g").resolve().as_posix()}"\n'
        'train: train/images\nval: val/images\ntest: test/images\nnc: 2\n'
        'names:\n  0: "scratch"\n  1: "dent"\n'
        'negative_setting:\n  neg_ratio: 0\n  use_extra_neg: true\n  extra_neg_sources:\n    neg_train: 3\n'
        '  fix_dataset_length: 0\n')
    assert out['format'] == 'yolo' and out['neg_train'] == 3 and out['files'] == len(tree) - 1
    assert 'missing_splits' not in out and any('neg_train/' in line for line in lines)

    # a label of blank lines only is frozen empty, so the size of a label tells whether it has a box
    stored = fetch(api, cache, name, tmp_path / 'all')
    assert stored['labels/train/SES/TX/neg_train/images/n1.txt'] == b''

    assert fw_cli.main(['get', name, '--to', str(tmp_path / 'c'), '--neg-ratio', '0.2', '--fix-dataset-length',
                        '5000']) == 0
    yaml = (tmp_path / 'c' / 'data.yaml').read_text(encoding='utf-8')
    assert '  neg_ratio: 0.2\n' in yaml and '  fix_dataset_length: 5000\n' in yaml and '    neg_train: 3\n' in yaml
    ops.get(api, cache, name, tmp_path / 'n', echo=quiet, neg_ratio=100.0)
    assert '  neg_ratio: 100\n' in (tmp_path / 'n' / 'data.yaml').read_text(encoding='utf-8')  # a count
    for kw in ({'fmt': 'coco', 'neg_ratio': 0.2}, {'fmt': 'yolo-tree', 'fix_dataset_length': 10},
               {'subpath': 'val', 'neg_ratio': 0.2}):
        with pytest.raises(ops.FwError, match='只用于'):
            ops.get(api, cache, name, tmp_path / 'x', echo=quiet, **kw)
    for kw in ({'neg_ratio': -1}, {'neg_ratio': float('nan')}, {'fix_dataset_length': 1.5}, {'fix_dataset_length': -3}):
        with pytest.raises(ops.FwError, match='不小于 0'):
            ops.get(api, cache, name, tmp_path / 'x', echo=quiet, **kw)
    assert not (tmp_path / 'x').exists()

    part = fetch(api, cache, name, tmp_path / 'part', fmt=None)  # nothing to download: only made labels
    assert 'val/labels/w0.txt' in part
    ops.get(api, cache, name, tmp_path / 'sub', echo=quiet, subpath='val/labels')
    assert sorted(read_tree(tmp_path / 'sub')) == ['val/labels/v0.txt', 'val/labels/w0.txt', 'val/labels/w1.txt']

    tree = fetch(api, cache, name, tmp_path / 't', fmt='yolo-tree')  # the layout before, with the pool's folders
    assert 'images/train/neg_train/n0.bmp' in tree and 'labels/train/train/t0.txt' in tree
    yaml = tree['data.yaml'].decode()
    assert 'train: images/train\n' in yaml and '\nnegative_setting' not in yaml

    import fw
    ts = fw.get_trainset(name, tmp_path / 'script', neg_ratio=0.5)
    assert ts['neg_train'] == 3 and '  neg_ratio: 0.5\n' in (tmp_path / 'script' / 'data.yaml').read_text('utf-8')

    rows = api.get(f'/trainsets/{name}/versions/1/layout')
    assert rows['format'] == 'yolo' and rows['problems'] == []
    assert {r[0] for r in rows['files']} == set(part) - {'data.yaml'}
    assert ['neg_train/n2.bmp', 'images/train/SES/TX/neg_train/images/n2.bmp'] in [[r[0], r[3]] for r in rows['files']]
    assert [r[3] for r in rows['files'] if r[0] == 'val/labels/w0.txt'] == [None]


def test_no_negative_setting_without_train_negatives(api, cache, tmp_path):
    src = pool(api, tmp_path, {'a/1.png': png(uuid.uuid4().hex), 'a/1.json': label('scratch'),
                               'b/2.png': png(uuid.uuid4().hex)})
    name = f'T-{uuid.uuid4().hex[:8]}'
    make(api, name, [f'{src}@1'], split={'SES/TX/a/1.png': 'train', 'SES/TX/b/2.png': 'val'})
    lines = []
    out = ops.get(api, cache, name, tmp_path / 'g', echo=lines.append, neg_ratio=0.3)
    yaml = (tmp_path / 'g' / 'data.yaml').read_text(encoding='utf-8')
    assert '\nnegative_setting' not in yaml and 'nc: 2\n' in yaml and out['neg_train'] == 0
    assert 'train: train/images\nval: val/images\nnc: 2\n' in yaml  # no test split, no test line
    make(api, name, [f'{src}@1'], split={'SES/TX/a/1.png': 'val', 'SES/TX/b/2.png': 'train'})
    out = ops.get(api, cache, name, tmp_path / 'h', echo=lines.append)
    assert out['missing_splits'] == ['train'] and out['neg_train'] == 1  # train holds no image with a box
    assert any('train 只算有框的图' in line for line in lines)


def test_names_that_collide_flat_are_told_in_the_preview_and_refused_by_fw_get(api, cache, tmp_path):
    src = pool(api, tmp_path, {'a/1.png': png(uuid.uuid4().hex), 'a/1.json': label('scratch'),
                               'b/1.png': png(uuid.uuid4().hex), 'b/1.json': label('dent'),
                               'c/2.png': png(uuid.uuid4().hex), 'c/2.json': label('dent')})
    name = f'T-{uuid.uuid4().hex[:8]}'
    lines = []
    v = ops.create_trainset(api, name, [f'{src}@1'], CLASSES, val=0, echo=lines.append)  # made all the same
    told = [line for line in lines if '平铺后会重名' in line]
    assert len(told) == 1 and 'images/train/SES/TX/a/1.png、images/train/SES/TX/b/1.png' in told[0]
    with pytest.raises(ops.FwError, match='yolo-tree') as e:
        ops.get(api, cache, v['ref'], tmp_path / 'g', echo=quiet)
    assert 'images/train/SES/TX/a/1.png、images/train/SES/TX/b/1.png 平铺后是同一个文件' in str(e.value)
    assert not (tmp_path / 'g').exists()
    tree = fetch(api, cache, v['ref'], tmp_path / 't', fmt='yolo-tree')
    assert 'images/train/a/1.png' in tree and 'images/train/b/1.png' in tree
    rows = api.get(f'/trainsets/{name}/versions/1/layout')
    assert rows['files'] == [] and rows['problem_count'] == 1 and '平铺后是同一个文件' in rows['problems'][0]


def test_a_three_way_split_gives_every_split_its_share_of_negatives(service, tokens, api, tmp_path, monkeypatch):
    cli_login(service, tokens, tmp_path, monkeypatch)
    files = sample_files('p', 40)
    files.update({f'n/neg{i:03}.png': png(uuid.uuid4().hex) for i in range(15)})
    files.update({f'n/empty{i:03}.png': png(uuid.uuid4().hex) for i in range(5)})
    files.update({f'n/empty{i:03}.json': label() for i in range(5)})  # a label without a box: a negative too
    src = pool(api, tmp_path, files)
    name = f'T-{uuid.uuid4().hex[:8]}'
    body = {'sources': [f'{src}@1'], 'classes': CLASSES, 'val': 0.2, 'test': 0.1, 'seed': 3}
    p = api.preview_trainset(name, body)
    c = p['counts']
    assert [(c[s]['samples'], c[s]['no_boxes']) for s in ('train', 'val', 'test')] == [(42, 14), (12, 4), (6, 2)]
    assert p['rule']['test'] == 0.1 and p['rule']['stratify'] == 'boxes' and p['rule']['val'] == 0.2
    assert api.preview_trainset(name, body)['counts'] == c  # the same every time
    v = make(api, name, [f'{src}@1'], val=0.2, test=0.1, seed=3)
    assert v['trainset']['counts'] == c
    assert fw_cli.main(['trainset', 'create', f'{name}-b', '--from', f'{src}@1', '--classes',
                        str(write(tmp_path, 'classes.txt', b'scratch\ndent\n')), '--val', '0.2', '--test', '0.1',
                        '--seed', '3', '--yes']) == 0
    assert api.trainset(f'{name}-b')['trainset']['counts'] == c
    for bad, why in (({'val': 0.5, 'test': 0.5}, '加起来要小于 1'), ({'test': 1.0}, '0 到 1 之间'),
                     ({'test': -0.1}, '0 到 1 之间'), ({'test': 0.1, 'merge_test': True}, '--merge-test'),
                     ({'test': 0.1, 'split': {}}, '划分清单')):
        with pytest.raises(ApiError, match=why) as e:
            api.preview_trainset(name, dict(body, **bad))
        assert e.value.status == 400
