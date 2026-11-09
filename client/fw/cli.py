"""fw：数据飞轮平台的命令行。给服务器和训练机用；人日常操作用网页。"""
from __future__ import annotations

import argparse
import datetime
import getpass
import os
import signal
import sys
import time
import unicodedata

import httpx

from . import __version__, config, ops, worker
from .api import Api, ApiError
from .cache import Cache
from .contract import ContractError

ROLE_NAMES = {'admin': '管理员', 'algo': '算法工程师', 'field': '现场实施', 'deploy': '部署', 'worker': 'GPU worker'}
KINDS = {'pool': '数据池', 'test': '测试集', 'regression': '回归集', 'other': '其他'}  # what fw dataset create makes
KIND_NAMES = dict(KINDS, trainset='训练集快照')  # trainsets are made by fw trainset create only


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


def _table(headers, rows) -> None:
    rows = [[str(c) for c in r] for r in rows]
    widths = [max(_width(r[i]) for r in [headers] + rows) for i in range(len(headers))]
    for r in [headers] + rows:
        print('  '.join(c + ' ' * (w - _width(c)) for c, w in zip(r, widths)).rstrip())


def _when(stamp: str) -> str:
    return datetime.datetime.fromisoformat(stamp).astimezone().strftime('%Y-%m-%d %H:%M')


def _ask(prompt: str) -> bool:
    if not sys.stdin.isatty():
        raise ops.FwError('需要确认才能继续：在终端里运行，或者加 --yes')
    return input(f'{prompt}[y/N] ').strip().lower() in ('y', 'yes')


def cmd_login(args) -> None:
    token = args.token or getpass.getpass('凭据（输入时不显示）：').strip()
    api = Api(args.server, token)
    try:
        me = api.me()
    finally:
        api.close()
    path = config.save(args.server.rstrip('/'), token)
    print(f'已登录 {args.server}：{me["name"]}（{ROLE_NAMES.get(me["role"], me["role"])}）；设置存在 {path}')


def cmd_whoami(args, api: Api) -> None:
    me = api.me()
    print(f'{me["name"]}（{ROLE_NAMES.get(me["role"], me["role"])}），凭据 {me["token_label"] or me["token_id"]}，'
          f'服务器 {api.server}')


def cmd_dataset_list(args, api: Api) -> None:
    rows = []
    for d in api.datasets():
        v = d.get('latest')
        rows.append([d['name'], KIND_NAMES.get(d['kind'], d['kind']), d['site'], d['line'],
                     f'@{v["number"]}' if v else '-', v['file_count'] if v else 0,
                     ops.size_text(v['total_bytes']) if v else '-', _when(v['created_at']) if v else '-'])
    if rows:
        _table(['数据集', '类型', '现场', '产线', '最新', '文件数', '大小', '更新时间'], rows)
    else:
        print('还没有数据集；用 fw dataset create 建一个')


def cmd_dataset_create(args, api: Api) -> None:
    d = api.create_dataset(args.name, args.site, args.line, args.kind, args.note)
    print(f'已建数据集 {d["name"]}（{KINDS[d["kind"]]}，现场 {d["site"] or "-"}，产线 {d["line"] or "-"}）；'
          f'用 fw dataset import {d["name"]} <目录> 导入第一个版本')


def cmd_dataset_versions(args, api: Api) -> None:
    rows = []
    for v in api.versions(args.name):
        rows.append([f'@{v["number"]}', v['parent']['ref'] if v.get('parent') else '-', v['file_count'],
                     ops.size_text(v['total_bytes']), _when(v['created_at']), v['created_by'], v['note'] or '',
                     v['source'] if args.source else ''])
    if not rows:
        print(f'{args.name} 还没有版本')
        return
    headers = ['版本', '父版本', '文件数', '大小', '时间', '操作人', '备注', '来源' if args.source else '']
    _table(headers, rows)


def cmd_dataset_import(args, api: Api) -> int:
    result = ops.import_folder(api, args.name, args.folder, parent=args.parent, note=args.note,
                               allow_other_files=args.allow_other_files, force=args.force, dry_run=args.dry_run,
                               confirm=None if args.yes else _ask, workers=args.workers)
    return 0 if result is not None or args.dry_run else 1


def cmd_dataset_diff(args, api: Api) -> None:
    if args.second:
        ops.diff(api, args.second, args.first, show_lists=args.list)
    else:
        ops.diff(api, args.first, show_lists=args.list)


def cmd_get(args, api: Api) -> None:
    cache = Cache()
    try:
        link = 'copy' if args.copy else 'hardlink' if args.hardlink else 'auto'
        ops.get(api, cache, args.ref, args.to, link=link, subpath=args.subpath, workers=args.workers,
                fmt=args.format)
    finally:
        cache.close()


def cmd_export(args, api: Api) -> None:
    cache = Cache()
    try:
        ops.export(api, cache, args.ref, args.to, workers=args.workers)
    finally:
        cache.close()


def cmd_verify(args) -> int:
    return 0 if ops.verify(args.folder, workers=args.workers) else 1


def cmd_cache(args) -> None:
    cache = Cache()
    try:
        if args.action == 'clear':
            n = cache.clear()
            print(f'已清空本机缓存 {cache.root}：删了 {n} 个文件；已经取出的目录不受影响（用 --hardlink 取出的也一样）')
        else:
            count, total = cache.stats()
            print(f'本机缓存 {cache.root}：{count} 个文件，{ops.size_text(total)}')
    finally:
        cache.close()


def cmd_trainset_create(args, api: Api) -> int:
    classes = ops.read_classes(args.classes)
    label_classes = ops.read_classes(args.label_classes) if args.label_classes else None
    class_map = ops.parse_class_map(args.class_map)
    split = ops.read_split_file(args.split_file) if args.split_file else None
    test_dirs = [d for item in args.test_dirs for d in item.split(',') if d.strip()]
    result = ops.create_trainset(api, args.name, args.sources, classes, val=args.val, seed=args.seed,
                                 test_dirs=test_dirs, merge_test=args.merge_test, new_to=args.new_to, split=split,
                                 fresh=args.fresh, note=args.note, require_labels=args.require_labels,
                                 dry_run=args.dry_run, label_source=args.label_source,
                                 label_classes=label_classes, class_map=class_map,
                                 confirm=None if args.yes else _ask)
    return 0 if result is not None or args.dry_run else 1


