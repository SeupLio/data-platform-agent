"""数据治理助手：把「规范」变成可执行的检查。

治理 Agent 的输入是一段 SQL / 一张表 / 一个新指标，输出是**问题清单**：
规则号 + 级别 + 命中证据 + 修复建议 + 责任组。

关键设计：规则**数据驱动** —— 规则本身来自知识库 `policies.yaml`，
这里只写「怎么判」，不写「判什么」。加规则改 wiki，不改代码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..knowledge.index import KnowledgeIndex
from ..knowledge.schema import PolicyRule

SENSITIVE_COLUMNS = ("up_id", "user_id", "phone", "device_id", "id_card")
MASK_PREFIX = "mask_"

_TABLE_RE = re.compile(r"\b((?:ods|dwd|dws|ads|dim)_[A-Za-z0-9_]+)\b", re.IGNORECASE)
_CAMEL_RE = re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9_]*\b")
_FUNC_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()]*)\)")
_SQL_KEYWORDS = {"select", "from", "where", "group", "order", "by", "limit", "join", "on", "and", "or"}


@dataclass(frozen=True)
class Finding:
    rule_id: str
    rule_name: str
    severity: str
    evidence: str
    fix: str
    owner: str
    target: str

    def as_line(self) -> str:
        return f"[{self.severity}] {self.rule_id} {self.rule_name} —— {self.evidence}"


@dataclass(frozen=True)
class GovernanceReport:
    target: str
    target_kind: str
    findings: tuple[Finding, ...]
    checked_rules: int

    @property
    def counts(self) -> dict[str, int]:
        out = {"P0": 0, "P1": 0, "P2": 0}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    def rule_ids(self) -> tuple[str, ...]:
        return tuple(sorted({f.rule_id for f in self.findings}))


def _where_clause(sql: str) -> str:
    m = re.search(r"\bwhere\b(.*?)(?:\bgroup\b|\border\b|\blimit\b|$)", sql, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else ""


def _tables(sql: str) -> list[str]:
    return [t.lower() for t in _TABLE_RE.findall(sql)]


def _masked_columns(sql: str) -> set[str]:
    masked: set[str] = set()
    for fname, args in _FUNC_CALL_RE.findall(sql):
        if fname.lower().startswith(MASK_PREFIX):
            for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", args):
                masked.add(tok.lower())
    return masked


def check_sql(sql: str, index: KnowledgeIndex) -> tuple[Finding, ...]:
    rules = index.kb.policies
    findings: list[Finding] = []

    def hit(rule_id: str, evidence: str) -> None:
        r: PolicyRule = rules[rule_id]
        findings.append(
            Finding(
                rule_id=r.id,
                rule_name=r.name,
                severity=r.severity,
                evidence=evidence,
                fix=r.fix,
                owner=r.owner,
                target="sql",
            )
        )

    tables = _tables(sql)
    detail_tables = [t for t in tables if t.startswith(("dwd_", "dws_"))]
    where = _where_clause(sql)
    lower = sql.lower()

    # G01 分区过滤缺失
    if detail_tables and "date" not in where.lower():
        hit("G01", f"查询了 {', '.join(sorted(set(detail_tables)))}，但 where 中没有 date 条件")

    # G02 禁止 SELECT *
    if re.search(r"select\s+\*|,\s*\*", lower):
        hit("G02", "select 列表中出现 *")

    # G03 敏感字段未脱敏
    masked = _masked_columns(sql)
    for col in SENSITIVE_COLUMNS:
        for m in re.finditer(rf"\b{col}\b", lower):
            start = m.start()
            before = lower[max(0, start - 12) : start]
            if col in masked or MASK_PREFIX in before:
                continue
            hit("G03", f"敏感字段 {col} 未脱敏直接输出")
            break

    # G04 指标口径硬编码
    if "/" in sql and re.search(r"\bsum\s*\(", lower):
        hit("G04", "SQL 内直接书写比率计算，未引用指标平台")

    # G06 字段命名不规范
    for m in _CAMEL_RE.finditer(sql):
        tok = m.group(0)
        if tok.lower() in _SQL_KEYWORDS:
            continue
        hit("G06", f"出现驼峰字段 {tok}")
        break

    # G07 跨层依赖
    if any(t.startswith("ads_") for t in tables) and any(t.startswith("dwd_") for t in tables):
        hit("G07", "ADS 层查询直接引用 DWD 层表")

    # G08 JOIN 缺少关联条件
    if re.search(r"\bjoin\b", lower) and not re.search(r"\bjoin\b.*?\bon\b", lower, re.DOTALL):
        hit("G08", "存在 join 但没有 on 关联条件")

    # G09 无上限全表导出
    if any(t.startswith("dwd_") for t in tables) and "date" not in where.lower() and "limit" not in lower:
        hit("G09", "明细层查询既无分区过滤也无 limit")

    return tuple(findings)


def check_table(table: str, index: KnowledgeIndex) -> tuple[Finding, ...]:
    rules = index.kb.policies
    findings: list[Finding] = []
    if not re.match(r"^(ods|dwd|dws|ads|dim)_[a-z0-9_]+$", table):
        r = rules["G05"]
        reason = "缺少分层前缀" if not re.match(r"^(ods|dwd|dws|ads|dim)_", table) else "含大写或非下划线命名"
        findings.append(
            Finding(
                rule_id=r.id,
                rule_name=r.name,
                severity=r.severity,
                evidence=f"表名 {table} 不规范（{reason}）",
                fix=r.fix,
                owner=r.owner,
                target="table",
            )
        )
    return tuple(findings)


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text)
    return {t[i : i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else {t}


def similarity(a: str, b: str) -> float:
    """用于「指标重复建设」的口径相似度（ containment，长文本不会被判低 ）。"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / min(len(A), len(B))


