"""Evaluation jobs: the platform runs aoi-deteval on a model version (docs/architecture.md, 统一评估).

Models are only ever run as TensorRT engines: the worker builds one on its GPU machine from the model version's
registered ONNX with the runner's onnx2engine (cached there by ONNX, program, environment and GPU; never uploaded
or released: a site builds its own engine from the same ONNX) and runs it with the deploy code's inference
module. Uploaded weights (.pt / .pth, pickles that run code when loaded) are never loaded.

Submitting pins everything the job will read: the model version (its ONNX), the test images (chosen folders
of a dataset version, or one split of a training-set snapshot, written to the store as a manifest of their
own: the job's input list) and the runner version (the conda environment and framework source on one GPU
machine). The worker lays out those files, runs `deteval predict` and `deteval evaluate` in that environment
and sends back the output folder; finishing the job registers it as an evaluation report in the same
transaction, so a report exists exactly when its job is done.

Test images the model was trained on (the same content as an image of its snapshot's train or val split, by
hash, whatever the path) make the result look better than it is: the job is still queued, with a warning that
is shown when it is submitted and kept in the report's note.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional

from fw.contract import ContractError, eval_dirs, eval_selection, manifest_bytes

from . import datasets, formats, jobs, models, predictions, trainsets
from .datasets import DatasetError
from .store import Store, StoreError

TOOL = 'aoi-deteval'
DETEVAL_NAME = re.compile(r'[^A-Za-z0-9._+-]')


def _ref(ref: Any, what: str) -> tuple:
    if not isinstance(ref, str) or not ref.strip():
        raise DatasetError(400, f'要写{what}')
    name, sep, number = ref.strip().partition('@')
    if not name:
        raise DatasetError(400, f'{what}写法不对：{ref!r}，应为 名称@序号')
    return name, (number if sep else 'latest')


def _onnx(conn, mv: dict) -> List[list]:
    """The model version's ONNX, which the worker builds its engine from."""
    files = models.fileset_files(conn, mv['fileset_id'])
    onnx = [f for f in files if f['path'] == 'model.onnx']
    if not onnx:
        raise DatasetError(400, f'{mv["ref"]} 没有 model.onnx，评不了')
    return [['model.onnx', onnx[0]['sha256'], onnx[0]['size']]]


def _overlap(conn, mv: dict, images: List[tuple]) -> Optional[str]:
    """A warning when test images have the content of images the model was trained or validated on."""
    trained = {}
    for path, digest, _ in datasets.version_files(conn, mv['trainset']['id']):
        if path.startswith(('images/train/', 'images/val/')):
            trained.setdefault(digest, path.split('/')[1])
    seen = [(p, trained[d]) for p, d, _ in images if d in trained]
    if not seen:
        return None
    splits = sorted({s for _, s in seen})
    return (f'选的图里有 {len(seen)} 张和训练集快照 {mv["trainset"]["ref"]} 的 {"、".join(splits)} 里的图内容相同'
            f'（例如 {seen[0][0]}），这些图的结果会偏好')


def _same_classes(conn, version: dict, mv: dict) -> None:
    """A snapshot's YOLO labels number classes by its own class table, and deteval reads them by the model's:
    the two must be the same list in the same order, or right boxes count as the wrong class."""
    theirs = list(trainsets.info(conn, version['id'])['trainset']['classes'] or [])
    ours = list(mv['classes'] or [])
    if theirs != ours:
        raise DatasetError(400, f'{version["ref"]} 的类别表（{"、".join(theirs)}）和模型 {mv["ref"]} 的（{"、".join(ours)}）'
                                '不一样（顺序也要一样），快照里 txt 的类别编号会对错类别；用和模型相同的类别表建快照再评')


def _known_names(store: Store, files: List[tuple], mv: dict) -> None:
    """The JSON labels of a dataset version are read by class name: every box must name a class of the model
    (an unknown name would be dropped or miscounted by deteval), and every label must be readable."""
    known = set(mv['classes'] or [])
    unknown: dict = {}
    bad: List[str] = []
    seen = set()
    for path, digest, _ in files:
        if not path.endswith('.json') or digest in seen:
            continue
        seen.add(digest)
        data = store.read(digest)
        if data is None:
            raise DatasetError(503, f'服务器上没有 {path} 的内容，先运行 fw-server verify')
        try:
            parsed = formats.parse_json(data.decode('utf-8-sig'))
        except UnicodeDecodeError:
            parsed = '不是 UTF-8'
        if isinstance(parsed, str):
            bad.append(f'{path}（{parsed}）')
            continue
        for box in parsed.boxes:
            if box.name not in known:
                unknown.setdefault(box.name, path)
    problems = []
    if bad:
        problems.append(f'{len(bad)} 个标注读不了：{"；".join(bad[:3])}')
    if unknown:
        problems.append(f'标注里有模型 {mv["ref"]} 类别表（{"、".join(mv["classes"] or [])}）里没有的类别：'
                        + '、'.join(f'{n}（例如 {p}）' for n, p in list(unknown.items())[:5]))
    if problems:
        raise DatasetError(400, '评不了：' + '；'.join(problems))


