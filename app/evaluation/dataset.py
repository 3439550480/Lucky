"""
评测集加载与校验（03 文档 §3.5）

职责：JSON → 强类型 dataclass，错误带定位信息抛 ValueError（§2.1：不静默跳过）
被检字段口径：expected.columns 格式与 ColumnInfo.id 一致（表名.字段名），
加载器不做格式强制（§7.1——避免过度耦合，匹配不到时指标自然为 0）
"""
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EvaluationCase:
    id: str
    query: str
    capability: str = "dataquery"      # 缺省视为 dataquery（§2.1）
    staff_id: str | None = None        # [R1] 员工身份注入：runner 按 dim_staff 组装
                                       # StaffIdentity（缺省 None = 离线路径，不渲染角色段）
    expected: dict = field(default_factory=dict)   # intent/tools/columns/metrics/values/tables，缺省项为空
    golden_sql: str | None = None      # 缺省则不计入 SQL 正确性（仍计入可执行率）
    match_mode: str = "exact"          # 继承 defaults
    k: int = 5                         # 继承 defaults
    notes: str = ""
    memory_setup: dict | None = None   # [06] 基础回忆用例：{"setup_runs": [...], "expected_recall": [...], "expect_note": bool}
    dialogue: list[str] | None = None  # [v1.1] 多轮对话用例：逐轮同 thread_id 顺序执行（SParC/CHASE 范式）；
                                       # expected/golden_sql 只对最后一轮评估，历史经 context_store 自然承接

    # ---- [R2] 机制断言字段（缺省 None = 该维度不断言）----
    # 判定逻辑全部在 evaluation/assertions.py；字段名即断言键（报告与数据集同名，排查免翻译）
    expect_permission_denied: str | None = None     # 权限矩阵：本轮被拒 + 被拒权限点
    # [F4] 权限矩阵的另一半：true = 本轮必须放行（未被权限拒绝、也未被登录门禁拦下）
    expect_permission_allowed: bool | None = None
    expect_sql_tables: list[str] | None = None      # SQL 涉及表集合（治多轮串台）
    expect_sql_not_tables: list[str] | None = None  # SQL 禁忌表集合
    expect_inherited: dict | None = None            # 继承条件块需含的 `键：值` 子串对
    # [F2] 继承条件块**不得**含这些键（条件推翻："不要鞋类了，全部品类" → 品类不得再继承）
    expect_inherited_absent: list[str] | None = None
    expect_memory_injected: bool | None = None      # 记忆块是否进了提示词
    expect_memory_scope: str | None = None          # 本轮落库条目 scope（依赖 runner 记忆快照）
    expect_zero_extract: bool | None = None         # 本轮无新增记忆（同上）
    expect_pending_slots: dict | None = None        # 流程槽位（{} = 无进行中流程）
    expect_history_len_le: int | None = None        # 历史区字符数上限（brief / 预算守卫）
    expect_login_rejected: str | None = None        # F4 停用/过期：拒绝原因码（disabled/expired）
    # [R3] 出入库写库结果：{"doc_no": str, "committed": bool(缺省 true)}。
    # committed=true  → 期望该单据已落 fact_inventory_flow 流水，且经办人 = 注入身份
    # committed=false → 期望**未**记账（取消/超时/幂等重复，均不得新增流水）
    # 证据由 runner 查流水提供：state 里提交与取消都表现为 pending_action=None，
    # 只断槽位清空无法区分"真的记了账"与"被取消了"（见 runner 注释）
    expect_flow_doc: dict | None = None
    # [F9 答案质量] 解释文本数字对账（防幻觉硬约束）：解释里出现的每个数字都必须能在本轮
    # state 的真实来源里找到（result_sample / result_total / 查询句 / 日期上下文 / SQL 常量）。
    # true = 期望全部对账得上（复述型问句、注入对抗里的"别采信用户报的数字"）；
    # false = 期望存在无来源数字（反例形态，语义对称保留）。
    # 判定纯字符串/数值匹配，零 LLM（方案 6.2 规则级）。
    expect_explanation_grounded: bool | None = None

    # ---- [R3] 流程用例：槽位与确认的结构化注入（与 dialogue 同序）----
    slots_sequence: list[dict] | None = None    # 第 i 轮注入 pending_action.slots（NONE = 不注入）
    confirm_sequence: list[str] | None = None   # 第 i 轮注入 confirm_decision（accept/cancel）
    # ---- [R4] 超长会话构造器 ----
    session_filler: dict | None = None          # {"turns": 24, "pool": ["闲聊", "查询", ...]}


