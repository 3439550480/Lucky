"""
SQL 指标（03 文档 §3.3，v1.1 口径）

可执行率 = run() 成功用例 / 全部 enabled 且走完链路的用例
结果集正确性（仅 golden_sql 用例）：行多重集相等，**忽略列名与列序**——
行签名 = 行内值归一化后排序的元组（LLM 生成 SQL 的列别名不可控，
首版"列名集合相等+按列名对齐"导致正确性恒为 0，2026-09-30 修订）；
值归一化——Decimal/float 按 round(4) 比较、datetime/date 统一 isoformat()、其余 str()；
行序无关；空结果集视为等价。报告注明口径
"""
from collections import Counter
from datetime import date, datetime
from decimal import Decimal


def _normalize_cell(v) -> str:
    """单元格归一化——两类结果集的值必须落到同一可比较形态"""
    if isinstance(v, bool):                      # bool 是 int 子类，必须先判
        return str(v)
    if isinstance(v, (Decimal, float)):
        return repr(round(float(v), 4))          # round(4) 口径（§3.3）
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _result_multiset(rows: list[dict] | None) -> Counter:
    """结果集 → 行签名多重集。
    行签名 = 行内值归一化后排序的元组（忽略列名与列序，v1.1 口径）；
    None 与空列表同义：空结果集（两空 = 等价语义 §3.3）"""
    if not rows:
        return Counter()
    return Counter(
        tuple(sorted(_normalize_cell(v) for v in row.values()))
        for row in rows
    )


def results_equal(golden_rows: list[dict] | None, actual_rows: list[dict] | None) -> bool:
    """结果集相等判定：行签名多重集相等（忽略列名/列序/行序，§3.3 v1.1）"""
    return _result_multiset(golden_rows) == _result_multiset(actual_rows)


def compute_sql_metrics(cases: list[dict]) -> dict:
    """输入逐用例（全部 enabled 且走完链路的用例）：
    {"has_golden": bool, "executed": bool, "exec_error": str|None,
     "golden_rows": list[dict]|None, "actual_rows": list[dict]|None, "retry_count": int}
    返回 {"executability": float|None, "correctness": float|None,
          "avg_retries": float, "support_exec": int, "support_correct": int,
          "errors": list[str]}                    # 前 5 条失败摘要（报告用，§3.3）
    """
    # step 1: 可执行率——分母 = 全部 enabled 且走完链路的用例
    support_exec = len(cases)
    executed = sum(1 for c in cases if c["executed"])
    executability = executed / support_exec if support_exec else None
    # step 2: 正确性——仅 has_golden 且双方都有结果的用例参与（分母独立呈现 §3.3）
    golden_cases = [c for c in cases if c["has_golden"]]
    comparable = [c for c in golden_cases if c["executed"] and c.get("golden_rows") is not None]
    support_correct = len(comparable)
    correct = sum(1 for c in comparable if results_equal(c["golden_rows"], c["actual_rows"]))
    correctness = correct / support_correct if support_correct else None
    # step 3: 辅助统计——平均重试 + 失败摘录
    avg_retries = (sum(c["retry_count"] for c in cases) / support_exec) if support_exec else 0.0
    errors = [c["exec_error"] for c in cases if not c["executed"] and c.get("exec_error")][:5]
    return {
        "executability": executability,
        "correctness": correctness,
        "avg_retries": avg_retries,
        "support_exec": support_exec,
        "support_correct": support_correct,
        "errors": errors,
    }