def cmd_trainset_list(args, api: Api) -> None:
    rows = []
    for d in api.trainsets():
        v = d.get('latest')
        rows.append([d['name'], d['site'], d['line'], f'@{v["number"]}' if v else '-', v['file_count'] if v else 0,
                     ops.size_text(v['total_bytes']) if v else '-', _when(v['created_at']) if v else '-'])
    if rows:
        _table(['训练集', '现场', '产线', '最新', '文件数', '大小', '更新时间'], rows)
    else:
        print('还没有训练集快照；用 fw trainset create 建一个')


def cmd_trainset_show(args, api: Api) -> None:
    name, number = ops.parse_ref(args.ref)
    v = api.trainset(name, number)
    ts = v['trainset']
    rule = ts['rule']
    print(f'{v["ref"]}（id {v["id"]}）：{v["file_count"]} 个文件，{ops.size_text(v["total_bytes"])}，'
          f'{_when(v["created_at"])} {v["created_by"]}' + (f'，备注：{v["note"]}' if v['note'] else ''))
    print(f'来源：{"、".join(ts["sources"])}')
    if rule['mode'] == 'split-file':
        how = f'按划分清单（{rule["split_count"]} 行）'
    else:
        how = (f'接着 {ts["based_on"]}' if ts['based_on'] else '重新划分') + f'，val {rule["val"]}，种子 {rule["seed"]}'
        if rule.get('test_dirs'):
            how += f'，test 目录 {"、".join(rule["test_dirs"])}'
        if rule.get('merge_test'):
            how += '，旧 test 并入 train/val'
        if rule.get('new_to') == 'trainval':
            how += '，新样本进 train/val'
    print(f'规则：{how}')
    labels = ops.label_rule_text(rule)
    if labels:
        print(f'标注：{labels}')
    print(f'标注格式：{"、".join(ops.FORMAT_NAMES.get(f, f) for f in ts["formats"])}（检测框）')
    ops.show_trainset_plan(print, {'counts': ts['counts']}, ts['classes'])


# --- models, evaluations, releases -----------------------------------------------------------------
TASK_NAMES = {'det': '检测'}


def _tags(mv: dict) -> str:
    return '、'.join(ops.TAG_NAMES.get(t, t) for t in mv['tags'])


def _number(v) -> str:
    return f'{v:.4f}'.rstrip('0').rstrip('.') if isinstance(v, float) else str(v)


def _show_metrics(metrics: dict, indent: str = '  ', limit: int = 40) -> None:
    rows = ops.flatten_metrics(metrics)
    for k, v in rows[:limit]:
        print(f'{indent}{k}: {_number(v)}')
    if len(rows) > limit:
        print(f'{indent}……还有 {len(rows) - limit} 项，见 metrics.json')


def cmd_model_create(args, api: Api) -> None:
    m = api.create_model(args.name, args.site, args.line, args.task, args.note)
    print(f'已建模型 {m["name"]}（{m["site"]}/{m["line"]}，{TASK_NAMES.get(m["task"], m["task"])}）；'
          f'登记版本：fw model register {m["name"]} --onnx best.onnx --trainset <快照>')


def cmd_model_list(args, api: Api) -> None:
    rows = api.models()
    if not rows:
        print('还没有模型；用 fw model create 建')
        return
    _table(['名称', '现场', '产线', '任务', '版本数', '最新', '备注'],
           [[m['name'], m['site'], m['line'], TASK_NAMES.get(m['task'], m['task']), m['version_count'],
             m['latest'] or '-', m['note']] for m in rows])


def cmd_model_versions(args, api: Api) -> None:
    rows = api.model_versions(args.name)
    if not rows:
        print(f'{args.name} 还没有版本；用 fw model register 登记')
        return
    _table(['版本', '训练集快照', 'ONNX', '代码提交', '标签', '登记人', '登记时间', '备注'],
           [[v['ref'], v['trainset']['ref'], v['onnx_sha256'][:12], (v['code_commit'] or '-')[:10] + ('*' if v['code_dirty'] else ''),
             _tags(v) or '-', v['created_by'], _when(v['created_at']), v['note']] for v in rows])
    if any(v['code_dirty'] for v in rows):
        print('提交号后面有 * 的：训练时代码有没提交的改动')


def cmd_model_show(args, api: Api) -> None:
    mv = api.model_version(*ops.parse_model_ref(args.ref))
    print(f'{mv["ref"]}：{mv["site"]}/{mv["line"]}，{TASK_NAMES.get(mv["task"], mv["task"])}，'
          f'{mv["created_by"]} 登记于 {_when(mv["created_at"])}')
    print(f'训练集快照：{mv["trainset"]["ref"]}；类别：{"、".join(mv["classes"])}')
    print('训练代码：' + ops.code_text(mv) + ('（训练时有没提交的改动）' if mv['code_dirty'] else ''))
    if mv['tags']:
        print(f'标签：{_tags(mv)}')
    if mv['note']:
        print(f'备注：{mv["note"]}')
    print(f'文件（{mv["file_count"]} 个，{ops.size_text(mv["total_bytes"])}）：')
    for f in mv['files']:
        print(f'  {f["path"]}  {f["sha256"][:12]}  {ops.size_text(f["size"])}')
    if mv['metrics']:
        print('验证指标：')
        _show_metrics(mv['metrics'])
    for e in mv['evals']:
        print(f'评估报告 #{e["id"]}：{e["testset"]["ref"]}' + (f'，{e["tool"]} {e["tool_version"]}' if e['tool'] else '')
              + f'，{e["created_by"]} 登记于 {_when(e["created_at"])}')
    for r in mv['releases']:
        print(f'发布 {r["ref"]}：部署于 {_when(r["deployed_at"])}')


def cmd_model_register(args, api: Api) -> None:
    ops.register_model(api, args.name, args.onnx, args.trainset, args.metrics, args.file, args.code_commit,
                       args.code_repo, args.note, workers=args.workers, weights=args.weights, code_dir=args.code_dir)


def cmd_model_get(args, api: Api) -> None:
    link = 'hardlink' if args.hardlink else 'copy' if args.copy else 'auto'
    cache = Cache()
    try:
        ops.get_model(api, cache, args.ref, args.to, link=link, workers=args.workers)
    finally:
        cache.close()


def cmd_model_tag(args, api: Api) -> None:
    name, number = ops.parse_model_ref(args.ref)
    mv = api.set_tag(name, number, args.tag, not args.remove, args.note)
    label = ops.TAG_NAMES.get(args.tag, args.tag)
    if not mv['changed']:
        print(f'{mv["ref"]} {"本来就没有" if args.remove else "已经有"}标签「{label}」')
    else:
        print(f'{mv["ref"]} {"去掉了" if args.remove else "加上了"}标签「{label}」；现在的标签：{_tags(mv) or "无"}')


