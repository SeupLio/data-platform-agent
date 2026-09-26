"""变异测试：证明评测真的看得见缺陷。

这条测试是整套评测的「牙齿」—— 如果六个变异体里有一个跑完还是满分，
说明那一维的分数是装饰品。
"""

from __future__ import annotations

import pytest

from dataagent.eval.harness import run_eval
from dataagent.eval.metrics import DIMENSIONS
from dataagent.eval.mutants import MUTANTS, baseline_dims, run_mutations


@pytest.fixture(scope="module")
def baseline():
    scores, _ = run_eval(progress=False)
    return baseline_dims(scores)


@pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: m.id)
def test_mutant_is_caught(baseline, mutant):
    with mutant.patch():
        _, summary = run_eval(progress=False)
    deltas = {d: summary.dimension_means[d] - baseline[d] for d in DIMENSIONS}
    dropped = [d for d in DIMENSIONS if deltas[d] < -1e-9]
    assert dropped, (
        f"{mutant.id} 没被抓到：注入「{mutant.description}」之后 "
        f"五个维度一个都没掉分（{deltas}）"
    )


@pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: m.id)
def test_mutant_drops_the_dimension_it_should(baseline, mutant):
    """不只是「被抓到」，还要掉在**该掉的那一维**上。

    掉在不相干的维度上，说明维度定义串味了。
    """
    with mutant.patch():
        _, summary = run_eval(progress=False)
    deltas = {d: summary.dimension_means[d] - baseline[d] for d in DIMENSIONS}
    uncovered = [d for d in mutant.expected_dims if deltas[d] >= -1e-9]
    assert not uncovered, f"{mutant.id} 的 {uncovered} 没有掉分（{deltas}）"


def test_all_mutants_are_covered_by_the_suite(baseline):
    results = run_mutations(baseline, MUTANTS)
    assert len(results) == len(MUTANTS)
    assert all(r.caught for r in results), [r.mutant_id for r in results if not r.caught]
    assert not any(r.uncovered for r in results)
