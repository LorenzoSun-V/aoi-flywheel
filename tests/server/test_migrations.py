import uuid

import psycopg
import pytest

from fwserver import db

from conftest import ADMIN_DSN, _dsn_for


@pytest.fixture
def empty_db():
    name = f'fwmig_{uuid.uuid4().hex[:8]}'
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f'CREATE DATABASE {name}')
    yield _dsn_for(name)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f'DROP DATABASE {name} WITH (FORCE)')


def test_migrate_once_then_nothing(empty_db):
    assert db.migrate(empty_db) == ['0001_init.sql', '0002_trainsets.sql', '0003_models.sql', '0004_optional_code.sql',
                                    '0005_jobs.sql', '0006_eval_jobs.sql', '0007_predictions.sql',
                                    '0008_version_changes.sql', '0009_events_roles.sql', '0010_batches.sql',
                                    '0011_batch_prelabel.sql', '0012_labels.sql', '0013_sites.sql']
    assert db.migrate(empty_db) == []
    with db.connect(empty_db) as c:
        assert db.schema_problems(c) == []


def test_edited_or_newer_schema_is_refused(empty_db):
    db.migrate(empty_db)
    with db.connect(empty_db) as c:
        c.execute("UPDATE schema_migrations SET sha256 = 'x' WHERE version = 1")
        assert any('被改过' in p for p in db.schema_problems(c))
        with pytest.raises(db.SchemaError):
            db.migrate(empty_db)
        c.execute("UPDATE schema_migrations SET sha256 = %s WHERE version = 1",
                  (db._digest(db.migration_files()[0][2]),))
        c.execute("INSERT INTO schema_migrations (version, name, sha256) VALUES (99, '0099_future.sql', 'y')")
        assert any('比这个版本的程序新' in p for p in db.schema_problems(c))


def test_unmigrated_database_is_reported(empty_db):
    with db.connect(empty_db) as c:
        assert db.schema_problems(c)


def test_what_existed_becomes_the_register_of_sites(empty_db, monkeypatch):
    """0013 on a database that has pools, models and releases: their sites and lines are registered, spellings
    that differ in case are one, the first one used is the code; nothing stored is rewritten."""
    files = db.migration_files()
    monkeypatch.setattr(db, 'migration_files', lambda: [f for f in files if f[0] < 13])
    db.migrate(empty_db)
    with db.connect(empty_db) as c:
        u = c.execute("INSERT INTO users (name, role) VALUES ('a', 'admin') RETURNING id").fetchone()['id']
        for i, (name, site, line, kind) in enumerate([
                ('p1', 'SES', 'TX', 'pool'), ('p2', 'ses', 'tx', 'pool'), ('p3', 'SES', 'TY', 'test'),
                ('p4', '', '', 'other'), ('p5', 'bad-site', 'L', 'pool'), ('p6', 'OnlySite', '', 'pool'),
                ('t1', 'Snap', 'Only', 'trainset')]):
            c.execute("INSERT INTO datasets (name, site, line, kind, created_by, created_at) VALUES (%s, %s, %s, %s, %s, "
                      "now() + %s * interval '1 second')", (name, site, line, kind, u, i))
        c.execute("INSERT INTO models (name, site, line, task, created_by) VALUES ('m1', '苏州', '一线', 'det', %s)", (u,))
    monkeypatch.setattr(db, 'migration_files', lambda: files)
    assert db.migrate(empty_db) == ['0013_sites.sql']
    with db.connect(empty_db) as c:
        got = [(r['site'], r['line']) for r in c.execute(
            'SELECT s.code AS site, l.code AS line FROM sites s LEFT JOIN lines l ON l.site_id = s.id '
            'ORDER BY lower(s.code), lower(l.code)')]
        assert got == [('OnlySite', None), ('SES', 'TX'), ('SES', 'TY'), ('苏州', '一线')]
        assert [r['site'] for r in c.execute("SELECT site FROM datasets WHERE name IN ('p2', 'p5') ORDER BY name")] \
            == ['ses', 'bad-site']