@dataclass
class EvaluationDataset:
    dataset_id: str
    version: str
    description: str
    cases: list[EvaluationCase]        # 仅含 enabled=True；disabled 仅计数
    disabled_count: int = 0
    # [S3 分场] 场名与通过线：八场拆分后每场独立数据集、独立通过线（方案 2.2 / 决策 2）。
    # scene 缺省空串 = 不分场（存量 eval_anta.json 等老数据集照常加载，无破坏性）。
    # pass_line = **用例通过率**线（0~1），口径见 report.compute_scene_verdict：
    # 用例通过 = 图跑通且无失败机制断言（拒绝路径须有断言作证）；None = 不判定达标。
    scene: str = ""
    pass_line: float | None = None


def _err(line_hint: str, msg: str) -> ValueError:
    return ValueError(f"[评测集校验失败] {line_hint}: {msg}")


# [R2] 断言字段类型表（字段名与 evaluation/assertions.py 的断言键一一对应）
_ASSERT_FIELD_TYPES: dict[str, type] = {
    "expect_permission_denied": str,
    "expect_permission_allowed": bool,
    "expect_sql_tables": list,
    "expect_sql_not_tables": list,
    "expect_inherited": dict,
    "expect_inherited_absent": list,
    "expect_memory_injected": bool,
    "expect_memory_scope": str,
    "expect_zero_extract": bool,
    "expect_pending_slots": dict,
    "expect_history_len_le": int,
    "expect_login_rejected": str,
    "expect_flow_doc": dict,
    "expect_explanation_grounded": bool,     # [F9] 解释文本数字对账（防幻觉）
}


def _assert_field_ok(value, types: type) -> bool:
    """断言字段类型判定——bool 必须与 bool 严格对应

    Python 里 bool 是 int 子类，若直接 isinstance 判定，`expect_history_len_le: true`
    会被静默接受（True 当 1 用），断言口径就被悄悄改了；反过来 `expect_memory_injected: 1`
    也不该被当作 True 放行。两侧都要求显式类型。
    """
    if isinstance(value, bool):
        return types is bool
    return isinstance(value, types)


# [R2/R3] 断言字段统一前缀：拼错的名字既不在 _ASSERT_FIELD_TYPES 里，加载器也不会报错
_EXPECT_PREFIX = "expect_"


def _reject_unknown_expect_keys(item: dict, case_id: str, hint: str) -> None:
    """未知 expect_* 断言字段守卫（"拼错 = 不断言"的静默失效，必须在加载期堵死）

    谁调用：load_dataset 逐用例校验段（类型校验之前）
    设计 why：断言字段名刻意与 evaluation/assertions.py 的断言键同名（报告/数据集/排查免翻译），
    代价是**名字拼错时加载器完全不吭声**——如 expect_flow_committed（真名 expect_flow_doc）：
    类型表里没有它 → 跳过类型校验 → EvaluationCase 也不接收 → 该用例静默变成"无断言"，
    机制真断了也全绿。同款坑本会话已踩两次（R3 漏装 inventory_write_repository、
    R4 传 messages=[]），共同特征都是"配置没生效但不报错"，故在唯一入口统一堵。
    只拦 expect_ 前缀：其余键（notes/tags/difficulty…）是元数据，不属于断言面，避免误伤存量数据集。
    示例：_reject_unknown_expect_keys({"expect_flow_committed": {"doc_no": "RK1"}},
                                     "F3-01", "cases[0]")
          → ValueError: [评测集校验失败] cases[0]: 用例 F3-01 的断言字段
            expect_flow_committed 未定义（可用字段见 dataset._ASSERT_FIELD_TYPES）
    """
    # step 1: 取"以 expect_ 开头但不在类型表里"的键（类型表即白名单，新增断言键自动纳入）
    unknown = sorted(k for k in item
                     if k.startswith(_EXPECT_PREFIX) and k not in _ASSERT_FIELD_TYPES)
    # step 2: 命中即抛（定位含 cases[i] 与用例 id，便于 JSON 里直接找到那一行）
    if unknown:
        raise _err(hint, f"用例 {case_id} 的断言字段 {', '.join(unknown)} 未定义"
                         f"（可用字段见 dataset._ASSERT_FIELD_TYPES）")


