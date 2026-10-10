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
# 确认词（策略 3.6）：只认"整句就是确认"——缺陷留档 + 修复说明见 is_confirm
# [S3 临时兜底] 结构化确认（前端确认/取消按钮 + 独立端点）落地前，先把误判率压到最低
_CONFIRM_SET = {"确认", "确定", "提交", "是的", "是", "对", "没错", "可以",
                "好的", "好", "ok", "确认提交", "没问题"}
_CONFIRM_REJECT = ("不", "别", "改", "换", "错", "重", "取消", "算了", "稍等", "等一下")
_CONFIRM_STRIP = re.compile(r"[\s，,。.！!？?~、；;：:]")   # 噪音归一：空白/标点
# 单据号：字母前缀 + ≥6 位数字（RK2026101001 / XS2026100201）
_DOC_PAT = re.compile(r"\b([A-Za-z]{1,3}\d{6,})\b")
# SKU：货号8位-色2位-尺码（15262011-01-42）
_SKU_PAT = re.compile(r"\b(\d{8}-\d{1,2}-\d{1,3})\b")
# 写意图：句首方向词（窄规则防误劫持——"昨天入库了多少件"不会命中）。
# 前缀白名单枚举而非任意 N 字符，避免"昨天/查一下"类查询前缀误入
_INTENT_PAT = re.compile(r"^(帮我|给我|登记|我要|我先|我先要|先|准备|现在|做一?个)?(要)?(入库|收货|出库)")
# 方向词后紧跟的"查询语义词"= 问数句而非写意图
# [2.0 P0 修复留档] 原版只有句首规则：裸方向词开头的问数句（"入库了多少件"/
# "出库流水"/"入库明细"）照样命中 → 被误劫持进出库槽位流程
_QUERY_FOLLOWERS = ("多少", "几", "什么", "哪些", "记录", "明细", "流水", "情况",
                    "数据", "统计", "查询", "列表", "汇总", "量", "件数", "单号", "单据")
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
    """确认门判定（策略 3.6）：只在"整句就是确认"时返回 True。

    [2.0 P0 修复留档] 原版对 ("确认","是的","对","提交","ok","OK") 做任意子串匹配——
    单字"对"使"不对"/"对不上"/"对，改成30件"全部命中，确认门被绕过直接记账
    （2026-10-10 核实）。修复三件套：
      ① 噪音归一（空白/标点/句尾语气词）后整句比对（"确认吧"→"确认"）
      ② 含否定或修正词（不/别/改/换/错/重/取消/稍等）直接否决
      ③ 只认白名单整句，不做前缀匹配（"确认下昨天入库多少"不再误命中）
    定位（2026-10-10 定稿）：本函数是**兜底通道**，不是主通道。主通道是结构化确认
    （前端按钮 → POST /api/flow/confirm → state.confirm_decision），服务端不解析文本；
    保留本函数只为降级场景：手打"确认"、接入方未实现按钮、无障碍输入。

    真实用法（flow_step 确认门，结构化决定优先）：
        decision = state.get("confirm_decision") or ""   # 按钮通道
        if pending.get("awaiting_confirm"):
            if decision == "cancel" or (not decision and is_cancel(query)): ...取消
            if decision != "confirm" and not is_confirm(query): ...推确认卡片
    """
    stripped = _CONFIRM_STRIP.sub("", text or "").lower()
    stripped = stripped.rstrip("吧啊哦嗯呀了")
    if not stripped:
        return False
    if any(word in stripped for word in _CONFIRM_REJECT):
        return False
    return stripped in _CONFIRM_SET


def detect_write_intent(text: str) -> bool:
    """写意图探测（flow_guard 用）：句首方向词，且方向词后不是查询语义词。

    命中："入库 20 件 15262011-01-42" / "帮我出库" / "出库单 RK2026101001"
    不命中："入库了多少件" / "出库流水" / "入库明细" / "出库量"
    （"入库了100件"仍算写意图——"了"只是语气，后面不是查询语义词）
    """
    stripped = (text or "").strip()
    match = _INTENT_PAT.match(stripped)
    if not match:
        return False
    rest = stripped[match.end():].lstrip()
    if rest.startswith("了"):          # "入库了多少件" → 剥"了"后按查询词判
        rest = rest[1:].lstrip()
    return not rest.startswith(_QUERY_FOLLOWERS)


def flow_expired(pending: dict, now: float | None = None) -> bool:
    """流程超时判定（10 分钟，策略 3.6）"""
    started_at = (pending or {}).get("started_at")
    if not started_at:
        return False
    return (now if now is not None else time.time()) - started_at > _FLOW_TIMEOUT_SECONDS
