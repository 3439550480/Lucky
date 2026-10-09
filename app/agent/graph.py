"""
电商问数 Agent 图编排 —— 能力路由版（04 文档）

链路：START → route_capability（五级递进路由，04 §3.2）→ 按能力 entry 分发：
  dataquery → 原问数 19 节点链路（内部逻辑未动，04 红线）
  default   → default_answer（通用对话，模型自由发挥）
问数链路内：抽取关键词 → 三路召回并行 → 合并 → 表/指标过滤 → 补上下文
→ 生成 SQL → 校验 →（错误）修正循环（最多 max_retries 次）→ 执行 → 解释结果
"""
import asyncio

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.capabilities.registry import registry
from app.agent.capabilities.router import route_capability
from app.agent.context import DataAgentContext
from app.agent.nodes.dataquery.add_extra_context import add_extra_context
from app.agent.nodes.dataquery.correct_sql import correct_sql
from app.agent.nodes.default.default_answer import default_answer
from app.agent.nodes.dataquery.explain_result import explain_result
from app.agent.nodes.dataquery.extract_keywords import extract_keywords
from app.agent.nodes.dataquery.fail import fail
from app.agent.nodes.dataquery.filter_metric import filter_metric
from app.agent.nodes.dataquery.filter_table import filter_table
from app.agent.nodes.dataquery.generate_sql import generate_sql
from app.agent.nodes.dataquery.merge_retrieved_info import merge_retrieved_info
from app.agent.nodes.dataquery.recall_column import recall_column
from app.agent.nodes.dataquery.recall_metric import recall_metric
from app.agent.nodes.dataquery.recall_value import recall_value
from app.agent.nodes.dataquery.run_sql import run_sql
from app.agent.nodes.dataquery.validate_sql import validate_sql
from app.agent.state import DataAgentState
from app.clients.embedding_client_manager import embedding_client_manager
from app.conf.app_config import app_config
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    meta_mysql_client_manager, dw_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository

# StateGraph 声明整张图使用的状态结构和运行时上下文结构
graph_builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)

# ---- 注册节点：问数链路（dataquery 能力，04 红线：内部逻辑未动）----
graph_builder.add_node("extract_keywords", extract_keywords)
graph_builder.add_node("recall_column", recall_column)
graph_builder.add_node("recall_value", recall_value)
graph_builder.add_node("recall_metric", recall_metric)
graph_builder.add_node("merge_retrieved_info", merge_retrieved_info)
graph_builder.add_node("filter_metric", filter_metric)
graph_builder.add_node("filter_table", filter_table)
graph_builder.add_node("add_extra_context", add_extra_context)
graph_builder.add_node("generate_sql", generate_sql)
graph_builder.add_node("validate_sql", validate_sql)
graph_builder.add_node("correct_sql", correct_sql)
graph_builder.add_node("run_sql", run_sql)
graph_builder.add_node("explain_result", explain_result)
graph_builder.add_node("fail", fail)

# ---- 注册节点：能力路由 + default 能力（04 文档）----
graph_builder.add_node("route_capability", route_capability)
graph_builder.add_node("default_answer", default_answer)

# ---- 注册表 fail-fast 校验：能力 entry / routing 兜底必须指向已注册节点（04 §2.2）----
registry.validate_entries(set(graph_builder.nodes))

# START → 能力路由（取代原 intent_classify 五分类）
graph_builder.add_edge(START, "route_capability")


def route_by_capability(state: DataAgentState) -> str:
    """按路由选中的能力分发到其 entry。
    path_map 由 registry 动态构建 —— 新增能力零路由改动（04 文档核心承诺）"""
    capability = state.get("capability") or registry.default_capability
    cap = registry.capabilities.get(capability) or registry.capabilities[registry.default_capability]
    return cap.entry


path_map = {cap.entry: cap.entry for cap in registry.capabilities.values()}
graph_builder.add_conditional_edges(
    source="route_capability",
    path=route_by_capability,
    path_map=path_map,
)

# default 能力：回答即终点
graph_builder.add_edge("default_answer", END)

# ---- 问数链路边（原样保留）----
# 抽取关键词后，三路召回并行扇出
graph_builder.add_edge("extract_keywords", "recall_column")
graph_builder.add_edge("extract_keywords", "recall_value")
graph_builder.add_edge("extract_keywords", "recall_metric")

