"""Batch transfer of small objects (fw-objects 1, see fw.contract): many objects per request, each one still
checked against its own hash; a cut or malformed body never counts as received."""
import hashlib
import os

import pytest

from helpers import read_tree, write
from fw import cache as fw_cache
from fw import ops
from fw.api import ApiError
from fw.contract import OBJECTS_MAGIC, OBJECTS_TYPE, frame_header
from fwserver import app as fw_app
from fwserver.store import FLAG


def quiet(_):
    pass


def make_objects(root, sizes):
    """Files with random content of these sizes; returns [(sha256, path, size)]."""
    out = []
    for i, size in enumerate(sizes):
        data = os.urandom(size)
        out.append((hashlib.sha256(data).hexdigest(), write(root, f'o{i}.bin', data), size))
    return out


def fetch_all(api, objs):
    got = []
    api.fetch_objects([(d, size) for d, _, size in objs], lambda d, data: got.append((d, data)))
    return got


def post_body(api, body):
    return api.http.post('/objects/batch', content=body, headers={'Content-Type': OBJECTS_TYPE})


def test_round_trip(api, tmp_path):
    objs = make_objects(tmp_path, [0, 1, 1000, 70000, 5])
    assert api.put_objects(objs) == sum(size for _, _, size in objs)
    assert api.missing([(d, size) for d, _, size in objs]) == []
    assert fetch_all(api, objs) == [(d, path.read_bytes()) for d, path, _ in objs]


def test_wrong_hash_is_refused_and_not_stored(api, tmp_path):
    good, other = make_objects(tmp_path, [100, 100])
    wrong = hashlib.sha256(b'not the content ' + os.urandom(8)).hexdigest()
    with pytest.raises(ApiError) as e:
        api.put_objects([good, (wrong, other[1], other[2])])
    assert e.value.status == 400
    assert api.missing([(good[0], good[2]), (wrong, other[2])]) == [wrong]


@pytest.mark.parametrize('body', [
    b'fw-objects 2\n',
    OBJECTS_MAGIC + b'not a header\n',
    OBJECTS_MAGIC + frame_header('0' * 64, 5) + b'abc',  # stops inside an object
])
def test_malformed_or_cut_bodies_are_refused(api, body):
    r = post_body(api, body)
    assert r.status_code == 400, r.text


def test_upload_needs_a_length_and_has_a_limit(api, monkeypatch):
    r = api.http.post('/objects/batch', content=iter([OBJECTS_MAGIC]), headers={'Content-Type': OBJECTS_TYPE})
    assert r.status_code == 411  # chunked, no Content-Length
    monkeypatch.setattr(fw_app, 'BATCH_BODY_MAX', 10)
    assert post_body(api, OBJECTS_MAGIC + b'x' * 100).status_code == 413


def test_upload_refused_while_the_store_is_flagged(api, tmp_path, service):
    store = service.app.state.store
    (store.root / FLAG).write_text('test flag\n', encoding='utf-8')
    try:
        with pytest.raises(ApiError) as e:
            api.put_objects(make_objects(tmp_path, [10]))
        assert e.value.status == 503
    finally:
        store.clear_flag()


def test_only_data_roles_can_transfer(make_api, tmp_path):
    objs = make_objects(tmp_path, [10])
    field = make_api('field')
    with pytest.raises(ApiError) as e:
        field.put_objects(objs)
    assert e.value.status == 403
    make_api('algo').put_objects(objs)
    with pytest.raises(ApiError) as e:
        fetch_all(field, objs)
    assert e.value.status == 403


def test_fetch_errors(api, tmp_path, monkeypatch):
    objs = make_objects(tmp_path, [10, 20])
    api.put_objects(objs)
    absent = hashlib.sha256(b'never uploaded ' + os.urandom(8)).hexdigest()
    with pytest.raises(ApiError) as e:
        fetch_all(api, objs + [(absent, None, 3)])
    assert e.value.status == 404 and '1 个文件' in str(e.value)
    monkeypatch.setattr(fw_app, 'BATCH_OBJECT_MAX', 15)
    with pytest.raises(ApiError) as e:
        fetch_all(api, objs)
    assert e.value.status == 400  # bigger objects are fetched one per request
    monkeypatch.setattr(fw_app, 'BATCH_OBJECT_MAX', 1 << 20)
    monkeypatch.setattr(fw_app, 'FETCH_BYTES_MAX', 25)
    with pytest.raises(ApiError) as e:
        fetch_all(api, objs)
    assert e.value.status == 413


def test_fetch_resumes_after_the_response_is_cut(api, tmp_path, service, monkeypatch):
    objs = make_objects(tmp_path, [600 << 10, 600 << 10, 100, 200])
    api.put_objects(objs)
    store = service.app.state.store
    real_read = store.read
    vanish_once = {objs[2][0]}

    def read(digest):
        if digest in vanish_once:  # as if it went to quarantine while the response was being sent
            vanish_once.discard(digest)
            return None
        return real_read(digest)

    monkeypatch.setattr(store, 'read', read)
    got = fetch_all(api, objs)
    assert [d for d, _ in got] == [d for d, _, _ in objs]  # every object once, in order: the first two
    assert [data for _, data in got] == [path.read_bytes() for _, path, _ in objs]  # came before the cut


def test_import_and_get_mixing_batched_and_single_files(api, dataset, cache, tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'BATCH_OBJECT_MAX', 300)  # bigger files take the one-per-request way
    monkeypatch.setattr(fw_cache, 'BATCH_OBJECT_MAX', 300)
    monkeypatch.setattr(ops, 'UPLOAD_COUNT', 4)
    monkeypatch.setattr(fw_cache, 'FETCH_COUNT', 4)
    files = {f'line{i % 3}/img{i}.bmp': os.urandom(100 + 50 * i) for i in range(10)}
    files.update({f'line{i % 3}/img{i}.json': b'{"i": %d}' % i for i in range(10)})
    for rel, data in files.items():
        write(tmp_path / 'src', rel, data)
    v = ops.import_folder(api, dataset, tmp_path / 'src', echo=quiet)
    ops.get(api, cache, v['ref'], tmp_path / 'out', echo=quiet)
    assert read_tree(tmp_path / 'out') == files
