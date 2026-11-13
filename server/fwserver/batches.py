"""Labelling batches: a folder of images on its way into a data pool, through labelling and review.

    upload ─► open ─submit─► review ─approve─► approved ─import─► imported
                ▲               │ reject          │ reject
                └──new revision─┴──► rejected ◄───┘            (close: any state before imported)

- A batch belongs to one account (its owner, who uploaded it) and to one pool; it will become the folder
  `dir` of that pool. Every upload is a frozen revision: a version of the batch's own dataset (kind 'batch',
  named like the batch), whose file list holds paths inside the folder. Revisions never enter the pool by
  themselves and are never removed.
- Each revision records who labelled it (a person's name as typed) next to the account that uploaded it.
- The owner sends the newest revision to a reviewer (an account that may review; an algorithm engineer may
  ask themselves, which is recorded as a self-review). The reviewer rejects it with a comment, and optionally
  the images meant, or approves it; an approval can carry the reviewer's own corrected folder, which becomes
  one more revision uploaded by the reviewer. The last answer counts: an approved batch can still be rejected
  until it is imported.
- Import adds the approved revision to the pool with changes.create, the same code and checks as
  fw dataset add (a folder the pool already has is refused unless replaced; duplicate images are reported).
  The pool version's source is `batch:<name>@<revision>`, and the batch keeps the pool version's id; both are
  written in the transaction that freezes the pool version.
- Pre-labels: the owner asks for a model version to be run over the newest revision of an open or rejected
  batch (predictions.submit_for_batch; the job is noted here), fetches the result as a folder with
  fw batch get --prelabel, corrects it and uploads it as the next revision.
- Every step is a row of batch_steps (only ever added) and an event, in the transaction of the step.

Who may do what beyond the capability the endpoint asks for: only the owner (or an administrator) uploads
revisions and submits; only the reviewer who was asked (or an administrator) rejects and approves; the owner,
the reviewer who was asked and an administrator close.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from fw.contract import IMAGE_EXTS, ContractError, blocking_reasons, classify_paths, fold, parse_manifest

from . import auth, changes, datasets, events, formats
from .datasets import NAME, DatasetError, check_digest
from .store import Store

STATUS = {'open': '待提交复核', 'review': '待复核', 'rejected': '已退回', 'approved': '复核通过', 'imported': '已入库',
          'closed': '已关闭'}
STEPS = {'upload': '上传', 'submit': '提交复核', 'reject': '退回', 'approve': '通过', 'import': '入库', 'close': '关闭',
         'prelabel': '预标注'}
UNFINISHED = ('open', 'review', 'rejected', 'approved')
MAX_COMMENT = 2000
MAX_IMAGES = 500         # images a rejection points at
EXAMPLES = 200           # images a comparison lists
MAX_LABEL_READS = 20000  # labels a comparison reads; beyond this the rest are counted but not compared
MAX_PAIRS = 250_000      # old x new boxes of one image a comparison pairs up; beyond this only added / removed
MAX_WORK = 5_000_000     # the same, summed over one comparison
BATCH_LOCK = 0x62617463  # advisory lock class: creating a batch for a pool
SAME_BOX = 0.5           # two boxes are one box that was moved or renamed when they overlap this much (IoU)

_BATCH = """
SELECT b.id, b.dataset_id, d.name, b.pool_id, p.name AS pool, p.site, p.line, b.dir, b.owner_id, o.name AS owner,
       b.status, b.reviewer_id, r.name AS reviewer, b.imported_version_id, iv.number AS imported_number,
       b.created_at, b.updated_at, lv.id AS latest_id, lv.number AS latest_number, lv.file_count, lv.total_bytes,
       encode(lv.manifest_sha256, 'hex') AS manifest_sha256, br.labeler
FROM batches b
JOIN datasets d ON d.id = b.dataset_id
JOIN datasets p ON p.id = b.pool_id
JOIN users o ON o.id = b.owner_id
LEFT JOIN users r ON r.id = b.reviewer_id
LEFT JOIN versions iv ON iv.id = b.imported_version_id
JOIN LATERAL (SELECT v.id, v.number, v.file_count, v.total_bytes, v.manifest_sha256 FROM versions v
              WHERE v.dataset_id = b.dataset_id ORDER BY v.number DESC LIMIT 1) lv ON true
