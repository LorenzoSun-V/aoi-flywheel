"""Accounts and tokens when two administrators act at the same moment (real PostgreSQL)."""
import threading
import uuid

import pytest

from fwserver import auth, db


@pytest.fixture
def account(database):
    name = f'race-{uuid.uuid4().hex[:6]}'
    with db.connect(database) as c:
        auth.add_user(c, name, 'algo')
    return name


def test_token_created_while_the_account_is_disabled_is_revoked_too(database, account, monkeypatch):
    inside, release = threading.Event(), threading.Event()
    real_new_token = auth.new_token

    def slow_new_token():  # create_token has read the account and is about to insert the token
        inside.set()
        assert release.wait(20)
        return real_new_token()

    monkeypatch.setattr(auth, 'new_token', slow_new_token)
    created, disabled = {}, threading.Event()

    def create():
        with db.connect(database) as c:
            created['token'] = auth.create_token(c, account)[0]

    def disable():
        with db.connect(database) as c:
            auth.set_disabled(c, account, True)
        disabled.set()

    maker = threading.Thread(target=create)
    maker.start()
    assert inside.wait(20)
    stopper = threading.Thread(target=disable)
    stopper.start()
    try:
        assert not disabled.wait(1)  # the disable waits for the token being created ...
    finally:
        release.set()
        maker.join(20)
        stopper.join(20)
    assert disabled.is_set()
    with db.connect(database) as c:  # ... and revokes it with the others: enabling again does not revive it
        auth.set_disabled(c, account, False)
        assert auth.authenticate(c, created['token']) is None


def test_token_for_an_account_being_disabled_is_refused(database, account):
    result = {}

    def create():
        try:
            with db.connect(database) as c:
                result['token'] = auth.create_token(c, account)[0]
        except auth.AuthError as e:
            result['error'] = str(e)

    maker = threading.Thread(target=create)
    with db.connect(database) as c:
        with c.transaction():  # the disable has run but not committed yet
            auth.set_disabled(c, account, True)
            maker.start()
            maker.join(1)
            assert maker.is_alive()  # create_token waits for it
    maker.join(20)
    assert '已停用' in result.get('error', '')
