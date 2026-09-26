"""知识拆解流水线：非结构化 wiki 语料 → 结构化知识库。

这是 JD 里「知识库的内容录入、知识拆解、结构化整理」对应的那一步。
设计要点：

1. **原始语料是唯一真相源**。`data/raw/*.md` 是输入，`data/built/*.yaml` 是产物，
   产物必须能被重新生成且逐字节一致（有护栏测试盯着）。
2. **解析失败要炸，不要兜底**。口径解析不出来比解析错更危险 —— 错口径会静默污染所有回答。
3. **一个指标拆成多个 chunk**，回答时引用 chunk 而不是整篇文档，证据才对得上。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from .schema import Chunk, DataModel, KnowledgeBase, Metric, PolicyRule, parse_formula

RAW_DIR = Path(__file__).parent / "data" / "raw"
BUILT_DIR = Path(__file__).parent / "data" / "built"

RAW_FILES = {
    "metrics": "metric_glossary.md",
    "models": "model_spec.md",
    "policies": "governance_rules.md",
}

METRIC_FIELDS = {
    "指标编码": "id",
    "业务域": "domain",
    "口径定义": "definition",
    "计算方式": "formula",
    "来源表": "table",
    "支持维度": "dimensions",
    "单位": "unit",
    "数值类型": "value_type",
    "指标层级": "level",
    "口径负责人": "owner",
    "别名": "aliases",
    "易混指标": "confusable_with",
    "注意事项": "caveats",
}

MODEL_FIELDS = {
    "分层": "layer",
    "业务域": "domain",
    "粒度": "grain",
    "分区字段": "partition_key",
    "字段": "columns",
    "上游": "upstream",
    "下游": "downstream",
    "负责人": "owner",
    "备注": "notes",
}

POLICY_FIELDS = {
    "严重级别": "severity",
    "适用对象": "target",
    "检查逻辑": "logic",
    "修复建议": "fix",
    "负责人": "owner",
}

REQUIRED_METRIC_FIELDS = (
    "id",
    "domain",
    "definition",
    "formula",
    "table",
    "dimensions",
    "unit",
    "value_type",
    "level",
    "owner",
    "aliases",
)

LIST_FIELDS = {"dimensions", "aliases", "confusable_with", "columns", "upstream", "downstream"}


class KnowledgeBuildError(ValueError):
    """拆解流水线失败。"""


def _read(name: str) -> str:
    path = RAW_DIR / name
    if not path.exists():
        raise KnowledgeBuildError(f"缺少原始语料: {path}")
    return path.read_text(encoding="utf-8")


def split_blocks(markdown: str) -> list[tuple[str, str]]:
    """按 `## ` 标题切块，返回 [(标题, 正文)]。"""
    blocks: list[tuple[str, str]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if current_title is not None:
                blocks.append((current_title, "\n".join(current_lines)))
            current_title = line[3:].strip()
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)
    if current_title is not None:
        blocks.append((current_title, "\n".join(current_lines)))
    if not blocks:
        raise KnowledgeBuildError("原始语料里没有解析到任何 `## ` 分块")
    return blocks


def parse_fields(body: str, mapping: dict[str, str]) -> dict[str, Any]:
    """解析 `- 键：值` 形式的字段行。"""
    out: dict[str, Any] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        raw = line[2:].strip()
        if "：" not in raw:
            continue
        key, _, value = raw.partition("：")
        key = key.strip()
        value = value.strip()
        field = mapping.get(key)
        if field is None:
            continue
        if field in LIST_FIELDS:
            items = [x.strip() for x in re.split(r"[|,]", value) if x.strip()]
            out[field] = tuple(items)
        else:
            out[field] = value
    return out


def _split_list(value: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in re.split(r"[|,]", value) if x.strip())


def build_metrics(markdown: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for title, body in split_blocks(markdown):
        fields = parse_fields(body, METRIC_FIELDS)
        fields.setdefault("name", title)
        missing = [f for f in REQUIRED_METRIC_FIELDS if not fields.get(f)]
        if missing:
            raise KnowledgeBuildError(f"指标《{title}》缺少必填字段: {missing}")
        # 计算式必须真的能解析 —— 这是「口径可计算」的硬门槛
        parse_formula(fields["formula"])
        fields.setdefault("confusable_with", ())
        fields.setdefault("caveats", "")
        rows.append(fields)
    rows.sort(key=lambda r: r["id"])
    ids = [r["id"] for r in rows]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise KnowledgeBuildError(f"指标编码重复: {sorted(dup)}")
    return rows


def build_models(markdown: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for title, body in split_blocks(markdown):
        fields = parse_fields(body, MODEL_FIELDS)
        fields["id"] = title
        if not fields.get("columns"):
            raise KnowledgeBuildError(f"数据模型《{title}》缺少字段清单")
        fields.setdefault("upstream", ())
        fields.setdefault("downstream", ())
        fields.setdefault("notes", "")
        rows.append(fields)
    rows.sort(key=lambda r: r["id"])
    return rows


def build_policies(markdown: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for title, body in split_blocks(markdown):
        m = re.match(r"(G\d+)\s+(.*)", title.strip())
        if not m:
            raise KnowledgeBuildError(f"治理规则标题不规范: {title!r}")
        fields = parse_fields(body, POLICY_FIELDS)
        fields["id"] = m.group(1)
        fields["name"] = m.group(2).strip()
        for f in ("severity", "target", "logic", "fix", "owner"):
            if not fields.get(f):
                raise KnowledgeBuildError(f"治理规则 {fields['id']} 缺少字段 {f}")
        rows.append(fields)
    rows.sort(key=lambda r: r["id"])
    return rows


def build_chunks(metric_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把一个指标拆成若干可独立检索/引用的原子知识点。"""
    chunks: list[dict[str, Any]] = []
    for row in metric_rows:
        mid = row["id"]
        name = row["name"]
        pieces = [
            ("definition", f"{name}（{row['unit']}）：{row['definition']}"),
            ("formula", f"{name} 的计算方式：{row['formula']}，来源表 {row['table']}"),
            ("dimension", f"{name} 支持维度：{'、'.join(row['dimensions'])}"),
        ]
        caveats = row.get("caveats", "")
        if caveats and caveats != "无":
            pieces.append(("caveat", f"{name} 口径注意：{caveats}"))
        conf = row.get("confusable_with", ())
        if conf and conf != ("无",):
            pieces.append(("confusable", f"{name} 易混指标：{'、'.join(conf)}"))
        for kind, text in pieces:
            chunks.append({"id": f"{mid}::{kind}", "metric_id": mid, "kind": kind, "text": text})
    chunks.sort(key=lambda c: c["id"])
    return chunks


