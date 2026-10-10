"""
replenish 能力处理节点（2.0 S3b）—— 补货计划：固化算法 + LLM 解释

[规则3 设计留档] 为什么不是 dataquery 式 prompt+子图：
补货是"查数 + 业务数学 + 决策建议"的复合任务，不是 NL→SQL 查询——
让 LLM 每次现场发明补货 SQL，同一问题会算出不同数字（决策场景不可接受，
评估也无法打 golden 答案）；而补货不需要语义召回（表与口径固定）、不需要
校验修正闭环（SQL 是参数化模板）。因此采用第三种能力形状：单节点三步流水：
  ① 取数：固定参数化 SQL（近 7 天日均销量 / 最新快照可用库存 / 品类策略 / SKU 属性）
  ② 计算：纯 Python 固化公式（GWT 单测覆盖：tests/test_replenish_algo.py）
  ③ 解释：LLM 只做自然语言包装（禁止改数字），SSE 发 replenish 结构化事件
       + explanation 文本兜底（旧渲染器不至于空白）。

公式（D14，取整规则钉死）：
  目标库存   = ceil(日均销量 × (覆盖天数 + 安全天数))
  建议补货量 = max(0, 目标库存 − 可用库存 − 在途)
  在途 v1 恒 0（数据集无在途单据类型，减项为 warehouse_replenish 扩展保留）
  日均销量   = 近 window 天销量 / window（数据集即国庆 7 天，含假期高基线——演示口径）
输出：仅建议量 > 0 的 SKU，按断货倒计时（可用库存/日均销量）升序——越快断货越靠前。
品类范围：本轮查询提及的品类（鞋类/服装类/配件类）优先；未提及则继承对话
最近一轮（多轮省略句），仍无则全店。
warehouse_replenish 预留：dim_replenish_policy.scope='warehouse' 行已预埋，
参数化 scope 后在 capability_config.yaml 加条目即可启用（现注释预留，fail-fast 约束）。
"""
import math
import yaml
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.session.history_provider import get_conversation_history
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

# 品类取值与 dim_replenish_policy.category / dim_product.category_l1 逐字对应
_CATEGORIES = ("鞋类", "服装类", "配件类")
_PLAN_EVENT_ROWS = 50   # SSE 事件携带的明细行上限（表格演示够用，防止超大 payload）


# ==================== ② 固化算法（纯函数，GWT 单测直接覆盖） ====================

def compute_replenish(
    daily_sales: dict[str, float],
    available: dict[str, int],
    policy: dict[str, dict],
    sku_meta: dict[str, dict],
    on_transit: dict[str, int] | None = None,
) -> list[dict]:
    """固化补货算法：目标覆盖 + 安全 − 现有 − 在途；只留缺口项，按断货紧急度升序。

    Args:
        daily_sales: sku_id → 日均销量（件/天）
        available:   sku_id → 最新快照可用库存（件）
        policy:      品类 → {coverage_days, safety_days}（来自 dim_replenish_policy，store 行）
        sku_meta:    sku_id → {product_name, category_l1}
        on_transit:  sku_id → 在途数量（v1 恒空，warehouse 预留）
    Returns:
        按 days_left 升序（同分按建议量降序）的缺口行列表；建议量已向上取整到整件。
    """
    on_transit = on_transit or {}
    rows: list[dict] = []
    for sku_id, meta in sku_meta.items():
        pol = policy.get(meta.get("category_l1", ""))
        if not pol:
            continue    # 无策略品类不生成建议（宁缺勿错，不让模型补口径）
        avg_daily = float(daily_sales.get(sku_id, 0.0))
        avail = int(available.get(sku_id, 0))
        transit = int(on_transit.get(sku_id, 0))
        target = math.ceil(avg_daily * (int(pol["coverage_days"]) + int(pol["safety_days"])))
        suggest = max(0, target - avail - transit)
        if suggest <= 0:
            continue    # 仅缺口项
        # 断货倒计时：可用库存烧完的天数；无销量=暂不断货（且此时建议量必为 0，不会进到这里）
        days_left = round(avail / avg_daily, 1) if avg_daily > 0 else None
        rows.append({
            "sku_id": sku_id,
            "product_name": meta.get("product_name", ""),
            "category_l1": meta.get("category_l1", ""),
            "avg_daily_sales": round(avg_daily, 2),
            "coverage_days": int(pol["coverage_days"]),
            "safety_days": int(pol["safety_days"]),
            "available_qty": avail,
            "on_transit": transit,
            "target_qty": target,
            "suggest_qty": suggest,
            "days_left": days_left,
        })
    # 紧急度排序：断货倒计时小者靠前；无销量（days_left=None）垫底；同分建议量大者靠前
    rows.sort(key=lambda r: (
        r["days_left"] is None,
        r["days_left"] if r["days_left"] is not None else 0.0,
        -r["suggest_qty"],
    ))
    return rows


# ==================== 范围解析（品类，含多轮继承） ====================

def _parse_scope(query: str) -> list[str]:
    return [c for c in _CATEGORIES if c in query]


