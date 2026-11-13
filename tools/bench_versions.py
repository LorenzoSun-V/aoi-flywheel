"""How a pool behaves over many versions: one folder added a day, through the HTTP API with the real fw client.

    FW_SERVER=http://<platform>:8000 FW_TOKEN=<algo or admin token> \\
        python tools/bench_versions.py --files 1000000 --versions 300 --work /data/tmp

Makes a pool and adds `versions` folders to it, one version each (`fw dataset add`), until it holds `files`
files (image + label pairs; random image bytes, ~1 KB X-AnyLabeling JSON). Every version is a full file list in
the database, so the rows grow with files x versions / 2: this measures what one more day costs as the pool
and its history grow. Prints the time of each add (sampled), and at the end: listing the versions, the diff of
the last day, the diff first -> last, fetching the last version's file list. Prints JSON.

What only the server can measure is not here; run on the server afterwards (README, deployment):
the size of the database, `fw-server verify`, and the time of deploy/backup.sh.
"""
import argparse
import json
import os
import random
import shutil
import time
from pathlib import Path

from fw import ops

from bench import label, timed


def make_day(folder: Path, rng, day: int, pairs: int, img_bytes: int) -> None:
    folder.mkdir(parents=True)
    for i in range(pairs):
        name = f'd{day:04d}-{i:06d}.bmp'
        (folder / name).write_bytes(os.urandom(img_bytes))
        (folder / f'd{day:04d}-{i:06d}.json').write_bytes(label(rng, name))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--files', type=int, default=1000000, help='files in the pool at the end (two per pair)')
    ap.add_argument('--versions', type=int, default=300)
    ap.add_argument('--img-kb', type=int, default=1)
    ap.add_argument('--work', default='.', help='scratch folder (holds one day of files at a time)')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--every', type=int, default=10, help='report the add of every n-th version')
    a = ap.parse_args()
    rng = random.Random(7)
    work = Path(a.work) / f'fw-bench-versions-{int(time.time())}'
    pairs = max(1, a.files // (2 * a.versions))
    api = ops.connect()
    name = f'bench-versions-{time.strftime("%Y%m%d-%H%M%S")}'
    out = {'files': 2 * pairs * a.versions, 'versions': a.versions, 'files_per_version': 2 * pairs,
           'img_kb': a.img_kb, 'dataset': name, 'server': api.server, 'adds': []}
    started = time.perf_counter()
    try:
        api.create_dataset(name, '', '', 'other', 'tools/bench_versions.py')
        first = last = before = None
        for day in range(1, a.versions + 1):
            folder = work / f'day-{day:04d}'
            make_day(folder, rng, day, pairs, a.img_kb * 1024)
            phases = []
            version, seconds = timed(lambda: ops.add_folders(
                api, name, [folder], workers=a.workers,
                echo=lambda line: phases.append(line[line.index('用时'):].strip()) if '用时' in line else None))
            shutil.rmtree(folder)
            first, before, last = first or version, last, version
            if day == 1 or day % a.every == 0 or day == a.versions:
                row = {'version': day, 'files': version['file_count'], 'add_s': seconds,
                       'elapsed_min': round((time.perf_counter() - started) / 60, 1), 'phases': phases}
                out['adds'].append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
        _, out['list_versions_s'] = timed(lambda: api.versions(name))
        _, out['list_datasets_s'] = timed(lambda: api.datasets())
        _, out['diff_last_day_s'] = timed(lambda: api.diff(before['id'], last['id']))
        _, out['diff_first_to_last_s'] = timed(lambda: api.diff(first['id'], last['id']))
        _, out['manifest_last_s'] = timed(lambda: api.read_object(last['manifest_sha256']))
        out['total_min'] = round((time.perf_counter() - started) / 60, 1)
    finally:
        api.close()
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
