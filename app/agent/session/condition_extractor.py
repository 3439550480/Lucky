"""
多轮条件提取公共组件（2.0 上下文策略 1.6.9，✅ 已采纳推广到 dataquery）

模式来源：replenish_plan._resolve_scope 的"prompt 不带历史 + 代码按需提条件"
（策略 1.6.8 树为示范模式）。本组件把它泛化为六类条件，供 replenish 与
dataquery（generate_sql）共用。

设计要点：
- 零 LLM：六类条件取值域有限、规则可实现——成本 0、确定性、可单测
- 来源优先级：当前 query（提到就用）→ 上一轮 user query（继承）→ 无
- 时间换算依赖传入的 today（来自 state["date_info"]，节点已有）
- 国庆档是数据集常量（2026-10-01 ~ 10-07），非通用节假日推算

已知边界（策略 1.6.9 权衡说明）：规则覆盖不到的模糊指代（"那个红色的鞋"）
会丢失 → generate_sql 的继承块里**保留最近 1 轮 user 原文作兜底**。
"""
import re
import datetime

# 取值域（与 dim_product / dim_replenish_policy / meta_config alias 逐字对应）
_CATEGORIES = ("鞋类", "服装类", "配件类")
_GENDERS = ("男子", "女子", "儿童", "男女同款")
_SERIES = ("跑步", "篮球", "综训", "生活", "潮流", "冠军", "足球")
_METRICS = ("销售额", "销量", "折扣率", "动销率", "客单价", "营业额")
_SORTS = ("按销量", "按金额", "按销售额", "降序", "从高到低", "从低到高")

# 国庆档数据集常量（策略 1.6.9；数据集扩窗时同步改这里与 meta_config）
_HOLIDAY_RANGE = "2026-10-01 ~ 2026-10-07（国庆档）"

_DATE_PAT = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{8}")


def _first_hit(text: str, words: tuple[str, ...]) -> str | None:
    """返回第一个命中的关键词（取值域互斥场景用）"""
    for w in words:
        if w in text:
            return w
    return None


def extract_categories(text: str) -> list[str]:
    """品类提取（replenish 的范围语义；多值都给）"""
    return [c for c in _CATEGORIES if c in (text or "")]


def _extract_time(text: str, today: datetime.date | None) -> str | None:
    """时间条件：相对词换算（依赖 today）→ 具体日期透传 → 国庆档常量"""
    if "国庆" in text:
        return _HOLIDAY_RANGE
    date_pat = _DATE_PAT.search(text or "")
    if date_pat:
        raw = date_pat.group()
        return raw if "-" in raw else f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if today is None:
        return "昨天" if "昨天" in text else ("今天" in text and "今天" or None)
    if "昨天" in text:
        return f"{today - datetime.timedelta(days=1)}（昨天）"
    if "今天" in text:
        return f"{today}（今天）"
    if "本周" in text:
        monday = today - datetime.timedelta(days=today.weekday())
        return f"{monday} ~ {today}（本周至今）"
    return None


def extract_conditions(text: str, today: datetime.date | None = None) -> dict[str, str]:
    """从一句话提取结构化条件（只含命中的键；单值取值域取第一个命中）"""
    text = text or ""
    out: dict[str, str] = {}
    cats = extract_categories(text)
    if cats:
        out["品类"] = "、".join(cats)
    gender = _first_hit(text, _GENDERS)
    if gender:
        out["人群"] = gender
    series = _first_hit(text, _SERIES)
    if series:
        out["系列"] = series
    time_val = _extract_time(text, today)
    if time_val:
        out["时间"] = time_val
    metric = _first_hit(text, _METRICS)
    if metric:
        out["指标"] = metric
    sort_kw = _first_hit(text, _SORTS)
    if sort_kw:
        out["排序"] = sort_kw
    return out


def _last_user_text(history: list[dict], current_query: str) -> str | None:
    """上一轮 user 原文（跳过与当前问句相同的条目——extract_keywords 先写入轨迹
    与 replenish 直连入口两种轨迹状态的兼容，同 replenish 旧 _resolve_scope 结论）"""
    for message in reversed(history or []):
        if message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        if content == current_query:
            continue
        return content
    return None


def resolve_categories(current_query: str, history: list[dict]) -> list[str]:
    """品类范围解析（replenish 专用语义）：本轮 → 继承上一轮 → 全店（空列表）。
    与原 replenish_plan._resolve_scope 行为逐字一致，仅实现收敛到本组件"""
    hits = extract_categories(current_query or "")
    if hits:
        return hits
    prev = _last_user_text(history, current_query)
    if prev:
        hits = extract_categories(prev)
        if hits:
            return hits
    return []


def build_inherited_block(current_query: str, history: list[dict],
                          today: datetime.date | None = None) -> str:
    """生成 generate_sql 的 {inherited_conditions} 块（替换整段 conversation_history）。

    形态（策略 1.6.9 产出形态）：
      【继承条件（本轮未提及，来自上一轮）】
      品类：鞋类
      时间：2026-10-07（昨天）
      【上一轮提问（用于理解指代，如"那个/刚才"）】
      昨天店里卖了多少钱
    两者皆空 → 空串（本轮自足，历史区不占 token——缓存最友好形态）。
    """
    current = extract_conditions(current_query, today)
    prev_text = _last_user_text(history, current_query)
    prev = extract_conditions(prev_text, today) if prev_text else {}

    inherited = {k: v for k, v in prev.items() if k not in current}
    lines: list[str] = []
    if inherited:
        lines.append("【继承条件（本轮未提及，来自上一轮）】")
        lines += [f"{k}：{v}" for k, v in inherited.items()]
    if prev_text:
        lines.append("【上一轮提问（用于理解指代，如\"那个/刚才\"）】")
        lines.append(prev_text)
    return "\n".join(lines)
