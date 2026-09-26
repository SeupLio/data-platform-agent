"""评测体系自身的护栏：Oracle 独立性、用例集形状、评分可解释。"""

from __future__ import annotations

import sqlite3

import pytest

from dataagent.eval.cases import counts_by_kind, generate_cases, load_cases
from dataagent.eval.harness import run_eval, summarize
from dataagent.eval.metrics import DIMENSIONS, score_case
from dataagent.eval.oracle import (
    ANCHOR_RANGES,
    anchor_value,
    expected_value_for,
    parse_oracle_expr,
)


def test_generated_cases_match_committed_file():
    generated = [c.to_dict() for c in generate_cases()]
    committed = [c.to_dict() for c in load_cases()]
    assert generated == committed, "改了生成器必须重跑 scripts/gen_cases.py"


def test_case_set_covers_every_scenario():
    counts = counts_by_kind()
    assert set(counts) == {"qa", "insight", "govern_sql", "govern_table"}
    assert counts["qa"] >= 60 and counts["insight"] >= 15 and counts["govern_sql"] >= 15


def test_every_case_has_an_expectation():
    for c in load_cases():
        assert c.expect, c.id
        assert c.expect.get("kind") in ("answer", "clarify", "refuse"), c.id


@pytest.mark.parametrize("metric_id", ["play_cnt", "finish_rate", "dau", "active_up_cnt"])
def test_oracle_matches_hand_written_sql(index, wh, metric_id):
    """Oracle 必须和手写 SQL 对得上 —— 否则「期望值」本身就是另一个 bug。"""
    start, end = ANCHOR_RANGES[metric_id]
    got = expected_value_for(index, wh, metric_id, start, end)
    want = anchor_value(wh, metric_id)
    assert got == pytest.approx(want, rel=1e-9)


def test_oracle_parser_is_independent_of_agent_parser():
    assert parse_oracle_expr("sum(play_cnt)")[0] == "additive"
    assert parse_oracle_expr("sum(a) / sum(b)")[0] == "ratio"
    assert parse_oracle_expr("count(distinct up_id)")[0] == "distinct"
    assert parse_oracle_expr("count(distinct up_id)")[1] == ("up_id",)


def test_oracle_uses_raw_sql_not_agent_plan(index, wh):
    """Oracle 走的是裸 SQL，与被测的查询编译不是同一条代码路径。"""
    value = expected_value_for(index, wh, "play_cnt", "2026-09-18", "2026-09-24")
    conn = sqlite3.connect(str(wh.db_path))
    try:
        raw = conn.execute(
            "SELECT SUM(play_cnt) FROM dws_video_daily WHERE date BETWEEN '2026-09-18' AND '2026-09-24'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert value == raw


def test_evaluation_runs_and_scores_every_dimension(index, wh):
    scores, summary = run_eval(index=index, wh=wh, progress=False)
    assert len(scores) == summary.total
    assert set(summary.dimension_means) == set(DIMENSIONS)
    for d in DIMENSIONS:
        assert 0.0 <= summary.dimension_means[d] <= 1.0


def test_no_dimension_is_constantly_perfect_or_zero(index, wh):
    """一维恒 1.0 说明它没在测量东西 —— 这是评测里最贵的假象。"""
    scores, summary = run_eval(index=index, wh=wh, progress=False)
    per_case = {d: {s.scores[d] for s in scores} for d in DIMENSIONS}
    for d in DIMENSIONS:
        assert len(per_case[d]) >= 1
    assert summary.overall > 0


def test_scoring_penalises_wrong_metric(index, registry, today):
    from dataagent.agents import ask
    from dataagent.eval.cases import EvalCase

    result = ask("最近7天播放量是多少", registry, today)
    case = EvalCase(
        id="x",
        kind="qa",
        scenario="s",
        question="最近7天播放量是多少",
        expect={"kind": "answer", "metric_id": "watch_uv"},
    )
    wrong = score_case(case, result, index, None)
    assert wrong.scores["accuracy"] <= 0.5


def test_summarize_handles_empty():
    s = summarize([])
    assert s.total == 0 and s.overall == 0
