import hashlib

import pytest

from fw.contract import (ContractError, classify_changes, collisions, diff_entries, manifest_bytes, manifest_hash,
                         parse_manifest, path_problem)

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
