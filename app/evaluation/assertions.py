"""
机制断言器（R2 断言扩展，评测升级方案 §2.1）

职责：把「结果对错」之外的机制断言字段（expect_*）变成可自动判定的结果。
        评测升级方案的出发点正是"机制加了但没加评测"，本模块是被测机制的判卷人。

谁调用：report._cases_detail（逐例明细）+ report.compute_aggregate（机制断言通过率）
链路位置：CaseResult.final_state →（本模块）→ 报告 cases[].assertions
         → aggregate.quality.assertions（按字段聚合的通过率）

设计 why：
① 断言口径全部取自 state **显式字段**（R2-a 为此专门把 inherited_conditions /
   memory_block / history_chars 落 state）——不做自由文本正则猜测，避免"文本里恰好
   出现了关键词、但那个机制其实没生效"的假通过；
② 表名断言按**集合**判定而非字面（方案风险 #2：等价 JOIN 写法不得误伤）：归一化去掉
   库名前缀与反引号，实际覆盖期望即通过，多余表由 not_tables 单独管；
③ **无法判定的断言返回 passed=None（未评估），而不是默认 True/False**：聚合计分时
   把它排除出分母——既不冤枉用例，也不制造假通过（F3 的 scope 断言在 runner 未提供
   记忆快照时就走这条路）；
④ 单条断言失败不短路其它断言（与 runner 的用例容错同纪律）——一条用例可以同时暴露
   多个机制问题。

示例：
    rows = evaluate_assertions(case, final_state)
    # [{"key": "expect_permission_denied", "expect": "inventory.write",
    #   "actual": "inventory.write", "passed": True, "note": ""}, ...]
    assertion_summary(rows)          # {"total": 3, "passed": 2, "failed": 1, "unevaluated": 0}
"""
import re

# SQL 表名抽取：(1) JOIN 目标；(2) 每个 FROM 关键字之后的表列表
# 两处都不能省：
# - 只认 \bfrom|join\b 会漏掉 `FROM a, b` 隐式连接 → 禁忌表断言可被绕过（单测抓出）；
# - 用"整段 FROM 子句"正则会吞掉子查询里的 FROM（外层匹配区间覆盖内层）
#   → 改为逐个 FROM 关键字向后扫，嵌套/子查询都能覆盖。
_SQL_JOIN_RE = re.compile(r"\bjoin\s+([`\w\.]+)", re.IGNORECASE)
_SQL_FROM_KW_RE = re.compile(r"\bfrom\b", re.IGNORECASE)
_SQL_CLAUSE_END_RE = re.compile(
    r"\bwhere\b|\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|\bjoin\b|\bon\b|\)",
    re.IGNORECASE)
_SQL_IDENTIFIER_RE = re.compile(r"[`\w\.]+")      # 裸标识符（过滤子查询括号等噪声）

# 断言摘要里 actual 的截断长度（继承块/记忆块可能很长，报告不必全文）
_ACTUAL_MAX = 300


def extract_sql_tables(sql: str) -> set[str]:
    """从 SQL 抽表名集合（去库名前缀 `dw.`、去反引号、小写归一）

    谁调用：_check_sql_tables / _check_sql_not_tables；报告排查也用
    设计 why：不引第三方 SQL 解析器——评测只需要表集合（方案风险 #2 明确按集合判定），
    正则足以覆盖本项目 SQL 形态（FROM/JOIN + 两类表），零依赖且不会因方言差异报错。
    示例：extract_sql_tables("SELECT * FROM fact_sales f JOIN dw.dim_sku s ON ...")
          → {"fact_sales", "dim_sku"}
    """
    text = re.sub(r"\s+", " ", sql or "")                # 压平空白，让子句切分简单可靠
    tables: set[str] = set()
    for m in _SQL_JOIN_RE.finditer(text):                # JOIN 目标表
        tables.add(_normalize_table(m.group(1)))
    for m in _SQL_FROM_KW_RE.finditer(text):             # 逐个 FROM（子查询内也会被扫到）
        rest = text[m.end():]
        end = _SQL_CLAUSE_END_RE.search(rest)
        clause = rest[:end.start()] if end else rest     # 子句止于 where/join/on/")"
        for part in clause.split(","):                   # 逗号连接 → 多段
            token = part.strip().split(" ")[0]           # 每段首 token = 表名（后接别名）
            if _SQL_IDENTIFIER_RE.fullmatch(token or ""):   # 过滤 "(select" 之类子查询噪声
                tables.add(_normalize_table(token))
    return {t for t in tables if t}