def _resolve_scope(query: str, state: DataAgentState) -> list[str]:
    """品类范围：本轮优先 → 继承对话最近一轮的用户提及 → 全店（空列表）。
    注意：replenish 直连入口不走 extract_keywords，当前问句通常尚未入轨迹；
    但 inventory 同入口路径下当前问句可能已写入——跳过与当前问句相同的
    末条用户消息，两种情形下"上一轮"语义都正确。"""
    hits = _parse_scope(query)
    if hits:
        return hits
    for message in reversed(get_conversation_history(state)):
        if message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        if content == query:
            continue    # 当前轮已入轨迹（extract_keywords 先写的情形），跳过找真正的上一轮
        hits = _parse_scope(content)
        if hits:
            logger.info(f"[replenish] 品类继承自上一轮: {hits}")
            return hits
        break   # 只看最近一轮，避免把很久以前的限定词翻出来
    return []


# ==================== 节点主体 ====================

async def replenish_plan(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """补货计划节点：① 固定 SQL 取数 → ② 固化算法计算 → ③ LLM 解释（流式 SSE）。"""
    writer = runtime.stream_writer
    llm = runtime.context["llm"]
    dw = runtime.context["dw_mysql_repository"]
    step = "生成补货计划"
    writer({"type": "progress", "step": step, "status": "running"})
    query = state["query"]

    try:
        # ---- ① 取数：固定参数化 SQL（补货不做语义召回，表与口径固定）----
        # 近期销量（件/SKU）：窗口 = 数据集日期跨度（国庆 7 天）
        sales_rows = await dw.run(
            "SELECT sku_id, SUM(quantity) AS sold FROM fact_sales GROUP BY sku_id",
            timeout_ms=15000, max_rows=5000,
        )
        range_rows = await dw.run(
            "SELECT MIN(date_id) AS mn, MAX(date_id) AS mx FROM fact_sales",
            timeout_ms=5000, max_rows=1,
        )
        window = 7
        if range_rows and range_rows[0].get("mn") is not None:
            window = max(1, int(range_rows[0]["mx"]) - int(range_rows[0]["mn"]) + 1)
        daily_sales = {r["sku_id"]: float(r["sold"]) / window for r in sales_rows}

        # 最新快照可用库存（"当前"口径 = MAX(snapshot_date)，meta_config 已同步该语义）
        snap_rows = await dw.run(
            "SELECT sku_id, available_qty FROM dim_inventory_snapshot "
            "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM dim_inventory_snapshot)",
            timeout_ms=15000, max_rows=5000,
        )
        available = {r["sku_id"]: int(r["available_qty"]) for r in snap_rows}

        # 品类策略（仅 store 行生效，warehouse 预留不取）
        policy_rows = await dw.run(
            "SELECT category, coverage_days, safety_days FROM dim_replenish_policy "
            "WHERE scope = 'store'",
            timeout_ms=5000, max_rows=10,
        )
        policy = {r["category"]: dict(r) for r in policy_rows}

        # SKU 属性（品名/品类，用于展示与策略映射）
        sku_rows = await dw.run(
            "SELECT s.sku_id, p.product_name, p.category_l1 "
            "FROM dim_sku s JOIN dim_product p ON s.product_id = p.product_id",
            timeout_ms=15000, max_rows=5000,
        )
        sku_meta = {r["sku_id"]: {"product_name": r["product_name"],
                                  "category_l1": r["category_l1"]} for r in sku_rows}

        # ---- ② 固化计算 ----
        plan = compute_replenish(daily_sales, available, policy, sku_meta)
        scope = _resolve_scope(query, state)
        if scope:
            plan = [r for r in plan if r["category_l1"] in scope]
            writer({"type": "progress", "step": f"范围：{'、'.join(scope)}", "status": "success"})

        total_suggest = sum(r["suggest_qty"] for r in plan)
        plan_rows = plan[:_PLAN_EVENT_ROWS]
        writer({"type": "progress", "step": step, "status": "success"})

        # ---- ③ SSE 结构化事件（新事件类型；QueryService 通用透传，无需改动）----
        writer({
            "type": "replenish",
            "plan": plan_rows,
            "total_gap": len(plan),
            "total_suggest_qty": total_suggest,
            "scope": scope,
        })

        # explanation 文本兜底：结构化事件之外，保证旧渲染器与记忆提取拿到文本
        if not plan:
            explanation = "当前库存均可满足品类覆盖目标，暂无需要补货的 SKU。"
        else:
            plan_summary = yaml.dump(
                {"缺口SKU数": len(plan), "合计建议补货件数": total_suggest,
                 "明细（按断货紧急度排序）": plan[:10]},
                allow_unicode=True, sort_keys=False,
            )
            prompt = PromptTemplate(
                template=load_prompt("replenish_plan"),
                input_variables=["system_prefix", "plan_summary", "query"],
            )
            chain = prompt | llm | StrOutputParser()
            from app.agent.session.prefix import build_system_prefix
            explanation = await chain.ainvoke({
                "system_prefix": build_system_prefix(runtime.context.get("staff")),
                "plan_summary": plan_summary,
                "query": query,
            })
        logger.info(f"补货计划：{len(plan)} 个缺口 SKU，合计建议 {total_suggest} 件")
        writer({"type": "explanation", "text": explanation})

        # 助手消息入历史（与 explain_result 同一套约定）
        if app_config.features.context_management:
            from app.agent.session.context_store import append_assistant_message
            staff = runtime.context.get("staff")
            messages = append_assistant_message(state, explanation,
                                                capability=state.get("capability"),
                                                staff_id=staff.staff_id if staff else None)
        else:
            messages = list(state.get("messages", []))
            messages.append({"role": "assistant", "content": explanation})
        return {"replenish_plan": plan_rows, "messages": messages}

    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise
