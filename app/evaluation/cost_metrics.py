"""
成本指标（03 文档 §3.4）

口径（01 §7.2 / 03 §7.4）：v1 一律按"缓存未命中价"核算（缓存命中节省不体现）；
档位单价缺失（null）→ 对应费用字段 None，报告标注"未配置单价"
"""
from app.agent.usage import LLMCallRecord
from app.evaluation.pricing import resolve_price_tier


def compute_case_cost(records: list[LLMCallRecord], tracker_summary: dict,
                      provider_cfg: dict, forced_tier: str | None) -> dict:
    """单用例成本。逐条调用按 record.ts 判档（forced_tier 非空则恒用该档——
    实验 --pricing-tier 强制固定时消除时间分布噪声，03 §7.3）"""
    pricing = provider_cfg.get("pricing") or {}
    tiers = pricing.get("tiers") or {}
    # step 1: 逐调用计费（token 为 None 的失败调用不计费用，仅计延迟与档位计数）
    cost_input = cost_output = 0.0
    cost_known = False                       # 至少一条费用可算时为 True，否则输出 None
    tier_dist: dict[str, int] = {}
    for r in records:
        tier = forced_tier or resolve_price_tier(r.ts, pricing)
        tier_dist[tier] = tier_dist.get(tier, 0) + 1
        tier_price = tiers.get(tier) or {}
        p_in, p_out = tier_price.get("input"), tier_price.get("output")
        if r.input_tokens is not None and p_in is not None:
            cost_input += r.input_tokens / 1e6 * p_in
            cost_known = True
        if r.output_tokens is not None and p_out is not None:
            cost_output += r.output_tokens / 1e6 * p_out
            cost_known = True
    # step 2: 汇总输出（口径字段齐全，供报告与 aggregate 消费）
    return {
        "input_tokens": tracker_summary.get("input_tokens"),
        "output_tokens": tracker_summary.get("output_tokens"),
        "total_tokens": tracker_summary.get("total_tokens"),
        "latency_ms": tracker_summary.get("total_latency_ms", 0),
        "calls": tracker_summary.get("calls", 0),
        "errors": tracker_summary.get("errors", 0),
        "cost_input": round(cost_input, 6) if cost_known else None,
        "cost_output": round(cost_output, 6) if cost_known else None,
        "cost_total": round(cost_input + cost_output, 6) if cost_known else None,
        "tier_distribution": tier_dist,
        "by_stage": tracker_summary.get("by_stage", {}),   # 环节归属直接复用 tracker 聚合（§3.4）
    }


def aggregate_costs(case_costs: list[dict]) -> dict:
    """数据集级：总/均费用、token 分布、环节聚合（跨用例累加 by_stage）、峰谷占比"""
    if not case_costs:
        return {"total_cost": None, "avg_cost_per_case": None,
                "tokens": {"input": 0, "output": 0, "total": 0},
                "by_stage": {}, "tier_distribution": {}}
    # step 1: 费用——全部用例都"未配置单价"时输出 None（绝不假装 0 成本）
    known = [c for c in case_costs if c.get("cost_total") is not None]
    total_cost = round(sum(c["cost_total"] for c in known), 6) if known else None
    # step 2: token 与环节聚合
    agg_stage: dict[str, dict] = {}
    for c in case_costs:
        for stage, s in (c.get("by_stage") or {}).items():
            b = agg_stage.setdefault(stage, {"calls": 0, "input_tokens": 0, "latency_ms": 0})
            b["calls"] += s.get("calls", 0)
            b["input_tokens"] += s.get("input_tokens", 0)
            b["latency_ms"] += s.get("latency_ms", 0)
    tier_totals: dict[str, int] = {}
    for c in case_costs:
        for t, n in (c.get("tier_distribution") or {}).items():
            tier_totals[t] = tier_totals.get(t, 0) + n
    return {
        "total_cost": total_cost,
        "avg_cost_per_case": round(total_cost / len(case_costs), 6) if total_cost is not None else None,
        "tokens": {
            "input": sum(c.get("input_tokens") or 0 for c in case_costs),
            "output": sum(c.get("output_tokens") or 0 for c in case_costs),
            "total": sum(c.get("total_tokens") or 0 for c in case_costs),
        },
        "by_stage": agg_stage,
        "tier_distribution": tier_totals,
    }
