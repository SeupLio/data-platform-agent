"""期望值 Oracle。

**刻意与被测代码不同源**：这里自己解析口径文本、自己拼 SQL、自己算比率。
如果 Oracle 直接复用 `MetricExpr`，那么「口径解析写错了」这件事会同时污染
期望值和实际值，评测就会**恒绿而什么都没测到**。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

from ..knowledge.index import KnowledgeIndex
from ..knowledge.schema import Metric
from ..warehouse.engine import Warehouse

_SUM_RE = re.compile(r"sum\(\s*([^()]*?)\s*\)", re.IGNORECASE)
_DISTINCT_RE = re.compile(r"count\(\s*distinct\s+([A-Za-z_][A-Za-z0-9_]*)\s*\)", re.IGNORECASE)
_COL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class OracleError(ValueError):
    pass


def parse_oracle_expr(formula: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """返回 (kind, numerator, denominator)，kind ∈ additive | ratio | distinct。"""
    raw = (formula or "").strip()
    m = _DISTINCT_RE.search(raw)
    if m:
        return "distinct", (m.group(1),), ()
    if "/" in raw:
        head, _, tail = raw.partition("/")
        num = _cols_of(head)
        den = _cols_of(tail)
        return "ratio", num, den
    return "additive", _cols_of(raw), ()


def _cols_of(chunk: str) -> tuple[str, ...]:
    found: list[str] = []
    for inner in _SUM_RE.findall(chunk):
        for c in inner.split("+"):
            c = c.strip()
            if not c:
                continue
            if not _COL_RE.match(c):
                raise OracleError(f"Oracle 无法解析列名: {c!r}")
            found.append(c)
    if not found:
        raise OracleError(f"Oracle 无法解析 sum(): {chunk!r}")
    return tuple(dict.fromkeys(found))


def expected_value(
    wh: Warehouse,
    metric: Metric,
    start: str,
    end: str,
    filters: Optional[dict[str, str]] = None,
) -> Optional[float]:
    """直接对 sqlite 下 SQL，得到期望数值。"""
    kind, num, den = parse_oracle_expr(metric.formula)
    conn = sqlite3.connect(str(wh.db_path))
    conn.row_factory = sqlite3.Row
    try:
        where = ["date BETWEEN ? AND ?"]
        params: list[str] = [start, end]
        for k, v in (filters or {}).items():
            if not _COL_RE.match(k):
                raise OracleError(f"Oracle 拒绝非法筛选列: {k!r}")
            where.append(f"{k} = ?")
            params.append(v)
        clause = " AND ".join(where)

        def _sum(cols: tuple[str, ...]) -> float:
            total = 0.0
            for c in cols:
                row = conn.execute(
                    f"SELECT SUM({c}) AS s FROM {metric.table} WHERE {clause}", params
                ).fetchone()
                total += float(row["s"] or 0) if row else 0.0
            return total

        if kind == "distinct":
            row = conn.execute(
                f"SELECT COUNT(DISTINCT {num[0]}) AS c FROM {metric.table} WHERE {clause}", params
            ).fetchone()
            return float(row["c"] or 0)
        if kind == "ratio":
            d = _sum(den)
            return (_sum(num) / d) if d else None
        return _sum(num)
    finally:
        conn.close()


def expected_value_for(index: KnowledgeIndex, wh: Warehouse, metric_id: str, start: str,
                       end: str, filters: Optional[dict[str, str]] = None) -> Optional[float]:
    return expected_value(wh, index.kb.metric(metric_id), start, end, filters)


# ── 手写 SQL 的锚点用例：证明 Oracle 本身没算错 ──────────────────────────
ANCHOR_SQL = {
    "play_cnt": (
        "SELECT SUM(play_cnt) AS v FROM dws_video_daily "
        "WHERE date BETWEEN '2026-09-18' AND '2026-09-24'"
    ),
    "finish_rate": (
        "SELECT CAST(SUM(finish_cnt) AS REAL) / SUM(play_cnt) AS v FROM dws_video_daily "
        "WHERE date BETWEEN '2026-09-18' AND '2026-09-24'"
    ),
    "dau": (
        "SELECT SUM(dau) AS v FROM dws_user_daily WHERE date BETWEEN '2026-09-14' AND '2026-09-20'"
    ),
    "active_up_cnt": (
        "SELECT COUNT(DISTINCT up_id) AS v FROM dws_up_daily WHERE date = '2026-09-24'"
    ),
}

ANCHOR_RANGES = {
    "play_cnt": ("2026-09-18", "2026-09-24"),
    "finish_rate": ("2026-09-18", "2026-09-24"),
    "dau": ("2026-09-14", "2026-09-20"),
    "active_up_cnt": ("2026-09-24", "2026-09-24"),
}


def anchor_value(wh: Warehouse, metric_id: str) -> Optional[float]:
    sql = ANCHOR_SQL[metric_id]
    conn = sqlite3.connect(str(wh.db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql).fetchone()
        return float(row["v"]) if row and row["v"] is not None else None
    finally:
        conn.close()
