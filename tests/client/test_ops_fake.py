"""Client behaviour against an in-memory server: import, get, export and verify (also on Windows / Python 3.8)."""
import os
import stat
import sys
from pathlib import Path

import pytest

from helpers import read_tree, write
from fakeapi import FakeApi
from fw import cache as fw_cache
from fw import ops
from fw.cache import Cache
from fw.layout import Layout, hash_file


@pytest.fixture
def api():
    a = FakeApi()
    a.create_dataset('SES-TX')
    return a


@pytest.fixture
def cache(tmp_path):
    c = Cache(tmp_path / 'cache')
    yield c
    c.close()


def make_folder(root, files):
    for rel, data in files.items():
        write(root, rel, data)
    return root


def quiet(_):
    pass


def test_import_get_round_trip(tmp_path, api, cache):
    files = {'a.bmp': b'A' * 100, 'a.json': b'{}', 'sub/b.png': b'B', 'sub/b.json': b'{}', '中文/图.bmp': b'C',
             '中文/图.json': b'{"x": 1}', 'empty.bmp': b'', 'empty.json': b''}
    src = make_folder(tmp_path / 'v1', files)
    v1 = ops.import_folder(api, 'SES-TX', src, echo=quiet)
    assert v1['ref'] == 'SES-TX@1' and v1['file_count'] == len(files)
    out = tmp_path / 'out'
    ops.get(api, cache, 'SES-TX@1', out, echo=quiet)
    assert read_tree(out) == files
    # an unchanged folder makes no new version
    assert ops.import_folder(api, 'SES-TX', src, echo=quiet)['ref'] == 'SES-TX@1'
    assert len(api.datasets['SES-TX']) == 1


def test_copy_rename_version_and_cold_fetch(tmp_path, api, cache):
    v1 = {'a.bmp': b'1', 'a.json': b'j1', 'b.bmp': b'2', 'b.json': b'j2'}
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', v1), echo=quiet)
    v2 = {'a.bmp': b'1', 'a.json': b'j1', 'copy/a.bmp': b'1', 'copy/a.json': b'j1', 'renamed.bmp': b'2',
          'renamed.json': b'j2', 'a2.json': b'new'}
    lines = []
    version = ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v2', v2), echo=lines.append)
    assert version['ref'] == 'SES-TX@2' and version['parent']['ref'] == 'SES-TX@1'
    assert any('改名 2' in line for line in lines)
    for n, files in ((1, v1), (2, v2)):
        cold = Cache(tmp_path / f'cold{n}')
        try:
            ops.get(api, cold, f'SES-TX@{n}', tmp_path / f'out{n}', echo=quiet)
        finally:
            cold.close()
        assert read_tree(tmp_path / f'out{n}') == files


def test_get_needs_an_empty_folder_and_subpath(tmp_path, api, cache):
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', {'a/x.bmp': b'1', 'b/y.bmp': b'2'}),
                      allow_other_files=True, echo=quiet)
    write(tmp_path / 'busy', 'keep.txt', b'k')
    with pytest.raises(ops.FwError):
        ops.get(api, cache, 'SES-TX', tmp_path / 'busy', echo=quiet)
    ops.get(api, cache, 'SES-TX', tmp_path / 'part', subpath='a', echo=quiet)
    assert read_tree(tmp_path / 'part') == {'a/x.bmp': b'1'}


def test_blocking_checks_stop_before_upload(tmp_path, api):
    src = make_folder(tmp_path / 'v1', {'a.bmp': b'1', 'a.json': b'{}', 'report.xlsx': b'x'})
    with pytest.raises(ops.FwError):
        ops.import_folder(api, 'SES-TX', src, echo=quiet)
    assert api.uploads == 0 and not api.datasets['SES-TX']


