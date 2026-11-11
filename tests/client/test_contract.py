import hashlib

import pytest

from fw.contract import (OBJECTS_MAGIC, ContractError, FrameParser, batches, classify_changes, collisions,
                         diff_entries, frame_header, manifest_bytes, manifest_hash, parse_manifest, path_problem)

H1 = '1' * 64
H2 = '2' * 64
H3 = '3' * 64


@pytest.mark.parametrize('path', ['a.bmp', 'line/TX/2026_10_11 15.bmp', '中文/图.json', 'a/b/c.txt', 'con2.bmp',
                                  'COMPANY.json'])
def test_good_paths(path):
    assert path_problem(path) is None


@pytest.mark.parametrize('path', ['', '/abs.bmp', 'a//b', './a', 'a/../b', 'a:b.json', 'a\\b.json', 'a?.json',
                                  'tab\there', 'end.', 'space ', 'dir./x', 'CON', 'con.json', 'x/NUL.txt',
                                  'LPT1.bmp', 'é.bmp', 'x' * 1025])
def test_bad_paths(path):
    assert path_problem(path) is not None


def test_collisions_case_and_file_vs_dir():
    assert collisions(['a/B.json', 'a/b.json', 'x', 'x/y', 'ok.bmp'])
    assert len(collisions(['a/B.json', 'a/b.json'])) == 1
    assert collisions(['a/b', 'a/b/c']) == ['a/b 既是文件又是目录']
    assert collisions(['A/x', 'a/y']) == []  # the same folder spelt twice is not a clash of files
    assert collisions(['a.bmp', 'a.json']) == []


def test_manifest_round_trip_and_order():
    entries = [('b.json', H2, 5), ('a.bmp', H1, 0), ('中/x', H3, 10), ('Z.bmp', H1, 0), ('a/b', H2, 1)]
    data = manifest_bytes(entries)
    assert data.startswith(b'fw-manifest 1\n') and data.endswith(b'\n')
    parsed = parse_manifest(data)
    assert [p for p, _, _ in parsed] == sorted(p for p, _, _ in entries)  # str order == UTF-8 byte order
    assert sorted(parsed) == sorted(entries)
    assert manifest_hash(data) == hashlib.sha256(data).hexdigest()
    assert manifest_bytes([]) == b'fw-manifest 1\n'
    assert parse_manifest(b'fw-manifest 1\n') == []


@pytest.mark.parametrize('data', [
    b'fw-manifest 2\n',
    b'fw-manifest 1',
    b'fw-manifest 1\r\na\t' + H1.encode() + b'\t1\r\n',
    b'fw-manifest 1\nb\t' + H1.encode() + b'\t1\na\t' + H1.encode() + b'\t1\n',  # not sorted
    b'fw-manifest 1\na\t' + H1.encode() + b'\t1\na\t' + H1.encode() + b'\t1\n',  # duplicate
    b'fw-manifest 1\na\t' + ('a' * 64).upper().encode() + b'\t1\n',
    b'fw-manifest 1\na\t' + H1.encode() + b'\t01\n',
    b'fw-manifest 1\na\t' + H1.encode() + b'\t-1\n',
    b'fw-manifest 1\na\t' + H1.encode() + b'\n',
    b'fw-manifest 1\n../a\t' + H1.encode() + b'\t1\n',
    b'fw-manifest 1\nA\t' + H1.encode() + b'\t1\na\t' + H1.encode() + b'\t1\n',  # case twins
    b'fw-manifest 1\n\xff\t' + H1.encode() + b'\t1\n',
])
def test_strict_parse(data):
    with pytest.raises(ContractError):
        parse_manifest(data)


def test_diff_classification():
    old = [('a', H1, 1), ('b', H2, 1), ('c', H3, 1), ('same', H1, 1)]
    new = [('a2', H1, 1), ('a3', H1, 1), ('b', H3, 1), ('d', H2, 1), ('same', H1, 1)]
    d = diff_entries(old, new)
    assert d['renamed'] == [['a', 'a2']]  # a's content went to a2 and a3: one rename, one addition
    assert d['added'] == ['a3', 'd']
    assert d['deleted'] == ['c']
    assert d['modified'] == ['b']
    assert classify_changes({}, {}, []) == {'added': [], 'deleted': [], 'modified': [], 'renamed': []}


