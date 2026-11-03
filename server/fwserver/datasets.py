"""Datasets and their frozen versions. Every version is a full manifest; nothing is read through a parent.

Creating a version (create_version):
1. the manifest object must already be in the store and parse as a canonical manifest;
2. every file it lists must be in the store with the listed size (otherwise: refused, nothing written);
3. store.flush(): one syncfs makes all those objects and their directory entries durable;
4. one transaction: lock the dataset row, check that nobody added a version meanwhile, insert the version
   unfrozen, COPY its rows, freeze it. The database recomputes the manifest hash from the rows before it
   lets the version freeze, and refuses to commit an unfrozen version.
A failure anywhere leaves no version behind; objects already uploaded stay and are reused by the next try.
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional

from fw.contract import ContractError, classify_changes, parse_manifest

from .store import HEX64, Store, StoreError, hash_path

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
KINDS = ('pool', 'test', 'regression', 'other')


class DatasetError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def check_digest(digest: str) -> str:
    if not HEX64.match(digest or ''):
        raise DatasetError(400, '哈希应为 64 位小写十六进制')
    return digest


def _stamp(t) -> Optional[str]:
    return t.isoformat(timespec='seconds') if t is not None else None


_VERSION = """
SELECT v.id, d.name AS dataset, v.number, v.parent_id, pd.name AS parent_dataset, p.number AS parent_number,
       encode(v.manifest_sha256, 'hex') AS manifest_sha256, v.file_count, v.total_bytes, v.source, v.note,
       u.name AS created_by, v.created_at
FROM versions v
JOIN datasets d ON d.id = v.dataset_id
JOIN users u ON u.id = v.created_by
LEFT JOIN versions p ON p.id = v.parent_id
LEFT JOIN datasets pd ON pd.id = p.dataset_id
"""


def _version(row: dict) -> dict:
    parent = None
    if row['parent_id'] is not None:
        parent = {'id': row['parent_id'], 'dataset': row['parent_dataset'], 'number': row['parent_number'],
                  'ref': f'{row["parent_dataset"]}@{row["parent_number"]}'}
    return {'id': row['id'], 'dataset': row['dataset'], 'number': row['number'],
            'ref': f'{row["dataset"]}@{row["number"]}', 'parent': parent,
            'manifest_sha256': row['manifest_sha256'], 'file_count': row['file_count'],
            'total_bytes': row['total_bytes'], 'source': row['source'], 'note': row['note'],
            'created_by': row['created_by'], 'created_at': _stamp(row['created_at'])}


_DATASET = """
SELECT d.id, d.name, d.site, d.line, d.kind, d.note, u.name AS created_by, d.created_at,
       (SELECT count(*) FROM versions v WHERE v.dataset_id = d.id) AS version_count,
       (SELECT v.id FROM versions v WHERE v.dataset_id = d.id ORDER BY v.number DESC LIMIT 1) AS latest_id
