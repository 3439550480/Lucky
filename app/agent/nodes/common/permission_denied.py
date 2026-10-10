"""
权限拒绝终点节点（2.0 上下文策略 1.4）

链路位置：route_capability 检出权限不足时（state["permission_denied"]=True），
route_by_capability 分发到此节点终点化——不进入任何能力链路。

设计要点：
- capability 语义照实保留（审计可见"用户想用什么"），本节点只负责把拒绝讲成人话
- 文案给两条路：请联系店长开通权限 / 换个问法（不泄露权限点名称——那是内部实现）
- 轨迹照常写入（策略 1.1：拒绝也是一次对话事件，可审计）
"""
from langgraph.runtime import Runtime

from app.agent.auth.roles import role_registry
from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger


async def permission_denied(state: DataAgentState, runtime: Runtime[DataAgentContext]) -> dict:
    """权限不足的终点：SSE 解释 + 轨迹写入，直接 END"""
    writer = runtime.stream_writer
    staff = runtime.context.get("staff")
    capability = state.get("capability") or ""
    required = state.get("denied_permission") or ""

    # 权限点 → 用户可读的能力名（来自 roles.yaml 的 label，如 replenish.store → 门店补货建议）
    perm_def = role_registry.permissions.get(required)
    label = perm_def.label if perm_def else capability
    role_name = role_registry.display_role_name(staff.role_codes) if staff else "当前角色"

    text = (f"当前角色（{role_name}）没有「{label}」权限。"
            f"如需使用，请联系店长开通；也可以换个问题试试。")
    logger.warning(f"[auth] 已终点化拒绝: staff={staff.staff_id if staff else '?'} "
                   f"capability={capability} required={required}")

    writer({"type": "progress", "step": "权限校验", "status": "error"})
    writer({"type": "explanation", "text": text})

    # 轨迹写入（拒绝事件可审计）
    from app.agent.session.context_store import append_assistant_message
    if staff is not None:
        messages = append_assistant_message(state, text, capability=capability,
                                            staff_id=staff.staff_id)
        return {"messages": messages}
    return {}
