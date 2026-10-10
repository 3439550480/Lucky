"""
每日开档脚本（Daily Append）—— 向 dw 库增量追加「昨天」一天的数据

链路位置：数据面增量入口（generate_anta_data.py 的每日续写器）。
  初始化（一次性）：uv run python -m app.scripts.generate_anta_data
  每日开档：        uv run python -m app.scripts.daily_append
谁调用：演示环境运维/定时任务（crontab 每日 09:00）；读侧 DataAgent 只查库，无感知。

设计 why：
 1. 日末口径（q-0 拍板）：T 日销售在 T+1 开档时入库（真实门店日结后才出数），
    因此补齐到「昨天」；问「今天卖了多少」应诚实回「今日未日结」。
 2. 选品不重算：以库内近 N 天实际销量为选品权重来源，新一天热销结构自然延续，
    避免另写一套 SKU 展开逻辑导致与主数据漂移。
 3. 补货复用（q-1 拍板）：入库量 = compute_replenish 的缺口建议量，不另写补货逻辑。
 4. 确定性：每日种子 = BASE_SEED + date_id，同日重放结果一致、跨日互不干扰。
 5. 幂等：目标日不晚于库内最新快照日即跳过；每日一个事务，半写不落地（可重复运行）。
 6. 节后曲线（q-2 拍板）：10-08 起回落平日（周末略升），构成「假日效应归因」演示点。

使用示例：
    uv run python -m app.scripts.daily_append                       # 追加 [库内最新日+1, 昨天]
    uv run python -m app.scripts.daily_append --to 2026-10-09 --dry-run
"""
import argparse
import asyncio
import random
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.mysql_client_manager import dw_mysql_client_manager
from app.scripts.generate_anta_data import base_rate

# ============================================================================
# 配置常量（成组声明：口径单一真源；调业务水平只改此处，不动逻辑）
# ============================================================================
BASE_SEED = 20261001                 # 与 generate_anta_data 同源，保证可复现
SEED_STRIDE = 10000                  # 每日种子 = BASE_SEED + date_id（同日重放一致）
APPEND_START = date(2026, 10, 8)     # 初始化数据止于 10-07，可补首日
BATCH_SIZE = 500                     # INSERT 分批行数（与初始化脚本一致）

# 日订单量：平日 500~1000，周末 1000~2000（q-2 节后回落；单位=订单数/天）
DAILY_ORDERS_WEEKDAY = (500, 1000)
DAILY_ORDERS_WEEKEND = (1000, 2000)
WEEKEND_WEEKDAYS = (5, 6)            # date.weekday()：5=周六 6=周日
APPEND_HOLIDAYS: set[date] = set()   # 追加区间内法定假日（10-08 后为空；跨元旦/春节时在此登记）

# 篮子结构：与初始化脚本同口径（连带率≈2.2、会员率 40%、件数偏 1 件）
LINES_VALUES, LINES_WEIGHTS = (1, 2, 3, 4), (35, 30, 20, 15)
QTY_VALUES, QTY_WEIGHTS = (1, 2), (8, 2)
MEMBER_RATIO = 0.40
MEMBER_LEVELS = ("金卡", "银卡", "普通")

# 库存扰动：与初始化脚本同口径（2% 销售行触发退货入库；70% 概率 1 次报损 1~3 件）
RETURN_RATE = 0.02
SHRINK_LOSS_PROB = 0.7
SHRINK_LOSS_QTY = (1, 3)

# 补货算法输入：日均销量窗口（近 N 天，含国庆高基线 → 演示「假期拉高基线」口径）
SALES_WINDOW_DAYS = 7

# 经办人（键=flow_type 全名，与 DDL 注释及历史数据一致；None=系统自动；姓名对应 dim_staff）
OPERATOR_BY_TYPE = {
    "补货入库-早班": ("K001", "库管·李慧"),
    "补货入库-晚班": ("K001", "库管·李慧"),
    "退货入库": ("S001", "收银台·陈明"),
    "报损出库": ("K001", "库管·李慧"),
    "销售出库": (None, "POS系统"),
}


