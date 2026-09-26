"""自动化数据分析 Agent。

产品定位（PRD 场景二）：用户不再问「是多少」，而是问「为什么变了」。
Agent 要自动完成：定指标 → 取当前值 → 取等长基线段 → 异常检测 → 维度归因 → 说人话。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Union

from ..analysis import shift_range
from ..warehouse.seed import CATEGORIES
from . import nlu
from .loop import AgentContext, AgentResult, Final, Step, ToolCall, run
from .tools import ToolRegistry


@dataclass
class InsightPlanner:
    categories: tuple[str, ...] = CATEGORIES

    def next_step(self, ctx: AgentContext, steps: tuple[Step, ...]) -> Union[ToolCall, Final]:
        n = len(steps)
        question = ctx.question

        if n == 0:
            return ToolCall("search_metric", {"query": question, "top_k": 3})

        if n == 1:
            payload = steps[0].result.payload
            if not steps[0].result.ok:
                return Final(kind="error", text=f"指标检索失败：{steps[0].result.error}")
            if not payload.get("candidates"):
                return Final(
                    kind="refuse",
                    text=f"「{question}」没有匹配到已注册指标，无法自动分析。",
                    data={"reason": "metric_not_found"},
                )
            if payload.get("need_clarification"):
                return Final(
                    kind="clarify",
                    text=f"需要先确认口径：{payload.get('reason', '')}（可选：{' / '.join(payload.get('options', []))}）",
                    data={"options": payload.get("options", [])},
                )
            if not payload.get("chosen"):
                return Final(
                    kind="refuse",
                    text=f"「{question}」匹配到的指标置信度不足，无法自动分析。",
                    data={"reason": "low_confidence"},
                )
            return ToolCall("get_metric", {"metric_id": payload["chosen"]})

        if n == 2:
            metric = steps[1].result.payload
            tr = nlu.parse_time(question, ctx.today)
            filters = dict(nlu.parse_filters(question, self.categories))
            return ToolCall(
                "query_metric",
                {
                    "metric_id": metric["metric_id"],
                    "start": tr.start,
                    "end": tr.end,
                    "dimensions": [],
                    "filters": filters,
                },
            )

        metric = steps[1].result.payload
        tr = nlu.parse_time(question, ctx.today)
        filters = dict(nlu.parse_filters(question, self.categories))
        dims = [d for d in metric["dimensions"] if d != "date"]

        # 逐个维度做一次归因（每个维度一个 tool step）
        if n - 3 < len(dims):
            dim = dims[n - 3]
            return ToolCall(
                "attribute_metric",
                {
                    "metric_id": metric["metric_id"],
                    "start": tr.start,
                    "end": tr.end,
                    "dim": dim,
                    "filters": filters,
                },
            )

        current = steps[2].result.payload
        if not steps[2].result.ok:
            return Final(kind="error", text=f"取数失败：{steps[2].result.error}")

        attrs = [s.result.payload for s in steps[3:] if s.result.ok]
        span_days = (date.fromisoformat(tr.end) - date.fromisoformat(tr.start)).days + 1
        base_start, base_end = shift_range(tr.start, tr.end, span_days)

        lines: list[str] = []
        total_delta_pct = 0.0
        if attrs:
            # 用变化绝对值最大的那个维度作为主归因
            main = max(attrs, key=lambda a: max((abs(i["delta"]) for i in a["items"]), default=0.0))
            total_delta_pct = main["total_delta_pct"]
            flat = sorted(main["items"], key=lambda i: -abs(i["delta"]))
            top_txt = "，".join(
                f"{i['dim']}={i['dim_value']} 变化 {i['delta']:+,.0f}（贡献 {i['contribution'] * 100:+.1f}%）"
                for i in flat[:2]
            )
            range_text = tr.label if tr.label != f"{tr.start} 至 {tr.end}" else f"{tr.start} 至 {tr.end}"
            lines.append(
                f"{metric['name']}在{range_text}为 {current['text']}，"
                f"较等长基线段（{base_start} 至 {base_end}）{total_delta_pct * 100:+.1f}%。"
            )
            if abs(total_delta_pct) < 0.01:
                lines.append("整体波动在 1% 以内，但维度内部仍有分化：")
            lines.append(f"变化主要来自 {main['dim']} 维度：{top_txt}。")
            others = [a for a in attrs if a is not main]
            for a in others:
                t = a["items"][0] if a["items"] else None
                if t:
                    lines.append(
                        f"按{a['dim']}拆：{a['dim']}={t['dim_value']} 变化 {t['delta']:+,.0f}"
                        f"（贡献 {t['contribution'] * 100:+.1f}%）。"
                    )
        else:
            lines.append(f"{metric['name']}在{tr.label}为 {current['text']}，但无法完成维度归因。")

        lines.append(
            f"口径：{metric['formula']}（来源表 {metric['table']}，口径负责人 {metric['owner']}）。"
        )
        evidence = tuple(
            c["id"] for c in metric.get("chunks", []) if c["kind"] in ("definition", "formula")
        )
        return Final(
            kind="answer",
            text="".join(lines),
            data={
                "metric_id": metric["metric_id"],
                "value": current.get("value"),
                "value_text": current.get("text"),
                "start": tr.start,
                "end": tr.end,
                "baseline_start": base_start,
                "baseline_end": base_end,
                "total_delta_pct": total_delta_pct,
                "attributions": attrs,
            },
            evidence=evidence,
        )


def analyze(question: str, registry: ToolRegistry, today: str, max_steps: int = 8) -> AgentResult:
    return run(InsightPlanner(), registry, AgentContext(question=question, today=today), max_steps)
