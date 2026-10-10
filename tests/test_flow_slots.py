"""
出入库槽位 GWT 单测（2.0 上下文策略 3.2/3.5/3.6）
- 纯函数：槽位解析 / 取消确认判定 / 写意图窄规则 / 超时
- [S3 结构化确认] 条件边与确认门：按钮决定（confirm_decision）优先，自由文本退为兜底
运行：PYTHONPATH=. uv run python tests/test_flow_slots.py
"""
import asyncio
import time

from app.agent.nodes.inventory_write.slots import (
    detect_write_intent,
    flow_expired,
    is_cancel,
    is_confirm,
    parse_direction,
    parse_doc_no,
    parse_qty,
    parse_sku,
)


def test_parse_direction():
    assert parse_direction("入库 RK2026101001 ... 20") == "in"
    assert parse_direction("收货 RK1") == "in"
    assert parse_direction("出库 5 件") == "out"
    assert parse_direction("昨天卖了多少") is None


def test_parse_doc_no():
    assert parse_doc_no("入库 rk2026101001 15262011-01-42 20") == "RK2026101001"
    assert parse_doc_no("出库 XS2026100201 20 件") == "XS2026100201"
    assert parse_doc_no("入库 20 件") is None


def test_parse_sku():
    assert parse_sku("入库 RK1 15262011-01-42 20") == "15262011-01-42"
    assert parse_sku("氢跑8 的 42 码") is None          # v1 不做模糊匹配


def test_parse_qty():
    assert parse_qty("入库 RK2026101001 15262011-01-42 20") == 20
    assert parse_qty("入库 RK1 ... 35 件") == 35
    assert parse_qty("入库 20") == 20                    # 剔除单据号后独立整数
    assert parse_qty("入库 RK2026101001") is None        # 无独立数量（不能拿单据号当数量）
    assert parse_qty("入库 RK1 15262011-01-42 0") is None  # 零件数无效


def test_cancel_confirm():
    assert is_cancel("算了不弄了") and is_cancel("取消")
    assert not is_cancel("确认提交")
    assert is_confirm("确认") and is_confirm("OK")
    assert not is_confirm("取消")
    # [2.0 P0 修复] 确认词严格化：子串误判（"对"）与否定/修正句一律不算确认
    assert is_confirm("确认吧") and is_confirm("好的") and is_confirm("是的")
    assert not is_confirm("不对")
    assert not is_confirm("对不上库存")
    assert not is_confirm("对，改成30件")
    assert not is_confirm("确认下昨天入库多少")
    assert not is_confirm("")


def test_write_intent_no_hijack():
    """窄规则：句首方向词才劫持——查询类语句不误入流程"""
    assert detect_write_intent("入库 RK2026101001 15262011-01-42 20")
    assert detect_write_intent("我要入库")
    assert detect_write_intent("登记出库 XS1 5 件")
    # 误劫持防线
    assert not detect_write_intent("昨天入库了多少件")
    assert not detect_write_intent("查一下出库流水")
    assert not detect_write_intent("国庆期间销售出库有多少")
    # [2.0 P0 修复] 裸方向词开头的问数句（原版命中 → 误劫持进槽位流程）
    assert not detect_write_intent("入库了多少件")
    assert not detect_write_intent("出库流水")
    assert not detect_write_intent("入库明细给我看看")
    assert not detect_write_intent("出库量是多少")
    assert not detect_write_intent("出库单号是多少")
    # 真写意图不受影响
    assert detect_write_intent("出库")
    assert detect_write_intent("入库了100件")
    assert detect_write_intent("出库单 RK2026101001 15262011-01-42 5")


def test_flow_expired():
    pending = {"started_at": time.time() - 700}
    assert flow_expired(pending)                      # >10 分钟
    assert not flow_expired({"started_at": time.time() - 60})
    assert not flow_expired({})                       # 无时间戳不误判


# ==================== [S3 结构化确认] 节点/条件边 ====================

def _awaiting_pending() -> dict:
    """四槽已齐、等待确认的流程状态（确认门的输入）"""
    return {"type": "inventory_write",
            "slots": {"direction": "out", "doc_no": "XS2026100201",
                      "sku_id": "15262011-01-42", "qty": 5},
            "started_at": time.time(), "awaiting_confirm": True}


class _FakeWriter:
    """stream_writer 替身：把节点写出的事件收进列表"""

    def __init__(self):
        self.events: list[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)


