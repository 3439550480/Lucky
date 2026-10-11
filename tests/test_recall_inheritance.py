"""
召回继承 GWT 单测（P0-3：extract_keywords 把上一轮条件词并入召回种子）
运行：PYTHONPATH=. uv run python tests/test_recall_inheritance.py
"""
import asyncio
from types import SimpleNamespace

from app.agent.nodes.dataquery.extract_keywords import extract_keywords


def _runtime():
    """替身 Runtime：stream_writer 只吞事件；staff=None（评测同款无身份路径）。
    状态保真依据：get_trajectory 直读 state["messages"]，节点内 append_user_message
    只改返回值不动原 state——替身 state 与真实图内 state 行为一致，无需 mock。"""
    return SimpleNamespace(stream_writer=lambda evt: None, context={"staff": None})


async def _run(query: str, history: list[dict]):
    state = {"query": query, "capability": "", "messages": list(history)}
    result = await extract_keywords(state, _runtime())
    return result["keywords"], result["messages"]


def test_ellipsis_inherits_metric():
    """省略句"那只看鞋类呢" ← 上一轮"销售额最高的商品是哪个"：指标词进召回种子。
    （mt_category_drill 实测根因：召回层看不见继承条件 → fact_sales 被过滤 → SQL 跑偏）"""
    hist = [{"role": "user", "content": "销售额最高的商品是哪个"},
            {"role": "assistant", "content": "冠军是…"}]
    keywords, _ = asyncio.run(_run("那只看鞋类呢", hist))
    assert "销售额" in keywords                     # 继承的指标词（extract_conditions 实证产出）
    assert any("鞋类" in k for k in keywords)       # 本轮品类（jieba 抽出或 query 原句兜底）


def test_time_not_inherited_into_recall():
    """时间条件不进召回种子（已由 date_info/继承块承载，策略约定排除键）"""
    hist = [{"role": "user", "content": "国庆期间鞋类的销售额"},
            {"role": "assistant", "content": "…"}]
    keywords, _ = asyncio.run(_run("那只看服装类呢", hist))
    assert "销售额" in keywords                     # 指标继承
    assert "2026-10-01 ~ 2026-10-07（国庆档）" not in keywords   # 时间被排除


def test_no_history_no_crash():
    """无历史（首轮）→ 不继承也不报错，写入轨迹正常"""
    keywords, messages = asyncio.run(_run("国庆卖了多少", []))
    assert messages and messages[-1]["content"] == "国庆卖了多少"
    assert any("国庆" in k for k in keywords)


def test_ellipsis_skips_current_entry():
    """当前问句已在轨迹末条的特殊轨迹状态（replenish 直连入口形态）→ 仍取到真正的上一轮。
    注：上一轮若不含六域条件（如"哪些 SKU 库存紧张"），继承为空是正确行为——
    六域条件抽取器不产话题词，话题接续由 P2 路由层解决，召回层不硬塞噪声"""
    hist = [{"role": "user", "content": "国庆期间鞋类的销售额"},
            {"role": "assistant", "content": "国庆档鞋类销售额…"},
            {"role": "user", "content": "那服装类呢"}]   # 当前问句已在轨迹（同文条目被跳过）
    keywords, _ = asyncio.run(_run("那服装类呢", hist))
    assert "销售额" in keywords                       # 继承自真正的上一轮（而非末条同文）


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in fns:
        fn()
        print(f"PASS {name}")
    print(f"ALL GWT OK ({len(fns)} cases)")
