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
