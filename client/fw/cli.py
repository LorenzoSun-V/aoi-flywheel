"""fw：数据飞轮平台的命令行。给服务器和训练机用；人日常操作用网页。"""
from __future__ import annotations

import argparse
import datetime
import getpass
import sys
import unicodedata

import httpx

from . import __version__, config, ops
from .api import Api, ApiError
from .cache import Cache
from .contract import ContractError

ROLE_NAMES = {'admin': '管理员', 'algo': '算法工程师', 'field': '现场实施', 'deploy': '部署'}
KINDS = {'pool': '数据池', 'test': '测试集', 'regression': '回归集', 'other': '其他'}


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
        rows.append([d['name'], KINDS.get(d['kind'], d['kind']), d['site'], d['line'],
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='fw', description='数据飞轮平台命令行：数据集版本的导入、取回、对比和导出。')
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


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')  # a path the console cannot show must not stop the command
        except AttributeError:
            pass
    args = build_parser().parse_args(argv)
    api = None
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
    except KeyboardInterrupt:
        print('\n已中断', file=sys.stderr)
        return 130
    finally:
        if api is not None:
            api.close()


if __name__ == '__main__':
    sys.exit(main())