JOIN batch_revisions br ON br.version_id = lv.id
"""


def _stamp(t) -> Optional[str]:
    return t.isoformat(timespec='seconds') if t is not None else None


def _is_admin(user: dict) -> bool:
    return auth.allowed(user['role'], 'users.manage')


def _owns(user: dict, b: dict) -> bool:
    return b['owner_id'] == user['user_id'] or _is_admin(user)


def _reviews(user: dict, b: dict) -> bool:
    return auth.allowed(user['role'], 'batches.review') and (b['reviewer_id'] == user['user_id'] or _is_admin(user))


def _actions(user: Optional[dict], b: dict) -> List[str]:
    """What this account can do with the batch as it stands (the web page shows these buttons)."""
    if user is None:
        return []
    out = []
    if _owns(user, b) and b['status'] in ('open', 'rejected'):
        out.append('revise')
    if _owns(user, b) and b['status'] in ('open', 'rejected'):
        out.append('prelabel')
    if _owns(user, b) and b['status'] == 'open':
        out.append('submit')
    if _reviews(user, b) and b['status'] == 'review':
        out.append('approve')
    if _reviews(user, b) and b['status'] in ('review', 'approved'):
        out.append('reject')
    if b['status'] == 'approved' and auth.allowed(user['role'], 'datasets.write'):
        out.append('import')
    if b['status'] in UNFINISHED and (_owns(user, b) or _reviews(user, b)):
        out.append('close')
    return out


def _row(r: dict, user: Optional[dict] = None) -> dict:
    return {'id': r['id'], 'name': r['name'], 'pool': r['pool'], 'site': r['site'], 'line': r['line'],
            'dir': r['dir'], 'owner': r['owner'], 'status': r['status'], 'status_text': STATUS[r['status']],
            'reviewer': r['reviewer'], 'revision': r['latest_number'], 'ref': f'{r["name"]}@{r["latest_number"]}',
            'latest_id': r['latest_id'], 'file_count': r['file_count'], 'total_bytes': r['total_bytes'],
            'manifest_sha256': r['manifest_sha256'], 'labeler': r['labeler'],
            'imported': f'{r["pool"]}@{r["imported_number"]}' if r['imported_number'] is not None else None,
            'created_at': _stamp(r['created_at']), 'updated_at': _stamp(r['updated_at']),
            'actions': _actions(user, {'owner_id': r['owner_id'], 'reviewer_id': r['reviewer_id'],
                                       'status': r['status']})}


def _find(conn, name: str, lock: bool = False) -> dict:
    """The batch's row; with lock, held until the transaction ends (every step takes it first)."""
    row = conn.execute('SELECT b.id, b.dataset_id, d.name, b.pool_id, b.dir, b.owner_id, b.status, b.reviewer_id '
                       'FROM batches b JOIN datasets d ON d.id = b.dataset_id WHERE lower(d.name) = lower(%s)'
                       + (' FOR UPDATE OF b' if lock else ''), (name,)).fetchone()
    if row is None:
        raise DatasetError(404, f'没有标注批次 {name}')
    return row


def _latest(conn, b: dict) -> dict:
    return conn.execute('SELECT id, number FROM versions WHERE dataset_id = %s ORDER BY number DESC LIMIT 1',
                        (b['dataset_id'],)).fetchone()


def _same_revision(b: dict, latest: dict, revision: Optional[int]) -> None:
    """revision: the one the caller looked at; a newer one since then means they have not seen what they act on."""
    if revision is not None and revision != latest['number']:
        raise DatasetError(409, f'{b["name"]} 现在最新是第 {latest["number"]} 版，不是第 {revision} 版；先看最新的一版')


def _step(conn, batch_id: int, actor_id: int, step: str, version_id: Optional[int], comment: str = '',
          reviewer_id: Optional[int] = None, self_review: bool = False, images: Optional[List[str]] = None,
          pool_version_id: Optional[int] = None, job_id: Optional[int] = None) -> None:
    conn.execute('INSERT INTO batch_steps (batch_id, actor_id, step, version_id, reviewer_id, self_review, comment, '
                 'images, pool_version_id, job_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                 (batch_id, actor_id, step, version_id, reviewer_id, self_review, comment,
                  json.dumps(images or [], ensure_ascii=False), pool_version_id, job_id))


def _text(value: Any, what: str, limit: int, required: bool = False) -> str:
    text = (value or '').strip() if isinstance(value, str) else ''
    if required and not text:
        raise DatasetError(400, f'要写{what}')
    if len(text) > limit:
        raise DatasetError(400, f'{what}最多 {limit} 个字')
    return text


def _labeler(value: Any) -> str:
    return _text(value, '标注人姓名', 64, required=True)


def _reviewer(conn, name: str) -> dict:
    """An enabled account that may review."""
    try:
        u = auth.get_user(conn, name)
    except auth.AuthError as e:
        raise DatasetError(400, str(e)) from None
    if u['disabled_at'] is not None:
        raise DatasetError(400, f'账号 {u["name"]} 已停用，不能当复核人')
    if not auth.allowed(u['role'], 'batches.review'):
        raise DatasetError(400, f'{u["name"]} 是{auth.ROLES[u["role"]]}账号，不能当复核人；复核人选算法工程师账号')
    return u


