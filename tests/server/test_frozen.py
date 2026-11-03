"""The database itself keeps frozen versions frozen (acceptance: adding rows after the freeze fails)."""
import hashlib

import psycopg
import pytest

from helpers import write
from fw import ops
from fw.contract import manifest_bytes

H = hashlib.sha256(b'x').digest()


def quiet(_):
    pass


@pytest.fixture
def frozen(api, dataset, tmp_path):
    write(tmp_path / 'v1', 'a.bmp', b'1')
    write(tmp_path / 'v1', 'a.json', b'{}')
    return ops.import_folder(api, dataset, tmp_path / 'v1', echo=quiet)['id']


@pytest.mark.parametrize('statement', [
    "INSERT INTO version_files VALUES (%(v)s, 'new.bmp', %(h)s, 1)",
    "UPDATE version_files SET size = size + 1 WHERE version_id = %(v)s",
    "UPDATE version_files SET sha256 = %(h)s WHERE version_id = %(v)s",
    "DELETE FROM version_files WHERE version_id = %(v)s",
    "UPDATE versions SET note = 'changed' WHERE id = %(v)s",
    "UPDATE versions SET frozen = false WHERE id = %(v)s",
    "DELETE FROM versions WHERE id = %(v)s",
    "TRUNCATE version_files",
    "TRUNCATE versions CASCADE",
])
def test_frozen_version_cannot_change(sql, frozen, statement):
    before = sql.execute('SELECT count(*) AS n FROM version_files WHERE version_id = %s', (frozen,)).fetchone()['n']
    with pytest.raises(psycopg.Error):
        with sql.transaction():
            sql.execute(statement, {'v': frozen, 'h': H})
    after = sql.execute('SELECT count(*) AS n FROM version_files WHERE version_id = %s', (frozen,)).fetchone()['n']
    assert before == after == 2


def _new_version(sql, frozen, number, entries, manifest_entries=None):
    data = manifest_bytes(manifest_entries if manifest_entries is not None else entries)
    ds = sql.execute('SELECT dataset_id, created_by FROM versions WHERE id = %s', (frozen,)).fetchone()
    vid = sql.execute(
        'INSERT INTO versions (dataset_id, number, manifest_sha256, file_count, total_bytes, created_by) '
        'VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
        (ds['dataset_id'], number, hashlib.sha256(data).digest(), len(entries), sum(e[2] for e in entries),
         ds['created_by'])).fetchone()['id']
    for p, d, s in entries:
        sql.execute('INSERT INTO version_files VALUES (%s, %s, %s, %s)', (vid, p, bytes.fromhex(d), s))
    return vid


ENTRIES = [('Z.bmp', 'a' * 64, 3), ('a.bmp', 'b' * 64, 0), ('中文/图.json', 'c' * 64, 12), ('a/b c.json', 'd' * 64, 1)]


class Rollback(Exception):
    pass


def test_database_rebuilds_the_manifest_hash_before_freezing(sql, frozen):
    with pytest.raises(Rollback):  # the fake objects are not in the store, so the version is not kept
        with sql.transaction():
            vid = _new_version(sql, frozen, 50, ENTRIES)
            sql.execute('UPDATE versions SET frozen = true WHERE id = %s', (vid,))  # same bytes as Python's manifest
            assert sql.execute('SELECT frozen FROM versions WHERE id = %s', (vid,)).fetchone()['frozen']
            raise Rollback


def test_rows_that_differ_from_the_manifest_cannot_freeze(sql, frozen):
    with pytest.raises(psycopg.Error, match='do not match'):
        with sql.transaction():
            vid = _new_version(sql, frozen, 51, ENTRIES[:3], manifest_entries=ENTRIES)
            sql.execute('UPDATE versions SET file_count = 3, total_bytes = 15 WHERE id = %s', (vid,))
            sql.execute('UPDATE versions SET frozen = true WHERE id = %s', (vid,))


def test_unfrozen_version_cannot_commit(sql, frozen):
    with pytest.raises(psycopg.Error, match='frozen before'):
        with sql.transaction():
            _new_version(sql, frozen, 52, ENTRIES)


def test_version_cannot_be_inserted_frozen(sql, frozen):
    ds = sql.execute('SELECT dataset_id, created_by FROM versions WHERE id = %s', (frozen,)).fetchone()
    with pytest.raises(psycopg.Error, match='inserted unfrozen'):
        with sql.transaction():
            sql.execute('INSERT INTO versions (dataset_id, number, frozen, manifest_sha256, file_count, total_bytes, '
                        "created_by) VALUES (%s, 53, true, %s, 0, 0, %s)", (ds['dataset_id'], H, ds['created_by']))
