"""Models, model versions, evaluation reports, releases, tags and the trace chain (docs/architecture.md).

Registering a model version or an evaluation report, which both hold files:
1. the client has uploaded every file; the request lists them as (path, sha256, size);
2. the server checks that each one is in the store with that size, reads metrics.json and the start of
   model.onnx from the store (what is recorded comes from the stored files, not from the request), writes the
   manifest of the file set and makes everything durable (store.flush);
3. one transaction: lock the model row, insert the file set unfrozen, COPY its rows, freeze it (the database
   rebuilds the manifest hash first) and insert the record. The database refuses any later change.

Registering the same thing again (a script that is run twice, a retry after a network error) returns the
record that exists instead of making a second one.
"""
from __future__ import annotations

import datetime
import json
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fw.contract import ContractError, check_path, collisions, eval_dirs, is_label, manifest_bytes
from fw.onnx_check import OnnxError, check_self_contained

from . import datasets, events, labels, trainsets
from .datasets import NAME, DatasetError, check_digest
from .store import Store, StoreError

TASKS = {'det': '检测'}
TAGS = {'candidate': '候选', 'deprecated': '弃用'}
PRECISIONS = ('fp32', 'fp16')
SPLITS = ('train', 'val', 'test')
ONNX, CLASSES, METRICS, EXTRA, WEIGHTS = 'model.onnx', 'classes.txt', 'metrics.json', 'files/', 'weights/'
COMMIT = re.compile(r'[0-9a-f]{7,64}\Z')
CREDENTIALS = re.compile(r'https?://[^/]*@', re.I)
# Letters, digits, _ and . plus Chinese characters (CJK unified ideographs and extension A), by code point,
# so that it does not depend on the database's locale: the same as the CHECKs on models and releases.
SITE_LINE = re.compile(r'[A-Za-z0-9_.\u3400-\u4DBF\u4E00-\u9FFF]+\Z')
RELEASE_REF = re.compile(r'(.+)-r([1-9][0-9]*)\Z')
MAX_FILES = 5000
MAX_METRICS = 1 << 20
FUTURE = datetime.timedelta(minutes=10)  # how far a deployment time may be ahead of the server's clock
RELEASE_LOCK = 7340002  # advisory lock space for numbering the releases of a line
Entry = Tuple[str, str, int]


def _stamp(t) -> Optional[str]:
    return t.isoformat(timespec='seconds') if t is not None else None


def _text(value: Any, what: str, limit: int = 2000, line: bool = False) -> str:
    if not isinstance(value, str):
        raise DatasetError(400, f'{what}应为文本')
    value = value.strip()
    if len(value) > limit:
        raise DatasetError(400, f'{what}不能超过 {limit} 个字符')
    if '\x00' in value or (line and any(c in value for c in '\t\r\n')):
        raise DatasetError(400, f'{what}里有不能用的字符')
    return value


def _commit(value: str, what: str) -> str:
    value = _text(value, what, 64, line=True).lower()
    if not COMMIT.match(value):
        raise DatasetError(400, f'{what}应为 git 提交号（7 到 64 位十六进制），例如 git rev-parse HEAD 的输出')
    return value


def _repo(value: str, what: str, required: bool) -> str:
    value = _text(value, what, 500, line=True)
    if required and not value:
        raise DatasetError(400, f'要写{what}')
    if CREDENTIALS.match(value):
        raise DatasetError(400, f'{what}里带着账号或令牌（https://…@），去掉 @ 之前的部分再登记')
    return value


def _name(value: str, what: str) -> str:
    """A site or a line: SITE_LINE, the very rule the database CHECKs (0003_models.sql). Release refs are
    <site>-<line>-r<n>, go into URL paths and are told from model versions by '@', so neither may hold '-'
    (A-B/C and A/B-C would both be A-B-C-r1), '/', '@' or anything else."""
    value = _text(value, what, 64, line=True)
    if not value:
        raise DatasetError(400, f'要写{what}')
    if not SITE_LINE.match(value):
        raise DatasetError(400, f'{what}只能用汉字、英文字母、数字、_ 和 .（发布编号是 <现场>-<产线>-r<序号>）：{value}')
    return value


def _pinned(name: str, number: str) -> str:
    """Registering on a model version needs its number: a retry must not land on a version made meanwhile."""
    if not (str(number).isdigit() or is_label(number)):  # a label names one version for good, like a number
        raise DatasetError(400, f'登记评估或发布要写具体的模型版本，例如 {name}@3，不能用 latest')
    return number


# --- file sets ---------------------------------------------------------------------------------------------
def _entries(files: Sequence, required: str, top: Sequence[str], weights: bool = False) -> List[Entry]:
    """The files of a request as (path, sha256, size): paths allowed for this record, no duplicates, no two
    paths that collide on Windows. weights: one file may be weights/<name> (the trained weights, .pt / .pth)."""
    if not files:
        raise DatasetError(400, '没有文件')
    if len(files) > MAX_FILES:
        raise DatasetError(400, f'文件不能超过 {MAX_FILES} 个')
    out: List[Entry] = []
    seen = set()
    for item in files:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise DatasetError(400, '每个文件写成 [路径, sha256, 大小]')
        path, digest, size = item
        if not isinstance(path, str) or not isinstance(digest, str) or isinstance(size, bool) \
                or not isinstance(size, int) or size < 0:
            raise DatasetError(400, '每个文件写成 [路径, sha256, 大小]')
        try:
            check_path(path)
        except ContractError as e:
            raise DatasetError(400, f'文件路径不能用：{e}') from None
        if weights and path.startswith(WEIGHTS):
            if '/' in path[len(WEIGHTS):] or any(p.startswith(WEIGHTS) for p in seen):
                raise DatasetError(400, f'{path}：weights/ 下只能有一个权重文件，不能有子目录')
            if size == 0:  # frozen, an empty file would also block the retry with the real one (same ONNX)
                raise DatasetError(400, f'{path}：权重文件是空的，没有登记')
        elif path not in top and not (path.startswith(EXTRA) and len(path) > len(EXTRA)):
            raise DatasetError(400, f'{path}：只能是 {"、".join(top)}' + ('、weights/<权重文件>' if weights else '')
                                    + ' 或 files/ 下的文件')
        if path in seen:
            raise DatasetError(400, f'{path} 写了两次')
        seen.add(path)
        out.append((path, check_digest(digest), size))
    problems = collisions(seen)
    if problems:
        raise DatasetError(400, '这些文件不能放在同一个目录里：' + '；'.join(problems[:5]))
    if required not in seen:
        raise DatasetError(400, f'缺 {required}')
    return out


