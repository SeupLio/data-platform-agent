"""知识库装载与检索。

检索不是「找个最像的」就完事 —— 数据问答里最贵的错误是**口径选错**，
所以这里的核心产出是 `Resolution`，它会显式回答「要不要先问一句」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .build import BUILT_DIR, knowledge_base_from_payload, load_payload
from .schema import Chunk, KnowledgeBase, Metric

_PUNCT = re.compile(r"[\s，。？！、；：,.\-_/()（）「」【】\"'`~!?]+")

# 第二个候选与第一个候选的得分比超过这个阈值，就认为「这两个口径分不开」，先问人
CLARIFY_RATIO = 0.80
MIN_SCORE = 12.0


def normalize(text: str) -> str:
    return _PUNCT.sub("", (text or "").strip().lower())


def bigrams(text: str) -> set[str]:
    t = normalize(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


@dataclass(frozen=True)
class Hit:
    metric: Metric
    score: float
    evidence: tuple[str, ...] = ()
    chunk_ids: tuple[str, ...] = ()
    matched_alias: str = ""
    match_kind: str = ""  # exact | in_query | query_in | ngram

    @property
    def metric_id(self) -> str:
        return self.metric.id

    def subsumes(self, other: "Hit") -> bool:
        """我的命中别名是不是把对方的命中别名整个包含进去了。

        「人均播放时长」命中时，「播放时长」必然也命中 —— 这不是两个口径在打架，
        而是一个更精确的匹配顺带触发了一个更泛的匹配。**不把它们当成歧义**，
        否则「问得越具体越会被反问」，产品上完全不可用。

        ⚠️ 只在「别名**出现在问题里**」时消解。问题本身只是个片段（比如用户只打了「播放」）
        时不能消解 —— 那种情况下两个口径是真分不开。
        """
        if self.match_kind not in ("exact", "in_query") or other.match_kind not in ("exact", "in_query"):
            return False
        a, b = normalize(self.matched_alias), normalize(other.matched_alias)
        return bool(a) and bool(b) and a != b and b in a


@dataclass(frozen=True)
class Resolution:
    """一次指标消歧的结果。

    `need_clarification=True` 时 `chosen` 为 None —— **不许猜**。
    猜错的口径会一路污染到 SQL、数字和结论，而且没人会发现。
    """

    query: str
    candidates: tuple[Hit, ...] = ()
    chosen: Optional[Hit] = None
    need_clarification: bool = False
    reason: str = ""
    clarify_options: tuple[str, ...] = ()

    @property
    def top(self) -> Optional[Hit]:
        return self.candidates[0] if self.candidates else None


def score_metric(query: str, metric: Metric) -> tuple[float, tuple[str, ...], str, str]:
    q = normalize(query)
    if not q:
        return 0.0, (), "", ""
    best = 0.0
    best_ev: tuple[str, ...] = ()
    best_alias = ""
    best_kind = ""
    qb = bigrams(q)

    for alias in (metric.name, *metric.aliases):
        a = normalize(alias)
        if not a:
            continue
        if q == a:
            score, ev, kind = 100.0, (f"精确命中口径名：{alias}",), "exact"
        elif a in q:
            score, ev, kind = 60.0 + min(len(a), 10), (f"问题中出现了口径名：{alias}",), "in_query"
        elif q in a:
            score, ev, kind = 50.0, (f"问题命中口径名片段：{alias}",), "query_in"
        else:
            ov = jaccard(qb, bigrams(alias))
            score, ev, kind = (
                (60.0 * ov, (f"与口径名「{alias}」的字符重合度 {ov:.2f}",), "ngram") if ov else (0.0, (), "")
            )
        if score > best:
            best, best_ev, best_alias, best_kind = score, ev, alias, kind

    # 业务域关键词只在没有强命中时作为微弱信号，避免「消费」把一堆消费域指标拉平
    if best < 60:
        dom = normalize(metric.domain)
        if dom and dom in q:
            best += 6.0
            best_ev = best_ev + (f"命中业务域：{metric.domain}",)
        else:
            ov = jaccard(qb, bigrams(metric.definition))
            if ov > 0.02:
                best += 20.0 * ov
                best_ev = best_ev + (f"与口径定义重合度 {ov:.2f}",)

    return round(best, 3), best_ev, best_alias, best_kind


class KnowledgeIndex:
    """在 KnowledgeBase 之上提供检索能力。"""

    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb
        self._alias_to_ids: dict[str, list[str]] = {}
        for m in kb.metrics.values():
            for alias in dict.fromkeys((m.name, *m.aliases)):
                key = normalize(alias)
                ids = self._alias_to_ids.setdefault(key, [])
                if m.id not in ids:
                    ids.append(m.id)

    def duplicated_aliases(self) -> dict[str, list[str]]:
        return {a: ids for a, ids in self._alias_to_ids.items() if len(ids) > 1}

    def search(self, query: str, top_k: int = 5) -> list[Hit]:
        scored: list[tuple[float, tuple[str, ...], str, str, Metric]] = []
        for mid in self.kb.metric_ids:
            metric = self.kb.metrics[mid]
            score, ev, alias, kind = score_metric(query, metric)
            if score <= 0:
                continue
            scored.append((score, ev, alias, kind, metric))
        scored.sort(key=lambda x: (-x[0], x[4].id))
        hits: list[Hit] = []
        for score, ev, alias, kind, metric in scored[:top_k]:
            chunk_ids = tuple(
                c.id for c in self.kb.chunks_of(metric.id) if c.kind in ("definition", "formula")
            )
            hits.append(
                Hit(
                    metric=metric,
                    score=score,
                    evidence=ev,
                    chunk_ids=chunk_ids,
                    matched_alias=alias,
                    match_kind=kind,
                )
            )
        return hits

    def resolve(self, query: str, top_k: int = 3) -> Resolution:
        hits = self.search(query, top_k=top_k)
        if not hits or hits[0].score < MIN_SCORE:
            return Resolution(
                query=query,
                candidates=tuple(hits),
                need_clarification=False,
                reason="知识库里没有匹配到任何已注册指标",
            )
        top = hits[0]
        second = hits[1] if len(hits) > 1 else None
        if second is not None and second.score >= CLARIFY_RATIO * top.score:
            if top.subsumes(second) or second.subsumes(top):
                # 更长的别名把更短的整个包住了 → 是精确匹配，不是口径打架
                winner = top if top.subsumes(second) else second
                return Resolution(
                    query=query,
                    candidates=tuple(hits),
                    chosen=winner,
                    need_clarification=False,
                    reason=f"以更精确的口径「{winner.metric.name}」为准",
                )
            options = tuple(h.metric.name for h in hits[:3])
            return Resolution(
                query=query,
                candidates=tuple(hits),
                chosen=None,
                need_clarification=True,
                reason=(
                    f"「{query}」同时命中 {hits[0].metric.name}"
                    f"（{hits[0].score:.1f}）与 {hits[1].metric.name}（{hits[1].score:.1f}），"
                    "两者口径不同，需要先确认"
                ),
                clarify_options=options,
            )
        return Resolution(query=query, candidates=tuple(hits), chosen=top, need_clarification=False)

    def confusable_with(self, metric_id: str) -> tuple[str, ...]:
        out: list[str] = []
        for a, b in self.kb.confusable_pairs:
            if a == metric_id:
                out.append(b)
            elif b == metric_id:
                out.append(a)
        return tuple(sorted(out))

    def chunk(self, chunk_id: str) -> Chunk:
        for c in self.kb.chunks:
            if c.id == chunk_id:
                return c
        raise KeyError(f"未注册的知识块: {chunk_id}")


@lru_cache(maxsize=8)
def load_index(built_dir: Optional[str] = None) -> KnowledgeIndex:
    path = Path(built_dir) if built_dir else BUILT_DIR
    payload = load_payload(path)
    return KnowledgeIndex(knowledge_base_from_payload(payload))
