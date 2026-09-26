"""Agent 主循环：规划 → 调工具 → 观察 → 再规划，直到给出最终答复。

刻意保留的两个约束：
  · **步数预算**。跑满预算还没结论就停下来说明卡在哪，而不是无限重试；
  · **trace 全留痕**。每一步调了什么、看到什么，都要能被评测和复盘读到。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Union

from .tools import ToolRegistry, ToolResult


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    index: int
    call: ToolCall
    result: ToolResult

    @property
    def ok(self) -> bool:
        return self.result.ok


@dataclass(frozen=True)
class AgentContext:
    question: str
    today: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Final:
    kind: str  # answer | clarify | refuse | error
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentTrace:
    steps: tuple[Step, ...]
    final: Final
    max_steps: int
    elapsed_ms: float

    @property
    def steps_used(self) -> int:
        return len(self.steps)

    @property
    def hit_budget(self) -> bool:
        return len(self.steps) >= self.max_steps

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(s.call.name for s in self.steps)

    @property
    def failed_steps(self) -> tuple[Step, ...]:
        return tuple(s for s in self.steps if not s.ok)


@dataclass(frozen=True)
class AgentResult:
    kind: str
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    trace: Optional[AgentTrace] = None
    question: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "kind": self.kind,
            "text": self.text,
            "data": self.data,
            "evidence": list(self.evidence),
            "steps_used": self.trace.steps_used if self.trace else 0,
            "tools": list(self.trace.tool_names) if self.trace else [],
            "elapsed_ms": round(self.trace.elapsed_ms, 2) if self.trace else 0.0,
        }


class Planner(Protocol):
    def next_step(self, ctx: AgentContext, steps: tuple[Step, ...]) -> Union[ToolCall, Final]: ...


DEFAULT_MAX_STEPS = 6


def run_agent(
    planner: Planner,
    registry: ToolRegistry,
    ctx: AgentContext,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> AgentTrace:
    started = time.perf_counter()
    steps: list[Step] = []
    final: Optional[Final] = None

    for i in range(max_steps):
        action = planner.next_step(ctx, tuple(steps))
        if isinstance(action, Final):
            final = action
            break
        result = registry.call(action.name, **action.args)
        steps.append(Step(index=i, call=action, result=result))

    if final is None:
        final = Final(
            kind="error",
            text=f"Agent 在 {max_steps} 步预算内没有收敛，最后一步："
            + (steps[-1].result.summary if steps else "未调用任何工具"),
        )

    return AgentTrace(
        steps=tuple(steps),
        final=final,
        max_steps=max_steps,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def run(planner: Planner, registry: ToolRegistry, ctx: AgentContext,
        max_steps: int = DEFAULT_MAX_STEPS) -> AgentResult:
    trace = run_agent(planner, registry, ctx, max_steps)
    return AgentResult(
        kind=trace.final.kind,
        text=trace.final.text,
        data=trace.final.data,
        evidence=trace.final.evidence,
        trace=trace,
        question=ctx.question,
    )
