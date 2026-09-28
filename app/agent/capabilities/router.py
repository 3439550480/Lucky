"""
能力路由节点（04 文档 §3.2 五级递进通道）

链路位置：graph.py 中取代 intent_classify 的入口节点，START 之后第一个执行。
五级通道（每级未命中/失败自动落入下一级）：
  tier 0  用户芯片显式选择（selectable=true 的能力）—— 确定性 100%
  tier 1  规则快路径（高确定性正则，0 token）
  tier 2  embedding 安全网（examples 向量相似度 >= 阈值）
  tier 3  LLM 分类（classifier_provider，暂定 deepseek）
  兜底    error_capability=default（致歉并建议重试）
所有出口统一走 _finish：写 capability_holder（SSE 注入）+ 组装 state 增量 +
发 progress 事件。capability_source 记录路由来源，03 评估据此过滤
（tool_metrics 只统计 source != "user" 的自动路由）。
"""
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.capabilities.registry import CapabilityRegistry
from app.agent.context import DataAgentContext
from app.agent.llm_factory import create_llm
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


def _last_assistant_msg(state: DataAgentState) -> str:
    """取最近一条助手消息（供分类提示词参考上下文）。
    [05 接管] 此逻辑将由 history_provider 统一供给，届时此处删除"""
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            return msg.get("content", "")
    return ""


def _finish(writer, registry: CapabilityRegistry, holder,
            capability: str, source: str) -> dict:
    """五级通道的统一出口：
    step 1: 写 capability_holder —— QueryService 逐 SSE 事件读取注入（04 §3.4）
    step 2: 组装 state 增量 —— tool_calls 按 v1 规则：dataquery → ["dataquery.search"]
    step 3: 发 success 事件并返回增量"""
    holder.value = capability
    tool_calls = ["dataquery.search"] if capability == "dataquery" else []
    writer({"type": "progress", "step": "理解用户意图", "status": "success"})
    logger.info(f"路由完成: {capability} (source={source})")
    return {"capability": capability, "intent": capability,
            "capability_source": source, "tool_calls": tool_calls, "intent_reply": ""}


async def route_capability(state: DataAgentState, runtime: Runtime[DataAgentContext]) -> dict:
    """路由节点主体：五级递进，永不抛异常（所有失败都落到兜底能力）"""
    writer = runtime.stream_writer
    registry: CapabilityRegistry = runtime.context["capability_registry"]
    holder = runtime.context["capability_holder"]
    writer({"type": "progress", "step": "理解用户意图", "status": "running"})
    query = state["query"]

    # 总开关关闭 → 恒 dataquery（关 = 旧行为，00 红线；用户芯片选择一并忽略）
    if not app_config.features.capability_routing:
        logger.info("能力路由已关闭，直通 dataquery（旧路径）")
        return _finish(writer, registry, holder, "dataquery", "fallback")

    # ---- tier 0: 用户芯片显式选择（selectable=true 才合法，非法值忽略）----
    requested = state.get("requested_capability", "")
    if app_config.features.capability_chip and requested:
        cap = registry.capabilities.get(requested)
        if cap and cap.selectable:
            logger.info(f"路由(tier0-user): {requested}")
            return _finish(writer, registry, holder, requested, "user")
        logger.warning(f"请求的 capability '{requested}' 不可选，忽略并走自动路由")

    # ---- tier 1: 规则快路径（高确定性正则，命中 0 token）----
    if app_config.features.rules_fast_path:
        hit = registry.match_rules(query)
        if hit:
            logger.info(f"路由(tier1-rules): {hit}")
            return _finish(writer, registry, holder, hit, "rules")

    # ---- tier 2: embedding 安全网（故障降级到 LLM，不阻断路由）----
    if app_config.features.embedding_route:
        try:
            embedding_client = runtime.context["embedding_client"]
            await registry.ensure_vectors(embedding_client)
            query_vector = await embedding_client.aembed_query(query)
            hit = await registry.match_embedding(query_vector)
            if hit:
                logger.info(f"路由(tier2-embedding): {hit[0]} score={hit[1]:.3f}")
                return _finish(writer, registry, holder, hit[0], "embedding")
        except Exception as e:
            logger.warning(f"embedding 安全网异常，跳过落入 LLM: {e}")

    # ---- tier 3: LLM 分类 + 双兜底 ----
    try:
        # 分类专用 LLM 与生成模型解耦（classifier_provider），计入同一份 tracker
        classifier = create_llm(registry.classifier_provider,
                                usage_tracker=runtime.context.get("usage_tracker"))
        chain = PromptTemplate(
            template=load_prompt("capability_route"),
            input_variables=["capabilities", "query", "last_assistant_msg"],
        ) | classifier | JsonOutputParser()
        result = await chain.ainvoke({
            "capabilities": registry.build_llm_context(),
            "query": query,
            "last_assistant_msg": _last_assistant_msg(state),
        })
        cap = (result or {}).get("capability", "")
        if cap in registry.capabilities:
            logger.info(f"路由(tier3-llm): {cap}")
            return _finish(writer, registry, holder, cap, "llm")
        # 分类成功但值不在注册表 → default_capability
        logger.warning(f"LLM 分类结果 '{cap}' 不在注册表，兜底 default")
        return _finish(writer, registry, holder, registry.default_capability, "fallback")
    except Exception as e:
        # LLM 调用/解析失败 → error_capability（default，致歉重试）
        logger.error(f"路由 LLM 分类失败: {e}")
        return _finish(writer, registry, holder, registry.error_capability, "fallback")
