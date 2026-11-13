"""Prediction sets: a model version's TensorRT engine run over chosen folders of a dataset version
(docs/architecture.md, 推理任务和 GPU worker).

Submitting pins the model version (its ONNX), the images (written to the store as the job's input list), the
runner version and the lowest score kept (conf). The same four again reuse what exists: a prediction set made
for them, or a job for them still queued or running, so the GPU never runs the same thing twice. The worker
builds (or reuses) its engine as for an evaluation, runs it over the images and sends back predictions.json
(every image of the input list: its size and its boxes) and engine.json; finishing the job checks
predictions.json against the input list and registers the prediction set in the same transaction.

Pre-labels are written from a prediction set by the client (fw predict get) at a score threshold, as
LiteAnno / X-AnyLabeling JSON with checked=false.

A labelling batch is pre-labelled the same way (submit_for_batch): the batch's owner, who may not submit jobs
in general, asks for the newest revision of their own batch; the model defaults to the one released on the
pool's line and the runner to the only one that can run a model. The batch notes the job; fw batch get
--prelabel lays the result out, the owner corrects it and uploads it as the next revision.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Optional

from fw.contract import ContractError, eval_dirs, manifest_bytes, parse_manifest, predict_selection

from . import batches, datasets, events, jobs, models
from .datasets import DatasetError
from .store import Store, StoreError

DEFAULT_CONF = 0.1
PREDICTIONS, ENGINE = 'predictions.json', 'engine.json'
MAX_PREDICTIONS = 256 << 20  # parsed whole while the job row is held
_SET = """
SELECT p.id, p.model_version_id, m.name AS model, mv.number AS model_number, p.dataset_version_id,
       d.name AS dataset, v.number AS dataset_number, p.dirs, encode(p.inputs_sha256, 'hex') AS inputs_sha256,
       p.conf, p.images, p.boxes, p.fileset_id, p.job_id, r.name || '@' || rv.number AS runner,
       u.name AS created_by, p.created_at