def _check_content(store: Store, manifest_sha256: str, allow_other_files: bool, force: bool) -> List[Tuple[str, str, int]]:
    """The checks of fw dataset import on the batch's folder, at upload: what would stop it from entering the
    pool is refused now, not after the review."""
    data = store.read(check_digest(manifest_sha256))
    if data is None:
        raise DatasetError(400, '批次的文件清单还没有上传')
    try:
        entries = parse_manifest(data)
    except ContractError as e:
        raise DatasetError(400, f'批次的文件清单不合格：{e}') from None
    if not entries:
        raise DatasetError(400, '目录里没有文件')
    reasons = blocking_reasons(classify_paths(p for p, _, _ in entries), allow_other_files, force)
    if reasons:
        raise DatasetError(400, '没有上传批次：' + '；'.join(reasons))
    if not any(PurePosixPath(p).suffix.lower() in IMAGE_EXTS for p, _, _ in entries):
        raise DatasetError(400, '目录里没有图片')
    return entries


def _submit(conn, user: dict, b: dict, version: dict, reviewer: dict) -> None:
    """Inside the step's transaction, with the batch locked."""
    mine = reviewer['id'] == b['owner_id']
    conn.execute("UPDATE batches SET status = 'review', reviewer_id = %s, updated_at = now() WHERE id = %s",
                 (reviewer['id'], b['id']))
    _step(conn, b['id'], user['user_id'], 'submit', version['id'], reviewer_id=reviewer['id'], self_review=mine)
    events.record(conn, user['user_id'], 'batch.submit', f'{b["name"]}@{version["number"]}',
                  reviewer=reviewer['name'], self_review=mine)


# --- uploading ---------------------------------------------------------------------------------------
def create(conn, store: Store, user: dict, name: str, pool: str, dir: str, manifest_sha256: str, labeler: str,
           note: str = '', source: str = '', reviewer: Optional[str] = None, allow_other_files: bool = False,
           force: bool = False, before_freeze=None) -> dict:
    """A new batch with its first revision. reviewer: also send it to review at once."""
    if not NAME.match(name or ''):
        raise DatasetError(400, '批次名称只能用字母、数字和 . _ -，以字母或数字开头，最多 64 个字符')
    labeler = _labeler(labeler)
    note = _text(note, '说明', MAX_COMMENT)
    d = changes.clean_dir(dir)
    target = datasets.get_dataset(conn, pool)
    if target['kind'] in datasets.MADE:
        raise DatasetError(400, f'{target["name"]} 是{datasets.MADE[target["kind"]][0]}，批次要进的是数据池')
    def no_other(c) -> None:
        for o in c.execute('SELECT d.name, b.dir FROM batches b JOIN datasets d ON d.id = b.dataset_id '
                           'WHERE b.pool_id = %s AND b.status = ANY(%s)', (target['id'], list(UNFINISHED))).fetchall():
            if fold(o['dir']) == fold(d):
                raise DatasetError(409, f'批次 {o["name"]} 也要进 {target["name"]} 的 {o["dir"]}/，还没有结束；'
                                        f'给那个批次上传新的一版（fw batch update {o["name"]}），或换一个目录名称')

    no_other(conn)  # said before anything is checked or made; decided under the lock in record()
    _check_content(store, manifest_sha256, allow_other_files, force)
    asked = _reviewer(conn, reviewer) if reviewer else None

    def record(c, version_id: int) -> None:
        # one new batch per pool at a time, so that two uploads for the same folder cannot both pass the check
        c.execute('SELECT pg_advisory_xact_lock(%s, %s)', (BATCH_LOCK, target['id'] % (1 << 31)))
        no_other(c)
        dataset_id = c.execute('SELECT dataset_id FROM versions WHERE id = %s', (version_id,)).fetchone()['dataset_id']
        batch_id = c.execute('INSERT INTO batches (dataset_id, pool_id, dir, owner_id) VALUES (%s, %s, %s, %s) '
                             'RETURNING id', (dataset_id, target['id'], d, user['user_id'])).fetchone()['id']
        c.execute('INSERT INTO batch_revisions (version_id, batch_id, labeler) VALUES (%s, %s, %s)',
                  (version_id, batch_id, labeler))
        _step(c, batch_id, user['user_id'], 'upload', version_id, comment=note)
        events.record(c, user['user_id'], 'batch.upload', f'{name}@1', pool=target['name'], dir=d, labeler=labeler)
        if asked is not None:
            _submit(c, user, {'id': batch_id, 'name': name, 'owner_id': user['user_id']},
                    {'id': version_id, 'number': 1}, asked)

    new = {'site': target['site'], 'line': target['line'], 'kind': 'batch', 'note': note}
    datasets.create_version(conn, store, user['user_id'], name, manifest_sha256, None, None, source, note,
                            before_freeze=before_freeze, record=record, new_dataset=new, batch=True)
    out = get(conn, name, user)
    in_pool = target['latest'] is not None and conn.execute(
        'SELECT 1 FROM version_files WHERE version_id = %s AND starts_with(path, %s) LIMIT 1',
        (target['latest']['id'], d + '/')).fetchone() is not None
    out['notices'] = [f'{target["name"]} 里已经有 {d}/（{target["latest"]["ref"]}）：入库时要选择替换，'
                      f'或者现在换一个目录名称重新上传'] if in_pool else []
    return out


