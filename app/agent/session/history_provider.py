"""
历史供给统一入口（05 文档 §3.3）

generate_sql / default_answer / capability 路由提示词的历史读取全部收敛于此，
替换各节点内联的 state["messages"] 切片逻辑（05 §4#9-11 的切换落点）。
v1 语义：返回全量轨迹（旧→新）——截断参数保留签名但不生效（§3.4 对话完整放入）。
"""
import yaml

from app.agent.session.context_store import get_trajectory
from app.conf.app_config import app_config
from app.core.log import logger


def _enforce_budget(messages: list[dict], budget: int) -> list[dict]:
    """历史区硬保护（2.0 上下文策略 1.6.17）：超预算从最旧整条丢弃。
    估算口径：1 字符 ≈ 1 token（对中文高估 → 更早裁剪 → 安全侧）。
    裁剪必记 warning——静默降级极难排查；缓存会失效一次（可接受的兜底代价）。
    trace_brief 开启后历史区天然极小，此守卫几乎不会触发（兜底保险定位）。"""
    total = sum(len(str(m.get("content", ""))) for m in messages)
    if total <= budget:
        return messages
    trimmed = list(messages)
    dropped = 0
    while trimmed and sum(len(str(m.get("content", ""))) for m in trimmed) > budget:
        trimmed.pop(0)
        dropped += 1
    logger.warning(f"[session] 历史区超预算（~{total} tok > {budget}），"
                   f"从最旧丢弃 {dropped} 条（硬保护，缓存将失效一次）")
    return trimmed


def get_conversation_history(state: dict, *, max_turns: int | None = None,
                             max_tokens: int | None = None) -> list[dict]:
    """统一历史供给 + 硬保护（2.0 上下文策略 1.6.17）。

    v1 常态：对话完整放入（追加式保 KV cache 命中，1.6.3 已算账）。
    [2.0] 激活休眠的 session.history_max_tokens（4000）作为历史区硬预算——
    远低于 64K 上下文的 70%（≈44K），常规件下守卫不触发。"""
    return _enforce_budget(get_trajectory(state), app_config.session.history_max_tokens)


def render_history(messages: list[dict], style: str = "text") -> str:
    """历史渲染统一出口（2.0 上下文策略差异 #3 + 1.7 简洁化收口点）。

    轨迹条目可能携带元数据（capability/ts/staff_id）——这些是审计字段，
    【不透给模型】。所有节点拼 prompt 的历史段一律经此函数，禁止直接
    yaml.dump(messages)（会把元数据暴露给模型，还会随元数据增长破坏缓存）。

    [2.0 上下文策略 1.6.10] features.trace_brief 开启时：assistant 条目若携带
    brief（节点写入时的结构化摘要），渲染用 brief 替代完整原文——这是"简洁化
    收口在一处"的落点，消费方（router/generate_sql/default/_resolve_scope）零改动。
    default 能力的条目不写 brief → 恒原文（其历史最需要话题脉络，1.6.11 例外条款）。

    style:
      "text" —— "[role] content" 逐行纯文本（router / default_answer 消费）
      "yaml" —— 仅 {role, content} 的 yaml（generate_sql / explain_result 消费，
                保留结构可读性，元数据已剥离）
    """
    use_brief = app_config.features.trace_brief
    clean = []
    for m in messages or []:
        content = m.get("content", "")
        if use_brief and m.get("role") == "assistant" and m.get("brief"):
            content = m["brief"]
        clean.append({"role": m.get("role", ""), "content": content})
    if style == "yaml":
        return yaml.dump(clean, allow_unicode=True, sort_keys=False)
    return "\n".join(f"[{m['role']}] {m['content']}" for m in clean)


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
