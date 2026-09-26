"""文档新鲜度护栏：README / 文档里印的数字必须和代码跑出来的一致。

⚠️ 只扫**指定的数据区**，不整份扫 —— 整份扫会把散文里引用的历史数字当成真输出，
误报之后下一个人就会加 skip 把它关掉，那比没有护栏更糟。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
EVAL_JSON = ROOT / "docs" / "reports" / "eval_report.json"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_readme_kb_counts_match_reality(index):
    text = _read(README)
    kb = index.kb.summary()
    assert f"{kb['metrics']} 指标" in text, "README 里的指标数与知识库不一致"
    assert f"{kb['models']} 数据模型" in text or f"{kb['models']} 模型" in text
    assert f"{kb['policies']} 治理规则" in text or f"{kb['policies']} 规则" in text
    assert f"{kb['chunks']} 知识块" in text
    assert f"{kb['confusable_pairs']} 易混对" in text or f"{kb['confusable_pairs']} 对" in text


def test_readme_case_counts_match_reality():
    from dataagent.eval.cases import counts_by_kind

    counts = counts_by_kind()
    text = _read(README)
    total = sum(counts.values())
    assert f"{total} 条用例" in text or f"{total} 条" in text
    for kind, n in counts.items():
        assert re.search(rf"{kind}\s*{n}\s*条", text), f"README 没写清 {kind} 的条数（应为 {n}）"


def test_eval_report_is_regenerable_and_consistent():
    """报告里的用例数必须等于当前用例集大小 —— 否则报告已经过期。"""
    from dataagent.eval.cases import counts_by_kind

    assert EVAL_JSON.exists(), "docs/reports/eval_report.json 不存在，请跑 python -m dataagent.cli eval"
    data = json.loads(_read(EVAL_JSON))
    assert data["total"] == sum(counts_by_kind().values())


def test_mutation_report_lists_all_mutants():
    from dataagent.eval.mutants import MUTANTS

    path = ROOT / "docs" / "reports" / "mutation_report.md"
    assert path.exists(), "mutation_report.md 不存在，请跑 python -m dataagent.cli mutate"
    text = _read(path)
    for m in MUTANTS:
        assert m.id in text, f"变异报告缺少 {m.id}"


def test_readme_does_not_overclaim():
    """README 里不许出现「已接入大模型」「真实用户」这类没做过的说法。"""
    text = _read(README)
    forbidden = ["已接入大模型", "真实用户调研显示", "生产环境验证"]
    for word in forbidden:
        assert word not in text, f"README 出现了没做过的说法：{word}"
    assert "诚实的局限" in text


@pytest.mark.parametrize(
    "doc",
    [
        "docs/PRD_数据平台Agent.md",
        "docs/产品方案.md",
        "docs/评测体系设计.md",
        "docs/知识库构建说明.md",
        "docs/面试问答.md",
        "docs/原型图.html",
    ],
)
def test_deliverable_docs_exist_and_are_substantial(doc):
    path = ROOT / doc
    assert path.exists(), f"缺少交付物 {doc}"
    assert path.stat().st_size > 4000, f"{doc} 内容过少"