def _normalize_table(name: str) -> str:
    """表名归一化：去反引号、去库名前缀、小写（`dw`.`fact_sales` → fact_sales）"""
    return name.replace("`", "").split(".")[-1].lower()


def _truncate(value):
    """actual 截断（长文本断言产物在报告里只留前 _ACTUAL_MAX 字符）"""
    if isinstance(value, str) and len(value) > _ACTUAL_MAX:
        return value[:_ACTUAL_MAX] + "…"
    return value


def _check_permission_denied(expect: str, final_state: dict, extra: dict):
    """权限矩阵：本轮必须被拒，且被拒的正是这个权限点"""
    denied = bool(final_state.get("permission_denied"))
    actual = final_state.get("denied_permission") or None
    passed = denied and actual == expect
    note = "" if passed else ("本轮未被拒绝" if not denied else f"被拒权限点不符（期望 {expect}）")
    return passed, actual, note


def _check_permission_allowed(expect: bool, final_state: dict, extra: dict):
    """权限矩阵：本轮**必须放行**（不得被权限拒绝，也不得被登录门禁拦下）

    谁调用：evaluate_assertions（F4 场"放行"类用例：TEMP 查销售额、KEEPER 门店补货、M001 全通过）
    设计 why：expect_permission_denied 只能断"被拒"，而权限矩阵有一半结论是"该放行"——
    没有这条，"放行"只能靠 SQL 表集合间接证明（补货类无 SQL，直接没法判）。
    两个判据都在：permission_denied（路由/流程守卫）+ extra["login_rejected"]（登录门禁），
    任一命中即未放行。图未执行（final_state=None）→ 未评估，不假通过。

    示例：expect=True, {"permission_denied": False} → 通过；
          expect=True, {"permission_denied": True} → 失败（note 提示被拒权限点）
    """
    # 图未执行 → 未评估：runner 对失败路径传 final_state=None，而 evaluate_assertions 会先
    # 归一成 {}（真实终态必有字段），故空 dict 同样按"没有证据"处理，不假通过也不假失败
    if not final_state:
        return None, None, "图未执行（终态为空），无法判定是否放行"
    if extra.get("login_rejected") is not None:
        return False, extra["login_rejected"], f"被登录门禁拦下（{extra['login_rejected']}）"
    denied = bool(final_state.get("permission_denied"))
    actual = final_state.get("denied_permission") or None
    passed = denied != bool(expect)          # expect=True → 不得被拒；expect=False → 必须被拒
    note = "" if passed else ("本轮被拒绝" + (f"（{actual}）" if actual else "")
                              if denied else "期望被拒，本轮却放行了")
    return passed, actual, note


def _check_sql_tables(expect: list, final_state: dict, extra: dict):
    """SQL 涉及表集合 ⊇ 期望表（治多轮串台：问销售额却查了补货策略表）"""
    tables = extract_sql_tables(final_state.get("sql") or "")
    missing = [t for t in expect if t.lower() not in tables]
    return (not missing), sorted(tables), (f"SQL 未涉及：{missing}" if missing else "")


def _check_sql_not_tables(expect: list, final_state: dict, extra: dict):
    """SQL 表集合 ∩ 禁忌表 = ∅（与上一条互补：多余表只在这里管）"""
    tables = extract_sql_tables(final_state.get("sql") or "")
    hit = [t for t in expect if t.lower() in tables]
    return (not hit), sorted(tables), (f"不该涉及却出现：{hit}" if hit else "")


def _check_inherited(expect: dict, final_state: dict, extra: dict):
    """继承条件块内容（逐条子串判定：块内需含 `键：值`）

    注意用全角冒号——condition_extractor 的产出形态即 `品类：鞋类`，
    断言与产出同形才能真实反映"模型看到的那一行"。
    """
    block = final_state.get("inherited_conditions") or ""
    missing = [f"{k}：{v}" for k, v in expect.items() if f"{k}：{v}" not in block]
    return (not missing), _truncate(block), (f"继承块缺少：{missing}" if missing else "")