def revise(conn, store: Store, user: dict, name: str, manifest_sha256: str, labeler: str,
           expect_latest_id: Optional[int], note: str = '', source: str = '', reviewer: Optional[str] = None,
           allow_other_files: bool = False, force: bool = False, before_freeze=None) -> dict:
    """The owner's next revision of an open or rejected batch. reviewer: also send it to review at once
    ('' or None: not yet; '-': the reviewer asked last time)."""
    labeler = _labeler(labeler)
    note = _text(note, '说明', MAX_COMMENT)
    b = _find(conn, name)
    _may_revise(user, b)
    _check_content(store, manifest_sha256, allow_other_files, force)
    asked = _who(conn, b, reviewer) if reviewer else None
    latest = _latest(conn, b)

    def record(c, version_id: int) -> None:
        now = _find(c, name, lock=True)
        _may_revise(user, now)
        number = latest['number'] + 1
        c.execute('INSERT INTO batch_revisions (version_id, batch_id, labeler) VALUES (%s, %s, %s)',
                  (version_id, now['id'], labeler))
        c.execute("UPDATE batches SET status = 'open', updated_at = now() WHERE id = %s", (now['id'],))
        _step(c, now['id'], user['user_id'], 'upload', version_id, comment=note)
        events.record(c, user['user_id'], 'batch.upload', f'{now["name"]}@{number}', labeler=labeler)
        if asked is not None:
            _submit(c, user, now, {'id': version_id, 'number': number}, asked)

    datasets.create_version(conn, store, user['user_id'], b['name'], manifest_sha256, latest['id'], expect_latest_id,
                            source, note, before_freeze=before_freeze, record=record, batch=True)
    return get(conn, name, user)


def _may_revise(user: dict, b: dict) -> None:
    if not _owns(user, b):
        raise DatasetError(403, f'{b["name"]} 不是你的批次；只有上传它的账号能上传新的一版')
    if b['status'] not in ('open', 'rejected'):
        why = {'review': '正在复核，等复核人通过或退回', 'approved': '已经复核通过，要改就请复核人退回'}.get(
            b['status'], STATUS[b['status']])
        raise DatasetError(409, f'{b["name"]} {why}，现在不能上传新的一版')


def _who(conn, b: dict, reviewer: Optional[str]) -> dict:
    """The reviewer to ask: the one named, or ('-' or nothing) the one asked last time."""
    if reviewer and reviewer != '-':
        return _reviewer(conn, reviewer)
    if b['reviewer_id'] is None:
        raise DatasetError(400, '要写复核人（--reviewer 账号名）')
    last = conn.execute('SELECT name FROM users WHERE id = %s', (b['reviewer_id'],)).fetchone()['name']
    return _reviewer(conn, last)


# --- review ------------------------------------------------------------------------------------------
def submit(conn, user: dict, name: str, reviewer: Optional[str] = None, revision: Optional[int] = None) -> dict:
    with conn.transaction():
        b = _find(conn, name, lock=True)
        if not _owns(user, b):
            raise DatasetError(403, f'{b["name"]} 不是你的批次；只有上传它的账号能提交复核')
        if b['status'] != 'open':
            why = '已退回：先上传改好的一版（fw batch update）' if b['status'] == 'rejected' else STATUS[b['status']]
            raise DatasetError(409, f'{b["name"]} {why}，不能提交复核')
        latest = _latest(conn, b)
        _same_revision(b, latest, revision)
        _submit(conn, user, b, latest, _who(conn, b, reviewer))
    return get(conn, name, user)


def _may_review(user: dict, b: dict, states: Tuple[str, ...], what: str) -> None:
    if not _reviews(user, b):
        raise DatasetError(403, f'{b["name"]} 的复核人不是你；只有被指定的复核人能{what}')
    if b['status'] not in states:
        raise DatasetError(409, f'{b["name"]} 现在是「{STATUS[b["status"]]}」，不能{what}')


