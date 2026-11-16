"""Evaluation jobs: the platform runs aoi-deteval on a model version (docs/architecture.md, 统一评估).

Models are only ever run as TensorRT engines: the worker builds one on its GPU machine from the model version's
registered ONNX with the runner's onnx2engine (cached there by ONNX, program, environment and GPU; never uploaded
or released: a site builds its own engine from the same ONNX) and runs it with the deploy code's inference
module. Uploaded weights (.pt / .pth, pickles that run code when loaded) are never loaded.

Submitting pins everything the job will read: the model version (its ONNX), the test images (chosen folders
of a dataset version, or one split of a training-set snapshot, written to the store as a manifest of their
own: the job's input list) and the runner version (the conda environment and framework source on one GPU
machine). The worker lays out those files, builds or reuses its engine, runs it over the images
(fw/trt_predict.py) and then `deteval evaluate` in that environment
and sends back the output folder; finishing the job registers it as an evaluation report in the same
transaction, so a report exists exactly when its job is done.

Test images the model was trained on (the same content as an image of its snapshot's train or val split, by
hash, whatever the path) make the result look better than it is: the job is still queued, with a warning that
is shown when it is submitted and kept in the report's note.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional

from fw.contract import TASKS, ContractError, eval_dirs, eval_selection, manifest_bytes

from . import datasets, formats, jobs, models, predictions, trainsets
from .datasets import DatasetError
from .store import Store, StoreError

DETEVAL_NAME = re.compile(r'[^A-Za-z0-9._+-]')


_ref = datasets.parse_ref


def _onnx(conn, mv: dict) -> List[list]:
    """The model version's ONNX, which the worker builds its engine from."""
    files = models.fileset_files(conn, mv['fileset_id'])
    onnx = [f for f in files if f['path'] == 'model.onnx']
    if not onnx:
        raise DatasetError(400, f'{mv["ref"]} 没有 model.onnx，无法评估')
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
    return (f'所选图片中有 {len(seen)} 张与训练集快照 {mv["trainset"]["ref"]} 的 {"、".join(splits)} 中的图片内容相同'
            f'（例如 {seen[0][0]}），这些图片的评估结果会偏高')


def _same_classes(conn, version: dict, mv: dict) -> None:
    """A snapshot's YOLO labels number classes by its own class table, and deteval reads them by the model's:
    the two must be the same list in the same order, or right boxes count as the wrong class."""
    theirs = list(trainsets.info(conn, version['id'])['trainset']['classes'] or [])
    ours = list(mv['classes'] or [])
    if theirs != ours:
        raise DatasetError(400, f'{version["ref"]} 的类别表（{"、".join(theirs)}）和模型 {mv["ref"]} 的类别表（{"、".join(ours)}）'
                                '不一致（顺序也须一致），快照中 txt 的类别编号将对应到错误的类别；请用与模型相同的类别表建立快照后再评估')


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
            raise DatasetError(503, f'服务器上缺少 {path} 的内容，请先运行 fw-server verify')
        try:
            parsed = formats.of(mv['task']).parse_json(data.decode('utf-8-sig'))  # read as the model's task
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
        problems.append(f'{len(bad)} 个标注无法读取：{"；".join(bad[:3])}')
    if unknown:
        problems.append(f'标注中有模型 {mv["ref"]} 类别表（{"、".join(mv["classes"] or [])}）中没有的类别：'
                        + '、'.join(f'{n}（例如 {p}）' for n, p in list(unknown.items())[:5]))
    if problems:
        raise DatasetError(400, '无法评估：' + '；'.join(problems))