def _check_inherited_absent(expect: list, final_state: dict, extra: dict):
    """继承条件块**不得**出现指定键（F2 的条件推翻语义）

    谁调用：evaluate_assertions（用例声明 expect_inherited_absent 时）
    设计 why：_check_inherited 只能证"含"，证不了"不含"——而"不要鞋类了，全部品类"这类
    推翻句，唯一可判定的规则信号就是"品类不得再被当成继承条件带进本轮提示词"
    （condition_extractor 的继承规则 = 上一轮有、本轮没有 → 本轮说出来即自然覆盖）。
    与 expect_inherited 互补使用：一条用例可同时断"时间仍继承 + 品类已被推翻"。

    示例：expect=["品类"], 继承块 "【继承条件…】\n品类：鞋类\n时间：国庆档" → 失败（含"品类："）
    """
    block = final_state.get("inherited_conditions") or ""
    hit = [k for k in expect if f"{k}：" in block]
    return (not hit), _truncate(block), (f"继承块不应出现：{hit}" if hit else "")


def _check_memory_injected(expect: bool, final_state: dict, extra: dict):
    """记忆块是否真的进了提示词（治"记忆 recall 0/3"的根因：知识型问题被路由去查库）"""
    block = final_state.get("memory_block") or ""
    injected = len(block) > 0
    actual = {"injected": injected, "chars": len(block)}
    passed = injected == bool(expect)
    return passed, actual, ("" if passed else f"期望 injected={bool(expect)}，实际 {injected}")


def _check_memory_scope(expect: str, final_state: dict, extra: dict):
    """落库条目的 scope（门店层写入门禁 / 降级）：期望"本轮写入的条目全部是该 scope"

    判定依据由 runner 提供（extra["memory_writes"] = [(scope, owner_id), ...]）；
    未提供 → passed=None（未评估），绝不猜。
    """
    writes = extra.get("memory_writes")
    if writes is None:
        return None, None, "runner 未提供本轮记忆快照（R5 接入）"
    scopes = sorted({s for s, _ in writes})
    owners = sorted({o for _, o in writes})
    actual = {"scopes": scopes, "owners": owners, "count": len(writes)}
    if not writes:
        return False, actual, "本轮未写入任何记忆条目"
    passed = scopes == [expect]
    return passed, actual, ("" if passed else f"期望 scope={expect}，实际 {scopes}")


def _check_zero_extract(expect: bool, final_state: dict, extra: dict):
    """本轮后记忆库无新增（禁经营数据 / 禁推断）"""
    added = extra.get("memory_added")
    if added is None:
        return None, None, "runner 未提供本轮记忆快照（R5 接入）"
    passed = (added == 0) == bool(expect)
    return passed, added, ("" if passed else f"期望新增={0 if expect else '>0'}，实际 {added}")


def _check_pending_slots(expect: dict, final_state: dict, extra: dict):
    """流程槽位状态：{} 表示"本轮结束后无进行中流程"，否则逐键比对 slots[k] == v"""
    action = final_state.get("pending_action") or None
    slots = (action or {}).get("slots") or {}
    if not expect:                                        # 期望无进行中流程
        passed = not action
        return passed, (None if not action else slots), (
            "" if passed else f"期望无进行中流程，实际 {slots}")
    mismatch = {k: (v, slots.get(k)) for k, v in expect.items() if slots.get(k) != v}
    passed = bool(slots) and not mismatch
    return passed, slots, (f"槽位不符（期望, 实际）：{mismatch}" if mismatch else
                           ("" if slots else "无进行中流程"))


def _check_history_len(expect: int, final_state: dict, extra: dict):
    """历史区字符数上限（F6 预算守卫 / brief 生效）"""
    chars = final_state.get("history_chars")
    if chars is None:
        return None, None, "state 无 history_chars（R2 可测性字段未落）"
    passed = chars <= expect
    return passed, chars, ("" if passed else f"历史区 {chars} 字符 > 上限 {expect}")


def _check_login_rejected(expect: str, final_state: dict, extra: dict):
    """登录即拒（F4 停用/过期）：拒绝原因码需与期望一致

    判定依据是 runner 捕获的 LoginRejected.reason（extra["login_rejected"]）——它与
    在线登录门禁的 StaffAuthRepository.check_validity 同源，断言的是"同一次判定"，
    不是错误文案匹配（文案会改，语义不会）。
    """
    actual = extra.get("login_rejected")
    if actual is None:
        return False, None, "本轮未被登录拒绝（staff 有效，或未注入身份）"
    passed = actual == expect
    return passed, actual, ("" if passed else f"拒绝原因不符（期望 {expect}）")


