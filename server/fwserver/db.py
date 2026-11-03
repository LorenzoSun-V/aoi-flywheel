"""Database access: connections, the per-process pool, and the numbered SQL migrations."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, List, Tuple

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

MIGRATIONS = Path(__file__).with_name('migrations')
_FILE = re.compile(r'(\d{4})_[a-z0-9_]+\.sql\Z')
_LOCK = 7340001  # advisory lock key held while migrating


class SchemaError(RuntimeError):
    pass


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def make_pool(dsn: str, size: int) -> ConnectionPool:
    return ConnectionPool(dsn, min_size=1, max_size=size, open=True, name='fw',
                          kwargs={'autocommit': True, 'row_factory': dict_row},
                          check=ConnectionPool.check_connection)


def migration_files() -> List[Tuple[int, str, str]]:
    out = []
    for path in sorted(MIGRATIONS.iterdir()):
        m = _FILE.match(path.name)
        if m:
            out.append((int(m.group(1)), path.name, path.read_text(encoding='utf-8')))
    if [n for n, _, _ in out] != list(range(1, len(out) + 1)):
        raise SchemaError('迁移文件的编号要从 0001 开始连续')
    return out


def _digest(sql: str) -> str:
    return hashlib.sha256(sql.encode('utf-8')).hexdigest()


def _applied(conn) -> Dict[int, Tuple[str, str]]:
    if conn.execute("SELECT to_regclass('schema_migrations') AS t").fetchone()['t'] is None:
        return {}
    rows = conn.execute('SELECT version, name, sha256 FROM schema_migrations').fetchall()
    return {r['version']: (r['name'], r['sha256']) for r in rows}


def schema_problems(conn) -> List[str]:
    """Why this code must not run against this database; empty when the schema is exactly as expected."""
    known = migration_files()
    applied = _applied(conn)
    problems = []
    for n, name, sql in known:
        if n not in applied:
            problems.append(f'数据库还没有应用 {name}：先运行 fw-server migrate')
        elif applied[n][1] != _digest(sql):
            problems.append(f'{name} 在应用到数据库以后被改过')
    newer = sorted(set(applied) - {n for n, _, _ in known})
    if newer:
        problems.append(f'数据库比这个版本的程序新（已经应用到 {applied[newer[-1]][0]}），请换用新版程序')
    return problems


def migrate(dsn: str) -> List[str]:
    """Apply the migrations not yet applied, each in its own transaction; returns their names."""
    done = []
    with connect(dsn) as conn:
        with conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (_LOCK,))
            conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version int PRIMARY KEY, '
                         'name text NOT NULL, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())')
        for n, name, sql in migration_files():
            with conn.transaction():
                conn.execute('SELECT pg_advisory_xact_lock(%s)', (_LOCK,))
                row = conn.execute('SELECT sha256 FROM schema_migrations WHERE version = %s', (n,)).fetchone()
                if row is not None:
                    if row['sha256'] != _digest(sql):
                        raise SchemaError(f'{name} 在应用到数据库以后被改过')
                    continue
                conn.execute(sql)
                conn.execute('INSERT INTO schema_migrations (version, name, sha256) VALUES (%s, %s, %s)',
                             (n, name, _digest(sql)))
                done.append(name)
        problems = schema_problems(conn)
        if problems:
            raise SchemaError('；'.join(problems))
    return done