FROM datasets d JOIN users u ON u.id = d.created_by
"""


def _datasets(conn, rows: List[dict]) -> List[dict]:
    latest_ids = [r['latest_id'] for r in rows if r['latest_id'] is not None]
    latest = {}
    if latest_ids:
        for v in conn.execute(_VERSION + ' WHERE v.id = ANY(%s)', (latest_ids,)).fetchall():
            latest[v['id']] = _version(v)
    return [{'id': r['id'], 'name': r['name'], 'site': r['site'], 'line': r['line'], 'kind': r['kind'],
             'note': r['note'], 'created_by': r['created_by'], 'created_at': _stamp(r['created_at']),
             'version_count': r['version_count'], 'latest': latest.get(r['latest_id'])} for r in rows]


def list_datasets(conn) -> List[dict]:
    return _datasets(conn, conn.execute(_DATASET + ' ORDER BY lower(d.name)').fetchall())


def get_dataset(conn, name: str) -> dict:
    rows = conn.execute(_DATASET + ' WHERE lower(d.name) = lower(%s)', (name,)).fetchall()
    if not rows:
        raise DatasetError(404, f'没有数据集 {name}；先用 fw dataset create 建')
    return _datasets(conn, rows)[0]


def create_dataset(conn, user_id: int, name: str, site: str = '', line: str = '', kind: str = 'pool',
                   note: str = '') -> dict:
    if not NAME.match(name or ''):
        raise DatasetError(400, '数据集名只能用字母、数字和 . _ -，以字母或数字开头，最多 64 个字符')
    if kind not in KINDS:
        raise DatasetError(400, f'类型只能是 {"、".join(KINDS)}')
    with conn.transaction():
        if conn.execute('SELECT 1 FROM datasets WHERE lower(name) = lower(%s)', (name,)).fetchone():
            raise DatasetError(409, f'数据集 {name} 已经存在（名称不分大小写）')
        conn.execute('INSERT INTO datasets (name, site, line, kind, note, created_by) VALUES (%s, %s, %s, %s, %s, %s)',
                     (name, site, line, kind, note, user_id))
    return get_dataset(conn, name)


def list_versions(conn, name: str) -> List[dict]:
    dataset = get_dataset(conn, name)
    rows = conn.execute(_VERSION + ' WHERE v.dataset_id = %s ORDER BY v.number', (dataset['id'],)).fetchall()
    return [_version(r) for r in rows]


def get_version(conn, name: str, number: str) -> dict:
    dataset = get_dataset(conn, name)
    if number == 'latest':
        if dataset['latest'] is None:
            raise DatasetError(404, f'{dataset["name"]} 还没有版本')
        return dataset['latest']
    if not str(number).isdigit():
        raise DatasetError(400, '版本序号应为正整数或 latest')
    row = conn.execute(_VERSION + ' WHERE v.dataset_id = %s AND v.number = %s',
                       (dataset['id'], int(number))).fetchone()
    if row is None:
        raise DatasetError(404, f'没有版本 {dataset["name"]}@{number}')
    return _version(row)


def get_version_by_id(conn, version_id: int) -> dict:
    row = conn.execute(_VERSION + ' WHERE v.id = %s', (version_id,)).fetchone()
    if row is None:
        raise DatasetError(404, f'没有 id 为 {version_id} 的版本')
    return _version(row)


def create_version(conn, store: Store, user_id: int, name: str, manifest_sha256: str, parent_id: Optional[int],
                   expect_latest_id: Optional[int], source: str = '', note: str = '',
                   before_freeze: Optional[Callable[[], None]] = None) -> dict:
    check_digest(manifest_sha256)
    data = store.read(manifest_sha256)
    if data is None:
        raise DatasetError(400, '清单文件还没有上传')
    try:
        entries = parse_manifest(data)
    except ContractError as e:
        raise DatasetError(400, f'清单不合格：{e}') from None
    if not entries:
        raise DatasetError(400, '清单里没有文件')
    bad = [(p, s, store.size(d)) for p, d, s in entries if store.size(d) != s]
    if bad:
        examples = '；'.join(f'{p}（{"没有" if got is None else f"大小 {got}，应为 {s}"}）' for p, s, got in bad[:5])
        raise DatasetError(400, f'有 {len(bad)} 个文件服务器上还没有或大小不对，没有建版本；重新运行导入会补传。例如 {examples}')
    try:
        store.flush()
    except StoreError as e:
        raise DatasetError(503, str(e)) from None
    total = sum(s for _, _, s in entries)
    with conn.transaction():
        dataset = conn.execute('SELECT id, name FROM datasets WHERE lower(name) = lower(%s) FOR UPDATE',
                               (name,)).fetchone()
        if dataset is None:
            raise DatasetError(404, f'没有数据集 {name}')
        latest = conn.execute('SELECT id, number FROM versions WHERE dataset_id = %s ORDER BY number DESC LIMIT 1',
                              (dataset['id'],)).fetchone()
        if (latest['id'] if latest else None) != expect_latest_id:
            now = f'@{latest["number"]}' if latest else '没有版本'
            raise DatasetError(409, f'{dataset["name"]} 在这期间有了变化（最新是 {now}）；请重新运行导入，以最新版本为父版本')
        if parent_id is not None:
            parent = conn.execute("SELECT encode(manifest_sha256, 'hex') AS m FROM versions WHERE id = %s",
                                  (parent_id,)).fetchone()
            if parent is None:
                raise DatasetError(400, f'父版本 {parent_id} 不存在')
            if parent['m'] == manifest_sha256:
                raise DatasetError(409, '内容和父版本完全一样，没有建新版本')
        number = (latest['number'] if latest else 0) + 1
        version_id = conn.execute(
            'INSERT INTO versions (dataset_id, number, parent_id, manifest_sha256, file_count, total_bytes, source, '
            'note, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
            (dataset['id'], number, parent_id, bytes.fromhex(manifest_sha256), len(entries), total, source, note,
             user_id)).fetchone()['id']
        with conn.cursor() as cur:
            with cur.copy('COPY version_files (version_id, path, sha256, size) FROM STDIN (FORMAT BINARY)') as cp:
                cp.set_types(['int8', 'text', 'bytea', 'int8'])
                for path, digest, size in entries:
                    cp.write_row((version_id, path, bytes.fromhex(digest), size))
        if before_freeze is not None:
            before_freeze()  # tests: a failure between writing the rows and freezing
        conn.execute('UPDATE versions SET frozen = true WHERE id = %s', (version_id,))
    version = get_version_by_id(conn, version_id)
    if parent_id is not None:
        d = diff(conn, parent_id, version_id)
        version['changes'] = {k: len(d[k]) for k in ('added', 'modified', 'deleted', 'renamed')}
    return version


def diff(conn, old_id: int, new_id: int) -> dict:
    old = get_version_by_id(conn, old_id)
    new = get_version_by_id(conn, new_id)
    rows = conn.execute(
        'SELECT path, a.sha256 AS old, b.sha256 AS new '
        'FROM (SELECT path, sha256 FROM version_files WHERE version_id = %s) a '
        'FULL OUTER JOIN (SELECT path, sha256 FROM version_files WHERE version_id = %s) b USING (path) '
        'WHERE a.sha256 IS DISTINCT FROM b.sha256', (old_id, new_id)).fetchall()
    added, deleted, modified = {}, {}, []
    for r in rows:
        if r['old'] is None:
            added[r['path']] = bytes(r['new'])
        elif r['new'] is None:
            deleted[r['path']] = bytes(r['old'])
        else:
            modified.append(r['path'])
    result = classify_changes(added, deleted, modified)
    result['old'] = {k: old[k] for k in ('id', 'ref')}
    result['new'] = {k: new[k] for k in ('id', 'ref')}
    return result


def verify(conn, store: Store, rehash: bool = False, repair_from=None, echo=print) -> List[str]:
    """Check that every version can be laid out exactly as frozen. Returns the problems left.

    - each version's rows rebuild its manifest hash, and the manifest object is in the store and intact;
    - every referenced object is in the store with the right size, and with rehash, the right content;
    - with rehash, also every object no version uses yet (left by an import that failed or is still
      running): the next import of the same files reuses whatever the store holds, so a damaged one is
      moved to quarantine/ and that import sends it again. These are not problems of any version.
    Damaged referenced objects are replaced from `repair_from` (another store, e.g. the backup) when it has
    an intact copy; otherwise they are moved to quarantine/ so that the next upload of that file restores
    them. With rehash every object is read back from the disk (not from the page cache, which can still hold
    data a failed write-back never got onto the disk). Repairs and moves are made durable before this
    returns; if that fails the store refuses writes and StoreError is raised.
    """
    if rehash:
        store.sync()  # write back what is pending first, so that the rehash reads what the disk holds
    problems = []
    versions = conn.execute(
        "SELECT v.id, d.name || '@' || v.number AS ref, encode(v.manifest_sha256, 'hex') AS m, "
        "encode(version_manifest_sha256(v.id), 'hex') AS rebuilt "
        "FROM versions v JOIN datasets d ON d.id = v.dataset_id ORDER BY v.id").fetchall()
    for v in versions:
        if v['m'] != v['rebuilt']:
            problems.append(f'{v["ref"]}：清单项和登记的清单哈希对不上')
    wanted = {v['m']: (None, v['ref']) for v in versions}  # digest -> (size if known, a version using it)
    rows = conn.execute(
        "SELECT encode(f.sha256, 'hex') AS d, f.size, min(d.name || '@' || v.number) AS ref "
        'FROM version_files f JOIN versions v ON v.id = f.version_id JOIN datasets d ON d.id = v.dataset_id '
        'GROUP BY f.sha256, f.size').fetchall()
    for r in rows:
        wanted[r['d']] = (r['size'], r['ref'])
    checked = repaired = 0
    for digest, (size, ref) in sorted(wanted.items()):
        checked += 1
        got = store.size(digest)
        ok = got is not None and (size is None or got == size)
        if ok and rehash:
            ok = hash_path(store.path(digest), from_disk=True)[0] == digest
        if ok:
            continue
        if repair_from is not None and store.repair_from(digest, repair_from):
            repaired += 1
            continue
        state = '缺失' if got is None else '已损坏'
        if got is not None:
            store.move_to_quarantine(digest)
            state += '，已移到 quarantine/，重新导入含它的目录即可补回'
        problems.append(f'文件 {digest[:12]}（{ref} 等用到）{state}')
    loose = loose_damaged = 0
    if rehash:
        for digest in store.iter_objects():
            if digest in wanted:
                continue
            loose += 1
            try:
                ok = hash_path(store.path(digest), from_disk=True)[0] == digest
            except FileNotFoundError:
                continue  # replaced or moved meanwhile
            if not ok:
                store.move_to_quarantine(digest)
                loose_damaged += 1
    if repaired or loose_damaged:
        store.sync()  # a repair (or a damaged file moved aside) counts only once it is on the disk
    echo(f'核对了 {len(versions)} 个版本、{checked} 个文件{"（重算了哈希）" if rehash else ""}；'
         f'从备份修复 {repaired} 个；问题 {len(problems)} 个')
    if rehash:
        echo(f'另外核对了还没有版本用到的文件 {loose} 个（中断的导入留下的）；'
             f'其中损坏的 {loose_damaged} 个已移到 quarantine/，再导入时会重新上传')
    return problems