# --- batch transfer (fw-objects 1) ---------------------------------------------------------------------
def objects_body(objs):
    return OBJECTS_MAGIC + b''.join(frame_header(hashlib.sha256(d).hexdigest(), len(d)) + d for d in objs)


def test_frame_parser_round_trip_at_any_chunk_size():
    objs = [b'', b'\n', b'x', bytes(range(256)) * 4, b'y' * 70000, b'fw-objects 1\n']
    body = objects_body(objs)
    expected = [(hashlib.sha256(d).hexdigest(), d) for d in objs]
    for step in (1, 2, 3, 7, 13, 64, 65, 66, 1000, len(body)):
        parser = FrameParser()
        got = []
        for i in range(0, len(body), step):
            got += parser.feed(body[i:i + step])
        parser.finish()
        assert got == expected, step


def test_frame_parser_finish_catches_a_body_cut_inside_a_frame():
    objs = [b'hello', b'world!']
    body = objects_body(objs)
    boundaries = {len(OBJECTS_MAGIC)}
    pos = len(OBJECTS_MAGIC)
    for d in objs:
        pos += len(frame_header(hashlib.sha256(d).hexdigest(), len(d))) + len(d)
        boundaries.add(pos)
    for cut in range(len(body) + 1):
        parser = FrameParser()
        parser.feed(body[:cut])
        if cut in boundaries:
            parser.finish()  # only whole objects so far: receivers also check the count or Content-Length
        else:
            with pytest.raises(ContractError):
                parser.finish()


@pytest.mark.parametrize('body', [
    b'fw-objects 2\n',
    OBJECTS_MAGIC + b'not a header\n',
    OBJECTS_MAGIC + b'a' * 64 + b' 03\nabc',   # leading zero
    OBJECTS_MAGIC + b'A' * 64 + b' 3\nabc',    # upper-case hash
    OBJECTS_MAGIC + b'a' * 63 + b' 3\nabc',    # short hash
    OBJECTS_MAGIC + b'a' * 200,                # no end of header in sight
])
def test_frame_parser_refuses_malformed_bodies(body):
    with pytest.raises(ContractError):
        FrameParser().feed(body)


def test_frame_parser_refuses_objects_over_the_limit():
    parser = FrameParser(max_object=10)
    assert parser.feed(objects_body([b'x' * 10])) == [(hashlib.sha256(b'x' * 10).hexdigest(), b'x' * 10)]
    with pytest.raises(ContractError):
        parser.feed(frame_header(H1, 11))


def test_batches_split_by_count_and_bytes():
    objs = [(f'{i:064x}', size) for i, size in enumerate([10, 10, 10, 50, 5, 100, 1])]
    out = batches(objs, max_count=3, max_bytes=40)
    assert [[size for _, size in b] for b in out] == [[10, 10, 10], [50], [5], [100], [1]]
    assert [o for b in out for o in b] == objs  # same order, nothing lost; a big object travels alone
    assert batches([], 3, 40) == []


def test_trainset_layouts():
    from fw.contract import trainset_layout
    stored = [('classes.txt', 'c', 1), ('trainset.json', 't', 1),
              ('images/train/SES/TX/a/1.png', 'i1', 1), ('labels/train/SES/TX/a/1.txt', 'l1', 1),
              ('json/train/SES/TX/a/1.json', 'j1', 1), ('images/val/SES/TX/2.png', 'i2', 1),
              ('coco/train.json', 'ct', 1), ('coco/val.json', 'cv', 1)]
    paths = lambda fmt: {p: d for p, d, _ in trainset_layout(stored, fmt)}  # noqa: E731
    assert paths('yolo-tree') == {'classes.txt': 'c', 'trainset.json': 't', 'images/train/SES/TX/a/1.png': 'i1',
                                  'labels/train/SES/TX/a/1.txt': 'l1', 'images/val/SES/TX/2.png': 'i2'}
    assert paths('coco') == {'classes.txt': 'c', 'trainset.json': 't', 'train/SES/TX/a/1.png': 'i1',
                             'valid/SES/TX/2.png': 'i2', 'train/_annotations.coco.json': 'ct',
                             'valid/_annotations.coco.json': 'cv'}
    assert paths('json') == {'classes.txt': 'c', 'trainset.json': 't', 'train/SES/TX/a/1.png': 'i1',
                             'train/SES/TX/a/1.json': 'j1', 'val/SES/TX/2.png': 'i2'}
    assert paths('all') == {p: d for p, d, _ in stored}
    with pytest.raises(ContractError):
        trainset_layout(stored, 'voc')
    with pytest.raises(ContractError, match='不认识的路径'):
        trainset_layout([('other/x.png', 'x', 1)], 'yolo-tree')