def test_reimport_that_brings_back_lost_files_flushes_before_reporting(tmp_path, api):
    src = make_folder(tmp_path / 'v1', {'a.bmp': b'1', 'a.json': b'{}'})
    v = ops.import_folder(api, 'SES-TX', src, echo=quiet)
    del api.objects[hash_file(src / 'a.bmp')]  # the server lost it, or verify moved it to quarantine
    api.events.clear()
    lines = []
    assert ops.import_folder(api, 'SES-TX', src, echo=lines.append)['id'] == v['id']
    assert api.events == ['put', 'flush']
    assert any('补传了 1 个' in line for line in lines)


def test_confirmation_and_dry_run(tmp_path, api):
    src = make_folder(tmp_path / 'v1', {'a.bmp': b'1', 'a.json': b'{}'})
    assert ops.import_folder(api, 'SES-TX', src, dry_run=True, echo=quiet) is None
    assert ops.import_folder(api, 'SES-TX', src, confirm=lambda prompt: False, echo=quiet) is None
    assert api.uploads == 0 and not api.datasets['SES-TX']
    asked = []
    v = ops.import_folder(api, 'SES-TX', src, confirm=lambda prompt: asked.append(prompt) or True, echo=quiet)
    assert v['ref'] == 'SES-TX@1' and asked == ['确认建 SES-TX@1 吗？']


def test_editing_a_fetched_folder_leaves_the_cache_alone(tmp_path, api, cache):
    """Acceptance: a training script rewriting files in place must not change the cache."""
    files = {'a.bmp': b'A' * 1000, 'a.json': b'{"shapes": []}'}
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', files), echo=quiet)
    out = tmp_path / 'out'
    ops.get(api, cache, 'SES-TX@1', out, echo=quiet)
    target = out / 'a.json'
    st = target.stat()
    with open(target, 'r+b') as f:  # same size, same mtime: only the content changes
        f.write(b'{"shapes": [1]')
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    downloads = api.downloads
    ops.get(api, cache, 'SES-TX@1', tmp_path / 'again', echo=quiet)
    assert read_tree(tmp_path / 'again') == files
    assert api.downloads == downloads  # the cache was never touched, nothing had to be fetched again


def test_damaged_cache_object_is_caught_and_downloaded_again(tmp_path, api, cache):
    files = {'a.bmp': b'A' * 1000, 'a.json': b'{}'}
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', files), echo=quiet)
    ops.get(api, cache, 'SES-TX@1', tmp_path / 'out1', echo=quiet)
    obj = cache.object_path(hash_file(tmp_path / 'v1' / 'a.bmp'))
    st = obj.stat()
    os.chmod(obj, stat.S_IREAD | stat.S_IWRITE)
    with open(obj, 'r+b') as f:
        f.write(b'Z')
    os.utime(obj, ns=(st.st_atime_ns, st.st_mtime_ns))  # size and mtime say nothing changed
    lines = []
    ops.get(api, cache, 'SES-TX@1', tmp_path / 'out2', echo=lines.append)
    assert read_tree(tmp_path / 'out2') == files
    assert any('重新下载' in line for line in lines)


def test_download_with_a_wrong_hash_never_enters_the_cache(tmp_path, api, cache):
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', {'a.bmp': b'abc', 'a.json': b'{}'}), echo=quiet)
    digest = hash_file(tmp_path / 'v1' / 'a.bmp')
    api.corrupt_next_download.add(digest)
    with pytest.raises(Exception):
        cache.fetch(api, digest)
    assert cache.cached(digest) is None and not cache.object_path(digest).exists()


@pytest.mark.skipif(sys.platform == 'win32', reason='hardlinks are a Linux option')
def test_hardlink_mode_is_read_only_and_checked(tmp_path, api, cache):
    files = {'a.bmp': b'A' * 10, 'a.json': b'{}'}
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', files), echo=quiet)
    out = tmp_path / 'out'
    ops.get(api, cache, 'SES-TX@1', out, link='hardlink', echo=quiet)
    st = (out / 'a.bmp').stat()
    assert st.st_nlink == 2 and stat.S_IMODE(st.st_mode) & 0o222 == 0
    # root ignores read-only: an in-place write changes the cache too, and the next fetch notices
    os.chmod(out / 'a.bmp', 0o644)
    with open(out / 'a.bmp', 'r+b') as f:
        f.write(b'Z')
    ops.get(api, cache, 'SES-TX@1', tmp_path / 'again', echo=quiet)
    assert read_tree(tmp_path / 'again') == files