def cmd_model_compare(args, api: Api) -> int:
    a = api.model_version(*ops.parse_model_ref(args.first))
    b = api.model_version(*ops.parse_model_ref(args.second))
    if args.testset:
        ea, eb = ops.latest_eval(a, args.testset), ops.latest_eval(b, args.testset)
        for mv, e in ((a, ea), (b, eb)):
            if e is None:
                raise ops.FwError(f'{mv["ref"]} 没有在 {args.testset} 上的评估报告；先 fw eval register')
        print(f'{args.testset} 上的评估：{a["ref"]} 用 #{ea["id"]}，{b["ref"]} 用 #{eb["id"]}')
        old, new = ea['metrics'], eb['metrics']
    else:
        print('验证指标（登记模型版本时的 metrics.json）：')
        old, new = a['metrics'], b['metrics']
    rows = ops.compare_metrics(old, new)
    if not rows:
        print('两边都没有数值指标')
        return 0
    def cell(v):
        return '-' if v is None else _number(v)
    def change(x, y):
        return '' if x is None or y is None else f'{y - x:+.4f}'.rstrip('0').rstrip('.')
    _table(['指标', a['ref'], b['ref'], '变化'], [[k, cell(x), cell(y), change(x, y)] for k, x, y in rows[:args.limit]])
    if len(rows) > args.limit:
        print(f'……还有 {len(rows) - args.limit} 项，加 --limit 看更多')
    return 0


def cmd_model_chain(args, api: Api) -> int:
    c = api.chain(args.ref, check=args.check)
    ops.show_chain(print, c)
    return 1 if c.get('check') and c['check']['problem_count'] else 0


def cmd_eval_register(args, api: Api) -> None:
    ops.register_eval(api, args.model, args.testset, metrics=args.metrics, deteval=args.deteval,
                      deteval_model=args.deteval_model, deteval_set=args.deteval_set, files=args.file, tool=args.tool,
                      tool_version=args.tool_version, note=args.note, workers=args.workers)


def cmd_eval_run(args, api: Api) -> int:
    params = {'model': args.model, 'data': args.data, 'dirs': args.dirs or []}
    job = api.submit_job('eval', runner=args.runner, params=params)
    p = job['params']
    where = p['data']['ref'] + (f':{p["data"]["split"]}' if p['data']['split'] else '')
    print(f'已提交评估任务 {job["id"]}：{p["model"]["ref"]} 在 {where}'
          + (f' 的 {"、".join(p["data"]["dirs"])}' if p['data']['dirs'] else '')
          + f' 上，{p["data"]["images"]} 张图，推理环境 {job["runner"]}；前面还有 {job["queued_ahead"]} 个任务')
    for w in p['warnings']:
        print(f'  注意：{w}')
    if not args.wait:
        print(f'看进度：fw job log -f {job["id"]}；结果：fw job show {job["id"]}')
        return 0
    args.id, args.attempt, args.follow = job['id'], 0, True
    cmd_job_log(args, api)
    j = api.job(job['id'])
    if j['status'] != 'done':
        print(f'评估任务{j["status_text"]}' + (f'：{j["error"]}' if j['error'] else ''))
        return 1
    print(f'已登记评估报告 #{j["result"]["eval_id"]}：fw eval show {j["result"]["eval_id"]}')
    return 0


def cmd_predict_run(args, api: Api) -> int:
    params = {'model': args.model, 'data': args.data, 'dirs': args.dirs or []}
    if args.conf is not None:
        params['conf'] = args.conf
    job = api.submit_job('predict', runner=args.runner, params=params)
    p = job['params']
    dirs = f' 的 {"、".join(p["data"]["dirs"])}' if p['data']['dirs'] else ''
    what = (f'{p["model"]["ref"]} 在 {p["data"]["ref"]}{dirs} 上，{p["data"]["images"]} 张图，推理环境 {job["runner"]}，'
            f'保留分数不低于 {p["conf"]:g} 的框')
    if job.get('reused'):
        print(f'同样的推理（同一模型版本、推理环境、图片和最低分数）已经有了：任务 {job["id"]}（{job["status_text"]}），不再排队')
    else:
        print(f'已提交推理任务 {job["id"]}：{what}；前面还有 {job["queued_ahead"]} 个任务')
    if job['status'] == 'done':
        print(f'预测集 #{job["result"]["prediction_set"]}：fw predict get {job["result"]["prediction_set"]} <目录>')
        return 0
    if not args.wait:
        print(f'看进度：fw job log -f {job["id"]}；结果：fw job show {job["id"]}')
        return 0
    args.id, args.attempt, args.follow = job['id'], 0, True
    cmd_job_log(args, api)
    j = api.job(job['id'])
    if j['status'] != 'done':
        print(f'推理任务{j["status_text"]}' + (f'：{j["error"]}' if j['error'] else ''))
        return 1
    print(f'预测集 #{j["result"]["prediction_set"]}：fw predict get {j["result"]["prediction_set"]} <目录>')
    return 0


def cmd_predict_list(args, api: Api) -> None:
    rows = api.prediction_sets(args.model or '')
    if not rows:
        print('还没有预测集')
        return
    _table(['编号', '模型版本', '数据', '图片', '框', '最低分数', '推理环境', '时间'],
           [[f'#{r["id"]}', r['model_version']['ref'],
             r['data']['ref'] + (f' {"、".join(r["dirs"])}' if r['dirs'] else ''), str(r['images']), str(r['boxes']),
             f'{r["conf"]:g}', r['runner'], _when(r['created_at'])] for r in rows])


def cmd_predict_show(args, api: Api) -> None:
    r = api.prediction_set(int(args.id.lstrip('#')))
    print(f'预测集 #{r["id"]}：{r["model_version"]["ref"]} 在 {r["data"]["ref"]}'
          + (f' 的 {"、".join(r["dirs"])}' if r['dirs'] else '') + f' 上，{r["images"]} 张图，{r["boxes"]} 个框'
          f'（分数不低于 {r["conf"]:g}）')
    print(f'由推理任务 {r["job_id"]} 在推理环境 {r["runner"]} 里算出，{r["created_by"]} 提交，{_when(r["created_at"])}')
    for f in r['files']:
        print(f'  {f["path"]}  {f["sha256"][:12]}  {ops.size_text(f["size"])}')


def cmd_predict_get(args, api: Api) -> None:
    from .cache import Cache
    cache = Cache()
    try:
        ops.get_predictions(api, cache, int(args.id.lstrip('#')), args.dest, threshold=args.threshold,
                            replace=args.replace, link=args.link, workers=args.workers)
    finally:
        cache.close()


