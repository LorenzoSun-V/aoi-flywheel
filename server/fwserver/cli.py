"""fw-server：平台服务的管理命令，在服务器上运行（docker compose exec server fw-server ...）。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, auth, changes, datasets, db, jobs, models
from .settings import Settings, SettingsError
from .store import Store, StoreError


def _print_rows(headers, rows):
    rows = [[('' if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in [headers] + rows) for i in range(len(headers))]
    for r in [headers] + rows:
        print('  '.join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def _stamp(t):
    return t.strftime('%Y-%m-%d %H:%M') if t else ''


def cmd_migrate(args, settings):
    done = db.migrate(settings.dsn)
    print('已应用：' + '、'.join(done) if done else '数据库已是最新')


def cmd_user(args, settings):
    with db.connect(settings.dsn) as conn:
        if args.action == 'add':
            u = auth.add_user(conn, args.name, args.role)
            print(f'已建账号 {u["name"]}（{auth.ROLES[u["role"]]}）；用 fw-server token create {u["name"]} 发凭据')
        elif args.action == 'list':
            _print_rows(['账号', '角色', '建立时间', '停用时间', '有效凭据'],
                        [[u['name'], auth.ROLES[u['role']], _stamp(u['created_at']), _stamp(u['disabled_at']),
                          u['active_tokens']] for u in auth.list_users(conn)])
        elif args.action == 'disable':
            n = auth.set_disabled(conn, args.name, True)
            print(f'已停用 {args.name}，吊销了 {n} 个凭据')
        elif args.action == 'enable':
            auth.set_disabled(conn, args.name, False)
            print(f'已启用 {args.name}；之前的凭据仍然无效，需要重新发')


def cmd_token(args, settings):
    with db.connect(settings.dsn) as conn:
        if args.action == 'create':
            token, token_id = auth.create_token(conn, args.name, args.label)
            print(f'凭据 {token_id}（{args.name}，{args.label or "无说明"}）：\n{token}\n'
                  f'只显示这一次，请直接交给使用的人或机器：fw login --server <地址> --token <凭据>')
        elif args.action == 'list':
            _print_rows(['编号', '账号', '说明', '建立时间', '最近使用', '吊销时间'],
                        [[t['id'], t['name'], t['label'], _stamp(t['created_at']), _stamp(t['last_used_at']),
                          _stamp(t['revoked_at'])] for t in auth.list_tokens(conn, args.name)])
        elif args.action == 'revoke':
            if auth.revoke_token(conn, args.id):
                print(f'已吊销凭据 {args.id}')
            else:
                print(f'凭据 {args.id} 不存在或已吊销')
                return 1


def verify_inputs(conn):
    """What the records above the datasets point at: model versions and evaluation reports, runner versions."""
    problems, wanted, count = models.verify_inputs(conn)
    for more in (jobs.verify_inputs, changes.verify_inputs):
        more_problems, more_wanted, more_count = more(conn)
        for digest, (size, ref) in more_wanted.items():
            if wanted.get(digest, (None,))[0] is None:
                wanted[digest] = (size, ref)
        problems, count = problems + more_problems, count + more_count
    return problems, wanted, count


def cmd_verify(args, settings):
    store = Store(settings.store)
    try:
        with db.connect(settings.dsn) as conn:
            problems = datasets.verify(conn, store, rehash=args.rehash,
                                       repair_from=Path(args.repair_from) if args.repair_from else None,
                                       more=verify_inputs)
        for p in problems[:50]:
            print(f'  {p}')
        if len(problems) > 50:
            print(f'  ……共 {len(problems)} 个')
        if problems:
            return 1
        blocked = store.problem()
        if args.rehash:
            store.mark_verified()  # durable first, then writes are allowed again in every server process
            if blocked is not None:
                print(f'全部核对通过，已解除：{blocked}')
        elif blocked is not None:
            print(f'{blocked}；要解除需要加 --rehash 全部重算一遍')
            return 1
    finally:
        store.close()


def cmd_serve(args, settings):
    import uvicorn
    uvicorn.run('fwserver.app:create_app', factory=True, host=args.host or settings.host,
                port=args.port or settings.port, workers=args.workers or settings.workers, access_log=False,
                log_level='info', timeout_keep_alive=30)


def build_parser():
    p = argparse.ArgumentParser(prog='fw-server', description='aoi-flywheel 平台服务的管理命令（读 FW_DSN、FW_STORE）')
    p.add_argument('--version', action='version', version=f'fw-server {__version__}')
    sub = p.add_subparsers(dest='command', required=True, metavar='命令')

    s = sub.add_parser('migrate', help='把数据库升级到这个版本的程序需要的结构')
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser('user', help='账号：add、list、disable、enable')
    u = s.add_subparsers(dest='action', required=True, metavar='操作')
    a = u.add_parser('add')
    a.add_argument('name')
    a.add_argument('--role', required=True, choices=list(auth.ROLES),
                   help='admin 管理员、algo 算法工程师、field 现场实施、deploy 部署')
    u.add_parser('list')
    for action in ('disable', 'enable'):
        a = u.add_parser(action, help='停用会立即吊销这个账号的全部凭据' if action == 'disable' else None)
        a.add_argument('name')
    s.set_defaults(func=cmd_user)

    s = sub.add_parser('token', help='凭据：create、list、revoke')
    t = s.add_subparsers(dest='action', required=True, metavar='操作')
    a = t.add_parser('create')
    a.add_argument('name', help='账号')
    a.add_argument('--label', default='', help='这个凭据给谁或哪台机器用，例如 训练机-01')
    a = t.add_parser('list')
    a.add_argument('name', nargs='?')
    a = t.add_parser('revoke')
    a.add_argument('id', type=int)
    s.set_defaults(func=cmd_token)

    s = sub.add_parser('verify', help='核对每个版本用到的文件都在、大小对；--rehash 时重算哈希')
    s.add_argument('--rehash', action='store_true', help='逐个重算哈希（慢，适合每周空闲时跑）')
    s.add_argument('--repair-from', help='另一个文件库的根目录（比如备份），从那里取回损坏或缺失的文件')
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser('serve', help='启动服务')
    s.add_argument('--host')
    s.add_argument('--port', type=int)
    s.add_argument('--workers', type=int)
    s.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        return args.func(args, settings) or 0
    except (SettingsError, auth.AuthError, datasets.DatasetError, db.SchemaError, StoreError) as e:
        print(f'错误：{e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
