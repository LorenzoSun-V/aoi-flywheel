"""Version labels: v0.1.0 as another name for a version of a dataset or of a model (migration 0012).

A label is written once and names its version for good; an administrator can retire one that was put on the
wrong version, and its string stays taken. Several labels may name one version. The rows of both kinds
(dataset versions, model versions) look the same, so this is one piece of code over two tables; datasets.py
and models.py call it, check who and what, and record the events.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from fw.contract import label_key, next_labels

# kind -> (table, column of the owner, column of the version, table of the versions, its owner column)
_KINDS = {'dataset': ('version_labels', 'dataset_id', 'version_id', 'versions', 'dataset_id'),
          'model': ('model_version_labels', 'model_id', 'model_version_id', 'model_versions', 'model_id')}
NOTE_MAX = 500


def find(conn, kind: str, owner_id: int, label: str) -> Optional[dict]:
    """The row of a label, retired or not: {version_id, retired_at, retired_note}."""
    table, owner, version, _, _ = _KINDS[kind]
    return conn.execute(f'SELECT {version} AS version_id, retired_at, retired_note FROM {table} '
                        f'WHERE {owner} = %s AND label = %s', (owner_id, label)).fetchone()


def of(conn, kind: str, version_ids: List[int]) -> Dict[int, List[str]]:
    """The labels in use of each version, lowest first."""
    table, _, version, _, _ = _KINDS[kind]
    out: Dict[int, List[str]] = {i: [] for i in version_ids}
    if version_ids:
        for r in conn.execute(f'SELECT {version} AS version_id, label FROM {table} '
                              f'WHERE {version} = ANY(%s) AND retired_at IS NULL', (list(version_ids),)):
            out[r['version_id']].append(r['label'])
    for labels in out.values():
        labels.sort(key=label_key)
    return out


def listing(conn, kind: str, owner_id: int) -> dict:
    """Every label of a dataset or model, highest first, the retired ones too, and what the next could be."""
    table, owner, version, versions, _ = _KINDS[kind]
    rows = conn.execute(
        f'SELECT l.label, l.{version} AS version_id, v.number, l.note, u.name AS created_by, l.created_at, '
        f'       r.name AS retired_by, l.retired_at, l.retired_note '
        f'FROM {table} l JOIN {versions} v ON v.id = l.{version} JOIN users u ON u.id = l.created_by '
        f'LEFT JOIN users r ON r.id = l.retired_by WHERE l.{owner} = %s', (owner_id,)).fetchall()
    rows.sort(key=lambda r: label_key(r['label']), reverse=True)
    return {'labels': [{'label': r['label'], 'version_id': r['version_id'], 'number': r['number'], 'note': r['note'],
                        'created_by': r['created_by'], 'created_at': r['created_at'].isoformat(),
                        'retired': r['retired_at'] is not None, 'retired_by': r['retired_by'],
                        'retired_at': r['retired_at'].isoformat() if r['retired_at'] else None,
                        'retired_note': r['retired_note']} for r in rows],
            'suggest': next_labels(r['label'] for r in rows)}


def add(conn, kind: str, owner_id: int, version_id: int, label: str, note: str, user_id: int) -> bool:
    """False when the dataset or model already has that label (also one being committed meanwhile)."""
    table, owner, version, _, _ = _KINDS[kind]
    return conn.execute(f'INSERT INTO {table} ({owner}, {version}, label, note, created_by) '
                        f'VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id',
                        (owner_id, version_id, label, note, user_id)).fetchone() is not None


def retire(conn, kind: str, owner_id: int, label: str, note: str, user_id: int) -> bool:
    """False when it is not there or retired already."""
    table, owner, _, _, _ = _KINDS[kind]
    return conn.execute(f'UPDATE {table} SET retired_by = %s, retired_at = now(), retired_note = %s '
                        f'WHERE {owner} = %s AND label = %s AND retired_at IS NULL RETURNING id',
                        (user_id, note, owner_id, label)).fetchone() is not None
