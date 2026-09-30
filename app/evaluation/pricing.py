"""
计费档位判定器（03 文档 §3.4）

DeepSeek 峰谷是计费时段而非模型：peak = 工作日 9:00–12:00、14:00–18:00（北京时间），
offpeak = 其余全部（含周末、法定节假日）。Qwen/GLM 仅 standard 单档，直接返回。
节假日表 evaluation/holidays.json 人工按年维护；未维护的年份按普通周末规则回退 + warning
"""
import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from app.core.log import logger

# 北京时间 = 固定 UTC+8（无夏令时，历史与可预见未来均无调整，固定偏移语义等价；
# 不用 zoneinfo：Windows 的 Python 缺 tzdata 数据库会抛 ZoneInfoNotFoundError，还需额外装包）
BEIJING = timezone(timedelta(hours=8))
HOLIDAYS_PATH = Path(__file__).resolve().parents[2] / "evaluation" / "holidays.json"

# 高峰时段边界（北京时间，工作日）
_PEAK_WINDOWS = ((time(9, 0), time(12, 0)), (time(14, 0), time(18, 0)))
_holiday_cache: dict | None = None       # 进程级缓存（评测进程生命周期内读一次）


def _load_holidays() -> dict:
    """读取节假日表 {年份: [ISO 日期, ...]}；文件缺失/损坏返回空表（调用处回退 + warning）"""
    global _holiday_cache
    if _holiday_cache is None:
        try:
            _holiday_cache = json.loads(HOLIDAYS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"节假日表不可用（{e}），峰谷判定按普通周末规则回退")
            _holiday_cache = {}
    return _holiday_cache


def _is_holiday(d, holidays: dict) -> bool:
    return d.isoformat() in holidays.get(str(d.year), [])


def resolve_price_tier(ts: float | datetime, pricing: dict) -> str:
    """按调用时间戳判定计费档位（03 §3.4）。
    ts 支持 float（LLMCallRecord.ts）或 datetime；
    pricing 形如 {"tiers": {...}, "tier_rules": "beijing_workweek"|None}
    """
    # step 1: 单档 provider（Qwen/GLM）直接返回唯一档
    tiers = pricing.get("tiers") or {}
    if len(tiers) == 1:
        return next(iter(tiers))
    # step 2: 多档但无判定规则 → 无法判定，回退 offpeak（保守取低价，报告展示 tier_distribution）
    if pricing.get("tier_rules") != "beijing_workweek":
        return "offpeak"
    # step 3: beijing_workweek 判定器
    dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, tz=timezone.utc)
    local = dt.astimezone(BEIJING)
    holidays = _load_holidays()
    if local.year not in holidays:
        logger.warning(f"节假日表未维护 {local.year} 年，按普通周末规则判定峰谷")
    is_offpeak = (
        local.weekday() >= 5                                    # 周末全天
        or _is_holiday(local.date(), holidays)                  # 法定节假日全天
        or not any(start <= local.time() < end for start, end in _PEAK_WINDOWS)
    )
    return "offpeak" if is_offpeak else "peak"
