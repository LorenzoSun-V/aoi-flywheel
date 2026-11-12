"""Who did what: one row per change, written in the transaction that makes the change (migration 0009).

Every module that changes something an account is answerable for calls record() inside its own transaction,
next to the change itself: if the change is rolled back so is the row, and a change cannot commit without it.
Rows are never changed or removed (a database trigger refuses). Reading: list_events().
"""
from __future__ import annotations

import json
from typing import Any, List, Optional

SERVER = 'fw-server'   # the actor of what is done with the fw-server command on the server itself
LIMIT = 500

# action -> what the web page and the docs call it. An action not listed here is refused, so the list stays
# the whole vocabulary.
ACTIONS = {
    'dataset.create': '新建数据集',
    'version.create': '建版本',
    'batch.upload': '上传批次版本',
    'batch.submit': '批次提交复核',
    'batch.reject': '退回批次',
    'batch.approve': '批次复核通过',
    'batch.import': '批次入库',
    'batch.close': '关闭批次',
    'model.create': '新建模型',
    'model_version.register': '登记模型版本',
    'model_version.tag': '改模型标签',
    'eval.register': '登记评估报告',
    'prediction_set.register': '登记预测集',
    'release.register': '登记发布',
    'runner.register': '登记推理环境',
    'job.submit': '提交任务',
    'job.cancel': '取消任务',
    'user.create': '新建账号',
    'user.role': '改账号角色',
    'user.disable': '停用账号',
    'user.enable': '启用账号',
    'token.create': '发凭据',
    'token.revoke': '吊销凭据',
}


def record(conn, actor_id: Optional[int], action: str, obj: str, **detail: Any) -> None:
    """Call inside the transaction of the change. actor_id None: done with fw-server on the server."""
    if action not in ACTIONS:
        raise ValueError(f'unknown event action {action}')
    conn.execute(
        'INSERT INTO events (actor_id, actor, action, object, detail) '
        'VALUES (%s, coalesce((SELECT name FROM users WHERE id = %s), %s), %s, %s, %s)',
        (actor_id, actor_id, SERVER, action, obj, json.dumps(detail, ensure_ascii=False, default=str)))


def list_events(conn, limit: int = 100, before: Optional[int] = None, actor: str = '', action: str = '',
                q: str = '') -> dict:
    """Newest first. before: an event id, for the next page. action: an action or its first part ('user');
    q: text in the object."""
    limit = max(1, min(int(limit), LIMIT))
    where, args = ['true'], []
    if before is not None:
        where.append('id < %s')
        args.append(before)
    if actor:
        where.append('lower(actor) = lower(%s)')
        args.append(actor)
    if action:
        where.append("(action = %s OR action LIKE %s || '.%%')")
        args += [action, action.replace('%', '').replace('_', r'\_')]
    if q:
        where.append("position(lower(%s) in lower(object)) > 0")
        args.append(q)
    rows = conn.execute('SELECT id, at, actor, actor_id, action, object, detail FROM events WHERE '
                        + ' AND '.join(where) + ' ORDER BY id DESC LIMIT %s', args + [limit + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {'events': [{'id': r['id'], 'at': r['at'].isoformat(), 'actor': r['actor'],
                        'by_server': r['actor_id'] is None, 'action': r['action'],
                        'action_text': ACTIONS.get(r['action'], r['action']), 'object': r['object'],
                        'detail': r['detail']} for r in rows],
            'next_before': rows[-1]['id'] if more and rows else None,
            'actions': ACTIONS}
