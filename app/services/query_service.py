"""
问数服务（01/04 文档）

职责：构建 DataAgentState + DataAgentContext（按请求注入 llm/tracker/registry/holder）
→ 驱动 LangGraph 图 → 把节点的 stream_writer 消息包装成 SSE 帧逐段 yield。
"""
import json

from app.agent.capabilities.registry import registry
from app.agent.context import CapabilityHolder, DataAgentContext
from app.agent.graph import graph
from app.agent.llm_factory import create_llm
from app.agent.state import DataAgentState
from app.agent.usage import LLMUsageTracker
from app.conf.app_config import app_config
from app.core.log import logger
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
    ):
        # MySQL 仓储分别负责元数据补全和真实数仓环境信息读取
        self.meta_mysql_repository = meta_mysql_repository
        self.dw_mysql_repository = dw_mysql_repository

        # 召回链路依赖的向量检索、Embedding 和全文检索能力由依赖层注入
        self.embedding_client = embedding_client
        self.column_qdrant_repository = column_qdrant_repository
        self.metric_qdrant_repository = metric_qdrant_repository
        self.value_es_repository = value_es_repository

    async def query(self, query: str, thread_id: str,
                    model: str | None = None, capability: str | None = None):
        """执行一次问数/对话，SSE 流式返回。

        - model: 前端选择的 LLM provider（None/非法 → create_llm 内兜底 default）
        - capability: 前端能力芯片显式选择（tier-0；None → 自动路由）
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
            retry_count=0,
            intent="",
            intent_reply="",
            requested_capability=capability or "",   # [04] tier-0 芯片选择
            capability="",
            capability_source="",
            tool_calls=[],
        )
        holder = CapabilityHolder()                                  # [04] 每请求新建（防串话）
        context = DataAgentContext(
            column_qdrant_repository=self.column_qdrant_repository,
            embedding_client=self.embedding_client,
            metric_qdrant_repository=self.metric_qdrant_repository,
            value_es_repository=self.value_es_repository,
            meta_mysql_repository=self.meta_mysql_repository,
            dw_mysql_repository=self.dw_mysql_repository,
            llm=create_llm(provider_name),   # [01] 按请求实例化（tracker 不挂实例——见下方 config 注释）
            capability_holder=holder,
            capability_registry=registry,
            usage_tracker=tracker,
        )
        try:
            # stream_mode="custom" 对应节点内部 writer(...) 写出的进度消息
            # tracker 经 run config 挂载（01 §7.1 预留方案）：实例级 callback 只能收到 LLM 层事件，
            # 环节归属（by_stage）依赖的 on_chain_start 节点层事件只有 run config 的 callbacks 能触达。
            # 注意不能两处同时挂——同一 tracker 会把每次调用记两次
            config = {"configurable": {"thread_id": thread_id}}
            if tracker:
                config["callbacks"] = [tracker]
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
