"""评测体系：用例集 + Oracle + 五维评分 + 跑批 + 变异测试 + 报告。"""

from .cases import CASES_PATH, EvalCase, counts_by_kind, generate_cases, load_cases, write_cases
from .harness import EvalSummary, run_eval, summarize
from .metrics import DIMENSIONS, DIMENSION_LABELS, CaseScore, score_case
from .mutants import (
    MUTANTS,
    Mutant,
    MutationResult,
    baseline_dims,
    run_mutations,
)
from .oracle import anchor_value, expected_value, expected_value_for
from .report import render_html, render_json, render_markdown

__all__ = [
    "CASES_PATH",
    "DIMENSIONS",
    "DIMENSION_LABELS",
    "MUTANTS",
    "CaseScore",
    "EvalCase",
    "EvalSummary",
    "Mutant",
    "MutationResult",
    "anchor_value",
    "baseline_dims",
    "counts_by_kind",
    "expected_value",
    "expected_value_for",
    "generate_cases",
    "load_cases",
    "render_html",
    "render_json",
    "render_markdown",
    "run_eval",
    "run_mutations",
    "score_case",
    "summarize",
    "write_cases",
]