FROM prediction_sets p
JOIN model_versions mv ON mv.id = p.model_version_id
JOIN models m ON m.id = mv.model_id
JOIN versions v ON v.id = p.dataset_version_id
JOIN datasets d ON d.id = v.dataset_id
JOIN runner_versions rv ON rv.id = p.runner_version_id
JOIN runners r ON r.id = rv.runner_id
JOIN users u ON u.id = p.created_by
"""


def _set(r: dict) -> dict:
    return {'id': r['id'], 'model_version': {'id': r['model_version_id'], 'ref': f'{r["model"]}@{r["model_number"]}'},
            'data': {'id': r['dataset_version_id'], 'ref': f'{r["dataset"]}@{r["dataset_number"]}'},
            'dirs': list(r['dirs']), 'inputs_sha256': r['inputs_sha256'], 'conf': r['conf'], 'images': r['images'],
            'boxes': r['boxes'], 'fileset_id': r['fileset_id'], 'job_id': r['job_id'], 'runner': r['runner'],
            'created_by': r['created_by'], 'created_at': models._stamp(r['created_at'])}


def list_sets(conn, model: str = '') -> List[dict]:
    if model:
        rows = conn.execute(_SET + ' WHERE mv.model_id = %s ORDER BY p.id', (models.get_model(conn, model)['id'],))
    else:
        rows = conn.execute(_SET + ' ORDER BY p.id')
    return [_set(r) for r in rows.fetchall()]


def get_set(conn, set_id: int) -> dict:
    row = conn.execute(_SET + ' WHERE p.id = %s', (set_id,)).fetchone()
    if row is None:
        raise DatasetError(404, f'没有预测集 #{set_id}')
    out = _set(row)
    out['files'] = models.fileset_files(conn, out['fileset_id'])
    return out


def _conf(value: Any) -> float:
    if value is None:
        return DEFAULT_CONF
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
        raise DatasetError(400, f'最低分数 conf 要在 0 和 1 之间（不含 0 和 1）：{value!r}')
    return float(value)


def _key(mv_id: int, rv_id: int, inputs: str, conf: float) -> str:
    text = json.dumps({'model_version': mv_id, 'runner_version': rv_id, 'inputs': inputs, 'conf': conf},
                      sort_keys=True)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def submit(conn, store: Store, user: dict, runner: Optional[str], params: Optional[dict]) -> dict:
    """params: model (a model version), data (a dataset version, not a snapshot), dirs (its folders; none = all
    of it), conf (the lowest score kept, default 0.1). Returns the job; reused: true when it is an earlier job
    for the same model version, runner version, images and conf (done, or still queued or running)."""
    params = dict(params or {})
    unknown = sorted(set(params) - {'model', 'data', 'dirs', 'conf'})
    if unknown:
        raise DatasetError(400, f'推理任务不认识这些参数：{"、".join(unknown)}')
    mv = models.get_model_version(conn, *_ref_of(params.get('model'), '用哪个模型版本'))
    version = datasets.get_version(conn, *_ref_of(params.get('data'), '在哪个数据集版本上推理'))
    if version['kind'] == 'trainset':
        raise DatasetError(400, f'{version["ref"]} 是训练集快照；预标注选数据池版本里的目录（--dirs）')
    dirs = params.get('dirs') or []
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise DatasetError(400, '目录写成一个文本列表')
    conf = _conf(params.get('conf'))
    rv = jobs.find_runner(conn, runner, '推理')
    if not rv['framework']:
        raise DatasetError(400, f'推理环境 {rv["ref"]} 登记时没写框架和 onnx2engine，推理不了；'
                                '在 GPU 机上带 --framework rfdetr --onnx2engine <程序> 再登记一次')
    onnx = models.fileset_files(conn, mv['fileset_id'])
    onnx = [['model.onnx', f['sha256'], f['size']] for f in onnx if f['path'] == 'model.onnx']
    if not onnx:
        raise DatasetError(400, f'{mv["ref"]} 没有 model.onnx，推理不了')
    try:
        images, problems = predict_selection(datasets.version_files(conn, version['id']), dirs)
        dirs = eval_dirs(dirs)
    except ContractError as e:
        raise DatasetError(400, str(e)) from None
    if problems:
        raise DatasetError(400, '推理不了：' + '；'.join(problems))
    if not images:
        raise DatasetError(400, f'{version["ref"]} 里没有图片')
    inputs, _ = store.put_bytes(manifest_bytes(images))
    try:
        store.flush()
    except StoreError as e:
        raise DatasetError(503, str(e)) from None
    key = _key(mv['id'], rv['id'], inputs, conf)
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (key,))
        same = conn.execute("SELECT id FROM jobs WHERE kind = 'predict' AND params->>'key' = %s "
                            "AND status IN ('queued', 'running', 'done') AND NOT cancel_requested "
                            'ORDER BY id DESC LIMIT 1',
                            (key,)).fetchone()
        if same:
            return dict(jobs.get_job(conn, same['id']), reused=True)
        job_params = {
            'key': key,
            'model': {'id': mv['id'], 'ref': mv['ref'], 'files': onnx, 'classes': mv['classes']},
            'data': {'id': version['id'], 'ref': version['ref'], 'dirs': dirs, 'inputs_sha256': inputs,
                     'images': len(images)},
            'conf': conf,
            'runner': jobs.runner_params(rv)}
        return dict(jobs.enqueue(conn, user, 'predict', rv, job_params), reused=False)


def batch_defaults(conn, name: str) -> dict:
    """What a pre-label of this batch runs with when nothing is said: the model version released on the
    pool's line (None when there is no release yet), and the runners that can run a model."""
    pool = batches.pool_of(conn, name)
    model = None
    if pool['site'] and pool['line']:
        try:
            release = models.current_release(conn, pool['site'], pool['line'])
            model = {'ref': release['model_version']['ref'], 'release': release['ref']}
        except DatasetError:
            pass
    runners = [r['runner'] for r in jobs.list_runners(conn) if r['framework']]
    return {'model': model, 'site': pool['site'], 'line': pool['line'], 'runners': runners}