def test_yolo_layout_renames_images_folders():
    from fw.contract import trainset_layout, yolo_safe
    assert yolo_safe('SES/TX/a/1.png') == 'SES/TX/a/1.png'
    assert yolo_safe('SES/images/images/images_/Images/images') == 'SES/images_/images_/images__/Images/images'
    stored = [('images/val/SES/TX/b/images/1.png', 'i', 1), ('labels/val/SES/TX/b/images/1.txt', 'l', 1),
              ('json/val/SES/TX/b/images/1.json', 'j', 1)]
    assert [p for p, _, _ in trainset_layout(stored, 'yolo-tree')] == ['images/val/SES/TX/b/images_/1.png',
                                                                       'labels/val/SES/TX/b/images_/1.txt']
    assert [p for p, _, _ in trainset_layout(stored, 'json')] == ['val/SES/TX/b/images/1.png',
                                                                 'val/SES/TX/b/images/1.json']


def test_short_paths_for_fw_get():
    from fw.contract import short_paths, trainset_layout
    assert short_paths(['SES/TX/images/1.bmp', 'SES/TX/b1/images/x/2.png', 'SES/RX/a/images/images/3.png',
                        'SES/TX/4.png']) == {
        'SES/TX/images/1.bmp': '1.bmp', 'SES/TX/b1/images/x/2.png': 'b1/x/2.png',
        'SES/RX/a/images/images/3.png': 'a/images/3.png', 'SES/TX/4.png': '4.png'}  # only the last images/ goes
    stored = [('images/train/SES/TX/images/1.bmp', 'i', 1), ('labels/train/SES/TX/images/1.txt', 'l', 1),
              ('json/train/SES/TX/images/1.json', 'j', 1), ('coco/train.json', 'c', 1)]
    lay = lambda fmt, short=True: [p for p, _, _ in trainset_layout(stored, fmt, short)]  # noqa: E731
    assert lay('yolo-tree') == ['images/train/1.bmp', 'labels/train/1.txt']
    assert lay('json') == ['train/1.bmp', 'train/1.json']
    assert lay('coco') == ['train/1.bmp', 'train/_annotations.coco.json']
    assert lay('all') == [p for p, _, _ in stored]
    assert lay('yolo-tree', False) == ['images/train/SES/TX/images_/1.bmp', 'labels/train/SES/TX/images_/1.txt']


def test_eval_selection_with_two_json_labels_differing_in_case():
    from fw.contract import eval_selection
    d = 'a' * 64
    files, images, problems = eval_selection([('x/1.png', d, 1), ('x/1.json', d, 1), ('x/1.JSON', d, 1)], False)
    assert problems and 'JSON' in problems[0]