class _FakeRuntime:
    """flow_step 只用到 stream_writer / context 两个属性（其余仓储按需给）"""

    def __init__(self, write_repo=None):
        self._writer = _FakeWriter()
        self.context = {"inventory_write_repository": write_repo,
                        "dw_mysql_repository": None, "staff": None}

    @property
    def stream_writer(self):
        return self._writer


class _FakeWriteRepo:
    """出入库写仓储替身：commit_flow 只记账不落库，便于断言本轮是否真的写库"""

    def __init__(self):
        self.commits: list[dict] = []

    async def commit_flow(self, **kwargs):
        self.commits.append(kwargs)
        return {"available_after": 80, "flow_id": 1}


def _run_flow(state: dict, repo=None):
    """驱动一次 flow_step，返回 (状态更新, 写出的事件)"""
    from app.agent.nodes.inventory_write.flow_step import flow_step

    repo = repo or _FakeWriteRepo()
    runtime = _FakeRuntime(write_repo=repo)
    result = asyncio.run(flow_step(state, runtime))
    return result, runtime.stream_writer.events, repo


def test_route_after_guard_structured_decision():
    """[S3] 按钮决定直达 flow_step：含"无 pending"的失效兜底（必须有人收尾）"""
    from app.agent.graph import route_after_guard

    assert route_after_guard({"confirm_decision": "confirm",
                              "pending_action": _awaiting_pending()}) == "flow_step"
    assert route_after_guard({"confirm_decision": "cancel"}) == "flow_step"
    assert route_after_guard({"confirm_decision": "", "pending_action": _awaiting_pending()}) == "flow_step"
    assert route_after_guard({"confirm_decision": ""}) == "route_capability"
    # 权限拒绝优先级高于一切
    assert route_after_guard({"confirm_decision": "confirm", "permission_denied": True}) == "permission_denied"


def test_flow_step_structured_confirm_commits():
    """结构化确认：即使 query 是完全无关的文本，也照决定写库（不依赖任何文本解析）"""
    result, events, repo = _run_flow(
        {"query": "嗯，就这条", "pending_action": _awaiting_pending(),
         "confirm_decision": "confirm", "messages": []})

    assert len(repo.commits) == 1
    assert repo.commits[0]["qty"] == 5 and repo.commits[0]["direction"] == "out"
    assert result["pending_action"] is None
    assert result["confirm_decision"] == ""          # 决定一次性，用完归零
    assert any(e["type"] == "result" for e in events)


def test_flow_step_structured_cancel():
    """结构化取消：不写库、清流程、明确告知（且不回落到能力路由再生成一段回答）"""
    result, events, repo = _run_flow(
        {"query": "确认", "pending_action": _awaiting_pending(),
         "confirm_decision": "cancel", "messages": []})

    assert repo.commits == []
    assert result["pending_action"] is None and result["confirm_decision"] == ""
    assert events == [{"type": "explanation", "text": "已取消本次出入库操作。"}]


def test_flow_step_text_fallback_still_works():
    """降级场景（手打"确认"）：无结构化决定时文本兜底仍然提交"""
    result, _events, repo = _run_flow(
        {"query": "确认", "pending_action": _awaiting_pending(),
         "confirm_decision": "", "messages": []})

    assert len(repo.commits) == 1 and result["pending_action"] is None


def test_flow_step_reexposes_confirm_card():
    """未决定（既非按钮也非确认词）：推 confirm 事件让前端渲染按钮，绝不写库"""
    result, events, repo = _run_flow(
        {"query": "等一下我看看", "pending_action": _awaiting_pending(),
         "confirm_decision": "", "messages": []})

    assert repo.commits == []
    confirm_events = [e for e in events if e["type"] == "confirm"]
    assert len(confirm_events) == 1
    assert confirm_events[0]["action"] == "inventory_write"
    assert confirm_events[0]["slots"]["doc_no"] == "XS2026100201"
    assert result["pending_action"]["awaiting_confirm"] is True


def test_flow_step_decision_without_pending():
    """决定已失效（已提交/已取消/超时）：明确告知，不猜测、不写库"""
    result, events, repo = _run_flow(
        {"query": "确认", "pending_action": None,
         "confirm_decision": "confirm", "messages": []})

    assert repo.commits == []
    assert result["pending_action"] is None and result["confirm_decision"] == ""
    assert "没有待确认" in events[0]["text"]


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in fns:
        fn()
        print(f"PASS {name}")
    print(f"ALL GWT OK ({len(fns)} cases)")
