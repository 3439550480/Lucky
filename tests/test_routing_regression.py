"""
路由回归单测（P1-1：正则改动的前置纪律——没有它，改 capability_config.yaml 就是盲改）
运行：PYTHONPATH=. uv run python tests/test_routing_regression.py

口径：只测 tier1（registry.match_rules，0 token、确定性、可离线）。
tier2（embedding）需向量服务，tier3（LLM）需网络——都不入单测。
基线（2026-10-11，P1-2 正则修复后实测）：tier1 正确 12 例、误抢 0 例。
改正则后：正确集只许扩大（⊇ 基线）、误抢集只许缩小（⊆ 基线）；
若有意提升基线，跑本脚本看打印的当前集合并更新两个常量。
"""
import json
from pathlib import Path

from app.agent.capabilities.registry import registry

DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "datasets" / "eval_anta.json"

# 基线（2026-10-11，P1-2 正则修复后实测上调）：正确 12 例、误抢 0 例
BASELINE_CORRECT = frozenset({
    "anta_qty_by_series", "anta_inv_available_now", "anta_inv_broken_size",
    "anta_inv_loss", "anta_inv_flow_net", "anta_replenish_shoes", "anta_replenish_apparel",
    "anta_attr_clearance_sales", "anta_holiday_days", "anta_mt_coref_qty",
    "anta_qty_by_category", "anta_top_shoe_sales",
})
BASELINE_MISFIRES = frozenset()


def _tier1_split():
    """逐例跑 tier1 并三分：命中且正确 / 命中但错（误抢，最危险）/ 未命中（落 tier2/3，不算错）"""
    ds = json.loads(DATASET.read_text(encoding="utf-8"))
    correct, misfires, missed = set(), set(), set()
    for case in ds["cases"]:
        if not case.get("enabled", True):
            continue
        expected = (case.get("expected") or {}).get("intent")
        hit = registry.match_rules(case["query"])
        if hit == expected:
            correct.add(case["id"])
        elif hit is None:
            missed.add(case["id"])
        else:
            misfires.add(case["id"])
    return correct, misfires, missed


def test_tier1_no_regression():
    correct, _, _ = _tier1_split()
    assert BASELINE_CORRECT <= correct, f"tier1 正确集回退: {BASELINE_CORRECT - correct}"


def test_tier1_no_new_misfire():
    _, misfires, _ = _tier1_split()
    assert misfires <= BASELINE_MISFIRES, f"新增确定性误抢: {misfires - BASELINE_MISFIRES}"


if __name__ == "__main__":
    correct, misfires, missed = _tier1_split()
    print(f"tier1 正确 {len(correct)} / 误抢 {len(misfires)} / 未命中落下层 {len(missed)}")
    print("正确集:", sorted(correct))
    print("误抢集:", sorted(misfires))
    test_tier1_no_regression()
    test_tier1_no_new_misfire()
    print("PASS test_tier1_no_regression")
    print("PASS test_tier1_no_new_misfire")
    print("ALL GWT OK (2 cases)")