# 三路召回都完成后，再进入统一的信息合并节点
graph_builder.add_edge("recall_column", "merge_retrieved_info")
graph_builder.add_edge("recall_value", "merge_retrieved_info")
graph_builder.add_edge("recall_metric", "merge_retrieved_info")

# 合并后的候选信息继续拆成表过滤和指标过滤两条线
graph_builder.add_edge("merge_retrieved_info", "filter_table")
graph_builder.add_edge("merge_retrieved_info", "filter_metric")

# 表和指标都过滤完成后，统一补充生成 SQL 所需的上下文
graph_builder.add_edge("filter_table", "add_extra_context")
graph_builder.add_edge("filter_metric", "add_extra_context")
graph_builder.add_edge("add_extra_context", "generate_sql")
graph_builder.add_edge("generate_sql", "validate_sql")


def route_after_validate(state: DataAgentState):
    if state.get("error") is None:
        return "run_sql"
    return "correct_sql"


def route_after_correct(state: DataAgentState):
    max_retries = app_config.sql.max_retries
    retry_count = state.get("retry_count", 0)
    if retry_count < max_retries:
        return "validate_sql"
    else:
        return "fail"


graph_builder.add_conditional_edges(
    source="validate_sql",
    path=route_after_validate,
    path_map={"run_sql": "run_sql", "correct_sql": "correct_sql"},
)

graph_builder.add_conditional_edges(
    source="correct_sql",
    path=route_after_correct,
    path_map={"validate_sql": "validate_sql", "fail": "fail"},
)

graph_builder.add_edge("run_sql", "explain_result")
graph_builder.add_edge("explain_result", END)

# 编译后的 graph 是对外使用的 Agent 执行入口
# [05 §3.5] checkpointer 经存储抽象装配（v1 恒 InMemory；Redis/Sqlite 后端预留接口）
from app.agent.session.session_store import build_session_store  # 局部导入：置于图组装处，来源一目了然
checkpointer = build_session_store().build_checkpointer()
graph = graph_builder.compile(checkpointer=checkpointer)

if __name__ == "__main__":

    async def test():
        """本地调试：走一遍能力路由 + 问数链路"""
        # 多路召回会同时访问 Qdrant、Embedding 和 Elasticsearch，所以测试入口先初始化依赖
        qdrant_client_manager.init()
        embedding_client_manager.init()
        es_client_manager.init()
        meta_mysql_client_manager.init()
        dw_mysql_client_manager.init()

        from app.agent.llm_factory import create_llm
        from app.agent.usage import LLMUsageTracker

        async with (
            meta_mysql_client_manager.session_factory() as meta_session,
            dw_mysql_client_manager.session_factory() as dw_session,
        ):
            meta_mysql_repository = MetaMySQLRepository(meta_session)
            dw_mysql_repository = DWMySQLRepository(dw_session)
            column_qdrant_repository = ColumnQdrantRepository(qdrant_client_manager.client)
            metric_qdrant_repository = MetricQdrantRepository(qdrant_client_manager.client)
            value_es_repository = ValueESRepository(es_client_manager.client)

            tracker = LLMUsageTracker("deepseek", "deepseek-flash")
            holder = type("Holder", (), {"value": None})()

            state = DataAgentState(
                query="统计华北地区的销售总额和平均额",
                keywords=[],
                retrieved_column_infos=[],
                retrieved_metric_infos=[],
                retrieved_value_infos=[],
                table_infos=[],
                metric_infos=[],
                error="",
                requested_capability="",
                capability="",
                capability_source="",
                tool_calls=[],
            )
            context = DataAgentContext(
                column_qdrant_repository=column_qdrant_repository,
                embedding_client=embedding_client_manager.client,
                metric_qdrant_repository=metric_qdrant_repository,
                value_es_repository=value_es_repository,
                meta_mysql_repository=meta_mysql_repository,
                dw_mysql_repository=dw_mysql_repository,
                llm=create_llm("deepseek", usage_tracker=tracker),
                capability_holder=holder,
                capability_registry=registry,
                usage_tracker=tracker,
            )

            async for update in graph.astream(
                    input=state,
                    context=context,
                    config={"configurable": {"thread_id": "debug"}},
                    stream_mode="updates",
            ):
                print(list(update.keys()))

        await qdrant_client_manager.close()
        await es_client_manager.close()
        await meta_mysql_client_manager.close()

    asyncio.run(test())