def reject(conn, user: dict, name: str, comment: str, images: Optional[List[str]] = None,
           revision: Optional[int] = None) -> dict:
    comment = _text(comment, '退回意见', MAX_COMMENT, required=True)
    images = sorted(set(images or []))
    if len(images) > MAX_IMAGES or not all(isinstance(i, str) for i in images):
        raise DatasetError(400, f'问题图片最多列 {MAX_IMAGES} 张，其余写在意见里')
    with conn.transaction():
        b = _find(conn, name, lock=True)
        _may_review(user, b, ('review', 'approved'), '退回')
        latest = _latest(conn, b)
        _same_revision(b, latest, revision)
        if images:
            found = {r['path'] for r in conn.execute('SELECT path FROM version_files WHERE version_id = %s '
                                                     'AND path = ANY(%s)', (latest['id'], images))}
            unknown = [i for i in images if i not in found]
            if unknown:
                raise DatasetError(400, f'第 {latest["number"]} 版里没有这些文件：{"、".join(unknown[:5])}'
                                        f'{" 等" if len(unknown) > 5 else ""}（写成批次目录里的相对路径）')
        conn.execute("UPDATE batches SET status = 'rejected', updated_at = now() WHERE id = %s", (b['id'],))
        _step(conn, b['id'], user['user_id'], 'reject', latest['id'], comment=comment, images=images)
        events.record(conn, user['user_id'], 'batch.reject', f'{b["name"]}@{latest["number"]}',
                      comment=comment[:200], images=len(images))
    return get(conn, name, user)


def approve(conn, store: Store, user: dict, name: str, comment: str = '', revision: Optional[int] = None,
            manifest_sha256: Optional[str] = None, labeler: Optional[str] = None, source: str = '',
            allow_other_files: bool = False, force: bool = False, before_freeze=None) -> dict:
    """manifest_sha256: the reviewer's own corrected folder; it becomes one more revision, uploaded by the
    reviewer, and that is the revision approved."""
    comment = _text(comment, '复核意见', MAX_COMMENT)

    def approved(c, b: dict, version_id: int, number: int) -> None:
        c.execute("UPDATE batches SET status = 'approved', updated_at = now() WHERE id = %s", (b['id'],))
        _step(c, b['id'], user['user_id'], 'approve', version_id, comment=comment)
        events.record(c, user['user_id'], 'batch.approve', f'{b["name"]}@{number}',
                      changed_by_reviewer=manifest_sha256 is not None)

    if manifest_sha256 is None:
        with conn.transaction():
            b = _find(conn, name, lock=True)
            _may_review(user, b, ('review',), '通过')
            latest = _latest(conn, b)
            _same_revision(b, latest, revision)
            approved(conn, b, latest['id'], latest['number'])
        return get(conn, name, user)

    labeler = _labeler(labeler)
    b = _find(conn, name)
    _may_review(user, b, ('review',), '通过')
    latest = _latest(conn, b)
    _same_revision(b, latest, revision)
    _check_content(store, manifest_sha256, allow_other_files, force)

    def record(c, version_id: int) -> None:
        now = _find(c, name, lock=True)
        _may_review(user, now, ('review',), '通过')
        number = latest['number'] + 1
        c.execute('INSERT INTO batch_revisions (version_id, batch_id, labeler, by_reviewer) VALUES (%s, %s, %s, true)',
                  (version_id, now['id'], labeler))
        _step(c, now['id'], user['user_id'], 'upload', version_id, comment='复核人修改后的一版')
        events.record(c, user['user_id'], 'batch.upload', f'{now["name"]}@{number}', labeler=labeler,
                      by_reviewer=True)
        approved(c, now, version_id, number)

    datasets.create_version(conn, store, user['user_id'], b['name'], manifest_sha256, latest['id'], latest['id'],
                            source, '复核人修改后的一版', before_freeze=before_freeze, record=record, batch=True)
    return get(conn, name, user)


def close(conn, user: dict, name: str, comment: str = '') -> dict:
    comment = _text(comment, '关闭原因', MAX_COMMENT)
    with conn.transaction():
        b = _find(conn, name, lock=True)
        if not (_owns(user, b) or _reviews(user, b)):
            raise DatasetError(403, f'{b["name"]} 不是你的批次，你也不是它的复核人，不能关闭')
        if b['status'] not in UNFINISHED:
            raise DatasetError(409, f'{b["name"]} {STATUS[b["status"]]}，不能关闭')
        latest = _latest(conn, b)
        conn.execute("UPDATE batches SET status = 'closed', updated_at = now() WHERE id = %s", (b['id'],))
        _step(conn, b['id'], user['user_id'], 'close', latest['id'], comment=comment)
        events.record(conn, user['user_id'], 'batch.close', f'{b["name"]}@{latest["number"]}', comment=comment[:200])
    return get(conn, name, user)


# --- pre-labels ---------------------------------------------------------------------------------------
def prelabel_target(conn, user: dict, name: str, revision: Optional[int] = None, lock: bool = False) -> dict:
    """The batch and its newest revision, if this account may have it pre-labelled now (predictions.py)."""
    b = _find(conn, name, lock=lock)
    if not _owns(user, b):
        raise DatasetError(403, f'{b["name"]} 不是你的批次；只有上传它的账号能给它跑预标注')
    if b['status'] not in ('open', 'rejected'):
        raise DatasetError(409, f'{b["name"]} 现在是「{STATUS[b["status"]]}」，不能跑预标注；'
                                f'预标注是给还在标的批次用的（待提交复核或已退回）')
    latest = _latest(conn, b)
    _same_revision(b, latest, revision)
    return dict(b, latest_id=latest['id'], revision=latest['number'], ref=f'{b["name"]}@{latest["number"]}')