# ============================================================================
# 每日档位：确定性种子 + 订单量档位（平日 500~1000 / 周末 1000~2000）
# ============================================================================
def _day_context(day: date) -> tuple[random.Random, int]:
    """构造当日的确定性随机源与订单量档位。

    谁调用：_build_sales（每日循环内一次）。链路位置：每日开档的随机性根。
    设计 why：种子 = BASE_SEED + date_id × SEED_STRIDE——同日重放结果逐字一致
    （题库复核/演示截图可复现），跨日种子间隔 1 万避免相邻日期随机序列相关；
    档位按 weekday 判定而非查 dim_date，因为追加日当天尚未落入维表（自依赖死锁）。

    Args:
        day: 待生成的业务日（T 日；T+1 开档时才入库）。
    Returns:
        (rng, orders): 当日随机源与订单数（平日 500~1000，周末 1000~2000）。
    """
    # step 1 确定性种子：同日重放一致、跨日不相关
    date_id = day.year * 10000 + day.month * 100 + day.day
    rng = random.Random(BASE_SEED + date_id * SEED_STRIDE)
    # step 2 档位判定：周六/周日走高基线，其余走节后回落基线（q-2 拍板）
    lo, hi = (DAILY_ORDERS_WEEKEND if day.weekday() in WEEKEND_WEEKDAYS
              else DAILY_ORDERS_WEEKDAY)
    return rng, rng.randint(lo, hi)


# 使用示例（在 _build_sales 中）：
#   rng, orders = _day_context(day)
#   print(day, orders)


