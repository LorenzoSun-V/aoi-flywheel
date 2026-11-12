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
                                    '0008_version_changes.sql']
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