@pytest.mark.skipif(sys.platform == 'win32', reason='hardlinks are a Linux option')
def test_clearing_the_cache_leaves_hardlinked_files_read_only(tmp_path, api, cache):
    files = {'a.bmp': b'A' * 10, 'a.json': b'{}'}
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', files), echo=quiet)
    for n in (1, 2):
        ops.get(api, cache, 'SES-TX@1', tmp_path / f'out{n}', link='hardlink', echo=quiet)
    cache.clear()
    for n in (1, 2):
        for rel in files:
            st = (tmp_path / f'out{n}' / rel).stat()
            assert stat.S_IMODE(st.st_mode) & 0o222 == 0, f'out{n}/{rel} became writable'
        assert read_tree(tmp_path / f'out{n}') == files


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows only')
def test_hardlink_is_refused_on_windows(tmp_path, api, cache):
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', {'a.bmp': b'1', 'a.json': b'{}'}), echo=quiet)
    with pytest.raises(ops.FwError, match='hardlink'):
        ops.get(api, cache, 'SES-TX@1', tmp_path / 'out', link='hardlink', echo=quiet)
    assert not (tmp_path / 'out').exists()


def test_a_second_client_installing_the_same_object_never_hides_it(tmp_path, api, monkeypatch):
    """Two fw processes share one cache. While B installs an object A already has (their downloads raced),
    A must still find the file."""
    data = b'A' * 100
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', {'a.bmp': data, 'a.json': b'{}'}), echo=quiet)
    digest = hash_file(tmp_path / 'v1' / 'a.bmp')
    a, b = Cache(tmp_path / 'shared'), Cache(tmp_path / 'shared')
    try:
        path = a.fetch(api, digest)
        monkeypatch.setattr(b, 'cached', lambda d: None)  # B looked before A's copy was there
        real_replace = os.replace
        seen = []

        def replace(src, dst):
            seen.append(Path(dst).read_bytes())  # what A reads at this moment
            return real_replace(src, dst)

        monkeypatch.setattr(os, 'replace', replace)
        assert b.fetch(api, digest) == path
        assert seen and all(s == data for s in seen)
        assert path.read_bytes() == data and a.cached(digest) == path
        assert stat.S_IMODE(path.stat().st_mode) & 0o222 == 0
    finally:
        a.close()
        b.close()


def test_a_second_client_installing_a_batch_never_hides_an_object(tmp_path, api, monkeypatch):
    """The same for objects that arrive in a batch download."""
    data = b'B' * 100
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', {'b.bmp': data, 'b.json': b'{}'}), echo=quiet)
    digest = hash_file(tmp_path / 'v1' / 'b.bmp')
    a, b = Cache(tmp_path / 'shared'), Cache(tmp_path / 'shared')
    try:
        path = a.fetch_many(api, [(digest, len(data))])[digest]
        monkeypatch.setattr(b, 'cached_many', lambda digests: {})
        real_replace = os.replace
        seen = []

        def replace(src, dst):
            seen.append(Path(dst).read_bytes())
            return real_replace(src, dst)

        monkeypatch.setattr(os, 'replace', replace)
        assert b.fetch_many(api, [(digest, len(data))])[digest] == path
        assert seen and all(s == data for s in seen) and path.read_bytes() == data
    finally:
        a.close()
        b.close()


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _StaleRead:
    """A cache's index connection that lets another fw process run just after this one has read its rows."""

    def __init__(self, db, query, then):
        self._db, self._query, self._then = db, query, then
        self.fired = False

    def execute(self, sql, *args):
        if self.fired or not sql.startswith(self._query):
            return self._db.execute(sql, *args)
        rows = self._db.execute(sql, *args).fetchall()
        self.fired = True
        self._then()
        return _Rows(rows)

    def __getattr__(self, name):
        return getattr(self._db, name)


