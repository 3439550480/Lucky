"""入口清残留 GWT 单测（2.0 上下文策略 1.6.14 附带发现 / final-verify 修复）

背景：checkpointer 会恢复上一轮的 State。凡"一次性输入/一次性标记"必须在
QueryService.query 组装入口 state 时显式归零，否则会在后续每一轮里复发：
- confirm_decision：上一轮点过的"确认"会反复触发确认门（S3 已修）
- permission_denied / denied_permission：上一轮的权限拒绝会让 route_after_guard
  把本轮直接短路到权限拒绝终点，能力链路完全不进（final-verify 抓出：
  TEMP 先试"入库…"被拒 → 同会话再问"生成鞋类的补货计划"，被上一轮的
  「出入库登记」拒绝文案劫持；新会话单发则正确判为「门店补货建议」）

做法：用替身图捕获 query() 真正交给 LangGraph 的 input state，直接断言入口值——
不依赖真实图、不依赖数据库与外部服务。
运行：PYTHONPATH=. uv run python tests/test_entry_state_reset.py
"""
import asyncio
from unittest.mock import patch

from app.services.query_service import QueryService


class _CapturingGraph:
    """graph.astream 替身：只记录入口 state，不执行任何节点"""

    def __init__(self):
        self.captured: dict | None = None

    async def astream(self, input=None, context=None, config=None, stream_mode=None):
        self.captured = input
        return
        yield  # pragma: no cover —— 让本方法成为异步生成器（query 用 async for 消费）


def _capture_entry_state(**query_kwargs) -> dict:
    """驱动一次 QueryService.query，返回它组装出的入口 state"""
    fake_graph = _CapturingGraph()
    service = QueryService(
        meta_mysql_repository=None,
        embedding_client=None,
        dw_mysql_repository=None,
        column_qdrant_repository=None,
        metric_qdrant_repository=None,
        value_es_repository=None,
    )

    async def _drive():
        with patch("app.services.query_service.graph", fake_graph), \
                patch("app.services.query_service.create_llm", lambda *a, **k: None):
            async for _ in service.query("生成鞋类的补货计划", "t-entry-reset", **query_kwargs):
                pass

    asyncio.run(_drive())
    assert fake_graph.captured is not None, "替身图未收到入口 state"
    return fake_graph.captured


def test_permission_flags_reset_per_turn():
    """一次性路由标记必须每轮归零：否则上一轮的拒绝会劫持本轮"""
    state = _capture_entry_state()
    assert state["permission_denied"] is False
    assert state["denied_permission"] == ""


def test_confirm_decision_reset_per_turn():
    """确认门的可信决定同样是一次性输入：普通对话轮必须是空串"""
    state = _capture_entry_state()
    assert state["confirm_decision"] == ""
    # 按钮通道传入时必须带值（回归：别把归零写成常量覆盖）
    assert _capture_entry_state(confirm_decision="confirm")["confirm_decision"] == "confirm"


def test_other_routing_fields_reset_per_turn():
    """路由/结果字段同口径归零；pending_action 属流程状态不在此列（须跨轮存活）"""
    state = _capture_entry_state()
    for field in ("capability", "capability_source", "intent", "intent_reply", "error", "sql"):
        assert state[field] == "", f"{field} 未归零"
    assert state["result_sample"] == [] and state["result_total"] == 0
    assert state["tool_calls"] == [] and state["retry_count"] == 0


if __name__ == "__main__":
    test_permission_flags_reset_per_turn()
    test_confirm_decision_reset_per_turn()
    test_other_routing_fields_reset_per_turn()
    print("入口清残留 GWT：3/3 通过")
