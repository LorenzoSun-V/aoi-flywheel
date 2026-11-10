"""Laying out from several threads: the mode found by the first files holds for the rest."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from fw import layout
from fw.layout import Layout


def _files(tmp_path, n):
    src = tmp_path / 'cache'
    src.mkdir()
    out = []
    for i in range(n):
        p = src / f'o{i}'
        p.write_bytes(os.urandom(64))
        os.chmod(p, 0o400)
        out.append(p)
    (tmp_path / 'dest').mkdir()
    return out


def test_auto_never_hardlinks_two_threads(tmp_path, monkeypatch):
    """Two threads both fail the first reflink, then take the lock one after the other."""
    srcs = _files(tmp_path, 4)
    gate = threading.Barrier(2)
    monkeypatch.setattr(layout, 'reflink', lambda s, d: gate.wait() is None and False)
    lay = Layout('auto')
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(lambda i: lay.place(srcs[i], tmp_path / 'dest' / f'f{i}'), range(2)))
    monkeypatch.setattr(layout, 'reflink', lambda s, d: False)
    for i in range(2, 4):
        lay.place(srcs[i], tmp_path / 'dest' / f'f{i}')
    links = [i for i in range(4) if os.stat(tmp_path / 'dest' / f'f{i}').st_nlink > 1]
    assert lay.mode == 'copy' and not links, (lay.mode, links, lay.used)


def test_auto_never_hardlinks_real_fs(tmp_path):
    """No patching: 8 threads, like _lay_out, on a file system without reflink."""
    srcs = _files(tmp_path, 400)
    lay = Layout('auto')
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda i: lay.place(srcs[i], tmp_path / 'dest' / f'f{i}'), range(len(srcs))))
    links = sum(os.stat(tmp_path / 'dest' / f'f{i}').st_nlink > 1 for i in range(len(srcs)))
    assert links == 0, (lay.mode, links, lay.used)


def test_job_mode_links_from_every_thread(tmp_path, monkeypatch):
    """A worker's job folder: without reflink every file is a hard link, whichever thread got there first."""
    srcs = _files(tmp_path, 4)
    gate = threading.Barrier(2)
    monkeypatch.setattr(layout, 'reflink', lambda s, d: gate.wait() is None and False)
    lay = Layout('job')
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(lambda i: lay.place(srcs[i], tmp_path / 'dest' / f'f{i}'), range(2)))
    for i in range(2, 4):
        lay.place(srcs[i], tmp_path / 'dest' / f'f{i}')
    assert lay.mode == 'job-hardlink' and lay.used == {'hardlink'}
    assert all(os.stat(tmp_path / 'dest' / f'f{i}').st_nlink == 2 for i in range(4))