def _out_of_date(cache, digest):
    """The cached file no longer matches its index row (some other program touched it), so the next fetch
    downloads it again. The row is moved an hour back: no copy installed from now on can match it, even where
    two installs land in the same tick of the file system's clock."""
    with cache._lock:
        cache._db.execute('UPDATE objects SET mtime_ns = mtime_ns - ? WHERE sha256 = ?', (3600 * 10 ** 9, digest))


def test_a_stale_index_row_never_deletes_a_fresh_copy(tmp_path, api, monkeypatch):
    """Two fw processes share one cache. B reads an object's index row; before B looks at the file, A finds
    the object out of date and installs a fresh copy (on Windows: keeps the intact one and records it). B's
    row no longer describes the file, and B must not delete A's copy because of it."""
    data = b'A' * 100
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', {'a.bmp': data, 'a.json': b'{}'}), echo=quiet)
    digest = hash_file(tmp_path / 'v1' / 'a.bmp')
    a, b = Cache(tmp_path / 'shared'), Cache(tmp_path / 'shared')
    try:
        path = a.fetch(api, digest)
        _out_of_date(a, digest)
        fresh = []
        monkeypatch.setattr(b, '_db', _StaleRead(b._db, 'SELECT size', lambda: fresh.append(a.fetch(api, digest))))
        assert b.cached(digest) is None  # the row B read is out of date by now: B would download it again
        assert fresh == [path]
        assert path.read_bytes() == data and a.cached(digest) == path
        assert b.fetch(api, digest) == path and path.read_bytes() == data
    finally:
        a.close()
        b.close()


def test_a_stale_index_row_never_deletes_a_fresh_copy_in_a_batch(tmp_path, api, monkeypatch):
    """The same for the lookup batch downloads use."""
    data = b'B' * 100
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', {'b.bmp': data, 'b.json': b'{}'}), echo=quiet)
    digest = hash_file(tmp_path / 'v1' / 'b.bmp')
    objects = [(digest, len(data))]
    a, b = Cache(tmp_path / 'shared'), Cache(tmp_path / 'shared')
    try:
        path = a.fetch_many(api, objects)[digest]
        _out_of_date(a, digest)
        fresh = []
        monkeypatch.setattr(b, '_db', _StaleRead(b._db, 'SELECT sha256, size, mtime_ns',
                                                 lambda: fresh.append(a.fetch_many(api, objects)[digest])))
        assert b.cached_many([digest]) == {}
        assert fresh == [path]
        assert path.read_bytes() == data and a.cached_many([digest]) == {digest: path}
        assert b.fetch_many(api, objects)[digest] == path and path.read_bytes() == data
    finally:
        a.close()
        b.close()


def test_copy_layout_is_independent(tmp_path):
    src = write(tmp_path, 'src', b'hello')
    layout = Layout('copy')
    assert layout.place(src, tmp_path / 'dst') == hash_file(src)
    assert (tmp_path / 'dst').stat().st_ino != src.stat().st_ino
    assert layout.describe() == '复制'


def test_export_and_offline_verify(tmp_path, api, cache):
    files = {'a.bmp': b'1', 'a.json': b'{}', 'd/b.bmp': b'2', 'd/b.json': b'{}'}
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', files), echo=quiet)
    out = tmp_path / 'export'
    ops.export(api, cache, 'SES-TX@1', out, echo=quiet)
    assert read_tree(out / 'data') == files
    assert ops.verify(out, echo=quiet)
    (out / 'data' / 'a.json').write_bytes(b'{"x":1}')
    (out / 'data' / 'extra.txt').write_bytes(b'x')
    (out / 'data' / 'd' / 'b.bmp').unlink()
    lines = []
    assert not ops.verify(out, echo=lines.append)
    text = '\n'.join(lines)
    assert '缺少的文件：1' in text and '多出来的文件：1' in text and '内容不一致的文件：1' in text


