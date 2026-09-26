"""变异测试：注入缺陷，验证「评测真的看得见缺陷」。

一个只会给满分的评测体系**等于没有评测**。
这里的每个变异体都对应一类真实会发生的退化，跑完之后必须
在至少一个维度上掉分，否则说明那一维是摆设。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional, Sequence

from .harness import run_eval
from .metrics import DIMENSIONS, CaseScore


@dataclass
class Mutant:
    id: str
    description: str
    expected_dims: tuple[str, ...]
    patch: Callable[[], Any]


@contextmanager
def _no_clarify() -> Iterator[None]:
    from ..knowledge import index as idx_mod

    old = idx_mod.CLARIFY_RATIO
    idx_mod.CLARIFY_RATIO = 0.0  # 永不澄清 → 直接猜一个口径
    try:
        yield
    finally:
        idx_mod.CLARIFY_RATIO = old


@contextmanager
def _ignore_explicit_range() -> Iterator[None]:
    from ..agents import nlu

    def fake_parse_time(text: str, today):
        return nlu.TimeRange("2026-09-18", "2026-09-24", "最近 7 天")

    old = nlu.parse_time
    nlu.parse_time = fake_parse_time
    try:
        yield
    finally:
        nlu.parse_time = old


@contextmanager
def _disable_g01() -> Iterator[None]:
    from ..agents import governance as gov

    old = gov.check_sql

    def patched(sql, index):
        return tuple(f for f in old(sql, index) if f.rule_id != "G01")

    gov.check_sql = patched
    try:
        yield
    finally:
        gov.check_sql = old


@contextmanager
def _attribution_unsorted() -> Iterator[None]:
    from ..agents import tools as tools_mod
    from ..analysis import Attribution

    old = tools_mod.attribute

    def patched(wh, metric_id, start, end, dim, filters=()):
        a = old(wh, metric_id, start, end, dim, filters)
        return Attribution(
            metric_id=a.metric_id,
            dim=a.dim,
            current_total=a.current_total,
            baseline_total=a.baseline_total,
            total_delta=a.total_delta,
            total_delta_pct=a.total_delta_pct,
            items=tuple(sorted(a.items, key=lambda i: abs(i.delta))),  # 升序：把最不重要的排前面
        )

    tools_mod.attribute = patched
    try:
        yield
    finally:
        tools_mod.attribute = old


@contextmanager
def _answer_without_context() -> Iterator[None]:
    from ..agents import qa as qa_mod
    from ..agents.loop import Final

    old = qa_mod.QAPlanner.next_step

    def patched(self, ctx, steps):
        r = old(self, ctx, steps)
        if isinstance(r, Final) and r.kind == "answer":
            return Final(
                kind="answer",
                text=str((r.data or {}).get("value_text", "")),  # 只丢一个数字，没有口径没有证据
                data=r.data,
                evidence=(),
            )
        return r

    qa_mod.QAPlanner.next_step = patched
    try:
        yield
    finally:
        qa_mod.QAPlanner.next_step = old


@contextmanager
def _distinct_as_additive() -> Iterator[None]:
    from ..knowledge import schema as schema_mod

    old = schema_mod.parse_formula

    def patched(formula: str):
        expr = old(formula)
        if expr.distinct:
            # 去重指标被当成可加指标 → 跨维度直接相加，数值虚高
            return schema_mod.MetricExpr(
                numerator=(expr.distinct,), denominator=(), distinct=None, raw=formula
            )
        return expr

    schema_mod.parse_formula = patched
    try:
        yield
    finally:
        schema_mod.parse_formula = old


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        id="M1-no-clarify",
        description="去掉口径消歧：命中多个候选时直接选第一个，不再反问",
        expected_dims=("adaptation", "accuracy"),
        patch=_no_clarify,
    ),
    Mutant(
        id="M2-ignore-range",
        description="时间解析忽略显式区间，一律按最近 7 天取数",
        expected_dims=("accuracy",),
        patch=_ignore_explicit_range,
    ),
    Mutant(
        id="M3-disable-G01",
        description="治理检查漏掉 G01（分区过滤缺失）",
        expected_dims=("accuracy", "reasoning"),
        patch=_disable_g01,
    ),
    Mutant(
        id="M4-attribution-unsorted",
        description="归因结果按「变化最小」排序，主因被埋掉",
        expected_dims=("reasoning",),
        patch=_attribution_unsorted,
    ),
    Mutant(
        id="M5-no-context",
        description="回答只给数字，不带口径定义与证据",
        expected_dims=("experience",),
        patch=_answer_without_context,
    ),
    Mutant(
        id="M6-distinct-as-additive",
        description="去重指标（活跃UP主数）被当成可加指标求和",
        expected_dims=("accuracy",),
        patch=_distinct_as_additive,
    ),
)


@dataclass
class MutationResult:
    mutant_id: str
    description: str
    baseline: dict[str, float]
    mutated: dict[str, float]
    deltas: dict[str, float]
    caught: bool
    caught_dims: tuple[str, ...] = ()
    expected_dims: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = field(default_factory=tuple)

    def as_row(self) -> str:
        flag = "被抓" if self.caught else "幸存"
        return f"| {self.mutant_id} | {self.description} | {flag} | " + " | ".join(
            f"{self.deltas[d]:+.3f}" for d in DIMENSIONS
        ) + " |"


def run_mutations(
    baseline: dict[str, float],
    mutants: Sequence[Mutant] = MUTANTS,
    cases=None,
    index=None,
    wh=None,
) -> tuple[MutationResult, ...]:
    results: list[MutationResult] = []
    for m in mutants:
        with m.patch():
            _, summary = run_eval(index=index, wh=wh, cases=cases, progress=False)
        deltas = {d: round(summary.dimension_means[d] - baseline[d], 4) for d in DIMENSIONS}
        caught_dims = tuple(d for d in DIMENSIONS if deltas[d] < -1e-9)
        results.append(
            MutationResult(
                mutant_id=m.id,
                description=m.description,
                baseline=dict(baseline),
                mutated=dict(summary.dimension_means),
                deltas=deltas,
                caught=bool(caught_dims),
                caught_dims=caught_dims,
                expected_dims=m.expected_dims,
                uncovered=tuple(d for d in m.expected_dims if d not in caught_dims),
            )
        )
    return tuple(results)


def baseline_dims(scores: Sequence[CaseScore]) -> dict[str, float]:
    n = len(scores)
    return {d: round(sum(s.scores[d] for s in scores) / n, 4) for d in DIMENSIONS} if n else {}