def test_the_flat_layout_keeps_train_negatives_apart():
    from fw.contract import EMPTY_SHA256, trainset_flat, trainset_layout
    stored = [('classes.txt', 'c', 9), ('trainset.json', 't', 9), ('coco/train.json', 'ct', 9),
              ('images/train/SES/TX/train/images/1.bmp', 'i1', 100),
              ('labels/train/SES/TX/train/images/1.txt', 'l1', 40),
              ('json/train/SES/TX/train/images/1.json', 'j1', 90),
              ('images/train/SES/TX/neg_train/images/2.bmp', 'i2', 100),  # no label at all
              ('images/train/SES/TX/a/b/3.bmp', 'i3', 100), ('labels/train/SES/TX/a/b/3.txt', EMPTY_SHA256, 0),
              ('images/train/SES/TX/4.bmp', 'i4', 100), ('labels/train/SES/TX/4.txt', 'l4', 8),  # too short for a box
              ('images/val/SES/TX/val/images/5.bmp', 'i5', 100), ('labels/val/SES/TX/val/images/5.txt', 'l5', 40),
              ('images/val/SES/TX/neg_val/images/6.bmp', 'i6', 100),
              ('images/test/SES/TX/field/7.bmp', 'i7', 100), ('labels/test/SES/TX/field/7.txt', 'l7', 1)]
    rows, problems = trainset_flat(stored)
    assert problems == []
    assert {r[0]: (r[1], r[3]) for r in rows} == {
        'classes.txt': ('c', 'classes.txt'), 'trainset.json': ('t', 'trainset.json'),
        'train/images/1.bmp': ('i1', 'images/train/SES/TX/train/images/1.bmp'),
        'train/labels/1.txt': ('l1', 'labels/train/SES/TX/train/images/1.txt'),
        'neg_train/2.bmp': ('i2', 'images/train/SES/TX/neg_train/images/2.bmp'),
        'neg_train/3.bmp': ('i3', 'images/train/SES/TX/a/b/3.bmp'),
        'neg_train/4.bmp': ('i4', 'images/train/SES/TX/4.bmp'),
        'val/images/5.bmp': ('i5', 'images/val/SES/TX/val/images/5.bmp'),
        'val/labels/5.txt': ('l5', 'labels/val/SES/TX/val/images/5.txt'),
        'val/images/6.bmp': ('i6', 'images/val/SES/TX/neg_val/images/6.bmp'),
        'val/labels/6.txt': (EMPTY_SHA256, None),  # made: a val image without a label gets an empty one
        'test/images/7.bmp': ('i7', 'images/test/SES/TX/field/7.bmp'),
        'test/labels/7.txt': ('l7', 'labels/test/SES/TX/field/7.txt')}  # a stored label stays, whatever it holds
    assert trainset_layout(stored, 'yolo') == [r[:3] for r in rows]
    assert trainset_layout(stored, 'yolo', short=True) == [r[:3] for r in rows]  # the same for every snapshot
    with pytest.raises(ContractError, match='不认识的路径'):
        trainset_flat([('images/other/x.png', 'x', 1)])


def test_names_that_collide_when_flat_are_listed():
    from fw.contract import trainset_flat, trainset_layout
    stored = [('images/val/SES/TX/a/1.bmp', 'i1', 1), ('images/val/SES/TX/b/1.bmp', 'i2', 1),  # one file name
              ('images/test/SES/TX/2.png', 'i3', 1), ('images/test/SES/TX/x/2.bmp', 'i4', 1),  # one label for both
              ('images/train/SES/TX/n/Ab.png', 'i5', 1), ('images/train/SES/TX/m/aB.png', 'i6', 1),  # one on Windows
              ('images/train/SES/TX/n/1.bmp', 'i7', 1),  # neg_train/1.bmp: another folder than val/images/1.bmp
              ('images/train/SES/TX/p/3.png', 'i8', 1), ('images/train/SES/TX/q/3.bmp', 'i9', 1)]  # no labels there
    rows, problems = trainset_flat(stored)
    assert problems == [
        'images/test/SES/TX/2.png、images/test/SES/TX/x/2.bmp 平铺后同名不同扩展名，会共用一份标注',
        'images/train/SES/TX/m/aB.png、images/train/SES/TX/n/Ab.png 平铺后是同一个文件',
        'images/val/SES/TX/a/1.bmp、images/val/SES/TX/b/1.bmp 平铺后是同一个文件']
    with pytest.raises(ContractError, match='yolo-tree') as e:
        trainset_layout(stored, 'yolo')
    assert 'images/val/SES/TX/a/1.bmp、images/val/SES/TX/b/1.bmp' in str(e.value)
    assert len(trainset_layout(stored, 'yolo-tree')) == len(stored)  # the folders are kept there
