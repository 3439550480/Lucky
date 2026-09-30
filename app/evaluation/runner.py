"""
评测执行器（03 文档 §3.6）

职责：应用开关覆盖 → 初始化客户端 → 逐用例独立会话执行图 → 提取最终 state 与 tracker 明细
红线：只读最终 state，零侵入主链路；单用例失败不中断整场评测（status=graph_error 继续）
"""
import asyncio
from dataclasses import dataclass, field, asdict

from app.agent.context import CapabilityHolder, DataAgentContext
from app.agent.graph import graph
from app.agent.llm_factory import create_llm
from app.agent.state import DataAgentState
from app.agent.usage import LLMUsageTracker
from app.conf.app_config import app_config
from app.core.log import logger
from app.evaluation.dataset import EvaluationCase, EvaluationDataset


def apply_feature_overrides(overrides: dict[str, str]) -> None:
    """把 --features key=value 应用到 app_config.features（运行时 mutation，§3.6——
    仅存在于评测进程，不影响在线服务与配置文件；key 用点路径，如 memory.short_term）"""
    for key, value in overrides.items():
        parts = key.split(".")
        obj = app_config.features
        for attr in parts[:-1]:                      # 逐级下钻（支持 memory.short_term 等嵌套）
            obj = getattr(obj, attr)
        current = getattr(obj, parts[-1])
        # 按字段现值类型转换（bool/int/float/str），转换失败直接抛错——实验配置错误要显式暴露
        if isinstance(current, bool):
            converted = value.lower() in ("true", "1", "yes")
        elif isinstance(current, int):
            converted = int(value)
        elif isinstance(current, float):
            converted = float(value)
        else:
            converted = value
        setattr(obj, parts[-1], converted)
        logger.info(f"[eval] 开关覆盖 {key} = {converted}")
    # usage_tracking 强制开启（§5.2：成本指标数据源，无论配置文件与覆盖值）
    app_config.features.usage_tracking = True


def flags_snapshot() -> dict:
    """完整 flags 快照（§5.2：未覆盖的开关也记录生效值——对比实验可复现的前提）"""
    return asdict(app_config.features)


@dataclass
class CaseResult:
    case: EvaluationCase
    status: str                                      # "ok" | "graph_error" | "skipped"
    final_state: dict | None
    tracker_summary: dict | None
    tracker_records: list = field(default_factory=list)   # LLMCallRecord 列表（cost 逐调用判档用）
    sql_metrics_input: dict = field(default_factory=dict)
    error: str | None = None


