"""Accounts, roles and tokens. A token is shown once; only its sha256 is stored."""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import List, Optional, Tuple

ROLES = {'admin': '管理员', 'algo': '算法工程师', 'field': '现场实施', 'deploy': '部署', 'worker': 'GPU worker'}
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


class AuthError(ValueError):
    pass


def new_token() -> str:
    return 'fw_' + secrets.token_urlsafe(32)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode('utf-8')).digest()


def _user(conn, name: str, lock: bool = False) -> dict:
    row = conn.execute('SELECT id, name, role, disabled_at FROM users WHERE lower(name) = lower(%s)'
                       + (' FOR SHARE' if lock else ''), (name,)).fetchone()
    if row is None:
        raise AuthError(f'没有账号 {name}')
    return row


def add_user(conn, name: str, role: str) -> dict:
    if not NAME.match(name):
        raise AuthError('账号名只能用字母、数字和 . _ -，以字母或数字开头，最多 64 个字符')
    if role not in ROLES:
        raise AuthError(f'角色只能是 {"、".join(ROLES)}')
    with conn.transaction():
        if conn.execute('SELECT 1 FROM users WHERE lower(name) = lower(%s)', (name,)).fetchone():
            raise AuthError(f'账号 {name} 已经存在')
        return conn.execute('INSERT INTO users (name, role) VALUES (%s, %s) RETURNING id, name, role',
                            (name, role)).fetchone()


def list_users(conn) -> List[dict]:
    return conn.execute(
        'SELECT u.name, u.role, u.created_at, u.disabled_at, '
        '(SELECT count(*) FROM tokens t WHERE t.user_id = u.id AND t.revoked_at IS NULL) AS active_tokens '
        'FROM users u ORDER BY u.name').fetchall()


def set_disabled(conn, name: str, disabled: bool) -> int:
    """Disable (revoking every token at once) or enable an account; returns the number of tokens revoked."""
    with conn.transaction():
        user = _user(conn, name)
        if not disabled:
            conn.execute('UPDATE users SET disabled_at = NULL WHERE id = %s', (user['id'],))
            return 0
        conn.execute('UPDATE users SET disabled_at = coalesce(disabled_at, now()) WHERE id = %s', (user['id'],))
        cur = conn.execute('UPDATE tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL',
                           (user['id'],))
        return cur.rowcount


def create_token(conn, name: str, label: str = '') -> Tuple[str, int]:
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
        return token, row['id']


def list_tokens(conn, name: Optional[str] = None) -> List[dict]:
    sql = ('SELECT t.id, u.name, u.role, t.label, t.created_at, t.last_used_at, t.revoked_at '
           'FROM tokens t JOIN users u ON u.id = t.user_id')
    if name:
        return conn.execute(sql + ' WHERE lower(u.name) = lower(%s) ORDER BY t.id', (name,)).fetchall()
    return conn.execute(sql + ' ORDER BY t.id').fetchall()


def revoke_token(conn, token_id: int) -> bool:
    cur = conn.execute('UPDATE tokens SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL', (token_id,))
    return cur.rowcount == 1


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