def _check_stored(store: Store, entries: List[Entry]) -> None:
    bad = [(p, s, store.size(d)) for p, d, s in entries if store.size(d) != s]
    if bad:
        examples = '；'.join(f'{p}（{"没有" if got is None else f"大小 {got}，应为 {s}"}）' for p, s, got in bad[:5])
        raise DatasetError(400, f'有 {len(bad)} 个文件服务器上还没有或大小不对，没有登记；重新运行会补传。例如 {examples}')


def _reject_constant(name: str):
    raise ValueError(f'不能用 {name}')


def _no_nul(value: Any) -> bool:
    """No \\u0000 and no infinite number (1e309 parses as inf): PostgreSQL jsonb takes neither."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return '\x00' not in value
    if isinstance(value, dict):
        return all(_no_nul(k) and _no_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_no_nul(v) for v in value)
    return True


def _metrics(store: Store, entries: List[Entry]) -> dict:
    """metrics.json parsed from the store; {} when there is none."""
    found = [e for e in entries if e[0] == METRICS]
    if not found:
        return {}
    _, digest, size = found[0]
    if size > MAX_METRICS:
        raise DatasetError(400, f'metrics.json 不能超过 {MAX_METRICS >> 20} MB')
    data = store.read(digest)
    if data is None:
        raise DatasetError(400, 'metrics.json 还没有上传')
    try:
        doc = json.loads(data.decode('utf-8-sig'), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as e:
        raise DatasetError(400, f'指标文件不是合法的 UTF-8 JSON（{e}）') from None
    if not isinstance(doc, dict):
        raise DatasetError(400, '指标文件应该是一个 JSON 对象（{…}）')
    if not _no_nul(doc):
        raise DatasetError(400, '指标文件里有 \\u0000 字符，或者有超出范围的数（例如 1e309）')
    return doc


def _check_onnx(store: Store, digest: str) -> None:
    try:
        with open(store.path(digest), 'rb') as f:
            head = f.read(1)
    except OSError as e:
        raise DatasetError(503, f'读不了 model.onnx（{e.strerror or e}）') from None
    if head != b'\x08':  # an ONNX ModelProto starts with its ir_version (field 1, varint)
        raise DatasetError(400, 'model.onnx 不像 ONNX 文件（ONNX 文件以 ir_version 开头）；'
                                '检查是不是传成了 .pt 等权重文件，权重可以作为附件登记')
    try:  # fw checks this before sending; the web sends without fw, so the server checks it too
        check_self_contained(store.path(digest))
    except OnnxError as e:
        raise DatasetError(400, f'model.onnx：{e}') from None
    except OSError as e:
        raise DatasetError(503, f'读不了 model.onnx（{e.strerror or e}）') from None


def _prepare(store: Store, entries: List[Entry]) -> str:
    """Write the manifest of these files and make them durable; returns the manifest's sha256."""
    digest, _ = store.put_bytes(manifest_bytes(entries))
    try:
        store.flush()
    except StoreError as e:
        raise DatasetError(503, str(e)) from None
    return digest


def _insert_fileset(conn, entries: List[Entry], manifest: str) -> int:
    fid = conn.execute('INSERT INTO filesets (manifest_sha256, file_count, total_bytes) VALUES (%s, %s, %s) '
                       'RETURNING id', (bytes.fromhex(manifest), len(entries), sum(s for _, _, s in entries))
                       ).fetchone()['id']
    with conn.cursor() as cur:
        with cur.copy('COPY fileset_files (fileset_id, path, sha256, size) FROM STDIN (FORMAT BINARY)') as cp:
            cp.set_types(['int8', 'text', 'bytea', 'int8'])
            for path, digest, size in entries:
                cp.write_row((fid, path, bytes.fromhex(digest), size))
    conn.execute('UPDATE filesets SET frozen = true WHERE id = %s', (fid,))
    return fid


def fileset_files(conn, fileset_id: int) -> List[dict]:
    return conn.execute("SELECT path, encode(sha256, 'hex') AS sha256, size FROM fileset_files WHERE fileset_id = %s "
                        'ORDER BY path', (fileset_id,)).fetchall()


# --- models ------------------------------------------------------------------------------------------------
_MODEL = """
SELECT m.id, m.name, m.site, m.line, m.task, m.note, u.name AS created_by, m.created_at,
       (SELECT count(*) FROM model_versions v WHERE v.model_id = m.id) AS version_count,
       (SELECT max(v.number) FROM model_versions v WHERE v.model_id = m.id) AS latest
FROM models m JOIN users u ON u.id = m.created_by
"""


def _model(r: dict) -> dict:
    return {'id': r['id'], 'name': r['name'], 'site': r['site'], 'line': r['line'], 'task': r['task'],
            'note': r['note'], 'created_by': r['created_by'], 'created_at': _stamp(r['created_at']),
            'version_count': r['version_count'],
            'latest': f'{r["name"]}@{r["latest"]}' if r['latest'] is not None else None}


