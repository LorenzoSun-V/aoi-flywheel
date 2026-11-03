"""fw-server verify: finds missing and damaged objects, repairs from a backup copy, quarantines the rest."""
import shutil
import uuid

import pytest

from helpers import quiet, write
from fw import ops
from fwserver import datasets as ds
from fwserver import db
from fwserver.store import Store


@pytest.fixture
def version(api, tmp_path):
    name = f'V-{uuid.uuid4().hex[:6]}'
    api.create_dataset(name, '', '', 'pool', '')
    for rel, data in {'a.bmp': b'aaaa', 'a.json': b'{"a": 1}', 'b.bmp': b'bbbb'}.items():
        write(tmp_path / 'src', rel, data)
    return ops.import_folder(api, name, tmp_path / 'src', echo=quiet)


def run_verify(database, store, **kw):
    with db.connect(database) as c:
        return ds.verify(c, store, echo=quiet, **kw)


def test_verify_finds_and_repairs(api, version, service, database, tmp_path):
    store = service.app.state.store
    assert run_verify(database, store, rehash=True) == []
    from fw.contract import sha256_file
    digest_a, _ = sha256_file(tmp_path / 'src' / 'a.bmp')
    digest_b, _ = sha256_file(tmp_path / 'src' / 'b.bmp')
    backup = tmp_path / 'backup'
    for d in (digest_a, digest_b):
        dst = backup / 'objects' / d[:2] / d[2:4] / d
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(store.path(d), dst)
    # damage one in place (same size) and lose another
    pa = store.path(digest_a)
    pa.chmod(0o644)
    pa.write_bytes(b'aaaX')
    store.path(digest_b).unlink()
    assert len(run_verify(database, store)) == 1  # without rehash only the missing one shows
    assert run_verify(database, store, rehash=True, repair_from=backup) == []
    assert store.path(digest_a).read_bytes() == b'aaaa' and store.path(digest_b).read_bytes() == b'bbbb'
    # no backup: the damaged object is quarantined and the next import of the folder brings it back
    pa = store.path(digest_a)
    pa.chmod(0o644)
    pa.write_bytes(b'aaaY')
    problems = run_verify(database, store, rehash=True)
    assert len(problems) == 1 and 'quarantine' in problems[0]
    assert api.missing([(digest_a, 4)]) == [digest_a]
    again = ops.import_folder(api, version['dataset'], tmp_path / 'src', echo=quiet)
    assert again['id'] == version['id']
    assert run_verify(database, store, rehash=True) == []