def cmd_eval_list(args, api: Api) -> None:
    rows = api.evals(args.model or '')
    if not rows:
        print('还没有评估报告')
        return
    _table(['编号', '模型版本', '测试集', '工具', '登记人', '登记时间', '备注'],
           [[f'#{e["id"]}', e['model_version']['ref'], e['testset']['ref'],
             f'{e["tool"]} {e["tool_version"]}'.strip() or '-', e['created_by'], _when(e['created_at']), e['note']]
            for e in rows])


def cmd_eval_show(args, api: Api) -> None:
    e = api.eval(int(args.id.lstrip('#')))
    print(f'评估报告 #{e["id"]}：{e["model_version"]["ref"]} 在 {e["testset"]["ref"]}'
          + (f' 的 {"、".join(e["dirs"])}' if e.get('dirs') else '') + ' 上'
          + (f'，{e["tool"]} {e["tool_version"]}' if e['tool'] else '')
          + f'，{e["created_by"]} 登记于 {_when(e["created_at"])}')
    if e.get('job_id'):
        print(f'由评估任务 {e["job_id"]} 在推理环境 {e["runner"]} 里算出')
    if e['note']:
        print(f'备注：{e["note"]}')
    print(f'文件（{e["file_count"]} 个，{ops.size_text(e["total_bytes"])}）：')
    for f in e['files']:
        print(f'  {f["path"]}  {f["sha256"][:12]}  {ops.size_text(f["size"])}')
    print('指标：')
    _show_metrics(e['metrics'])


def _runner_text(r: dict) -> str:
    src = (f'{r["src_dir"]} @ {r["src_commit"][:12]}{"（有未提交的改动）" if r["src_dirty"] else ""}'
           if r['src_dir'] else '（没登记源码）')
    return (f'conda 环境 {r["conda_env"]}（包清单 {r["env_sha256"][:12]}），源码 {src}'
            + (f'，框架 {r["framework"]}，onnx2engine {r["onnx2engine"]}（{(r.get("onnx2engine_sha256") or "")[:12]}）'
               if r.get('framework') else '，没写框架（只能做检查，不能评估）'))


def cmd_runner_register(args, api: Api) -> None:
    try:
        r = worker.register_runner(api, args.name, args.env, args.src or '', args.framework or '',
                                   args.onnx2engine or '')
    except worker.WorkerError as e:
        raise ops.FwError(str(e)) from None
    if r['created']:
        print(f'已登记 {r["ref"]}（在 {r["worker"]} 上）：{_runner_text(r)}')
    else:
        print(f'和 {r["ref"]} 一样，没有新版本：{_runner_text(r)}')


def cmd_runner_list(args, api: Api) -> None:
    rows = api.runners()
    if not rows:
        print('还没有推理环境；在 GPU 机上用 worker 账号运行 fw runner register 登记')
        return
    _table(['推理环境', '机器', 'conda 环境', '包清单', '源码提交', '登记时间'],
           [[r['ref'], r['worker'], r['conda_env'], r['env_sha256'][:12],
             (r['src_commit'][:12] + ('*' if r['src_dirty'] else '')) if r['src_commit'] else '-',
             _when(r['created_at'])] for r in rows])


def cmd_runner_show(args, api: Api) -> None:
    for r in api.runner(args.name):
        print(f'{r["ref"]}  {_when(r["created_at"])}  {r["worker"]}：{_runner_text(r)}')


def cmd_runner_check(args, api: Api) -> None:
    job = api.submit_job('check', runner=args.name)
    print(f'已提交任务 {job["id"]}：在 {job["runner"]} 所在的机器上检查环境和源码有没有变；'
          f'前面还有 {job["queued_ahead"]} 个任务。看结果：fw job show {job["id"]}')


def _job_row(j: dict) -> list:
    status = j['status_text'] + ('（取消中）' if j['cancel_requested'] and j['status'] == 'running' else '')
    if j['status'] == 'queued' and j['queued_ahead']:
        status += f'（前面 {j["queued_ahead"]} 个）'
    return [j['id'], j['kind_text'], j['runner'] or '-', status, j['worker'] or '-', j['submitted_by'],
            _when(j['created_at'])]


def cmd_job_list(args, api: Api) -> None:
    rows = api.jobs(args.status or '', args.limit)
    if not rows:
        print('没有任务')
        return
    _table(['编号', '类型', '推理环境', '状态', 'worker', '提交人', '提交时间'], [_job_row(j) for j in rows])


def _print_job(j: dict) -> None:
    print(f'任务 {j["id"]}：{j["kind_text"]}' + (f'，推理环境 {j["runner"]}' if j['runner'] else '')
          + f'，{j["submitted_by"]} 提交于 {_when(j["created_at"])}')
    print(f'状态：{_job_row(j)[3]}，领取 {j["attempts"]}/{j["max_attempts"]} 次'
          + (f'，在 {j["worker"]} 上运行' if j['worker'] else ''))
    for h in j.get('history', []):
        how = {'done': '完成', 'failed': '失败', 'cancelled': '已取消', 'lost': '失联（租约过期）', None: '运行中'}
        print(f'  第 {h["attempt"]} 次：{h["worker"]}，{_when(h["started_at"])} 开始，{how[h["outcome"]]}')
    if j['error']:
        print(f'原因：{j["error"]}')
    if j['result'] is not None:
        if j['kind'] == 'check':
            r = j['result']
            print('结果：包清单' + ('没变' if r['same_env'] else '变了') + '，源码' + ('没变' if r['same_src'] else '变了'))
        elif j['kind'] == 'predict':
            r = j['result']
            print(f'结果：预测集 #{r["prediction_set"]}，{r["images"]} 张图，{r["boxes"]} 个框'
                  f'（fw predict get {r["prediction_set"]} <目录>）')
        elif j['kind'] == 'eval':
            m = j['result']['metrics']
            print(f'结果：评估报告 #{j["result"]["eval_id"]}（fw eval show {j["result"]["eval_id"]}），'
                  f'{m.get("images")} 张图，mAP50 {m.get("mAP50")}，mAP50-95 {m.get("mAP50-95")}')
        else:
            print(f'结果：{j["result"]}')


def cmd_job_show(args, api: Api) -> None:
    _print_job(api.job(args.id))


