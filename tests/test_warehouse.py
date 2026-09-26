"""离线数仓与查询执行的护栏。"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from dataagent.knowledge.schema import parse_formula
from dataagent.warehouse.engine import QueryError, QueryPlan, Warehouse
from dataagent.warehouse.seed import ANOMALIES, generate


def test_seed_is_reproducible():
    """换机器 clone 后必须跑出同一份数据，否则 README 里每个数字都是假的。"""
    with tempfile.TemporaryDirectory() as td:
        a = generate(Path(td) / "a.db")
        b = generate(Path(td) / "b.db")
        assert a.video_rows == b.video_rows and a.user_rows == b.user_rows and a.up_rows == b.up_rows
        conn_a = sqlite3.connect(str(Path(td) / "a.db"))
        conn_b = sqlite3.connect(str(Path(td) / "b.db"))
        try:
            q = "SELECT SUM(play_cnt), SUM(duration_sec) FROM dws_video_daily"
            assert conn_a.execute(q).fetchone() == conn_b.execute(q).fetchone()
        finally:
            conn_a.close()
            conn_b.close()


def test_injected_anomalies_are_present(wh):
    conn = sqlite3.connect(str(wh.db_path))
    try:
        for a in ANOMALIES:
            if a["metric"] == "play_cnt":
                row = conn.execute(
                    "SELECT SUM(play_cnt) v FROM dws_video_daily WHERE date=? AND category=?",
                    (a["date"], a["value"]),
                ).fetchone()
                baseline = conn.execute(
                    "SELECT SUM(play_cnt) v FROM dws_video_daily WHERE date=? AND category=?",
                    ("2026-09-03", a["value"]),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT SUM(dau) v FROM dws_user_daily WHERE date=? AND platform=?",
                    (a["date"], a["value"]),
                ).fetchone()
                baseline = conn.execute(
                    "SELECT SUM(dau) v FROM dws_user_daily WHERE date=? AND platform=?",
                    ("2026-09-09", a["value"]),
                ).fetchone()
            assert row[0] is not None and baseline[0] is not None
            if a["factor"] < 1:
                assert row[0] < baseline[0] * 0.7, a
            else:
                assert row[0] > baseline[0] * 1.2, a
    finally:
        conn.close()


def test_today_comes_from_data_not_system_clock(wh):
    assert wh.today == "2026-09-24"


def test_query_requires_partition_filter(wh):
    plan = QueryPlan("play_cnt", "dws_video_daily", (), (), "", "")
    with pytest.raises(QueryError):
        wh.execute(plan, "次", "int")


def test_query_rejects_unknown_column(wh, index):
    from dataagent.knowledge.schema import Metric

    fake = Metric(
        id="play_cnt",
        name="播放量",
        domain="消费",
        definition="d",
        formula="sum(play_cnt)",
        table="dws_video_daily",
        dimensions=(),
        unit="次",
        value_type="int",
        level="原子指标",
        owner="o",
        aliases=(),
        confusable_with=(),
        caveats="",
    )
    index.kb.metrics["__fake__"] = fake
    try:
        plan = QueryPlan("__fake__", "dws_video_daily", (), (), "2026-09-01", "2026-09-07")
        wh.execute(plan, "次", "int")  # 字段合法，应当通过
        bad = QueryPlan(metric_id="__fake__", table="dws_video_daily", dimensions=("not_a_col",),
                        start="2026-09-01", end="2026-09-07")
        with pytest.raises(QueryError):
            wh.execute(bad, "次", "int")
    finally:
        del index.kb.metrics["__fake__"]


def test_query_rejects_dimension_not_supported_by_metric(wh):
    plan = QueryPlan("play_cnt", "dws_video_daily", ("up_id",), (), "2026-09-01", "2026-09-07")
    with pytest.raises(QueryError):
        wh.execute(plan, "次", "int")


def test_distinct_metric_is_not_additive_across_dimensions(wh):
    plan = QueryPlan("active_up_cnt", "dws_up_daily", ("category",), (), "2026-09-24", "2026-09-24")
    res = wh.execute(plan, "人", "int")
    assert res.value is None
    assert "不可加" in res.note
    assert res.breakdown, "逐维度仍应给出各自的去重值"


def test_distinct_metric_without_dimension_returns_count(wh):
    plan = QueryPlan("active_up_cnt", "dws_up_daily", (), (), "2026-09-24", "2026-09-24")
    res = wh.execute(plan, "人", "int")
    assert res.value is not None and res.value > 0


def test_ratio_metric_matches_hand_written_sql(wh, index):
    m = index.kb.metric("finish_rate")
    plan = QueryPlan("finish_rate", m.table, (), (), "2026-09-18", "2026-09-24")
    res = wh.execute(plan, m.unit, m.value_type)
    conn = sqlite3.connect(str(wh.db_path))
    try:
        row = conn.execute(
            "SELECT CAST(SUM(finish_cnt) AS REAL)/SUM(play_cnt) v FROM dws_video_daily "
            "WHERE date BETWEEN '2026-09-18' AND '2026-09-24'"
        ).fetchone()
    finally:
        conn.close()
    assert res.value == pytest.approx(row[0], rel=1e-9)


def test_compile_sql_is_parameterised(wh):
    expr = parse_formula("sum(play_cnt)")
    plan = QueryPlan("play_cnt", "dws_video_daily", (), (("platform", "ios"),),
                     "2026-09-18", "2026-09-24")
    sql, params = wh.compile_sql(plan, expr)
    assert "?" in sql
    assert "ios" not in sql  # 筛选值必须走绑定，不能拼进 SQL
    assert params[-1] == "ios"


def test_profile_reports_row_count(wh):
    prof = wh.profile("dws_video_daily")
    assert prof["rows"] > 0
    assert prof["date_range"][1] == "2026-09-24"