def list_models(conn, site: str = '', line: str = '', q: str = '', trainset: str = '') -> List[dict]:
    """site, line: only the models of that site or line; q: text in the name; trainset: only the models a version
    of which was trained on a version of the snapshot of that name."""
    where, args = datasets.where_place(site, line, q, 'm')
    if trainset:
        where.append('EXISTS (SELECT 1 FROM model_versions mv JOIN versions tv ON tv.id = mv.trainset_id '
                     'JOIN datasets td ON td.id = tv.dataset_id WHERE mv.model_id = m.id AND lower(td.name) = lower(%s))')
        args.append(trainset)
    return [_model(r) for r in conn.execute(_MODEL + (' WHERE ' + ' AND '.join(where) if where else '')
                                            + ' ORDER BY lower(m.name)', args).fetchall()]


def get_model(conn, name: str) -> dict:
    row = conn.execute(_MODEL + ' WHERE lower(m.name) = lower(%s)', (name,)).fetchone()
    if row is None:
        raise DatasetError(404, f'没有模型 {name}；先用 fw model create 建')
    return _model(row)


def create_model(conn, user_id: int, name: str, site: str, line: str, task: str = 'det', note: str = '') -> dict:
    if not NAME.match(name or ''):
        raise DatasetError(400, '模型名称只能用字母、数字和 . _ -，以字母或数字开头，最多 64 个字符')
    site, line = datasets.place(conn, _name(site, '现场'), _name(line, '产线'))
    if task not in TASKS:
        raise DatasetError(400, f'任务类型只能是 {"、".join(TASKS)}（{"、".join(TASKS.values())}）')
    note = _text(note, '备注')
    with conn.transaction():  # ON CONFLICT: someone creating the same name meanwhile also gets 409, not 500
        if conn.execute('INSERT INTO models (name, site, line, task, note, created_by) VALUES (%s, %s, %s, %s, %s, %s) '
                        'ON CONFLICT DO NOTHING RETURNING id', (name, site, line, task, note, user_id)).fetchone() is None:
            raise DatasetError(409, f'模型 {name} 已经存在（名称不分大小写）')
        events.record(conn, user_id, 'model.create', name, site=site, line=line, task=task)
    return get_model(conn, name)


# --- model versions ----------------------------------------------------------------------------------------
_MV = """
SELECT mv.id, m.id AS model_id, m.name AS model, m.site, m.line, m.task, mv.number, mv.fileset_id,
       encode(mv.onnx_sha256, 'hex') AS onnx_sha256, encode(s.manifest_sha256, 'hex') AS manifest_sha256,
       s.file_count, s.total_bytes, mv.trainset_id, td.name AS trainset_dataset, tv.number AS trainset_number,
       encode(tv.manifest_sha256, 'hex') AS trainset_manifest, mv.code_repo, mv.code_commit, mv.code_dirty,
       mv.metrics, mv.note, u.name AS created_by, mv.created_at,
       (SELECT f.size FROM fileset_files f WHERE f.fileset_id = mv.fileset_id AND f.path = 'model.onnx') AS onnx_size,
       (SELECT t.classes FROM trainset_versions t WHERE t.version_id = mv.trainset_id) AS classes
FROM model_versions mv
JOIN models m ON m.id = mv.model_id
JOIN filesets s ON s.id = mv.fileset_id
JOIN versions tv ON tv.id = mv.trainset_id
JOIN datasets td ON td.id = tv.dataset_id
JOIN users u ON u.id = mv.created_by
"""


def _tags(conn, ids: List[int]) -> Dict[int, List[str]]:
    out: Dict[int, List[str]] = {i: [] for i in ids}
    if ids:
        for r in conn.execute('SELECT DISTINCT ON (model_version_id, tag) model_version_id, tag, added '
                              'FROM model_tag_events WHERE model_version_id = ANY(%s) '
                              'ORDER BY model_version_id, tag, id DESC', (ids,)):
            if r['added']:
                out[r['model_version_id']].append(r['tag'])
    return out


def _model_versions(conn, rows: List[dict]) -> List[dict]:
    tags = _tags(conn, [r['id'] for r in rows])
    named = labels.of(conn, 'model', [r['id'] for r in rows])
    return [{'id': r['id'], 'model': r['model'], 'number': r['number'], 'ref': f'{r["model"]}@{r["number"]}',
             'site': r['site'], 'line': r['line'], 'task': r['task'], 'fileset_id': r['fileset_id'],
             'onnx_sha256': r['onnx_sha256'], 'onnx_size': r['onnx_size'], 'manifest_sha256': r['manifest_sha256'],
             'file_count': r['file_count'], 'total_bytes': r['total_bytes'],
             'trainset': {'id': r['trainset_id'], 'ref': f'{r["trainset_dataset"]}@{r["trainset_number"]}',
                          'manifest_sha256': r['trainset_manifest']},
             'classes': r['classes'], 'code_repo': r['code_repo'], 'code_commit': r['code_commit'] or '',
             'code_dirty': r['code_dirty'], 'metrics': r['metrics'], 'note': r['note'], 'tags': tags[r['id']],
             'labels': named[r['id']],
             'created_by': r['created_by'], 'created_at': _stamp(r['created_at'])} for r in rows]


def list_model_versions(conn, name: str) -> List[dict]:
    model = get_model(conn, name)
    return _model_versions(conn, conn.execute(_MV + ' WHERE mv.model_id = %s ORDER BY mv.number',
                                              (model['id'],)).fetchall())


