"""查询编译与执行。

安全边界（这也是治理规则 G01/G03 的执行侧）：
  · 只接受 `SELECT`；
  · 表名必须已注册在知识库；
  · 列名必须在该表的字段清单里，且匹配 `^[a-z_][a-z0-9_]*$`；
  · 所有筛选值走参数绑定，绝不拼字符串；
  · 没有日期区间就不允许查 dwd_/dws_ 明细表。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..knowledge.index import KnowledgeIndex
from ..knowledge.schema import MetricExpr
from .seed import DEFAULT_DB, generate

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_DETAIL_LAYERS = ("ODS", "DWD", "DWS")


class QueryError(ValueError):
    """查询不可执行。"""


@dataclass(frozen=True)
class QueryPlan:
    metric_id: str
    table: str
    dimensions: tuple[str, ...] = ()
    filters: tuple[tuple[str, str], ...] = ()
    start: str = ""
    end: str = ""
    limit: int = 5000

    def describe(self) -> str:
        parts = [f"指标={self.metric_id}", f"表={self.table}"]
        if self.dimensions:
            parts.append(f"维度={'、'.join(self.dimensions)}")
        if self.filters:
            parts.append("筛选=" + "、".join(f"{k}={v}" for k, v in self.filters))
        parts.append(f"区间={self.start}~{self.end}")
        return "；".join(parts)


@dataclass(frozen=True)
class MetricValue:
    value: Optional[float]
    unit: str
    value_type: str
    sql: str
    rowcount: int
    breakdown: tuple[dict[str, Any], ...] = ()
    note: str = ""

    def as_text(self) -> str:
        if self.value is None:
            return f"无法计算（{self.note or '口径不支持'}）"
        if self.value_type == "ratio" and self.unit == "百分比":
            return f"{self.value * 100:.2f}%"
        if self.value_type == "money":
            return f"{self.value:,.2f} 元"
        if self.unit == "秒":
            return f"{self.value:,.0f} 秒"
        return f"{self.value:,.0f} {self.unit}"


def _check_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise QueryError(f"非法标识符: {name!r}")
    return name


class Warehouse:
    def __init__(self, db_path: str | Path | None = None, index: KnowledgeIndex | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.index = index
        if not self.db_path.exists():
            generate(self.db_path)

    # ── 基础信息 ────────────────────────────────────────────────────────
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def date_range(self, table: str = "dws_video_daily") -> tuple[str, str]:
        _check_ident(table)
        with self.connect() as conn:
            row = conn.execute(f"SELECT MIN(date) AS a, MAX(date) AS b FROM {table}").fetchone()
        return (row["a"], row["b"])

    @property
    def today(self) -> str:
        """数据里的「今天」。

        刻意不取系统时间 —— 否则同一份代码在不同日期跑出不同数字，
        README 上的每个数字都不可复现。
        """
        return self.date_range()[1]

    # ── 编译 ────────────────────────────────────────────────────────────
    def compile_sql(self, plan: QueryPlan, expr: MetricExpr) -> tuple[str, tuple[Any, ...]]:
        if self.index is not None:
            model = self.index.kb.model(plan.table)
            allowed = set(model.columns)
            for col in (*expr.columns, *plan.dimensions, *(f[0] for f in plan.filters)):
                _check_ident(col)
                if col not in allowed:
                    raise QueryError(f"字段 {col} 不在表 {plan.table} 的字段清单里")
            for dim in plan.dimensions:
                if dim not in self.index.kb.metric(plan.metric_id).dimensions:
                    raise QueryError(
                        f"指标 {plan.metric_id} 不支持维度 {dim}"
                        f"（支持：{'、'.join(self.index.kb.metric(plan.metric_id).dimensions)}）"
                    )
            if model.layer in _DETAIL_LAYERS and not (plan.start and plan.end):
                raise QueryError(f"查询 {model.layer} 层表必须带日期区间（治理规则 G01）")
        else:
            for col in (*expr.columns, *plan.dimensions):
                _check_ident(col)

        select_cols = [_check_ident(d) for d in plan.dimensions]
        if expr.distinct:
            select_cols.append(f"COUNT(DISTINCT {_check_ident(expr.distinct)}) AS _value")
        else:
            for c in expr.columns:
                select_cols.append(f"SUM({_check_ident(c)}) AS _sum_{c}")

        where = ["1=1"]
        params: list[Any] = []
        if plan.start and plan.end:
            where.append("date BETWEEN ? AND ?")
            params.extend([plan.start, plan.end])
        for col, val in plan.filters:
            where.append(f"{_check_ident(col)} = ?")
            params.append(val)

        sql = f"SELECT {', '.join(select_cols)} FROM {_check_ident(plan.table)}"
        sql += f" WHERE {' AND '.join(where)}"
        if plan.dimensions:
            sql += " GROUP BY " + ", ".join(_check_ident(d) for d in plan.dimensions)
            sql += " ORDER BY " + ", ".join(_check_ident(d) for d in plan.dimensions)
        sql += f" LIMIT {int(plan.limit)}"
        if not sql.lstrip().upper().startswith("SELECT"):
            raise QueryError("只允许 SELECT 查询")
        return sql, tuple(params)

    # ── 执行 ────────────────────────────────────────────────────────────
    def execute(self, plan: QueryPlan, metric_unit: str, value_type: str) -> MetricValue:
        if self.index is None:
            raise QueryError("缺少知识库索引，无法校验查询计划")
        metric = self.index.kb.metric(plan.metric_id)
        expr = metric.expr
        sql, params = self.compile_sql(plan, expr)
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        breakdown: list[dict[str, Any]] = []
        note = ""
        if expr.distinct:
            for r in rows:
                key = {d: r[d] for d in plan.dimensions}
                breakdown.append({**key, "value": float(r["_value"])})
            if plan.dimensions:
                value: Optional[float] = None
                note = "去重指标跨维度不可加，只能逐维度查看"
            else:
                value = float(rows[0]["_value"]) if rows else 0.0
        else:
            num = 0.0
            den = 0.0
            for r in rows:
                n = sum(float(r[f"_sum_{c}"] or 0) for c in expr.numerator)
                d = sum(float(r[f"_sum_{c}"] or 0) for c in expr.denominator) if expr.denominator else 0.0
                if plan.dimensions:
                    key = {dname: r[dname] for dname in plan.dimensions}
                    v = (n / d) if expr.denominator and d else n
                    breakdown.append({**key, "value": v})
                num += n
                den += d
            if expr.denominator:
                value = (num / den) if den else None
                if value is None:
                    note = "分母为 0，比率不可计算"
            else:
                value = num

        return MetricValue(
            value=value,
            unit=metric_unit,
            value_type=value_type,
            sql=sql,
            rowcount=len(rows),
            breakdown=tuple(breakdown),
            note=note,
        )

    def profile(self, table: str) -> dict[str, Any]:
        """表健康度体检：行数、NULL 率、主键重复数 —— 治理助手的取数口。"""
        _check_ident(table)
        if self.index is not None:
            model = self.index.kb.model(table)
            cols = model.columns
        else:
            with self.connect() as conn:
                cols = tuple(r["name"] for r in conn.execute(f"PRAGMA table_info({table})"))
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            nulls: dict[str, int] = {}
            for c in cols:
                if c == "date":
                    continue
                nulls[c] = conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE {_check_ident(c)} IS NULL"
                ).fetchone()["c"]
            dup = 0
            key_cols = [c for c in ("date", "category", "platform") if c in cols]
            if key_cols:
                kl = ", ".join(_check_ident(c) for c in key_cols)
                dup = conn.execute(
                    f"SELECT COUNT(*) AS c FROM (SELECT {kl} FROM {table} "
                    f"GROUP BY {kl} HAVING COUNT(*) > 1)"
                ).fetchone()["c"]
        return {
            "table": table,
            "rows": total,
            "null_counts": nulls,
            "duplicate_key_groups": dup,
            "date_range": self.date_range(table),
        }
