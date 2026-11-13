"""Server tests: a fresh PostgreSQL database per test session and the real service on a local port.

Needs FW_TEST_ADMIN_DSN, a superuser DSN such as postgresql://fw@127.0.0.1:5432/postgres; without it these
tests are skipped. The service runs in a thread of the test process, so tests can patch it.
"""
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import helpers
import psycopg
import pytest
import uvicorn

from fw.api import Api
from fw.cache import Cache
from fwserver import auth, datasets, db
from fwserver.app import create_app
from fwserver.settings import Settings

ADMIN_DSN = os.environ.get('FW_TEST_ADMIN_DSN')
pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason='FW_TEST_ADMIN_DSN not set')


def _dsn_for(dbname: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN)
    info['dbname'] = dbname
    return psycopg.conninfo.make_conninfo(**info)


@pytest.fixture(scope='session')
def database():
    if not ADMIN_DSN:
        pytest.skip('FW_TEST_ADMIN_DSN not set')
    name = f'fwtest_{uuid.uuid4().hex[:10]}'
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f'CREATE DATABASE {name}')
    dsn = _dsn_for(name)
    db.migrate(dsn)
    yield dsn
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f'DROP DATABASE {name} WITH (FORCE)')


class Service:
    def __init__(self, dsn: str, store: Path, **settings):
        self.settings = Settings(dsn=dsn, store=store, pool_size=8, **settings)
        self.hooks = {}
        self.app = create_app(self.settings, hooks=self.hooks)
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            self.port = s.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.server = uvicorn.Server(uvicorn.Config(self.app, host='127.0.0.1', port=self.port, log_level='warning',
                                                    access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 20
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError('service did not start')
            time.sleep(0.05)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(20)


@pytest.fixture(scope='session')
def service(database, tmp_path_factory):
    svc = Service(database, tmp_path_factory.mktemp('store'))
    svc.start()
    yield svc
    svc.stop()


@pytest.fixture
def make_service(database, tmp_path):
    """Another instance of the service with its own store and other settings, for one test."""
    made = []

    def make(**settings):
        svc = Service(database, tmp_path / f'store{len(made)}', **settings)
        svc.start()
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.stop()


@pytest.fixture(scope='session')
def tokens(database):
    """One account and token per role."""
    def register(site, line):
        with db.connect(database) as k:
            try:
                datasets.add_place(k, None, f'{site}/{line}')
            except datasets.DatasetError:
                pass  # there already, or not a code: the server says so where it matters
    helpers._REGISTER = register

    out = {}
    with db.connect(database) as c:
        datasets.add_place(c, None, 'SES/TX', '贴片线')  # the line most tests put their pools and models on
        # 'deploy': a second field account, the one that fetches models and registers releases in the tests
        for role in ('admin', 'algo', 'field', 'deploy', 'worker'):
            name = f'{role}-{uuid.uuid4().hex[:6]}'
            auth.add_user(c, name, 'field' if role == 'deploy' else role)
            out[role] = auth.create_token(c, name, f'{role} test')[0]
            out[role + '_name'] = name
    return out


@pytest.fixture
def make_api(service, tokens):
    made = []

    def make(role='algo', token=None):
        api = Api(service.url, token if token is not None else tokens[role])
        made.append(api)
        return api

    yield make
    for a in made:
        a.close()


@pytest.fixture
def api(make_api):
    return make_api('algo')


@pytest.fixture
def cache(tmp_path):
    c = Cache(tmp_path / 'cache')
    yield c
    c.close()


@pytest.fixture
def dataset(api):
    """A new, empty dataset for one test."""
    name = f'DS-{uuid.uuid4().hex[:8]}'
    api.create_dataset(name, 'SES', 'TX', 'pool', 'test')
    return name


@pytest.fixture
def sql(database):
    with db.connect(database) as c:
        yield c