def get_model_version(conn, name: str, number: str) -> dict:
    """number: a version number, a label (v0.1.0) or 'latest'."""
    model = get_model(conn, name)
    number = str(number)
    if number == 'latest':
        if model['latest'] is None:
            raise DatasetError(404, f'{model["name"]} 还没有版本；用 fw model register 登记')
        number = model['latest'].rpartition('@')[2]
    if is_label(number):
        found = labels.find(conn, 'model', model['id'], number)
        if found is None:
            raise DatasetError(404, f'{model["name"]} 没有版本号 {number}')
        if found['retired_at'] is not None:
            raise DatasetError(404, f'{model["name"]} 的版本号 {number} 已撤销'
                                    + (f'（{found["retired_note"]}）' if found['retired_note'] else ''))
        return get_model_version_by_id(conn, found['version_id'])
    if not (number.isascii() and number.isdigit()):
        raise DatasetError(400, '版本应为序号（正整数）、版本号（v0.1.0）或 latest')
    rows = conn.execute(_MV + ' WHERE mv.model_id = %s AND mv.number = %s', (model['id'], int(number))).fetchall()
    if not rows:
        raise DatasetError(404, f'没有模型版本 {model["name"]}@{number}')
    return _model_versions(conn, rows)[0]


def list_labels(conn, name: str) -> dict:
    """Every label of a model (also the retired ones) and what the next could be."""
    model = get_model(conn, name)
    out = labels.listing(conn, 'model', model['id'])
    for row in out['labels']:
        row['ref'] = f'{model["name"]}@{row["number"]}'
    return dict(out, model=model['name'])


def add_label(conn, user_id: int, name: str, number: str, label: str, note: str = '') -> dict:
    """Give a model version a label (v0.1.0). A label is written once: it names this version for good."""
    label, note = datasets.check_label(label), _text(note, '备注')
    mv = get_model_version(conn, name, number)
    model = get_model(conn, name)
    with conn.transaction():
        if not labels.add(conn, 'model', model['id'], mv['id'], label, note, user_id):
            was = labels.find(conn, 'model', model['id'], label)
            if was['retired_at'] is None and was['version_id'] == mv['id']:
                return mv  # asked twice
            where = get_model_version_by_id(conn, was['version_id'])['ref']
            raise DatasetError(409, f'{model["name"]} 的版本号 {label} 已经'
                                    + (f'撤销（原来指 {where}），撤销的版本号不能再用' if was['retired_at'] is not None
                                       else f'用在 {where} 上，版本号定了就不能挪到别的版本') + '；换一个')
        events.record(conn, user_id, 'model_version.label', mv['ref'], label=label, note=note)
    return get_model_version_by_id(conn, mv['id'])


def retire_label(conn, user_id: int, name: str, label: str, note: str) -> dict:
    """Take a label off its model version (it was put on the wrong one). The string stays taken."""
    label, note = datasets.check_label(label), _text(note, '原因')
    if not note:
        raise DatasetError(400, '撤销版本号要写原因')
    model = get_model(conn, name)
    was = labels.find(conn, 'model', model['id'], label)
    if was is None:
        raise DatasetError(404, f'{model["name"]} 没有版本号 {label}')
    with conn.transaction():
        if not labels.retire(conn, 'model', model['id'], label, note, user_id):
            raise DatasetError(409, f'{model["name"]} 的版本号 {label} 已经撤销过了')
        mv = get_model_version_by_id(conn, was['version_id'])
        events.record(conn, user_id, 'model_version.unlabel', mv['ref'], label=label, note=note)
    return mv


def get_model_version_by_id(conn, mv_id: int) -> dict:
    rows = conn.execute(_MV + ' WHERE mv.id = %s', (mv_id,)).fetchall()
    if not rows:
        raise DatasetError(404, f'没有 id 为 {mv_id} 的模型版本')
    return _model_versions(conn, rows)[0]


def model_version_detail(conn, name: str, number: str) -> dict:
    mv = get_model_version(conn, name, number)
    mv['files'] = fileset_files(conn, mv['fileset_id'])
    mv['evals'] = list_evals(conn, model_version_id=mv['id'])
    mv['releases'] = list_releases(conn, model_version_id=mv['id'])
    return mv