def cmd_job_log(args, api: Api) -> None:
    after, attempt = -1, None
    while True:
        piece = api.job_log(args.id, args.attempt, after)
        if attempt is not None and piece['attempt'] != attempt:
            # the job was claimed again: its pieces are numbered from 0 again, so read this claim from the start
            attempt, after = piece['attempt'], -1
            print(f'\n--- 第 {attempt} 次领取 ---')
            continue
        attempt = piece['attempt']
        for p in piece['pieces']:
            sys.stdout.write(p['text'])
            after = p['seq']
        sys.stdout.flush()
        if not args.follow or piece['status'] in ('done', 'failed', 'cancelled'):
            if args.follow:
                print(f'（任务{api.job(args.id)["status_text"]}）')
            return
        time.sleep(2)


def cmd_job_cancel(args, api: Api) -> None:
    j = api.cancel_job(args.id)
    if j['status'] == 'cancelled':
        print(f'任务 {j["id"]} 已取消')
    else:
        print(f'已通知 {j["worker"]} 停下任务 {j["id"]}；它在下次续租时停下（默认最多约 20 秒）并报告已取消')


def cmd_worker(args, api: Api) -> None:
    me = api.me()
    if me['role'] != 'worker':
        raise ops.FwError(f'fw worker 要用 worker 账号的凭据（现在是 {ROLE_NAMES.get(me["role"], me["role"])}账号 '
                          f'{me["name"]}）；管理员用 fw-server user add <名字> --role worker 建账号')
    print(f'worker {me["name"]} 开始领任务（Ctrl+C 停止；停下时正在跑的任务会在租约到期后回到队列）')
    n = worker.run_worker(api, print, poll=args.poll, once=args.once)
    if args.once:
        print(f'队列里没有这台机器能做的任务了，这次做了 {n} 个')


def cmd_release_register(args, api: Api) -> None:
    ops.register_release(api, args.model, args.deploy_repo, args.deploy_commit, args.deployed_at, args.site,
                         args.line, args.gpu, args.trt, args.precision, args.engine_file, args.note)


def _release_rows(rows):
    return [[r['ref'], r['model_version']['ref'], _when(r['deployed_at']), r['deploy_commit'][:10],
             '，'.join(x for x in (r['gpu'], r['trt_version'] and f'TRT {r["trt_version"]}', r['precision'].upper()) if x)
             or '-', r['created_by'], r['note']] for r in rows]


def cmd_release_list(args, api: Api) -> None:
    rows = api.releases(args.site or '', args.line or '')
    if not rows:
        print('还没有登记过发布')
        return
    _table(['发布', '模型版本', '部署时间', '部署提交', '引擎', '登记人', '备注'], _release_rows(rows))


def cmd_release_current(args, api: Api) -> None:
    r = api.current_release(args.site, args.line)
    print(f'{r["site"]}/{r["line"]} 现在用的是 {r["model_version"]["ref"]}（发布 {r["ref"]}，部署于 {_when(r["deployed_at"])}）')