def _check_flow_doc(expect: dict, final_state: dict, extra: dict):
    """出入库写库结果（R3）：{"doc_no": str, "committed": bool}（缺省 committed=true）

    committed=true  → 该单据号已在 fact_inventory_flow 落流水，**且经办人 = 注入身份**
    committed=false → 该单据号**不得**出现在流水中（用户取消 / 确认超时 / 幂等重复）

    判定依据是 runner 查库拿到的真实流水行（extra["flow_records"]），不是话术：
    state 里"提交成功"与"取消/超时"表现完全一致（pending_action=None），只断槽位清空
    无法区分是否真的记了账。经办人一并校验——它必须等于本用例注入的 staff_id，这是
    "身份从登录贯穿到写库"的端到端证据（方案 F5 的"流水 + 经办人"）。

    extra["flow_records"] 三态：None = runner 没查（未评估，不假通过）；{} = 查了无该
    单据；{...} = 命中。
    """
    doc_no = expect["doc_no"]
    want = expect.get("committed", True)
    records = extra.get("flow_records")
    if records is None:
        return None, None, "runner 未提供流水查询结果（无 DB 会话或断言未接入）"
    if bool(records) != bool(want):
        return False, (records or None), (
            f"单据 {doc_no} 未落流水（未写入/未提交/已取消/幂等拦截）" if want
            else f"单据 {doc_no} 不应记账，却已落流水")
    if not want:                                      # 期望"未记账"：命中即通过，无经办人可比
        return True, None, ""
    staff_id = extra.get("staff_id")
    actual = {"doc_no": records.get("doc_no"), "operator_id": records.get("operator_id"),
              "quantity": records.get("quantity"), "flow_type": records.get("flow_type")}
    if staff_id and records.get("operator_id") != staff_id:
        return False, actual, f"经办人不符（期望 {staff_id}，实际 {records.get('operator_id')}）"
    return True, actual, ""


# [F9] 数字抽取：千分位整数 / 小数（"138,602" "5.5" 都算一个数字）
_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")

# [F9] 日期口径：ISO 片段（2026-10-01）——解释层常写成"10月7日"，也要算真实来源
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _norm_number(token: str) -> str:
    """数字归一（对账双方都过它，避免"写法不同 = 幻觉"的误判）

    归一内容：去千分位逗号、去尾随 .0（138602.0 → 138602）、去整数前导零（0012 → 12）。
    示例：_norm_number("256,011") → "256011"；_norm_number("138602.00") → "138602"
    """
    text = token.replace(",", "").strip().rstrip(".")
    if "." in text:                                      # 小数：整数部分去前导零，小数部分去尾零
        int_part, _, frac = text.partition(".")
        int_part = int_part.lstrip("0") or "0"
        frac = frac.rstrip("0")
        return f"{int_part}.{frac}" if frac else int_part
    return text.lstrip("0") or "0"


def _number_variants(value) -> set[str]:
    """把一个真实来源值展开成所有"可接受的数字写法"（千分位 / 小数位 / 整数化）

    谁调用：_grounded_sources
    设计 why：同一条真实数据在解释文本里写法不唯一（138602 / 138,602 / 138602.00），
    只比对字面会把"同一数字的另一种写法"误判成幻觉 —— 误伤比漏报更坏（F9 会变噪声场）。
    """
    variants: set[str] = set()
    if isinstance(value, bool) or value is None:         # bool 是"是/否"不是数量，不进对账池
        return variants
    if isinstance(value, (int, float)):
        for token in (str(value), f"{value:,.0f}", f"{value:.1f}", f"{value:.2f}"):
            variants.add(_norm_number(token))
    elif isinstance(value, str):
        variants |= {_norm_number(m.group()) for m in _NUMBER_RE.finditer(value)}
    return {v for v in variants if v}


def _date_variants(text: str) -> set[str]:
    """日期区间的中文/简写写法变体（"10月7日""10-7""20261007" 都算真实来源）

    为什么需要：S0 后数据边界由 date_info.data_range 动态给出（如 2026-10-01 ~ 2026-10-09），
    解释层回答边界问题时自然写成"10 月 7 日"这类中文形态 —— 只按 ISO 串对账会把
    正确的边界说明打成幻觉。
    """
    out: set[str] = set()
    for m in _DATE_RE.finditer(text or ""):
        y, mo, d = m.groups()
        for token in (f"{y}年{int(mo)}月{int(d)}日", f"{mo}月{int(d)}日", f"{int(mo)}月{int(d)}日",
                      f"{mo}-{d}", f"{int(mo)}-{int(d)}", f"{y}{mo}{d}"):
            out |= _number_variants(token)
    return out