def register_model_version(conn, store: Store, user_id: int, name: str, files: Sequence, trainset_id: int,
                           code_repo: str = '', code_commit: str = '', code_dirty: bool = False,
                           note: str = '') -> dict:
    """files: model.onnx, optionally weights/<name>, metrics.json and files/...; classes.txt is added from the
    snapshot. The training code (repository, commit) is optional: an empty commit records none."""
    model = get_model(conn, name)
    entries = _entries(files, ONNX, (ONNX, METRICS), weights=True)
    trainset = datasets.get_version_by_id(conn, trainset_id)
    if trainset['kind'] != 'trainset':
        raise DatasetError(400, f'{trainset["ref"]} 不是训练集快照；模型版本要引用训练用的快照（fw trainset create 建的）')
    ts = conn.execute('SELECT task FROM trainset_versions WHERE version_id = %s', (trainset_id,)).fetchone()
    if ts['task'] != model['task']:
        raise DatasetError(400, f'{trainset["ref"]} 是{TASKS.get(ts["task"], ts["task"])}的快照，'
                                f'模型 {model["name"]} 是{TASKS[model["task"]]}模型')
    classes = conn.execute("SELECT encode(sha256, 'hex') AS d, size FROM version_files "
                           "WHERE version_id = %s AND path = 'classes.txt'", (trainset_id,)).fetchone()
    entries.append((CLASSES, classes['d'], classes['size']))
    commit = _commit(code_commit, '训练代码的提交号') if _text(code_commit, '训练代码的提交号') else None
    repo = _repo(code_repo, '训练代码仓库', required=False)
    if commit is None and (repo or code_dirty):
        raise DatasetError(400, '写了训练代码仓库或「有没提交的改动」，就要写训练代码的提交号')
    note = _text(note, '备注')
    _check_stored(store, entries)
    onnx = next(d for p, d, _ in entries if p == ONNX)
    _check_onnx(store, onnx)
    metrics = _metrics(store, entries)
    manifest = _prepare(store, entries)
    with conn.transaction():
        conn.execute('SELECT id FROM models WHERE id = %s FOR UPDATE', (model['id'],))
        same = conn.execute(_MV + ' WHERE mv.model_id = %s AND mv.onnx_sha256 = %s ORDER BY mv.number LIMIT 1',
                            (model['id'], bytes.fromhex(onnx))).fetchall()
        if same:
            old = _model_versions(conn, same)[0]
            if (old['manifest_sha256'], old['trainset']['id'], old['code_commit'], old['code_repo'],
                    old['code_dirty']) == (manifest, trainset_id, commit or '', repo, bool(code_dirty)):
                return dict(old, existing=True)
            raise DatasetError(409, f'这个 ONNX 已经登记为 {old["ref"]}，但训练集快照、代码或附带的文件不一样；'
                                    '同一个 ONNX 只登记一次')
        number = conn.execute('SELECT coalesce(max(number), 0) + 1 AS n FROM model_versions WHERE model_id = %s',
                              (model['id'],)).fetchone()['n']
        fid = _insert_fileset(conn, entries, manifest)
        mv_id = conn.execute(
            'INSERT INTO model_versions (model_id, number, fileset_id, onnx_sha256, trainset_id, code_repo, '
            'code_commit, code_dirty, metrics, note, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
            'RETURNING id', (model['id'], number, fid, bytes.fromhex(onnx), trainset_id, repo, commit,
                             bool(code_dirty), json.dumps(metrics, ensure_ascii=False), note, user_id)
        ).fetchone()['id']
        events.record(conn, user_id, 'model_version.register', f'{model["name"]}@{number}', trainset_id=trainset_id,
                      onnx_sha256=onnx)
    return dict(get_model_version_by_id(conn, mv_id), existing=False)


def set_tag(conn, user_id: int, name: str, number: str, tag: str, added: bool, note: str = '') -> dict:
    if tag not in TAGS:
        raise DatasetError(400, f'标签只能是 {"、".join(f"{k}（{v}）" for k, v in TAGS.items())}')
    note = _text(note, '备注')
    mv = get_model_version(conn, name, number)
    with conn.transaction():
        conn.execute('SELECT id FROM model_versions WHERE id = %s FOR UPDATE', (mv['id'],))
        row = conn.execute('SELECT added FROM model_tag_events WHERE model_version_id = %s AND tag = %s '
                           'ORDER BY id DESC LIMIT 1', (mv['id'], tag)).fetchone()
        changed = bool(row and row['added']) != bool(added)
        if changed:
            conn.execute('INSERT INTO model_tag_events (model_version_id, tag, added, note, created_by) '
                         'VALUES (%s, %s, %s, %s, %s)', (mv['id'], tag, bool(added), note, user_id))
            events.record(conn, user_id, 'model_version.tag', mv['ref'], tag=tag, added=bool(added), note=note)
    return dict(get_model_version_by_id(conn, mv['id']), changed=changed)


def tag_history(conn, mv_id: int) -> List[dict]:
    return [{'tag': r['tag'], 'added': r['added'], 'note': r['note'], 'by': r['name'], 'at': _stamp(r['created_at'])}
            for r in conn.execute('SELECT e.tag, e.added, e.note, u.name, e.created_at FROM model_tag_events e '
                                  'JOIN users u ON u.id = e.created_by WHERE e.model_version_id = %s ORDER BY e.id',
                                  (mv_id,))]


# --- evaluation reports ------------------------------------------------------------------------------------
_EVAL = """
SELECT e.id, e.model_version_id, m.name AS model, mv.number AS model_number, e.testset_id,
       td.name AS testset_dataset, tv.number AS testset_number, td.kind AS testset_kind, e.split, e.fileset_id,
       encode(s.manifest_sha256, 'hex') AS manifest_sha256, s.file_count, s.total_bytes, e.metrics, e.tool,
       e.tool_version, e.note, u.name AS created_by, e.created_at, e.dirs, e.job_id,
       r.name || '@' || rv.number AS runner
FROM eval_reports e
JOIN model_versions mv ON mv.id = e.model_version_id
JOIN models m ON m.id = mv.model_id
JOIN versions tv ON tv.id = e.testset_id
JOIN datasets td ON td.id = tv.dataset_id
JOIN filesets s ON s.id = e.fileset_id
JOIN users u ON u.id = e.created_by
LEFT JOIN runner_versions rv ON rv.id = e.runner_version_id
LEFT JOIN runners r ON r.id = rv.runner_id
"""


def _eval(r: dict) -> dict:
    testset = f'{r["testset_dataset"]}@{r["testset_number"]}'
    return {'id': r['id'], 'model_version': {'id': r['model_version_id'], 'ref': f'{r["model"]}@{r["model_number"]}'},
            'testset': {'id': r['testset_id'], 'ref': testset + (f':{r["split"]}' if r['split'] else ''),
                        'kind': r['testset_kind']},
            'split': r['split'], 'fileset_id': r['fileset_id'], 'manifest_sha256': r['manifest_sha256'],
            'file_count': r['file_count'], 'total_bytes': r['total_bytes'], 'metrics': r['metrics'],
            'tool': r['tool'], 'tool_version': r['tool_version'], 'note': r['note'], 'created_by': r['created_by'],
            'created_at': _stamp(r['created_at']), 'dirs': list(r['dirs']), 'job_id': r['job_id'],
            'runner': r['runner']}


