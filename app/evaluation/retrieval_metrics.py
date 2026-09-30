"""
检索三通道指标（03 文档 §3.1）

通道：字段（retrieved_column_infos.id）/ 指标（retrieved_metric_infos.name）/
     取值（retrieved_value_infos.value）——各通道独立计算、期望集为空不计入（分母只含有标注用例）
匹配：exact = 完全相等（大小写敏感）；contains = 召回标识包含期望串（命名差异权宜，报告注明口径）

纯函数、无 IO，可单测（03 验收标准 3）
"""
from app.entities.column_info import ColumnInfo
from app.entities.metric_info import MetricInfo
from app.entities.value_info import ValueInfo


def _match(retrieved_id: str, expected_set: set[str], mode: str) -> bool:
    """单元素匹配判定（exact / contains 两档）"""
    if mode == "contains":
        return any(exp in retrieved_id for exp in expected_set)
    return retrieved_id in expected_set          # exact：完全相等


def hit_at_k(retrieved: list[str], expected: set[str], k: int, mode: str = "exact") -> float:
    """前 k 个召回中至少命中一个期望元素 → 1.0，否则 0.0"""
    return 1.0 if any(_match(r, expected, mode) for r in retrieved[:k]) else 0.0


def mrr(retrieved: list[str], expected: set[str], mode: str = "exact") -> float:
    """第一个命中位置的倒数；无命中 = 0"""
    for rank, r in enumerate(retrieved, start=1):
        if _match(r, expected, mode):
            return 1.0 / rank
    return 0.0


def precision_at_k(retrieved: list[str], expected: set[str], k: int, mode: str = "exact") -> float:
    """|前k∩E| / min(k, |R|)"""
    if not retrieved:
        return 0.0
    hits = sum(1 for r in retrieved[:k] if _match(r, expected, mode))
    return hits / min(k, len(retrieved))


def recall_at_k(retrieved: list[str], expected: set[str], k: int, mode: str = "exact") -> float:
    """|前k∩E| / |E|"""
    if not expected:
        return 0.0
    hits = sum(1 for r in retrieved[:k] if _match(r, expected, mode))
    return hits / len(expected)


# 三通道的提取器映射：state 字段 → 元素标识键（03 §3.1 表格的代码化）。
# tables 通道从字段召回 id 的表名前缀提取（state 无独立表召回字段，表级命中率为可选维度）
_CHANNEL_EXTRACTORS = {
    "columns": lambda state: [c.id for c in (state.get("retrieved_column_infos") or [])],
    "metrics": lambda state: [m.name for m in (state.get("retrieved_metric_infos") or [])],
    "values": lambda state: [v.value for v in (state.get("retrieved_value_infos") or [])],
    "tables": lambda state: sorted({c.id.split(".")[0] for c in (state.get("retrieved_column_infos") or [])}),
}


def compute_retrieval_metrics(state: dict, expected: dict, k: int, match_mode: str) -> dict:
    """单用例三通道指标。期望为空的通道输出 None（数据集聚合时跳过，§3.1 分母约定）

    返回 {"columns": {...}|None, "metrics": {...}|None, "values": {...}|None, "tables": {...}|None}
    """
    out: dict = {}
    for channel, extract in _CHANNEL_EXTRACTORS.items():
        exp_list = expected.get(channel) or []
        if not exp_list:                     # 无标注 → 不计入该通道指标
            out[channel] = None
            continue
        retrieved = extract(state)
        exp_set = set(exp_list)
        out[channel] = {
            "hit_at_k": hit_at_k(retrieved, exp_set, k, match_mode),
            "mrr": mrr(retrieved, exp_set, match_mode),
            "precision_at_k": precision_at_k(retrieved, exp_set, k, match_mode),
            "recall_at_k": recall_at_k(retrieved, exp_set, k, match_mode),
        }
    return out
