"""知识库：拆解流水线与检索的护栏。"""

from __future__ import annotations

import pytest

from dataagent.knowledge.build import (
    BUILT_DIR,
    KnowledgeBuildError,
    build_all,
    build_chunks,
    build_confusable_pairs,
    build_metrics,
    dump_yaml,
    load_payload,
)
from dataagent.knowledge.index import KnowledgeIndex, normalize, score_metric
from dataagent.knowledge.schema import parse_formula


def test_build_is_deterministic_and_fresh():
    """产物必须能从原始语料逐字节重建 —— 否则「产品侧读到的口径」和 wiki 就分家了。"""
    payload = build_all()
    current = load_payload(BUILT_DIR)
    for key in payload:
        assert dump_yaml(payload[key]) == dump_yaml(current[key]), f"{key} 与原始语料不一致"


def test_metric_required_fields_are_present(index):
    for mid, m in index.kb.metrics.items():
        assert m.definition and m.formula and m.table and m.owner, mid
        assert m.dimensions, mid
        assert m.unit and m.value_type in ("int", "ratio", "money"), mid
        assert m.level in ("原子指标", "派生指标"), mid


def test_every_metric_formula_is_computable(index):
    for mid, m in index.kb.metrics.items():
        expr = m.expr  # 解析不出来会抛异常
        assert expr.kind in ("additive", "ratio", "distinct"), mid
        assert expr.columns, mid


def test_metric_dependencies_exist_in_model(index):
    """指标引用的字段必须在来源表的字段清单里 —— 这是最容易悄悄坏掉的一致性。"""
    for mid, m in index.kb.metrics.items():
        model = index.kb.model(m.table)
        missing = (set(m.dimensions) | set(m.expr.columns)) - set(model.columns)
        assert not missing, f"{mid} 依赖了 {m.table} 里没有的字段 {missing}"


def test_confusable_pairs_are_symmetric_and_resolvable(index):
    for a, b in index.kb.confusable_pairs:
        assert a in index.kb.metrics and b in index.kb.metrics
        assert b in index.confusable_with(a) and a in index.confusable_with(b)


def test_no_duplicate_alias_across_metrics(index):
    """同一个别名指向两个指标 = 检索必然挑一个，而挑哪个没人说得清。"""
    assert index.duplicated_aliases() == {}


def test_chunks_cover_every_metric(index):
    for mid in index.kb.metric_ids:
        kinds = {c.kind for c in index.kb.chunks_of(mid)}
        assert {"definition", "formula", "dimension"} <= kinds, mid


def test_build_rejects_bad_glossary():
    bad = "## 坏指标\n\n- 指标编码：x\n- 业务域：消费\n"
    with pytest.raises(KnowledgeBuildError):
        build_metrics(bad)


def test_build_rejects_unresolvable_confusable():
    rows = [
        {
            "id": "a",
            "name": "指标A",
            "definition": "d",
            "formula": "sum(play_cnt)",
            "confusable_with": ("不存在的指标",),
        }
    ]
    with pytest.raises(KnowledgeBuildError):
        build_confusable_pairs(rows)


def test_chunk_count_matches_metrics(index):
    payload = build_all()
    assert payload["meta"]["counts"]["chunks"] == len(index.kb.chunks)


def test_parse_formula_shapes():
    assert parse_formula("sum(play_cnt)").kind == "additive"
    assert parse_formula("sum(finish_cnt) / sum(play_cnt)").kind == "ratio"
    assert parse_formula("count(distinct up_id)").kind == "distinct"
    multi = parse_formula("sum(like_cnt + coin_cnt) / sum(play_cnt)")
    assert multi.numerator == ("like_cnt", "coin_cnt")
    assert multi.denominator == ("play_cnt",)
    with pytest.raises(ValueError):
        parse_formula("")
    with pytest.raises(ValueError):
        parse_formula("avg(play_cnt)")


def test_specific_metric_beats_generic_alias(index):
    """问得具体时不能被反问 —— 「人均播放时长」里也含「播放时长」是正常现象。"""
    r = index.resolve("最近7天人均播放时长是多少")
    assert not r.need_clarification
    assert r.chosen.metric_id == "avg_play_duration"


def test_vague_query_triggers_clarification(index):
    for q in ("留存率", "播放人数还是播放量", "有效播放", "完播"):
        r = index.resolve(q)
        assert r.need_clarification, q
        assert len(r.clarify_options) >= 2, q


def test_unknown_query_finds_nothing(index):
    r = index.resolve("比特币今天多少钱")
    assert r.chosen is None


def test_subsumption_only_applies_to_in_query_matches(index):
    """「播放」这种片段型提问不能靠子串包含来消歧（两个口径是真分不开）。"""
    top, second = index.search("播放", top_k=2)[0], index.search("播放", top_k=2)[1]
    assert top.match_kind == "query_in"
    assert not top.subsumes(second)


def test_score_metric_is_deterministic(index):
    m = index.kb.metric("play_cnt")
    a = score_metric("最近7天播放量是多少", m)
    b = score_metric("最近7天播放量是多少", m)
    assert a == b


def test_normalize_strips_punctuation():
    assert normalize("播放量，是多少？") == "播放量是多少"
