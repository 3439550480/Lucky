"""
流程推进节点（2.0 上下文策略 3.6：槽位表驱动，单节点）

每轮：① 无流程却收到结构化决定 → 明确告知已失效
→ ② 确认门：结构化决定（状态）+ 前端按钮（SSE）优先，自由文本兜底
→ ③ 用本轮输入遍历全部空缺槽位（不假设顺序——"一次给全"能一轮就齐）
→ ④ 校验已填槽位（有错停原步，已填保留）→ ⑤ 齐全进确认门（仍不写库）
→ ⑥ 确认后才 commit（事务内幂等复查）。校验失败/缺槽位 → SSE 提示 → END。
[S3 结构化确认] 确认门推 confirm 事件（前端渲染按钮卡片），用户手打"确认/取消"仍可用。
流程中每轮问答照常写轨迹（brief），出入库不产生长期记忆（策略 3.7）。
"""
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.nodes.inventory_write.slots import (
    is_cancel,
    is_confirm,
    parse_direction,
    parse_doc_no,
    parse_qty,
    parse_sku,
)
from app.agent.state import DataAgentState
from app.core.log import logger

# 槽位表：parse 同步（纯函数），validate 异步（幂等/存在性/超量需查库）
_SLOTS = ("direction", "doc_no", "sku_id", "qty")
_SLOT_LABELS = {"direction": "方向（入库/出库）", "doc_no": "单据号",
                "sku_id": "SKU（货号-色码-尺码）", "qty": "数量（件）"}


def _confirm_text(pending: dict, staff_name: str, staff_id: str) -> str:
    s = pending["slots"]
    direction = "入库" if s["direction"] == "in" else "出库"
    return (f"请确认：{direction} / 单据 {s['doc_no']} / SKU {s['sku_id']} / "
            f"{s['qty']} 件 / 经办人 {staff_name}（{staff_id}）—— 确认提交？")


