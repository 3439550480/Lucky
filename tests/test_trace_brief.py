"""
trace_brief 与历史预算守卫 GWT 单测（2.0 上下文策略 1.6.16 / 1.6.17）
运行：PYTHONPATH=. uv run python tests/test_trace_brief.py
"""
from app.agent.session.trace_brief import build_brief
from app.agent.session.history_provider import _enforce_budget


def test_brief_aggregate():
    """Given 单行聚合结果 When 生成 Then 首行数值字段拼接"""
    brief = build_brief([{"销售额": 58432.0, "订单数": 137, "总件数": 312}])
    assert brief == "销售额 58,432 / 订单数 137 / 总件数 312"


def test_brief_ranking():
    """Given 20 行排行 When 生成 Then 行数前缀 + 首行（第一名）"""
    rows = [{"品名": f"商品{i}", "销量": 100 - i} for i in range(20)]
    brief = build_brief(rows)
    assert brief.startswith("20 行；首行 ")
    assert "销量 100" in brief


def test_brief_numeric_cap_and_bool_skip():
    """Given 数值字段超过 3 个且含 bool When 生成 Then 只取前 3 个数值、bool 被跳过"""
    brief = build_brief([{"a": 1, "b": 2, "c": 3, "d": 4, "e": True}])
    assert brief == "a 1 / b 2 / c 3"


def test_brief_text_fallback():
    """Given 纯文本行（无数值列）When 生成 Then 前 2 字段原值，超长截断"""
    brief = build_brief([{"品名": "超长商品名称一二三四五六七八九十甲乙丙丁戊己", "尺码": "42", "状态": "断码"}])
    assert brief == "品名 超长商品名称一二三四五六七八九十甲乙丙丁… / 尺码 42"


def test_brief_empty():
    """Given 空结果 When 生成 Then 无数据"""
    assert build_brief([]) == "无数据"


def test_brief_row_count_irrelevant():
    """Given 5000 行 vs 3 行 When 生成 Then 首行摘要部分一致（O(首行字段数)，与总行数无关）"""
    big = [{"销售额": 1.0}] * 5000
    small = [{"销售额": 1.0}] * 3
    assert build_brief(big).split("；首行 ")[1] == build_brief(small).split("；首行 ")[1]
    assert build_brief(big).startswith("5000 行")


def test_budget_trims_oldest():
    """Given 历史超预算 When 守卫 Then 从最旧整条丢弃 + 保留最近语境"""
    msgs = [{"role": "user", "content": "旧" * 600},      # ~600 tok
            {"role": "assistant", "content": "旧答" * 600},
            {"role": "user", "content": "新问题"}]
    out = _enforce_budget(msgs, budget=1000)
    assert out[0]["content"] == "新问题"                   # 最旧两条被丢
    assert len(out) == 1


def test_budget_within_noop():
    """Given 历史未超预算 When 守卫 Then 原样返回（追加式缓存不受影响）"""
    msgs = [{"role": "user", "content": "短问题"}]
    assert _enforce_budget(msgs, budget=1000) is msgs


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in fns:
        fn()
        print(f"PASS {name}")
    print(f"ALL GWT OK ({len(fns)} cases)")
