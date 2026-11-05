"""Training-set snapshots: the split, continuing a snapshot, the checks on labels and classes, and what the
database keeps frozen."""
import json
import struct
import uuid

import psycopg
import pytest

from helpers import quiet, read_tree, write
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
    api.create_dataset(name, site, line, 'pool', '')
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
    """{key: split} of every image in a snapshot fetched as yolo (the default)."""
    out = {}
    for path in tree:
        if path.startswith('images/'):
            split, _, key = path[len('images/'):].partition('/')
            out[key] = split
    return out


def fetch(api, cache, ref, dest, fmt=None) -> dict:
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

    tree = fetch(api, cache, v['ref'], tmp_path / 'yolo')  # yolo is the default
    assert tree['classes.txt'] == b'scratch\ndent\n'
    info = json.loads(tree['trainset.json'])
    assert info['format'] == trainsets.FORMAT and info['counts'] == counts and info['classes'] == CLASSES
    assert info['task'] == 'det' and info['formats'] == ['yolo', 'coco', 'json'] and info['missing_formats'] == {}
    assert info['sources'][0]['ref'] == f'{src}@1'
    assert info['rule'] == {'mode': 'fresh', 'val': 0.25, 'seed': 0, 'test_dirs': ['2026-10'], 'merge_test': False}
    splits = splits_of(tree)
    assert len(splits) == 29 and all(s == 'test' for k, s in splits.items() if k.startswith('SES/TX/2026-10/'))
    for path, data in files.items():
        key = f'SES/TX/{path}'
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
    assert not any(p.startswith(f'labels/{s}/SES/TX/2026-09/neg') for p in tree for s in ('train', 'val'))
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
    assert test['images'][0] == {'id': 1, 'file_name': 'SES/TX/2026-10/img000.png', 'width': W, 'height': H}
    assert test['annotations'][0] == {'id': 1, 'image_id': 1, 'category_id': 0, 'bbox': [10, 20, 40, 40],
                                      'area': 1600, 'iscrowd': 0, 'segmentation': []}
    trainval = [json.loads(coco[f'{d}/_annotations.coco.json']) for d in ('train', 'valid')]
    assert sum(len(d['images']) for d in trainval) == 24 and sum(len(d['annotations']) for d in trainval) == 42
    extra = [(d, i) for d in trainval for i in d['images'] if i['file_name'] == 'SES/TX/2026-09/extra.bmp'][0]
    assert extra[1]['width'] == W and extra[1]['height'] == H  # size from the BMP header
    boxes = [a['bbox'] for a in extra[0]['annotations'] if a['image_id'] == extra[1]['id']]
    assert boxes == [[90.0, 45.0, 20.0, 10.0], [30.0, 15.0, 20.0, 10.0]]

    js = fetch(api, cache, v['ref'], tmp_path / 'json', fmt='json')
    made = json.loads(js[f'{splits["SES/TX/2026-09/extra.bmp"]}/SES/TX/2026-09/extra.json'])
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
                    **{f'train/SES/TX/{p}': d for p, d in files.items()}}
    ops.get(api, cache, v['ref'], tmp_path / 'sub', fmt='json', subpath='train/SES/TX/a', echo=quiet)
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
    assert v['trainset']['rule'] == {'mode': 'split-file', 'split_count': 4}
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
    other = pool(api, tmp_path, {'a.png': b'3'}, site='SES', line='TY')
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
    for role in ('field', 'deploy'):
        api = make_api(role)
        assert status_of(api.trainsets) == 403
        assert status_of(lambda: api.trainset(name)) == 403
        assert status_of(lambda: api.preview_trainset(name, body)) == 403
        assert status_of(lambda: api.create_trainset(name, body)) == 403


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
        'SES/TX/same/a.png': ('train', {'j1', 'l1'}), 'SES/TX/same/a.jpg': ('val', {'l2'}),
        'SES/TX/b.png': ('test', set())}


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
    tree = fetch(api, cache, v['ref'], dest)
    images = sorted(p for p in tree if p.startswith('images/'))
    assert images == ['images/train/SES/images_/batch/images_/one.png', 'images/train/SES/images_/images__/two.png']
    for image in images:  # where ultralytics (and aoi-deteval) look for the label is where it is
        assert ultralytics_label('/data/' + image)[len('/data/'):] in tree
    assert tree['labels/train/SES/images_/batch/images_/one.txt'].startswith(b'0 ')
    json_tree = fetch(api, cache, v['ref'], tmp_path / 'j', fmt='json')  # only the YOLO layout renames
    assert 'train/SES/images/batch/images/one.json' in json_tree


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
    assert out['missing_splits'] == ['val'] and any('不能直接 yolo train' in line for line in lines)
    assert not any('yolo train data=' in line for line in lines)


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
    assert 'data_yaml' not in js and (tmp_path / 'json' / 'test' / 'SES' / 'TX' / '2026-10' / 'img000.json').is_file()