def submit(conn, store: Store, user: dict, runner: Optional[str], params: Optional[dict]) -> dict:
    """params: model (a model version), data (a dataset version, or a snapshot's split as NAME@N:test), dirs (folders
    of the dataset version; none = all of it)."""
    params = dict(params or {})
    unknown = sorted(set(params) - {'model', 'data', 'dirs'})
    if unknown:
        raise DatasetError(400, f'评估任务不认识这些参数：{"、".join(unknown)}')
    mv = models.get_model_version(conn, *_ref(params.get('model'), '评哪个模型版本'))
    data_ref, _, split = str(params.get('data') or '').partition(':')
    version = datasets.get_version(conn, *_ref(data_ref, '在哪个数据集版本上评'))
    snapshot = version['kind'] == 'trainset'
    dirs = params.get('dirs') or []
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise DatasetError(400, '目录写成一个文本列表')
    if snapshot and not split:
        raise DatasetError(400, f'{version["ref"]} 是训练集快照，要写评哪个划分，例如 {version["ref"]}:test')
    if snapshot and dirs:
        raise DatasetError(400, '训练集快照按划分评，不选目录')
    if not snapshot and split:
        raise DatasetError(400, f'{version["ref"]} 不是训练集快照，不用写划分；要评其中几个目录就用 --dirs')
    rv = jobs.find_runner(conn, runner, '评估')
    if not rv['framework']:
        raise DatasetError(400, f'推理环境 {rv["ref"]} 登记时没写框架和 onnx2engine，评不了；'
                                '在 GPU 机上带 --framework rfdetr --onnx2engine <程序> 再登记一次')
    onnx = _onnx(conn, mv)
    try:
        files, images, problems = eval_selection(datasets.version_files(conn, version['id']), snapshot, split, dirs)
        dirs = [] if snapshot else eval_dirs(dirs)
    except ContractError as e:
        raise DatasetError(400, str(e)) from None
    if problems:
        raise DatasetError(400, '评不了：' + '；'.join(problems))
    if not images:
        raise DatasetError(400, f'{version["ref"]}{":" + split if split else ""} 里没有图片')
    if snapshot:
        _same_classes(conn, version, mv)
    else:
        _known_names(store, files, mv)
    warnings = [w for w in (_overlap(conn, mv, images),) if w]
    inputs, _ = store.put_bytes(manifest_bytes(files))
    try:
        store.flush()
    except StoreError as e:
        raise DatasetError(503, str(e)) from None
    job_params = {
        'model': {'id': mv['id'], 'ref': mv['ref'], 'files': onnx, 'classes': mv['classes']},
        'data': {'id': version['id'], 'ref': version['ref'], 'snapshot': snapshot, 'split': split, 'dirs': dirs,
                 'inputs_sha256': inputs, 'images': len(images), 'files': len(files)},
        'runner': jobs.runner_params(rv),
        'deteval': {'name': DETEVAL_NAME.sub('_', f'{mv["model"]}_{mv["number"]}')},
        'warnings': warnings}
    return jobs.enqueue(conn, user, 'eval', rv, job_params)


def _note(p: dict, job_id: int) -> str:
    return '；'.join([f'评估任务 {job_id}（{TOOL}，在推理环境 {p["runner"]["conda_env"]} 里）'] + p['warnings'])


def finish(conn, store: Store, job_id: int, token: str, outcome: str, result: Any = None, error: str = '') -> dict:
    """Finish any job; a done prediction job registers its prediction set (predictions.register); a done
    evaluation sends {files: [[path, sha256, size], ...], tool_version} (metrics.json and
    files/...) and is registered as an evaluation report while the job row is held, so that a claim that lost its
    lease registers nothing and a retried finish registers nothing twice."""
    with conn.transaction():
        if jobs.finished_already(conn, job_id, token, outcome):
            return jobs.get_job(conn, job_id)
        row = jobs.held(conn, job_id, token)
        if row['cancel_requested'] and outcome == 'done':
            return jobs.finish(conn, job_id, token, 'cancelled')  # the accepted cancel stands: nothing registered
        if row['kind'] == 'eval' and outcome == 'done':
            if not isinstance(result, dict) or not isinstance(result.get('files'), list):
                raise DatasetError(400, '评估结果里要有文件清单')
            p = row['params']
            tool_version = result.get('tool_version') or ''
            e = models.register_eval(
                conn, store, row['submitted_by'], *_ref(p['model']['ref'], '模型版本'),
                p['data']['id'], p['data']['split'], result['files'], tool=TOOL,
                tool_version=tool_version if isinstance(tool_version, str) else '', note=_note(p, job_id),
                dirs=p['data']['dirs'], job_id=job_id, runner_version_id=row['runner_version_id'],
                images=p['data']['images'])
            result = {'eval_id': e['id'], 'metrics': {k: e['metrics'].get(k) for k in
                                                      ('images', 'boxes', 'mAP50', 'mAP50-95', 'P', 'R')}}
        elif row['kind'] == 'predict' and outcome == 'done':
            result = predictions.register(conn, store, row, result)
        return jobs.finish(conn, job_id, token, outcome, result, error)