def list_evals(conn, model: Optional[str] = None, model_version_id: Optional[int] = None) -> List[dict]:
    if model_version_id is not None:
        rows = conn.execute(_EVAL + ' WHERE e.model_version_id = %s ORDER BY e.id', (model_version_id,))
    elif model:
        rows = conn.execute(_EVAL + ' WHERE mv.model_id = %s ORDER BY e.id', (get_model(conn, model)['id'],))
    else:
        rows = conn.execute(_EVAL + ' ORDER BY e.id')
    return [_eval(r) for r in rows.fetchall()]


def get_eval(conn, eval_id: int) -> dict:
    row = conn.execute(_EVAL + ' WHERE e.id = %s', (eval_id,)).fetchone()
    if row is None:
        raise DatasetError(404, f'没有评估报告 #{eval_id}')
    out = _eval(row)
    out['files'] = fileset_files(conn, out['fileset_id'])
    return out


def register_eval(conn, store: Store, user_id: int, name: str, number: str, testset_id: int, split: str,
                  files: Sequence, tool: str = '', tool_version: str = '', note: str = '',
                  dirs: Sequence[str] = (), job_id: Optional[int] = None, runner_version_id: Optional[int] = None,
                  images: Optional[int] = None) -> dict:
    """files: metrics.json and optionally files/... (for aoi-deteval its output folder). dirs: the folders of
    the test set that were evaluated (none = all of it). job_id, runner_version_id: the evaluation job that made
    it, in a transaction that holds that job (evals.finish). images: how many images the metrics must say were
    evaluated (an aoi-deteval result counts them as 'images')."""
    mv = get_model_version(conn, name, _pinned(name, number))
    testset = datasets.get_version_by_id(conn, testset_id)
    split = split or ''
    if testset['kind'] == 'batch':
        raise DatasetError(400, f'{testset["ref"]} 是还没入库的标注批次，不能当测试集')
    if testset['kind'] == 'trainset':
        if split not in SPLITS:
            raise DatasetError(400, f'{testset["ref"]} 是训练集快照，要写明评的是哪个划分，例如 {testset["ref"]}:test')
        counts = trainsets.info(conn, testset_id)['trainset']['counts']
        if not counts.get(split, {}).get('samples'):
            raise DatasetError(400, f'{testset["ref"]} 里没有 {split} 划分的样本')
    elif split:
        raise DatasetError(400, f'{testset["ref"]} 不是训练集快照，不用写划分')
    try:
        dirs = eval_dirs(dirs)
    except ContractError as e:
        raise DatasetError(400, str(e)) from None
    if split and dirs:
        raise DatasetError(400, '训练集快照的划分整个评，不选目录')
    entries = _entries(files, METRICS, (METRICS,))
    tool = _text(tool, '评估工具名', 100, line=True)
    tool_version = _text(tool_version, '评估工具版本', 100, line=True)
    note = _text(note, '备注')
    _check_stored(store, entries)
    metrics = _metrics(store, entries)
    if images is not None and metrics.get('images') != images:
        raise DatasetError(400, f'指标里评了 {metrics.get("images")} 张图，应为 {images} 张：有图没评到，没有登记')
    manifest = _prepare(store, entries)
    with conn.transaction():
        conn.execute('SELECT id FROM model_versions WHERE id = %s FOR UPDATE', (mv['id'],))
        # the same report sent again: by hand, the same content; from a job, that job's report (another job
        # with the same result has a report of its own, which records that job and its runner)
        same = conn.execute(_EVAL + ' WHERE e.model_version_id = %s AND e.testset_id = %s AND e.split = %s '
                                    'AND e.dirs = %s AND s.manifest_sha256 = %s AND e.job_id IS NOT DISTINCT FROM %s '
                                    'ORDER BY e.id LIMIT 1',
                            (mv['id'], testset_id, split, dirs, bytes.fromhex(manifest), job_id)).fetchone()
        if same:
            return dict(_eval(same), existing=True)
        fid = _insert_fileset(conn, entries, manifest)
        eval_id = conn.execute(
            'INSERT INTO eval_reports (model_version_id, testset_id, split, fileset_id, metrics, tool, tool_version, '
            'note, created_by, dirs, job_id, runner_version_id) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
            (mv['id'], testset_id, split, fid, json.dumps(metrics, ensure_ascii=False), tool, tool_version, note,
             user_id, dirs, job_id, runner_version_id)).fetchone()['id']
        events.record(conn, user_id, 'eval.register', f'评估 {eval_id}', model_version=mv['ref'],
                      testset_id=testset_id, split=split, job_id=job_id)
    return dict(get_eval(conn, eval_id), existing=False)


# --- releases ----------------------------------------------------------------------------------------------
_RELEASE = """
SELECT r.id, r.site, r.line, r.number, r.model_version_id, m.name AS model, mv.number AS model_number,
       r.deploy_repo, r.deploy_commit, r.deployed_at, r.gpu, r.trt_version, r.engine_precision,
       encode(r.engine_sha256, 'hex') AS engine_sha256, r.engine_size, r.note, u.name AS created_by, r.created_at
FROM releases r
JOIN model_versions mv ON mv.id = r.model_version_id
JOIN models m ON m.id = mv.model_id
JOIN users u ON u.id = r.created_by
"""


def _release(r: dict) -> dict:
    return {'id': r['id'], 'ref': f'{r["site"]}-{r["line"]}-r{r["number"]}', 'site': r['site'], 'line': r['line'],
            'number': r['number'],
            'model_version': {'id': r['model_version_id'], 'ref': f'{r["model"]}@{r["model_number"]}'},
            'deploy_repo': r['deploy_repo'], 'deploy_commit': r['deploy_commit'],
            'deployed_at': _stamp(r['deployed_at']), 'gpu': r['gpu'], 'trt_version': r['trt_version'],
            'precision': r['engine_precision'], 'engine_sha256': r['engine_sha256'], 'engine_size': r['engine_size'],
            'note': r['note'], 'created_by': r['created_by'], 'created_at': _stamp(r['created_at'])}