def build_confusable_pairs(metric_rows: list[dict[str, Any]]) -> list[list[str]]:
    """把「易混指标」归一化成无向对。

    易混关系在 wiki 里是**单向**写的（只在其中一个指标上写），
    检索时要当无向用 —— 否则「从 B 查 A」会漏掉消歧。
    """
    name_to_id = {r["name"]: r["id"] for r in metric_rows}
    pairs: set[tuple[str, str]] = set()
    for row in metric_rows:
        for other in row.get("confusable_with", ()):
            if other in ("无",):
                continue
            if other not in name_to_id:
                raise KnowledgeBuildError(
                    f"指标 {row['id']} 的易混指标《{other}》在词典里找不到"
                )
            a, b = row["id"], name_to_id[other]
            pairs.add((a, b) if a < b else (b, a))
    return [list(p) for p in sorted(pairs)]


def build_all(raw_dir: Path | None = None) -> dict[str, Any]:
    raw_dir = Path(raw_dir) if raw_dir else RAW_DIR
    sources: dict[str, str] = {}
    for key, fname in RAW_FILES.items():
        text = (raw_dir / fname).read_text(encoding="utf-8")
        sources[key] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    metric_rows = build_metrics((raw_dir / RAW_FILES["metrics"]).read_text(encoding="utf-8"))
    model_rows = build_models((raw_dir / RAW_FILES["models"]).read_text(encoding="utf-8"))
    policy_rows = build_policies((raw_dir / RAW_FILES["policies"]).read_text(encoding="utf-8"))
    chunk_rows = build_chunks(metric_rows)
    pairs = build_confusable_pairs(metric_rows)

    # 指标引用了不存在的表 / 维度不在表里 —— 都是知识库自相矛盾，必须当场炸
    model_ids = {r["id"] for r in model_rows}
    model_by_id = {r["id"]: r for r in model_rows}
    for row in metric_rows:
        if row["table"] not in model_ids:
            raise KnowledgeBuildError(f"指标 {row['id']} 引用了未注册的来源表 {row['table']}")
    for row in metric_rows:
        cols = set(model_by_id[row["table"]]["columns"])
        needed = set(row["dimensions"]) | set(parse_formula(row["formula"]).columns)
        missing = sorted(needed - cols)
        if missing:
            raise KnowledgeBuildError(
                f"指标 {row['id']} 依赖的字段 {missing} 不在表 {row['table']} 的字段清单里"
            )

    return {
        "meta": {
            "source_sha256": sources,
            "counts": {
                "metrics": len(metric_rows),
                "models": len(model_rows),
                "policies": len(policy_rows),
                "chunks": len(chunk_rows),
                "confusable_pairs": len(pairs),
            },
        },
        "metrics": metric_rows,
        "models": model_rows,
        "policies": policy_rows,
        "chunks": chunk_rows,
        "confusable_pairs": pairs,
    }