def load_dataset(path: Path) -> EvaluationDataset:
    """json.load + 全量校验。校验规则见 03 文档 §2.1 字段表"""
    # step 1: 文件存在性与 JSON 合法性
    if not path.is_file():
        raise _err(str(path), "评测集文件不存在")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise _err(str(path), f"JSON 解析失败：{e}") from e

    # step 2: 顶层字段
    dataset_id = raw.get("dataset_id")
    if not dataset_id:
        raise _err(str(path), "缺少必填字段 dataset_id")
    defaults = raw.get("defaults") or {}
    match_mode = defaults.get("match_mode", "exact")
    if match_mode not in ("exact", "contains"):
        raise _err(str(path), f"defaults.match_mode 非法：{match_mode}（仅 exact/contains）")
    k = defaults.get("k", 5)
    if not isinstance(k, int) or k <= 0:
        raise _err(str(path), f"defaults.k 必须为正整数，实际：{k!r}")

    # [S3 分场] 场名 + 通过线（均可缺省：老数据集不分场照常加载）
    scene = raw.get("scene") or ""
    if not isinstance(scene, str):
        raise _err(str(path), "scene 必须为 str（场名，如 F4 权限矩阵场）")
    pass_line = raw.get("pass_line")
    if pass_line is not None and (isinstance(pass_line, bool)
                                  or not isinstance(pass_line, (int, float))
                                  or not 0 <= pass_line <= 1):
        # 写成 90（想表达 90%）是最容易犯的错：它会让"通过率 ≥90"永真 → 达标判定形同虚设
        raise _err(str(path), f"pass_line 必须为 0~1 之间的数值（0.9 = 90%），实际：{pass_line!r}")
    pass_line = None if pass_line is None else float(pass_line)

    # step 3: 逐用例校验（定位 = cases 数组下标，报错含用例 id 双保险）
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    disabled_count = 0
    for i, item in enumerate(raw.get("cases") or []):
        hint = f"cases[{i}]"
        case_id = item.get("id")
        if not case_id:
            raise _err(hint, "缺少必填字段 id")
        if case_id in seen_ids:
            raise _err(hint, f"用例 id 重复：{case_id}")
        seen_ids.add(case_id)
        if not item.get("query") and not item.get("dialogue"):
            raise _err(hint, f"用例 {case_id} 缺少必填字段 query（多轮用例可用 dialogue 替代）")
        if item.get("enabled") is None:
            raise _err(hint, f"用例 {case_id} 缺少必填字段 enabled（骨架占位机制依赖它）")
        expected = item.get("expected") or {}
        # expected 各键类型校验：集合类字段必须为 list[str]
        for list_key in ("columns", "metrics", "values", "tables", "tools"):
            v = expected.get(list_key)
            if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
                raise _err(hint, f"用例 {case_id} 的 expected.{list_key} 必须为 list[str]")
        if "intent" in expected and not isinstance(expected["intent"], str):
            raise _err(hint, f"用例 {case_id} 的 expected.intent 必须为 str")
        # [06] memory_setup 类型校验（基础回忆用例）
        mem = item.get("memory_setup")
        if mem is not None:
            if not isinstance(mem, dict):
                raise _err(hint, f"用例 {case_id} 的 memory_setup 必须为 dict")
            for list_key in ("setup_runs", "expected_recall"):
                v = mem.get(list_key)
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise _err(hint, f"用例 {case_id} 的 memory_setup.{list_key} 必须为 list[str]")
        # [v1.1] dialogue 多轮校验：非空 list[str]，且与 query 至少有一者存在
        dlg = item.get("dialogue")
        if dlg is not None:
            if not isinstance(dlg, list) or not dlg or not all(isinstance(x, str) and x.strip() for x in dlg):
                raise _err(hint, f"用例 {case_id} 的 dialogue 必须为非空 list[str]")
            if not item.get("query"):
                item["query"] = dlg[-1]           # target = 最后一轮（query 缺省取末轮，兼容下游）
            if len(dlg) < 2:
                raise _err(hint, f"用例 {case_id} 的 dialogue 至少需要 2 轮（单轮请直接用 query）")

        # [R1] staff_id：可缺省；给了必须是 str（工号形如 S001/T001——
        # "是否存在/是否停用/是否过期"交给 dim_staff 查询与校验，加载器不重复实现）
        staff_id = item.get("staff_id")
        if staff_id is not None and not isinstance(staff_id, str):
            raise _err(hint, f"用例 {case_id} 的 staff_id 必须为 str")

        # [R2/R3] 断言字段名守卫：拼错 = 静默不断言（类型校验只管"写了的字段"，管不了名字本身）
        _reject_unknown_expect_keys(item, case_id, hint)

        # [R2] 机制断言字段类型校验（判定逻辑在 evaluation/assertions.py，
        # 加载器只保证"写进来的值形态可用"，不做语义判定）
        for key, types in _ASSERT_FIELD_TYPES.items():
            value = item.get(key)
            if value is None:
                continue                                 # 缺省 = 不断言
            if not _assert_field_ok(value, types):
                raise _err(hint, f"用例 {case_id} 的 {key} 类型非法（应为 {types.__name__}）")
        if item.get("expect_memory_scope") not in (None, "personal", "store"):
            raise _err(hint, f"用例 {case_id} 的 expect_memory_scope 仅支持 personal/store")

        # [R3] 流程控制注入：序列长度不得超过轮数（错位会被静默忽略 → 用例假通过）
        turn_count = len(dlg) if dlg else 1
        for key in ("slots_sequence", "confirm_sequence"):
            seq = item.get(key)
            if seq is None:
                continue
            if not isinstance(seq, list) or len(seq) > turn_count:
                raise _err(hint, f"用例 {case_id} 的 {key} 必须为 list 且长度 ≤ 轮数({turn_count})")
        if any(v is not None and not isinstance(v, dict)
               for v in (item.get("slots_sequence") or [])):
            raise _err(hint, f"用例 {case_id} 的 slots_sequence 元素必须为 dict")
        if any(v not in (None, "accept", "cancel")
               for v in (item.get("confirm_sequence") or [])):
            raise _err(hint, f"用例 {case_id} 的 confirm_sequence 仅支持 accept/cancel")

        # [R3] 写库结果断言：嵌套形态单独校验（doc_no 是查库依据，写错会静默查空 → 假失败）
        flow_doc = item.get("expect_flow_doc")
        if flow_doc is not None:
            ok = (isinstance(flow_doc, dict) and isinstance(flow_doc.get("doc_no"), str)
                  and isinstance(flow_doc.get("committed", True), bool))
            if not ok:
                raise _err(hint, f"用例 {case_id} 的 expect_flow_doc 需为 "
                                 f"{{\"doc_no\": str, \"committed\": bool(缺省 true)}}")

        # [R4] 超长会话构造器：turns 与 pool 必须自洽（turns>0 却无 pool = 静默不生效）
        filler = item.get("session_filler")
        if filler is not None:
            if not isinstance(filler, dict) or not isinstance(filler.get("turns", 0), int):
                raise _err(hint, f"用例 {case_id} 的 session_filler 需为 "
                                 f"{{\"turns\": int, \"pool\": [str]}}")
            pool = filler.get("pool") or []
            if filler.get("turns") and (not isinstance(pool, list) or not any(pool)):
                raise _err(hint, f"用例 {case_id} 的 session_filler.turns>0 时 pool 必填")

        if not item["enabled"]:
            disabled_count += 1
            continue                                    # disabled 不进 cases，仅计数
        cases.append(EvaluationCase(
            id=case_id, query=item["query"],
            capability=item.get("capability") or "dataquery",
            staff_id=staff_id,
            expected=expected,
            golden_sql=item.get("golden_sql"),
            match_mode=item.get("match_mode", match_mode),
            k=item.get("k", k),
            notes=item.get("notes", ""),
            memory_setup=item.get("memory_setup"),
            dialogue=dlg,
            expect_permission_denied=item.get("expect_permission_denied"),
            expect_permission_allowed=item.get("expect_permission_allowed"),
            expect_sql_tables=item.get("expect_sql_tables"),
            expect_sql_not_tables=item.get("expect_sql_not_tables"),
            expect_inherited=item.get("expect_inherited"),
            expect_inherited_absent=item.get("expect_inherited_absent"),
            expect_memory_injected=item.get("expect_memory_injected"),
            expect_memory_scope=item.get("expect_memory_scope"),
            expect_zero_extract=item.get("expect_zero_extract"),
            expect_pending_slots=item.get("expect_pending_slots"),
            expect_history_len_le=item.get("expect_history_len_le"),
            expect_login_rejected=item.get("expect_login_rejected"),
            expect_flow_doc=item.get("expect_flow_doc"),
            expect_explanation_grounded=item.get("expect_explanation_grounded"),
            slots_sequence=item.get("slots_sequence"),
            confirm_sequence=item.get("confirm_sequence"),
            session_filler=item.get("session_filler"),
        ))

    return EvaluationDataset(
        dataset_id=dataset_id,
        version=raw.get("version", "1.0"),
        description=raw.get("description", ""),
        cases=cases,
        disabled_count=disabled_count,
        scene=scene,
        pass_line=pass_line,
    )