class EvaluationRunner:
    def __init__(self, dataset: EvaluationDataset, provider: str | None,
                 feature_overrides: dict[str, str], interval: int,
                 pricing_tier: str | None = None):
        # step 1: 保存评测参数（pricing_tier: None=auto 按时间判定；peak/offpeak=强制固定，§7.3）
        self.dataset = dataset
        self.provider = provider if provider in app_config.llm.providers else app_config.llm.default
        self.interval = interval
        self.pricing_tier = pricing_tier
        # step 2: 应用开关覆盖（构造时即生效，run() 前可校验 flags_snapshot）
        apply_feature_overrides(feature_overrides or {})

    async def run(self) -> list[CaseResult]:
        """主流程：init 客户端 → 逐用例独立会话 ainvoke → 提取 → finally 关闭全部连接"""
        # step 1: 客户端初始化（复用 build_meta_knowledge.py 的 manager 单例模式，03 §4 说明）
        from app.clients.embedding_client_manager import embedding_client_manager
        from app.clients.es_client_manager import es_client_manager
        from app.clients.mysql_client_manager import dw_mysql_client_manager, meta_mysql_client_manager
        from app.clients.qdrant_client_manager import qdrant_client_manager
        from app.repositories.es.value_es_repository import ValueESRepository

        meta_mysql_client_manager.init()
        dw_mysql_client_manager.init()
        qdrant_client_manager.init()
        embedding_client_manager.init()
        try:
            es_client_manager.init()
            value_es_repository = ValueESRepository(es_client_manager.client)
        except Exception as e:
            logger.warning(f"Elasticsearch 不可用，取值召回通道将失败: {e}")
            value_es_repository = None

        results: list[CaseResult] = []
        try:
            for idx, case in enumerate(self.dataset.cases):
                logger.info(f"[eval] ({idx + 1}/{len(self.dataset.cases)}) 用例 {case.id}: {case.query}")
                results.append(await self._run_case(
                    case, meta_mysql_client_manager, dw_mysql_client_manager,
                    qdrant_client_manager, embedding_client_manager, value_es_repository))
                if self.interval > 0:
                    await asyncio.sleep(self.interval)       # 限流保护（LLM 速率限制）
        finally:
            # 全部用例结束后统一关闭连接（manager.close 幂等；
            # embedding manager 无 close 方法——与 build_meta_knowledge.py 行为一致，防御性跳过）
            await meta_mysql_client_manager.close()
            await dw_mysql_client_manager.close()
            await qdrant_client_manager.close()
            emb_close = getattr(embedding_client_manager, "close", None)
            if emb_close:
                await emb_close()
            await es_client_manager.close()
        return results

    async def _run_case(self, case: EvaluationCase, meta_mgr, dw_mgr,
                        qdrant_mgr, emb_mgr, value_es_repo) -> CaseResult:
        """单用例：独立 Session + 独立 thread_id + 独立 tracker（会话隔离，用例间零污染）"""
        from app.agent.capabilities.registry import registry
        from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
        from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
        from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
        from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository

        thread_id = f"eval_{self.dataset.dataset_id}_{case.id}"
        tracker = LLMUsageTracker(provider=self.provider,
                                  model=app_config.llm.providers[self.provider].get("model", ""))
        state = DataAgentState(                          # 与 query_service.query() 初始值严格一致
            query=case.query, keywords=[],
            retrieved_column_infos=[], retrieved_metric_infos=[], retrieved_value_infos=[],
            table_infos=[], metric_infos=[], error="", sql="", retry_count=0,
            messages=[], intent="", intent_reply="",
            requested_capability="",                     # 恒空：runner 不带芯片，走自动路由（§3.9 口径）
            capability="", capability_source="", tool_calls=[],
        )
        try:
            async with dw_mgr.session_factory() as dw_session, \
                       meta_mgr.session_factory() as meta_session:
                context = DataAgentContext(
                    column_qdrant_repository=ColumnQdrantRepository(qdrant_mgr.client),
                    embedding_client=emb_mgr.client,
                    metric_qdrant_repository=MetricQdrantRepository(qdrant_mgr.client),
                    value_es_repository=value_es_repo,
                    meta_mysql_repository=MetaMySQLRepository(meta_session),
                    dw_mysql_repository=DWMySQLRepository(dw_session),
                    llm=create_llm(self.provider),   # tracker 不挂实例（与 query_service 同因，见下）
                    capability_holder=CapabilityHolder(),
                    capability_registry=registry,
                    usage_tracker=tracker,
                )
                # tracker 经 run config 挂载（01 §7.1 预留方案）：节点层 on_chain_start 只有
                # run config 的 callbacks 能触达——by_stage 环节归属的数据来源
                config: dict = {"configurable": {"thread_id": thread_id}}
                if app_config.features.usage_tracking:
                    config["callbacks"] = [tracker]
                final_state = await graph.ainvoke(
                    input=state, context=context,
                    config=config)
        except Exception as e:                           # 单用例失败不中断（§3.6 容错）
            logger.warning(f"[eval] 用例 {case.id} 图执行失败: {e}")
            return CaseResult(
                case=case, status="graph_error", final_state=None,
                tracker_summary=tracker.summary() if app_config.features.usage_tracking else None,
                tracker_records=tracker.records(),
                sql_metrics_input=self._sql_input(case, None, None, False, str(e)[:300]),
                error=str(e)[:500])
        # golden_sql 执行（§5.1：sql_metrics 关闭时跳过，golden 不执行）
        golden_rows = None
        if app_config.features.evaluation.sql_metrics and case.golden_sql:
            try:
                async with dw_mgr.session_factory() as dw_session:
                    golden_rows = await DWMySQLRepository(dw_session).run(case.golden_sql)
            except Exception as e:
                logger.warning(f"[eval] 用例 {case.id} golden_sql 执行失败: {e}")
                golden_rows = None
        return CaseResult(
            case=case, status="ok", final_state=final_state,
            tracker_summary=tracker.summary(), tracker_records=tracker.records(),
            sql_metrics_input=self._sql_input(case, final_state, golden_rows, True, None),
        )

    @staticmethod
    def _sql_input(case: EvaluationCase, final_state, golden_rows, executed, exec_error) -> dict:
        """组装 sql_metrics 输入行（§3.3 契约）"""
        if not executed or final_state is None:
            return {"has_golden": case.golden_sql is not None, "executed": False,
                    "exec_error": exec_error, "golden_rows": None, "actual_rows": None,
                    "retry_count": 0}
        return {"has_golden": case.golden_sql is not None, "executed": True,
                "exec_error": None,
                "golden_rows": golden_rows,
                "actual_rows": final_state.get("result") or None,
                "retry_count": final_state.get("retry_count", 0)}