def submit(conn, store: Store, user: dict, runner: Optional[str], params: Optional[dict]) -> dict:
    """params: model (a model version), data (a dataset version, or a snapshot's split as NAME@N:test), dirs (folders
    of the dataset version; none = all of it)."""
    params = dict(params or {})
    unknown = sorted(set(params) - {'model', 'data', 'dirs'})
    if unknown:
        raise DatasetError(400, f'评估任务不支持以下参数：{"、".join(unknown)}')
    mv = models.get_model_version(conn, *_ref(params.get('model'), '评估的模型版本'))
    data_ref, _, split = str(params.get('data') or '').partition(':')
    version = datasets.get_version(conn, *_ref(data_ref, '评估所用的数据集版本'))
    if version['kind'] == 'batch':
        raise DatasetError(400, f'{version["ref"]} 是尚未入库的标注批次，不能作为测试集')
    snapshot = version['kind'] == 'trainset'
    dirs = params.get('dirs') or []
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise DatasetError(400, '目录应为文本列表')
    if snapshot and not split:
        raise DatasetError(400, f'{version["ref"]} 是训练集快照，须写明评估的划分，例如 {version["ref"]}:test')
    if snapshot and dirs:
        raise DatasetError(400, '训练集快照按划分评估，不能选择目录')
    if not snapshot and split:
        raise DatasetError(400, f'{version["ref"]} 不是训练集快照，无需写划分；如需只评估其中几个目录，请用 --dirs')
    rv = jobs.find_runner(conn, runner, '评估')
    if not rv['framework']:
        raise DatasetError(400, f'推理环境 {rv["ref"]} 登记时未填写框架和 onnx2engine，无法评估；'
                                '请在 GPU 机上带 --framework rfdetr --onnx2engine <程序> 重新登记')
    onnx = _onnx(conn, mv)
    try:
        files, images, problems = eval_selection(datasets.version_files(conn, version['id']), snapshot, split, dirs)
        dirs = [] if snapshot else eval_dirs(dirs)
    except ContractError as e:
        raise DatasetError(400, str(e)) from None
    if problems:
        raise DatasetError(400, '无法评估：' + '；'.join(problems))
    if not images:
        raise DatasetError(400, f'{version["ref"]}{":" + split if split else ""} 中没有图片')
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


def retry(conn, store: Store, user: dict, job_id: int, runner: Optional[str] = None) -> dict:
    """Submit the work of a failed job again: the same model version and data (and the same lowest score of a
    pre-label), on the runner named or else the newest version of the job's runner (one re-registers a runner
    after fixing its machine). A pre-label of a labelling batch is asked through the batch, as on its page, and
    only for the revision the job had. Refused when the job did not fail or its work was submitted again. The
    failed job is held meanwhile, so that two clicks at once submit it once."""
    with conn.transaction():
        jobs.hold(conn, job_id)
        return _retry(conn, store, user, job_id, runner)


def _retry(conn, store: Store, user: dict, job_id: int, runner: Optional[str]) -> dict:
    old = jobs.get_job(conn, job_id)
    if old['status'] != 'failed':
        raise DatasetError(409, f'任务 {job_id} 的状态为「{old["status_text"]}」；只有失败的任务可以重新提交')
    again = old['retried_by']
    if again:
        raise DatasetError(409, f'任务 {job_id} 已重新提交为任务 {again["id"]}（{again["status_text"]}）；'
                                f'如需再次运行，请在任务 {again["id"]} 的页面上操作')
    runner = runner or (old['runner'] or '').partition('@')[0] or None
    p = old['params']
    if old['kind'] == 'check':
        return jobs.submit(conn, user, 'check', runner)
    if old['kind'] == 'register':  # the same registration, on the same machine
        return jobs.submit(conn, user, 'register', None, dict(p, machine=p['machine']['name']))
    data = p['data']
    if old['kind'] == 'eval':
        where = data['ref'] + (f':{data["split"]}' if data.get('split') else '')
        return submit(conn, store, user, runner, {'model': p['model']['ref'], 'data': where, 'dirs': data.get('dirs') or []})
    name, _, number = data['ref'].partition('@')
    if datasets.get_version(conn, name, number)['kind'] == 'batch':
        return predictions.submit_for_batch(conn, store, user, name, model=p['model']['ref'], runner=runner,
                                            conf=p['conf'], revision=int(number))
    return predictions.submit(conn, store, user, runner, {'model': p['model']['ref'], 'data': data['ref'],
                                                          'dirs': data.get('dirs') or [], 'conf': p['conf']})


def _note(p: dict, job_id: int, tool: str) -> str:
    return '；'.join([f'评估任务 {job_id}（{tool}，在推理环境 {p["runner"]["conda_env"]} 中）'] + p['warnings'])


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
                raise DatasetError(400, '评估结果中须包含文件清单')
            p = row['params']
            tool_version = result.get('tool_version') or ''
            task = TASKS[models.get_model_version_by_id(conn, p['model']['id'])['task']]
            e = models.register_eval(
                conn, store, row['submitted_by'], *_ref(p['model']['ref'], '模型版本'),
                p['data']['id'], p['data']['split'], result['files'], tool=task.eval_tool,
                tool_version=tool_version if isinstance(tool_version, str) else '', note=_note(p, job_id, task.eval_tool),
                dirs=p['data']['dirs'], job_id=job_id, runner_version_id=row['runner_version_id'],
                images=p['data']['images'])
            result = {'eval_id': e['id'], 'metrics': {k: e['metrics'].get(k) for k in
                                                      ('images', 'boxes', *task.key_metrics)}}
        elif row['kind'] == 'predict' and outcome == 'done':
            result = predictions.register(conn, store, row, result)
        return jobs.finish(conn, job_id, token, outcome, result, error)
