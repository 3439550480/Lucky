"""
历史供给统一入口（05 文档 §3.3）

generate_sql / default_answer / capability 路由提示词的历史读取全部收敛于此，
替换各节点内联的 state["messages"] 切片逻辑（05 §4#9-11 的切换落点）。
v1 语义：返回全量轨迹（旧→新）——截断参数保留签名但不生效（§3.4 对话完整放入）。
"""
from app.agent.session.context_store import get_trajectory


def get_conversation_history(state: dict, *, max_turns: int | None = None,
                             max_tokens: int | None = None) -> list[dict]:
    """统一历史供给。
    v1：max_turns/max_tokens 忽略（对话完整放入，保 KV cache 前缀一致性，§3.4）；
    未来启用时逻辑：倒序取 max_turns 轮 → token 预算粗估截断 → 正序返回"""
    return get_trajectory(state)


def get_recent_assistant_content(state: dict, max_items: int = 5) -> str:
    """倒序找最近一条 assistant 消息内容（收敛 router/default_answer 的内联循环，§4#11）；
    找不到返回空串（首轮对话场景，与现状行为一致）"""
    for msg in reversed(get_trajectory(state)[-max_items:]):
        if msg.get("role") == "assistant":
            return msg.get("content", "")
    return ""


def capability_view(history: list[dict], capability: str) -> list[dict]:
    """[隔离视图] 按能力过滤轨迹。共享层轨迹本就只含 user/assistant 消息
    （能力中间态不进轨迹——§1.1 不变量），本函数是防御性实现 + 06 记忆检索的语料过滤载体。
    capability 为 None 的旧轨迹条目对任意能力可见（向后兼容）"""
    return [m for m in history if m.get("capability") in (None, capability)]
