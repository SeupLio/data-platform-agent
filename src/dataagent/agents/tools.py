"""Agent 的工具箱。

每个工具都返回 `ToolResult(ok, payload, summary)`：
`ok=False` 表示工具执行失败（会被 Agent 看见并写进 trace），
而不是静默返回空结果 —— 静默兜底是测量和评价里最贵的一类污染。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..analysis import Attribution, attribute, detect_anomalies, daily_series
from ..knowledge.index import KnowledgeIndex
from ..warehouse.engine import QueryPlan, Warehouse


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    error: str = ""

    @classmethod
    def fail(cls, error: str) -> "ToolResult":
        return cls(ok=False, error=error, summary=f"工具执行失败：{error}")


class ToolRegistry:
    def __init__(self, index: KnowledgeIndex, warehouse: Warehouse):
        self.index = index
        self.wh = warehouse
        self._calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def call_log(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        return tuple(self._calls)

    def names(self) -> tuple[str, ...]:
        return (
            "search_metric",
            "get_metric",
            "query_metric",
            "profile_table",
            "check_sql",
            "check_table",
            "attribute_metric",
        )

    def call(self, name: str, **args: Any) -> ToolResult:
        self._calls.append((name, dict(args)))
        fn = getattr(self, f"_tool_{name}", None)
        if fn is None:
            return ToolResult.fail(f"未注册的工具 {name}")
        try:
            return fn(**args)
        except Exception as exc:  # noqa: BLE001 —— 工具失败要被 Agent 看见，不能冒泡打断流程
            return ToolResult.fail(f"{type(exc).__name__}: {exc}")

    # ── 工具实现 ────────────────────────────────────────────────────────
    def _tool_search_metric(self, query: str, top_k: int = 3) -> ToolResult:
        res = self.index.resolve(query, top_k=top_k)
        return ToolResult(
            ok=True,
            payload={
                "need_clarification": res.need_clarification,
                "reason": res.reason,
                "options": list(res.clarify_options),
                "candidates": [
                    {
                        "metric_id": h.metric_id,
                        "name": h.metric.name,
                        "score": h.score,
                        "evidence": list(h.evidence),
                        "chunk_ids": list(h.chunk_ids),
                    }
                    for h in res.candidates
                ],
                "chosen": (res.chosen.metric_id if res.chosen else None),
            },
            summary=(
                f"命中 {len(res.candidates)} 个候选"
                + (f"，需要澄清：{res.reason}" if res.need_clarification else "")
            ),
        )

    def _tool_get_metric(self, metric_id: str) -> ToolResult:
        m = self.index.kb.metric(metric_id)
        chunks = [{"id": c.id, "kind": c.kind, "text": c.text} for c in self.index.kb.chunks_of(metric_id)]
        return ToolResult(
            ok=True,
            payload={
                "metric_id": m.id,
                "name": m.name,
                "definition": m.definition,
                "formula": m.formula,
                "table": m.table,
                "unit": m.unit,
                "value_type": m.value_type,
                "dimensions": list(m.dimensions),
                "owner": m.owner,
                "caveats": m.caveats,
                "confusable_with": list(self.index.confusable_with(metric_id)),
                "chunks": chunks,
            },
            summary=f"{m.name}（{m.id}）：{m.formula}，来源表 {m.table}",
        )

    def _tool_query_metric(
        self,
        metric_id: str,
        start: str = "",
        end: str = "",
        dimensions: Optional[list[str]] = None,
        filters: Optional[dict[str, str]] = None,
    ) -> ToolResult:
        m = self.index.kb.metric(metric_id)
        plan = QueryPlan(
            metric_id=metric_id,
            table=m.table,
            dimensions=tuple(dimensions or ()),
            filters=tuple((filters or {}).items()),
            start=start,
            end=end,
        )
        res = self.wh.execute(plan, m.unit, m.value_type)
        return ToolResult(
            ok=res.value is not None,
            payload={
                "metric_id": metric_id,
                "value": res.value,
                "text": res.as_text(),
                "unit": res.unit,
                "sql": res.sql,
                "rowcount": res.rowcount,
                "breakdown": [dict(b) for b in res.breakdown],
                "plan": plan.describe(),
            },
            summary=f"{m.name} = {res.as_text()}（{plan.describe()}）",
            error="" if res.value is not None else res.note,
        )

    def _tool_profile_table(self, table: str) -> ToolResult:
        prof = self.wh.profile(table)
        return ToolResult(
            ok=True,
            payload=prof,
            summary=f"{table}：{prof['rows']} 行，重复主键组 {prof['duplicate_key_groups']}",
        )

    def _tool_check_sql(self, sql: str) -> ToolResult:
        from .governance import check_sql

        findings = check_sql(sql, self.index)
        return ToolResult(
            ok=True,
            payload={
                "findings": [
                    {
                        "rule_id": f.rule_id,
                        "rule_name": f.rule_name,
                        "severity": f.severity,
                        "evidence": f.evidence,
                        "fix": f.fix,
                        "owner": f.owner,
                    }
                    for f in findings
                ],
                "count": len(findings),
            },
            summary=f"命中 {len(findings)} 条治理规则",
        )

    def _tool_check_table(self, table: str) -> ToolResult:
        from .governance import check_table

        findings = check_table(table, self.index)
        return ToolResult(
            ok=True,
            payload={
                "findings": [
                    {
                        "rule_id": f.rule_id,
                        "rule_name": f.rule_name,
                        "severity": f.severity,
                        "evidence": f.evidence,
                        "fix": f.fix,
                        "owner": f.owner,
                    }
                    for f in findings
                ],
                "count": len(findings),
            },
            summary=f"命中 {len(findings)} 条治理规则",
        )

    def _tool_attribute_metric(
        self,
        metric_id: str,
        start: str,
        end: str,
        dim: str,
        filters: Optional[dict[str, str]] = None,
    ) -> ToolResult:
        a: Attribution = attribute(self.wh, metric_id, start, end, dim, tuple((filters or {}).items()))
        return ToolResult(
            ok=True,
            payload={
                "metric_id": metric_id,
                "dim": dim,
                "current_total": a.current_total,
                "baseline_total": a.baseline_total,
                "total_delta": a.total_delta,
                "total_delta_pct": a.total_delta_pct,
                "items": [
                    {
                        "dim": i.dim,
                        "dim_value": i.dim_value,
                        "current": i.current,
                        "baseline": i.baseline,
                        "delta": i.delta,
                        "contribution": i.contribution,
                    }
                    for i in a.items
                ],
                "top": (a.top.dim_value if a.top else None),
            },
            summary=(
                f"{metric_id} 总变化 {a.total_delta:,.0f}（{a.total_delta_pct * 100:+.1f}%），"
                f"{dim} 维度最大单点：{a.top.as_text() if a.top else '无'}"
            ),
        )

    def _tool_detect_anomaly(
        self, metric_id: str, start: str, end: str, z_threshold: float = 2.5
    ) -> ToolResult:
        pts = detect_anomalies(daily_series(self.wh, metric_id, start, end), z_threshold)
        return ToolResult(
            ok=True,
            payload={"points": [{"key": p.key, "value": p.value} for p in pts]},
            summary=f"检测到 {len(pts)} 个异常日",
        )
