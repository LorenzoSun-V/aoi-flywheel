import os
import subprocess
import sys
from pathlib import Path

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


def test_yolo_labels_pair_with_images(tmp_path):
    # tx_v8/images/1.bmp + tx_v8/labels/1.txt: the import used to call both unpaired
    for rel in ['images/1.bmp', 'labels/1.txt', 'images/2.bmp', 'labels/3.txt', 'b1/images/x/4.png',
                'b1/labels/x/4.txt', 'images/5.png', 'labels/5.json']:
        write(tmp_path, rel, rel.encode())
    r = scan_folder(tmp_path)
    assert r.unlabeled_images == ['images/2', 'images/5']  # YOLO only reads .txt from labels/
    assert r.orphan_labels == ['labels/3', 'labels/5']


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
    assert r.links == ['b.bmp：是链接（指向文件），先换成普通的文件']
    assert r.blocking(True, True)


def two_folders(tmp_path):
    for rel in ('a/x.bmp', 'a/x.json', 'b/y.bmp', 'b/y.json'):
        write(tmp_path, rel, b'x')


def test_a_folder_that_cannot_be_read_blocks(tmp_path, monkeypatch):
    """A folder the scan cannot open would otherwise look like deleted files in the new version."""
    two_folders(tmp_path)
    real = os.scandir

    def scandir(path='.'):
        if Path(path) == tmp_path / 'b':
            raise PermissionError(13, 'Permission denied', str(path))
        return real(path)

    monkeypatch.setattr(os, 'scandir', scandir)
    r = scan_folder(tmp_path)
    assert [rel for rel, _ in r.files] == ['a/x.bmp', 'a/x.json']
    assert r.unreadable == ['b/：读不了（Permission denied）']
    assert r.blocking(True, True)


@pytest.mark.skipif(sys.platform == 'win32' or os.geteuid() == 0, reason='needs POSIX permissions, not as root')
def test_a_folder_without_permission_blocks(tmp_path):
    two_folders(tmp_path)
    os.chmod(tmp_path / 'b', 0)
    try:
        r = scan_folder(tmp_path)
    finally:
        os.chmod(tmp_path / 'b', 0o755)
    assert [rel for rel, _ in r.files] == ['a/x.bmp', 'a/x.json']
    assert len(r.unreadable) == 1 and r.unreadable[0].startswith('b/：')
    assert r.blocking(True, True)


@pytest.mark.skipif(not hasattr(os, 'symlink') or sys.platform == 'win32', reason='symlinks')
def test_folder_links_are_refused_not_skipped(tmp_path):
    two_folders(tmp_path)
    os.symlink(tmp_path / 'b', tmp_path / 'c', target_is_directory=True)
    os.symlink(tmp_path / 'gone', tmp_path / 'dangling')
    r = scan_folder(tmp_path)
    assert [rel for rel, _ in r.files] == ['a/x.bmp', 'a/x.json', 'b/y.bmp', 'b/y.json']
    assert r.links == ['c：是链接（指向目录），先换成普通的目录', 'dangling：是链接（指向文件），先换成普通的文件']
    assert r.blocking(True, True)


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='named pipes')
def test_special_files_are_refused(tmp_path):
    two_folders(tmp_path)
    os.mkfifo(tmp_path / 'a' / 'pipe')
    r = scan_folder(tmp_path)
    assert r.links == ['a/pipe：是特殊文件，先换成普通文件'] and r.blocking(True, True)


@pytest.mark.skipif(sys.platform != 'win32', reason='directory junctions are a Windows thing')
def test_junctions_are_refused(tmp_path):
    two_folders(tmp_path)
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(tmp_path / 'c'), str(tmp_path / 'b')], check=True,
                   stdout=subprocess.DEVNULL)
    assert (tmp_path / 'c' / 'y.bmp').is_file()  # the junction works, so os.walk would have gone through it
    r = scan_folder(tmp_path)
    assert [rel for rel, _ in r.files] == ['a/x.bmp', 'a/x.json', 'b/y.bmp', 'b/y.json']
    assert r.links == ['c：是链接（指向目录），先换成普通的目录']
    assert r.blocking(True, True)
