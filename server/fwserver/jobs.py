"""The job queue for GPU workers, worker accounts' runners (inference environments), leases (docs/architecture.md).

A job is queued, then claimed by a worker: the claim takes the oldest queued job the worker can run
(FOR UPDATE SKIP LOCKED, so two workers never get the same one) and gives it a lease with a new random token;
only its sha256 is stored. Renewing the lease, sending log lines and finishing must present that token while
the lease holds. A lease that ran out (the machine died, the network went away) sends the job back to the
queue, or ends it when it has been tried max_attempts times or was cancelled meanwhile; the token of the lost
claim is then refused, so a worker that comes back cannot change a job that was given to another one.

Cancelling a queued job ends it at once; a running one is told on its next lease renewal, kills what it runs
and reports 'cancelled'. A finished job never changes (the database refuses it).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple

from .datasets import NAME, DatasetError, check_digest
from .store import Store, StoreError

KINDS = {'check': '检查推理环境', 'eval': '评估'}
FRAMEWORKS = ('ultralytics', 'rfdetr')  # the aoi-deteval predict adapters
STATUSES = {'queued': '排队', 'running': '运行中', 'done': '完成', 'failed': '失败', 'cancelled': '已取消'}
OUTCOMES = ('done', 'failed', 'cancelled')
COMMIT = re.compile(r'[0-9a-f]{7,64}\Z')
CONDA_ENV = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z')
MAX_LOG_PIECE = 256 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024  # per attempt; later pieces are dropped and the log says so
MAX_RESULT = 1 << 20
MAX_ERROR = 20000
LOG_CUT = '\n[日志超过 8 MB，后面的内容没有保存]\n'


def _stamp(t) -> Optional[str]:
    return t.isoformat(timespec='seconds') if t is not None else None


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode('utf-8')).digest()


def _text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str):
        raise DatasetError(400, f'{what}应为文本')
    if len(value) > limit:
        raise DatasetError(400, f'{what}不能超过 {limit} 个字符')
    if '\x00' in value:
        raise DatasetError(400, f'{what}里有不能用的字符')
    return value


# --- runners ------------------------------------------------------------------------------------------
def _runner_version(r: dict) -> dict:
    return {'id': r['id'], 'runner': r['name'], 'number': r['number'], 'ref': f'{r["name"]}@{r["number"]}',
            'worker': r['worker'], 'conda_env': r['conda_env'], 'env_sha256': bytes(r['env_sha256']).hex(),
            'src_dir': r['src_dir'], 'src_commit': r['src_commit'], 'src_dirty': r['src_dirty'],
            'src_state_sha256': bytes(r['src_state_sha256']).hex() if r['src_state_sha256'] else None,
            'framework': r['framework'], 'registered_by': r['registered_by'], 'created_at': _stamp(r['created_at'])}


_RV = ('SELECT rv.*, r.name, w.name AS worker, u.name AS registered_by FROM runner_versions rv '
       'JOIN runners r ON r.id = rv.runner_id JOIN users w ON w.id = rv.worker_id '
       'JOIN users u ON u.id = rv.registered_by ')


def list_runners(conn) -> List[dict]:
    """Every runner with its newest version."""
    rows = conn.execute(_RV + 'WHERE rv.number = (SELECT max(number) FROM runner_versions x '
                        'WHERE x.runner_id = rv.runner_id) ORDER BY lower(r.name)').fetchall()
    return [_runner_version(r) for r in rows]


def runner_versions(conn, name: str) -> List[dict]:
    rows = conn.execute(_RV + 'WHERE lower(r.name) = lower(%s) ORDER BY rv.number', (name,)).fetchall()
    if not rows:
        raise DatasetError(404, f'没有推理环境 {name}；在 GPU 机上用 fw runner register 登记')
    return [_runner_version(r) for r in rows]


def get_runner_version(conn, name: str, number: str = 'latest') -> dict:
    versions = runner_versions(conn, name)
    if number == 'latest':
        return versions[-1]
    for v in versions:
        if str(v['number']) == str(number):
            return v
    raise DatasetError(404, f'没有 {name}@{number}（最新是 @{versions[-1]["number"]}）')


def get_runner_version_by_id(conn, rv_id: int) -> dict:
    row = conn.execute(_RV + 'WHERE rv.id = %s', (rv_id,)).fetchone()
    if row is None:
        raise DatasetError(404, f'没有推理环境版本 {rv_id}')
    return _runner_version(row)


def register_runner(conn, store: Store, worker: dict, name: str, conda_env: str, env_sha256: str,
                    src_dir: str = '', src_commit: Optional[str] = None, src_dirty: bool = False,
                    src_state_sha256: Optional[str] = None, framework: str = '') -> dict:
    """A new version of the runner when anything differs from its newest one (another machine, another
    environment listing, another source commit); the newest one again otherwise. Only worker accounts
    register: what is recorded is what that machine has."""
    if not NAME.match(name or ''):
        raise DatasetError(400, '推理环境名只能用字母、数字和 . _ -，以字母或数字开头，最多 64 个字符')
    if not CONDA_ENV.match(conda_env or ''):
        raise DatasetError(400, f'conda 环境名不对：{conda_env!r}')
    env_sha256 = check_digest(env_sha256)
    if store.size(env_sha256) is None:
        raise DatasetError(400, '环境的包清单（conda list --explicit 和 pip freeze）还没上传')
    src_dir = _text(src_dir or '', '源码目录', 1000).strip()
    if any(c in src_dir for c in '\r\n\t'):
        raise DatasetError(400, '源码目录里有不能用的字符')
    if src_commit is not None:
        src_commit = src_commit.strip().lower()
        if not COMMIT.match(src_commit):
            raise DatasetError(400, '源码提交号应为 7 到 64 位十六进制')
    if not src_dir and (src_commit or src_dirty):
        raise DatasetError(400, '没有源码目录时不能有提交号')
    src_dirty = bool(src_dirty)
    if src_state_sha256 is not None:
        src_state_sha256 = check_digest(src_state_sha256)
    if src_dirty != (src_state_sha256 is not None):
        raise DatasetError(400, '源码有未提交的改动时要带上改动的哈希，没有改动时不带')
    try:
        store.flush()  # the listing is durable before any record points at it
    except StoreError as e:
        raise DatasetError(503, str(e)) from None
    state = bytes.fromhex(src_state_sha256) if src_state_sha256 else None
    framework = framework or ''
    if framework and framework not in FRAMEWORKS:
        raise DatasetError(400, f'框架只能是 {"、".join(FRAMEWORKS)}（aoi-deteval predict 的 --framework）')
    with conn.transaction():
        conn.execute('INSERT INTO runners (name) VALUES (%s) ON CONFLICT DO NOTHING', (name,))
        runner = conn.execute('SELECT id, name FROM runners WHERE lower(name) = lower(%s) FOR UPDATE',
                              (name,)).fetchone()
        last = conn.execute('SELECT * FROM runner_versions WHERE runner_id = %s ORDER BY number DESC LIMIT 1',
                            (runner['id'],)).fetchone()
        same = last is not None and (last['worker_id'], last['conda_env'], bytes(last['env_sha256']).hex(),
                                     last['src_dir'], last['src_commit'], last['src_dirty'],
                                     bytes(last['src_state_sha256']) if last['src_state_sha256'] else None,
                                     last['framework']) == (
            worker['user_id'], conda_env, env_sha256, src_dir, src_commit, src_dirty, state, framework)
        if same:
            return dict(get_runner_version_by_id(conn, last['id']), created=False)
        row = conn.execute(
            'INSERT INTO runner_versions (runner_id, number, worker_id, conda_env, env_sha256, src_dir, src_commit, '
            'src_dirty, src_state_sha256, framework, registered_by) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
            (runner['id'], (last['number'] if last else 0) + 1, worker['user_id'], conda_env,
             bytes.fromhex(env_sha256), src_dir, src_commit, src_dirty, state, framework,
             worker['user_id'])).fetchone()
    return dict(get_runner_version_by_id(conn, row['id']), created=True)


# --- jobs ---------------------------------------------------------------------------------------------
_JOB = ('SELECT j.*, s.name AS submitted_by_name, w.name AS worker_name FROM jobs j '
        'JOIN users s ON s.id = j.submitted_by LEFT JOIN users w ON w.id = j.worker_id ')


def _job(conn, r: dict) -> dict:
    ahead = None
    if r['status'] == 'queued':
        ahead = conn.execute("SELECT count(*) AS n FROM jobs WHERE status = 'queued' AND id < %s",
                             (r['id'],)).fetchone()['n']
    return {'id': r['id'], 'kind': r['kind'], 'kind_text': KINDS.get(r['kind'], r['kind']), 'params': r['params'],
            'runner_version_id': r['runner_version_id'],
            'runner': (get_runner_version_by_id(conn, r['runner_version_id'])['ref']
                       if r['runner_version_id'] is not None else None),
            'status': r['status'], 'status_text': STATUSES[r['status']], 'queued_ahead': ahead,
            'submitted_by': r['submitted_by_name'], 'created_at': _stamp(r['created_at']),
            'attempts': r['attempts'], 'max_attempts': r['max_attempts'], 'worker': r['worker_name'],
            'lease_expires': _stamp(r['lease_expires']), 'cancel_requested': r['cancel_requested'],
            'started_at': _stamp(r['started_at']), 'finished_at': _stamp(r['finished_at']),
            'result': r['result'], 'error': r['error']}


def expire_leases(conn) -> int:
    """Lost claims: back to the queue, or ended (cancelled meanwhile, or tried max_attempts times)."""
    n = 0
    with conn.transaction():
        rows = conn.execute("SELECT id, attempts, max_attempts, cancel_requested FROM jobs "
                            "WHERE status = 'running' AND lease_expires < clock_timestamp() "
                            "FOR UPDATE SKIP LOCKED").fetchall()
        for r in rows:
            conn.execute("UPDATE job_attempts SET ended_at = now(), outcome = 'lost' "
                         "WHERE job_id = %s AND attempt = %s AND outcome IS NULL", (r['id'], r['attempts']))
            if r['cancel_requested']:
                status, error = 'cancelled', None
            elif r['attempts'] >= r['max_attempts']:
                status, error = 'failed', f'领取了 {r["attempts"]} 次，每次 worker 都在租约到期前失联（掉线、死机或被停掉）'
            else:
                status, error = 'queued', None
            conn.execute("UPDATE jobs SET status = %s, error = %s, worker_id = NULL, lease_sha256 = NULL, "
                         "lease_expires = NULL, finished_at = CASE WHEN %s = 'queued' THEN NULL ELSE now() END "
                         "WHERE id = %s", (status, error, status, r['id']))
            n += 1
    return n


def get_job(conn, job_id: int) -> dict:
    expire_leases(conn)
    row = conn.execute(_JOB + 'WHERE j.id = %s', (job_id,)).fetchone()
    if row is None:
        raise DatasetError(404, f'没有任务 {job_id}')
    tries = conn.execute('SELECT a.attempt, u.name AS worker, a.started_at, a.ended_at, a.outcome FROM job_attempts a '
                         'JOIN users u ON u.id = a.worker_id WHERE a.job_id = %s ORDER BY a.attempt',
                         (job_id,)).fetchall()
    return dict(_job(conn, row), history=[{'attempt': t['attempt'], 'worker': t['worker'],
                                           'started_at': _stamp(t['started_at']), 'ended_at': _stamp(t['ended_at']),
                                           'outcome': t['outcome']} for t in tries])


def list_jobs(conn, status: str = '', limit: int = 50) -> List[dict]:
    expire_leases(conn)
    if status and status not in STATUSES:
        raise DatasetError(400, f'状态只能是 {"、".join(STATUSES)}')
    limit = max(1, min(int(limit), 1000))
    if status:
        rows = conn.execute(_JOB + 'WHERE j.status = %s ORDER BY j.id DESC LIMIT %s', (status, limit)).fetchall()
    else:
        rows = conn.execute(_JOB + 'ORDER BY j.id DESC LIMIT %s', (limit,)).fetchall()
    return [_job(conn, r) for r in rows]


def runner_params(rv: dict) -> dict:
    """What a job needs to know of its runner version: the worker checks the machine still has it."""
    return {'conda_env': rv['conda_env'], 'env_sha256': rv['env_sha256'], 'src_dir': rv['src_dir'],
            'src_commit': rv['src_commit'], 'src_dirty': rv['src_dirty'],
            'src_state_sha256': rv['src_state_sha256'], 'framework': rv['framework']}


def find_runner(conn, runner: Optional[str], what: str) -> dict:
    if not runner:
        raise DatasetError(400, f'要写{what}用哪个推理环境（fw runner list 看有哪些）')
    name, _, number = runner.partition('@')
    return get_runner_version(conn, name, number or 'latest')


def enqueue(conn, user: dict, kind: str, rv: Optional[dict], params: dict) -> dict:
    if kind not in KINDS:
        raise DatasetError(400, f'任务类型只能是 {"、".join(KINDS)}')
    row = conn.execute('INSERT INTO jobs (kind, params, runner_version_id, submitted_by) VALUES (%s, %s, %s, %s) '
                       'RETURNING id', (kind, json.dumps(params, ensure_ascii=False), rv['id'] if rv else None,
                                        user['user_id'])).fetchone()
    return get_job(conn, row['id'])


def submit(conn, user: dict, kind: str, runner: Optional[str] = None, params: Optional[dict] = None) -> dict:
    """A job that needs nothing but its runner (a check). Evaluations are submitted through evals.submit."""
    if kind != 'check':
        raise DatasetError(400, f'任务类型只能是 {"、".join(KINDS)}')
    if params:
        raise DatasetError(400, '检查推理环境不带参数')
    rv = find_runner(conn, runner, '检查')
    return enqueue(conn, user, kind, rv, runner_params(rv))


def cancel(conn, job_id: int) -> dict:
    with conn.transaction():
        row = conn.execute('SELECT status FROM jobs WHERE id = %s FOR UPDATE', (job_id,)).fetchone()
        if row is None:
            raise DatasetError(404, f'没有任务 {job_id}')
        if row['status'] == 'queued':
            conn.execute("UPDATE jobs SET status = 'cancelled', cancel_requested = true, finished_at = now() "
                         "WHERE id = %s", (job_id,))
        elif row['status'] == 'running':
            conn.execute('UPDATE jobs SET cancel_requested = true WHERE id = %s', (job_id,))
        elif row['status'] == 'cancelled':
            pass  # cancelled already (a retried request, or the worker stopped it): nothing to do
        else:
            raise DatasetError(409, f'任务 {job_id} 已经{STATUSES[row["status"]]}，不能取消')
    return get_job(conn, job_id)


def claim(conn, worker: dict, lease_seconds: int) -> Optional[dict]:
    """The oldest queued job this worker can run, now running under a new lease; None when there is none.
    Jobs bound to a runner go only to the worker the runner version is on."""
    expire_leases(conn)
    token = secrets.token_urlsafe(32)
    with conn.transaction():
        row = conn.execute(
            "SELECT j.id, j.attempts FROM jobs j LEFT JOIN runner_versions rv ON rv.id = j.runner_version_id "
            "WHERE j.status = 'queued' AND (j.runner_version_id IS NULL OR rv.worker_id = %s) "
            "ORDER BY j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED", (worker['user_id'],)).fetchone()
        if row is None:
            return None
        attempt = row['attempts'] + 1
        conn.execute("UPDATE jobs SET status = 'running', attempts = %s, worker_id = %s, lease_sha256 = %s, "
                     "lease_expires = clock_timestamp() + make_interval(secs => %s), "
                     "started_at = coalesce(started_at, now()) "
                     "WHERE id = %s", (attempt, worker['user_id'], _hash(token), lease_seconds, row['id']))
        conn.execute('INSERT INTO job_attempts (job_id, attempt, worker_id, lease_sha256) VALUES (%s, %s, %s, %s)',
                     (row['id'], attempt, worker['user_id'], _hash(token)))
    return dict(get_job(conn, row['id']), token=token, attempt=attempt, lease_seconds=lease_seconds)


def _held(conn, job_id: int, token: str) -> dict:
    """The job row, locked, if this token's claim still holds its lease; 409 otherwise. The lease is compared
    with the clock after the lock is taken (clock_timestamp, not the transaction's start, now()): a request that
    waited for the lock past the end of the lease is refused."""
    row = conn.execute('SELECT id, kind, params, runner_version_id, submitted_by, status, attempts, lease_sha256, '
                       'lease_expires, cancel_requested FROM jobs WHERE id = %s FOR UPDATE', (job_id,)).fetchone()
    if row is None:
        raise DatasetError(404, f'没有任务 {job_id}')
    expired = row['lease_expires'] is None or conn.execute(
        'SELECT %s::timestamptz <= clock_timestamp() AS e', (row['lease_expires'],)).fetchone()['e']
    if row['status'] != 'running' or bytes(row['lease_sha256']) != _hash(token or '') or expired:
        raise DatasetError(409, f'任务 {job_id} 的这次领取已经失效（租约过期、任务已结束或已交给别的 worker），'
                                f'不再接受它的结果')
    return row


def renew(conn, job_id: int, token: str, lease_seconds: int) -> dict:
    with conn.transaction():
        row = _held(conn, job_id, token)
        conn.execute('UPDATE jobs SET lease_expires = clock_timestamp() + make_interval(secs => %s) WHERE id = %s',
                     (lease_seconds, job_id))
    return {'cancel': row['cancel_requested'], 'lease_seconds': lease_seconds}


def add_log(conn, job_id: int, token: str, seq: int, text: str) -> dict:
    """One piece of output; sending the same seq again (a retry) changes nothing."""
    text = _text(text, '日志', MAX_LOG_PIECE)
    if not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq < 1 << 30:
        raise DatasetError(400, '日志序号不对')
    with conn.transaction():
        row = _held(conn, job_id, token)
        used = conn.execute('SELECT coalesce(sum(length(text)), 0) AS n, count(*) AS c FROM job_logs '
                            'WHERE job_id = %s AND attempt = %s', (job_id, row['attempts'])).fetchone()
        if used['n'] >= MAX_LOG_BYTES:
            return {'stored': False}
        if used['n'] + len(text) > MAX_LOG_BYTES:
            text = text[:MAX_LOG_BYTES - used['n']] + LOG_CUT
        cur = conn.execute('INSERT INTO job_logs (job_id, attempt, seq, text) VALUES (%s, %s, %s, %s) '
                           'ON CONFLICT DO NOTHING', (job_id, row['attempts'], seq, text))
    return {'stored': cur.rowcount == 1}


def job_log(conn, job_id: int, attempt: Optional[int] = None, after: int = -1) -> dict:
    job = get_job(conn, job_id)
    attempt = attempt or job['attempts']
    rows = conn.execute('SELECT seq, at, text FROM job_logs WHERE job_id = %s AND attempt = %s AND seq > %s '
                        'ORDER BY seq', (job_id, attempt, after)).fetchall()
    return {'job': job_id, 'attempt': attempt, 'status': job['status'],
            'pieces': [{'seq': r['seq'], 'at': _stamp(r['at']), 'text': r['text']} for r in rows]}


def _check_result(kind: str, result: Any) -> dict:
    if not isinstance(result, dict):
        raise DatasetError(400, '任务结果应为 JSON 对象')
    if len(json.dumps(result, ensure_ascii=False)) > MAX_RESULT:
        raise DatasetError(400, '任务结果太大')
    if kind == 'eval':
        if not isinstance(result.get('eval_id'), int) or isinstance(result['eval_id'], bool):
            raise DatasetError(400, '评估结果里要有评估报告的编号')
    if kind == 'check':
        if not isinstance(result.get('env_sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', result['env_sha256']):
            raise DatasetError(400, '检查结果里要有环境包清单的哈希')
        for key in ('same_env', 'same_src'):
            if not isinstance(result.get(key), bool):
                raise DatasetError(400, f'检查结果里的 {key} 应为 true 或 false')
    return result


def finished_already(conn, job_id: int, token: str, outcome: str) -> bool:
    """Did this claim already end the job, with this outcome or cancelled (a finish sent again after a network
    error)? In a transaction; locks the job's row first, so that a second finish of the same claim waiting for it
    then sees the first one's record."""
    conn.execute('SELECT 1 FROM jobs WHERE id = %s FOR UPDATE', (job_id,))
    done = conn.execute('SELECT a.outcome FROM job_attempts a JOIN jobs j ON j.id = a.job_id '
                        'WHERE a.job_id = %s AND a.lease_sha256 = %s AND j.status = a.outcome',
                        (job_id, _hash(token or ''))).fetchone()
    return done is not None and done['outcome'] in (outcome, 'cancelled')


def held(conn, job_id: int, token: str) -> dict:
    """In a transaction: the job, locked until it ends, while this claim holds its lease (409 otherwise)."""
    return _held(conn, job_id, token)


def finish(conn, job_id: int, token: str, outcome: str, result: Any = None, error: str = '') -> dict:
    """Ends the job as done (with its result), failed (with why) or cancelled. A job whose cancel was accepted
    ends cancelled whatever the worker sends. Sending the same finish again after a network error returns the
    job instead of an error."""
    if outcome not in OUTCOMES:
        raise DatasetError(400, f'结果只能是 {"、".join(OUTCOMES)}')
    error = _text(error or '', '出错原因', MAX_ERROR)
    with conn.transaction():
        if finished_already(conn, job_id, token, outcome):
            return get_job(conn, job_id)  # this claim already finished the job (like this, or cancelled)
        row = _held(conn, job_id, token)
        kind = row['kind']
        if outcome == 'done':
            result = _check_result(kind, result)
        elif result is not None:
            raise DatasetError(400, '只有完成的任务带结果')
        if outcome == 'failed' and not error.strip():
            raise DatasetError(400, '失败的任务要写原因')
        if row['cancel_requested'] and outcome != 'cancelled':
            # the cancel was accepted before the result came: it stands (a failure keeps its reason)
            outcome, result = 'cancelled', None
        conn.execute('UPDATE job_attempts SET ended_at = now(), outcome = %s WHERE job_id = %s AND attempt = %s',
                     (outcome, job_id, row['attempts']))
        conn.execute('UPDATE jobs SET status = %s, result = %s, error = %s, finished_at = now(), worker_id = NULL, '
                     'lease_sha256 = NULL, lease_expires = NULL WHERE id = %s',
                     (outcome, json.dumps(result, ensure_ascii=False) if result is not None else None,
                      error or None, job_id))
    return get_job(conn, job_id)


def verify_inputs(conn) -> Tuple[List[str], Dict[str, Tuple[Optional[int], str]], int]:
    """For datasets.verify (fw-server verify): the package listings runner versions point at."""
    wanted: Dict[str, Tuple[Optional[int], str]] = {}
    rows = conn.execute("SELECT encode(rv.env_sha256, 'hex') AS d, r.name || '@' || rv.number AS ref "
                        'FROM runner_versions rv JOIN runners r ON r.id = rv.runner_id ORDER BY rv.id').fetchall()
    for r in rows:
        wanted.setdefault(r['d'], (None, f'推理环境 {r["ref"]} 的包清单'))
    return [], wanted, len(rows)
