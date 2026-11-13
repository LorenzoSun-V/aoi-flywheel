"""Accounts, roles, what each role may do, and tokens. A token is shown once; only its sha256 is stored.

What a role may do is one table (CAPS): the HTTP interface asks allowed(role, capability) and nothing else,
so a new kind of object or a new role is a change here, not a search through the endpoints. Every change to
an account or a token is recorded (events.py) in the same transaction; actor_id None means it was done with
fw-server on the server.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import List, Optional, Tuple

from . import events

ROLES = {'admin': '管理员', 'algo': '算法工程师', 'field': '现场实施', 'worker': 'GPU worker'}
PEOPLE = ('admin', 'algo', 'field')  # roles of people; worker is the account of a GPU machine

# capability -> (roles that have it, what it lets them do, for the refusal message)
CAPS = {
    'datasets.read': (('admin', 'algo', 'field'), '查看数据集'),
    'datasets.write': (('admin', 'algo'), '新建或修改数据集、训练集快照'),
    'objects.upload': (('admin', 'algo', 'field', 'worker'), '上传文件'),
    'objects.download': (('admin', 'algo', 'field', 'worker'), '下载文件'),
    'batches.read': (('admin', 'algo', 'field'), '查看标注批次'),
    'batches.write': (('admin', 'algo', 'field'), '上传标注批次、提交复核'),
    'batches.review': (('admin', 'algo'), '复核标注批次；复核由算法工程师账号做'),
    'models.read': (('admin', 'algo', 'field'), '查看模型、评估和发布'),
    'models.write': (('admin', 'algo'), '登记模型和评估'),
    'releases.write': (('admin', 'field'), '登记发布；发布由现场实施账号在部署成功后登记'),
    'jobs.read': (('admin', 'algo', 'worker'), '查看任务'),
    'jobs.write': (('admin', 'algo'), '提交或取消任务'),
    'jobs.run': (('worker',), '登记推理环境、领取和交回任务；只有 worker 账号（GPU 机）能做'),
    'labels.retire': (('admin',), '撤销版本号；只有管理员能做'),
    'events.read': (('admin', 'algo'), '查看操作记录'),
    'users.manage': (('admin',), '管理账号'),
}


def allowed(role: str, cap: str) -> bool:
    return role in CAPS[cap][0]


def caps_of(role: str) -> List[str]:
    return sorted(c for c in CAPS if allowed(role, c))
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


class AuthError(ValueError):
    pass


def new_token() -> str:
    return 'fw_' + secrets.token_urlsafe(32)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode('utf-8')).digest()


def _user(conn, name: str, lock: bool = False, update: bool = False) -> dict:
    row = conn.execute('SELECT id, name, role, disabled_at FROM users WHERE lower(name) = lower(%s)'
                       + (' FOR UPDATE' if update else ' FOR SHARE' if lock else ''), (name,)).fetchone()
    if row is None:
        raise AuthError(f'没有账号 {name}')
    return row


def get_user(conn, name: str) -> dict:
    return _user(conn, name)


def add_user(conn, name: str, role: str, actor_id: Optional[int] = None) -> dict:
    if not NAME.match(name):
        raise AuthError('账号名只能用字母、数字和 . _ -，以字母或数字开头，最多 64 个字符')
    if role not in ROLES:
        raise AuthError(f'角色只能是 {"、".join(ROLES)}')
    with conn.transaction():
        if conn.execute('SELECT 1 FROM users WHERE lower(name) = lower(%s)', (name,)).fetchone():
            raise AuthError(f'账号 {name} 已经存在')
        user = conn.execute('INSERT INTO users (name, role) VALUES (%s, %s) RETURNING id, name, role',
                            (name, role)).fetchone()
        events.record(conn, actor_id, 'user.create', user['name'], role=role)
        return user


def _keep_an_admin(conn, user: dict) -> None:
    """Refuse to take away the last working administrator (nobody could manage accounts from the web then)."""
    if user['role'] != 'admin' or user['disabled_at'] is not None:
        return
    # the same lock for every such change, so two of them cannot each see the other as the one that stays
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('fw-last-admin', 0))")
    others = conn.execute("SELECT count(*) AS n FROM users WHERE role = 'admin' AND disabled_at IS NULL "
                          'AND id <> %s', (user['id'],)).fetchone()['n']
    if not others:
        raise AuthError(f'{user["name"]} 是唯一在用的管理员账号，先建好另一个管理员')


def set_role(conn, name: str, role: str, actor_id: Optional[int] = None) -> dict:
    """Change what an account may do, from its next request on. Its tokens stay valid. A worker account is a
    machine's and stays one (and a person's account does not become one): runners are registered to it."""
    if role not in ROLES:
        raise AuthError(f'角色只能是 {"、".join(ROLES)}')
    with conn.transaction():
        user = _user(conn, name, update=True)
        if user['role'] == role:
            return user
        if 'worker' in (user['role'], role):
            raise AuthError('worker 账号是 GPU 机用的，不和人的角色互换；需要的话另建一个账号')
        _keep_an_admin(conn, user)
        conn.execute('UPDATE users SET role = %s WHERE id = %s', (role, user['id']))
        events.record(conn, actor_id, 'user.role', user['name'], **{'from': user['role'], 'to': role})
        return dict(user, role=role)


def list_users(conn) -> List[dict]:
    return conn.execute(
        'SELECT u.id, u.name, u.role, u.created_at, u.disabled_at, '
        '(SELECT count(*) FROM tokens t WHERE t.user_id = u.id AND t.revoked_at IS NULL) AS active_tokens '
        'FROM users u ORDER BY u.name').fetchall()


def names_with(conn, cap: str) -> List[str]:
    """The enabled accounts that have a capability, by name (e.g. who can be asked to review a batch)."""
    return [r['name'] for r in conn.execute('SELECT name FROM users WHERE disabled_at IS NULL AND role = ANY(%s) '
                                            'ORDER BY lower(name)', (list(CAPS[cap][0]),))]


def set_disabled(conn, name: str, disabled: bool, actor_id: Optional[int] = None) -> int:
    """Disable (revoking every token at once) or enable an account; returns the number of tokens revoked."""
    with conn.transaction():
        user = _user(conn, name, update=True)
        if not disabled:
            if user['disabled_at'] is not None:
                conn.execute('UPDATE users SET disabled_at = NULL WHERE id = %s', (user['id'],))
                events.record(conn, actor_id, 'user.enable', user['name'])
            return 0
        _keep_an_admin(conn, user)
        conn.execute('UPDATE users SET disabled_at = coalesce(disabled_at, now()) WHERE id = %s', (user['id'],))
        cur = conn.execute('UPDATE tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL',
                           (user['id'],))
        if user['disabled_at'] is None or cur.rowcount:
            events.record(conn, actor_id, 'user.disable', user['name'], tokens_revoked=cur.rowcount)
        return cur.rowcount


def create_token(conn, name: str, label: str = '', actor_id: Optional[int] = None) -> Tuple[str, int]:
    if len(label) > 200 or any(ord(ch) < 32 for ch in label):
        raise AuthError('说明写一行，最多 200 个字符')
    with conn.transaction():
        # FOR SHARE holds the account row until the new token is committed. A disable (UPDATE users) that
        # starts meanwhile waits for it and then revokes it with the others; a disable that came first makes
        # this wait, and then the row read here already says disabled.
        user = _user(conn, name, lock=True)
        if user['disabled_at'] is not None:
            raise AuthError(f'账号 {name} 已停用')
        token = new_token()
        row = conn.execute('INSERT INTO tokens (user_id, token_sha256, label) VALUES (%s, %s, %s) RETURNING id',
                           (user['id'], token_hash(token), label)).fetchone()
        events.record(conn, actor_id, 'token.create', user['name'], token_id=row['id'], label=label)
        return token, row['id']


def list_tokens(conn, name: Optional[str] = None) -> List[dict]:
    sql = ('SELECT t.id, u.name, u.role, t.label, t.created_at, t.last_used_at, t.revoked_at '
           'FROM tokens t JOIN users u ON u.id = t.user_id')
    if name:
        return conn.execute(sql + ' WHERE lower(u.name) = lower(%s) ORDER BY t.id', (name,)).fetchall()
    return conn.execute(sql + ' ORDER BY t.id').fetchall()


def revoke_token(conn, token_id: int, actor_id: Optional[int] = None, owner_id: Optional[int] = None) -> bool:
    """owner_id: revoke it only if it is this account's (someone revoking a token of their own)."""
    with conn.transaction():
        row = conn.execute('UPDATE tokens t SET revoked_at = now() FROM users u WHERE t.id = %s AND u.id = t.user_id '
                           'AND t.revoked_at IS NULL AND (%s::bigint IS NULL OR t.user_id = %s) '
                           'RETURNING u.name, t.label', (token_id, owner_id, owner_id)).fetchone()
        if row is None:
            return False
        events.record(conn, actor_id, 'token.revoke', row['name'], token_id=token_id, label=row['label'])
        return True


def authenticate(conn, token: str) -> Optional[dict]:
    """The account behind a live token, or None (unknown, revoked, or the account is disabled)."""
    return conn.execute(
        'SELECT u.id AS user_id, u.name, u.role, t.id AS token_id, t.label AS token_label, t.last_used_at '
        'FROM tokens t JOIN users u ON u.id = t.user_id '
        'WHERE t.token_sha256 = %s AND t.revoked_at IS NULL AND u.disabled_at IS NULL',
        (token_hash(token),)).fetchone()


def touch(conn, token_id: int) -> None:
    conn.execute("UPDATE tokens SET last_used_at = now() WHERE id = %s "
                 "AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')", (token_id,))