def submit_for_batch(conn, store: Store, user: dict, name: str, model: Optional[str] = None,
                     runner: Optional[str] = None, conf: Optional[float] = None,
                     revision: Optional[int] = None) -> dict:
    """Queue the pre-labelling of the newest revision of a batch, for its owner. Returns the job (reused: as
    for submit). The job and the batch's note of it are written together. A revision with the same images as
    an earlier one (only labels changed) reuses that revision's job, since the model would answer the same,
    and the batch notes the job for this revision too. One unfinished pre-label per batch: the GPU queue is
    shared by everyone."""
    b = batches.prelabel_target(conn, user, name, revision)
    defaults = batch_defaults(conn, name)
    if not model:
        if defaults['model'] is None:
            raise DatasetError(400, f'{defaults["site"]}/{defaults["line"]} 还没有登记过发布，不知道用哪个模型；'
                                    f'写明模型版本（--model 名称@序号）')
        model = defaults['model']['ref']
    if not runner:
        if len(defaults['runners']) != 1:
            raise DatasetError(400, '要写用哪个推理环境（--runner）：' + (
                f'有 {"、".join(defaults["runners"])}' if defaults['runners'] else '还没有能推理的推理环境，请算法工程师在 GPU 机上登记'))
        runner = defaults['runners'][0]
    params = {'model': model, 'data': b['ref']}
    if conf is not None:
        params['conf'] = conf
    with conn.transaction():
        now = batches.prelabel_target(conn, user, name, b['revision'], lock=True)
        busy = [j for j, _ in batches.prelabel_jobs(conn, name)
                if conn.execute("SELECT 1 FROM jobs WHERE id = %s AND status IN ('queued', 'running')", (j,)).fetchone()]
        job = submit(conn, store, user, runner, params)
        if busy and job['id'] not in busy:  # raising takes the new job back
            raise DatasetError(409, f'{b["name"]} 的预标注任务 {busy[0]} 还没跑完；等它跑完再换模型或分数重跑')
        batches.note_prelabel(conn, user, now, job['id'],
                              f'{job["params"]["model"]["ref"]}，推理环境 {job["runner"]}，最低分数 {job["params"]["conf"]:g}')
    return job


def for_batch(conn, name: str, detail: bool = True) -> List[dict]:
    """The pre-labels asked for a batch, newest first: the job's state and, once done, its prediction set.
    For accounts that see batches but not the job queue; detail false: without the worker's error text (it can
    name paths on the GPU machine), for accounts that may not read jobs."""
    out = []
    for job_id, revision in reversed(batches.prelabel_jobs(conn, name)):
        j = jobs.get_job(conn, job_id)
        result = j['result'] or {}
        out.append({'job_id': j['id'], 'revision': revision, 'status': j['status'], 'status_text': j['status_text'],
                    'queued_ahead': j['queued_ahead'], 'model': j['params']['model']['ref'], 'runner': j['runner'],
                    'conf': j['params']['conf'], 'images': j['params']['data']['images'],
                    'prediction_set': result.get('prediction_set'), 'boxes': result.get('boxes'),
                    'error': j['error'] if detail or not j['error'] else '失败原因请算法工程师在任务页看',
                    'submitted_by': j['submitted_by'], 'created_at': j['created_at'],
                    'finished_at': j['finished_at']})
    return out


_ref_of = datasets.parse_ref


def _box(box: Any, nc: int, conf: float, size: List[int]) -> bool:
    if not isinstance(box, list) or len(box) != 6:
        return False
    x1, y1, x2, y2, score, cls = box
    nums = (x1, y1, x2, y2, score)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in nums):
        return False
    if isinstance(cls, bool) or not isinstance(cls, int) or not 0 <= cls < nc:
        return False
    return x1 <= x2 and y1 <= y2 and conf <= score <= 1.0 + 1e-6


