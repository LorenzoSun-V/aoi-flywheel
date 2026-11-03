import os
import sys

import pytest

from helpers import write
from fw.checks import scan_folder


def test_report_and_blocking(tmp_path):
    for rel in ['a.bmp', 'a.json', 'b.png', 'sub/c.jpg', 'sub/c.txt', 'orphan.json', 'classes.txt',
                'notes.docx', 'Thumbs.db', 'x/a.bmp', 'x/a.json']:
        write(tmp_path, rel, rel.encode())
    r = scan_folder(tmp_path)
    assert r.junk == ['Thumbs.db']
    assert sorted(r.images) == ['a.bmp', 'b.png', 'sub/c.jpg', 'x/a.bmp']
    assert r.meta == ['classes.txt']
    assert r.others == ['notes.docx']
    assert r.unlabeled_images == ['b']
    assert r.orphan_labels == ['orphan']
    assert r.same_name_in_dirs == [['a.bmp', 'x/a.bmp']]
    assert r.blocking(False, False)  # notes.docx
    assert r.blocking(True, False) == []
    assert 'Thumbs.db' not in [rel for rel, _ in r.files]


def test_same_stem_images_need_force(tmp_path):
    for rel in ['a.bmp', 'a.png', 'a.json']:
        write(tmp_path, rel, b'x')
    r = scan_folder(tmp_path)
    assert r.same_stem_images == [['a.bmp', 'a.png']]
    assert r.blocking(False, False)
    assert r.blocking(False, True) == []


@pytest.mark.skipif(sys.platform != 'linux', reason='needs a case-sensitive file system')
def test_case_twins_and_bad_names_always_block(tmp_path):
    for rel in ['A.json', 'a.json', 'a.bmp', 'b:c.bmp', 'CON.json', 'dir /d.bmp']:
        write(tmp_path, rel, b'x')
    r = scan_folder(tmp_path)
    assert r.collisions and len(r.bad_paths) == 3
    assert r.blocking(True, True)


@pytest.mark.skipif(not hasattr(os, 'symlink') or sys.platform == 'win32', reason='symlinks')
def test_symlinks_are_refused(tmp_path):
    write(tmp_path, 'a.bmp', b'x')
    os.symlink(tmp_path / 'a.bmp', tmp_path / 'b.bmp')
    r = scan_folder(tmp_path)
    assert any('链接' in p for p in r.bad_paths)
    assert r.blocking(True, True)