def list_releases(conn, site: str = '', line: str = '', model_version_id: Optional[int] = None) -> List[dict]:
    where, args = [], []
    if site:
        where.append('lower(r.site) = lower(%s)')
        args.append(site)
    if line:
        where.append('lower(r.line) = lower(%s)')
        args.append(line)
    if model_version_id is not None:
        where.append('r.model_version_id = %s')
        args.append(model_version_id)
    sql = _RELEASE + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY r.deployed_at, r.id'
    return [_release(r) for r in conn.execute(sql, args).fetchall()]


def current_release(conn, site: str, line: str) -> dict:
    row = conn.execute(_RELEASE + ' WHERE lower(r.site) = lower(%s) AND lower(r.line) = lower(%s) '
                                  'ORDER BY r.deployed_at DESC, r.id DESC LIMIT 1', (site, line)).fetchone()
    if row is None:
        raise DatasetError(404, f'{site}/{line} 还没有登记过发布')
    return _release(row)


def get_release(conn, ref: str) -> dict:
    m = RELEASE_REF.match(ref or '')
    if not m:
        raise DatasetError(400, f'发布编号写成 <现场>-<产线>-r<序号>，例如 SES-TX-r3：{ref}')
    rows = conn.execute(_RELEASE + " WHERE lower(r.site || '-' || r.line) = lower(%s) AND r.number = %s",
                        (m.group(1), int(m.group(2)))).fetchall()
    if not rows:
        raise DatasetError(404, f'没有发布 {ref}')
    if len(rows) > 1:
        pairs = '、'.join(f'{r["site"]}/{r["line"]}' for r in rows)
        raise DatasetError(400, f'{ref} 对得上好几条产线（{pairs}），用 fw release list --site --line 找到要的那条')
    return _release(rows[0])


def _deployed_at(value: str) -> datetime.datetime:
    try:
        t = datetime.datetime.fromisoformat(_text(value, '部署时间', 64, line=True))
    except ValueError:
        raise DatasetError(400, f'部署时间写成 ISO 8601 并带时区，例如 2026-11-20T10:30+08:00：{value}') from None
    if t.tzinfo is None:
        raise DatasetError(400, f'部署时间要带时区，例如 2026-11-20T10:30+08:00：{value}')
    if t > datetime.datetime.now(datetime.timezone.utc) + FUTURE:
        raise DatasetError(400, f'部署时间 {value} 比服务器现在的时间还晚；检查这台机器的时钟和时区')
    return t


def register_release(conn, user_id: int, name: str, number: str, site: str, line: str, deploy_repo: str,
                     deploy_commit: str, deployed_at: str, gpu: str = '', trt_version: str = '', precision: str = '',
                     engine_sha256: Optional[str] = None, engine_size: Optional[int] = None,
                     note: str = '') -> dict:
    mv = get_model_version(conn, name, _pinned(name, number))
    site, line = datasets.place(conn, _name(site or mv['site'], '现场'), _name(line or mv['line'], '产线'))
    repo = _repo(deploy_repo, '部署仓库地址', required=True)
    commit = _commit(deploy_commit, '部署仓库的提交号')
    when = _deployed_at(deployed_at)
    gpu = _text(gpu, 'GPU 型号', 100, line=True)
    trt = _text(trt_version, 'TensorRT 版本', 100, line=True)
    precision = (precision or '').lower()
    if precision not in ('',) + PRECISIONS:
        raise DatasetError(400, f'精度只能是 {"、".join(PRECISIONS)}')
    if (engine_sha256 is None) != (engine_size is None):
        raise DatasetError(400, '引擎的哈希和大小要一起写')
    if engine_sha256 is not None:
        check_digest(engine_sha256)
        if isinstance(engine_size, bool) or not isinstance(engine_size, int) or engine_size <= 0:
            raise DatasetError(400, '引擎大小应为正整数')
    note = _text(note, '备注')
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(lower(%s) || '/' || lower(%s)))",
                     (RELEASE_LOCK, site, line))
        same = conn.execute(_RELEASE + ' WHERE lower(r.site) = lower(%s) AND lower(r.line) = lower(%s) '
                                       'AND r.model_version_id = %s AND r.deploy_commit = %s AND r.deployed_at = %s '
                                       'ORDER BY r.id LIMIT 1', (site, line, mv['id'], commit, when)).fetchone()
        if same:
            out = dict(_release(same), existing=True)
            if (out['deploy_repo'], out['gpu'], out['trt_version'], out['precision'], out['engine_sha256'],
                    out['engine_size']) != (repo, gpu, trt, precision, engine_sha256, engine_size):
                raise DatasetError(409, f'{out["ref"]} 已经登记了同一次部署（同一条产线、模型版本、部署提交号和部署时间），'
                                        '但仓库地址、GPU、TensorRT、精度或引擎不一样；发布登记后不能改')
        else:
            n = conn.execute('SELECT coalesce(max(number), 0) + 1 AS n FROM releases '
                             'WHERE lower(site) = lower(%s) AND lower(line) = lower(%s)', (site, line)).fetchone()['n']
            rid = conn.execute(
                'INSERT INTO releases (site, line, number, model_version_id, deploy_repo, deploy_commit, deployed_at, '
                'gpu, trt_version, engine_precision, engine_sha256, engine_size, note, created_by) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                (site, line, n, mv['id'], repo, commit, when, gpu, trt, precision,
                 bytes.fromhex(engine_sha256) if engine_sha256 else None, engine_size, note, user_id)
            ).fetchone()['id']
            out = dict(_release(conn.execute(_RELEASE + ' WHERE r.id = %s', (rid,)).fetchone()), existing=False)
            events.record(conn, user_id, 'release.register', out['ref'], model_version=mv['ref'], site=site,
                          line=line, deploy_commit=commit)
    current = current_release(conn, site, line)
    out['current'] = {'ref': current['ref'], 'model_version': current['model_version'],
                      'deployed_at': current['deployed_at']}
    return out


