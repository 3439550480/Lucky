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
from app.agent.session.history_provider import get_conversation_history, render_history
from app.agent.session.prefix import build_system_prefix
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


# 能力 -> 工具序列映射（03 tool_metrics 数据源；新增能力在此登记，
# v1.1 由 "dataquery 硬编码" 泛化——inventory/replenish 等新能力不再被记为漏调）
_TOOL_MAP = {
    "dataquery": ["dataquery.search"],
    "inventory": ["inventory.query"],
    "replenish": ["replenish.plan"],
}

# 能力 → 所需权限点（2.0 上下文策略 1.4：代码层硬校验，能力级粒度）。
# 用户拍板：dataquery.hot/sales 合并为 dataquery.query（热卖排行移前端侧边栏），
# 权限校验全部落在能力级——inventory 与 dataquery 虽共用入口，校验在路由出口按
# 能力分别判定，无需能力内语义细分。default 兜底不设权限。
_CAPABILITY_PERM = {
    "dataquery": "dataquery.query",
    "inventory": "inventory.read",
    "replenish": "replenish.store",
    "warehouse_replenish": "replenish.warehouse",   # 预留能力启用时自动生效
}


def _disabled_capabilities() -> set[str]:
    """按 Feature Flags 计算本轮被禁用的能力（2.0 S3b 门控）。
    禁用语义 = 路由层视该能力不存在：tier1/2 匹配时跳过（不占用命中），
    tier3 分类结果不接标 —— 关闭 capability_inventory/capability_replenish
    即恢复 v1.1 的 dataquery+default 双能力行为（关 = 旧行为纪律）"""
    flags = app_config.features
    disabled: set[str] = set()
    if not flags.capability_inventory:
        disabled.add("inventory")
    if not flags.capability_replenish:
        disabled.add("replenish")
    return disabled


def _finish(writer, registry: CapabilityRegistry, holder,
            capability: str, source: str) -> dict:
    """五级通道的统一出口：
    step 1: 写 capability_holder —— QueryService 逐 SSE 事件读取注入（04 §3.4）
    step 2: 组装 state 增量 —— tool_calls 按 v1 规则：dataquery → ["dataquery.search"]
    step 3: 发 success 事件并返回增量"""
    holder.value = capability
    tool_calls = _TOOL_MAP.get(capability, [])
    writer({"type": "progress", "step": "理解用户意图", "status": "success"})
    logger.info(f"路由完成: {capability} (source={source})")
    return {"capability": capability, "intent": capability,
            "capability_source": source, "tool_calls": tool_calls, "intent_reply": ""}


def _finish_denied(writer, holder, capability: str, staff, required: str) -> dict:
    """权限拒绝出口（2.0 上下文策略 1.4）：不改写能力语义——capability 照实写入
    （评估/审计能看到"他想用什么"），由 permission_denied 节点终点化。
    source 记 "denied"，tool_metrics 据此过滤；tool_calls 置空（未调用任何工具）。"""
    holder.value = capability
    logger.warning(f"[auth] 权限拒绝: {staff.staff_id}（{staff.role_codes}）"
                   f"请求 {capability}，缺权限点 {required}")
    writer({"type": "progress", "step": "理解用户意图", "status": "success"})
    return {"capability": capability, "intent": capability,
            "capability_source": "denied", "tool_calls": [], "intent_reply": "",
            "permission_denied": True, "denied_permission": required}


