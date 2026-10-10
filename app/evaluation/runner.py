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
from app.agent.memory.store import build_memory_store
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
            # [07 S10] 严格解析：非法布尔串（如 "abc"）报错而非静默转 False
            if value.lower() in ("true", "1", "yes"):
                converted = True
            elif value.lower() in ("false", "0", "no"):
                converted = False
            else:
                raise ValueError(
                    f"--features {key}={value} 非法：布尔开关只接受 true/false/1/0/yes/no")
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
    memory_result: dict | None = None    # [06] 基础回忆用例：{"stored","retrieved","recalled"}
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

        # [06] 基础回忆用例分支（memory_setup 存在时走专用流程）
        if case.memory_setup:
            if not app_config.features.memory.long_term:
                return CaseResult(
                    case=case, status="skipped", final_state=None,
                    tracker_summary=None, tracker_records=[],
                    sql_metrics_input=self._sql_input(case, None, None, False, None),
                    error="memory.long_term=false，memory 用例跳过（06 §5）")
            return await self._run_memory_case(
                case, tracker, meta_mgr, dw_mgr, qdrant_mgr, emb_mgr, value_es_repo)
        # [v1.1] 多轮对话：dialogue 逐轮同 thread_id 顺序执行 —— 历史经 context_store 承接
        # （与在线多会话完全同机制），指代消解/条件继承在后续轮自然生效；
        # expected/golden_sql 只对最后一轮评估，成本/延迟覆盖全部轮次（共享 tracker）
        turns = case.dialogue or [case.query]
        state = DataAgentState(                          # 与 query_service.query() 初始值严格一致
            query=turns[0], keywords=[],
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
                    memory_store=build_memory_store(),   # [06] 评测与在线共用同一记忆库（单用户假设）
                )
                # tracker 经 run config 挂载（01 §7.1 预留方案）：节点层 on_chain_start 只有
                # run config 的 callbacks 能触达——by_stage 环节归属的数据来源
                config: dict = {"configurable": {"thread_id": thread_id}}
                if app_config.features.usage_tracking:
                    config["callbacks"] = [tracker]
                final_state = None
                for i, turn in enumerate(turns):
                    if i > 0:
                        # 后续轮重建 state（只带本轮 query；历史在 context_store 里，
                        # 与在线 QueryService 每请求新建 state 完全同构）
                        state = DataAgentState(
                            query=turn, keywords=[],
                            retrieved_column_infos=[], retrieved_metric_infos=[], retrieved_value_infos=[],
                            table_infos=[], metric_infos=[], error="", sql="", retry_count=0,
                            messages=[], intent="", intent_reply="",
                            requested_capability="", capability="", capability_source="", tool_calls=[],
                        )
                        logger.info(f"[eval] 用例 {case.id} 第 {i + 1}/{len(turns)} 轮: {turn}")
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

    async def _ainvoke(self, query: str, thread_id: str, tracker,
                       meta_mgr, dw_mgr, qdrant_mgr, emb_mgr, value_es_repo):
        """单次图执行（memory 用例的 setup/probe 复用入口；与 _run_case 的 context 组装一致）"""
        from app.agent.capabilities.registry import registry
        from app.agent.context import CapabilityHolder, DataAgentContext
        from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
        from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
        from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
        from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository

        state = DataAgentState(
            query=query, keywords=[],
            retrieved_column_infos=[], retrieved_metric_infos=[], retrieved_value_infos=[],
            table_infos=[], metric_infos=[], error="", sql="", retry_count=0,
            messages=[], intent="", intent_reply="",
            requested_capability="", capability="", capability_source="", tool_calls=[],
        )
        async with dw_mgr.session_factory() as dw_session, \
                   meta_mgr.session_factory() as meta_session:
            context = DataAgentContext(
                column_qdrant_repository=ColumnQdrantRepository(qdrant_mgr.client),
                embedding_client=emb_mgr.client,
                metric_qdrant_repository=MetricQdrantRepository(qdrant_mgr.client),
                value_es_repository=value_es_repo,
                meta_mysql_repository=MetaMySQLRepository(meta_session),
                dw_mysql_repository=DWMySQLRepository(dw_session),
                llm=create_llm(self.provider),
                capability_holder=CapabilityHolder(),
                capability_registry=registry,
                usage_tracker=tracker,
                memory_store=build_memory_store(),   # 每次新实例（重读磁盘，持久化可验证）
            )
            config: dict = {"configurable": {"thread_id": thread_id}}
            if tracker:
                config["callbacks"] = [tracker]
            return await graph.ainvoke(input=state, context=context, config=config)

    async def _run_memory_case(self, case: EvaluationCase, tracker,
                               meta_mgr, dw_mgr, qdrant_mgr, emb_mgr, value_es_repo) -> CaseResult:
        """基础回忆用例三阶段（06 §3.5 v1.1 分层）：
        阶段 1  setup_runs 逐条运行 + 逐轮提取
        阶段 2  fresh_store 落盘校验（⚠️ 阶段 1 全部完成后才构建——测"落盘"而非内存态）
        阶段 3  probe（全新 thread_id 跨会话 + 全新 store 实例跨实例代理）；
                检索层（确定性）与输出层（LLM）分开度量——评审意见 3"""
        import json as _json
        import time as _time

        from app.agent.memory.extractor import extract_memories
        from app.agent.memory.store import build_memory_store as _fresh_store
        from app.evaluation.memory_metrics import value_in_text

        base = f"eval_{self.dataset.dataset_id}_{case.id}"
        mem = case.memory_setup or {}
        expected = mem.get("expected_recall") or []

        # ---- 阶段 1: setup runs + 逐轮提取 ----
        for i, setup_query in enumerate(mem.get("setup_runs") or []):
            setup_tracker = LLMUsageTracker(
                provider=self.provider,
                model=app_config.llm.providers[self.provider].get("model", ""))
            final = await self._ainvoke(setup_query, f"{base}_setup{i}",
                                        setup_tracker, meta_mgr, dw_mgr, qdrant_mgr, emb_mgr, value_es_repo)
            store = build_memory_store()           # 每轮重建（读盘最新状态做去重）
            await extract_memories(
                query=setup_query,
                answer=final.get("intent_reply") or "",
                store=store,
                llm=create_llm(app_config.memory.extraction_provider),
                tracker=None,                      # 提取成本不在本用例 tracker 口径（setup 独立）
                embedding_client=emb_mgr.client,
                thread_id=f"{base}_setup{i}",
            )
            await asyncio.sleep(self.interval)

        # ---- 阶段 2: fresh_store 落盘校验（阶段 1 完成后才构建——评审意见 1）----
        fresh_store = _fresh_store()
        contents = {n.content for n in fresh_store.all_notes()}
        contents |= {f for c in fresh_store.all_cards() for f in c.facts}
        stored = all(any(value_in_text(v, s) for s in contents) for v in expected) if expected else True

        # ---- 阶段 3: probe（跨会话新 thread_id）----
        probe_state = await self._ainvoke(case.query, f"{base}_probe",
                                          tracker, meta_mgr, dw_mgr, qdrant_mgr, emb_mgr, value_es_repo)
        output_text = (probe_state.get("intent_reply") or "") + \
            _json.dumps(probe_state.get("result_sample") or [], ensure_ascii=False, default=str)
        recalled = all(value_in_text(v, output_text) for v in expected) if expected else True

        # ---- 检索层（确定性）：期望值条目必须被 probe query 的向量检索召回 ----
        retrieved = False
        try:
            qvec = await emb_mgr.client.aembed_query(case.query)
            ranked = fresh_store.search(qvec, app_config.memory.retrieval_top_k)
            item_texts = []
            for item, _score in ranked:
                if hasattr(item, "subject"):
                    item_texts.append(item.subject + "；" + "；".join(item.facts))
                else:
                    item_texts.append(item.content)
            retrieved = all(any(value_in_text(v, t) for t in item_texts) for v in expected)
        except Exception as e:
            logger.warning(f"[eval] memory 检索层度量失败: {e}")

        if not all([stored, retrieved, recalled]):
            logger.warning(f"[eval] memory 用例 {case.id}: stored={stored} "
                           f"retrieved={retrieved} recalled={recalled}")
        return CaseResult(
            case=case, status="ok", final_state=probe_state,
            tracker_summary=tracker.summary(), tracker_records=tracker.records(),
            sql_metrics_input=self._sql_input(case, None, None, False, None),
            memory_result={"stored": stored, "retrieved": retrieved, "recalled": recalled},
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
                "actual_rows": final_state.get("result_sample") or None,
                "retry_count": final_state.get("retry_count", 0)}
