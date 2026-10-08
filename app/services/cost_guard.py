"""
公网 demo 密钥日费用熔断（v1.1 PRD FR-05 规则 3 / 异常 E-04）

链路位置：QueryService.query() 的 finally 块在每次请求结束后调用 cost_guard.record()，
把本次请求的 LLM 费用记入"当日账户"；POST /api/query 的中间件在请求进入前调用
check_allowed()——硬上限触发后所有问数请求收到 429 + 公告文案，次日 0 点自动恢复。

设计决策（why）：
  1. 记账挂在 QueryService 而非 llm_factory callback —— tracker 是请求级实例，请求结束
     即丢弃，需要一个跨请求的常驻聚合点；QueryService 的 finally 恰是每请求必经的收口。
  2. 计价复用 03 评估口径（resolve_price_tier + providers pricing，缓存未命中价）——
     在线熔断数值与离线评估报告可直接对账，避免"两套成本口径"。
  3. 状态只存内存（当日累计 + 日期戳）—— demo 单进程部署，持久化无必要；
     进程重启清零的代价是熔断阈值可能晚触发几分钟，可接受（E-05 有进程守护兜底）。
  4. 开关与阈值全部走 conf/app_config.yaml security.cost_guard —— 默认关闭 = 旧行为，
     本地开发零影响（Feature Flags 纪律）。
"""
import datetime
import threading

from app.agent.usage import LLMCallRecord
from app.conf.app_config import app_config
from app.core.log import logger
from app.evaluation.pricing import resolve_price_tier


class CostGuard:
    """当日费用累计与熔断判定（进程级单例，见文件尾 cost_guard）。

    线程安全：FastAPI 在多 worker 下每进程一个实例，单进程内用 threading.Lock
    保护"读改写当日累计"的临界区；record() 内无 await，同步锁足够。
    """

    def __init__(self) -> None:
        # step 1: 状态初始化 —— 日期戳用于"次日 0 点自动恢复"判定（E-04：次日自动恢复）
        self._lock = threading.Lock()
        self._date: str = ""          # 当日日期（YYYY-MM-DD），变更即重置累计
        self._today_cost: float = 0.0     # 当日累计费用（元）
        self._paused_today: bool = False  # 硬上限触发标记（当日剩余时间恒拒绝）

    def _rollover_if_new_day(self) -> None:
        """跨日滚动：日期变化时清零累计与暂停标记（调用方必须已持有 _lock）"""
        today = datetime.date.today().isoformat()
        if today != self._date:
            self._date = today
            self._today_cost = 0.0
            self._paused_today = False

    def record(self, provider: str, records: list[LLMCallRecord]) -> float:
        """把一次请求的 LLM 记账条目折算为费用并累加（QueryService.finally 调用）。

        返回本次请求费用；计价口径与 03 评估一致（缓存未命中价，token 缺失的
        失败调用不计费）。开关关闭时直接返回 0，不产生任何状态。
        """
        cfg = app_config.security.cost_guard
        if not cfg.enabled:
            return 0.0
        # step 1: 逐条调用按 record.ts 判档计价（与 cost_metrics.compute_case_cost 同口径）
        cost = 0.0
        pricing = (app_config.llm.providers.get(provider) or {}).get("pricing") or {}
        tiers = pricing.get("tiers") or {}
        for r in records:
            tier_price = tiers.get(resolve_price_tier(r.ts, pricing)) or {}
            p_in, p_out = tier_price.get("input"), tier_price.get("output")
            if r.input_tokens is not None and p_in is not None:
                cost += r.input_tokens / 1e6 * p_in
            if r.output_tokens is not None and p_out is not None:
                cost += r.output_tokens / 1e6 * p_out
        # step 2: 累加并判断阈值 —— 软上限告警（仅日志，可重复触发），
        # 硬上限一次性置 paused（当日剩余请求全部 429，次日自动恢复）
        with self._lock:
            self._rollover_if_new_day()
            self._today_cost += cost
            if not self._paused_today and self._today_cost >= cfg.hard_limit:
                self._paused_today = True
                logger.warning(
                    f"[cost_guard] 日费用 {self._today_cost:.2f} 元达硬上限 "
                    f"{cfg.hard_limit} 元，今日对外服务已暂停（次日自动恢复）"
                )
            elif self._today_cost >= cfg.soft_limit:
                logger.warning(
                    f"[cost_guard] 日费用 {self._today_cost:.2f} 元超软上限 "
                    f"{cfg.soft_limit} 元（仅告警，服务继续）"
                )
        return cost

    def check_allowed(self) -> tuple[bool, str]:
        """中间件前置检查：当前是否允许问数请求（cost_guard.enabled 且当日熔断 → 拒绝）。

        返回 (allowed, message)；message 用于 429 响应体的友好公告文案。
        """
        cfg = app_config.security.cost_guard
        if not cfg.enabled:
            return True, ""
        with self._lock:
            self._rollover_if_new_day()
            if self._paused_today:
                return False, (
                    "今日体验额度已用完，服务暂时休息，明天再来吧。"
                    "（demo 每日费用达到保护阈值后自动暂停，次日恢复）"
                )
        return True, ""


# 进程级单例：QueryService（记账）与中间件（前置检查）共享同一份当日状态
cost_guard = CostGuard()


# ---- 使用示例（验证方式）----
# uv run python -c "
# from app.services.cost_guard import cost_guard
# from app.conf.app_config import app_config
# ok, msg = cost_guard.check_allowed()
# print(ok, msg)   # 开关默认关闭 → (True, '')，行为与 v1.0 完全一致
# "
