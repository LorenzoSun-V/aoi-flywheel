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
from typing import Any, Callable, List, Optional, Set, Tuple

from fw.contract import LABEL_HELP, ContractError, classify_changes, is_label, parse_manifest, split_ref

from . import events, labels, sites
from .store import HEX64, Store, StoreError, hash_path

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
KINDS = ('pool', 'test', 'regression', 'other')  # what people create
MADE = {'trainset': ('训练集快照', 'fw trainset create'),  # made only by trainsets.py
        'batch': ('标注批次', 'fw batch update')}           # made only by batches.py: a batch's revisions


class DatasetError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_ref(ref: Any, what: str = '版本', labels: bool = True) -> Tuple[str, str]:
    """(name, which) of a version written by a person: see fw.contract.split_ref. For every place of the
    server that is handed such a text."""
    try:
        return split_ref(ref, what, labels)
    except ContractError as e:
        raise DatasetError(400, str(e))


def check_digest(digest: str) -> str:
    if not HEX64.match(digest or ''):
        raise DatasetError(400, '哈希应为 64 位小写十六进制')
    return digest


def _stamp(t) -> Optional[str]:
    return t.isoformat(timespec='seconds') if t is not None else None


_VERSION = """
SELECT v.id, d.name AS dataset, d.kind, v.number, v.parent_id, pd.name AS parent_dataset, p.number AS parent_number,
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
    return {'id': row['id'], 'dataset': row['dataset'], 'kind': row['kind'], 'number': row['number'],
            'ref': f'{row["dataset"]}@{row["number"]}', 'parent': parent,
            'manifest_sha256': row['manifest_sha256'], 'file_count': row['file_count'],
            'total_bytes': row['total_bytes'], 'source': row['source'], 'note': row['note'],
            'created_by': row['created_by'], 'created_at': _stamp(row['created_at']), 'labels': []}


def _labelled(conn, versions: List[dict]) -> List[dict]:
    """With the labels (v0.1.0) of each."""
    found = labels.of(conn, 'dataset', [v['id'] for v in versions])
    for v in versions:
        v['labels'] = found[v['id']]
    return versions


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
        for v in _labelled(conn, [_version(v) for v in conn.execute(_VERSION + ' WHERE v.id = ANY(%s)',
                                                                    (latest_ids,)).fetchall()]):
            latest[v['id']] = v
    return [{'id': r['id'], 'name': r['name'], 'site': r['site'], 'line': r['line'], 'kind': r['kind'],
             'note': r['note'], 'created_by': r['created_by'], 'created_at': _stamp(r['created_at']),
             'version_count': r['version_count'], 'latest': latest.get(r['latest_id'])} for r in rows]


# --- sites and lines -------------------------------------------------------------------------------------
def place(conn, site: Any, line: Any, required: bool = True) -> Tuple[str, str]:
    """The registered site and line a new pool, model or release is given, as their codes (typed ses/tx ->
    SES/TX). Unregistered ones are refused: they are registered first, once, so that every list can be
    grouped by them. required false: none at all is allowed too (a test set that belongs to no line)."""
    site = site.strip() if isinstance(site, str) else None
    line = line.strip() if isinstance(line, str) else None
    if site is None or line is None:
        raise DatasetError(400, '现场和产线应为文本')
    if not site and not line and not required:
        return '', ''
    if not site or not line:
        raise DatasetError(400, '要写现场和产线（fw site list 看登记了哪些）')
    found = sites.find(conn, site, line)
    if found is None:
        raise DatasetError(400, f'{site}/{line} 还没有登记；fw site list 看登记了哪些，新的现场或产线先用 '
                                f'fw site add {site}/{line} 登记（管理员或算法工程师）')
    return found[0], found[1]


def _place_name(name: Any) -> str:
    name = name.strip() if isinstance(name, str) else None
    if name is None or len(name) > 64 or any(c in name for c in '\x00\t\r\n'):
        raise DatasetError(400, '显示名最多 64 个字，不能换行')
    return name


def _place_ref(ref: Any) -> Tuple[str, Optional[str]]:
    """'SES' -> the site; 'SES/TX' -> its line."""
    if not isinstance(ref, str) or not ref.strip():
        raise DatasetError(400, '要写现场，或 现场/产线')
    site, sep, line = ref.strip().partition('/')
    for code, what in ((site, '现场'), (line if sep else None, '产线')):
        if code is not None and not sites.CODE.match(code):
            raise DatasetError(400, f'{what} {code!r} 写法不对：{sites.CODE_HELP}')
    return site, (line if sep else None)


def list_places(conn) -> dict:
    return sites.tree(conn)


def add_place(conn, user_id: Optional[int], ref: str, name: str = '') -> dict:
    """Register a site ('SES') or a line of a site ('SES/TX'; the site is registered with it when it is
    new). name: what is shown next to the code of the site or line registered. Codes are for good."""
    site, line = _place_ref(ref)
    name = _place_name(name)
    with conn.transaction():
        new_site = sites.add_site(conn, site, name if line is None else '', user_id)
        code = sites.find(conn, site)[0]
        if new_site:
            events.record(conn, user_id, 'site.add', code, name=name if line is None else '')
        elif line is None:
            raise DatasetError(409, f'现场 {code} 已经登记过了')
        if line is not None:
            if not sites.add_line(conn, code, line, name, user_id):
                raise DatasetError(409, f'{"/".join(sites.find(conn, code, line))} 已经登记过了')
            events.record(conn, user_id, 'line.add', f'{code}/{line}', name=name)
    return sites.tree(conn)


def rename_place(conn, user_id: Optional[int], ref: str, name: str) -> dict:
    """Change the name shown next to a site's or line's code. The code itself never changes."""
    site, line = _place_ref(ref)
    name = _place_name(name)
    found = sites.find(conn, site, line)
    if found is None:
        raise DatasetError(404, f'{ref} 没有登记')
    with conn.transaction():
        sites.rename(conn, site, line, name)
        events.record(conn, user_id, 'site.rename', '/'.join(x for x in found if x), name=name)
    return sites.tree(conn)


def where_place(site: str, line: str, q: str, alias: str = 'd') -> Tuple[List[str], List[Any]]:
    """The conditions of a list narrowed to a site, a line of it, and text in the name."""
    where, args = [], []
    for column, value in (('site', site), ('line', line)):
        if value:
            where.append(f'lower({alias}.{column}) = lower(%s)')
            args.append(value)
    if q:
        where.append(f"position(lower(%s) in lower({alias}.name)) > 0")
        args.append(q)
    return where, args


def list_datasets(conn, site: str = '', line: str = '', q: str = '', kind: str = '', source: str = '') -> List[dict]:
    """Without the datasets that hold the revisions of labelling batches: those are listed as batches.
    site, line: only what belongs there; a training snapshot also belongs to every line one of its versions
    took data from. q: text in the name. kind: only that kind. source: only the snapshots one of whose versions
    took data from the dataset of that name."""
    where, args = ["d.kind <> 'batch'"], []
    own, own_args = where_place(site, line, '')
    if own:
        src, src_args = where_place(site, line, '', 'sd')
        where.append(f'(({" AND ".join(own)}) OR EXISTS (SELECT 1 FROM versions v '
                     f'JOIN trainset_sources ts ON ts.version_id = v.id JOIN versions sv ON sv.id = ts.source_id '
                     f'JOIN datasets sd ON sd.id = sv.dataset_id WHERE v.dataset_id = d.id AND {" AND ".join(src)}))')
        args += own_args + src_args
    if source:
        where.append('EXISTS (SELECT 1 FROM versions v JOIN trainset_sources ts ON ts.version_id = v.id '
                     'JOIN versions sv ON sv.id = ts.source_id JOIN datasets sd ON sd.id = sv.dataset_id '
                     'WHERE v.dataset_id = d.id AND lower(sd.name) = lower(%s))')
        args.append(source)
    more, more_args = where_place('', '', q)
    if kind:
        more.append('d.kind = %s')
        more_args.append(kind)
    return _datasets(conn, conn.execute(_DATASET + ' WHERE ' + ' AND '.join(where + more) + ' ORDER BY lower(d.name)',
                                        args + more_args).fetchall())


def get_dataset(conn, name: str) -> dict:
    rows = conn.execute(_DATASET + ' WHERE lower(d.name) = lower(%s)', (name,)).fetchall()
    if not rows:
        raise DatasetError(404, f'没有数据集 {name}；先用 fw dataset create 建')
    return _datasets(conn, rows)[0]


def create_dataset(conn, user_id: int, name: str, site: str = '', line: str = '', kind: str = 'pool',
                   note: str = '', internal: bool = False) -> dict:
    """internal: also allow kind 'trainset' (only trainsets.py creates those)."""
    if not NAME.match(name or ''):
        raise DatasetError(400, '名称只能用字母、数字和 . _ -，以字母或数字开头，最多 64 个字符')
    if kind not in KINDS and not (internal and kind == 'trainset'):
        raise DatasetError(400, f'类型只能是 {"、".join(KINDS)}')
    if not internal:  # a data pool belongs to a line; a test set may belong to none
        site, line = place(conn, site, line, required=kind == 'pool')
    with conn.transaction():
        if not _insert_dataset(conn, user_id, name, site, line, kind, note):
            raise DatasetError(409, f'{name} 已经存在（数据集、训练集快照和标注批次共用名称，不分大小写）')
        events.record(conn, user_id, 'dataset.create', name, kind=kind, site=site, line=line)
    return get_dataset(conn, name)


def _insert_dataset(conn, user_id: int, name: str, site: str, line: str, kind: str, note: str) -> bool:
    """False when the name is taken (also by someone committing it meanwhile: never a unique-key error)."""
    return conn.execute('INSERT INTO datasets (name, site, line, kind, note, created_by) '
                        'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id',
                        (name, site, line, kind, note, user_id)).fetchone() is not None


def list_versions(conn, name: str) -> List[dict]:
    dataset = get_dataset(conn, name)
    rows = conn.execute(_VERSION + ' WHERE v.dataset_id = %s ORDER BY v.number', (dataset['id'],)).fetchall()
    return _labelled(conn, [_version(r) for r in rows])


def get_version(conn, name: str, number: str) -> dict:
    """number: a version number, a label (v0.1.0) or 'latest'."""
    dataset = get_dataset(conn, name)
    number = str(number)
    if number == 'latest':
        if dataset['latest'] is None:
            raise DatasetError(404, f'{dataset["name"]} 还没有版本')
        return dataset['latest']
    if is_label(number):
        found = labels.find(conn, 'dataset', dataset['id'], number)
        if found is None:
            raise DatasetError(404, f'{dataset["name"]} 没有版本号 {number}')
        if found['retired_at'] is not None:
            raise DatasetError(404, f'{dataset["name"]} 的版本号 {number} 已撤销'
                                    + (f'（{found["retired_note"]}）' if found['retired_note'] else ''))
        return get_version_by_id(conn, found['version_id'])
    if not (number.isascii() and number.isdigit()):
        raise DatasetError(400, '版本应为序号（正整数）、版本号（v0.1.0）或 latest')
    row = conn.execute(_VERSION + ' WHERE v.dataset_id = %s AND v.number = %s',
                       (dataset['id'], int(number))).fetchone()
    if row is None:
        raise DatasetError(404, f'没有版本 {dataset["name"]}@{number}')
    return _labelled(conn, [_version(row)])[0]


def _note(note: Any) -> str:
    note = (note or '').strip() if isinstance(note, str) else None
    if note is None or len(note) > labels.NOTE_MAX:
        raise DatasetError(400, f'备注最多 {labels.NOTE_MAX} 个字')
    return note


def check_label(label: Any) -> str:
    if not is_label(label):
        raise DatasetError(400, f'{label!r}：{LABEL_HELP}')
    return label


def list_labels(conn, name: str) -> dict:
    """Every label of a dataset (also the retired ones) and what the next could be."""
    dataset = get_dataset(conn, name)
    out = labels.listing(conn, 'dataset', dataset['id'])
    for row in out['labels']:
        row['ref'] = f'{dataset["name"]}@{row["number"]}'
    return dict(out, dataset=dataset['name'])


def add_label(conn, user_id: int, name: str, number: str, label: str, note: str = '') -> dict:
    """Give a version a label. A label is written once: it names this version for good."""
    label, note = check_label(label), _note(note)
    version = get_version(conn, name, number)
    if version['kind'] == 'batch':
        raise DatasetError(400, f'{version["dataset"]} 是标注批次，它的各版只有序号，不起版本号')
    dataset = get_dataset(conn, name)
    with conn.transaction():
        if not labels.add(conn, 'dataset', dataset['id'], version['id'], label, note, user_id):
            was = labels.find(conn, 'dataset', dataset['id'], label)
            if was['retired_at'] is None and was['version_id'] == version['id']:
                return version  # asked twice
            where = get_version_by_id(conn, was['version_id'])['ref']
            raise DatasetError(409, f'{dataset["name"]} 的版本号 {label} 已经'
                                    + (f'撤销（原来指 {where}），撤销的版本号不能再用' if was['retired_at'] is not None
                                       else f'用在 {where} 上，版本号定了就不能挪到别的版本') + '；换一个')
        events.record(conn, user_id, 'version.label', version['ref'], label=label, note=note)
    return get_version_by_id(conn, version['id'])


def retire_label(conn, user_id: int, name: str, label: str, note: str) -> dict:
    """Take a label off its version (it was put on the wrong one). The string stays taken."""
    label, note = check_label(label), _note(note)
    if not note:
        raise DatasetError(400, '撤销版本号要写原因')
    dataset = get_dataset(conn, name)
    was = labels.find(conn, 'dataset', dataset['id'], label)
    if was is None:
        raise DatasetError(404, f'{dataset["name"]} 没有版本号 {label}')
    with conn.transaction():
        if not labels.retire(conn, 'dataset', dataset['id'], label, note, user_id):
            raise DatasetError(409, f'{dataset["name"]} 的版本号 {label} 已经撤销过了')
        version = get_version_by_id(conn, was['version_id'])
        events.record(conn, user_id, 'version.unlabel', version['ref'], label=label, note=note)
    return version


def version_files(conn, version_id: int) -> List[Tuple[str, str, int]]:
    """Every file of a version as (path, sha256 hex, size), by path."""
    return [(r['path'], r['d'], r['size']) for r in conn.execute(
        "SELECT path, encode(sha256, 'hex') AS d, size FROM version_files WHERE version_id = %s ORDER BY path",
        (version_id,))]


def get_version_by_id(conn, version_id: int) -> dict:
    row = conn.execute(_VERSION + ' WHERE v.id = %s', (version_id,)).fetchone()
    if row is None:
        raise DatasetError(404, f'没有 id 为 {version_id} 的版本')
    return _labelled(conn, [_version(row)])[0]


def create_version(conn, store: Store, user_id: int, name: str, manifest_sha256: str, parent_id: Optional[int],
                   expect_latest_id: Optional[int], source: str = '', note: str = '',
                   before_freeze: Optional[Callable[[], None]] = None, trainset: bool = False,
                   record: Optional[Callable[[Any, int], None]] = None, new_dataset: Optional[dict] = None,
                   inherited: Optional[Set[Tuple[str, str, int]]] = None, batch: bool = False) -> dict:
    """trainset: the dataset must be a trainset (and only then may it be one); batch: the same for a labelling
    batch (batches.py); record(conn, version_id) writes
    the caller's own rows (a trainset's, or how the version was changed from its parent) inside the transaction,
    before the version is frozen. new_dataset (site, line,
    kind, note): create the dataset in the same transaction, so a failed first version leaves no name behind;
    if the name was taken meanwhile, nothing is created (409). inherited: entries taken unchanged from the
    frozen parent version (changes.py): they were checked when that version was made, so the store is not asked
    about each of them again (a file lost since then is found by fw-server verify, for both versions)."""
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
    bad = [(p, s, store.size(d)) for p, d, s in entries
           if (inherited is None or (p, d, s) not in inherited) and store.size(d) != s]
    if bad:
        examples = '；'.join(f'{p}（{"没有" if got is None else f"大小 {got}，应为 {s}"}）' for p, s, got in bad[:5])
        raise DatasetError(400, f'有 {len(bad)} 个文件服务器上还没有或大小不对，没有建版本；重新运行导入会补传。例如 {examples}')
    try:
        store.flush()
    except StoreError as e:
        raise DatasetError(503, str(e)) from None
    total = sum(s for _, _, s in entries)
    with conn.transaction():
        if new_dataset is not None and not _insert_dataset(conn, user_id, name, new_dataset['site'],
                                                           new_dataset['line'], new_dataset['kind'],
                                                           new_dataset['note']):
            again = '请重新运行 fw trainset create' if trainset else '换一个批次名称' if batch else '请重新运行导入'
            raise DatasetError(409, f'{name} 在这期间被别人建了；{again}')
        dataset = conn.execute('SELECT id, name, kind FROM datasets WHERE lower(name) = lower(%s) FOR UPDATE',
                               (name,)).fetchone()
        if dataset is None:
            raise DatasetError(404, f'没有数据集 {name}')
        made = 'trainset' if trainset else 'batch' if batch else None
        if dataset['kind'] != made and (dataset['kind'] in MADE or made is not None):
            if dataset['kind'] in MADE:
                what, how = MADE[dataset['kind']]
                raise DatasetError(400, f'{dataset["name"]} 是{what}，只能用 {how} 建新版本')
            raise DatasetError(400, f'{dataset["name"]} 不是{MADE[made][0]}')
        latest = conn.execute('SELECT id, number FROM versions WHERE dataset_id = %s ORDER BY number DESC LIMIT 1',
                              (dataset['id'],)).fetchone()
        if (latest['id'] if latest else None) != expect_latest_id:
            now = f'@{latest["number"]}' if latest else '没有版本'
            again = ('请重新运行 fw trainset create' if trainset else '请重新运行 fw batch update' if batch
                     else '请重新运行导入，以最新版本为父版本')
            raise DatasetError(409, f'{dataset["name"]} 在这期间有了变化（最新是 {now}）；{again}')
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
        if record is not None:
            record(conn, version_id)
        if before_freeze is not None:
            before_freeze()  # tests: a failure between writing the rows and freezing
        conn.execute('UPDATE versions SET frozen = true WHERE id = %s', (version_id,))
        events.record(conn, user_id, 'version.create', f'{dataset["name"]}@{number}', kind=dataset['kind'],
                      file_count=len(entries), total_bytes=total, parent_id=parent_id, source=source)
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


def verify(conn, store: Store, rehash: bool = False, repair_from=None, echo=print, *, more) -> List[str]:
    """Check that every version (and, through `more`, every model version and evaluation report) can be laid
    out exactly as frozen. Returns the problems left.

    - each version's rows rebuild its manifest hash, and the manifest object is in the store and intact;
    - more(conn) -> (problems, {digest: (size or None, where it is used)}, count): what the records of the
      modules above hold, checked the same way. Always pass cli.verify_inputs (model versions, evaluation
      reports and runners; it is required so that no caller forgets it: with rehash, an object nothing here
      knows about counts as left over by an import);
    - every referenced object is in the store with the right size, and with rehash, the right content;
    - with rehash, also every object no version uses yet (left by an import that failed or is still
      running): the next import of the same files reuses whatever the store holds, so a damaged one is
      moved to quarantine/ and that import sends it again. These are not problems of any version.
    Damaged referenced objects are replaced from `repair_from` (another store, e.g. the backup) when it has
    an intact copy; otherwise they are moved to quarantine/ so that the next upload of that file restores
    them. With rehash every object is read back from the disk (not from the page cache, which can still hold
    data a failed write-back never got onto the disk). A file or folder that cannot be read is a problem
    too: a check that skipped it has not seen what it holds. Repairs and moves are made durable before this
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
    # objects other records hold (the file sets of model versions and evaluation reports, models.verify_inputs)
    more_problems, more_wanted, sets = more(conn)
    problems += more_problems
    wanted = {v['m']: (None, v['ref']) for v in versions}  # digest -> (size if known, a version using it)
    rows = conn.execute(
        "SELECT encode(f.sha256, 'hex') AS d, f.size, min(d.name || '@' || v.number) AS ref "
        'FROM version_files f JOIN versions v ON v.id = f.version_id JOIN datasets d ON d.id = v.dataset_id '
        'GROUP BY f.sha256, f.size').fetchall()
    for r in rows:
        wanted[r['d']] = (r['size'], r['ref'])
    for digest, (size, ref) in more_wanted.items():
        if wanted.get(digest, (None,))[0] is None:
            wanted[digest] = (size, ref)
    checked = repaired = 0
    for digest, (size, ref) in sorted(wanted.items()):
        checked += 1
        try:
            got = store.size(digest)
            ok = got is not None and (size is None or got == size)
            if ok and rehash:
                ok = hash_path(store.path(digest), from_disk=True)[0] == digest
        except OSError as e:
            problems.append(f'文件 {digest[:12]}（{ref} 等用到）读不出来（{e.strerror or e}），先检查磁盘')
            continue
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
        try:
            for digest in store.iter_objects():
                if digest in wanted:
                    continue
                loose += 1
                try:
                    ok = hash_path(store.path(digest), from_disk=True)[0] == digest
                except FileNotFoundError:
                    continue  # replaced or moved meanwhile
                except OSError as e:
                    problems.append(f'文件 {digest[:12]}（还没有版本用到）读不出来（{e.strerror or e}），先检查磁盘')
                    continue
                if not ok:
                    store.move_to_quarantine(digest)
                    loose_damaged += 1
        except StoreError as e:  # a folder could not be listed: the check is incomplete and must not pass
            problems.append(str(e))
    if repaired or loose_damaged:
        store.sync()  # a repair (or a damaged file moved aside) counts only once it is on the disk
    echo(f'核对了 {len(versions)} 个版本、{sets} 个模型版本和评估报告、{checked} 个文件'
         f'{"（重算了哈希）" if rehash else ""}；'
         f'从备份修复 {repaired} 个；问题 {len(problems)} 个')
    if rehash:
        echo(f'另外核对了还没有版本用到的文件 {loose} 个（中断的导入留下的）；'
             f'其中损坏的 {loose_damaged} 个已移到 quarantine/，再导入时会重新上传')
    return problems
