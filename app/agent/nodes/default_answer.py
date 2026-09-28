"""
default 能力处理节点（04 文档 §3.3）

链路位置：能力路由（route_capability）选中 default 后的终点节点。
设计定位（项目所有者确认）：项目是"通用平台助手附带数据查询能力"，
default 交由模型自由发挥 —— 我们只负责过程细节（SSE 事件、轨迹写入、降级兜底），
不预设人设、不硬性引导任何能力介绍。
与旧节点（recap/chitchat/help 静态文案）的差异：
  1. 回复由 LLM 生成，失败降级为静态兜底文案（兜底节点自己不能再失败）
  2. 用户消息与回复都写入轨迹（旧闲聊路径不写 —— 轨迹完整性改进，05 统一管理）
历史读取沿用 state["messages"] 模式，05 落地后切换到 history_provider
"""
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

# LLM 失败时的静态兜底文案 —— 兜底节点自己不能再失败（04 §3.3）
_FALLBACK_REPLY = "抱歉，我暂时没能生成回复，请稍后重试。"


def _recent_assistant_content(state: DataAgentState) -> str:
    """倒序取最近一条助手消息内容（供"刚才/上次"类问题参考）——05 起移交 history_provider"""
    for msg in reversed(state.get("messages", [])):
        if msg.get("role") == "assistant":
            return msg.get("content", "")
    return ""


async def default_answer(state: DataAgentState, runtime: Runtime[DataAgentContext]) -> dict:
    writer = runtime.stream_writer
    llm = runtime.context["llm"]          # [新机制样板] 用户选择的生成模型，已挂 tracker
    writer({"type": "progress", "step": "生成回复", "status": "running"})
    query = state["query"]

    # step 1: LLM 生成回复 —— 失败降级为静态文案（兜底节点不抛异常）
    fallback_used = False
    try:
        chain = PromptTemplate(
            template=load_prompt("default_answer"),
            input_variables=["capabilities", "query", "last_assistant_msg"],
        ) | llm | StrOutputParser()
        reply = await chain.ainvoke({
            "capabilities": runtime.context["capability_registry"].build_llm_context(),
            "query": query,
            "last_assistant_msg": _recent_assistant_content(state),
        })
    except Exception as e:
        logger.error(f"default_answer LLM 失败，使用静态兜底: {e}")
        reply, fallback_used = _FALLBACK_REPLY, True

    # step 2: 推送进度与解释 —— 前端按 explanation 事件渲染文本气泡
    writer({"type": "progress", "step": "生成回复",
            "status": "error" if fallback_used else "success"})
    writer({"type": "explanation", "text": reply})

    # step 3: 轨迹写入 —— 用户消息 + 助手回复都入轨迹（trajectory 完整性，05 统一管理）
    messages = list(state.get("messages", []))
    messages.append({"role": "user", "content": query})
    messages.append({"role": "assistant", "content": reply})
    return {"intent_reply": reply, "messages": messages}