async def route_capability(state: DataAgentState, runtime: Runtime[DataAgentContext]) -> dict:
    """路由节点主体：五级递进，永不抛异常（所有失败都落到兜底能力）"""
    writer = runtime.stream_writer
    registry: CapabilityRegistry = runtime.context["capability_registry"]
    holder = runtime.context["capability_holder"]
    writer({"type": "progress", "step": "理解用户意图", "status": "running"})
    query = state["query"]

    # 总开关关闭 → 恒 dataquery（关 = 旧行为，00 红线；用户芯片选择一并忽略）。
    # 权限校验不受此开关影响——安全边界独立于路由实验开关
    if not app_config.features.capability_routing:
        logger.info("能力路由已关闭，直通 dataquery（旧路径）")
        denied = _deny("dataquery")
        return denied if denied else _finish(writer, registry, holder, "dataquery", "fallback")

    def _deny(capability: str) -> dict:
        """能力级权限硬校验（策略 1.4）：staff 为 None（评测/离线路径）时跳过——
        评估直接构造图运行，不经过鉴权，保持旧行为可测"""
        required = _CAPABILITY_PERM.get(capability)
        staff = runtime.context.get("staff")
        if required and staff is not None and not staff.has(required):
            return _finish_denied(writer, holder, capability, staff, required)
        return {}

    disabled = _disabled_capabilities()

    # ---- tier 0: 用户芯片显式选择（selectable=true 且未禁用才合法，非法值忽略）----
    requested = state.get("requested_capability", "")
    if app_config.features.capability_chip and requested:
        cap = registry.capabilities.get(requested)
        if cap and cap.selectable and cap.name not in disabled:
            logger.info(f"路由(tier0-user): {requested}")
            denied = _deny(requested)
            return denied if denied else _finish(writer, registry, holder, requested, "user")
        logger.warning(f"请求的 capability '{requested}' 不可选/已禁用，忽略并走自动路由")

    # ---- tier 1: 规则快路径（高确定性正则，命中 0 token）----
    if app_config.features.rules_fast_path:
        hit = registry.match_rules(query, skip=disabled)
        if hit:
            logger.info(f"路由(tier1-rules): {hit}")
            denied = _deny(hit)
            return denied if denied else _finish(writer, registry, holder, hit, "rules")

    # ---- tier 2: embedding 安全网（故障降级到 LLM，不阻断路由）----
    if app_config.features.embedding_route:
        try:
            embedding_client = runtime.context["embedding_client"]
            await registry.ensure_vectors(embedding_client)
            query_vector = await embedding_client.aembed_query(query)
            hit = await registry.match_embedding(query_vector, skip=disabled)
            if hit:
                logger.info(f"路由(tier2-embedding): {hit[0]} score={hit[1]:.3f}")
                denied = _deny(hit[0])
                return denied if denied else _finish(writer, registry, holder, hit[0], "embedding")
        except Exception as e:
            logger.warning(f"embedding 安全网异常，跳过落入 LLM: {e}")

    # ---- tier 3: LLM 分类 + 双兜底 ----
    try:
        # 分类专用 LLM 与生成模型解耦（classifier_provider），计入同一份 tracker
        classifier = create_llm(registry.classifier_provider,
                                usage_tracker=runtime.context.get("usage_tracker"))
        # [05 上下文管理] 开关二选一：新 = 三区模板（capabilities 进前缀，完整对话历史）；
        # 旧 = legacy 模板（capabilities + last_assistant_msg 内联）
        if app_config.features.context_management:
            chain = PromptTemplate(
                template=load_prompt("capability_route"),
                input_variables=["system_prefix", "conversation_history", "query"],
            ) | classifier | JsonOutputParser()
            chain_input = {
                # [2.0 上下文策略 1.2] 前缀按角色渲染【当前用户】段（4 份分裂）
                "system_prefix": build_system_prefix(runtime.context.get("staff")),
                # [2.0 上下文策略] 历史渲染统一走 render_history（元数据不透模型）
                "conversation_history": render_history(
                    get_conversation_history(state), "text"),
                "query": query,
            }
        else:
            chain = PromptTemplate(
                template=load_prompt("legacy/capability_route"),
                input_variables=["capabilities", "query", "last_assistant_msg"],
            ) | classifier | JsonOutputParser()
            last_assistant = next(
                (m.get("content", "") for m in reversed(state.get("messages", []))
                 if m.get("role") == "assistant"),
                "",
            )
            chain_input = {
                "capabilities": registry.build_llm_context(),
                "query": query,
                "last_assistant_msg": last_assistant,
            }
        result = await chain.ainvoke(chain_input)
        cap = (result or {}).get("capability", "")
        if cap in disabled:
            # 分类为被禁用能力 → 不接标，落 default（与"不在注册表"同待遇）
            logger.info(f"LLM 分类结果 '{cap}' 已被 Feature Flags 禁用，兜底 default")
            return _finish(writer, registry, holder, registry.default_capability, "fallback")
        if cap in registry.capabilities:
            logger.info(f"路由(tier3-llm): {cap}")
            denied = _deny(cap)
            return denied if denied else _finish(writer, registry, holder, cap, "llm")
        # 分类成功但值不在注册表 → default_capability
        logger.warning(f"LLM 分类结果 '{cap}' 不在注册表，兜底 default")
        return _finish(writer, registry, holder, registry.default_capability, "fallback")
    except Exception as e:
        # LLM 调用/解析失败 → error_capability（default，致歉重试）
        logger.error(f"路由 LLM 分类失败: {e}")
        return _finish(writer, registry, holder, registry.error_capability, "fallback")
