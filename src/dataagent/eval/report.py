"""评测报告渲染（JSON / Markdown / HTML）。

报告是**给人做决策用的**：先给结论，再给证据，最后给「这份报告没覆盖什么」。
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any, Optional, Sequence

from .metrics import DIMENSIONS, DIMENSION_LABELS
from .mutants import MutationResult

CSS = """
body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;background:#f7f8fa;color:#222;margin:0;padding:32px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:17px;margin:28px 0 10px;border-left:4px solid #fb7299;padding-left:8px}
.sub{color:#666;font-size:13px;margin-bottom:20px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid #e5e6eb;padding:6px 10px;text-align:left}
th{background:#f2f3f5;font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.good{color:#1a7f37}
.bad{color:#c1121f}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}
.card{background:#fff;border:1px solid #e5e6eb;border-radius:8px;padding:12px 16px;min-width:150px}
.card .k{font-size:12px;color:#888}
.card .v{font-size:22px;font-weight:600}
.note{background:#fff8e6;border:1px solid #ffe58f;border-radius:6px;padding:10px 14px;font-size:13px}
code{background:#f2f3f5;padding:1px 5px;border-radius:3px;font-size:12px}
ul{font-size:13px;line-height:1.7}
"""


def render_json(summary: Any, mutations: Sequence[MutationResult] = ()) -> str:
    payload = summary.to_dict()
    payload["mutations"] = [
        {
            "mutant_id": m.mutant_id,
            "description": m.description,
            "caught": m.caught,
            "deltas": m.deltas,
            "expected_dims": list(m.expected_dims),
            "uncovered": list(m.uncovered),
        }
        for m in mutations
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_markdown(summary: Any, mutations: Sequence[MutationResult] = ()) -> str:
    lines: list[str] = []
    lines.append("# 数据平台 Agent 评测报告")
    lines.append("")
    lines.append(
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}"
        f"（离线可复现，不依赖模型调用）"
    )
    lines.append(f"- 用例数：**{summary.total}**")
    lines.append(f"- 综合得分：**{summary.overall:.4f}**")
    lines.append("")
    lines.append("## 五维得分")
    lines.append("")
    lines.append("| 维度 | 得分 |")
    lines.append("|---|---|")
    for d in DIMENSIONS:
        lines.append(f"| {DIMENSION_LABELS[d]}（{d}） | {summary.dimension_means[d]:.4f} |")
    lines.append("")
    lines.append("## 分场景")
    lines.append("")
    lines.append("| 用例类型 | 条数 | 综合得分 |")
    lines.append("|---|---|---|")
    for kind, info in summary.by_kind.items():
        lines.append(f"| {kind} | {info['count']} | {info['overall']:.4f} |")
    lines.append("")
    if summary.failures:
        lines.append(f"## 未满分用例（{len(summary.failures)} 条）")
        lines.append("")
        lines.append("| 用例 | 类型 | 综合 | 明细 |")
        lines.append("|---|---|---|---|")
        for f in summary.failures[:30]:
            dims = " ".join(f"{k}={v}" for k, v in f["scores"].items())
            lines.append(f"| {f['case_id']} | {f['kind']} | {f['overall']:.3f} | {dims} |")
        lines.append("")
    if mutations:
        lines.append("## 变异测试")
        lines.append("")
        lines.append("| 变异体 | 注入的缺陷 | 结果 | " + " | ".join(DIMENSIONS) + " |")
        lines.append("|---|---|---|" + "---|" * len(DIMENSIONS))
        for m in mutations:
            lines.append(m.as_row())
        lines.append("")
    return "\n".join(lines)


def _fmt_delta(v: float) -> str:
    cls = "bad" if v < 0 else ("good" if v > 0 else "")
    return f'<span class="{cls}">{v:+.3f}</span>' if cls else f"{v:+.3f}"


def render_html(summary: Any, mutations: Sequence[MutationResult] = (),
                meta: Optional[dict[str, Any]] = None) -> str:
    meta = meta or {}
    e = html.escape
    parts: list[str] = []
    parts.append("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>")
    parts.append("<title>数据平台 Agent 评测报告</title>")
    parts.append(f"<style>{CSS}</style></head><body><div class='wrap'>")
    parts.append("<h1>数据平台 Agent 评测报告</h1>")
    parts.append(
        f"<div class='sub'>生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
        f"离线可复现（0 次模型调用） · 用例 {summary.total} 条</div>"
    )

    parts.append("<div class='cards'>")
    parts.append(
        f"<div class='card'><div class='k'>综合得分</div><div class='v'>{summary.overall:.4f}</div></div>"
    )
    for d in DIMENSIONS:
        parts.append(
            f"<div class='card'><div class='k'>{e(DIMENSION_LABELS[d])}</div>"
            f"<div class='v'>{summary.dimension_means[d]:.3f}</div></div>"
        )
    parts.append("</div>")

    parts.append("<h2>分场景得分</h2><table><tr><th>用例类型</th><th>条数</th><th>综合</th>"
                 + "".join(f"<th>{e(DIMENSION_LABELS[d])}</th>" for d in DIMENSIONS) + "</tr>")
    for kind, info in summary.by_kind.items():
        parts.append(
            f"<tr><td>{e(kind)}</td><td class='num'>{info['count']}</td>"
            f"<td class='num'>{info['overall']:.4f}</td>"
            + "".join(f"<td class='num'>{info['dimensions'][d]:.3f}</td>" for d in DIMENSIONS)
            + "</tr>"
        )
    parts.append("</table>")

    if mutations:
        parts.append("<h2>变异测试（注入的缺陷有没有被评测看见）</h2>")
        parts.append(
            "<table><tr><th>变异体</th><th>注入的缺陷</th><th>结果</th>"
            + "".join(f"<th>{e(DIMENSION_LABELS[d])} Δ</th>" for d in DIMENSIONS)
            + "<th>预期维度是否命中</th></tr>"
        )
        for m in mutations:
            flag = "<span class='good'>被抓</span>" if m.caught else "<span class='bad'>幸存</span>"
            ok = "是" if not m.uncovered else f"否（{e('、'.join(m.uncovered))} 没掉）"
            parts.append(
                f"<tr><td>{e(m.mutant_id)}</td><td>{e(m.description)}</td><td>{flag}</td>"
                + "".join(f"<td class='num'>{_fmt_delta(m.deltas[d])}</td>" for d in DIMENSIONS)
                + f"<td>{ok}</td></tr>"
            )
        parts.append("</table>")

    if summary.failures:
        parts.append(f"<h2>未满分用例（{len(summary.failures)} 条）</h2>")
        parts.append("<table><tr><th>用例</th><th>类型</th><th>场景</th><th>综合</th>"
                     + "".join(f"<th>{e(DIMENSION_LABELS[d])}</th>" for d in DIMENSIONS)
                     + "<th>关键信息</th></tr>")
        for f in summary.failures[:60]:
            info = f.get("detail", {})
            keys = {k: info.get(k) for k in ("expected_kind", "got_kind", "expected_rules", "predicted_rules", "oracle_value", "actual_value") if k in info}
            parts.append(
                f"<tr><td>{e(str(f['case_id']))}</td><td>{e(str(f['kind']))}</td>"
                f"<td>{e(str(f['scenario']))}</td><td class='num'>{f['overall']:.3f}</td>"
                + "".join(f"<td class='num'>{f['scores'][d]:.2f}</td>" for d in DIMENSIONS)
                + f"<td>{e(json.dumps(keys, ensure_ascii=False))}</td></tr>"
            )
        parts.append("</table>")

    notes = meta.get("notes") or [
        "本报告的每一个数字都是离线跑出来的，不依赖任何模型调用；换机器 clone 后重跑应完全一致。",
        "评测的期望值由独立 Oracle 计算（自建 SQL），不复用被测的查询编译路径。",
        "「用户体验」「场景适配度」两维是结构化判分，不是模型主观打分，所以没有裁判噪声。",
    ]
    parts.append("<h2>这份报告没覆盖什么</h2><ul>")
    for n in notes:
        parts.append(f"<li>{e(n)}</li>")
    parts.append("</ul>")

    parts.append("</div></body></html>")
    return "".join(parts)
