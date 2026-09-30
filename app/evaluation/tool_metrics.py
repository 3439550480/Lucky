"""
工具/能力触发指标（03 文档 §3.9）

v1 数据源：state["tool_calls"]（路由节点写入：dataquery → ["dataquery.search"]，否则 []）
——skill+工具化后由真实工具执行覆写同一字段，本模块接口不变、数据集无需重标
口径补充（04 tier-0 芯片）：capability_source == "user" 的用例由 runner 在提取阶段排除——
用户显式选择是确定性操作，不属于"agent 主动性"的度量范围
"""
_TOOL_FOR_CAPABILITY = {"dataquery": "dataquery.search"}


def extract_invoked_tools(state: dict) -> list[str]:
    """从最终 state 提取实际调用的工具集。
    v1 优先读 tool_calls 字段；缺失时从 capability 兜底映射（旧 state 兼容路径）"""
    tool_calls = state.get("tool_calls")
    if tool_calls is not None:
        return list(tool_calls)
    # 兜底：路由结果 → 工具映射（tool_calls 字段落地前的旧图兼容）
    return [_TOOL_FOR_CAPABILITY[c] for c in [state.get("capability") or ""] if c in _TOOL_FOR_CAPABILITY]


def compute_tool_metrics(cases: list[dict]) -> dict:
    """输入逐用例 {"expected_tools": list[str]|None, "invoked_tools": list[str]}
    返回 {"invocation_accuracy": float|None, "tool_recall": float|None, "tool_precision": float|None,
          "support": int, "confusion": {"missed": int, "false_positive": int, "correct": int}}
    expected_tools 为 None 的用例不计入（分母约定同 §3.1）
    """
    correct = missed = false_positive = 0
    support = 0
    total_exp = total_act = inter = 0
    for c in cases:
        expected = c.get("expected_tools")
        if expected is None:
            continue
        support += 1
        exp_set, act_set = set(expected), set(c["invoked_tools"])
        if exp_set == act_set:
            correct += 1                              # 完全一致（v1 主指标）
        elif exp_set - act_set:
            missed += 1                               # 漏调主导：期望的工具没被调用
        else:
            false_positive += 1                       # 误调主导：调了期望之外的工具
        # 集合层面的 recall/precision 聚合口径（跨用例累计，比逐用例二值平均更真实）
        total_exp += len(exp_set)
        total_act += len(act_set)
        inter += len(exp_set & act_set)
    if support == 0:
        return {"invocation_accuracy": None, "tool_recall": None, "tool_precision": None,
                "support": 0, "confusion": {"missed": 0, "false_positive": 0, "correct": 0}}
    return {
        "invocation_accuracy": correct / support,
        "tool_recall": inter / total_exp if total_exp else None,
        "tool_precision": inter / total_act if total_act else None,
        "support": support,
        "confusion": {"missed": missed, "false_positive": false_positive, "correct": correct},
    }
