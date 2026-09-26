"""五维评分：问答准确率 / 数据推理有效性 / 场景适配度 / 响应效率 / 用户体验。

每一维都回答一个**不同的问题**，任何一维恒为 1 都说明它没在测量东西
（这和「一条永远绿的断言」是同一类病）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..agents.loop import AgentResult
from ..knowledge.index import KnowledgeIndex
from .cases import EvalCase

DIMENSIONS: tuple[str, ...] = ("accuracy", "reasoning", "adaptation", "efficiency", "experience")

DIMENSION_LABELS = {
    "accuracy": "问答准确率",
    "reasoning": "数据推理有效性",
    "adaptation": "场景适配度",
    "efficiency": "响应效率",
    "experience": "用户体验",
}

LATENCY_BUDGET_MS = 2000.0
VALUE_REL_TOL = 1e-6

WEIGHTS = {"accuracy": 0.35, "reasoning": 0.25, "adaptation": 0.20, "efficiency": 0.10, "experience": 0.10}


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    kind: str
    scenario: str
    scores: dict[str, float]
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def overall(self) -> float:
        return round(sum(WEIGHTS[d] * self.scores[d] for d in DIMENSIONS), 4)

    def failed(self, threshold: float = 0.999) -> bool:
        return self.overall < threshold


def _value_ok(actual: Optional[float], expected: Optional[float], tol: float = VALUE_REL_TOL) -> bool:
    if actual is None or expected is None:
        return False
    if expected == 0:
        return abs(actual) < 1e-9
    return abs(actual - expected) / abs(expected) <= tol


def _efficiency(result: AgentResult) -> float:
    ms = result.trace.elapsed_ms if result.trace else 0.0
    steps = result.trace.steps_used if result.trace else 0
    budget = result.trace.max_steps if result.trace else 6
    lat = 1.0 if ms <= LATENCY_BUDGET_MS else max(0.0, 1.0 - (ms - LATENCY_BUDGET_MS) / LATENCY_BUDGET_MS)
    step = 1.0 if steps <= budget else max(0.0, 1.0 - (steps - budget) / max(budget, 1))
    return round(0.6 * lat + 0.4 * step, 4)


def _sql_ok(result: AgentResult, index: KnowledgeIndex) -> bool:
    sql = (result.data or {}).get("sql", "")
    mid = (result.data or {}).get("metric_id")
    if not sql or not mid:
        return False
    try:
        metric = index.kb.metric(mid)
    except KeyError:
        return False
    if metric.table not in sql:
        return False
    return all(c in sql for c in metric.expr.columns)


def _f1(pred: set[str], exp: set[str]) -> float:
    if not pred and not exp:
        return 1.0
    if not pred or not exp:
        return 0.0
    tp = len(pred & exp)
    precision = tp / len(pred)
    recall = tp / len(exp)
    return 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0


def score_case(case: EvalCase, result: AgentResult, index: KnowledgeIndex,
               oracle_value: Optional[float] = None) -> CaseScore:
    exp = case.expect
    expected_kind = exp.get("kind", "answer")
    got = result.kind
    data = result.data or {}
    detail: dict[str, Any] = {"expected_kind": expected_kind, "got_kind": got}

    adaptation = 1.0 if got == expected_kind else 0.0

    if case.kind in ("govern_sql", "govern_table"):
        exp_rules = set(exp.get("rule_ids", ()))
        pred_rules = set(data.get("rule_ids", ()))
        accuracy = _f1(pred_rules, exp_rules)
        reasoning = (
            1.0
            if not exp_rules and not pred_rules
            else (len(exp_rules & pred_rules) / len(exp_rules) if exp_rules else 0.0)
        )
        if data.get("findings"):
            experience = 1.0 if "建议" in (result.text or "") else 0.5
        else:
            experience = 1.0 if ("未命中" in (result.text or "") or "未发现" in (result.text or "")) else 0.5
        return CaseScore(
            case.id,
            case.kind,
            case.scenario,
            {
                "accuracy": round(accuracy, 4),
                "reasoning": round(reasoning, 4),
                "adaptation": round(adaptation, 4),
                "efficiency": _efficiency(result),
                "experience": round(experience, 4),
            },
            {**detail, "expected_rules": sorted(exp_rules), "predicted_rules": sorted(pred_rules)},
        )

    if expected_kind in ("clarify", "refuse"):
        hit = 1.0 if got == expected_kind else 0.0
        if expected_kind == "clarify":
            options = data.get("options", [])
            reasoning = 1.0 if len(options) >= 2 else (0.5 if options else 0.0)
            experience = 1.0 if len(options) >= 2 and len(result.text or "") > 10 else 0.4
        else:
            reasoning = hit
            experience = 1.0 if len(result.text or "") > 10 else 0.4
        return CaseScore(
            case.id,
            case.kind,
            case.scenario,
            {
                "accuracy": hit,
                "reasoning": round(reasoning, 4),
                "adaptation": hit,
                "efficiency": _efficiency(result),
                "experience": round(experience, 4),
            },
            detail,
        )

    # ── answer 类（qa / insight）─────────────────────────────────────────
    metric_ok = 1.0 if data.get("metric_id") == exp.get("metric_id") else 0.0
    value_ok = 1.0 if _value_ok(data.get("value"), oracle_value) else 0.0
    accuracy = 0.5 * metric_ok + 0.5 * value_ok

    if case.kind == "insight":
        attrs = data.get("attributions") or []
        expected_top = exp.get("expect_top")
        expected_dim = exp.get("expect_top_dim")
        if expected_top:
            hit_attr = False
            for a in attrs:
                if a.get("dim") == expected_dim and a.get("items"):
                    if a["items"][0].get("dim_value") == expected_top:
                        hit_attr = True
            reasoning = 1.0 if hit_attr else 0.0
        else:
            reasoning = 1.0 if attrs else 0.0
    else:
        reasoning = 1.0 if _sql_ok(result, index) else 0.0

    # 场景适配度：不仅要看「有没有答」，还要看维度/筛选有没有按问法来
    struct_ok = True
    if "filters" in exp:
        struct_ok = struct_ok and dict(data.get("filters") or {}) == dict(exp["filters"])
    if exp.get("dimensions"):
        got_dims = data.get("dimensions") or []
        struct_ok = struct_ok and set(exp["dimensions"]).issubset(set(got_dims))
    adaptation = adaptation * (1.0 if struct_ok else 0.6)

    text = result.text or ""
    if case.kind == "insight":
        experience = 1.0 if (result.evidence and "贡献" in text and "口径" in text) else 0.5
    else:
        experience = 1.0 if (result.evidence and "口径" in text and data.get("value_text")) else 0.5

    return CaseScore(
        case.id,
        case.kind,
        case.scenario,
        {
            "accuracy": round(accuracy, 4),
            "reasoning": round(reasoning, 4),
            "adaptation": round(adaptation, 4),
            "efficiency": _efficiency(result),
            "experience": round(experience, 4),
        },
        {
            **detail,
            "metric_ok": metric_ok,
            "value_ok": value_ok,
            "oracle_value": oracle_value,
            "actual_value": data.get("value"),
        },
    )
