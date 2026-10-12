"""
评测执行器（03 文档 §3.6）

职责：应用开关覆盖 → 初始化客户端 → 逐用例独立会话执行图 → 提取最终 state 与 tracker 明细
红线：只读最终 state，零侵入主链路；单用例失败不中断整场评测（status=graph_error 继续）
"""
import asyncio
import datetime
from dataclasses import dataclass, field, asdict

from app.agent.auth.staff_identity import StaffIdentity, build_staff_identity
from app.agent.context import CapabilityHolder, DataAgentContext
from app.agent.graph import graph
from app.agent.llm_factory import create_llm
from app.agent.memory.store import build_memory_store
from app.agent.state import DataAgentState
from app.agent.usage import LLMUsageTracker
from app.conf.app_config import app_config
from app.core.log import logger
from app.evaluation.assertions import evaluate_assertions
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
    assertions: list = field(default_factory=list)   # [R2] 机制断言结果行（evaluation/assertions.py）


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
        from app.repositories.mysql.dw.inventory_write_repository import InventoryWriteRepository
        from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
        from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
        from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
        from app.repositories.mysql.dw.staff_auth_repository import LoginRejected

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
            try:
                return await self._run_memory_case(
                    case, tracker, meta_mgr, dw_mgr, qdrant_mgr, emb_mgr, value_es_repo)
            except Exception as e:
                # [R5 收口] 与普通分支同纪律地容错：memory 分支此前裸调用，任一 memory
                # 用例异常（DB/向量库/图执行）会冒泡到 run() → **整场评测中断**，前序
                # 用例结果全部丢失——与本模块"单用例失败不中断整场"红线相悖（F3 场
                # 10 例连跑时风险最大）。降级为 graph_error 继续后续用例。
                logger.warning(f"[eval] 用例 {case.id} memory 流程失败: {e}")
                return CaseResult(
                    case=case, status="graph_error", final_state=None,
                    tracker_summary=tracker.summary(), tracker_records=tracker.records(),
                    sql_metrics_input=self._sql_input(case, None, None, False, str(e)[:300]),
                    error=str(e)[:500])
        # [v1.1] 多轮对话：dialogue 逐轮同 thread_id 顺序执行 —— 历史经 context_store 承接
        # （与在线多会话完全同机制），指代消解/条件继承在后续轮自然生效；
        # expected/golden_sql 只对最后一轮评估，成本/延迟覆盖全部轮次（共享 tracker）
        turns = case.dialogue or [case.query]
        state = DataAgentState(                          # 与 query_service.query() 初始值严格一致
            query=turns[0], keywords=[],
            retrieved_column_infos=[], retrieved_metric_infos=[], retrieved_value_infos=[],
            table_infos=[], metric_infos=[], error="", sql="", retry_count=0,
            intent="", intent_reply="",
            inherited_conditions="", memory_block="", history_chars=0,   # [R2] 诊断字段
            requested_capability="",                     # 恒空：runner 不带芯片，走自动路由（§3.9 口径）
            capability="", capability_source="", tool_calls=[],
            # [多轮修复] 刻意不传 messages——query_service 同样不传（checkpointer 负责恢复
            # 上一轮轨迹）。此前传 messages=[] 会把 checkpoint 里的历史**覆盖为空**，
            # 多轮用例的指代消解/条件继承在评测里静默失效（实测 router 处 len(messages)==0），
            # 与在线行为不一致 → 多轮结论失真。
        )
        login_rejected = None           # [R2] F4 登录即拒的原因码（disabled/expired/not_found）
        flow_records = None             # [R3] 流水证据（None = 本轮未查询 → 断言判"未评估"）
        try:
            async with dw_mgr.session_factory() as dw_session, \
                       meta_mgr.session_factory() as meta_session:
                # [R1] 身份注入：staff_id 缺省 → None（离线路径，与现状一致）
                try:
                    staff = await self._prepare_staff(dw_session, case.staff_id)
                except LoginRejected as e:
                    # [R2] F4 停用/过期：登录即拒 —— 不进图（与在线登录失败同构），
                    # 但要让断言拿到原因码：记下后交给下方 except 统一产出失败结果
                    login_rejected = e.reason
                    raise
                # [R4] 超长会话填充：与正式轮同 thread_id、同身份（历史自然累积），
                # 位置在身份就绪之后——填充轮也是"这个员工"在说话
                await self._fill_session(case, thread_id, tracker, meta_mgr, dw_mgr,
                                         qdrant_mgr, emb_mgr, value_es_repo, staff)
                context = DataAgentContext(
                    column_qdrant_repository=ColumnQdrantRepository(qdrant_mgr.client),
                    embedding_client=emb_mgr.client,
                    metric_qdrant_repository=MetricQdrantRepository(qdrant_mgr.client),
                    value_es_repository=value_es_repo,
                    meta_mysql_repository=MetaMySQLRepository(meta_session),
                    dw_mysql_repository=DWMySQLRepository(dw_session),
                    # [R3 缺陷修复 2026-10-12] flow_step 第 42 行是硬取键
                    # （runtime.context["inventory_write_repository"]，非 .get）——此处漏装配
                    # 会让**任何**进出 flow_step 的用例抛 KeyError 被降级成 graph_error：
                    # R3 的确认门/幂等/取消三类断言全部测不了（此前只有在线 QueryService
                    # 装了它，离线评测路径漏装 → 机制在评测里"看起来不可用"）。
                    inventory_write_repository=InventoryWriteRepository(dw_session),
                    llm=create_llm(self.provider),   # tracker 不挂实例（与 query_service 同因，见下）
                    capability_holder=CapabilityHolder(),
                    capability_registry=registry,
                    usage_tracker=tracker,
                    memory_store=build_memory_store(),   # [06] 评测与在线共用同一记忆库（单用户假设）
                    staff=staff,                         # [R1] 用例 staff_id 决定；缺省 None
                )
                # tracker 经 run config 挂载（01 §7.1 预留方案）：节点层 on_chain_start 只有
                # run config 的 callbacks 能触达——by_stage 环节归属的数据来源
                config: dict = {"configurable": {"thread_id": thread_id}}
                if app_config.features.usage_tracking:
                    config["callbacks"] = [tracker]
                final_state = None
                # [R5] 记忆断言 extra：开关未开或未提取时保持 None（断言判为"未评估"）
                mem_extra: dict = {"memory_writes": None, "memory_added": None}
                for i, turn in enumerate(turns):
                    # [R3] 本轮流程控制注入（slots 补位 / confirm 决策）——与在线按钮点击同通道
                    slots, confirm = self._turn_control(case, i)
                    if i > 0:
                        # 后续轮重建 state（只带本轮 query；历史在 context_store 里，
                        # 与在线 QueryService 每请求新建 state 完全同构）
                        state = DataAgentState(
                            query=turn, keywords=[],
                            retrieved_column_infos=[], retrieved_metric_infos=[], retrieved_value_infos=[],
                            table_infos=[], metric_infos=[], error="", sql="", retry_count=0,
                            intent="", intent_reply="",
                            inherited_conditions="", memory_block="", history_chars=0,   # [R2] 诊断字段
                            requested_capability="", capability="", capability_source="", tool_calls=[],
                            confirm_decision=confirm or "",                             # [R3]
                            # [R3] 刻意**不写 pending_action 键**（区别于 None）：不写 = 由
                            # checkpointer 恢复上一轮的 pending（含 awaiting_confirm=True 的
                            # 确认门状态）→ 与本轮 confirm_decision 组合即在线"点确认按钮"的
                            # 同构路径。写成 None 会把等待确认的状态冲掉：flow_step 的写库
                            # 分支永远不可达（实测复现：只重推确认卡片，pending 停在原位）。
                            # [多轮修复] 同首轮：不传 messages，历史由 checkpointer 按 thread_id 恢复
                        )
                        if slots:                                   # [R3] 仅显式补位时覆盖槽位
                            state["pending_action"] = {"slots": slots}
                        logger.info(f"[eval] 用例 {case.id} 第 {i + 1}/{len(turns)} 轮: {turn}")
                    elif slots or confirm:
                        # 首轮 state 在循环外构建（与 query_service 初始值严格一致），
                        # 这里只补写流程注入，不改其它字段
                        if slots:
                            state["pending_action"] = {"slots": slots}
                        if confirm:
                            state["confirm_decision"] = confirm
                    mem_before = self._memory_snapshot()      # [R5] 提取前快照（差分基准）
                    final_state = await graph.ainvoke(
                        input=state, context=context,
                        config=config)
                    mem_extra = await self._extract_after_turn(   # [R5] 复刻在线运行后提取
                        turn, final_state, thread_id, staff, mem_before, emb_mgr)
                # [R3] 写库结果证据：仅对声明了 expect_flow_doc 的用例查一次真实流水。
                # 为什么必须查库而非读 state：提交成功、用户取消、确认超时在 state 里都
                # 是 pending_action=None —— 只断槽位清空无法区分"真的记了账"和"被取消了"。
                # 沿用同一 dw_session（commit_flow 已提交，本会话可见）；查库异常 → 保持
                # None（未评估，不伪造结论）；查到无该单据 → {}（由 committed 决定判通过与否）
                if case.expect_flow_doc:
                    from app.repositories.mysql.dw.inventory_write_repository import (
                        InventoryWriteRepository,
                    )
                    try:
                        row = await InventoryWriteRepository(dw_session).get_flow(
                            case.expect_flow_doc["doc_no"])
                        flow_records = row or {}
                    except Exception as e:
                        logger.warning(f"[eval] 用例 {case.id} 流水查询失败: {e}")
        except Exception as e:                           # 单用例失败不中断（§3.6 容错）
            logger.warning(f"[eval] 用例 {case.id} 图执行失败: {e}")
            return CaseResult(
                case=case, status="graph_error", final_state=None,
                tracker_summary=tracker.summary() if app_config.features.usage_tracking else None,
                tracker_records=tracker.records(),
                sql_metrics_input=self._sql_input(case, None, None, False, str(e)[:300]),
                error=str(e)[:500],
                # [R2] 失败路径同样出断言：权限/越权类断言本就发生在"非 ok"路径上
                # （login_rejected 供 F4 停用/过期用例判定）
                assertions=evaluate_assertions(case, None, {
                    "login_rejected": login_rejected,
                    "staff_id": case.staff_id,          # [R3] 经办人比对基准
                    "flow_records": None,
                    "answer_text": None}))              # [F9] 图未跑通 → 无回答文本（未评估）
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
            # [R2] 机制断言（口径见 evaluation/assertions.py）；[R5] 记忆快照走 extra；
            # [R3] 流水 + 经办人走 extra（staff_id 即比对基准）
            assertions=evaluate_assertions(case, final_state, {
                "login_rejected": None,
                "staff_id": case.staff_id,
                "flow_records": flow_records,
                "answer_text": self._answer_text(final_state),   # [F9] 对账文本（与 SSE 同源）
                **mem_extra}),
        )

    async def _prepare_staff(self, dw_session, staff_id: str | None) -> StaffIdentity | None:
        """[R1] 按用例 staff_id 从 dim_staff 组装身份（缺省 None = 离线路径）

        谁调用：_run_case 组装 DataAgentContext 时（R5 的 memory_setup 路径复用同一函数）
        链路位置：run → _run_case →（本函数）→ DataAgentContext(staff=...)
                  → 图内节点 runtime.context["staff"].has(权限点)
        设计 why：
        ① 与在线同构靠"同一函数"：组装走 build_staff_identity、校验走
           StaffAuthRepository.check_validity —— 在线登录与评测注入共用，无法各写一套。
        ② 校验不通过**显式失败**，绝不静默注入一个"假身份"：否则 F4 的
           "S007 停用 / T005 过期 → 登录即拒"会变成假通过（评测里照样放行）。
        ③ 缺省 None 保持现状：离线路径不渲染角色段（session/prefix.py 已声明此为预期）。

        示例：await self._prepare_staff(dw_session, "S001")
              → StaffIdentity(staff_id="S001", role_codes=["STAFF"], permissions={...})
              await self._prepare_staff(dw_session, "S007")   # 停用 → LoginRejected
        """
        if not staff_id:
            return None
        # 局部导入：与文件其余仓储导入风格一致（保持模块可在无 DB 配置下被导入）
        from app.repositories.mysql.dw.staff_auth_repository import (
            LoginRejected,
            StaffAuthRepository,
        )

        repository = StaffAuthRepository(dw_session)
        row = await repository.get_staff(staff_id)
        if row is None:
            raise LoginRejected("not_found", f"用例 staff_id 不存在于 dim_staff：{staff_id}")
        reason = repository.check_validity(row, datetime.date.today())
        if reason:
            # 停用/过期：与在线登录同样拒绝（F4 断言的正是这条路径）
            raise LoginRejected(reason, f"用例 staff 校验不通过（{reason}）：{staff_id}")
        logger.info(f"[eval] staff 注入 {staff_id}（角色 {row['role_codes']}）")
        return build_staff_identity(row)

    @staticmethod
    def _turn_control(case, index: int) -> tuple[dict | None, str | None]:
        """[R3] 取第 index 轮的流程控制注入（slots / confirm），未声明或越界 → (None, None)

        谁调用：_run_case 的逐轮循环
        设计 why：流程用例的槽位不该靠 LLM 抽槽（不确定）——像在线"按钮点击"那样走
        **结构化输入通道**：slots 直进 pending_action、confirm 直进 confirm_decision。
        这样测的是流程机本身（槽位承接 / 确认 / 取消），不是抽槽模型的运气；
        越界返回 None 而不是抛错：单轮用例也能带 slots_sequence，取不到就是不注入。
        [R3 口径对齐] 数据集用人类语义的 accept/cancel，state 通道用流程机与前端协议的
        confirm/cancel（flow_step 消费的就是 "confirm"）——映射收敛在此一处，数据集侧
        保持可读；若原样透传 "accept"，确认门会判为"尚未决定"而**只重推卡片不写库**
        （写库分支静默不可达 → F5 场全体假失败）。
        示例：_turn_control(case, 0) → ({"direction": "in", "sku_id": "15262011"}, None)
              _turn_control(case, 1) → (None, "confirm")     # 数据集处写的是 "accept"
        """
        slots = case.slots_sequence or []
        confirms = case.confirm_sequence or []
        raw = confirms[index] if index < len(confirms) else None
        confirm = {"accept": "confirm"}.get(raw or "", raw)
        return (slots[index] if index < len(slots) else None, confirm)

    @staticmethod
    def _memory_snapshot() -> dict:
        """[R5] 记忆库快照 {条目id: (scope, owner_id)}（notes + cards 合并）

        谁调用：_run_case 每轮（提取前）+ _extract_after_turn（提取后）差分
        设计 why：断言"落库 scope / owner"必须看**真实落库结果**，而不是解析提取模型的
        输出——快照差分同时给出新增条数与层级归属，且与在线共用同一 memory_store，
        不会出现"评测的库和线上的库不是同一个"这类失真。
        示例：_memory_snapshot() → {"n_3f2a": ("personal", "S001")}
        """
        store = build_memory_store()
        snapshot: dict = {}
        for note in store.all_notes():
            snapshot[note.id] = (note.scope, note.owner_id)
        for card in store.all_cards():
            snapshot[card.id] = (card.scope, card.owner_id)
        return snapshot

    async def _extract_after_turn(self, turn: str, final_state: dict, thread_id: str,
                                  staff, mem_before: dict, emb_mgr) -> dict:
        """[R5] 单轮结束后复刻在线侧的运行后记忆提取，并做快照差分 → 断言 extra

        谁调用：_run_case 每轮图执行之后
        链路位置：graph.ainvoke →（本函数：extract_memories + 快照差分）→ 断言 extra
        设计 why：
        ① 提取这一步在**图外**（query_service 的 finally 里），评测若不复刻，F3 的
           "说'负责鞋类'→ 断言落 personal"就永远测不到（图里根本没有写记忆的逻辑）；
        ② 开关关闭或提取异常时返回 {"memory_writes": None, ...}（**未评估**）而不是
           "0 条新增"——否则 expect_zero_extract 会在功能没开时静默真通过；
        ③ 入参与在线逐字对齐（answer 取本轮话术、staff 取注入身份），保证层级门禁
           在评测里与在线是同一次判定。
        示例：await self._extract_after_turn("我负责鞋类", fs, "eval_f3_s001_t1",
                                          staff, before, emb_mgr)
              → {"memory_writes": [("personal", "S001")], "memory_added": 1}
        """
        if not (app_config.features.memory.long_term and app_config.memory.extract_after_run):
            return {"memory_writes": None, "memory_added": None}   # 开关未开 → 断言未评估
        from app.agent.memory.extractor import extract_memories        # 局部导入：重型依赖

        try:
            await extract_memories(
                query=turn,
                answer=final_state.get("intent_reply") or "",
                store=build_memory_store(),
                llm=create_llm(app_config.memory.extraction_provider),
                tracker=None,
                embedding_client=emb_mgr.client,
                thread_id=thread_id,
                staff=staff,
            )
        except Exception as e:                       # 提取失败不得中断用例，但也不谎报"无新增"
            logger.warning(f"[eval] 记忆提取失败（本轮记忆断言判为未评估）: {e}")
            return {"memory_writes": None, "memory_added": None}
        writes = [v for k, v in self._memory_snapshot().items() if k not in mem_before]
        return {"memory_writes": writes, "memory_added": len(writes)}

    async def _fill_session(self, case, thread_id: str, tracker, meta_mgr, dw_mgr,
                            qdrant_mgr, emb_mgr, value_es_repo, staff=None) -> None:
        """[R4] 超长会话构造器：正式轮次之前，在同一 thread_id 上灌 N 轮填充对话

        谁调用：_run_case 进入轮次循环之前（try 块内，失败按 graph_error 处理）
        设计 why：F6 要验证的是"历史变长后预算守卫 / brief 是否生效"，所以填充轮必须走
        **与正式轮完全相同的链路**（_ainvoke：同 context、同 thread_id、同 tracker）——
        直接往 context_store 塞假历史会绕过渲染路径，测不出真实字符增长；
        填充轮费用计入本用例 tracker（F6 的成本断言正是要看到它）。
        示例：case.session_filler = {"turns": 24, "pool": ["你好", "断码清仓还剩几件"]}
        """
        filler = case.session_filler or {}
        turns = int(filler.get("turns") or 0)
        pool = [q for q in (filler.get("pool") or []) if q]
        if not turns or not pool:
            return
        logger.info(f"[eval] 用例 {case.id} 灌入 {turns} 轮填充会话（F6 预算守卫）")
        for i in range(turns):
            await self._ainvoke(pool[i % len(pool)], thread_id, tracker,
                                meta_mgr, dw_mgr, qdrant_mgr, emb_mgr, value_es_repo, staff)

    async def _ainvoke(self, query: str, thread_id: str, tracker,
                       meta_mgr, dw_mgr, qdrant_mgr, emb_mgr, value_es_repo, staff=None):
        """单次图执行（memory 用例的 setup/probe、R4 填充轮复用入口；与 _run_case 的 context 组装一致）

        [R5] staff：身份注入（缺省 None = 离线路径）。setup/probe 轮必须与正式轮同身份，
        否则 F3 的门店层写入门禁在 setup 阶段就已经判错层级——用例失败时会误判成
        "提取器写错层"，实际是评测没给身份。

        [R4 修复] 刻意**不传 messages**（此前的 messages=[] 是 R4 的致命缺陷）：
        messages 在 DataAgentState 里是普通覆盖式通道，传 [] 会在每次 invoke 入口把
        checkpointer 里累积的轨迹**冲成空**——填充轮各自"只看得见自己"，历史永远不涨，
        `expect_history_len_le` 于是平凡通过（假通过），F6 想测的预算守卫/brief 完全没被
        触达。与 _run_case 多轮修复同因同解（那里也是"实测 len(messages)==0"才发现的）：
        不传该键 = 由 checkpointer 按 thread_id 恢复；fresh thread 下等价于空（与
        query_service 首轮形态一致）。
        """
        from app.agent.capabilities.registry import registry
        from app.agent.context import CapabilityHolder, DataAgentContext
        from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
        from app.repositories.mysql.dw.inventory_write_repository import InventoryWriteRepository
        from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
        from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
        from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository

        state = DataAgentState(
            # [R4 缺陷修复 2026-10-12] 此处**不得**出现 messages=[],——messages 是覆盖式通道，
            # 传 [] 会在每轮入口把 checkpointer 累积的轨迹冲成空，填充轮历史永远不涨，
            # 断言 expect_history_len_le 平凡通过（假通过）。见 docstring [R4 修复]。
            query=query, keywords=[],
            retrieved_column_infos=[], retrieved_metric_infos=[], retrieved_value_infos=[],
            table_infos=[], metric_infos=[], error="", sql="", retry_count=0,
            intent="", intent_reply="",
            inherited_conditions="", memory_block="", history_chars=0,   # [R2] 诊断字段
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
                # [R3] 与 _run_case 同因同解：填充轮若命中写意图（flow_guard → flow_step）
                # 也会硬取该键，缺装配同样 KeyError。两处必须同步。
                inventory_write_repository=InventoryWriteRepository(dw_session),
                llm=create_llm(self.provider),
                capability_holder=CapabilityHolder(),
                capability_registry=registry,
                usage_tracker=tracker,
                memory_store=build_memory_store(),   # 每次新实例（重读磁盘，持久化可验证）
                staff=staff,                         # [R5] 身份透传（F3 层级门禁 / 隔离用例依赖）
            )
            config: dict = {"configurable": {"thread_id": thread_id}}
            if tracker:
                config["callbacks"] = [tracker]
            return await graph.ainvoke(input=state, context=context, config=config)

    async def _run_memory_case(self, case: EvaluationCase, tracker,
                               meta_mgr, dw_mgr, qdrant_mgr, emb_mgr, value_es_repo) -> CaseResult:
        """基础回忆用例四阶段（06 §3.5 v1.1 分层 + [R5] 身份注入）：
        阶段 0  [R5] 身份注入（case.staff_id → StaffIdentity；缺省 None = 离线路径）
        阶段 1  setup_runs 逐条运行 + 逐轮提取（提取带同一身份——门店层门禁的依据）
        阶段 2  fresh_store 落盘校验（⚠️ 阶段 1 全部完成后才构建——测"落盘"而非内存态）
        阶段 3  probe（全新 thread_id 跨会话 + 全新 store 实例跨实例代理）；
                检索层（确定性）与输出层（LLM）分开度量——评审意见 3

        [R5] 身份为什么必须贯穿 setup/probe 三段：F3 的断言"TEMP 说'我们店规则是 X'
        → 落 personal（门禁降级）""S001 说'负责鞋类'→ 个人层 owner=S001"都取决于
        **注入的是谁**——extract_memories 用 staff 判降级与 owner_id。缺身份时
        门禁恒降级 personal、owner_id 变空串，用例会把"评测没给身份"误判成"提取器写错层"。

        [R5] 落库断言的数据源：setup 逐轮**快照差分**（提取前 vs 提取后）取真实落库
        的 (scope, owner_id) → assertions 的 memory_writes/memory_added；setup_runs
        为空时给 None（未评估），不谎报"0 条新增"。

        示例：case.memory_setup = {"setup_runs": ["我负责鞋类"], "expected_recall": ["鞋类"]}
              case.staff_id = "S001"  → 断言 expect_memory_scope=personal → owner=S001
        """
        import json as _json
        import time as _time

        from app.agent.memory.extractor import extract_memories
        from app.agent.memory.store import build_memory_store as _fresh_store
        from app.evaluation.memory_metrics import value_in_text
        from app.repositories.mysql.dw.staff_auth_repository import LoginRejected

        base = f"eval_{self.dataset.dataset_id}_{case.id}"
        mem = case.memory_setup or {}
        expected = mem.get("expected_recall") or []

        # ---- 阶段 0: [R5] 身份注入（与 _run_case 同一函数 _prepare_staff，判定同源）----
        try:
            async with dw_mgr.session_factory() as dw_session:
                staff = await self._prepare_staff(dw_session, case.staff_id)
        except LoginRejected as e:
            # 停用/过期：与在线登录同构地"不进图"，但仍产出断言（F4 口径，不静默跳过）
            logger.warning(f"[eval] memory 用例 {case.id} 登录即拒：{e.reason}")
            return CaseResult(
                case=case, status="graph_error", final_state=None,
                tracker_summary=None, tracker_records=[],
                sql_metrics_input=self._sql_input(case, None, None, False, None),
                error=f"LoginRejected: {e.reason}",
                assertions=evaluate_assertions(case, None, {"login_rejected": e.reason}),
            )

        # ---- 阶段 1: setup runs + 逐轮提取（[R5] 逐轮快照差分 = 真实落库条目）----
        setup_runs = mem.get("setup_runs") or []
        setup_writes: list = []                        # [(scope, owner_id), ...]
        for i, setup_query in enumerate(setup_runs):
            setup_tracker = LLMUsageTracker(
                provider=self.provider,
                model=app_config.llm.providers[self.provider].get("model", ""))
            final = await self._ainvoke(setup_query, f"{base}_setup{i}",
                                        setup_tracker, meta_mgr, dw_mgr, qdrant_mgr, emb_mgr,
                                        value_es_repo, staff)
            before = self._memory_snapshot()          # [R5] 提取前快照（差分基准）
            store = build_memory_store()           # 每轮重建（读盘最新状态做去重）
            await extract_memories(
                query=setup_query,
                answer=final.get("intent_reply") or "",
                store=store,
                llm=create_llm(app_config.memory.extraction_provider),
                tracker=None,                      # 提取成本不在本用例 tracker 口径（setup 独立）
                embedding_client=emb_mgr.client,
                thread_id=f"{base}_setup{i}",
                staff=staff,                       # [R5] 降级门禁 / owner_id 的依据
            )
            # [R5] 差分取新增条目（跨轮累积：多条 setup 都写入时逐条并入）
            setup_writes.extend(v for k, v in self._memory_snapshot().items() if k not in before)
            await asyncio.sleep(self.interval)

        # ---- 阶段 2: fresh_store 落盘校验（阶段 1 完成后才构建——评审意见 1）----
        fresh_store = _fresh_store()
        contents = {n.content for n in fresh_store.all_notes()}
        contents |= {f for c in fresh_store.all_cards() for f in c.facts}
        stored = all(any(value_in_text(v, s) for s in contents) for v in expected) if expected else True

        # ---- 阶段 3: probe（跨会话新 thread_id）----
        probe_state = await self._ainvoke(case.query, f"{base}_probe",
                                          tracker, meta_mgr, dw_mgr, qdrant_mgr, emb_mgr,
                                          value_es_repo, staff)   # [R5] 同身份（可见性过滤依赖它）
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
            # [R5] 机制断言：机制字段走 probe 终态（如 expect_memory_injected），
            # 落库 scope/owner 走 setup 差分 extra；无 setup_runs → None = 未评估（不假通过）
            assertions=evaluate_assertions(case, probe_state, {
                "login_rejected": None,
                "memory_writes": setup_writes if setup_runs else None,
                "memory_added": len(setup_writes) if setup_runs else None,
                "answer_text": self._answer_text(probe_state),    # [F9] probe 轮回答
            }),
        )

    @staticmethod
    def _answer_text(final_state: dict | None) -> str | None:
        """[F9] 取"店长实际看到的那段回答文本"（解释层 / 兜底与拒绝话术）

        谁调用：_run_case 组装断言 extra 的两条路径（正常与失败）
        链路位置：图终态 messages →（本函数）→ assertions 的 extra["answer_text"]
                  → _check_explanation_grounded 数字对账
        设计 why：F9 的对账对象就是这段文本本身，必须取与在线同源的那一份——
        优先取轨迹里最后一条 assistant 消息（explain_result / default_answer 都写这里，
        与前端 SSE 推的是同一段字符串，见 context_store.append_assistant_message）；
        无 assistant 轨迹时退到 intent_reply（权限拒绝 / 兜底节点只写它）。
        取不到（图未执行/无回答）返回 None → 断言判"未评估"，不假通过。

        示例：EvaluationRunner._answer_text({"messages": [{"role": "assistant",
                                                          "content": "鞋类共 138,602 件"}]})
              → "鞋类共 138,602 件"
        """
        state = final_state or {}
        for msg in reversed(state.get("messages") or []):
            if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("content"):
                return str(msg["content"])
        return state.get("intent_reply") or None

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
