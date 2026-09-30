"""
意图分类指标（03 文档 §3.2）

v1 口径对齐（能力路由两分流）：expected.intent 取值 dataquery | default；
现状五分类跑 baseline 时归并映射：data_query/follow_up → dataquery，recap/chitchat/help → default
因此 v1 阶段意图指标与工具触发指标（tool_metrics）数值上等价（同一次分流决策的两种视角），
分化发生在 skill+工具化之后（§3.9）
"""
# baseline 归并映射（五分类体系的历史取值 → 两分流）
_INTENT_ALIAS = {
    "data_query": "dataquery",
    "follow_up": "dataquery",
    "recap": "default",
    "chitchat": "default",
    "help": "default",
}


def normalize_intent(intent: str) -> str:
    """五分类 → 两分流归并；已是 dataquery/default 的原样返回"""
    return _INTENT_ALIAS.get(intent, intent)


def compute_intent_metrics(cases: list[dict]) -> dict:
    """输入逐用例 {"expected_intent": str, "actual_intent": str}（仅含双方都有的用例）
    返回 {"accuracy": float|None, "support": int, "confusion_matrix": {expected: {actual: count}}}
    support=0 时 accuracy 返回 None（报告显示"—"——§5.1：绝不输出 0 分）
    """
    # step 1: 归并后构建混淆矩阵（expected → actual 计数）
    confusion: dict[str, dict[str, int]] = {}
    for c in cases:
        exp = normalize_intent(c["expected_intent"])
        act = normalize_intent(c["actual_intent"])
        confusion.setdefault(exp, {}).setdefault(act, 0)
        confusion[exp][act] += 1
    support = sum(sum(row.values()) for row in confusion.values())
    # step 2: 准确率 = 对角线之和 / 总数
    if support == 0:
        return {"accuracy": None, "support": 0, "confusion_matrix": {}}
    correct = sum(confusion[e].get(e, 0) for e in confusion)
    return {"accuracy": correct / support, "support": support, "confusion_matrix": confusion}