def test_someone_else_added_a_version_meanwhile(tmp_path, api):
    src = make_folder(tmp_path / 'v1', {'a.bmp': b'1', 'a.json': b'{}'})

    def sneak(prompt):
        api.datasets['SES-TX'].append(dict(id=99, number=1, ref='SES-TX@1', manifest_sha256='0' * 64))
        return True

    with pytest.raises(Exception, match='变化'):
        ops.import_folder(api, 'SES-TX', src, confirm=sneak, echo=quiet)


def test_small_files_travel_in_batches_and_large_ones_alone(tmp_path, api, cache, monkeypatch):
    monkeypatch.setattr(ops, 'UPLOAD_COUNT', 3)
    monkeypatch.setattr(fw_cache, 'FETCH_COUNT', 3)
    monkeypatch.setattr(ops, 'BATCH_OBJECT_MAX', 500)
    monkeypatch.setattr(fw_cache, 'BATCH_OBJECT_MAX', 500)
    files = {f'{i}.bmp': bytes([i]) * 10 for i in range(5)}
    files.update({f'{i}.json': b'{"i": %d}' % i for i in range(5)})
    files.update({'big.bmp': b'B' * 1000, 'big.json': b'{}'})
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', files), echo=quiet)
    assert (api.batch_uploads, api.uploads) == (4, 12)  # 11 small files in batches of 3, the big one alone
    ops.get(api, cache, 'SES-TX@1', tmp_path / 'out', echo=quiet)
    assert read_tree(tmp_path / 'out') == files
    assert (api.batch_downloads, api.downloads) == (4, 12)


def test_bad_object_in_a_batch_download(tmp_path, api, cache):
    files = {f'{i}.bmp': bytes([i]) * 50 for i in range(6)}
    ops.import_folder(api, 'SES-TX', make_folder(tmp_path / 'v1', files), echo=quiet)
    digests = sorted(hash_file(tmp_path / 'v1' / rel) for rel in files)
    bad = digests[3]
    api.corrupt_next_download.add(bad)
    with pytest.raises(Exception, match='哈希对不上'):
        ops.get(api, cache, 'SES-TX@1', tmp_path / 'out1', workers=1, echo=quiet)
    assert cache.cached(bad) is None and not cache.object_path(bad).exists()
    assert all(cache.cached(d) for d in digests[:3])  # objects that arrived intact before it are kept
    before = api.downloads
    ops.get(api, cache, 'SES-TX@1', tmp_path / 'out2', echo=quiet)
    assert read_tree(tmp_path / 'out2') == files
    assert api.downloads - before == 3


@pytest.mark.parametrize('new_content, message', [(b'abcd', '改过'), (b'xyz', '哈希')])
def test_file_changed_after_hashing_is_not_uploaded(tmp_path, api, new_content, message):
    src = make_folder(tmp_path / 'v1', {'a.bmp': b'abc', 'a.json': b'{}'})

    def change_then_confirm(prompt):
        (src / 'a.bmp').write_bytes(new_content)
        return True

    with pytest.raises(Exception, match=message):
        ops.import_folder(api, 'SES-TX', src, confirm=change_then_confirm, echo=quiet)
    assert api.datasets['SES-TX'] == []


def test_import_timing_leaves_out_the_time_spent_confirming(tmp_path, api, monkeypatch):
    """The person reads the preview and answers y: those seconds are not the upload's."""
    import re
    clock = [100.0]
    monkeypatch.setattr(ops.time, 'perf_counter', lambda: clock[0])
    src = make_folder(tmp_path / 'src', {'a.png': b'a', 'a.json': b'{}'})
    lines = []

    def answer_slowly(prompt):
        clock[0] += 60  # a minute reading the preview
        return True

    ops.import_folder(api, 'SES-TX', src, confirm=answer_slowly, echo=lines.append)
    timing = next(line for line in lines if '用时' in line)
    assert float(re.search(r'上传 ([0-9.]+) 秒', timing).group(1)) < 1, timing
    assert '不含等你确认的时间' in timing
