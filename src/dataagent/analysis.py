"""自动化数据分析的算子层：趋势、异常检测、维度归因。

归因用的是**贡献度分解**（delta 拆解到每个维度值上），不是「哪个维度值最大」。
这两者的区别是：最大的维度值可能根本没变化，而变化最大的那个才是原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Sequence

from .knowledge.index import KnowledgeIndex
from .warehouse.engine import QueryPlan, Warehouse


@dataclass(frozen=True)
class Point:
    key: str
    value: float


@dataclass(frozen=True)
class Contribution:
    dim: str
    dim_value: str
    current: float
    baseline: float
    delta: float
    contribution: float  # delta / 总 delta，可正可负

    def as_text(self) -> str:
        sign = "+" if self.delta >= 0 else ""
        return (
            f"{self.dim}={self.dim_value}：{sign}{self.delta:,.0f}"
            f"（贡献 {self.contribution * 100:+.1f}%）"
        )


@dataclass(frozen=True)
class Attribution:
    metric_id: str
    dim: str
    current_total: float
    baseline_total: float
    total_delta: float
    total_delta_pct: float
    items: tuple[Contribution, ...]

    @property
    def top(self) -> Optional[Contribution]:
        return self.items[0] if self.items else None


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def shift_range(start: str, end: str, days: int) -> tuple[str, str]:
    """把区间整体往前平移 N 天，得到等长基线段。"""
    s = date.fromisoformat(start) - timedelta(days=days)
    e = date.fromisoformat(end) - timedelta(days=days)
    return s.isoformat(), e.isoformat()


def daily_series(wh: Warehouse, metric_id: str, start: str, end: str,
                 filters: Sequence[tuple[str, str]] = ()) -> tuple[Point, ...]:
    """按天取一个指标的序列。"""
    index = wh.index
    if index is None:
        raise ValueError("缺少知识库索引")
    metric = index.kb.metric(metric_id)
    points: list[Point] = []
    cur = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while cur <= last:
        day = cur.isoformat()
        plan = QueryPlan(
            metric_id=metric_id,
            table=metric.table,
            dimensions=(),
            filters=tuple(filters),
            start=day,
            end=day,
        )
        res = wh.execute(plan, metric.unit, metric.value_type)
        points.append(Point(key=day, value=float(res.value or 0.0)))
        cur += timedelta(days=1)
    return tuple(points)


def detect_anomalies(series: Sequence[Point], z_threshold: float = 2.5) -> tuple[Point, ...]:
    """用稳健 z-score（中位数 + MAD）找异常点。

    不用均值/标准差 —— 均值会被异常本身拉走，导致「大异常把自己藏起来」。
    """
    values = [p.value for p in series]
    if len(values) < 5:
        return ()
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    mad = sorted(abs(v - median) for v in values)[mid]
    scale = mad * 1.4826 or 1.0
    out = []
    for p in series:
        z = (p.value - median) / scale
        if abs(z) >= z_threshold:
            out.append(p)
    return tuple(out)


def attribute(wh: Warehouse, metric_id: str, start: str, end: str,
              dim: str, filters: Sequence[tuple[str, str]] = ()) -> Attribution:
    """把「当前区间 vs 等长基线段」的总变化拆到维度的每个取值上。"""
    index = wh.index
    if index is None:
        raise ValueError("缺少知识库索引")
    metric = index.kb.metric(metric_id)
    if dim not in metric.dimensions:
        raise ValueError(f"指标 {metric_id} 不支持维度 {dim}")

    n = _days(start, end)
    base_start, base_end = shift_range(start, end, n)

    def _total(s: str, e: str) -> float:
        plan = QueryPlan(metric_id, metric.table, (), tuple(filters), s, e)
        res = wh.execute(plan, metric.unit, metric.value_type)
        return float(res.value or 0.0) if res.value is not None else 0.0

    def _by_dim(s: str, e: str) -> dict[str, float]:
        plan = QueryPlan(metric_id, metric.table, (dim,), tuple(filters), s, e)
        res = wh.execute(plan, metric.unit, metric.value_type)
        out: dict[str, float] = {}
        for row in res.breakdown:
            out[str(row[dim])] = float(row["value"])
        return out

    cur_total = _total(start, end)
    base_total = _total(base_start, base_end)
    cur_dim = _by_dim(start, end)
    base_dim = _by_dim(base_start, base_end)

    total_delta = cur_total - base_total
    items: list[Contribution] = []
    for key in sorted(set(cur_dim) | set(base_dim)):
        c = cur_dim.get(key, 0.0)
        b = base_dim.get(key, 0.0)
        delta = c - b
        contrib = (delta / total_delta) if total_delta else 0.0
        items.append(
            Contribution(dim=dim, dim_value=key, current=c, baseline=b, delta=delta, contribution=contrib)
        )
    items.sort(key=lambda x: -abs(x.delta))

    return Attribution(
        metric_id=metric_id,
        dim=dim,
        current_total=cur_total,
        baseline_total=base_total,
        total_delta=total_delta,
        total_delta_pct=(total_delta / base_total) if base_total else 0.0,
        items=tuple(items),
    )


def auto_attribute(wh: Warehouse, metric_id: str, start: str, end: str,
                   filters: Sequence[tuple[str, str]] = ()) -> tuple[Attribution, ...]:
    """对指标支持的每个维度各做一次归因，按「最大单项变化」排序。"""
    index = wh.index
    if index is None:
        raise ValueError("缺少知识库索引")
    metric = index.kb.metric(metric_id)
    outs = []
    for dim in metric.dimensions:
        if dim == "date":
            continue
        outs.append(attribute(wh, metric_id, start, end, dim, filters))
    outs.sort(key=lambda a: -abs(a.top.delta if a.top else 0.0))
    return tuple(outs)


def describe_metric(index: KnowledgeIndex, metric_id: str) -> str:
    m = index.kb.metric(metric_id)
    return f"{m.name}（{m.id}）：{m.definition} 计算方式 {m.formula}"
