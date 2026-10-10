"""
问数服务（01/04 文档）

职责：构建 DataAgentState + DataAgentContext（按请求注入 llm/tracker/registry/holder）
→ 驱动 LangGraph 图 → 把节点的 stream_writer 消息包装成 SSE 帧逐段 yield。
"""
import json

from app.agent.auth.staff_identity import StaffIdentity
from app.agent.capabilities.registry import registry
from app.agent.context import CapabilityHolder, DataAgentContext
from app.agent.graph import graph
from app.agent.llm_factory import create_llm
from app.agent.state import DataAgentState
from app.agent.usage import LLMUsageTracker
from app.conf.app_config import app_config
from app.core.log import logger
from app.services.cost_guard import cost_guard
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository


class QueryService:
    def __init__(
        self,
        meta_mysql_repository: MetaMySQLRepository,
        embedding_client,
        dw_mysql_repository: DWMySQLRepository,
        column_qdrant_repository: ColumnQdrantRepository,
        metric_qdrant_repository: MetricQdrantRepository,
        value_es_repository: ValueESRepository,
        inventory_write_repository=None,   # [2.0 第三章] 出入库写仓储（flow_step 消费）
        memory_store=None,   # [06] 长期记忆存储（dependencies 注入；None = 记忆功能不可用）
    ):
        # MySQL 仓储分别负责元数据补全和真实数仓环境信息读取
        self.meta_mysql_repository = meta_mysql_repository
        self.dw_mysql_repository = dw_mysql_repository
        self.inventory_write_repository = inventory_write_repository

        # 召回链路依赖的向量检索、Embedding 和全文检索能力由依赖层注入
        self.embedding_client = embedding_client
        self.column_qdrant_repository = column_qdrant_repository
        self.metric_qdrant_repository = metric_qdrant_repository
        self.value_es_repository = value_es_repository

        # [06] 记忆存储（运行后提取用）
        self.memory_store = memory_store

    async def query(self, query: str, thread_id: str,
                    model: str | None = None, capability: str | None = None,
                    staff: StaffIdentity | None = None,
                    confirm_decision: str = ""):
        """执行一次问数/对话，SSE 流式返回。

        - model: 前端选择的 LLM provider（None/非法 → create_llm 内兜底 default）
        - capability: 前端能力芯片显式选择（tier-0；None → 自动路由）
        - staff: 登录员工身份（鉴权中间件解析；2.0 起必传——登录后全端点受保护）
        - confirm_decision: [S3 结构化确认] 出入库确认门决定（"confirm"/"cancel"）。
          只由 confirm() 传入；空串 = 普通对话轮（字段必须显式归零，理由见下方 state 注释）
        """
        # step 1: 请求级计量器 —— provider 名先本地解析（非法值由 create_llm 再兜底一次，
        # 两处一致），model 名取自配置供 03 报告标注
        provider_name = model if model in app_config.llm.providers else app_config.llm.default
        tracker = None
        if app_config.features.usage_tracking:
            tracker = LLMUsageTracker(
                provider=provider_name,
                model=app_config.llm.providers[provider_name].get("model", ""),
            )
        # step 2: 组装 state 与 context —— State 放可合并业务数据，Context 放工具与请求级注入物
        # [2.0 P0 修复] 显式 result=[] 清残留：checkpointer 会恢复上一轮的 result，
        # 若本轮在 run_sql 前失败，残留旧值存在误用隐患（策略 1.6.14 附带发现）
        state = DataAgentState(
            query=query,
            keywords=[],
            retrieved_column_infos=[],
            retrieved_metric_infos=[],
            retrieved_value_infos=[],
            table_infos=[],
            metric_infos=[],
            error="",
            sql="",
            result_sample=[],
            result_total=0,
            retry_count=0,
            intent="",
            intent_reply="",
            requested_capability=capability or "",   # [04] tier-0 芯片选择
            capability="",
            capability_source="",
            tool_calls=[],
            # [S3 结构化确认] 决定是一次性输入，必须显式归零：checkpoint 会恢复上一轮的值，
            # 若本轮不覆盖，上一次点过的"确认"会在后续每一轮里复发（确认门被反复触发）
            confirm_decision=confirm_decision,
            # [final-verify 修复] 一次性路由标记同样必须显式归零：checkpointer 会恢复上一轮的
            # permission_denied / denied_permission，而 route_after_guard 会据此把本轮直接短路
            # 到权限拒绝终点（能力链路完全不进）。复现：TEMP 先试"入库…"被拒 → 同会话再问
            # "生成鞋类的补货计划"，补货请求被上一轮的「出入库登记」拒绝文案劫持（新会话单发
            # 则正确判为「门店补货建议」）。同确认门一个缺陷类：一次性输入 = 入口清残留
            permission_denied=False,
            denied_permission="",
        )
        holder = CapabilityHolder()                                  # [04] 每请求新建（防串话）
        # [2.0 P0 修复] memory_store 接线：此前漏传 → 记忆检索恒空串（读路径断裂）
        context = DataAgentContext(
            column_qdrant_repository=self.column_qdrant_repository,
            embedding_client=self.embedding_client,
            metric_qdrant_repository=self.metric_qdrant_repository,
            value_es_repository=self.value_es_repository,
            meta_mysql_repository=self.meta_mysql_repository,
            dw_mysql_repository=self.dw_mysql_repository,
            inventory_write_repository=self.inventory_write_repository,
            llm=create_llm(provider_name),   # [01] 按请求实例化（tracker 不挂实例——见下方 config 注释）
            capability_holder=holder,
            capability_registry=registry,
            usage_tracker=tracker,
            memory_store=self.memory_store,
            staff=staff,   # [2.0 上下文策略 0.4] 身份进 Context 不进 State
        )
        try:
            # stream_mode="custom" 对应节点内部 writer(...) 写出的进度消息
            # tracker 经 run config 挂载（01 §7.1 预留方案）：实例级 callback 只能收到 LLM 层事件，
            # 环节归属（by_stage）依赖的 on_chain_start 节点层事件只有 run config 的 callbacks 能触达。
            # 注意不能两处同时挂——同一 tracker 会把每次调用记两次
            config = {"configurable": {"thread_id": thread_id}}
            if tracker:
                config["callbacks"] = [tracker]
            last_answer = ""    # [06] 最后一条解释文本（运行失败时为空串，事实仍可提取）
            async for chunk in graph.astream(
                input=state,
                context=context,
                config=config,
                stream_mode="custom",
            ):
                # SSE capability 注入（04 §3.4）：路由完成后 holder 有值，
                # 其后所有事件携带 capability；路由前的事件允许缺失（前端容忍）
                if holder.value:
                    chunk.setdefault("capability", holder.value)
                # [06] 捕获最后一条解释文本（finally 阶段记忆提取的 answer 输入；
                # 记录在循环局部变量，运行失败时为空串——事实依然可提取）
                if chunk.get("type") == "explanation" and chunk.get("text"):
                    last_answer = chunk["text"]
                # SSE 要求每条消息以 data: 开头，并以两个换行符结束
                yield f"data: {json.dumps(chunk, ensure_ascii=False, default=str)}\n\n"
        except Exception as e:
            # 流式接口已经开始返回后不能再改 HTTP 状态码，因此把异常也包装成一条 SSE 消息
            error = {"type": "error", "message": str(e)}
            if holder.value:
                error["capability"] = holder.value
            yield f"data: {json.dumps(error, ensure_ascii=False, default=str)}\n\n"
        finally:
            # 请求结束输出用量汇总 —— 在线可观测 + 与 03 评估口径一致
            if tracker:
                logger.info(f"LLM usage | {json.dumps(tracker.summary(), ensure_ascii=False)}")
                # [v1.1 FR-05] 费用熔断记账 —— 开关关闭时 record() 内部直接返回 0，
                # 零状态变更；计价与 03 报告同口径，方便在线离线对账
                cost_guard.record(provider_name, tracker.records())
            # [06] 运行后记忆提取：响应已发送完毕，不阻塞用户；双重开关（long_term + extract_after_run）
            if (self.memory_store
                    and app_config.features.memory.long_term
                    and app_config.memory.extract_after_run):
                try:
                    from app.agent.memory.extractor import extract_memories
                    extract_llm = create_llm(app_config.memory.extraction_provider)
                    await extract_memories(
                        query=query,
                        answer=last_answer,
                        store=self.memory_store,
                        llm=extract_llm,
                        tracker=tracker,
                        embedding_client=self.embedding_client,
                        thread_id=thread_id,
                        staff=staff,   # [2.0] 个人层 owner_id + 门店层写入门禁的依据
                    )
                except Exception as e:
                    logger.warning(f"[memory] 记忆提取异常（不影响主链路）: {e}")

    def confirm(self, thread_id: str, decision: str,
                model: str | None = None, staff: StaffIdentity | None = None):
        """[S3 结构化确认] 前端"确认/取消"按钮 → 独立端点：把可信决定注入 state，
        驱动 flow_step 的确认门（服务端不再解析自由文本，误写库失去入口）。

        - decision: "confirm" | "cancel"（端点层已用 Literal 白名单校验，此处不重复判定）
        - 返回与 query() 同构的 SSE 异步生成器；本方法刻意不是 async —— 直接把生成器
          交给 StreamingResponse，多包一层协程反而要 await（且无法再流式）
        - query 文本按按钮语义填（"确认"/"取消"）：轨迹、记忆提取与文本兜底判定口径一致
        """
        return self.query("确认" if decision == "confirm" else "取消", thread_id,
                          model=model, staff=staff, confirm_decision=decision)