def check_metric(name: str, definition: str, index: KnowledgeIndex,
                 threshold: float = 0.90) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    r = index.kb.policies["G10"]
    for mid, metric in index.kb.metrics.items():
        score = similarity(definition, metric.definition)
        if score >= threshold and metric.name != name:
            findings.append(
                Finding(
                    rule_id=r.id,
                    rule_name=r.name,
                    severity=r.severity,
                    evidence=f"《{name}》与已注册指标《{metric.name}》（{mid}）口径相似度 {score:.2f}",
                    fix=r.fix,
                    owner=r.owner,
                    target="metric",
                )
            )
    # 同一条规则对同一目标报一次就够，取最像的那个
    if findings:
        findings.sort(key=lambda f: f.evidence)
        findings = findings[:1]
    return tuple(findings)


def govern_sql(sql: str, index: KnowledgeIndex) -> GovernanceReport:
    return GovernanceReport(
        target=sql.strip(),
        target_kind="sql",
        findings=check_sql(sql, index),
        checked_rules=len([r for r in index.kb.policies.values() if r.target == "sql"]),
    )


def govern_table(table: str, index: KnowledgeIndex) -> GovernanceReport:
    return GovernanceReport(
        target=table,
        target_kind="table",
        findings=check_table(table, index),
        checked_rules=1,
    )


def govern_metric(name: str, definition: str, index: KnowledgeIndex) -> GovernanceReport:
    return GovernanceReport(
        target=name,
        target_kind="metric",
        findings=check_metric(name, definition, index),
        checked_rules=1,
    )


# ── 治理助手 Agent（同一套循环里的第三个场景） ─────────────────────────────

from dataclasses import dataclass as _dc  # noqa: E402
from typing import Union as _Union  # noqa: E402

from .loop import AgentContext as _AgentContext  # noqa: E402
from .loop import AgentResult as _AgentResult  # noqa: E402
from .loop import Final as _Final  # noqa: E402
from .loop import Step as _Step  # noqa: E402
from .loop import ToolCall as _ToolCall  # noqa: E402
from .loop import run as _run  # noqa: E402
from .tools import ToolRegistry as _ToolRegistry  # noqa: E402


@_dc
class GovernancePlanner:
    """输入一段 SQL / 一张表 / 一个新指标，输出问题清单。"""

    def next_step(self, ctx: _AgentContext, steps: tuple["_Step", ...]) -> _Union[_ToolCall, _Final]:
        n = len(steps)
        if n == 0:
            if "sql" in ctx.extra:
                return _ToolCall("check_sql", {"sql": ctx.extra["sql"]})
            if "table" in ctx.extra:
                return _ToolCall("profile_table", {"table": ctx.extra["table"]})
            return _Final(
                kind="refuse",
                text="治理助手需要一段 SQL、一张表名或一个待注册指标作为输入。",
                data={"reason": "missing_target"},
            )

        if n == 1 and "table" in ctx.extra:
            return _ToolCall("check_table", {"table": ctx.extra["table"]})

        payload = steps[-1].result.payload
        table_profile = steps[0].result.payload if "table" in ctx.extra else None
        if "sql" in ctx.extra or "table" in ctx.extra:
            findings = payload.get("findings", [])
            counts = {"P0": 0, "P1": 0, "P2": 0}
            for f in findings:
                counts[f["severity"]] = counts.get(f["severity"], 0) + 1
            if not findings:
                text = "这段 SQL 未命中治理规则，可以提交。"
            else:
                lines = "\n".join(
                    f"- [{f['severity']}] {f['rule_id']} {f['rule_name']}：{f['evidence']}；"
                    f"建议：{f['fix']}（责任组 {f['owner']}）"
                    for f in findings
                )
                text = (
                    f"共命中 {len(findings)} 条（P0 {counts['P0']} / P1 {counts['P1']} / "
                    f"P2 {counts['P2']}）：\n{lines}"
                )
            return _Final(
                kind="answer",
                text=text,
                data={
                    "findings": findings,
                    "counts": counts,
                    "rule_ids": sorted({f["rule_id"] for f in findings}),
                    "profile": table_profile,
                },
                evidence=tuple(f["rule_id"] for f in findings),
            )

        return _Final(kind="error", text="治理助手没有可判定的目标")


def govern(target: str, registry: _ToolRegistry, kind: str = "sql", max_steps: int = 4) -> _AgentResult:
    extra = {"sql": target} if kind == "sql" else {"table": target}
    return _run(
        GovernancePlanner(),
        registry,
        _AgentContext(question=f"治理检查：{target}", extra=extra),
        max_steps,
    )