async def flow_step(state: DataAgentState, runtime: Runtime[DataAgentContext]) -> dict:
    writer = runtime.stream_writer
    dw = runtime.context["dw_mysql_repository"]
    write_repo = runtime.context["inventory_write_repository"]
    staff = runtime.context.get("staff")
    staff_id = staff.staff_id if staff else "SYSTEM"
    staff_name = staff.name if staff else "系统"
    query = state["query"]
    pending = dict(state.get("pending_action") or {})
    slots = dict(pending.get("slots") or {})
    # [S3 结构化确认] 前端按钮决定的注入通道（POST /api/flow/confirm → QueryService.confirm）。
    # "" = 本轮没有结构化决定 → 退回自由文本判定（手打"确认/取消"的老路径保持可用）
    decision = state.get("confirm_decision") or ""

    # ---- 0) 有结构化决定但没有进行中的流程：状态已失效 ----
    # （已提交 / 已取消 / 超时清理 / 进程重启后 checkpoint 不存在）
    # 必须在这里收尾：否则本轮会落到能力路由，对"确认"两个字再做一次语义猜测
    if decision and not pending:
        writer({"type": "explanation",
                "text": "没有待确认的出入库操作（可能已提交、已取消或已超时），请重新发起。"})
        return {"pending_action": None, "confirm_decision": ""}

    # ---- 1) 确认门：四槽已齐且等待确认 ----
    if pending.get("awaiting_confirm"):
        # [S3 结构化确认] 决策优先级：结构化按钮（可信，不经过任何文本解析）>
        # 自由文本（兜底。slots.is_confirm 已严格化，但仍留它是因为"手打确认"是
        # 无障碍/降级场景下唯一入口）
        if decision == "cancel" or (not decision and is_cancel(query)):
            writer({"type": "explanation", "text": "已取消本次出入库操作。"})
            return {"pending_action": None, "confirm_decision": ""}
        if decision != "confirm" and not is_confirm(query):
            # 尚未决定 → 推 confirm 事件：前端渲染「确认提交 / 取消」按钮卡片
            writer({"type": "confirm", "action": "inventory_write",
                    "slots": dict(slots),
                    "text": _confirm_text(pending, staff_name, staff_id)})
            return {"pending_action": pending}
        try:
            result = await write_repo.commit_flow(
                direction=slots["direction"], doc_no=slots["doc_no"],
                sku_id=slots["sku_id"], qty=int(slots["qty"]),
                operator_id=staff_id, operator_name=staff_name)
        except Exception as e:
            from app.repositories.mysql.dw.inventory_write_repository import FlowAlreadyProcessed
            if isinstance(e, FlowAlreadyProcessed):
                writer({"type": "explanation",
                        "text": f"单据 {slots['doc_no']} 已处理过（幂等拦截），未重复记账。"})
                return {"pending_action": None, "confirm_decision": ""}
            raise
        direction = "入库" if slots["direction"] == "in" else "出库"
        done_text = (f"✅ 已{direction} {slots['qty']} 件（{slots['sku_id']}），"
                     f"该 SKU 当前可用库存 {result['available_after']} 件。")
        writer({"type": "explanation", "text": done_text})
        writer({"type": "result", "data": [{
            "操作": direction, "单据号": slots["doc_no"], "SKU": slots["sku_id"],
            "数量": int(slots["qty"]), "当前可用": result["available_after"],
            "经办人": staff_id,
        }]})
        # 轨迹写入（brief 摘要随条目；出入库不产生长期记忆——策略 3.7）
        from app.agent.session.context_store import append_assistant_message
        messages = append_assistant_message(state, done_text, capability="inventory_write",
                                            staff_id=staff_id, brief=done_text)
        logger.info(f"[flow] 写库完成: {direction} {slots['qty']} 件 {slots['sku_id']} "
                    f"单据 {slots['doc_no']} 经办 {staff_id} flow_id={result['flow_id']}")
        # confirm_decision 归零：决定是一次性输入，不能留在 checkpoint 里复发
        return {"pending_action": None, "confirm_decision": "", "messages": messages}

    # ---- 2) 遍历全部空缺槽位（不假设顺序）----
    if slots.get("direction") is None:
        slots["direction"] = parse_direction(query)
    if slots.get("doc_no") is None:
        slots["doc_no"] = parse_doc_no(query)
    if slots.get("sku_id") is None:
        slots["sku_id"] = parse_sku(query)
    if slots.get("qty") is None:
        slots["qty"] = parse_qty(query)

    # ---- 3) 校验已填槽位（有错停原步，已填槽位保留）----
    if slots.get("direction") is None:
        writer({"type": "explanation",
                "text": "方向不明确：请说明是入库还是出库（如\"入库 RK… … 20\"）。"})
        return {"pending_action": {**pending, "slots": slots}}
    if slots.get("doc_no") and await write_repo.doc_exists(slots["doc_no"]):
        writer({"type": "explanation",
                "text": f"单据 {slots['doc_no']} 已处理过（幂等拦截），请核对或更换单据号。"})
        return {"pending_action": {**pending, "slots": slots}}
    if slots.get("sku_id") and not await write_repo.sku_exists(slots["sku_id"]):
        writer({"type": "explanation",
                "text": f"SKU {slots['sku_id']} 在系统里不存在，请核对货号-色码-尺码。"})
        slots["sku_id"] = None
        return {"pending_action": {**pending, "slots": slots}}
    if slots.get("qty") is not None and int(slots["qty"]) <= 0:
        slots["qty"] = None
    if slots.get("qty") is not None and slots.get("direction") == "out":
        available = await write_repo.latest_available(slots["sku_id"]) if slots.get("sku_id") else None
        if available is not None and int(slots["qty"]) > available:
            writer({"type": "explanation",
                    "text": f"该 SKU 当前可用库存 {available} 件，出库 {slots['qty']} 件超量，请调整数量。"})
            slots["qty"] = None
            return {"pending_action": {**pending, "slots": slots}}

    pending = {**pending, "slots": slots}

    # ---- 4) 四槽齐全 → 确认门（仍不写库）----
    if all(slots.get(n) is not None for n in _SLOTS):
        pending["awaiting_confirm"] = True
        # [S3 结构化确认] 首轮集齐四槽也推 confirm 事件（不是只回一句"请回复确认"）：
        # 前端直接渲染按钮卡片，slots 快照随事件下发供人工核对——本节点不擅自写库
        writer({"type": "confirm", "action": "inventory_write",
                "slots": dict(slots),
                "text": _confirm_text(pending, staff_name, staff_id)})
        return {"pending_action": pending}

    # ---- 5) 不全 → 只问缺的那几个 ----
    missing = [(_SLOT_LABELS[n]) for n in _SLOTS if slots.get(n) is None]
    writer({"type": "explanation",
            "text": f"还缺：{'、'.join(missing)}。可以一次给全（如\"入库 RK2026101001 15262011-01-42 20\"）。"})
    return {"pending_action": pending}
