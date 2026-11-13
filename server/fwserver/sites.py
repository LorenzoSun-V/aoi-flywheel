"""The register of sites and their production lines (migration 0013).

A code (SES, TX) is what pools, models and releases store, and it is fixed for good: it is in the folders
of training snapshots and in release numbers. The name shown next to it may change. Stored text is found
without regard to case and never rewritten. datasets.py checks who and what and records the events.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

CODE = re.compile(r'[A-Za-z0-9_.㐀-䶿一-鿿]{1,64}\Z')  # = models.SITE_LINE, and the database's CHECK
CODE_HELP = '只能用汉字、英文字母、数字、_ 和 .，最多 64 个字（发布编号是 <现场>-<产线>-r<序号>，所以不能有 -）'


def find(conn, site: str, line: Optional[str] = None) -> Optional[Tuple[str, Optional[str]]]:
    """The codes as registered (typed ses/tx -> SES/TX), or None when the site, or the line given, is not
    registered."""
    s = conn.execute('SELECT id, code FROM sites WHERE lower(code) = lower(%s)', (site,)).fetchone()
    if s is None:
        return None
    if line is None:
        return s['code'], None
    row = conn.execute('SELECT code FROM lines WHERE site_id = %s AND lower(code) = lower(%s)', (s['id'], line)).fetchone()
    return (s['code'], row['code']) if row is not None else None


def add_site(conn, code: str, name: str, user_id: Optional[int]) -> bool:
    """False when it is registered already (in any case of letters)."""
    return conn.execute('INSERT INTO sites (code, name, created_by) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING '
                        'RETURNING id', (code, name, user_id)).fetchone() is not None


def add_line(conn, site: str, code: str, name: str, user_id: Optional[int]) -> bool:
    """False when the site has that line already. The site must be registered."""
    return conn.execute('INSERT INTO lines (site_id, code, name, created_by) '
                        'SELECT id, %s, %s, %s FROM sites WHERE lower(code) = lower(%s) ON CONFLICT DO NOTHING '
                        'RETURNING id', (code, name, user_id, site)).fetchone() is not None


def rename(conn, site: str, line: Optional[str], name: str) -> bool:
    if line is None:
        return conn.execute('UPDATE sites SET name = %s WHERE lower(code) = lower(%s) RETURNING id',
                            (name, site)).fetchone() is not None
    return conn.execute('UPDATE lines l SET name = %s FROM sites s WHERE s.id = l.site_id AND lower(s.code) = lower(%s) '
                        'AND lower(l.code) = lower(%s) RETURNING l.id', (name, site, line)).fetchone() is not None


_COUNTS = """
SELECT lower(site) AS site, lower(line) AS line, what, count(*) AS n FROM (
  SELECT site, line, CASE WHEN kind = 'trainset' THEN 'trainsets' ELSE 'datasets' END AS what
    FROM datasets WHERE kind <> 'batch'
  UNION ALL SELECT site, line, 'models' FROM models
  UNION ALL SELECT p.site, p.line, 'batches' FROM batches b JOIN datasets p ON p.id = b.pool_id
    WHERE b.status NOT IN ('imported', 'closed')
  UNION ALL SELECT site, line, 'releases' FROM releases
) x GROUP BY 1, 2, 3
"""
WHAT = ('datasets', 'trainsets', 'models', 'batches', 'releases')


def tree(conn) -> dict:
    """Every registered site with its lines, by code, and how much each line holds: data pools and test sets
    (datasets), training snapshots of that line alone (trainsets), models, labelling batches still under way
    (batches), releases. 'other': what names a site or line that is not registered, or none."""
    counts: dict = {}
    for r in conn.execute(_COUNTS):
        counts.setdefault((r['site'], r['line']), {})[r['what']] = r['n']
    out: List[dict] = []
    lines = {}
    for s in conn.execute('SELECT id, code, name FROM sites ORDER BY lower(code)').fetchall():
        site = {'code': s['code'], 'name': s['name'], 'lines': []}
        lines[s['id']] = site
        out.append(site)
    for r in conn.execute('SELECT site_id, code, name FROM lines ORDER BY lower(code)').fetchall():
        site = lines[r['site_id']]
        n = counts.pop((site['code'].lower(), r['code'].lower()), {})
        site['lines'].append(dict({'code': r['code'], 'name': r['name']}, **{k: n.get(k, 0) for k in WHAT}))
    other = {k: sum(n.get(k, 0) for n in counts.values()) for k in WHAT}
    return {'sites': out, 'other': other}
