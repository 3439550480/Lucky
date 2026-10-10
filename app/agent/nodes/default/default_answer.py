"""
default 能力处理节点（04 文档 §3.3）

链路位置：能力路由（route_capability）选中 default 后的终点节点。
设计定位：default 交由模型自由发挥 —— 我们只负责过程细节（SSE 事件、轨迹写入、降级兜底），
不预设人设。
[规则3 留档] v1.0 定位"不硬性引导任何能力介绍"；v1.1 PRD FR-07 调整为
"无关闲聊/超纲问题必须礼貌引导回问数场景"——引导语句已移入 prompts/default_answer.prompt
（【任务要求】段），节点代码不变。
与旧节点（recap/chitchat/help 静态文案）的差异：
  1. 回复由 LLM 生成，失败降级为静态兜底文案（兜底节点自己不能再失败）
  2. 用户消息与回复都写入轨迹（旧闲聊路径不写 —— 轨迹完整性改进，05 统一管理）
历史读取沿用 state["messages"] 模式，05 落地后切换到 history_provider
"""
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.session.context_store import append_assistant_message, append_user_message
from app.agent.session.history_provider import (
    get_conversation_history,
    get_recent_assistant_content,
    render_history,
)
from app.agent.session.prefix import build_system_prefix
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

# LLM 失败时的静态兜底文案 —— 兜底节点自己不能再失败（04 §3.3）
_FALLBACK_REPLY = "抱歉，我暂时没能生成回复，请稍后重试。"


async def default_answer(state: DataAgentState, runtime: Runtime[DataAgentContext]) -> dict:
    writer = runtime.stream_writer
    llm = runtime.context["llm"]          # [新机制样板] 用户选择的生成模型，已挂 tracker
    writer({"type": "progress", "step": "生成回复", "status": "running"})
    query = state["query"]

    # step 1: LLM 生成回复 —— 失败降级为静态文案（兜底节点不抛异常）
    # [05 上下文管理] 开关二选一：新 = 三区模板 + history_provider；旧 = legacy 模板 + 内联读取
    fallback_used = False
    try:
        if app_config.features.context_management:
            # [06 记忆注入] 动态尾部（对话历史之后，06 §3.4 纪律）；long_term 关闭/无记忆 → 空串
            memory_block = ""
            if app_config.features.memory.long_term:
                try:
                    from app.agent.memory.retriever import retrieve_memory_block
                    memory_block = await retrieve_memory_block(
                        query, runtime.context.get("memory_store"),
                        runtime.context.get("embedding_client"))
                except Exception as e:
                    logger.warning(f"[memory] 记忆注入失败（跳过）: {e}")
            chain = PromptTemplate(
                template=load_prompt("default_answer"),
                input_variables=["system_prefix", "conversation_history",
                                 "memory_block", "query"],
            ) | llm | StrOutputParser()
            chain_input = {
                "system_prefix": build_system_prefix(),
                # [2.0 上下文策略] 历史渲染统一走 render_history（元数据不透模型）
                "conversation_history": render_history(
                    get_conversation_history(state), "text"),
                "memory_block": memory_block,
                "query": query,
            }
        else:
            chain = PromptTemplate(
                template=load_prompt("legacy/default_answer"),
                input_variables=["capabilities", "query", "last_assistant_msg"],
            ) | llm | StrOutputParser()
            chain_input = {
                "capabilities": runtime.context["capability_registry"].build_llm_context(),
                "query": query,
                "last_assistant_msg": get_recent_assistant_content(state),
            }
        reply = await chain.ainvoke(chain_input)
    except Exception as e:
        logger.error(f"default_answer LLM 失败，使用静态兜底: {e}")
        reply, fallback_used = _FALLBACK_REPLY, True

    # step 2: 推送进度与解释 —— 前端按 explanation 事件渲染文本气泡
    writer({"type": "progress", "step": "生成回复",
            "status": "error" if fallback_used else "success"})
    writer({"type": "explanation", "text": reply})

    # step 3: 轨迹写入 —— 用户消息 + 助手回复都入轨迹
    # [05] 开启开关时走 context_store（带 capability 元数据）
    if app_config.features.context_management:
        cap = state.get("capability") or "default"
        messages = append_user_message(state, query, capability=cap)
        messages = append_assistant_message({"messages": messages}, reply, capability=cap)
    else:
        messages = list(state.get("messages", []))
        messages.append({"role": "user", "content": query})
        messages.append({"role": "assistant", "content": reply})
    return {"intent_reply": reply, "messages": messages}
