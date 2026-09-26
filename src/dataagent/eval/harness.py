"""评测跑批：跑用例 → 打分 → 汇总。

支持 `--checkpoint`（逐条落盘 + 断点续跑）。长跑批不落盘是最常见的
「跑了 40 分钟崩了重来」的来源，这里从一开始就把检查点做成一等公民。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..agents import analyze, ask, govern
from ..agents.loop import AgentResult
from ..agents.tools import ToolRegistry
from ..knowledge.index import KnowledgeIndex, load_index
from ..warehouse.engine import Warehouse
from .cases import EvalCase, load_cases
from .metrics import DIMENSIONS, CaseScore, score_case
from .oracle import expected_value_for


@dataclass
class EvalSummary:
    total: int
    dimension_means: dict[str, float]
    overall: float
    by_kind: dict[str, Any] = field(default_factory=dict)
    by_scenario: dict[str, Any] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "dimension_means": self.dimension_means,
            "overall": self.overall,
            "by_kind": self.by_kind,
            "by_scenario": self.by_scenario,
            "failures": self.failures,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }

    def dimension_line(self) -> str:
        return "  ".join(f"{d}={self.dimension_means[d]:.3f}" for d in DIMENSIONS)


def _run_case(case: EvalCase, registry: ToolRegistry, today: str) -> AgentResult:
    if case.kind == "qa":
        return ask(case.question, registry, today)
    if case.kind == "insight":
        return analyze(case.question, registry, today, max_steps=8)
    if case.kind == "govern_sql":
        return govern(case.expect.get("sql", ""), registry, kind="sql")
    if case.kind == "govern_table":
        return govern(case.expect.get("table", ""), registry, kind="table")
    raise ValueError(f"未知用例类型: {case.kind}")


def _oracle_for(case: EvalCase, index: KnowledgeIndex, wh: Warehouse) -> Optional[float]:
    exp = case.expect
    if exp.get("kind", "answer") != "answer":
        return None
    if case.kind not in ("qa", "insight"):
        return None
    mid = exp.get("metric_id")
    start, end = exp.get("start"), exp.get("end")
    if not (mid and start and end):
        return None
    return expected_value_for(index, wh, mid, start, end, exp.get("filters"))


def run_eval(
    index: Optional[KnowledgeIndex] = None,
    wh: Optional[Warehouse] = None,
    cases: Optional[Sequence[EvalCase]] = None,
    checkpoint: Optional[str | Path] = None,
    resume: bool = False,
    progress: bool = True,
    progress_every: int = 25,
) -> tuple[list[CaseScore], EvalSummary]:
    index = index or load_index()
    wh = wh or Warehouse(index=index)
    cases = list(cases if cases is not None else load_cases())
    today = wh.today
    registry = ToolRegistry(index, wh)

    done: dict[str, CaseScore] = {}
    ckpt = Path(checkpoint) if checkpoint else None
    if ckpt and resume and ckpt.exists():
        for line in ckpt.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            done[row["case_id"]] = CaseScore(
                case_id=row["case_id"],
                kind=row["kind"],
                scenario=row["scenario"],
                scores=row["scores"],
                detail=row.get("detail", {}),
            )

    started = time.perf_counter()
    scores: list[CaseScore] = []
    handle = ckpt.open("a", encoding="utf-8") if ckpt else None
    try:
        for i, case in enumerate(cases, 1):
            if case.id in done:
                scores.append(done[case.id])
                continue
            result = _run_case(case, registry, today)
            ov = _oracle_for(case, index, wh)
            sc = score_case(case, result, index, ov)
            scores.append(sc)
            if handle:
                handle.write(
                    json.dumps(
                        {
                            "case_id": sc.case_id,
                            "kind": sc.kind,
                            "scenario": sc.scenario,
                            "scores": sc.scores,
                            "detail": sc.detail,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                handle.flush()
            if progress and i % progress_every == 0:
                print(f"  … {i}/{len(cases)}", file=sys.stderr, flush=True)
    finally:
        if handle:
            handle.close()

    return scores, summarize(scores, elapsed_ms=(time.perf_counter() - started) * 1000)


def summarize(scores: Sequence[CaseScore], elapsed_ms: float = 0.0) -> EvalSummary:
    total = len(scores)
    dim_means = {
        d: round(sum(s.scores[d] for s in scores) / total, 4) if total else 0.0 for d in DIMENSIONS
    }
    overall = round(sum(s.overall for s in scores) / total, 4) if total else 0.0

    by_kind: dict[str, Any] = {}
    for kind in sorted({s.kind for s in scores}):
        subset = [s for s in scores if s.kind == kind]
        by_kind[kind] = {
            "count": len(subset),
            "overall": round(sum(s.overall for s in subset) / len(subset), 4),
            "dimensions": {
                d: round(sum(s.scores[d] for s in subset) / len(subset), 4) for d in DIMENSIONS
            },
        }

    by_scenario: dict[str, Any] = {}
    for scen in sorted({s.scenario for s in scores}):
        subset = [s for s in scores if s.scenario == scen]
        by_scenario[scen] = {
            "count": len(subset),
            "overall": round(sum(s.overall for s in subset) / len(subset), 4),
        }

    failures = [
        {
            "case_id": s.case_id,
            "kind": s.kind,
            "scenario": s.scenario,
            "overall": s.overall,
            "scores": s.scores,
            "detail": s.detail,
        }
        for s in scores
        if s.failed()
    ]
    return EvalSummary(
        total=total,
        dimension_means=dim_means,
        overall=overall,
        by_kind=by_kind,
        by_scenario=by_scenario,
        failures=failures,
        elapsed_ms=elapsed_ms,
    )