def _grounded_sources(final_state: dict) -> set[str]:
    """本轮"真实来源"数字集合（对账白名单）：结果样本 + 行数 + 查询句 + SQL 字面量 + 日期上下文

    谁调用：_check_explanation_grounded
    设计 why：白名单必须含**查询句与日期上下文**，否则"入库 5 件"的 5、边界说明里的日期
    都会被判成幻觉。反过来，模型自行计算/编造的比例（"比上周涨 30%"）不在任何来源里 ——
    这正是要抓的一类（无出处数字）。
    """
    allowed: set[str] = set()
    sample = final_state.get("result_sample") or []
    for row in sample:
        if isinstance(row, dict):
            for v in row.values():
                allowed |= _number_variants(v)
        else:
            allowed |= _number_variants(row)
    allowed |= _number_variants(final_state.get("result_total"))
    allowed |= _number_variants(len(sample))
    allowed |= _number_variants(final_state.get("query"))     # 用户给的数字（"出库 5 件"）
    allowed |= _number_variants(final_state.get("sql"))       # SQL 字面量（LIMIT / where 取值）
    # 补货计划是规则算出来的结构化结果（不进 result_sample，但它是解释文本的数字来源：
    # "鞋类建议补 5,681 件"里的每个数都必须能在 plan 行里找到）——不加这条 F9 会误伤补货场
    plan = final_state.get("replenish_plan")
    if isinstance(plan, dict):
        rows = plan.get("plan") or plan.get("rows") or []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                for v in row.values():
                    allowed |= _number_variants(v)
        for v in (plan.get("total_gap"), plan.get("total_suggest_qty")):
            allowed |= _number_variants(v)
    date_info = final_state.get("date_info") or {}
    for key in ("date", "quarter", "data_range"):
        allowed |= _number_variants(date_info.get(key))
    allowed |= _date_variants(date_info.get("data_range") or "")
    return allowed


def _check_explanation_grounded(expect: bool, final_state: dict, extra: dict):
    """[F9 答案质量] 解释文本数字对账：解释里每个数字都必须能在本轮真实来源中找到出处

    谁调用：evaluate_assertions（用例声明 expect_explanation_grounded 时）；runner 提供
    extra["answer_text"]（与在线 SSE 同源的回答文本）
    设计 why：现有指标全部止于 SQL 层，最终给店长看的那段话此前**零检测**（方案 6.2 Gap 2）——
    而"验证方式缺失的约束等于没有约束"。只对账**数字**、不做语义判定：数字是幻觉里最可判也
    最致命的一类，且纯字符串匹配零 LLM、可复现、成本 0。
    反例语义（expect=False）：期望解释里存在无来源数字（用于注入对抗——模型若采信用户口报的
    "5 万"就会带出无来源数字）。

    示例：expect=True，解释 "鞋类可用库存 138,602 件" → 138602 在 result_sample 中 → 通过
          expect=True，解释 "比上周增长 30%"（30 无来源）→ 失败，note 列出 ['30']
    """
    text = extra.get("answer_text")
    if not text:
        return None, None, "runner 未采集到本轮回答文本（F9 断言未接入或本轮无回答）"
    allowed = _grounded_sources(final_state)
    ungrounded = sorted({_norm_number(m.group()) for m in _NUMBER_RE.finditer(text)} - allowed)
    passed = (not ungrounded) == bool(expect)
    actual = {"grounded": not ungrounded, "ungrounded": ungrounded[:10], "count": len(ungrounded)}
    if passed:
        return True, actual, ""
    note = (f"解释文本含无来源数字：{ungrounded[:10]}" if expect
            else "期望存在无来源数字，实际全部可对账")
    return False, actual, note


# 数据集字段名 → 判定函数（字段名即断言键，报告与数据集同名，排查无需翻译）
_CHECKS = {
    "expect_permission_denied": _check_permission_denied,
    "expect_permission_allowed": _check_permission_allowed,   # F4 放行结论（矩阵的另一半）
    "expect_sql_tables": _check_sql_tables,
    "expect_sql_not_tables": _check_sql_not_tables,
    "expect_inherited": _check_inherited,
    "expect_memory_injected": _check_memory_injected,
    "expect_memory_scope": _check_memory_scope,
    "expect_zero_extract": _check_zero_extract,
    "expect_pending_slots": _check_pending_slots,
    "expect_history_len_le": _check_history_len,
    "expect_login_rejected": _check_login_rejected,   # F4 停用/过期（R1 注入 + 本断言配套）
    "expect_inherited_absent": _check_inherited_absent,   # F2 条件推翻（继承块不得含某键）
    "expect_flow_doc": _check_flow_doc,               # R3 写库结果（流水 + 经办人）
    "expect_explanation_grounded": _check_explanation_grounded,   # F9 答案质量（防幻觉对账）
}

