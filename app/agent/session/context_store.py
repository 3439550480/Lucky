"""
轨迹记账入口（05 文档 §3.2）

设计决策（§3.1）：轨迹仍以 state["messages"] 为存储介质（LangGraph checkpointer 持久化），
本模块是无状态策略层——写入返回完整新列表（TypedDict 字段覆盖语义），不引入第二份存储。
所有条目为向后兼容的 {"role","content"} + 可选元数据（capability/ts，§3.1）。
"""
import time


def _new_entry(role: str, content: str, capability: str | None) -> dict:
    """构造单条轨迹条目（capability 为 None 时省略元数据字段——与旧轨迹形态完全一致）"""
    entry = {"role": role, "content": content}
    if capability:
        entry["capability"] = capability
        entry["ts"] = time.time()
    return entry


def get_trajectory(state: dict) -> list[dict]:
    """读全量轨迹（缺省 [] 兜底——保留 query_service 未初始化 messages 时的容错）"""
    return state.get("messages") or []


def append_user_message(state: dict, query: str, capability: str | None = None,
                        staff_id: str | None = None) -> list[dict]:
    """追加用户消息，返回完整新列表（extract_keywords 的写入入口，§4#7）。
    [2.0 上下文策略 1.1] staff_id：条目级身份（审计与一致性校验用），可缺省"""
    messages = list(state.get("messages") or [])
    entry = _new_entry("user", query, capability)
    if staff_id:
        entry["staff_id"] = staff_id
    messages.append(entry)
    return messages


def append_assistant_message(state: dict, content: str, capability: str | None = None,
                             staff_id: str | None = None,
                             brief: str | None = None) -> list[dict]:
    """追加助手消息（explain_result / default_answer 的写入入口，§4#8）。
    [2.0 上下文策略 1.1] staff_id：条目级身份（审计与一致性校验用），可缺省。
    [2.0 上下文策略 1.6.10] brief：轨迹摘要（渲染产物随条目暂存；轨迹仍存完整原文，
    简洁化统一收口在 history_provider.render_history）。可缺省"""
    messages = list(state.get("messages") or [])
    entry = _new_entry("assistant", content, capability)
    if staff_id:
        entry["staff_id"] = staff_id
    if brief:
        entry["brief"] = brief
    messages.append(entry)
    return messages


def summarize_trajectory_hook(state: dict) -> None:
    """[预留接口，v1 空实现] 轨迹轮数超 summary_trigger_turns 时被调用（§3.2）。
    摘要算法后续迭代；v1 仅记录 debug 日志不压缩——避免摘要不确定性污染 baseline（§5）"""
    from app.conf.app_config import app_config   # 局部导入：避免与 app_config.py 初始化顺序耦合
    from app.core.log import logger

    turns = len(state.get("messages") or []) // 2
    if turns > app_config.session.summary_trigger_turns:
        logger.debug(f"[session] 轨迹 {turns} 轮超阈值 {app_config.session.summary_trigger_turns}，"
                     f"摘要压缩未启用（v1 预留钩子）")
