"""Scale check of a running platform through its HTTP API, with the real fw client.

    FW_SERVER=http://<platform>:8000 FW_TOKEN=<algo or admin token> python tools/bench.py --pairs 100000 --work /data/tmp

Builds N image + label pairs (random image bytes, ~1 KB X-AnyLabeling JSON) in 100 folders, then measures:
v1 import; v2 import after 1% labels edited + 1% pairs added + 1% deleted + 100 renamed + 100 copied pairs;
the diff v1 -> v2; a cold fetch of v2 (empty cache) and a warm fetch (everything cached). Each fetched folder
is compared file by file with the source. Creates a dataset named bench-<time>; prints JSON.
"""
import argparse
import json
import os
import random
import shutil
import time
from pathlib import Path

from fw import ops
from fw.cache import Cache


def label(rng, name):
    shapes = [{'label': rng.choice(['Dirty', 'Scratch', 'Chip']), 'shape_type': 'rectangle',
               'points': [[rng.uniform(0, 700), rng.uniform(0, 700)], [rng.uniform(0, 768), rng.uniform(0, 768)]],
               'flags': {}, 'group_id': None, 'description': ''} for _ in range(rng.randint(1, 4))]
    doc = {'version': '2.4.0', 'flags': {}, 'shapes': shapes, 'imagePath': name, 'imageData': None,
           'imageHeight': 768, 'imageWidth': 768}
    return json.dumps(doc, ensure_ascii=False, indent=2).encode()


def make_pair(src, rng, i, img_bytes):
    d = src / f'line{i % 100:02d}'
    name = f'img{i:07d}.bmp'
    (d / name).write_bytes(os.urandom(img_bytes))
    (d / f'img{i:07d}.json').write_bytes(label(rng, name))


def same_tree(a: Path, b: Path) -> bool:
    def listing(root):
        out = {}
        for dirpath, _, names in os.walk(root):
            for n in names:
                p = Path(dirpath) / n
                out[p.relative_to(root).as_posix()] = p
        return out
    la, lb = listing(a), listing(b)
    return la.keys() == lb.keys() and all(la[k].read_bytes() == lb[k].read_bytes() for k in la)


def timed(fn):
    t = time.perf_counter()
    result = fn()
    return result, round(time.perf_counter() - t, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pairs', type=int, default=100000)
    ap.add_argument('--img-kb', type=int, default=8)
    ap.add_argument('--work', default='.', help='scratch folder (needs about 5x the data size free)')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--keep', action='store_true')
    a = ap.parse_args()
    rng = random.Random(7)
    work = Path(a.work) / f'fw-bench-{int(time.time())}'
    src = work / 'src'
    for i in range(100):
        (src / f'line{i:02d}').mkdir(parents=True)
    api = ops.connect()
    name = f'bench-{time.strftime("%Y%m%d-%H%M%S")}'
    out = {'pairs': a.pairs, 'files_v1': 2 * a.pairs, 'img_kb': a.img_kb, 'dataset': name, 'server': api.server}
    phases = {}  # the timing line each step prints, e.g. how an import splits into hashing, upload, commit

    def keep(step):
        def echo(line):
            if '用时' in line:
                phases[step] = line[line.index('用时'):].strip()
        return echo

    try:
        api.create_dataset(name, '', '', 'other', 'tools/bench.py')
        _, out['generate_s'] = timed(lambda: [make_pair(src, rng, i, a.img_kb * 1024) for i in range(a.pairs)])
        v1, out['v1_import_s'] = timed(lambda: ops.import_folder(api, name, src, workers=a.workers,
                                                                 echo=keep('v1_import')))

        k = max(1, a.pairs // 100)
        ids = rng.sample(range(a.pairs), 3 * k + 200)
        edit, delete, rename, copy = ids[:k], ids[k:2 * k], ids[2 * k:2 * k + 100], ids[2 * k + 100:2 * k + 200]
        for i in edit:
            p = src / f'line{i % 100:02d}' / f'img{i:07d}.json'
            p.write_bytes(label(rng, p.with_suffix('.bmp').name))
        for i in range(a.pairs, a.pairs + k):
            make_pair(src, rng, i, a.img_kb * 1024)
        for i in delete:
            for ext in ('bmp', 'json'):
                (src / f'line{i % 100:02d}' / f'img{i:07d}.{ext}').unlink()
        for i in rename:
            for ext in ('bmp', 'json'):
                (src / f'line{i % 100:02d}' / f'img{i:07d}.{ext}').rename(
                    src / f'line{(i + 1) % 100:02d}' / f'moved{i:07d}.{ext}')
        for i in copy:
            for ext in ('bmp', 'json'):
                shutil.copy(src / f'line{i % 100:02d}' / f'img{i:07d}.{ext}',
                            src / f'line{(i + 2) % 100:02d}' / f'copy{i:07d}.{ext}')
        v2, out['v2_import_s'] = timed(lambda: ops.import_folder(api, name, src, workers=a.workers,
                                                                 echo=keep('v2_import')))
        out['v2_files'] = v2['file_count']
        out['v2_changes'] = v2.get('changes')
        _, out['diff_s'] = timed(lambda: api.diff(v1['id'], v2['id']))

        cache = Cache(work / 'cache')
        try:
            _, out['cold_get_s'] = timed(lambda: ops.get(api, cache, v2['ref'], work / 'cold', workers=a.workers,
                                                         echo=keep('cold_get')))
            out['cold_downloaded_files'] = cache.downloaded_files
            _, out['warm_get_s'] = timed(lambda: ops.get(api, cache, v2['ref'], work / 'warm', workers=a.workers,
                                                         echo=keep('warm_get')))
            out['warm_downloaded_files'] = cache.downloaded_files - out['cold_downloaded_files']
        finally:
            cache.close()
        out['cold_identical'] = same_tree(src, work / 'cold')
        out['warm_identical'] = same_tree(src, work / 'warm')
        out['phases'] = phases
    finally:
        api.close()
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
