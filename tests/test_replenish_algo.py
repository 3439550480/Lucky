"""
replenish 固化算法 GWT 单测（2.0 S3b）
无 pytest 依赖，断言直跑：
  uv run python tests/test_replenish_algo.py
或 pytest：
  uv run pytest tests/test_replenish_algo.py -q
"""
from app.agent.nodes.replenish.replenish_plan import _resolve_scope, compute_replenish

# 与 dim_replenish_policy（store 行）一致：鞋 7/服 3/配 14，安全统一 3 天
POLICY = {
    "鞋类": {"coverage_days": 7, "safety_days": 3},
    "服装类": {"coverage_days": 3, "safety_days": 3},
    "配件类": {"coverage_days": 14, "safety_days": 3},
}


def test_gap_basic():
    """Given 日均10/可用40/鞋类(7+3) When 计算 Then 目标100、建议60、倒计时4.0天"""
    plan = compute_replenish(
        daily_sales={"S1": 10.0},
        available={"S1": 40},
        policy=POLICY,
        sku_meta={"S1": {"product_name": "跑鞋", "category_l1": "鞋类"}},
    )
    assert len(plan) == 1
    row = plan[0]
    assert row["target_qty"] == 100        # ceil(10 × (7+3))
    assert row["suggest_qty"] == 60        # 100 − 40 − 0
    assert row["days_left"] == 4.0         # 40 / 10


def test_no_gap_clamped_to_zero():
    """Given 库存充足 When 计算 Then 无缺口行（不输出建议量≤0 的行）"""
    plan = compute_replenish(
        daily_sales={"S1": 2.0},
        available={"S1": 999},
        policy=POLICY,
        sku_meta={"S1": {"product_name": "T恤", "category_l1": "服装类"}},
    )
    assert plan == []


def test_ceil_rounding():
    """Given 日均3.3/可用5/鞋类 When 计算 Then 目标33（向上取整）、建议28——取整规则钉死"""
    plan = compute_replenish(
        daily_sales={"S1": 3.3},
        available={"S1": 5},
        policy=POLICY,
        sku_meta={"S1": {"product_name": "跑鞋", "category_l1": "鞋类"}},
    )
    assert plan[0]["target_qty"] == 33
    assert plan[0]["suggest_qty"] == 28


def test_on_transit_subtracted():
    """Given 在途20 When 计算 Then 建议量再减在途（100−40−20=40）——warehouse 预留减项生效"""
    plan = compute_replenish(
        daily_sales={"S1": 10.0},
        available={"S1": 40},
        policy=POLICY,
        sku_meta={"S1": {"product_name": "跑鞋", "category_l1": "鞋类"}},
        on_transit={"S1": 20},
    )
    assert plan[0]["suggest_qty"] == 40


def test_category_policy_mapping_and_sorting():
    """Given 两品类不同策略 When 计算 Then 各按本品类策略取参，且断货倒计时小者靠前"""
    plan = compute_replenish(
        daily_sales={"S_XIE": 10.0, "S_FU": 10.0},
        available={"S_XIE": 40, "S_FU": 10},
        policy=POLICY,
        sku_meta={
            "S_XIE": {"product_name": "跑鞋", "category_l1": "鞋类"},    # 目标100→60件，倒计时4.0
            "S_FU": {"product_name": "卫衣", "category_l1": "服装类"},   # 目标60→50件，倒计时1.0
        },
    )
    assert [r["sku_id"] for r in plan] == ["S_FU", "S_XIE"]
    assert plan[0]["coverage_days"] == 3 and plan[1]["coverage_days"] == 7


def test_no_policy_category_skipped():
    """Given 无策略品类 When 计算 Then 不生成建议（宁缺勿错，不让模型补口径）"""
    plan = compute_replenish(
        daily_sales={"S1": 10.0},
        available={"S1": 0},
        policy=POLICY,
        sku_meta={"S1": {"product_name": "袜子", "category_l1": "袜类"}},
    )
    assert plan == []


def test_zero_sales_no_row():
    """Given 无销量 When 计算 Then 目标0→建议0→不输出（新 SKU 不靠此规则铺货）"""
    plan = compute_replenish(
        daily_sales={},
        available={"S1": 5},
        policy=POLICY,
        sku_meta={"S1": {"product_name": "跑鞋", "category_l1": "鞋类"}},
    )
    assert plan == []


def test_scope_inheritance():
    """Given 省略句追问 When 解析范围 Then 继承最近一轮用户消息的品类；本轮明确提及则覆盖"""
    state = {"messages": [
        {"role": "user", "content": "看看鞋类的销售"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "生成补货计划"},
    ]}
    assert _resolve_scope("生成补货计划", state) == ["鞋类"]      # 继承
    assert _resolve_scope("服装类的补货计划", state) == ["服装类"]  # 本轮优先
    assert _resolve_scope("生成补货计划", {"messages": []}) == []  # 无历史=全店


if __name__ == "__main__":
    fns = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in fns:
        fn()
        print(f"PASS {name}")
    print(f"ALL GWT OK ({len(fns)} cases)")
