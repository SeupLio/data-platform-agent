"""知识库的结构化数据模型。

知识库里的东西必须**有形状**才能被 Agent 消费、被测试断言。
这一层只定义形状，不做解析；解析在 `build.py`，装载与检索在 `index.py`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence


@dataclass(frozen=True)
class MetricExpr:
    """指标计算式的结构化表示。

    支持三种形态（覆盖当前知识库全部 30 个指标）：
      · 可加型  sum(a)
      · 比率型  sum(a) / sum(b)
      · 去重型  count(distinct x)
    """

    numerator: tuple[str, ...] = ()
    denominator: tuple[str, ...] = ()
    distinct: Optional[str] = None
    raw: str = ""

    @property
    def kind(self) -> str:
        if self.distinct:
            return "distinct"
        if self.denominator:
            return "ratio"
        return "additive"

    @property
    def columns(self) -> tuple[str, ...]:
        cols = list(self.numerator) + list(self.denominator)
        if self.distinct:
            cols.append(self.distinct)
        return tuple(dict.fromkeys(cols))


_SUM_RE = re.compile(r"sum\(\s*([^()]*?)\s*\)", re.IGNORECASE)
_DISTINCT_RE = re.compile(r"count\(\s*distinct\s+([A-Za-z_][A-Za-z0-9_]*)\s*\)", re.IGNORECASE)


def parse_formula(formula: str) -> MetricExpr:
    """把「计算方式」文本解析成结构化表达式。

    解析失败时抛异常而不是静默兜底 —— 一个解析不出来的口径，
    比一个错误的口径更该被发现（见 README「诚实的局限」）。
    """
    raw = (formula or "").strip()
    if not raw:
        raise ValueError("空的指标计算式")

    distinct = _DISTINCT_RE.search(raw)
    if distinct:
        return MetricExpr(distinct=distinct.group(1), raw=raw)

    parts = raw.split("/", 1)
    sums = _SUM_RE.findall(parts[0])
    if not sums:
        raise ValueError(f"无法解析指标计算式的分子: {raw!r}")

    def _cols(chunk: str) -> tuple[str, ...]:
        inner = chunk.strip()
        cols = [c.strip() for c in inner.split("+") if c.strip()]
        if not cols:
            raise ValueError(f"无法解析 sum 内容: {chunk!r}")
        for c in cols:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c):
                raise ValueError(f"非法列名: {c!r}")
        return tuple(cols)

    numerator: tuple[str, ...] = ()
    for s in sums:
        numerator = numerator + _cols(s)

    denominator: tuple[str, ...] = ()
    if len(parts) == 2:
        dsums = _SUM_RE.findall(parts[1])
        if not dsums:
            raise ValueError(f"无法解析指标计算式的分母: {raw!r}")
        for s in dsums:
            denominator = denominator + _cols(s)

    return MetricExpr(numerator=numerator, denominator=denominator, raw=raw)


@dataclass(frozen=True)
class Metric:
    id: str
    name: str
    domain: str
    definition: str
    formula: str
    table: str
    dimensions: tuple[str, ...]
    unit: str
    value_type: str  # int | ratio | money
    level: str  # 原子指标 | 派生指标
    owner: str
    aliases: tuple[str, ...]
    confusable_with: tuple[str, ...]
    caveats: str

    @property
    def expr(self) -> MetricExpr:
        return parse_formula(self.formula)

    def is_confusable_with(self, other_id: str) -> bool:
        return other_id in self.confusable_with


@dataclass(frozen=True)
class DataModel:
    id: str  # 表名
    layer: str  # ODS/DWD/DWS/ADS/DIM
    domain: str
    grain: str
    partition_key: str
    columns: tuple[str, ...]
    upstream: tuple[str, ...]
    downstream: tuple[str, ...]
    owner: str
    notes: str


@dataclass(frozen=True)
class PolicyRule:
    id: str
    name: str
    severity: str  # P0/P1/P2
    target: str  # sql | table | metric
    logic: str
    fix: str
    owner: str


@dataclass(frozen=True)
class Chunk:
    """知识拆解后的原子知识点。

    一个指标被拆成若干可独立检索、可独立引用的 chunk，
    回答里引用的是 chunk 而不是整篇文档 —— 这样「证据」才对得上。
    """

    id: str
    metric_id: str
    kind: str  # definition | formula | caveat | dimension
    text: str


@dataclass(frozen=True)
class KnowledgeBase:
    metrics: dict[str, Metric] = field(default_factory=dict)
    models: dict[str, DataModel] = field(default_factory=dict)
    policies: dict[str, PolicyRule] = field(default_factory=dict)
    chunks: tuple[Chunk, ...] = ()
    confusable_pairs: tuple[tuple[str, str], ...] = ()

    def metric(self, metric_id: str) -> Metric:
        if metric_id not in self.metrics:
            raise KeyError(f"未注册的指标: {metric_id}")
        return self.metrics[metric_id]

    def model(self, table: str) -> DataModel:
        if table not in self.models:
            raise KeyError(f"未注册的数据模型: {table}")
        return self.models[table]

    def chunks_of(self, metric_id: str) -> tuple[Chunk, ...]:
        return tuple(c for c in self.chunks if c.metric_id == metric_id)

    @property
    def metric_ids(self) -> list[str]:
        return sorted(self.metrics)

    def summary(self) -> dict[str, int]:
        return {
            "metrics": len(self.metrics),
            "models": len(self.models),
            "policies": len(self.policies),
            "chunks": len(self.chunks),
            "confusable_pairs": len(self.confusable_pairs),
        }


def as_tuple(value: Sequence[str] | str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)
