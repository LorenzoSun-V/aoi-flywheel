"""fw：数据飞轮平台的命令行。给服务器和训练机用；人日常操作用网页。"""
from __future__ import annotations

import argparse
import datetime
import getpass
import os
import signal
import sys
import unicodedata

import httpx

from . import __version__, config, ops
from .api import Api, ApiError
from .cache import Cache
from .contract import ContractError

ROLE_NAMES = {'admin': '管理员', 'algo': '算法工程师', 'field': '现场实施', 'deploy': '部署'}
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
        ops.get(api, cache, args.ref, args.to, link=link, subpath=args.subpath, workers=args.workers)
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
    split = ops.read_split_file(args.split_file) if args.split_file else None
    test_dirs = [d for item in args.test_dirs for d in item.split(',') if d.strip()]
    result = ops.create_trainset(api, args.name, args.sources, classes, val=args.val, seed=args.seed,
                                 test_dirs=test_dirs, merge_test=args.merge_test, new_to=args.new_to, split=split,
                                 fresh=args.fresh, note=args.note, dry_run=args.dry_run,
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
    ops.show_trainset_plan(print, {'counts': ts['counts']}, ts['classes'])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='fw', description='数据飞轮平台命令行：数据集版本的导入、取回、对比和导出，'
                                                       '训练集快照。')
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
    s.add_argument('--note', default='', help='备注')
    s.add_argument('--dry-run', action='store_true', help='只预览划分和统计，不建快照')
    s.add_argument('--yes', action='store_true', help='不问确认，直接建快照')
    s.set_defaults(func=cmd_trainset_create)
    s = ts.add_parser('list', help='列出训练集快照')
    s.set_defaults(func=cmd_trainset_list)
    s = ts.add_parser('show', help='看一个快照的来源、划分和每类框数')
    s.add_argument('ref', help='快照，例如 SES-TX-det@2；只写名称就是最新版')
    s.set_defaults(func=cmd_trainset_show)

    s = sub.add_parser('get', help='把一个版本取到本地目录')
    s.add_argument('ref', help='版本，例如 SES-TX@3；只写名称就是最新版')
    s.add_argument('--to', required=True, help='目标目录（不存在或为空）')
    g = s.add_mutually_exclusive_group()
    g.add_argument('--copy', action='store_true', help='总是复制（默认先试 reflink，不支持再复制）')
    g.add_argument('--hardlink', action='store_true',
                   help='硬链接（只限 Linux）：不多占盘，但文件只读、和缓存共用同一份数据，取出后不能改')
    s.add_argument('--subpath', help='只取这个子目录')
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