def cmd_release_show(args, api: Api) -> None:
    ops.show_chain(print, api.chain(args.ref))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='fw', description='数据飞轮平台命令行：数据集版本的导入、取回、对比和导出，'
                                                       '训练集快照，模型版本、评估、发布和追溯链。')
    p.add_argument('--version', action='version', version=f'fw {__version__}')
    sub = p.add_subparsers(dest='command', required=True, metavar='命令')

    s = sub.add_parser('login', help='记下服务器地址和凭据')
    s.add_argument('--server', required=True, help='平台地址，例如 http://10.0.0.5:8000')
    s.add_argument('--token', help='凭据（不写就在提示里输入，不会留在命令历史里）')
    s.set_defaults(func=cmd_login, needs_api=False)

    s = sub.add_parser('whoami', help='看当前账号')
    s.set_defaults(func=cmd_whoami)

    ds = sub.add_parser('dataset', help='数据集：list、create、versions、import、diff').add_subparsers(
        dest='action', required=True, metavar='操作')
    s = ds.add_parser('list', help='列出数据集')
    s.set_defaults(func=cmd_dataset_list)
    s = ds.add_parser('create', help='新建数据集')
    s.add_argument('name', help='名称：字母、数字、. _ -，例如 SES-TX')
    s.add_argument('--site', default='', help='现场，例如 SES')
    s.add_argument('--line', default='', help='产线，例如 TX')
    s.add_argument('--kind', default='pool', choices=list(KINDS), help='pool 数据池（默认）、test、regression、other')
    s.add_argument('--note', default='', help='备注')
    s.set_defaults(func=cmd_dataset_create)
    s = ds.add_parser('versions', help='列出一个数据集的版本')
    s.add_argument('name')
    s.add_argument('--source', action='store_true', help='同时显示每个版本从哪里导入')
    s.set_defaults(func=cmd_dataset_versions)
    s = ds.add_parser('import', help='检查一个目录，作为下一个版本入库')
    s.add_argument('name', help='数据集名称')
    s.add_argument('folder', help='要导入的目录；整个目录就是新版本的全部内容')
    s.add_argument('--parent', default='latest', help='父版本：latest（默认）、none 或版本序号')
    s.add_argument('--note', default='', help='备注，例如这一版改了什么')
    s.add_argument('--allow-other-files', action='store_true', help='图片和标注以外的文件也一起入库')
    s.add_argument('--force', action='store_true', help='有同名不同扩展名的图片也入库')
    s.add_argument('--dry-run', action='store_true', help='只检查和预览，不上传')
    s.add_argument('--yes', action='store_true', help='不问确认，直接建版本')
    s.add_argument('--workers', type=int, default=8, help='并行数（默认 8）')
    s.set_defaults(func=cmd_dataset_import)
    s = ds.add_parser('diff', help='对比两个版本；只写一个就和它的父版本比')
    s.add_argument('first', help='旧版本，例如 SES-TX@1；只写这一个时，看它相比父版本改了什么')
    s.add_argument('second', nargs='?', help='新版本，例如 SES-TX@3')
    s.add_argument('--list', action='store_true', help='列出全部变化的文件（制表符分隔）')
    s.set_defaults(func=cmd_dataset_diff)

    ts = sub.add_parser('trainset', help='训练集快照：create、list、show').add_subparsers(
        dest='action', required=True, metavar='操作')
    s = ts.add_parser('create', help='从数据池版本建训练集快照（划分好 train/val/test 并冻结）')
    s.add_argument('name', help='快照名称，例如 SES-TX-det；再次运行就是它的下一版')
    s.add_argument('--from', dest='sources', action='append', required=True, metavar='版本',
                   help='来源数据池版本，例如 SES-TX@4；可写多次')
    s.add_argument('--classes', required=True, help='类别表 classes.txt：一行一个类别名，行号（从 0 起）是类别编号')
    s.add_argument('--val', type=float, default=0.2, help='train/val 里 val 的比例（默认 0.2）')
    s.add_argument('--seed', type=int, default=0, help='划分用的种子（默认 0）；同样的样本和种子总是同样的划分')
    s.add_argument('--test-dirs', action='append', default=[], metavar='目录',
                   help='这些目录下的新样本进 test，例如 2026-10 或 SES/TX/2026-10；逗号分隔或写多次')
    s.add_argument('--merge-test', action='store_true', help='上一版 test 里的样本重新分到 train/val')
    s.add_argument('--new-to', choices=['test', 'trainval'], default='test',
                   help='接着上一版时，新样本去哪：test（默认）或 trainval')
    s.add_argument('--split-file', help='用自己的划分清单：每行「<现场>/<产线>/<图片路径> train|val|test」')
    s.add_argument('--fresh', action='store_true', help='不接着上一版的划分，全部重新分')
    s.add_argument('--require-labels', action='store_true',
                   help='每张图都要有标注，有没标注的图就不建（默认没标注的图当负样本收进来）')
    s.add_argument('--label-source', choices=('json', 'txt'),
                   help='一张图同时有 JSON 和 txt 标注时用哪一种（不写就拒绝建）')
    s.add_argument('--label-classes', metavar='类别表',
                   help='txt 标注自己的类别表（行号是 txt 里的类别编号），和 --classes 不同时用；不写就按 --classes 读')
    s.add_argument('--map', dest='class_map', action='append', default=[], metavar='源=目标',
                   help='把标注里的一个类别算作类别表里的另一个，例如 DL1=DL；目标留空就去掉这类框、当背景，'
                        '例如 others=；可写多次。数据池里的标注不变')
    s.add_argument('--note', default='', help='备注')
    s.add_argument('--dry-run', action='store_true', help='只预览划分和统计，不建快照')
    s.add_argument('--yes', action='store_true', help='不问确认，直接建快照')
    s.set_defaults(func=cmd_trainset_create)
    s = ts.add_parser('list', help='列出训练集快照')
    s.set_defaults(func=cmd_trainset_list)
    s = ts.add_parser('show', help='看一个快照的来源、划分和每类框数')
    s.add_argument('ref', help='快照，例如 SES-TX-det@2；只写名称就是最新版')
    s.set_defaults(func=cmd_trainset_show)

    md = sub.add_parser('model', help='模型：create、list、versions、show、register、get、tag、compare、chain').add_subparsers(
        dest='action', required=True, metavar='操作')
    s = md.add_parser('create', help='新建模型（一条产线上的一种模型）')
    s.add_argument('name', help='名称：字母、数字、. _ -，例如 SES-TX-detr')
    s.add_argument('--site', required=True, help='现场，例如 SES')
    s.add_argument('--line', required=True, help='产线，例如 TX')
    s.add_argument('--task', default='det', choices=list(TASK_NAMES), help='任务类型：det（检测，默认）')
    s.add_argument('--note', default='', help='备注')
    s.set_defaults(func=cmd_model_create)
    s = md.add_parser('list', help='列出模型')
    s.set_defaults(func=cmd_model_list)
    s = md.add_parser('versions', help='列出一个模型的版本')
    s.add_argument('name')
    s.set_defaults(func=cmd_model_versions)
    s = md.add_parser('show', help='看一个模型版本：文件、快照、代码、指标、评估和发布')
    s.add_argument('ref', help='模型版本，例如 SES-TX-detr@3；只写名称就是最新版')
    s.set_defaults(func=cmd_model_show)
    s = md.add_parser('register', help='上传 ONNX，登记为模型的下一个版本')
    s.add_argument('name', help='模型名称（先用 fw model create 建）')
    s.add_argument('--onnx', required=True, help='ONNX 文件')
    s.add_argument('--trainset', required=True, help='训练用的训练集快照，例如 SES-TX-det@2')
    s.add_argument('--weights', help='训练出的权重（.pt、.pth），登记在 weights/ 下，以后平台用它统一评估')
    s.add_argument('--metrics', help='可选：训练框架自己的验证指标 JSON，只作参考')
    s.add_argument('--file', action='append', default=[], metavar='文件或目录',
                   help='附件：训练配置等，登记在 files/ 下；可写多次')
    s.add_argument('--code-dir', help='可选：训练代码的 git 目录，记下它的 HEAD 和 origin（去掉其中的账号和令牌）')
    s.add_argument('--code-commit', help='可选：训练代码的 git 提交号（不用 --code-dir 时）')
    s.add_argument('--code-repo', help='可选：训练代码仓库地址，和 --code-commit 一起写')
    s.add_argument('--note', default='', help='备注')
    s.add_argument('--workers', type=int, default=8)
    s.set_defaults(func=cmd_model_register)
    s = md.add_parser('get', help='把模型版本的文件取到本地目录（部署账号也能用）')
    s.add_argument('ref', help='模型版本，例如 SES-TX-detr@3')
    s.add_argument('--to', required=True, help='目标目录（不存在或为空）')
    g = s.add_mutually_exclusive_group()
    g.add_argument('--copy', action='store_true', help='总是复制（默认先试 reflink，不支持再复制）')
    g.add_argument('--hardlink', action='store_true', help='硬链接（只限 Linux），文件只读')
    s.add_argument('--workers', type=int, default=8)
    s.set_defaults(func=cmd_model_get)
    s = md.add_parser('tag', help='给模型版本加上或去掉标签 candidate（候选）、deprecated（弃用）')
    s.add_argument('ref', help='模型版本，例如 SES-TX-detr@3')
    s.add_argument('tag', choices=list(ops.TAG_NAMES))
    s.add_argument('--remove', action='store_true', help='去掉这个标签')
    s.add_argument('--note', default='', help='为什么改')
    s.set_defaults(func=cmd_model_tag)
    s = md.add_parser('compare', help='对比两个模型版本的指标')
    s.add_argument('first', help='旧版本，例如 SES-TX-detr@2')
    s.add_argument('second', help='新版本，例如 SES-TX-detr@3')
    s.add_argument('--testset', help='对比在这个测试集上的评估报告（例如 SES-TX-det@2:test）；不写就对比验证指标')
    s.add_argument('--limit', type=int, default=60, help='最多列多少项（默认 60）')
    s.set_defaults(func=cmd_model_compare)
    s = md.add_parser('chain', help='追溯链：从发布或模型版本一路查到数据池版本')
    s.add_argument('ref', help='发布（例如 SES-TX-r3）或模型版本（例如 SES-TX-detr@3）')
    s.add_argument('--check', action='store_true', help='同时核对链上每个文件都还在文件库里')
    s.set_defaults(func=cmd_model_chain)

    pr = sub.add_parser('predict', help='推理和预标注：run、list、show、get').add_subparsers(
        dest='action', required=True, metavar='操作')
    s = pr.add_parser('run', help='排一个推理任务：GPU 机用模型的 TensorRT 引擎跑选中的图，结果存成预测集')
    s.add_argument('model', help='模型版本，例如 SES-TX-detr@3')
    s.add_argument('--data', required=True, help='数据集版本（数据池），例如 SES-TX@6')
    s.add_argument('--dirs', nargs='+', metavar='目录', help='只推理版本里的这几个目录（包括子目录）；不写就是整个版本')
    s.add_argument('--runner', required=True, help='推理环境，例如 detr-pro（fw runner list 看有哪些）')
    s.add_argument('--conf', type=float, help='保留的最低分数，默认 0.1；预标注阈值只能在它之上调')
    s.add_argument('--wait', action='store_true', help='等任务跑完，跟着显示输出')
    s.set_defaults(func=cmd_predict_run)
    s = pr.add_parser('list', help='列出预测集')
    s.add_argument('model', nargs='?', help='只列这个模型的')
    s.set_defaults(func=cmd_predict_list)
    s = pr.add_parser('show', help='看一个预测集')
    s.add_argument('id', help='预测集编号，例如 3 或 #3')
    s.set_defaults(func=cmd_predict_show)
    s = pr.add_parser('get', help='取出预测集的图片和预标注（LiteAnno 能直接打开的目录）')
    s.add_argument('id', help='预测集编号，例如 3 或 #3')
    s.add_argument('dest', help='放到哪个目录（要是空的或不存在）')
    s.add_argument('--threshold', type=float, help='分数阈值，默认 0.3，不能低于推理时的最低分数')
    s.add_argument('--replace', action='store_true', help='已有标注的图也写预标注（默认保留已有标注）')
    s.add_argument('--link', default='auto', choices=['auto', 'copy', 'hardlink'], help='同 fw get')
    s.add_argument('--workers', type=int, default=8)
    s.set_defaults(func=cmd_predict_get)
    ev = sub.add_parser('eval', help='评估：run、register、list、show').add_subparsers(
        dest='action', required=True, metavar='操作')
    s = ev.add_parser('run', help='排一个评估任务：GPU 机在推理环境里跑 aoi-deteval，结果自动登记成评估报告')
    s.add_argument('model', help='模型版本，例如 SES-TX-detr@3（要登记过权重）')
    s.add_argument('--data', required=True,
                   help='在哪个数据集版本上评，例如 SES-TX@5；训练集快照写划分，例如 SES-TX-det@2:test')
    s.add_argument('--dirs', nargs='+', metavar='目录',
                   help='只评版本里的这几个目录（包括下面的子目录），例如 SES/TX/test_1015；不写就是整个版本')
    s.add_argument('--runner', required=True, help='推理环境，例如 detr-pro 或 detr-pro@2（fw runner list 看有哪些）')
    s.add_argument('--wait', action='store_true', help='等任务跑完，跟着显示输出')
    s.set_defaults(func=cmd_eval_run)
    s = ev.add_parser('register', help='登记一个模型版本在测试集上的评估结果')
    s.add_argument('model', help='模型版本，例如 SES-TX-detr@3')
    s.add_argument('--testset', required=True,
                   help='测试集版本，例如 SES-TX-test@1；训练集快照要写划分，例如 SES-TX-det@2:test')
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument('--deteval', metavar='目录', help='aoi-deteval evaluate 的输出目录（summary.json、run_info.json 等，整个目录都登记）')
    g.add_argument('--metrics', help='别的评估工具的指标 JSON（一个 JSON 对象）')
    s.add_argument('--deteval-model', help='summary.json 里有几个模型时，这个模型版本对应哪一个')
    s.add_argument('--deteval-set', help='aoi-deteval 结果里哪个集合是这个测试集（默认是和划分同名的那个）；'
                                         '评的图片数要和测试集一样')
    s.add_argument('--file', action='append', default=[], metavar='文件或目录', help='附带的报告文件；可写多次')
    s.add_argument('--tool', help='评估工具名（用 --deteval 时是 aoi-deteval）')
    s.add_argument('--tool-version', help='评估工具版本（用 --deteval 时从 run_info.json 读）')
    s.add_argument('--note', default='', help='备注')
    s.add_argument('--workers', type=int, default=8)
    s.set_defaults(func=cmd_eval_register)
    s = ev.add_parser('list', help='列出评估报告')
    s.add_argument('model', nargs='?', help='只列这个模型的')
    s.set_defaults(func=cmd_eval_list)
    s = ev.add_parser('show', help='看一份评估报告的文件和指标')
    s.add_argument('id', help='编号，例如 12')
    s.set_defaults(func=cmd_eval_show)

    rl = sub.add_parser('release', help='发布：register、list、current、show').add_subparsers(
        dest='action', required=True, metavar='操作')
    s = rl.add_parser('register', help='部署成功后登记一次发布（部署账号或管理员）')
    s.add_argument('--model', required=True, help='部署的模型版本，例如 SES-TX-detr@3')
    s.add_argument('--site', default='', help='现场；不写就是模型的现场')
    s.add_argument('--line', default='', help='产线；不写就是模型的产线')
    s.add_argument('--deploy-repo', required=True, help='部署仓库地址（不要带账号和令牌）')
    s.add_argument('--deploy-commit', required=True, help='部署仓库的 git 提交号')
    s.add_argument('--deployed-at', help='部署成功的时间，例如 2026-11-20T10:30+08:00；不写就是现在')
    s.add_argument('--gpu', default='', help='现场构建引擎用的 GPU，例如 RTX3070')
    s.add_argument('--trt', default='', help='现场的 TensorRT 版本，例如 10.3')
    s.add_argument('--precision', default='', choices=['', 'fp32', 'fp16'], help='引擎精度 fp32 或 fp16')
    s.add_argument('--engine-file', help='现场从 ONNX 构建的引擎文件：只记它的哈希和大小，文件不上传')
    s.add_argument('--note', default='', help='备注')
    s.set_defaults(func=cmd_release_register)
    s = rl.add_parser('list', help='列出发布，按部署时间排')
    s.add_argument('--site')
    s.add_argument('--line')
    s.set_defaults(func=cmd_release_list)
    s = rl.add_parser('current', help='一条产线现在用的是哪个模型（部署时间最晚的发布）')
    s.add_argument('--site', required=True)
    s.add_argument('--line', required=True)
    s.set_defaults(func=cmd_release_current)
    s = rl.add_parser('show', help='看一次发布和它的追溯链')
    s.add_argument('ref', help='发布编号，例如 SES-TX-r3')
    s.set_defaults(func=cmd_release_show)

    rn = sub.add_parser('runner', help='推理环境（GPU 机上的 conda 环境）：register、list、show、check').add_subparsers(
        dest='cmd', required=True)
    s = rn.add_parser('register', help='在 GPU 机上用 worker 账号登记一个推理环境；环境或源码变了再登记就是新版本')
    s.add_argument('name', help='推理环境名，例如 yolo26、detr-pro')
    s.add_argument('--env', required=True, help='conda 环境名')
    s.add_argument('--src', help='框架源码目录（git 仓库）；用 pip 装的官方版框架就不写')
    s.add_argument('--framework', choices=['rfdetr'],
                   help='用哪个部署代码的推理模块跑 TensorRT 引擎（rfdetr：py_rfdetr）；评估用的环境要写，'
                        '环境里要有 tensorrt、OpenCV、这个推理模块和 aoi-deteval')
    s.add_argument('--onnx2engine', help='部署代码的 onnx2engine 程序（从 ONNX 建 TensorRT 引擎），和 --framework 一起写')
    s.set_defaults(func=cmd_runner_register)
    s = rn.add_parser('list', help='列出推理环境（每个的最新版本）')
    s.set_defaults(func=cmd_runner_list)
    s = rn.add_parser('show', help='一个推理环境的全部版本')
    s.add_argument('name')
    s.set_defaults(func=cmd_runner_show)
    s = rn.add_parser('check', help='提交一个任务，在 GPU 机上检查环境和源码有没有变')
    s.add_argument('name', help='推理环境，例如 yolo26 或 yolo26@2')
    s.set_defaults(func=cmd_runner_check)

    jb = sub.add_parser('job', help='任务：list、show、log、cancel').add_subparsers(dest='cmd', required=True)
    s = jb.add_parser('list', help='列出任务，最新的在前')
    s.add_argument('--status', choices=['queued', 'running', 'done', 'failed', 'cancelled'])
    s.add_argument('--limit', type=int, default=50)
    s.set_defaults(func=cmd_job_list)
    s = jb.add_parser('show', help='看一个任务的状态、每次领取和结果')
    s.add_argument('id', type=int)
    s.set_defaults(func=cmd_job_show)
    s = jb.add_parser('log', help='看任务的输出')
    s.add_argument('id', type=int)
    s.add_argument('--attempt', type=int, default=0, help='第几次领取的输出（默认最近一次）')
    s.add_argument('-f', '--follow', action='store_true', help='一直跟着，直到任务结束')
    s.set_defaults(func=cmd_job_log)
    s = jb.add_parser('cancel', help='取消任务：排队的立即取消，运行中的通知 worker 停下')
    s.add_argument('id', type=int)
    s.set_defaults(func=cmd_job_cancel)

    s = sub.add_parser('worker', help='在 GPU 机上常驻，领取并运行任务（要 worker 账号）')
    s.add_argument('--poll', type=float, default=5.0, help='没有任务时每隔几秒问一次（默认 5）')
    s.add_argument('--once', action='store_true', help='把能做的任务做完就退出')
    s.set_defaults(func=cmd_worker)

    s = sub.add_parser('get', help='把一个版本取到本地目录')
    s.add_argument('ref', help='版本，例如 SES-TX@3；只写名称就是最新版')
    s.add_argument('--to', required=True, help='目标目录（不存在或为空）')
    g = s.add_mutually_exclusive_group()
    g.add_argument('--copy', action='store_true', help='总是复制（默认先试 reflink，不支持再复制）')
    g.add_argument('--hardlink', action='store_true',
                   help='硬链接（只限 Linux）：不多占盘，但文件只读、和缓存共用同一份数据，取出后不能改')
    s.add_argument('--subpath', help='只取这个子目录')
    s.add_argument('--format', choices=['yolo', 'coco', 'json', 'all'],
                   help='训练集快照按哪种格式摆：yolo（默认，另生成 data.yaml）、coco、json（LiteAnno 能直接打开）、all（原样）')
    s.add_argument('--workers', type=int, default=8)
    s.set_defaults(func=cmd_get)

    s = sub.add_parser('export', help='导出一个版本，拷到连不上平台的机器上用')
    s.add_argument('ref')
    s.add_argument('--to', required=True, help='目标目录（不存在或为空）')
    s.add_argument('--workers', type=int, default=8)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser('verify', help='按清单核对导出的目录（不用连平台）')
    s.add_argument('folder')
    s.add_argument('--workers', type=int, default=8)
    s.set_defaults(func=cmd_verify, needs_api=False)

    s = sub.add_parser('cache', help='本机缓存：info 看大小，clear 清空')
    s.add_argument('action', choices=['info', 'clear'])
    s.set_defaults(func=cmd_cache, needs_api=False)
    return p