def note_prelabel(conn, user: dict, b: dict, job_id: int, what: str) -> None:
    """Inside the transaction that queued (or found) the job, with the batch locked: one step per job and
    revision (a later revision with the same images is answered by the same job)."""
    if conn.execute('SELECT 1 FROM batch_steps WHERE batch_id = %s AND job_id = %s AND version_id = %s',
                    (b['id'], job_id, b['latest_id'])).fetchone():
        return
    _step(conn, b['id'], user['user_id'], 'prelabel', b['latest_id'], comment=what, job_id=job_id)
    conn.execute('UPDATE batches SET updated_at = now() WHERE id = %s', (b['id'],))
    events.record(conn, user['user_id'], 'batch.prelabel', b['ref'], job_id=job_id, what=what)


def prelabel_jobs(conn, name: str) -> List[Tuple[int, int]]:
    """(job id, revision) of every pre-label asked for, oldest first."""
    b = _find(conn, name)
    return [(r['job_id'], r['number']) for r in conn.execute(
        'SELECT s.job_id, v.number FROM batch_steps s JOIN versions v ON v.id = s.version_id '
        'WHERE s.batch_id = %s AND s.job_id IS NOT NULL ORDER BY s.id', (b['id'],))]


def pool_of(conn, name: str) -> dict:
    """The batch's pool: name, site, line."""
    b = _find(conn, name)
    return conn.execute('SELECT name, site, line FROM datasets WHERE id = %s', (b['pool_id'],)).fetchone()


# --- into the pool -----------------------------------------------------------------------------------
def _approved(conn, name: str) -> Tuple[dict, dict]:
    b = get(conn, name)
    if b['status'] != 'approved':
        raise DatasetError(409, f'{b["name"]} 现在是「{b["status_text"]}」；复核通过以后才能入库')
    return b, {'dir': b['dir'], 'manifest_sha256': b['manifest_sha256']}


def import_plan(conn, store: Store, name: str, replace: bool = False, allow_other_files: bool = False,
                force: bool = False) -> dict:
    """What adding the approved revision to its pool would make (changes.plan); nothing is created."""
    b, add = _approved(conn, name)
    out = changes.plan(conn, store, b['pool'], [dict(add, replace=replace)], [], allow_other_files, force).summary()
    out['batch'] = {'name': b['name'], 'revision': b['revision'], 'ref': b['ref']}
    return out


def import_(conn, store: Store, user: dict, name: str, expect_latest_id: Optional[int], replace: bool = False,
            allow_other_files: bool = False, force: bool = False, note: str = '', before_freeze=None) -> dict:
    """Add the approved revision to the pool as its folder. The batch becomes 'imported' in the transaction
    that freezes the pool version, so there is never one without the other."""
    b, add = _approved(conn, name)

    def also(c, version_id: int) -> None:
        now = _find(c, name, lock=True)
        latest = _latest(c, now)
        if now['status'] != 'approved' or latest['id'] != b['latest_id']:
            raise DatasetError(409, f'{b["name"]} 在这期间有了变化（{STATUS[now["status"]]}，第 {latest["number"]} 版）；'
                                    f'请重新看一遍再入库')
        c.execute("UPDATE batches SET status = 'imported', imported_version_id = %s, updated_at = now() "
                  'WHERE id = %s', (version_id, now['id']))
        _step(c, now['id'], user['user_id'], 'import', latest['id'], comment=note, pool_version_id=version_id)
        number = c.execute('SELECT number FROM versions WHERE id = %s', (version_id,)).fetchone()['number']
        events.record(c, user['user_id'], 'batch.import', b['ref'], pool_version=f'{b["pool"]}@{number}',
                      dir=b['dir'], replace=replace)

    out = changes.create(conn, store, user['user_id'], b['pool'], [dict(add, replace=replace)], [], expect_latest_id,
                         allow_other_files, force, source=f'batch:{b["ref"]}',
                         note=_text(note, '说明', MAX_COMMENT) or f'批次 {b["name"]} 第 {b["revision"]} 版（复核人 {b["reviewer"]}）',
                         before_freeze=before_freeze, also=also)
    out['batch'] = get(conn, name, user)
    return out


# --- reading -----------------------------------------------------------------------------------------
def list_batches(conn, user: Optional[dict] = None, mine: bool = False, todo: bool = False, status: str = '',
                 pool: str = '') -> List[dict]:
    """Newest change first. mine: the account's own; todo: those waiting for this account's review."""
    where, args = ['true'], []
    if status:
        if status not in STATUS:
            raise DatasetError(400, f'状态只能是 {"、".join(STATUS)}')
        where.append('b.status = %s')
        args.append(status)
    if pool:
        where.append('lower(p.name) = lower(%s)')
        args.append(pool)
    if mine and user is not None:
        where.append('b.owner_id = %s')
        args.append(user['user_id'])
    if todo and user is not None:
        where.append("b.status = 'review' AND b.reviewer_id = %s")
        args.append(user['user_id'])
    rows = conn.execute(_BATCH + ' WHERE ' + ' AND '.join(where) + ' ORDER BY b.updated_at DESC, b.id DESC',
                        args).fetchall()
    return [_row(r, user) for r in rows]


