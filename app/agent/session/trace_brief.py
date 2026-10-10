"""
轨迹摘要 brief（2.0 上下文策略 1.6.16；features.trace_brief 开关，默认关）

定位：brief 是**渲染产物**不是存储——轨迹条目存完整原文（可回溯可审计），
拼 prompt 时由 history_provider 现算简洁版注入。策略 1.6.10：
摘要式累积 = 追加式保缓存（第 10 轮成本 147 vs 全量 366，省 60%）。

生成规则（1.6.16，"不总结，只取首行"——聚合工作已在 SQL 里做完）：
  第一优先：首行的数值型字段（跳过 bool），最多 3 个
  兜底：整行无数值列 → 取前 2 个字段原值（单值截断 20 字）
  多行结果：前缀行数（`N 行；首行 …`）——明细行的价值只在行数
  空结果：`无数据`
复杂度 O(首行字段数)，与 len(result) 完全无关（5000 行和 3 行开销一样）。
"""
_MAX_NUMERIC = 3        # 数值型字段上限
_MAX_TEXT = 2           # 兜底：文本型字段上限
_TEXT_MAX_LEN = 20      # 兜底：单个文本值截断长度


def build_brief(result: list[dict]) -> str:
    """从 SQL 结果生成轨迹摘要。只看第一行——与总行数无关"""
    if not result:
        return "无数据"
    row = result[0]                                   # ★ 只看第一行
    parts: list[str] = []

    # 第一优先：数值型字段（跳过 bool —— 它是 int 的子类）
    for col, val in row.items():
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            parts.append(f"{col} {val:,.0f}")
            if len(parts) >= _MAX_NUMERIC:
                break

    # 兜底：整行无数值列（如纯文本结果）→ 取前 2 个字段原值并截断
    if not parts:
        for col, val in list(row.items())[:_MAX_TEXT]:
            s = str(val)
            if len(s) > _TEXT_MAX_LEN:
                s = s[:_TEXT_MAX_LEN] + "…"
            parts.append(f"{col} {s}")

    head = " / ".join(parts) if parts else "（空行）"
    return head if len(result) == 1 else f"{len(result)} 行；首行 {head}"
