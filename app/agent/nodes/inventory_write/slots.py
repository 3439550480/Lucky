"""
出入库槽位解析纯函数（2.0 上下文策略 3.5：规则优先，LLM 兜底暂不启用）

全部为纯函数——GWT 单测直接覆盖（tests/test_flow_slots.py）。
解析可以扩展 LLM，但流程推进与校验永远是规则的（策略 3.5 原则）。
"""
import re
import time

# 槽位取值域
_DIRECTIONS = {"入库": "in", "收货": "in", "出库": "out", "卖出": "out"}
_CANCEL_WORDS = ("取消", "算了", "不弄了", "不做了", "先不")
_CONFIRM_WORDS = ("确认", "是的", "对", "提交", "ok", "OK")
# 单据号：字母前缀 + ≥6 位数字（RK2026101001 / XS2026100201）
_DOC_PAT = re.compile(r"\b([A-Za-z]{1,3}\d{6,})\b")
# SKU：货号8位-色2位-尺码（15262011-01-42）
_SKU_PAT = re.compile(r"\b(\d{8}-\d{1,2}-\d{1,3})\b")
# 写意图：句首方向词（窄规则防误劫持——"昨天入库了多少件"不会命中）。
# 前缀白名单枚举而非任意 N 字符，避免"昨天/查一下"类查询前缀误入
_INTENT_PAT = re.compile(r"^(帮我|给我|登记|我要|我先|我先要|先|准备|现在|做一?个)?(要)?(入库|收货|出库)")
_FLOW_TIMEOUT_SECONDS = 600   # 10 分钟（策略 3.6）


def parse_direction(text: str) -> str | None:
    """方向槽位：第一个命中的方向词"""
    for word, direction in _DIRECTIONS.items():
        if word in text:
            return direction
    return None


def parse_doc_no(text: str) -> str | None:
    """单据号槽位：字母前缀+数字串，统一大写"""
    match = _DOC_PAT.search(text or "")
    return match.group(1).upper() if match else None


def parse_sku(text: str) -> str | None:
    """SKU 槽位：货号-色码-尺码 精确格式（v1 不做模糊匹配，宁缺勿错）"""
    match = _SKU_PAT.search(text or "")
    return match.group(1) if match else None


def parse_qty(text: str) -> int | None:
    """数量槽位：优先"N 件/个"搭配；否则取剔除单据号/SKU 后的独立整数"""
    text = text or ""
    bundled = re.search(r"(\d+)\s*(?:件|个|双)", text)
    if bundled:
        value = int(bundled.group(1))
        return value if value > 0 else None
    residue = _DOC_PAT.sub(" ", text)
    residue = _SKU_PAT.sub(" ", residue)
    for match in re.finditer(r"\b(\d{1,4})\b", residue):
        value = int(match.group(1))
        if value > 0:
            return value
    return None


def is_cancel(text: str) -> bool:
    return any(w in (text or "") for w in _CANCEL_WORDS)


def is_confirm(text: str) -> bool:
    return any(w in (text or "") for w in _CONFIRM_WORDS)


def detect_write_intent(text: str) -> bool:
    """写意图探测（flow_guard 用）：句首方向词——窄规则防误劫持查询
    （"昨天入库了多少件"/"出库流水"不以方向词开头，不会命中）"""
    return bool(_INTENT_PAT.match((text or "").strip()))


def flow_expired(pending: dict, now: float | None = None) -> bool:
    """流程超时判定（10 分钟，策略 3.6）"""
    started_at = (pending or {}).get("started_at")
    if not started_at:
        return False
    return (now if now is not None else time.time()) - started_at > _FLOW_TIMEOUT_SECONDS