def get(conn, name: str, user: Optional[dict] = None) -> dict:
    row = conn.execute(_BATCH + ' WHERE lower(d.name) = lower(%s)', (name,)).fetchone()
    if row is None:
        raise DatasetError(404, f'没有标注批次 {name}')
    out = _row(row, user)
    numbers: Dict[int, int] = {}
    out['revisions'] = []
    for r in conn.execute(
            'SELECT v.id, v.number, v.file_count, v.total_bytes, v.note, v.source, v.created_at, u.name AS created_by, '
            'br.labeler, br.by_reviewer FROM versions v JOIN batch_revisions br ON br.version_id = v.id '
            'JOIN users u ON u.id = v.created_by WHERE br.batch_id = %s ORDER BY v.number', (row['id'],)):
        numbers[r['id']] = r['number']
        out['revisions'].append({'id': r['id'], 'number': r['number'], 'ref': f'{row["name"]}@{r["number"]}',
                                 'file_count': r['file_count'], 'total_bytes': r['total_bytes'], 'note': r['note'],
                                 'source': r['source'], 'created_at': _stamp(r['created_at']),
                                 'created_by': r['created_by'], 'labeler': r['labeler'],
                                 'by_reviewer': r['by_reviewer']})
    out['steps'] = [
        {'at': _stamp(s['at']), 'actor': s['actor'], 'step': s['step'], 'step_text': STEPS[s['step']],
         'revision': numbers.get(s['version_id']), 'reviewer': s['reviewer'], 'self_review': s['self_review'],
         'comment': s['comment'], 'images': s['images'], 'job_id': s['job_id'],
         'pool_version': f'{row["pool"]}@{s["pool_number"]}' if s['pool_number'] is not None else None}
        for s in conn.execute(
            'SELECT s.at, a.name AS actor, s.step, s.version_id, r.name AS reviewer, s.self_review, s.comment, '
            's.images, s.job_id, pv.number AS pool_number FROM batch_steps s JOIN users a ON a.id = s.actor_id '
            'LEFT JOIN users r ON r.id = s.reviewer_id LEFT JOIN versions pv ON pv.id = s.pool_version_id '
            'WHERE s.batch_id = %s ORDER BY s.id', (row['id'],))]
    return out


def of_version(conn, version_id: int) -> Optional[dict]:
    """The batch a pool version took in (None: it was not made by importing a batch)."""
    row = conn.execute('SELECT d.name, b.dir, v.number FROM batch_steps s JOIN batches b ON b.id = s.batch_id '
                       'JOIN datasets d ON d.id = b.dataset_id JOIN versions v ON v.id = s.version_id '
                       'WHERE s.pool_version_id = %s', (version_id,)).fetchone()
    return None if row is None else {'name': row['name'], 'dir': row['dir'], 'revision': row['number'],
                                     'ref': f'{row["name"]}@{row["number"]}'}


# --- comparing two revisions --------------------------------------------------------------------------
Box = Tuple[str, float, float, float, float]


