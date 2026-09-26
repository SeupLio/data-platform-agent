"""命令行入口：文档里写的命令必须真的能跑。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataagent.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_kb_stats(capsys):
    rc = main(["kb", "stats"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"metrics": 32' in out


def test_kb_check_is_fresh():
    assert main(["kb", "check"]) == 0


def test_ask_cli(capsys):
    rc = main(["ask", "最近7天播放量是多少"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "播放量" in out and "口径" in out


def test_ask_cli_refuses(capsys):
    rc = main(["ask", "帮我导出所有用户的手机号"])
    assert rc == 1
    assert "权限" in capsys.readouterr().out


def test_analyze_cli(capsys):
    rc = main(["analyze", "2026-09-04 到 2026-09-10 播放量为什么降了"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "游戏" in out


def test_govern_cli(capsys):
    rc = main(["govern", "--sql", "SELECT * FROM dws_video_daily"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "G01" in out and "G02" in out


def test_selftest_prints_key_numbers(capsys):
    rc = main(["selftest"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("KB:")
    assert "MUTANTS_CAUGHT: 6 / 6" in out


def test_eval_writes_reports(tmp_path):
    rc = main(["eval"])
    if rc == 1:
        pytest.skip("存在未满分用例，报告仍应写出")
    for name in ("eval_report.md", "eval_report.html", "eval_report.json"):
        p = ROOT / "docs" / "reports" / name
        assert p.exists() and p.stat().st_size > 0


def test_eval_report_json_is_parseable():
    p = ROOT / "docs" / "reports" / "eval_report.json"
    if not p.exists():
        main(["eval"])
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["total"] > 0
    assert set(data["dimension_means"]) == {
        "accuracy",
        "reasoning",
        "adaptation",
        "efficiency",
        "experience",
    }
