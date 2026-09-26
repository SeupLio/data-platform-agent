"""重新生成评测用例集。

    python scripts/gen_cases.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataagent.eval.cases import counts_by_kind, write_cases  # noqa: E402


def main() -> int:
    path = write_cases()
    counts = counts_by_kind()
    print(f"已生成用例集：{path.relative_to(ROOT)}")
    total = 0
    for k, v in counts.items():
        print(f"  {k}: {v}")
        total += v
    print(f"  合计: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