def _interrupted() -> None:
    """Ctrl+C: say so and exit at once with 130. No cleanup runs first: closing the cache or the connection
    could wait for an upload or download thread still running, and nothing needs it (the server keeps only
    what it has checked, the cache replaces files atomically, SQLite survives being cut off)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, RuntimeError, ValueError):  # RuntimeError: Ctrl+C came in the middle of a write
            pass
    try:
        sys.stderr.write('\n已中断\n')
        sys.stderr.flush()
    except (OSError, RuntimeError, ValueError):
        pass
    os._exit(130)


def _on_sigint(signum, frame) -> None:
    _interrupted()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')  # a path the console cannot show must not stop the command
        except AttributeError:
            pass
    args = build_parser().parse_args(argv)
    api = None
    try:
        previous_sigint = signal.signal(signal.SIGINT, _on_sigint)
    except ValueError:  # not the main thread (main() called from a thread): KeyboardInterrupt never comes here
        previous_sigint = None
    try:
        if getattr(args, 'needs_api', True):
            api = ops.connect()
            result = args.func(args, api)
        else:
            result = args.func(args)
        return result or 0
    except (ops.FwError, ApiError, ContractError) as e:
        print(f'错误：{e}', file=sys.stderr)
        return 1
    except httpx.TransportError as e:
        print(f'错误：连不上平台服务（{e.__class__.__name__}: {e}）', file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # raised by code rather than by Ctrl+C, which _on_sigint handles at once
        _interrupted()
        return 130  # only reached when os._exit is replaced in tests
    finally:
        if previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)
        if api is not None:
            api.close()


if __name__ == '__main__':
    sys.exit(main())