# --- trace chain -------------------------------------------------------------------------------------------
def chain(conn, store: Store, ref: str, check: bool = False) -> dict:
    """From a release (SES-TX-r3) or a model version (SES-TX-detr@3) down to the pool versions the snapshot was
    made from. check: every file of every level is in the store with its size."""
    if '@' in ref:
        mv = get_model_version(conn, *datasets.parse_ref(ref, '模型版本'))
        release = None
    else:
        release = get_release(conn, ref)
        mv = get_model_version_by_id(conn, release['model_version']['id'])
    mv['files'] = fileset_files(conn, mv['fileset_id'])
    mv['tag_history'] = tag_history(conn, mv['id'])
    evals = list_evals(conn, model_version_id=mv['id'])
    trainset = datasets.get_version_by_id(conn, mv['trainset']['id'])
    trainset.update(trainsets.info(conn, trainset['id']))
    source_ids = [r['source_id'] for r in conn.execute(
        'SELECT source_id FROM trainset_sources WHERE version_id = %s ORDER BY position', (trainset['id'],))]
    sources = [datasets.get_version_by_id(conn, i) for i in source_ids]
    out = {'release': release, 'releases': list_releases(conn, model_version_id=mv['id']), 'model_version': mv,
           'evals': evals, 'trainset': trainset, 'sources': sources}
    if check:
        out['check'] = _check_chain(conn, store, mv, evals, trainset, sources)
    return out


def _check_chain(conn, store: Store, mv: dict, evals: List[dict], trainset: dict, sources: List[dict]) -> dict:
    wanted: Dict[str, Tuple[Optional[int], str]] = {}  # digest -> (size if known, where it is used)
    for fid, ref, manifest in [(mv['fileset_id'], mv['ref'], mv['manifest_sha256'])] + \
            [(e['fileset_id'], f'评估 #{e["id"]}', e['manifest_sha256']) for e in evals]:
        wanted.setdefault(manifest, (None, f'{ref} 的清单'))
        for f in fileset_files(conn, fid):
            wanted.setdefault(f['sha256'], (f['size'], f'{ref}：{f["path"]}'))
    testsets = [datasets.get_version_by_id(conn, i) for i in sorted({e['testset']['id'] for e in evals})]
    for v in [trainset] + sources + testsets:  # the test sets of the reports too: a report is only as good as them
        wanted.setdefault(v['manifest_sha256'], (None, f'{v["ref"]} 的清单'))
        for r in conn.execute("SELECT path, encode(sha256, 'hex') AS d, size FROM version_files WHERE version_id = %s",
                              (v['id'],)):
            wanted.setdefault(r['d'], (r['size'], f'{v["ref"]}：{r["path"]}'))
    problems = []
    for digest, (size, where) in sorted(wanted.items(), key=lambda kv: kv[1][1]):
        try:
            got = store.size(digest)
        except OSError as e:
            problems.append(f'{where}（{digest[:12]}）读不出来：{e.strerror or e}')
            continue
        if got is None:
            problems.append(f'{where}（{digest[:12]}）文件库里没有')
        elif size is not None and got != size:
            problems.append(f'{where}（{digest[:12]}）大小是 {got}，应为 {size}')
    return {'objects': len(wanted), 'problem_count': len(problems), 'problems': problems[:50]}


# --- fw-server verify --------------------------------------------------------------------------------------
def verify_inputs(conn) -> Tuple[List[str], Dict[str, Tuple[Optional[int], str]], int]:
    """For datasets.verify: file sets whose rows no longer rebuild their manifest hash, every object the file
    sets of model versions, evaluation reports and prediction sets hold (and the image lists of prediction
    sets) (its size, None for a manifest, and where it is used),
    and how many file sets there are."""
    sets = conn.execute(
        "SELECT s.id, encode(s.manifest_sha256, 'hex') AS m, encode(fileset_manifest_sha256(s.id), 'hex') AS rebuilt, "
        "coalesce((SELECT m.name || '@' || mv.number FROM model_versions mv JOIN models m ON m.id = mv.model_id "
        "          WHERE mv.fileset_id = s.id), "
        "         (SELECT '评估报告 #' || e.id FROM eval_reports e WHERE e.fileset_id = s.id), "
        "         (SELECT '预测集 #' || p.id FROM prediction_sets p WHERE p.fileset_id = s.id)) AS ref "
        'FROM filesets s ORDER BY s.id').fetchall()
    problems = [f'{s["ref"]}：文件清单和登记的清单哈希对不上' for s in sets if s['m'] != s['rebuilt']]
    ref = {s['id']: s['ref'] for s in sets}
    wanted: Dict[str, Tuple[Optional[int], str]] = {s['m']: (None, s['ref']) for s in sets}
    for r in conn.execute("SELECT encode(sha256, 'hex') AS d, size, min(fileset_id) AS fid FROM fileset_files "
                          'GROUP BY sha256, size'):
        wanted[r['d']] = (r['size'], ref[r['fid']])
    for r in conn.execute("SELECT id, encode(inputs_sha256, 'hex') AS d FROM prediction_sets ORDER BY id"):
        wanted.setdefault(r['d'], (None, f'预测集 #{r["id"]} 的图片清单'))
    return problems, wanted, len(sets)
