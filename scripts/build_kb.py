"""重新生成结构化知识库。

    python scripts/build_kb.py            # 重新拆解并落盘
    python scripts/build_kb.py --check    # 只校验产物是否最新（CI / 护栏用）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataagent.knowledge.build import (  # noqa: E402
    BUILT_DIR,
    build_all,
    dump_yaml,
    load_payload,
)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    payload = build_all()
    if check:
        current = load_payload(BUILT_DIR)
        stale = [k for k in payload if dump_yaml(payload[k]) != dump_yaml(current[k])]
        if stale:
            print(f"STALE: {stale} —— 请运行 python scripts/build_kb.py")
            return 1
        print("FRESH: 结构化知识库与原始语料一致")
        return 0

    written = []
    for key in ("metrics", "models", "policies", "chunks", "confusable_pairs", "meta"):
        path = BUILT_DIR / f"{key}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_yaml(payload[key]), encoding="utf-8")
        written.append(path)
    counts = payload["meta"]["counts"]
    print("已生成结构化知识库：")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    for p in written:
        print(f"  -> {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