ASSERTION_KEYS = tuple(_CHECKS)


def evaluate_assertions(case, final_state: dict | None,
                        extra: dict | None = None) -> list[dict]:
    """逐条判定用例声明的机制断言（未声明的字段跳过）→ 结果行列表

    参数：case = EvaluationCase（断言值即其 expect_* 字段）；final_state = 图终态；
         extra = 图外上下文（R5 记忆快照：memory_writes / memory_added），缺省空
    返回：[{"key","expect","actual","passed","note"}]，passed=None 表示未评估

    示例：evaluate_assertions(case, {"permission_denied": True,
                                    "denied_permission": "inventory.write"})
          → [{"key": "expect_permission_denied", "expect": "inventory.write",
              "actual": "inventory.write", "passed": True, "note": ""}]
    """
    state = final_state or {}
    rows: list[dict] = []
    for key, check in _CHECKS.items():
        expect = getattr(case, key, None)
        if expect is None:                                # 未声明 → 不断言
            continue
        try:
            passed, actual, note = check(expect, state, extra or {})
        except Exception as e:                            # 断言自身异常不得中断评测（同 runner 纪律）
            passed, actual, note = None, None, f"断言执行异常：{e}"
        rows.append({"key": key, "expect": expect, "actual": _truncate(actual),
                     "passed": passed, "note": note})
    return rows


def assertion_summary(rows: list[dict]) -> dict:
    """断言行列表 → 计数摘要（passed=None 计入 unevaluated 并从通过率分母剔除）

    示例：assertion_summary([{"passed": True}, {"passed": False}, {"passed": None}])
          → {"total": 3, "passed": 1, "failed": 1, "unevaluated": 1, "pass_rate": 0.5}
    """
    passed = sum(1 for r in rows if r["passed"] is True)
    failed = sum(1 for r in rows if r["passed"] is False)
    unevaluated = sum(1 for r in rows if r["passed"] is None)
    judged = passed + failed
    return {"total": len(rows), "passed": passed, "failed": failed,
            "unevaluated": unevaluated,
            "pass_rate": (passed / judged) if judged else None}


def aggregate_assertions(cases_detail: list[dict]) -> dict:
    """按断言字段聚合通过率（报告"机制覆盖矩阵"的数据源）

    参数：报告逐例明细（含 assertions 行）；返回：
      {"total": 断言条数, "passed": n, "failed": n, "unevaluated": n,
       "pass_rate": float|None,
       "by_key": {字段: {cases（覆盖用例数）, total, passed, failed, pass_rate}}}

    示例：aggregate_assertions([{"assertions": [{"key": "expect_x", "passed": True}]}])
          → {"total": 1, ..., "by_key": {"expect_x": {"cases": 1, "total": 1, ...}}}
    """
    by_key: dict[str, dict] = {}
    totals = {"total": 0, "passed": 0, "failed": 0, "unevaluated": 0}
    for item in cases_detail:
        rows = item.get("assertions") or []
        # 机制覆盖矩阵的"覆盖用例数"：一个用例对同一断言键只算一次覆盖
        # （判定的是"这个机制在本场有没有被用例覆盖"，不是断言条数）
        for key in {r["key"] for r in rows}:
            by_key.setdefault(key, {"total": 0, "passed": 0, "failed": 0,
                                    "unevaluated": 0, "cases": 0})["cases"] += 1
        for row in rows:
            bucket = by_key.setdefault(row["key"], {"total": 0, "passed": 0,
                                                    "failed": 0, "unevaluated": 0,
                                                    "cases": 0})
            # 未评估的断言也计数（报告要能看出"声明了但没判"），但不进通过率分母
            for target in (totals, bucket):
                target["total"] += 1
                if row["passed"] is True:
                    target["passed"] += 1
                elif row["passed"] is False:
                    target["failed"] += 1
                else:
                    target["unevaluated"] += 1
    for bucket in by_key.values():
        judged = bucket["passed"] + bucket["failed"]
        bucket["pass_rate"] = (bucket["passed"] / judged) if judged else None
    judged_all = totals["passed"] + totals["failed"]
    return {**totals, "by_key": by_key,
            "pass_rate": (totals["passed"] / judged_all) if judged_all else None}
