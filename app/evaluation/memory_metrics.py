"""
基础回忆指标（06 文档 §3.5，v1.1 分层口径）

四指标（2026-10-03 评审修订：检索层/输出层分层，原 recall 拆开）：
- store_rate       落库成功率：提取→store 内容中出现期望值（确定性）
- retrieval_rate   检索层召回：probe query 的向量检索结果包含期望值条目（确定性、无 LLM——
                   写入端漏算向量的 bug 会在此层精确暴露：content 在库但 search 不可见）
- recall_accuracy  输出层回忆：LLM 回答包含期望值（flaky 维度，单独呈现）
- persistence      跨实例存活代理：fresh store 实例重读磁盘后的 recall（真正的分进程版在 07 手工验收）

值匹配口径：纯数字值加数字边界（防"1"匹配"123456"式子串假阳性，评审意见 2）
"""
import re


def value_in_text(value: str, text: str) -> bool:
    """边界感知的值匹配：纯数字值要求数字边界，其余子串匹配"""
    if not value or text is None:
        return False
    if value.isdigit():
        return bool(re.search(r"(?<!\d)" + re.escape(value) + r"(?!\d)", text))
    return value in text


def compute_memory_metrics(memory_rows: list) -> dict:
    """输入逐 memory 用例 {"stored": bool, "retrieved": bool, "recalled": bool}
    空列表 → 全 None（绝不输出 0 分，03 §5.1 语义）"""
    if not memory_rows:
        return {"store_rate": None, "retrieval_rate": None, "recall_accuracy": None,
                "persistence": None, "support": 0,
                "note": "persistence 为跨实例代理口径（fresh 实例重读磁盘），分进程版在 07 验收"}

    def _mean(flags: list) -> float:
        return sum(1 for f in flags if f) / len(flags)

    recall = _mean([r["recalled"] for r in memory_rows])
    return {
        "store_rate": _mean([r["stored"] for r in memory_rows]),
        "retrieval_rate": _mean([r["retrieved"] for r in memory_rows]),
        "recall_accuracy": recall,
        # v1 代理口径：runner 的 probe 阶段用全新 store 实例重读磁盘，
        # 能回忆 = 数据确实落盘存活（真正的分进程/跨实例版在 07 验收手工执行）
        "persistence": recall,
        "support": len(memory_rows),
    }
