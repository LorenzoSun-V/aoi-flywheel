"""Random version histories through the real service: every version must come back exactly as imported,
cold (empty cache) and warm, and the server's diff must match the change actually made.

FW_RANDOM_VERSIONS (default 12) versions per seed, FW_RANDOM_SEEDS (default "1,2")."""
import os
import random
import shutil

import pytest

from helpers import quiet, read_tree, write
from fw import ops
from fw.cache import Cache
from fw.contract import diff_entries, sha256_file

VERSIONS = int(os.environ.get('FW_RANDOM_VERSIONS', 12))
SEEDS = [int(s) for s in os.environ.get('FW_RANDOM_SEEDS', '1,2').split(',')]
NAMES = ['img', 'pic', 'a', 'b', 'c', '中文', 'x y']
DIRS = ['', 'd1/', 'd2/', 'd1/sub/', '批次/']


def mutate(rng, files, history):
    files = dict(files)
    for _ in range(rng.randint(1, 6)):
        op = rng.choice(['add', 'add', 'modify', 'copy', 'rename', 'delete', 'swap', 'dup', 'empty', 'revert'])
        paths = sorted(files)
        if op == 'add' or not paths:
            files[f'{rng.choice(DIRS)}{rng.choice(NAMES)}{rng.randint(0, 40)}.{rng.choice(["bmp", "json"])}'] = \
                rng.randbytes(rng.randint(0, 3000))
        elif op == 'modify':
            files[rng.choice(paths)] = rng.randbytes(rng.randint(1, 3000))
        elif op == 'copy':
            src = rng.choice(paths)
            files[f'{rng.choice(DIRS)}copy{rng.randint(0, 99)}_{src.rsplit("/", 1)[-1]}'] = files[src]
        elif op == 'rename':
            src = rng.choice(paths)
            files[f'{rng.choice(DIRS)}moved{rng.randint(0, 99)}_{src.rsplit("/", 1)[-1]}'] = files.pop(src)
        elif op == 'delete':
            files.pop(rng.choice(paths))
        elif op == 'swap' and len(paths) > 1:
            a, b = rng.sample(paths, 2)
            files[a], files[b] = files[b], files[a]
        elif op == 'dup':
            data = files[rng.choice(paths)]
            for i in range(rng.randint(2, 5)):
                files[f'dup/{i}_{rng.randint(0, 99)}.json'] = data
        elif op == 'empty':
            files[f'{rng.choice(DIRS)}empty{rng.randint(0, 9)}.json'] = b''
        elif op == 'revert' and history:
            return dict(rng.choice(history))
    # keep names that collide only by case or as file vs folder out of the model (the import refuses them)
    seen, clean = set(), {}
    for p in sorted(files):
        key = p.lower()
        prefixes = {key.rsplit('/', i)[0] for i in range(1, key.count('/') + 1)}
        if key in seen or prefixes & seen or any(s.startswith(key + '/') for s in seen):
            continue
        seen.add(key)
        clean[p] = files[p]
    return clean or {'fallback.json': b'{}'}


def entries_of(root, files):
    return [(rel, *sha256_file(root.joinpath(*rel.split('/')))) for rel in sorted(files)]


@pytest.mark.parametrize('seed', SEEDS)
def test_random_history(api, dataset, tmp_path, seed):
    rng = random.Random(seed)
    warm = Cache(tmp_path / 'warm-cache')
    history = []
    files = {}
    previous = None
    try:
        for step in range(VERSIONS):
            files = mutate(rng, files, history)
            src = tmp_path / 'src'
            shutil.rmtree(src, ignore_errors=True)
            for rel, data in files.items():
                write(src, rel, data)
            v = ops.import_folder(api, dataset, src, allow_other_files=True, force=True, echo=quiet)
            if previous is not None and v['id'] == previous['id']:
                continue  # same content as the latest: no new version
            history.append(files)
            # cold: a brand-new cache
            cold = Cache(tmp_path / f'cold-{step}')
            try:
                ops.get(api, cold, v['ref'], tmp_path / f'cold-out-{step}', echo=quiet)
            finally:
                cold.close()
            assert read_tree(tmp_path / f'cold-out-{step}') == files, f'cold {v["ref"]}'
            ops.get(api, warm, v['ref'], tmp_path / f'warm-out-{step}', echo=quiet)
            assert read_tree(tmp_path / f'warm-out-{step}') == files, f'warm {v["ref"]}'
            if previous is not None:
                expected = diff_entries(previous['entries'], entries_of(src, files))
                got = api.diff(previous['id'], v['id'])
                assert {k: got[k] for k in expected} == expected
            previous = dict(v, entries=entries_of(src, files))
            shutil.rmtree(tmp_path / f'cold-out-{step}')
            shutil.rmtree(tmp_path / f'cold-{step}')
        # every earlier version is still intact after all the later ones
        for n, expected_files in enumerate(history, start=1):
            out = tmp_path / f'final-{n}'
            ops.get(api, warm, f'{dataset}@{n}', out, echo=quiet)
            assert read_tree(out) == expected_files
    finally:
        warm.close()
