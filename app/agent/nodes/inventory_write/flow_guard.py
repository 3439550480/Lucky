"""
流程守卫节点（2.0 上下文策略 3.4/3.6）

链路位置：START 之后第一个节点（取代直连 route_capability）。
分支：
  ① 有 pending_action → 取消/超时检测（命中则清空放行）→ 否则进 flow_step
  ② 无流程 → 写意图探测（句首方向词，窄规则防误劫持查询）：
       - 命中且 staff.has("inventory.write") → 创建 pending_action（初始槽位）→ flow_step
       - 命中但无权限 → permission_denied 终点（复用权限拒绝节点）
       - 未命中 → route_capability（原链路零改动）
设计依据（策略 3.4）：一旦进入流程优先级最高——否则用户在第 3 步回答"42 码"
会被误判成库存查询；写意图探测放守卫层而非能力注册表，避免绕开五级路由语义。
"""
import time

from langgraph.runtime import Runtime

from app.agent.nodes.inventory_write.slots import (
    detect_write_intent,
    flow_expired,
    is_cancel,
    parse_direction,
    parse_doc_no,
    parse_qty,
    parse_sku,
)
from app.agent.state import DataAgentState


def _new_pending(query: str) -> dict:
    """从首轮输入尽可能多填槽位（策略 3.2：一次能填多少填多少）"""
    return {
        "type": "inventory_write",
        "slots": {
            "direction": parse_direction(query),
            "doc_no": parse_doc_no(query),
            "sku_id": parse_sku(query),
            "qty": parse_qty(query),
        },
        "started_at": time.time(),
        "awaiting_confirm": False,
    }


async def flow_guard(state: DataAgentState, runtime: Runtime) -> dict:
    writer = runtime.stream_writer
    query = state["query"]
    pending = state.get("pending_action")

    # ---- ① 流程进行中 ----
    if pending:
        if is_cancel(query):
            writer({"type": "explanation", "text": "已取消本次出入库操作。"})
            return {"pending_action": None}
        if flow_expired(pending):
            writer({"type": "explanation",
                    "text": "上次的出入库操作已超时（超过 10 分钟），请重新开始。"})
            return {"pending_action": None}
        return {}   # 继续 flow_step（槽位增量在本节点不填，交给 flow_step 统一处理）

    # ---- ② 无流程：写意图探测 ----
    if not detect_write_intent(query):
        return {}   # 放行 route_capability

    staff = runtime.context.get("staff")
    if staff is not None and not staff.has("inventory.write"):
        # 权限门禁（策略 3.7）：无权限不启动流程 → 复用权限拒绝终点
        return {"capability": "inventory_write", "permission_denied": True,
                "denied_permission": "inventory.write", "tool_calls": [],
                "capability_source": "denied", "intent": "inventory_write"}

    # 评测/离线路径（staff 为 None）：允许流程推进（与能力校验同款容错）
    return {"pending_action": _new_pending(query)}