def check_predictions(store: Store, digest: str, size: int, p: dict) -> int:
    """predictions.json against the job's input list: every image once (by its path in the version), a size
    and boxes [x1, y1, x2, y2, score, class] with class in the model's class table and score at or above conf.
    Returns how many boxes there are."""
    if size > MAX_PREDICTIONS:
        raise DatasetError(400, f'predictions.json 不能超过 {MAX_PREDICTIONS >> 20} MB')
    data = store.read(digest)
    raw = store.read(p['data']['inputs_sha256'])
    if data is None or raw is None:
        raise DatasetError(400, 'predictions.json 或输入清单还没有上传')
    try:
        doc = json.loads(data.decode('utf-8'), parse_constant=models._reject_constant)
    except (UnicodeDecodeError, ValueError) as e:
        raise DatasetError(400, f'predictions.json 不是合法的 UTF-8 JSON（{e}）') from None
    wanted = {e[0] for e in parse_manifest(raw)}
    rows: Dict[str, Any] = doc.get('images') if isinstance(doc, dict) else None
    if doc.get('schema_version') != 1 or not isinstance(rows, dict):
        raise DatasetError(400, 'predictions.json 要有 schema_version 1 和 images')
    missing = sorted(wanted - set(rows))
    extra = sorted(set(rows) - wanted)
    if missing or extra:
        raise DatasetError(400, f'predictions.json 和输入清单对不上：少了 {len(missing)} 张'
                                f'{"（例如 " + missing[0] + "）" if missing else ""}，多了 {len(extra)} 张'
                                f'{"（例如 " + extra[0] + "）" if extra else ""}；没有登记')
    nc = len(p['model']['classes'] or [])
    boxes = 0
    for path, row in rows.items():
        ok = isinstance(row, dict) and isinstance(row.get('size'), list) and len(row['size']) == 2 \
            and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in row['size']) \
            and isinstance(row.get('boxes'), list)
        if not ok or not all(_box(b, nc, p['conf'], row['size']) for b in row['boxes']):
            raise DatasetError(400, f'predictions.json 里 {path} 的内容不对（要有 size [宽, 高] 和 boxes '
                                    '[[x1, y1, x2, y2, 分数, 类别], ...]，类别在模型类别表里，分数不低于 conf）')
        boxes += len(row['boxes'])
    return boxes


def register(conn, store: Store, row: dict, result: Any) -> dict:
    """The prediction set of a done prediction job, while the job row is held (evals.finish)."""
    if not isinstance(result, dict) or not isinstance(result.get('files'), list):
        raise DatasetError(400, '推理结果里要有文件清单')
    p = row['params']
    entries = models._entries(result['files'], PREDICTIONS, (PREDICTIONS, ENGINE))
    models._check_stored(store, entries)
    pred = [e for e in entries if e[0] == PREDICTIONS][0]
    boxes = check_predictions(store, pred[1], pred[2], p)
    manifest = models._prepare(store, entries)
    fid = models._insert_fileset(conn, entries, manifest)
    set_id = conn.execute(
        'INSERT INTO prediction_sets (model_version_id, runner_version_id, dataset_version_id, dirs, inputs_sha256, '
        'conf, images, boxes, fileset_id, job_id, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
        'RETURNING id',
        (p['model']['id'], row['runner_version_id'], p['data']['id'], p['data']['dirs'],
         bytes.fromhex(p['data']['inputs_sha256']), p['conf'], p['data']['images'], boxes, fid, row['id'],
         row['submitted_by'])).fetchone()['id']
    events.record(conn, row['submitted_by'], 'prediction_set.register', f'预测集 {set_id}', model=p['model']['ref'],
                  data=p['data']['ref'], job_id=row['id'], images=p['data']['images'], boxes=boxes)
    return {'prediction_set': set_id, 'images': p['data']['images'], 'boxes': boxes}
