"""自测台的差分测试：页面上的结果必须和命令行一致。

如果这里不盯住，控制台很容易长成「另一套实现」，然后和 CLI 的数字分家。
"""

from __future__ import annotations

from dataagent.agents import analyze, ask, govern
from dataagent.studio import _dispatch


def test_studio_ask_matches_cli(registry, today):
    q = "最近7天播放量是多少"
    direct = ask(q, registry, today)
    via_ui = _dispatch("ask", q)
    assert via_ui["kind"] == direct.kind
    assert via_ui["data"]["value"] == direct.data["value"]


def test_studio_analyze_matches_cli(registry, today):
    q = "2026-09-04 到 2026-09-10 播放量为什么降了"
    direct = analyze(q, registry, today, max_steps=8)
    via_ui = _dispatch("analyze", q)
    assert via_ui["kind"] == direct.kind
    assert via_ui["data"]["metric_id"] == direct.data["metric_id"]


def test_studio_govern_matches_cli(registry):
    sql = "SELECT * FROM dws_video_daily"
    direct = govern(sql, registry, kind="sql")
    via_ui = _dispatch("govern_sql", sql)
    assert via_ui["data"]["rule_ids"] == direct.data["rule_ids"]


def test_studio_unknown_mode_reports_error():
    out = _dispatch("nope", "")
    assert out["kind"] == "error"