def dump_yaml(payload: dict[str, Any]) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=True, default_flow_style=False)


def write_built(payload: dict[str, Any], out_dir: Path | None = None) -> list[Path]:
    out_dir = Path(out_dir) if out_dir else BUILT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for key in ("metrics", "models", "policies", "chunks", "confusable_pairs", "meta"):
        path = out_dir / f"{key}.yaml"
        path.write_text(dump_yaml(payload[key]), encoding="utf-8")
        written.append(path)
    return written


def load_payload(built_dir: Path | None = None) -> dict[str, Any]:
    built_dir = Path(built_dir) if built_dir else BUILT_DIR
    data: dict[str, Any] = {}
    for key in ("metrics", "models", "policies", "chunks", "confusable_pairs", "meta"):
        path = built_dir / f"{key}.yaml"
        if not path.exists():
            raise KnowledgeBuildError(f"缺少拆解产物，请先运行 scripts/build_kb.py: {path}")
        data[key] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def knowledge_base_from_payload(payload: dict[str, Any]) -> KnowledgeBase:
    metrics = {
        r["id"]: Metric(
            id=r["id"],
            name=r["name"],
            domain=r["domain"],
            definition=r["definition"],
            formula=r["formula"],
            table=r["table"],
            dimensions=tuple(r["dimensions"]),
            unit=r["unit"],
            value_type=r["value_type"],
            level=r["level"],
            owner=r["owner"],
            aliases=tuple(r["aliases"]),
            confusable_with=tuple(r["confusable_with"]),
            caveats=r.get("caveats", ""),
        )
        for r in payload["metrics"]
    }
    models = {
        r["id"]: DataModel(
            id=r["id"],
            layer=r["layer"],
            domain=r["domain"],
            grain=r["grain"],
            partition_key=r["partition_key"],
            columns=tuple(r["columns"]),
            upstream=tuple(r["upstream"]),
            downstream=tuple(r["downstream"]),
            owner=r["owner"],
            notes=r["notes"],
        )
        for r in payload["models"]
    }
    policies = {
        r["id"]: PolicyRule(
            id=r["id"],
            name=r["name"],
            severity=r["severity"],
            target=r["target"],
            logic=r["logic"],
            fix=r["fix"],
            owner=r["owner"],
        )
        for r in payload["policies"]
    }
    chunks = tuple(
        Chunk(id=c["id"], metric_id=c["metric_id"], kind=c["kind"], text=c["text"])
        for c in payload["chunks"]
    )
    pairs = tuple((a, b) for a, b in payload["confusable_pairs"])
    return KnowledgeBase(
        metrics=metrics, models=models, policies=policies, chunks=chunks, confusable_pairs=pairs
    )
