"""数据智能问答 Agent。

产品定位（对应 PRD 的场景一）：用户用大白话问一个数，Agent 要给出
**数 + 口径 + 证据**，而不是只丢一个数字。口径选错比答不上来更危险，
所以「分不清就先问一句」是这个 Agent 的一等公民行为。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Union

from ..warehouse.seed import CATEGORIES
from . import nlu
from .loop import AgentContext, AgentResult, Final, Step, ToolCall, run
from .tools import ToolRegistry

# 涉及个体明细 / 敏感信息的问法 —— 平台侧一律不答，交由权限体系处理
REFUSAL_PATTERNS = (
    r"手机号",
    r"身份证",
    r"设备号",
    r"个人信息",
    r"用户明细",
    r"某个用户",
    r"某个UP主",
    r"导出.{0,6}明细",
    r"user_id",
    r"up_id",
)


@dataclass
class QAPlanner:
    """确定性规划器。

    之所以写成「规划器 + 通用循环」而不是一段流水脚本：
    换成 LLM 规划器时循环、预算、trace、评测都不用动，
    可以直接做「规则规划 vs 模型规划」的对照评测。
    """

    categories: tuple[str, ...] = CATEGORIES

    def next_step(self, ctx: AgentContext, steps: tuple[Step, ...]) -> Union[ToolCall, Final]:
        n = len(steps)

        if n == 0:
            for pat in REFUSAL_PATTERNS:
                if re.search(pat, ctx.question, re.IGNORECASE):
                    return Final(
                        kind="refuse",
                        text=(
                            "这个问题涉及个体明细或敏感字段，数据平台侧不直接提供，"
                            "请走数据权限申请流程后再查询。"
                        ),
                        data={"reason": "sensitive_or_individual"},
                    )
            return ToolCall("search_metric", {"query": ctx.question, "top_k": 3})

        if n == 1:
            payload = steps[0].result.payload
            if not steps[0].result.ok:
                return Final(kind="error", text=f"指标检索失败：{steps[0].result.error}")
            if not payload.get("candidates"):
                return Final(
                    kind="refuse",
                    text=(
                        f"「{ctx.question}」没有匹配到指标平台里已注册的指标。"
                        "可以先在指标平台确认口径，或补充业务域关键词再问一次。"
                    ),
                    data={"reason": "metric_not_found"},
                )
            if payload.get("need_clarification"):
                options = payload.get("options", [])
                return Final(
                    kind="clarify",
                    text=(
                        f"需要先确认口径：{payload.get('reason', '')}"
                        f"可选：{' / '.join(options)}"
                    ),
                    data={"options": options, "reason": payload.get("reason", "")},
                )
            if not payload.get("chosen"):
                return Final(
                    kind="refuse",
                    text=(
                        f"「{ctx.question}」匹配到的指标置信度不足（最高分低于阈值），"
                        "为避免给错口径，这里不猜。"
                    ),
                    data={"reason": "low_confidence"},
                )
            return ToolCall("get_metric", {"metric_id": payload["chosen"]})

        if n == 2:
            metric = steps[1].result.payload
            if not steps[1].result.ok:
                return Final(kind="error", text=f"读取指标口径失败：{steps[1].result.error}")
            dims = [d for d in nlu.parse_dimensions(ctx.question) if d in metric["dimensions"]]
            filters = {k: v for k, v in nlu.parse_filters(ctx.question, self.categories)}
            tr = nlu.parse_time(ctx.question, ctx.today)
            return ToolCall(
                "query_metric",
                {
                    "metric_id": metric["metric_id"],
                    "start": tr.start,
                    "end": tr.end,
                    "dimensions": dims,
                    "filters": filters,
                },
            )

        metric = steps[1].result.payload
        qr = steps[2].result
        if not qr.ok:
            return Final(
                kind="error",
                text=f"取数失败：{qr.error}（查询计划：{qr.payload.get('plan', '未知')}）",
                data={"plan": qr.payload.get("plan", "")},
            )
        payload = qr.payload
        tr = nlu.parse_time(ctx.question, ctx.today)
        filters = dict(nlu.parse_filters(ctx.question, self.categories))

        dim_text = ""
        if payload.get("breakdown"):
            preview = payload["breakdown"][:3]
            dim_text = "；分维度：" + "，".join(
                "、".join(f"{k}={v}" for k, v in row.items() if k != "value") + f" → {row['value']:,.4g}"
                for row in preview
            )
        filter_text = f"（{'、'.join(f'{k}={v}' for k, v in filters.items())}）" if filters else ""

        range_text = tr.label if tr.label != f"{tr.start} 至 {tr.end}" else f"{tr.start} 至 {tr.end}"
        text = (
            f"{metric['name']}在{range_text}{filter_text}为 {payload['text']}。"
            f"口径：{metric['definition']}（{metric['formula']}，来源表 {metric['table']}，"
            f"口径负责人 {metric['owner']}）。{dim_text}"
        )
        evidence = tuple(
            c["id"] for c in metric.get("chunks", []) if c["kind"] in ("definition", "formula")
        )
        return Final(
            kind="answer",
            text=text,
            data={
                "metric_id": metric["metric_id"],
                "metric_name": metric["name"],
                "value": payload.get("value"),
                "value_text": payload.get("text"),
                "start": tr.start,
                "end": tr.end,
                "label": tr.label,
                "dimensions": payload.get("breakdown") and list(payload["breakdown"][0].keys()),
                "filters": filters,
                "sql": payload.get("sql", ""),
                "plan": payload.get("plan", ""),
            },
            evidence=evidence,
        )


def ask(question: str, registry: ToolRegistry, today: str,
        max_steps: int = 6) -> AgentResult:
    return run(QAPlanner(), registry, AgentContext(question=question, today=today), max_steps)
