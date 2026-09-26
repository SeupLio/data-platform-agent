"""命令行入口。

    python -m dataagent.cli kb stats
    python -m dataagent.cli ask "最近7天播放量是多少"
    python -m dataagent.cli analyze "2026-09-04 到 2026-09-10 播放量为什么降了"
    python -m dataagent.cli govern --sql "SELECT * FROM dws_video_daily"
    python -m dataagent.cli eval
    python -m dataagent.cli mutate
    python -m dataagent.cli studio
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DOCS_REPORTS = Path(__file__).resolve().parents[2] / "docs" / "reports"


def _ctx():
    from .knowledge.index import load_index
    from .warehouse.engine import Warehouse

    index = load_index()
    return index, Warehouse(index=index)


def _registry(index, wh):
    from .agents.tools import ToolRegistry

    return ToolRegistry(index, wh)


def cmd_kb(args: argparse.Namespace) -> int:
    from .knowledge.build import BUILT_DIR, build_all, dump_yaml, load_payload, write_built

    if args.action == "build":
        payload = build_all()
        for p in write_built(payload):
            print(f"  -> {p}")
        print(json.dumps(payload["meta"]["counts"], ensure_ascii=False, indent=2))
        return 0
    if args.action == "check":
        payload = build_all()
        current = load_payload(BUILT_DIR)
        stale = [k for k in payload if dump_yaml(payload[k]) != dump_yaml(current[k])]
        if stale:
            print(f"STALE: {stale}")
            return 1
        print("FRESH")
        return 0
    index, _ = _ctx()
    print(json.dumps(index.kb.summary(), ensure_ascii=False, indent=2))
    print(f"易混指标对：{len(index.kb.confusable_pairs)}")
    dup = index.duplicated_aliases()
    print(f"重复别名：{dup if dup else '无'}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from .warehouse.seed import DEFAULT_DB, generate

    s = generate(args.path or DEFAULT_DB)
    print(json.dumps(s.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from .agents import ask

    index, wh = _ctx()
    res = ask(args.question, _registry(index, wh), wh.today)
    print(res.text)
    if args.json:
        print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
    return 0 if res.kind == "answer" else 1


def cmd_analyze(args: argparse.Namespace) -> int:
    from .agents import analyze

    index, wh = _ctx()
    res = analyze(args.question, _registry(index, wh), wh.today, max_steps=8)
    print(res.text)
    if args.json:
        print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
    return 0 if res.kind == "answer" else 1


def cmd_govern(args: argparse.Namespace) -> int:
    from .agents import govern

    index, wh = _ctx()
    target = args.sql or args.table or ""
    kind = "sql" if args.sql else "table"
    res = govern(target, _registry(index, wh), kind=kind)
    print(res.text)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .eval.harness import run_eval
    from .eval.report import render_html, render_json, render_markdown

    index, wh = _ctx()
    scores, summary = run_eval(index=index, wh=wh, checkpoint=args.checkpoint, resume=args.resume)
    print(f"用例 {summary.total} 条 · 综合 {summary.overall:.4f} · {summary.dimension_line()}")
    for kind, info in summary.by_kind.items():
        print(f"  {kind}: {info['count']} 条 · {info['overall']:.4f}")
    if summary.failures:
        print(f"  未满分 {len(summary.failures)} 条：")
        for f in summary.failures[:10]:
            print(f"    - {f['case_id']} ({f['kind']}) {f['overall']:.3f} {f['scores']}")

    DOCS_REPORTS.mkdir(parents=True, exist_ok=True)
    (DOCS_REPORTS / "eval_report.md").write_text(render_markdown(summary), encoding="utf-8")
    (DOCS_REPORTS / "eval_report.html").write_text(render_html(summary), encoding="utf-8")
    (DOCS_REPORTS / "eval_report.json").write_text(render_json(summary), encoding="utf-8")
    print(f"  报告已写入 {DOCS_REPORTS}")
    return 0 if not summary.failures else 1


def cmd_mutate(args: argparse.Namespace) -> int:
    from .eval.harness import run_eval
    from .eval.mutants import MUTANTS, baseline_dims, run_mutations
    from .eval.report import render_markdown

    index, wh = _ctx()
    scores, summary = run_eval(index=index, wh=wh, progress=False)
    base = baseline_dims(scores)
    print(f"基线：{summary.dimension_line()}")
    results = run_mutations(base, MUTANTS, index=index, wh=wh)
    uncovered_expected: list[str] = []
    for r in results:
        print(f"  {r.mutant_id}: {'被抓' if r.caught else '幸存'}  " + " ".join(
            f"{d}={r.deltas[d]:+.3f}" for d in r.deltas
        ))
        if r.uncovered:
            uncovered_expected.append(f"{r.mutant_id} 的 {'、'.join(r.uncovered)} 没掉")
    DOCS_REPORTS.mkdir(parents=True, exist_ok=True)
    (DOCS_REPORTS / "mutation_report.md").write_text(
        render_markdown(summary, results), encoding="utf-8"
    )
    if uncovered_expected:
        print("预期维度未掉分：" + "；".join(uncovered_expected))
    return 0 if all(r.caught for r in results) else 1


def cmd_studio(args: argparse.Namespace) -> int:
    from .studio import serve

    return serve(port=args.port, open_browser=not args.no_open)


def cmd_selftest(args: argparse.Namespace) -> int:
    """把 README 里的关键数字重算一遍并打印 —— 用来核对文档有没有过期。"""
    from .eval.cases import counts_by_kind
    from .eval.harness import run_eval
    from .eval.mutants import MUTANTS, baseline_dims, run_mutations
    from .knowledge.build import build_all

    payload = build_all()
    print("KB:", json.dumps(payload["meta"]["counts"], ensure_ascii=False))
    print("CASES:", json.dumps(counts_by_kind(), ensure_ascii=False))
    index, wh = _ctx()
    scores, summary = run_eval(index=index, wh=wh, progress=False)
    print("EVAL:", summary.total, summary.overall, json.dumps(summary.dimension_means, ensure_ascii=False))
    results = run_mutations(baseline_dims(scores), MUTANTS, index=index, wh=wh)
    print("MUTANTS_CAUGHT:", sum(1 for r in results if r.caught), "/", len(results))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dataagent", description="数据平台 Agent 产品原型")
    sub = p.add_subparsers(dest="command", required=True)

    kb = sub.add_parser("kb", help="知识库：拆解 / 校验 / 统计")
    kb.add_argument("action", choices=["build", "check", "stats"], nargs="?", default="stats")
    kb.set_defaults(func=cmd_kb)

    seed = sub.add_parser("seed", help="重建离线数仓")
    seed.add_argument("--path", default=None)
    seed.set_defaults(func=cmd_seed)

    ask = sub.add_parser("ask", help="数据智能问答")
    ask.add_argument("question")
    ask.add_argument("--json", action="store_true")
    ask.set_defaults(func=cmd_ask)

    ana = sub.add_parser("analyze", help="自动化数据分析")
    ana.add_argument("question")
    ana.add_argument("--json", action="store_true")
    ana.set_defaults(func=cmd_analyze)

    gov = sub.add_parser("govern", help="数据治理助手")
    gov.add_argument("--sql", default=None)
    gov.add_argument("--table", default=None)
    gov.set_defaults(func=cmd_govern)

    ev = sub.add_parser("eval", help="跑评测")
    ev.add_argument("--checkpoint", default=None)
    ev.add_argument("--resume", action="store_true")
    ev.set_defaults(func=cmd_eval)

    mu = sub.add_parser("mutate", help="变异测试")
    mu.set_defaults(func=cmd_mutate)

    st = sub.add_parser("studio", help="本地自测台（网页）")
    st.add_argument("--port", type=int, default=8765)
    st.add_argument("--no-open", action="store_true")
    st.set_defaults(func=cmd_studio)

    sel = sub.add_parser("selftest", help="重算 README 里的关键数字")
    sel.set_defaults(func=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
