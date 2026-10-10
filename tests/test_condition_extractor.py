"""
condition_extractor GWT 单测（2.0 上下文策略 1.6.9；含 replenish 旧 _resolve_scope 用例迁移）
运行：PYTHONPATH=. uv run python tests/test_condition_extractor.py
"""
import datetime

from app.agent.session.condition_extractor import (
    build_inherited_block,
    extract_categories,
    extract_conditions,
    resolve_categories,
)

TODAY = datetime.date(2026, 10, 8)   # 数据边界次日，与策略 1.5 示例一致


def test_category_basic():
    assert extract_categories("鞋类卖得怎么样") == ["鞋类"]
    assert extract_categories("服装类和配件类对比") == ["服装类", "配件类"]
    assert extract_categories("你好") == []


def test_conditions_six_domains():
    c = extract_conditions("国庆期间男子跑步鞋的销售额按降序", today=TODAY)
    assert c["时间"] == "2026-10-01 ~ 2026-10-07（国庆档）"
    assert c["人群"] == "男子" and c["系列"] == "跑步"
    assert c["指标"] == "销售额" and c["排序"] == "降序"


def test_time_relative_conversion():
    assert extract_conditions("昨天卖了多少钱", today=TODAY)["时间"] == "2026-10-07（昨天）"
    assert extract_conditions("本周数据", today=TODAY)["时间"] == "2026-10-05 ~ 2026-10-08（本周至今）"
    assert extract_conditions("20261005 的销量", today=TODAY)["时间"] == "2026-10-05"


# ==== replenish 旧 _resolve_scope 用例迁移（行为逐字一致）====

def test_scope_current_priority():
    """本轮明确提及 → 覆盖继承"""
    hist = [{"role": "user", "content": "看看鞋类的销售"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "服装类的补货计划"}]
    assert resolve_categories("服装类的补货计划", hist) == ["服装类"]


def test_scope_inheritance():
    """省略句追问 → 继承最近一轮；跳过与当前问句相同的末条（两种轨迹状态兼容）"""
    hist_with_current = [{"role": "user", "content": "看看鞋类的销售"},
                         {"role": "assistant", "content": "好的"},
                         {"role": "user", "content": "生成补货计划"}]   # 当前问句已入轨迹
    assert resolve_categories("生成补货计划", hist_with_current) == ["鞋类"]
    assert resolve_categories("生成补货计划", [{"messages": []}]) == []   # 无历史=全店
    assert resolve_categories("生成补货计划", []) == []


def test_scope_no_lookback_beyond_one_turn():
    """只看最近一轮用户消息——不把很久以前的限定词翻出来"""
    hist = [{"role": "user", "content": "鞋类销售"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "今天日期是什么"},
            {"role": "assistant", "content": "2026-10-08"},
            {"role": "user", "content": "生成补货计划"}]
    assert resolve_categories("生成补货计划", hist) == []


# ==== build_inherited_block ====

def test_block_inherits_only_missing_keys():
    """本轮已提到的条件不重复继承；本轮未提到的从上一轮继承"""
    hist = [{"role": "user", "content": "昨天鞋类的销售额"},
            {"role": "assistant", "content": "昨日鞋类销售额 12,345 元"}]
    block = build_inherited_block("那服装类呢", hist, today=TODAY)
    assert "品类" not in block                         # 本轮新条件不进继承块（已由本轮提供）
    assert "时间：2026-10-07（昨天）" in block          # 上一轮条件被继承
    assert "上一轮提问" in block and "昨天鞋类的销售额" in block


def test_block_empty_when_self_sufficient():
    """本轮自足（无条件可继承、无上一轮）→ 空串（缓存最友好形态）"""
    assert build_inherited_block("国庆7天卖了多少", [], today=TODAY) == ""


def test_block_no_history():
    assert build_inherited_block("那鞋类呢", [], today=TODAY) == ""


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in fns:
        fn()
        print(f"PASS {name}")
    print(f"ALL GWT OK ({len(fns)} cases)")