def _boxes(store: Store, label: Optional[Tuple[str, str, int]]) -> Optional[List[Box]]:
    """The boxes of a label file (JSON: pixels; YOLO txt: the class number and 0..1 coordinates); [] for no
    label; None when the file cannot be read as boxes."""
    if label is None:
        return []
    data = store.read(label[1])
    if data is None:
        return None
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return None
    if label[0].lower().endswith('.json'):
        parsed = formats.parse_json(text)
        if isinstance(parsed, str):
            return None
        return [(b.name, b.x1, b.y1, b.x2, b.y2) for b in parsed.boxes]
    out = []
    for row in text.splitlines():
        fields = row.split()
        if not fields:
            continue
        try:
            cx, cy, w, h = (float(v) for v in fields[1:5])
        except ValueError:
            return None
        if len(fields) != 5:
            return None  # segmentation, rotated boxes, keypoints: not compared box by box
        out.append((fields[0], cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
    return out


def _iou(a: Box, b: Box) -> float:
    w = min(a[3], b[3]) - max(a[1], b[1])
    h = min(a[4], b[4]) - max(a[2], b[2])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    union = (a[3] - a[1]) * (a[4] - a[2]) + (b[3] - b[1]) * (b[4] - b[2]) - inter
    return inter / union if union > 0 else 0.0


def box_changes(old: List[Box], new: List[Box], budget: Optional[List[int]] = None) -> Dict[str, int]:
    """How the boxes of one image changed: added, removed, moved (same class, another place), relabeled
    (another class, with or without moving). Boxes that are the same on both sides are left out; of the rest,
    an old and a new box that overlap by SAME_BOX or more are one box that was changed. budget: [pairs left]
    for the whole comparison; an image with too many pairs, or when nothing is left, gets added / removed only."""
    def key(b: Box):
        return (b[0],) + tuple(round(v, 4) for v in b[1:])

    left: Dict[tuple, int] = {}
    for b in old:
        left[key(b)] = left.get(key(b), 0) + 1
    added = []
    for b in new:
        if left.get(key(b)):
            left[key(b)] -= 1
        else:
            added.append(b)
    removed = []
    for b in old:
        if left.get(key(b)):
            left[key(b)] -= 1
            removed.append(b)
    out = {'added': len(added), 'removed': len(removed), 'moved': 0, 'relabeled': 0}
    work = len(added) * len(removed)
    if not work or work > MAX_PAIRS or (budget is not None and work > budget[0]):
        return out
    if budget is not None:
        budget[0] -= work
    pairs = sorted(((_iou(a, r), i, j) for i, a in enumerate(added) for j, r in enumerate(removed)), reverse=True)
    used_a, used_r = set(), set()
    for overlap, i, j in pairs:
        if overlap < SAME_BOX:
            break
        if i in used_a or j in used_r:
            continue
        used_a.add(i)
        used_r.add(j)
        out['relabeled' if added[i][0] != removed[j][0] else 'moved'] += 1
        out['added'] -= 1
        out['removed'] -= 1
    return out


def compare(conn, store: Store, name: str, old: Optional[int] = None, new: Optional[int] = None) -> dict:
    """What changed between two revisions (default: the newest and the one before it): files, and for every
    image whose label changed, how many boxes were added, removed, moved or given another class."""
    b = _find(conn, name)
    latest = _latest(conn, b)
    new = latest['number'] if new is None else new
    old = new - 1 if old is None else old
    if old < 1 or new < 1 or old == new:
        raise DatasetError(400, '要比较两个不同的版次；第 1 版之前没有可比的')
    a = datasets.get_version(conn, b['name'], str(old))
    z = datasets.get_version(conn, b['name'], str(new))
    before = datasets.version_files(conn, a['id'])
    after = datasets.version_files(conn, z['id'])
    d = datasets.diff(conn, a['id'], z['id'])
    files = {k: {'count': len(d[k]), 'examples': [f'{x} → {y}' for x, y in d[k][:50]] if k == 'renamed' else d[k][:50]}
             for k in ('added', 'modified', 'deleted', 'renamed')}

    def is_image(path: str) -> bool:
        return PurePosixPath(path).suffix.lower() in IMAGE_EXTS

    old_images = {p: h for p, h, _ in before if is_image(p)}
    new_images = {p: h for p, h, _ in after if is_image(p)}
    old_labels, new_labels = changes.label_index(before), changes.label_index(after)
    totals = {'added': 0, 'removed': 0, 'moved': 0, 'relabeled': 0}
    changed: List[dict] = []
    reads = uncompared = 0
    cache: Dict[str, Optional[List[Box]]] = {}
    budget = [MAX_WORK]

    def boxes(label):
        nonlocal reads
        if label is None:
            return []
        if label[1] not in cache:
            reads += 1
            cache[label[1]] = _boxes(store, label)
        return cache[label[1]]

    for image in sorted(set(old_images) & set(new_images)):
        la, lb = changes.label_of(image, old_labels), changes.label_of(image, new_labels)
        same_label = (la[1] if la else None) == (lb[1] if lb else None)
        if same_label and old_images[image] == new_images[image]:
            continue
        row = {'image': image, 'image_changed': old_images[image] != new_images[image],
               'label': (lb or la or (None,))[0], 'old_boxes': None, 'new_boxes': None, 'added': 0, 'removed': 0,
               'moved': 0, 'relabeled': 0, 'unreadable': False}
        if not same_label:
            if reads >= MAX_LABEL_READS:
                uncompared += 1
                continue
            ba, bb = boxes(la), boxes(lb)
            if ba is None or bb is None:
                row['unreadable'] = True
            else:
                delta = box_changes(ba, bb, budget)
                row.update(delta, old_boxes=len(ba), new_boxes=len(bb))
                if not any(delta.values()) and not row['image_changed']:
                    continue  # the file changed (formatting, order, other fields) but not its boxes
                for k in totals:
                    totals[k] += delta[k]
        changed.append(row)
    return {'batch': b['name'], 'old': a['ref'], 'new': z['ref'], 'old_id': a['id'], 'new_id': z['id'], 'files': files,
            'images': {'old': len(old_images), 'new': len(new_images),
                       'added': len(set(new_images) - set(old_images)),
                       'removed': len(set(old_images) - set(new_images)),
                       'changed': len(changed), 'examples': changed[:EXAMPLES]},
            'boxes': totals, 'uncompared': uncompared}
