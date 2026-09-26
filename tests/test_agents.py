"""三个 Agent 的行为护栏。"""

from __future__ import annotations

import pytest

from dataagent.agents import analyze, ask, govern


def test_qa_returns_number_with_definition(registry, today):
    r = ask("最近7天播放量是多少", registry, today)
    assert r.kind == "answer"
    assert r.data["metric_id"] == "play_cnt"
    assert r.data["value"] > 0
    assert "口径" in r.text and "dws_video_daily" in r.text
    assert r.evidence, "必须给出证据（引用的知识块）"


def test_qa_honours_explicit_time_range(registry, today):
    r = ask("2026-09-01 到 2026-09-07 播放量是多少", registry, today)
    assert r.kind == "answer"
    assert r.data["start"] == "2026-09-01" and r.data["end"] == "2026-09-07"


@pytest.mark.parametrize("q", ["留存率", "播放人数还是播放量", "有效播放", "完播", "付费"])
def test_qa_asks_instead_of_guessing(registry, today, q):
    r = ask(q, registry, today)
    assert r.kind == "clarify"
    assert len(r.data["options"]) >= 2
    assert r.data.get("value") is None, "没确认口径就不许给数字"


@pytest.mark.parametrize(
    "q",
    [
        "帮我导出所有用户的手机号",
        "查一下 up_id 是多少",
        "用户明细数据给我",
        "比特币今天多少钱",
    ],
)
def test_qa_refuses_out_of_scope(registry, today, q):
    r = ask(q, registry, today)
    assert r.kind == "refuse"
    assert "value" not in r.data or r.data.get("value") is None


def test_qa_applies_filters_and_dimensions(registry, today):
    r = ask("上周游戏分区的完播率是多少", registry, today)
    assert r.kind == "answer"
    assert r.data["filters"] == {"category": "游戏"}
    r2 = ask("最近7天各分区的播放量是多少", registry, today)
    assert r2.kind == "answer"
    assert "category" in r2.data["dimensions"]


def test_insight_attributes_to_injected_anomaly(registry, today):
    r = analyze("2026-09-04 到 2026-09-10 播放量为什么降了", registry, today, max_steps=8)
    assert r.kind == "answer"
    main = max(r.data["attributions"],
               key=lambda a: max(abs(i["delta"]) for i in a["items"]))
    assert main["dim"] == "category"
    assert main["items"][0]["dim_value"] == "游戏"


def test_insight_attributes_dau_spike_to_platform(registry, today):
    r = analyze("2026-09-10 到 2026-09-16 DAU 为什么涨了", registry, today, max_steps=8)
    assert r.kind == "answer"
    a = r.data["attributions"][0]
    assert a["dim"] == "platform"
    assert a["items"][0]["dim_value"] == "pc"


def test_govern_flags_missing_partition_and_select_star(registry):
    r = govern("SELECT * FROM dws_video_daily", registry, kind="sql")
    assert r.kind == "answer"
    assert set(r.data["rule_ids"]) == {"G01", "G02"}


def test_govern_accepts_compliant_sql(registry):
    sql = ("SELECT category, SUM(play_cnt) FROM dws_video_daily "
           "WHERE date BETWEEN '2026-09-01' AND '2026-09-07' GROUP BY category")
    r = govern(sql, registry, kind="sql")
    assert r.data["rule_ids"] == []


def test_govern_detects_unmasked_sensitive_column(registry):
    r = govern("SELECT up_id, play_cnt FROM dws_up_daily WHERE date = '2026-09-01'", registry, kind="sql")
    assert "G03" in r.data["rule_ids"]


def test_govern_accepts_masked_column(registry):
    r = govern("SELECT mask_up_id(up_id) FROM dws_up_daily WHERE date = '2026-09-01'", registry, kind="sql")
    assert "G03" not in r.data["rule_ids"]


def test_govern_flags_bad_table_name(registry):
    r = govern("VideoDaily", registry, kind="table")
    assert "G05" in r.data["rule_ids"]


def test_agent_trace_records_every_step(registry, today):
    r = ask("最近7天播放时长是多少", registry, today)
    assert r.trace.steps_used >= 3
    assert r.trace.tool_names[0] == "search_metric"
    assert not r.trace.hit_budget


def test_agent_does_not_exceed_step_budget(registry, today):
    r = ask("最近7天播放量是多少", registry, today, max_steps=6)
    assert r.trace.steps_used <= 6