# ============================================================================
# 状态读取：一次性取齐追加所需的基线（最新日 / 库存 / 主数据 / 策略 / 销量）
# ============================================================================
async def _load_state(session: AsyncSession) -> dict:
    """读取库内最新日与追加基线状态。

    谁调用：_append_day（每日循环内一次）。链路位置：每日开档的数据入口。
    设计 why：库存衔接点取「最新快照日」而非 fact_sales 最新日——补货与出库都
    以快照为基准，二者同日可确保库存恒非负；选品权重直接复用库内近 N 天真实
    销量，新一天的热销结构自然延续，不必重算 SKU 展开与流行度。

    Args:
        session: dw 库会话（读写共用，写路径见 _append_day）。
    Returns:
        dict: last_id/last_date、available、dead_skus（断码清仓，恒零库存）、
              sku_meta、pricing、policy、daily_sales、sold、pick_ids、
              pick_weights（选品池已剔除无货 SKU）、max_sale_id、max_flow_id。
    """
    # step 1 最新快照日：库存衔接点；空库=未跑初始化，直接拒绝（fail-fast，不静默补零）
    row = (await session.execute(
        text("SELECT MAX(snapshot_date) AS last_id FROM dim_inventory_snapshot"))).mappings().first()
    last_id = row["last_id"] if row else None
    if not last_id:
        raise RuntimeError(
            "dw 库无库存快照，请先运行：uv run python -m app.scripts.generate_anta_data")
    last_id = int(last_id)
    last_date = date(last_id // 10000, last_id // 100 % 100, last_id % 100)

    # step 2 日初可用库存 + 历史峰值：一次查询同时给出两者
    #        available=最新日快照（库存衔接点）；peak_qty 恒 0 ⇒ 断码清仓款（永不补货，
    #        与初始化脚本 k["sold_out"] 的语义等价，但无需依赖主数据侧的临时标记）
    avail_rows = (await session.execute(
        text("SELECT sku_id, "
             "MAX(CASE WHEN snapshot_date = :snap THEN available_qty END) AS now_qty, "
             "MAX(available_qty) AS peak_qty FROM dim_inventory_snapshot "
             "GROUP BY sku_id ORDER BY sku_id"), {"snap": last_id})).mappings().all()
    available = {r["sku_id"]: int(r["now_qty"] or 0) for r in avail_rows}
    dead_skus = {r["sku_id"] for r in avail_rows if int(r["peak_qty"] or 0) <= 0}

    # step 3 SKU 元数据 + 定价档：品类映射源与销售行落账依据（一次 JOIN 取齐）
    meta_rows = (await session.execute(
        text("SELECT s.sku_id, p.product_name, p.category_l1, p.list_price, "
             "p.prod_year, p.season FROM dim_sku s "
             "JOIN dim_product p ON s.product_id = p.product_id"))).mappings().all()
    sku_meta = {r["sku_id"]: {"product_name": r["product_name"],
                              "category_l1": r["category_l1"]} for r in meta_rows}
    # 基准折扣率复用初始化脚本的 base_rate（同一真源）；此处取基准，满件加折由 _build_sales 现算
    pricing = {r["sku_id"]: {"list_price": float(r["list_price"]),
                             "base_rate": base_rate(r["prod_year"], r["season"])}
               for r in meta_rows}

    # step 4 门店补货策略（compute_replenish 的 policy 输入；warehouse 行本期不用）
    pol_rows = (await session.execute(
        text("SELECT category, coverage_days, safety_days FROM dim_replenish_policy "
             "WHERE scope = 'store'"))).mappings().all()
    policy = {r["category"]: {"coverage_days": int(r["coverage_days"]),
                              "safety_days": int(r["safety_days"])} for r in pol_rows}

    # step 5 近 N 天销量（含最新日）→ 日均：补货算法输入，同时作选品权重源
    start_id = int((last_date - timedelta(days=SALES_WINDOW_DAYS - 1)).strftime("%Y%m%d"))
    sales_rows = (await session.execute(
        text("SELECT sku_id, SUM(quantity) AS sold FROM fact_sales "
             "WHERE date_id BETWEEN :a AND :b GROUP BY sku_id"),
        {"a": start_id, "b": last_id})).mappings().all()
    sold = {r["sku_id"]: int(r["sold"]) for r in sales_rows}
    daily_sales = {k: v / SALES_WINDOW_DAYS for k, v in sold.items()}

    # step 6 选品池：有货才可卖（断码清仓 SKU 快照恒 0 → 天然出局，无需额外标记）；
    #        权重=近 N 天销量，零销量 SKU 给保底 0.05（与初始化脚本 max(pop, 0.05) 同思路）
    pick_ids = [s for s in sku_meta if available.get(s, 0) > 0]
    pick_weights = [max(float(sold.get(s, 0)), 0.05) for s in pick_ids]

    # step 7 事实表主键起点：sale_id/flow_id 全表自增，避免与初始化 1..N 撞主键
    max_sale_id = int((await session.execute(
        text("SELECT COALESCE(MAX(sale_id), 0) AS m FROM fact_sales"))).scalar() or 0)
    max_flow_id = int((await session.execute(
        text("SELECT COALESCE(MAX(flow_id), 0) AS m FROM fact_inventory_flow"))).scalar() or 0)

    return {"last_id": last_id, "last_date": last_date, "available": available,
            "dead_skus": dead_skus, "sku_meta": sku_meta, "policy": policy,
            "daily_sales": daily_sales, "sold": sold, "pick_ids": pick_ids,
            "pick_weights": pick_weights, "pricing": pricing,
            "max_sale_id": max_sale_id, "max_flow_id": max_flow_id}


# 使用示例（在 _append_day 中）：
#   state = await _load_state(session)
#   print(state["last_date"], len(state["pick_ids"]))


# ============================================================================
# 销售事实：当日订单 → fact_sales 行（含整单满件折扣预计算）
# ============================================================================
def _build_sales(day: date, orders: int, rng: random.Random, state: dict,
                 sale_id_start: int) -> tuple[list[dict], list[dict]]:
    """把当日订单数展开为 fact_sales 行。

    谁调用：_append_day（每日一次）。链路位置：每日开档的销售事实生成。
    设计 why：口径与初始化脚本逐字对齐（行数 35/30/20/15、件数 8:2、会员 40%、
    满 4 件折扣=缺件数×0.10 且封顶 1.0），保证追加日与国庆 7 天同分布、题库跨期
    可比；选品权重取库内近 N 天销量，热销结构自然延续，不另算流行度。

    Args:
        day: 业务日。
        orders: 当日订单数（来自 _day_context）。
        rng: 当日随机源。
        state: _load_state 结果（用 pick_ids/pick_weights/pricing）。
        sale_id_start: 本批首行 sale_id（= 库内 max(sale_id)+1，全表唯一）。
    Returns:
        (rows, order_meta): rows 列名逐一对齐 fact_sales 的 INSERT 列；
        order_meta 供 _append_day 汇总打印（满件订单数等）。
    """
    date_id = day.year * 10000 + day.month * 100 + day.day
    ids, weights = state["pick_ids"], state["pick_weights"]
    pricing = state["pricing"]
    rows, order_meta = [], []

    # step 1 逐单开单：订单号 = AT + 日期 + 日内序号（zfill 6，与初始化同构）
    for seq in range(1, orders + 1):
        order_id = "AT" + str(date_id) + str(seq).zfill(6)
        n_lines = rng.choices(LINES_VALUES, weights=LINES_WEIGHTS, k=1)[0]
        is_member = rng.random() < MEMBER_RATIO
        level = rng.choice(MEMBER_LEVELS) if is_member else None

        # step 2 行级选品与件数：先定行清单，再算整单件数（满件折扣是跨行依赖）
        lines = []
        for _ in range(n_lines):
            sku_id = rng.choices(ids, weights=weights, k=1)[0]
            qty = rng.choices(QTY_VALUES, weights=QTY_WEIGHTS, k=1)[0]
            lines.append((sku_id, qty))
        total_qty = sum(q for _, q in lines)

        # step 3 整单折扣：满 4 件不加折；不足按缺件数 ×0.10 加折，最终封顶 1.0
        add_rate = 0.0 if total_qty >= 4 else round((4 - total_qty) * 0.10, 2)

        # step 4 行级落账：实付 = 吊牌价 × 件数 × 最终折扣（与初始化同公式）
        for line_no, (sku_id, qty) in enumerate(lines, 1):
            pr = pricing[sku_id]
            # 折扣率定两位小数再落账：DDL 为 DECIMAL(4,2)，浮点尾巴（0.2+0.1=
            # 0.30000000000000004）会被 MySQL 判为 Data truncated（严格模式下直接报错）；
            # round 后 actual_amount 与存库 final_rate 自洽（差异 < 1e-13，被分位吸收）
            final_rate = round(min(pr["base_rate"] + add_rate, 1.0), 2)
            # sale_id 由「起点 + 已生成行数」推导（无状态自增，杜绝 off-by-one）
            rows.append({"sale_id": sale_id_start + len(rows), "order_id": order_id,
                         "line_no": line_no, "date_id": date_id, "sku_id": sku_id,
                         "quantity": qty, "list_price": pr["list_price"],
                         "base_rate": pr["base_rate"], "order_total_qty": total_qty,
                         "final_rate": final_rate,
                         "actual_amount": round(pr["list_price"] * qty * final_rate, 2),
                         "is_member": 1 if is_member else 0, "member_level": level})
        order_meta.append({"order_id": order_id, "total_qty": total_qty})

    return rows, order_meta


# 使用示例（在 _append_day 中）：
#   rows, ometa = _build_sales(day, orders, rng, state, state["max_sale_id"] + 1)
#   print(day, len(ometa), len(rows), sum(r["quantity"] for r in rows))


# ============================================================================
# 库存推演：补货 → 销售出库 → 退货/报损 → 日末快照（恒非负）
# ============================================================================
def _build_flow_and_snapshot(day: date, sales_rows: list[dict], rng: random.Random,
                             state: dict, flow_id_start: int
                             ) -> tuple[list[dict], list[dict]]:
    """推演当日库存变动，输出出入库流水与日末快照。

    谁调用：_append_day（每日一次）。链路位置：库存事实生成，承接 _build_sales。
    设计 why：动作顺序与初始化脚本一致——补货排在销售出库之前，使补货按日初库存
    判定、出库在补货之后扣减，库存恒非负（与初始化 self_check 的负值断言同源）；
    补货目标取当日该 SKU 出库量（下限 1 件）而非滚动均值，与初始化口径一致；
    历史零库存 SKU（断码清仓）不参与补货，避免把清仓款「复活」回可售池。

    Args:
        day: 业务日。
        sales_rows: _build_sales 的行（仅取 sku_id/quantity 聚合出库量）。
        rng: 当日随机源（退货/报损扰动）。
        state: _load_state 结果（available/policy/sku_meta/dead_skus）。
        flow_id_start: 本批首个 flow_id（= 库内 max(flow_id)+1）。
    Returns:
        (flows, snapshots): flows 列对齐 fact_inventory_flow；
        snapshots 列对齐 dim_inventory_snapshot（账面=可用）。
    """
    date_id = day.year * 10000 + day.month * 100 + day.day
    stock = dict(state["available"])
    policy, sku_meta = state["policy"], state["sku_meta"]
    dead = state["dead_skus"]

    # step 1 当日出库量按 SKU 聚合：补货目标与安全库存线的判定输入
    daily_out: dict[str, int] = {}
    for r in sales_rows:
        daily_out[r["sku_id"]] = daily_out.get(r["sku_id"], 0) + r["quantity"]

    flows: list[dict] = []

    def _emit(sku_id: str, qty: int, flow_type: str, doc_prefix: str) -> None:
        """记一条流水；flow_id/doc_no 由「起点 + 已生成数」推导（无状态自增）"""
        flow_id = flow_id_start + len(flows)
        doc_no = doc_prefix + str(date_id) + str(flow_id).zfill(5)
        op_id, op_name = OPERATOR_BY_TYPE[flow_type]
        flows.append({"flow_id": flow_id, "doc_no": doc_no, "flow_type": flow_type,
                      "sku_id": sku_id, "quantity": qty, "date_id": date_id,
                      "operator_id": op_id, "operator": op_name,
                      "idempotency_key": doc_no + ":" + sku_id + ":" + flow_type})

    # step 2 两班补货：早班按安全库存线触发，晚班按覆盖天数目标补齐（与初始化同判定）
    for flow_type, ratio_key in (("补货入库-早班", "safety_days"),
                                 ("补货入库-晚班", "coverage_days")):
        for sku_id in stock:
            if sku_id in dead:
                continue
            pol = policy[sku_meta[sku_id]["category_l1"]]
            avg = max(1.0, float(daily_out.get(sku_id, 0)))
            hit = stock[sku_id] < avg * pol[ratio_key]
            need = int(avg * pol["coverage_days"] - stock[sku_id])
            if hit and need > 0:
                stock[sku_id] += need
                _emit(sku_id, need, flow_type, "RK")

    # step 3 销售出库：按 SKU 日聚合扣减一次（与初始化一致，非逐单逐行）
    for sku_id, out in daily_out.items():
        if out:
            stock[sku_id] = max(0, stock[sku_id] - out)
            _emit(sku_id, -out, "销售出库", "XS")

    # step 4 退货入库：按初始化口径对「销售行」抽样，整行件数退回
    for r in sales_rows:
        if rng.random() < RETURN_RATE:
            stock[r["sku_id"]] += r["quantity"]
            _emit(r["sku_id"], r["quantity"], "退货入库", "TH")

    # step 5 报损出库：当日 70% 概率发生 1 次、1~3 件（同参数），不足以扣减则跳过
    candidates = [s for s in stock if s not in dead]
    if candidates and rng.random() < SHRINK_LOSS_PROB:
        pick = rng.choice(candidates)
        qty = min(rng.randint(*SHRINK_LOSS_QTY), stock[pick])
        if qty > 0:
            stock[pick] -= qty
            _emit(pick, -qty, "报损出库", "BS")

    # step 6 日末快照：账面=可用（本期无锁定库存），覆盖全部 SKU，与初始化一致
    snapshots = [{"snapshot_date": date_id, "sku_id": s, "book_qty": stock[s],
                  "available_qty": stock[s]} for s in stock]
    return flows, snapshots


# 使用示例（在 _append_day 中）：
#   flows, snaps = _build_flow_and_snapshot(day, rows, rng, state, state["max_flow_id"] + 1)
#   print(day, len(flows), len(snaps), min(s["book_qty"] for s in snaps))


# ============================================================================
# 写库：分批 INSERT 与「整日一事务」的追加入口
# ============================================================================
FACT_SALES_COLUMNS = ("sale_id", "order_id", "line_no", "date_id", "sku_id",
                      "quantity", "list_price", "base_rate", "order_total_qty",
                      "final_rate", "actual_amount", "is_member", "member_level")
FACT_FLOW_COLUMNS = ("flow_id", "doc_no", "flow_type", "sku_id", "quantity",
                     "date_id", "operator_id", "idempotency_key", "operator")
SNAPSHOT_COLUMNS = ("snapshot_date", "sku_id", "book_qty", "available_qty")
DIM_DATE_COLUMNS = ("date_id", "year", "quarter", "month", "day", "weekday",
                    "is_weekend", "is_holiday")


async def _insert_rows(session: AsyncSession, table: str, columns: tuple[str, ...],
                       rows: list[dict]) -> None:
    """分批参数化 INSERT：所有批次共用同一占位模板，交给驱动走 executemany。

    谁调用：_append_day。设计 why：不用 ORM 批量映射——追加是纯写入路径，原生
    参数化 SQL 少一层对象构造开销；分批是为了避开单条 SQL 的包体上限（与初始化
    脚本 BATCH_SIZE 同口径）。
    """
    if not rows:
        return
    stmt = text("INSERT INTO " + table + " (" + ", ".join(columns) + ") VALUES ("
                + ", ".join(":" + c for c in columns) + ")")
    for i in range(0, len(rows), BATCH_SIZE):
        await session.execute(stmt, rows[i:i + BATCH_SIZE])


def _dim_date_row(day: date) -> dict:
    """构造 dim_date 行。

    谁调用：_append_day。设计 why：初始化维表止于 2026-10-07，追加日若不补维表，
    所有按日期 JOIN dim_date 的分析（周末/节假日口径）会整段丢行；is_holiday 取
    APPEND_HOLIDAYS 常量而非硬编码，跨元旦/春节时只改常量表。
    """
    weekday = day.weekday()
    return {"date_id": day.year * 10000 + day.month * 100 + day.day,
            "year": day.year, "quarter": "Q" + str((day.month - 1) // 3 + 1),
            "month": day.month, "day": day.day,
            "weekday": "周" + "一二三四五六日"[weekday],
            "is_weekend": "是" if weekday in WEEKEND_WEEKDAYS else "否",
            "is_holiday": "是" if day in APPEND_HOLIDAYS else "否"}


async def _append_day(session: AsyncSession, day: date, dry_run: bool = False) -> dict:
    """生成单个业务日的全部数据并落库（整日一个事务）。

    谁调用：_run（逐日循环）。链路位置：每日追加的唯一写入口。
    设计 why：销售/流水/快照三表必须同生同死——半途提交会出现「有销售无出库」
    的账实不符，故整日一 commit、异常即 rollback；幂等判据取「目标日 <= 库内最新
    快照日」，重跑直接跳过而非覆盖重写，使脚本可反复执行（演示前误跑、中断后补跑
    都安全）。

    Args:
        session: dw 库会话（事务由本函数提交）。
        day: 目标业务日。
        dry_run: True 时只生成并汇总，不写库（供预演核对）。
    Returns:
        dict: 当日汇总（订单/行/件数/实收/流水/快照/最低库存），供 CLI 打印。
    """
    state = await _load_state(session)

    # step 1 幂等门：目标日不晚于库内最新日 ⇒ 已追加过，跳过
    if day <= state["last_date"]:
        return {"date": day.isoformat(), "status": "skipped",
                "detail": "库内最新日已到 " + state["last_date"].isoformat()}

    # step 2 生成当日数据：种子 → 订单 → 销售行 → 库存推演（纯计算，不触库）
    rng, orders = _day_context(day)
    rows, order_meta = _build_sales(day, orders, rng, state, state["max_sale_id"] + 1)
    flows, snaps = _build_flow_and_snapshot(day, rows, rng, state,
                                            state["max_flow_id"] + 1)
    summary = {"date": day.isoformat(), "status": "dry-run" if dry_run else "appended",
               "orders": len(order_meta), "lines": len(rows),
               "qty": sum(r["quantity"] for r in rows),
               "gmv": round(sum(r["actual_amount"] for r in rows), 2),
               "flows": len(flows), "snapshots": len(snaps),
               "min_stock": min(s["book_qty"] for s in snaps)}

    # step 3 写库：先补维表再写事实表，最后一次性提交（任一异常 → 整日回滚）
    if not dry_run:
        try:
            await _insert_rows(session, "dim_date", DIM_DATE_COLUMNS,
                               [_dim_date_row(day)])
            await _insert_rows(session, "fact_sales", FACT_SALES_COLUMNS, rows)
            await _insert_rows(session, "fact_inventory_flow", FACT_FLOW_COLUMNS,
                               flows)
            await _insert_rows(session, "dim_inventory_snapshot", SNAPSHOT_COLUMNS,
                               snaps)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return summary


# 使用示例（在 _run 中）：
#   print(await _append_day(session, date(2026, 10, 8)))
#   print(await _append_day(session, date(2026, 10, 8), dry_run=True))


# ============================================================================
# CLI 入口：库内最新日 + 1 → 目标日（默认昨天），逐日追加
# ============================================================================
def _target_days(last_date: date, to_day: date) -> list[date]:
    """算出待追加的日期序列（含首尾）。

    谁调用：_run。设计 why：起点取「库内最新日+1」再对 APPEND_START 取大——既
    保证续写不重不漏，又避免对空库/半初始化库从错误的日期开档；目标日早于起点
    说明无需追加，返回空列表（CLI 打印提示而非报错）。
    """
    start = max(last_date + timedelta(days=1), APPEND_START)
    if to_day < start:
        return []
    return [start + timedelta(days=i) for i in range((to_day - start).days + 1)]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 CLI 参数：--to 目标日（含），--dry-run 只生成不写库"""
    parser = argparse.ArgumentParser(
        prog="daily_append", description="每日开档：向 dw 库追加 T-1 日数据")
    parser.add_argument("--to", type=date.fromisoformat, default=None,
                        help="追加到该日期（含，YYYY-MM-DD），默认昨天")
    parser.add_argument("--dry-run", action="store_true",
                        help="只生成并打印当日汇总，不写库")
    return parser.parse_args(argv)


async def _run(to_day: date, dry_run: bool) -> list[dict]:
    """连接 dw 库、逐日追加并返回各日汇总。

    谁调用：main（asyncio.run 包装）。设计 why：客户端 init/close 在本函数内配对，
    即使中途抛错也走 finally 释放连接池（避免脚本以非零码退出时连接悬挂）。
    """
    dw_mysql_client_manager.init()
    try:
        async with dw_mysql_client_manager.session_factory() as session:
            # 先用一次基线读取决定日期区间；各日内部仍会重新读基线（数据在变）
            last_date = (await _load_state(session))["last_date"]
            return [await _append_day(session, day, dry_run)
                    for day in _target_days(last_date, to_day)]
    finally:
        await dw_mysql_client_manager.close()


def main(argv: list[str] | None = None) -> None:
    """CLI 主函数：默认补到「昨天」（日末口径：T 日数据 T+1 开档才入库）"""
    args = _parse_args(argv)
    to_day = args.to if args.to else date.today() - timedelta(days=1)
    results = asyncio.run(_run(to_day, args.dry_run))
    if not results:
        print("[daily-append] 无需追加：目标日", to_day.isoformat(),
              "未晚于库内最新日")
        return
    for item in results:
        print("[daily-append]", item)
    print("[daily-append] 完成：", len(results), "天 |",
          "dry-run" if args.dry_run else "已提交")


if __name__ == "__main__":
    main()
