"""
R2 机制断言器单测（纯函数：无 LLM / 无 DB / 不执行图）
运行：PYTHONPATH=. uv run python tests/test_eval_assertions.py
"""
from types import SimpleNamespace

from app.evaluation.assertions import (aggregate_assertions, assertion_summary,
                                       evaluate_assertions, extract_sql_tables)


def _case(**kwargs):
    """最小用例替身：断言器只读 expect_* 属性（duck typing，不依赖 EvaluationCase 构造）"""
    return SimpleNamespace(**kwargs)


def test_extract_sql_tables_normalizes():
    # Given 带库名前缀/反引号/别名的 SQL, When 抽表名, Then 得到小写裸表名集合
    sql = "SELECT * FROM `dw`.`fact_sales` f JOIN dim_sku s ON f.sku_id = s.sku_id"
    assert extract_sql_tables(sql) == {"fact_sales", "dim_sku"}


def test_extract_sql_tables_covers_implicit_join_and_subquery():
    # 隐式连接（逗号）与子查询都不得成为禁忌表断言的绕过口
    implicit = "SELECT a.x FROM fact_sales a, fact_replenish_policy p WHERE a.id = p.id"
    assert extract_sql_tables(implicit) == {"fact_sales", "fact_replenish_policy"}
    subquery = "SELECT * FROM (SELECT sku_id FROM fact_inventory) t WHERE t.sku_id > 0"
    assert extract_sql_tables(subquery) == {"fact_inventory"}


def test_table_set_semantics():
    # Given 期望表用 JOIN 写法（与 golden 字面不同）, When 判定, Then 通过（不误伤等价写法）
    case = _case(expect_sql_tables=["fact_sales"], expect_sql_not_tables=["fact_replenish_policy"])
    rows = evaluate_assertions(case, {"sql": "SELECT a FROM fact_sales a JOIN dim_sku b ON 1=1"})
    assert [r["passed"] for r in rows] == [True, True], rows
    # 禁忌表命中 → 失败（治多轮串台：问销售额却查了补货策略表）
    rows = evaluate_assertions(case, {"sql": "SELECT a FROM fact_sales a, fact_replenish_policy p"})
    assert [r["passed"] for r in rows] == [True, False], rows


def test_permission_denied_requires_same_point():
    case = _case(expect_permission_denied="inventory.write")
    assert evaluate_assertions(
        case, {"permission_denied": True,
               "denied_permission": "inventory.write"})[0]["passed"] is True
    # 被拒了但不是这个权限点 → 不通过（防"整类都被拒"式假通过）
    assert evaluate_assertions(
        case, {"permission_denied": True,
               "denied_permission": "replenish.store"})[0]["passed"] is False
    assert evaluate_assertions(case, {})[0]["passed"] is False      # 根本没被拒


def test_login_rejected_by_reason_code():
    case = _case(expect_login_rejected="disabled")
    assert evaluate_assertions(case, None, {"login_rejected": "disabled"})[0]["passed"] is True
    assert evaluate_assertions(case, None, {"login_rejected": "expired"})[0]["passed"] is False
    row = evaluate_assertions(case, None, {})[0]                    # staff 有效 → 明确失败
    assert row["passed"] is False and "未被登录拒绝" in row["note"]


def test_inherited_uses_fullwidth_colon():
    case = _case(expect_inherited={"品类": "鞋类", "时间": "2026-10-01 ~ 2026-10-07"})
    block = "【继承条件】\n品类：鞋类\n时间：2026-10-01 ~ 2026-10-07\n"
    assert evaluate_assertions(case, {"inherited_conditions": block})[0]["passed"] is True
    # 半角冒号（形态不符）→ 失败：断言口径必须与产出形态一致
    bad = evaluate_assertions(case, {"inherited_conditions": "品类: 鞋类"})[0]
    assert bad["passed"] is False and "继承块缺少" in bad["note"]


def test_memory_injection_and_history_len():
    case = _case(expect_memory_injected=True, expect_history_len_le=300)
    assert [r["passed"] for r in evaluate_assertions(
        case, {"memory_block": "【记忆】负责鞋类", "history_chars": 120})] == [True, True]
    assert [r["passed"] for r in evaluate_assertions(
        case, {"memory_block": "", "history_chars": 900})] == [False, False]


def test_pending_slots_supports_no_flow_semantics():
    case = _case(expect_pending_slots={})
    assert evaluate_assertions(case, {"pending_action": None})[0]["passed"] is True
    assert evaluate_assertions(
        case, {"pending_action": {"type": "inventory_in"}})[0]["passed"] is False
    case2 = _case(expect_pending_slots={"direction": "in", "sku_id": "15262011"})
    state = {"pending_action": {"slots": {"direction": "in", "sku_id": "15262011", "qty": 3}}}
    assert evaluate_assertions(case2, state)[0]["passed"] is True


def test_unevaluated_excluded_from_pass_rate():
    # Given 依赖记忆快照的断言但 runner 未提供快照, When 判定, Then passed=None（不进分母）
    case = _case(expect_memory_scope="personal", expect_sql_tables=["fact_sales"])
    rows = evaluate_assertions(case, {"sql": "SELECT 1 FROM fact_sales"})
    scope_row = next(r for r in rows if r["key"] == "expect_memory_scope")
    assert scope_row["passed"] is None and "R5" in scope_row["note"], rows
    assert assertion_summary(rows) == {"total": 2, "passed": 1, "failed": 0,
                                       "unevaluated": 1, "pass_rate": 1.0}


def test_aggregate_counts_failure_path_cases():
    detail = [
        {"assertions": [{"key": "expect_sql_tables", "passed": True}]},
        {"assertions": [{"key": "expect_sql_tables", "passed": False},
                        {"key": "expect_permission_denied", "passed": True}]},
    ]
    agg = aggregate_assertions(detail)
    assert agg["total"] == 3 and agg["passed"] == 2 and agg["failed"] == 1
    assert agg["by_key"]["expect_sql_tables"] == {"total": 2, "passed": 1, "failed": 1,
                                                  "unevaluated": 0, "pass_rate": 0.5,
                                                  "cases": 2}      # 机制覆盖矩阵：覆盖用例数
    assert agg["by_key"]["expect_permission_denied"]["pass_rate"] == 1.0
    assert agg["by_key"]["expect_permission_denied"]["cases"] == 1


def test_assertion_exception_does_not_interrupt():
    # 期望值形态错误 → 记未评估 + 说明，绝不抛异常中断整场评测
    rows = evaluate_assertions(_case(expect_history_len_le=100), {"history_chars": "不是数字"})
    assert rows[0]["passed"] is None and "断言执行异常" in rows[0]["note"]


def test_explanation_grounded_catches_unsourced_number():
    # [F9] Given 结果里只有 138602, When 解释复述该数, Then 通过（含千分位写法）
    case = _case(expect_explanation_grounded=True)
    state = {"result_sample": [{"品类": "鞋类", "可用库存": 138602}], "result_total": 1,
             "query": "鞋类可用库存有多少", "sql": "SELECT SUM(available_qty) FROM fact_inventory"}
    ok = evaluate_assertions(case, state, {
        "answer_text": "鞋类当前可用库存为 138,602 件（样本 138602.00 一致）"})[0]
    assert ok["passed"] is True, ok
    # 编造的来源外数字（"增长 30%"）→ 失败，且 note 列出该数字供排查
    bad = evaluate_assertions(case, state, {"answer_text": "库存充足，比上周增长 30%"})[0]
    assert bad["passed"] is False and "30" in bad["note"], bad


def test_explanation_grounded_allows_query_and_date_numbers():
    # 查询句里的数字、动态数据边界的日期写法都是真实来源（否则 F9 会变噪声场）
    case = _case(expect_explanation_grounded=True)
    state = {"result_sample": [{"qty": 5}], "result_total": 1,
             "query": "给 15262011 入库 5 件", "sql": "SELECT 5",
             "date_info": {"date": "2026-10-12", "data_range": "2026-10-01 ~ 2026-10-09"}}
    row = evaluate_assertions(case, state, {
        "answer_text": "已为 SKU 15262011 入库 5 件；数据可用区间为 10 月 1 日 ~ 10 月 9 日"})[0]
    assert row["passed"] is True, row


def test_explanation_grounded_unevaluated_and_reverse_semantics():
    # runner 未采集回答文本 → 未评估（不进通过率分母，不假通过）
    row = evaluate_assertions(_case(expect_explanation_grounded=True),
                              {"result_sample": []}, {})[0]
    assert row["passed"] is None and "未采集" in row["note"], row
    # expect=False（反例语义）：期望出现无来源数字 —— 注入对抗用它判"是否采信了用户口报的数"
    case = _case(expect_explanation_grounded=False)
    state = {"result_sample": [{"可用库存": 138602}]}
    assert evaluate_assertions(case, state, {"answer_text": "我按你说的 5 万来算"})[0]["passed"] is True
    assert evaluate_assertions(
        case, state, {"answer_text": "可用库存 138,602 件"})[0]["passed"] is False


def test_inherited_absent_blocks_override():
    # [F2] 条件推翻：_check_inherited 只能证"含"，推翻句只能靠"不得含"来判
    case = _case(expect_inherited_absent=["品类"])
    block = "【继承条件（本轮未提及，来自上一轮）】\n时间：2026-10-01 ~ 2026-10-07（国庆档）"
    assert evaluate_assertions(case, {"inherited_conditions": block})[0]["passed"] is True
    bad = block + "\n品类：鞋类"
    row = evaluate_assertions(case, {"inherited_conditions": bad})[0]
    assert row["passed"] is False and "品类" in row["note"], row


def test_grounded_sources_include_replenish_plan():
    # [F9] 补货建议是规则产出的结构化结果（不进 result_sample）：不纳入白名单会误伤整个补货场
    case = _case(expect_explanation_grounded=True)
    state = {"replenish_plan": {"plan": [{"sku_id": "15241012-05-43", "gap": 12, "suggest_qty": 15}],
                                "total_gap": 235, "total_suggest_qty": 5681}}
    row = evaluate_assertions(case, state, {
        "answer_text": "鞋类建议补 5,681 件（缺口 235 个 SKU，示例 SKU 补 15 件）"})[0]
    assert row["passed"] is True, row


def test_permission_allowed_is_matrix_other_half():
    # [F4] 放行结论：未拒 → 通过；被拒 → 失败且 note 指出被拒权限点
    case = _case(expect_permission_allowed=True)
    assert evaluate_assertions(case, {"permission_denied": False}, {})[0]["passed"] is True
    row = evaluate_assertions(case, {"permission_denied": True,
                                     "denied_permission": "inventory.write"}, {})[0]
    assert row["passed"] is False and "inventory.write" in row["note"], row
    # 被登录门禁拦下 = 没放行；图未执行 = 未评估（不假通过）
    assert evaluate_assertions(case, {"permission_denied": False},
                               {"login_rejected": "expired"})[0]["passed"] is False
    assert evaluate_assertions(case, None, {})[0]["passed"] is None


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")     # Windows 控制台默认 GBK，中文输出会乱码
    test_extract_sql_tables_normalizes()
    test_extract_sql_tables_covers_implicit_join_and_subquery()
    test_table_set_semantics()
    test_permission_denied_requires_same_point()
    test_login_rejected_by_reason_code()
    test_inherited_uses_fullwidth_colon()
    test_memory_injection_and_history_len()
    test_pending_slots_supports_no_flow_semantics()
    test_unevaluated_excluded_from_pass_rate()
    test_aggregate_counts_failure_path_cases()
    test_assertion_exception_does_not_interrupt()
    test_explanation_grounded_catches_unsourced_number()
    test_explanation_grounded_allows_query_and_date_numbers()
    test_explanation_grounded_unevaluated_and_reverse_semantics()
    test_inherited_absent_blocks_override()
    test_grounded_sources_include_replenish_plan()
    test_permission_allowed_is_matrix_other_half()
    print("R2 机制断言器单测：17/17 通过")
