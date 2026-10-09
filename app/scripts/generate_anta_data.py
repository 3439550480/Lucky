"""
安踏特卖店演示数据集生成器（Lucky 2.0 · 方案 v4 S2）

链路位置：数据面唯一入口。运行后把 8 表 DDL + 仿真数据整体写入 docker/mysql/dw.sql
（docker compose 首次初始化自动执行）；数据损坏时一条命令重建：
    uv run python -m app.scripts.generate_anta_data

设计决策（why）：
  1. SPU 主数据从 docs/specs/Lucky2.0-商品主数据清单-120SPU.md 解析而非硬编码，
     清单是需求定稿物（单一真源）；解析时逐货号重算校验位，失败即拒绝生成。
  2. 满件折扣按方案甲预计算（fact_sales 存吊牌价/基准折扣/整单件数/最终折扣率/
     实付）；保留整单件数与基准折扣列，"窗口函数现算"演示口径仍可行（方案 3.3）。
  3. 固定随机种子（SEED=20261001）——题库答案/评估基线/演示现场永远对得上。
  4. 库存流水与销售强一致：早/晚补货在销售前执行（库存恒非负）、销售出库按
     SKU×日聚合、退货/报损按比例扰动。
  5. 断码清仓 SPU 随机 1-3 个冷门尺码库存 0 且不补货、不产生销售。

合规声明：商品名与吊牌价来自安踏官方商城公开信息（仅学习用途）；货号/色码/
库存/销售均为按规则生成的仿真数据，不代表安踏真实经营情况。
"""
import hashlib
import random
import re
from datetime import date, timedelta
from pathlib import Path

SEED = 20261001
ROOT = Path(__file__).resolve().parents[2]
CATALOG_MD = ROOT / "docs" / "specs" / "Lucky2.0-商品主数据清单-120SPU.md"
OUTPUT = ROOT / "docker" / "mysql" / "dw.sql"
BATCH_SIZE = 500

SALE_DAYS = [date(2026, 10, 1) + timedelta(days=i) for i in range(7)]
DATE_SPAN_START, DATE_SPAN_END = date(2025, 1, 1), date(2026, 10, 7)
ORDERS_PER_DAY = (4500, 5000)
COLORS = ["黑/安踏白", "暴风蓝/黑", "晨曦红/安踏白", "岩灰/芥末绿", "藏青/荧光黄", "米白/浅卡其"]
SIZE_LARGE_SHOE = ["39", "40", "41", "42", "43", "44"]
SIZE_SMALL_SHOE = ["36", "37", "38", "39", "40"]
SIZE_KID_SHOE = ["28", "30", "32", "34", "36", "38"]
SIZE_ADULT_WEAR = ["S", "M", "L", "XL", "XXL"]
SIZE_KID_WEAR = ["120", "130", "140", "150", "160"]
SIZE_ONE = ["均码"]
MEMBER_LEVELS = ["金卡", "银卡", "普通"]

rng = random.Random(SEED)

# ============================================================================
# 主数据解析：120 SPU 清单 → 结构化记录（货号校验位重算，失败即拒绝生成）
# ============================================================================
def check_digit(code7: str) -> int:
    """货号校验位：位 1~7 加权和（权重 1,3,1,3,1,3,1）mod 10（清单 1.3 节）"""
    weights = (1, 3, 1, 3, 1, 3, 1)
    return sum(int(c) * w for c, w in zip(code7, weights)) % 10


def parse_catalog() -> list[dict]:
    """解析 120SPU 清单：二级品类标题定位 → 表行提取 → 校验位自校验"""
    text = CATALOG_MD.read_text(encoding="utf-8")
    spus, l1, l2 = [], "", ""
    row_re = re.compile(r"^\| (\d{8}) \| (.+?) \| (.+?) \| (.+?) \| (.+?) \| (\d{2}) \| ¥(\d+) \| (.+?) \|")
    for line in text.splitlines():
        m1 = re.match(r"^## [三四五]、(\S+?)（\d+ SPU）", line)
        if m1:
            l1 = m1.group(1)
            continue
        m2 = re.match(r"^### \d+\.\d+ (.+?)（\d+ SPU）", line)
        if m2:
            l2 = m2.group(1).strip()
            continue
        m3 = row_re.match(line)
        if m3:
            code, name, gender, series, season, year, price, attr = m3.groups()
            if check_digit(code[:7]) != int(code[7]):
                raise ValueError("货号校验位不符: " + code)
            spus.append({"product_id": code, "product_name": name, "category_l1": l1,
                         "category_l2": l2, "gender": gender, "series": series,
                         "season": season, "prod_year": year, "sale_attr": attr,
                         "list_price": int(price)})
    if len(spus) != 120:
        raise ValueError("SPU 解析数 %d != 120" % len(spus))
    return spus


def base_rate(year_code: str, season_code: str) -> float:
    """基准折扣率：货号年份位+季节位判定（清单 1.2 节；取代上市日期字段）"""
    if year_code == "24":
        return 0.20
    if year_code == "25":
        return 0.35
    return 0.70 if season_code == "夏" else 0.50

# ============================================================================
# SKU 展开：颜色 2-4 色 × 尺码档；断码清仓 SPU 随机 1-3 个冷门尺码置 0
# ============================================================================
def expand_skus(spus: list[dict]) -> list[dict]:
    skus = []
    for spu in spus:
        colors = rng.sample(COLORS, rng.randint(2, 4))
        if spu["category_l1"] == "配件类":
            sizes = SIZE_ONE
        elif spu["category_l1"] == "服装类":
            sizes = SIZE_KID_WEAR if "儿童" in spu["gender"] else SIZE_ADULT_WEAR
        elif "儿童" in spu["gender"]:
            sizes = SIZE_KID_SHOE
        elif spu["product_id"][0] == "2":
            sizes = SIZE_SMALL_SHOE
        else:
            sizes = SIZE_LARGE_SHOE
        combos = [(c, s) for c in colors for s in sizes]
        for i, (color, size) in enumerate(combos, 1):
            skus.append({"sku_id": "%s-%02d-%s" % (spu["product_id"], i, size),
                         "product_id": spu["product_id"], "color": color,
                         "barcode": "69%d" % rng.randint(10**10, 10**11 - 1),
                         "sold_out": False})
        if spu["sale_attr"] == "断码清仓":
            own = [k for k in skus if k["product_id"] == spu["product_id"]]
            for k in rng.sample(own, min(len(own), rng.randint(1, 3))):
                k["sold_out"] = True
    return skus

def build_sales(skus: list[dict], spus_by_id: dict) -> tuple[list[dict], list[dict]]:
    """销售：7 天 x 约 1 万行，订单 1-4 行（连带率约 2.2），行级数量 + 整单折扣预计算"""
    sellable = [k for k in skus if not k["sold_out"]]
    pop = {}
    for k in sellable:
        br = base_rate(spus_by_id[k["product_id"]]["prod_year"], spus_by_id[k["product_id"]]["season"])
        pop[k["sku_id"]] = (1.0 - (br - 0.2) / 0.8) * rng.uniform(0.4, 1.6)
    sku_pool = [k["sku_id"] for k in sellable]
    weights = [max(pop[s], 0.05) for s in sku_pool]
    orders, seq = [], 0
# 逐日生成订单（下方缩进为 build_sales 函数体）
    for day in SALE_DAYS:
        date_id = day.year * 10000 + day.month * 100 + day.day
        base_orders = {1: 5800, 2: 5600, 3: 5000, 4: 4900, 5: 4800, 6: 4600, 7: 4400}
        day_orders = rng.randint(base_orders[day.day], base_orders[day.day] + 300)
        for _ in range(day_orders):
            seq = seq + 1
            order_id = "AT" + str(date_id) + str(seq).zfill(6)
            n_lines = rng.choices([1, 2, 3, 4], weights=[35, 30, 20, 15], k=1)[0]
            is_member = rng.random() < 0.40
            level = rng.choice(MEMBER_LEVELS) if is_member else None
            lines = []
            for _ in range(n_lines):
                sku = sellable[rng.choices(range(len(sku_pool)), weights=weights, k=1)[0]]
                spu = spus_by_id[sku["product_id"]]
                lines.append({"sku": sku, "spu": spu, "qty": rng.choices([1, 2], weights=[8, 2], k=1)[0]})
            orders.append({"order_id": order_id, "lines": lines, "is_member": is_member,
                           "level": level, "date_id": date_id})
    return orders, sku_pool

# ============================================================================
# 库存：日初补货（覆盖天数目标）→ 销售出库 → 退货/报损扰动 → 日末快照；恒非负
# ============================================================================
def build_replenish_policy() -> list[dict]:
    """补货参数（D14/D15 用户真实业务值）：门店无 MOQ；仓库 MOQ 预留第 4 能力"""
    rows = []
    for cat, cover, wh_moq in [("鞋类", 7, 20), ("服装类", 3, 30), ("配件类", 14, 20)]:
        rows.append({"scope": "store", "category": cat, "coverage_days": cover,
                     "safety_days": 3, "moq": None, "note": "门店向仓库要货，不设起订量"})
        rows.append({"scope": "warehouse", "category": cat, "coverage_days": cover,
                     "safety_days": 3, "moq": wh_moq, "note": "预留 warehouse_replenish 能力"})
    return rows


def build_pricing_rule() -> list[dict]:
    """折扣规则配置化（方案四节 dim_pricing_rule）：业务调整改配置不改代码"""
    return [
        {"rule_type": "base_discount", "year_code": "24", "season_code": None, "base_rate": 0.20, "threshold_qty": None, "add_per_unit": None, "cap_rate": None},
        {"rule_type": "base_discount", "year_code": "25", "season_code": None, "base_rate": 0.35, "threshold_qty": None, "add_per_unit": None, "cap_rate": None},
        {"rule_type": "base_discount", "year_code": "26", "season_code": "夏", "base_rate": 0.70, "threshold_qty": None, "add_per_unit": None, "cap_rate": None},
        {"rule_type": "base_discount", "year_code": "26", "season_code": None, "base_rate": 0.50, "threshold_qty": None, "add_per_unit": None, "cap_rate": None},
        {"rule_type": "full_price_threshold", "year_code": None, "season_code": None,
         "base_rate": None, "threshold_qty": 4, "add_per_unit": 0.10, "cap_rate": 1.0},
    ]

def build_inventory(skus: list[dict], sales: list[dict], spus_by_id: dict):
    """库存快照 + 出入库流水：补货在销售前（恒非负）；销售出库按 SKU x 日聚合"""
    policy = {r["category"]: r for r in build_replenish_policy() if r["scope"] == "store"}
    stock = {}
    for k in skus:
        spu = spus_by_id[k["product_id"]]
        if k["sold_out"]:
            stock[k["sku_id"]] = 0
        elif spu["sale_attr"] == "断码清仓":
            stock[k["sku_id"]] = rng.randint(5, 40)
        else:
            stock[k["sku_id"]] = rng.randint(150, 400)
    daily_out = {}
    for s in sales:
        key = (s["sku_id"], s["date_id"])
        daily_out[key] = daily_out.get(key, 0) + s["quantity"]
    snapshots, flows, fid = [], [], 0
    for day in SALE_DAYS:
        date_id = day.year * 10000 + day.month * 100 + day.day
        avg_out = {}
        for k in skus:
            avg_out[k["sku_id"]] = max(1.0, daily_out.get((k["sku_id"], date_id), 0))
        for slot, label, op in [("m", "补货入库-早班", "早班·王店长"), ("e", "补货入库-晚班", "晚班·李店长")]:
            for k in skus:
                if k["sold_out"]:
                    continue
                pol = policy[spus_by_id[k["product_id"]]["category_l1"]]
                need = int(avg_out[k["sku_id"]] * pol["coverage_days"] - stock[k["sku_id"]])
                hit = (slot == "m" and stock[k["sku_id"]] < avg_out[k["sku_id"]] * pol["safety_days"]) \
                    or (slot == "e" and stock[k["sku_id"]] < avg_out[k["sku_id"]] * pol["coverage_days"])
                if hit and need > 0:
                    fid = fid + 1
                    stock[k["sku_id"]] = stock[k["sku_id"]] + need
                    flows.append({"flow_id": fid, "doc_no": "RK" + str(date_id) + str(fid).zfill(5),
                                  "flow_type": label, "sku_id": k["sku_id"], "quantity": need,
                                  "date_id": date_id, "operator": op})
        for k in skus:
            out = daily_out.get((k["sku_id"], date_id), 0)
            if out:
                fid = fid + 1
                stock[k["sku_id"]] = max(0, stock[k["sku_id"]] - out)
                flows.append({"flow_id": fid, "doc_no": "XS" + str(date_id) + str(fid).zfill(5),
                              "flow_type": "销售出库", "sku_id": k["sku_id"], "quantity": -out,
                              "date_id": date_id, "operator": "POS系统"})
        for s in sales:
            if s["date_id"] == date_id and rng.random() < 0.02:
                fid = fid + 1
                stock[s["sku_id"]] = stock[s["sku_id"]] + s["quantity"]
                flows.append({"flow_id": fid, "doc_no": "TH" + str(date_id) + str(fid).zfill(5),
                              "flow_type": "退货入库", "sku_id": s["sku_id"],
                              "quantity": s["quantity"], "date_id": date_id, "operator": "收银台"})
        if rng.random() < 0.7:
            k = rng.choice([k for k in skus if not k["sold_out"]])
            q = rng.randint(1, 3)
            fid = fid + 1
            stock[k["sku_id"]] = max(0, stock[k["sku_id"]] - q)
            flows.append({"flow_id": fid, "doc_no": "BS" + str(date_id) + str(fid).zfill(5),
                          "flow_type": "报损出库", "sku_id": k["sku_id"], "quantity": -q,
                          "date_id": date_id, "operator": "晚班·李店长"})
        for k in skus:
            snapshots.append({"snapshot_date": date_id, "sku_id": k["sku_id"],
                              "book_qty": stock[k["sku_id"]], "available_qty": stock[k["sku_id"]]})
    op_ids = {"补货入库-早班": "K001", "补货入库-晚班": "K001",
              "退货入库": "S001", "报损出库": "K001", "销售出库": None}
    for f in flows:
        f["operator_id"] = op_ids.get(f["flow_type"])
        f["idempotency_key"] = f["doc_no"] + ":" + f["sku_id"] + ":" + f["flow_type"]
    return snapshots, flows


def build_dim_date() -> list[dict]:
    """日期维：2025-01-01 ~ 2026-10-07（支撑账龄/折扣判定）；is_holiday 演示口径"""
    holidays = set()
    for seg in [("2025-01-01", "2025-01-01"), ("2025-01-28", "2025-02-03"),
                ("2025-04-04", "2025-04-06"), ("2025-05-01", "2025-05-05"),
                ("2025-05-31", "2025-06-02"), ("2025-10-01", "2025-10-08"),
                ("2026-01-01", "2026-01-03"), ("2026-10-01", "2026-10-07")]:
        d0 = date.fromisoformat(seg[0])
        d1 = date.fromisoformat(seg[1])
        while d0 <= d1:
            holidays.add(d0)
            d0 = d0 + timedelta(days=1)
    rows, d = [], DATE_SPAN_START
    while d <= DATE_SPAN_END:
        weekday = d.weekday()
        rows.append({"date_id": d.year * 10000 + d.month * 100 + d.day, "year": d.year,
                     "quarter": "Q" + str((d.month - 1) // 3 + 1), "month": d.month,
                     "day": d.day, "weekday": "周" + "一二三四五六日"[weekday],
                     "is_weekend": "是" if weekday >= 5 else "否",
                     "is_holiday": "是" if d in holidays else "否"})
        d = d + timedelta(days=1)
    return rows

# ============================================================================
# SQL 输出：DDL + 分批 INSERT（无百分号字符，规避传输层限制）
# ============================================================================


def build_staff() -> list[dict]:
    """dim_staff 初始名单（权限文档 5.9 定稿 14 条）；凭据=加盐哈希，演示统一 PIN 123456"""
    spec = [
        ("M001", "王志远", "MANAGER", "启用", "2024-03-01", None, "2024-03-01", None),
        ("K001", "李慧", "STAFF,KEEPER", "启用", "2024-05-15", None, "2024-05-15", None),
        ("S001", "陈明", "STAFF", "启用", "2025-03-10", None, "2025-03-10", None),
        ("S002", "赵雪", "STAFF", "启用", "2025-03-10", None, "2025-03-10", None),
        ("S003", "刘洋", "STAFF", "启用", "2025-08-01", None, "2025-08-01", None),
        ("S004", "孙倩", "STAFF", "启用", "2025-08-01", None, "2025-08-01", None),
        ("S005", "郑凯", "STAFF", "启用", "2026-01-05", None, "2026-01-05", None),
        ("S006", "马琳", "STAFF", "启用", "2026-01-05", None, "2026-01-05", None),
        ("T001", "周小雨", "TEMP", "启用", "2026-09-28", "2026-10-12", "2026-09-28", None),
        ("T002", "吴强", "TEMP", "启用", "2026-09-28", "2026-10-12", "2026-09-28", None),
        ("T003", "何静", "TEMP", "启用", "2026-09-28", "2026-10-12", "2026-09-28", None),
        ("T004", "林浩", "TEMP", "启用", "2026-09-28", "2026-10-12", "2026-09-28", None),
        ("S007", "高翔", "STAFF", "停用", "2025-02-10", None, "2025-02-10", "2026-09-15"),
        ("T005", "徐婷", "TEMP", "启用", "2026-08-01", "2026-08-31", "2026-08-01", None),
    ]
    rows = []
    for sid, name, roles, status, vfrom, vto, hire, leave in spec:
        salt = format(rng.getrandbits(64), "016x")
        digest = hashlib.sha256((salt + "123456").encode("utf-8")).hexdigest()
        rows.append({"staff_id": sid, "name": name, "role_codes": roles,
                     "credential_hash": "sha256$" + salt + "$" + digest,
                     "status": status, "valid_from": vfrom, "valid_to": vto,
                     "hire_date": hire, "leave_date": leave, "created_by": "M001",
                     "created_at": "2026-09-28 09:00:00",
                     "updated_at": "2026-09-28 09:00:00"})
    return rows

def _esc(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return str(v)


def _inserts(table: str, columns: str, rows: list[dict]) -> str:
    chunks = []
    for i in range(0, len(rows), BATCH_SIZE):
        vals = []
        for r in rows[i:i + BATCH_SIZE]:
            vals.append("(" + ", ".join(_esc(r[c]) for c in columns) + ")")
        head = "INSERT INTO " + table + " (" + ", ".join(columns) + ") VALUES"
        chunks.append(head + "\n" + ",\n".join(vals) + ";")
    return "\n\n".join(chunks)

DDL = """-- ============================================================
-- Lucky 2.0 演示数据集：安踏特卖店（单店，2026 国庆 7 天）
-- 由 app/scripts/generate_anta_data.py 生成（SEED=20261001），请勿手改
-- 重建：uv run python -m app.scripts.generate_anta_data
-- 声明：商品名/吊牌价来自安踏官方商城公开信息（仅学习用途）；
--       货号/色码/库存/销售均为仿真数据，非真实经营数据。
-- ============================================================
SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS dw DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
GRANT ALL PRIVILEGES ON dw.* TO 'lzs'@'%';
USE dw;

DROP TABLE IF EXISTS fact_inventory_flow;
DROP TABLE IF EXISTS dim_inventory_snapshot;
DROP TABLE IF EXISTS fact_sales;
DROP TABLE IF EXISTS dim_replenish_policy;
DROP TABLE IF EXISTS dim_pricing_rule;
DROP TABLE IF EXISTS dim_sku;
DROP TABLE IF EXISTS dim_product;
DROP TABLE IF EXISTS dim_date;

CREATE TABLE dim_product
(
    product_id   VARCHAR(10) PRIMARY KEY COMMENT '货号：8 位（位1尺码容别/位2男女/位3-4年份/位5季节/位6-7序号/位8校验）',
    product_name VARCHAR(100),
    category_l1  VARCHAR(10) COMMENT '一级品类：鞋类/服装类/配件类',
    category_l2  VARCHAR(30) COMMENT '二级品类（约 16 类）',
    gender       VARCHAR(10) COMMENT '男/女/儿童·大童/儿童·小童/男女同款',
    series       VARCHAR(20) COMMENT '跑步/篮球/综训/生活/潮流/冠军/足球',
    season       VARCHAR(4) COMMENT '生产季节（货号位5）：春/夏/秋/冬',
    prod_year    VARCHAR(4) COMMENT '生产年份（货号位3-4）：24/25/26，决定基准折扣',
    sale_attr    VARCHAR(10) COMMENT '特卖属性：断码清仓/过季商品/特卖专供款/正价转特卖',
    list_price   DECIMAL(8,2) COMMENT '吊牌价（元）'
);

CREATE TABLE dim_staff
(
    staff_id        VARCHAR(20) PRIMARY KEY COMMENT '工号（登录名）：角色前缀 M/K/S/T + 3 位序号',
    name            VARCHAR(30) NOT NULL,
    role_codes      VARCHAR(50) NOT NULL COMMENT '角色码多值逗号分隔（MANAGER/KEEPER/STAFF/TEMP），权限取并集',
    credential_hash VARCHAR(128) NOT NULL COMMENT '凭据加盐哈希（sha256$salt$hash），严禁明文',
    status          VARCHAR(10) NOT NULL COMMENT '启用/停用',
    valid_from      DATE NULL COMMENT '账号生效日（临时工必填）',
    valid_to        DATE NULL COMMENT '失效日；NULL=长期有效',
    hire_date       DATE NULL,
    leave_date      DATE NULL,
    created_by      VARCHAR(20) NULL COMMENT '开通人工号（留痕）',
    created_at      DATETIME NULL,
    updated_at      DATETIME NULL
);

CREATE TABLE dim_sku
(
    sku_id     VARCHAR(20) PRIMARY KEY COMMENT 'SKU编码：货号-色号-尺码',
    product_id VARCHAR(10),
    color      VARCHAR(30),
    barcode    VARCHAR(13) COMMENT '仿真 EAN 条码'
);

CREATE TABLE dim_date
(
    date_id    INT PRIMARY KEY COMMENT 'yyyyMMdd',
    year       INT,
    quarter    VARCHAR(2),
    month      INT,
    day        INT,
    weekday    VARCHAR(4),
    is_weekend VARCHAR(4) COMMENT '是/否',
    is_holiday VARCHAR(4) COMMENT '是/否（演示口径：法定节假日含调休）'
);

CREATE TABLE dim_pricing_rule
(
    rule_id       INT AUTO_INCREMENT PRIMARY KEY,
    rule_type     VARCHAR(20) COMMENT 'base_discount=账龄基准折扣 / full_price_threshold=满件门槛',
    year_code     VARCHAR(4) NULL COMMENT '生产年份位（账龄档）',
    season_code   VARCHAR(4) NULL COMMENT '季节（26 夏=新款 7 折）',
    base_rate     DECIMAL(4,2) NULL COMMENT '基准折扣率（售价/吊牌价）',
    threshold_qty INT NULL COMMENT '满件门槛（4 件）',
    add_per_unit  DECIMAL(3,2) NULL COMMENT '每缺 1 件加折幅度（+0.10）',
    cap_rate      DECIMAL(3,2) NULL COMMENT '折扣率上限（1.0 = 原价封顶）'
);

CREATE TABLE fact_sales
(
    sale_id         BIGINT PRIMARY KEY,
    order_id        VARCHAR(20) COMMENT '订单号',
    line_no         INT COMMENT '订单内行号',
    date_id         INT,
    sku_id          VARCHAR(20),
    quantity        INT COMMENT '件数',
    list_price      DECIMAL(8,2) COMMENT '吊牌价（行单价）',
    base_rate       DECIMAL(4,2) COMMENT '基准折扣率（由货号账龄档判定）',
    order_total_qty INT COMMENT '整单总件数（跨行依赖，满件折扣判定依据）',
    final_rate      DECIMAL(4,2) COMMENT '最终折扣率 = 基准 + 缺口加折，上限 1.0',
    actual_amount   DECIMAL(10,2) COMMENT '实付金额 = 吊牌价 x 数量 x 最终折扣率',
    is_member       TINYINT COMMENT '1=会员 0=非会员',
    member_level    VARCHAR(10) NULL COMMENT '会员等级（金卡/银卡/普通）；NULL=非会员'
);

CREATE TABLE dim_inventory_snapshot
(
    snap_id       INT AUTO_INCREMENT PRIMARY KEY,
    snapshot_date INT COMMENT 'yyyyMMdd（日末快照）',
    sku_id        VARCHAR(20),
    book_qty      INT COMMENT '账面库存',
    available_qty INT COMMENT '可用库存'
);

CREATE TABLE fact_inventory_flow
(
    flow_id   BIGINT PRIMARY KEY,
    doc_no    VARCHAR(20) COMMENT '单据号（RK补货/XS销售出库/TH退货/BS报损）',
    flow_type VARCHAR(10) COMMENT '补货入库-早班/补货入库-晚班/销售出库/退货入库/报损出库',
    sku_id    VARCHAR(20),
    quantity  INT COMMENT '正=入库 负=出库',
    date_id   INT,
    operator_id VARCHAR(10) NULL COMMENT '经办人工号（关联 dim_staff）；NULL=系统自动（POS出库）',
    idempotency_key VARCHAR(80) COMMENT '幂等键：单据号:SKU:操作类型（D21 防重复记账）',
    operator  VARCHAR(20) COMMENT '经办人姓名（展示用）'
);

CREATE TABLE dim_replenish_policy
(
    policy_id     INT AUTO_INCREMENT PRIMARY KEY,
    scope         VARCHAR(10) COMMENT 'store=门店（本期）/ warehouse=仓库（预留）',
    category      VARCHAR(10) COMMENT '鞋类/服装类/配件类',
    coverage_days INT COMMENT '目标覆盖天数：鞋7/服3/配14（D14）',
    safety_days   INT COMMENT '安全库存天数：3（D14）',
    moq           INT NULL COMMENT '最小起订量；门店行留空=不设（D14），仓库行鞋20/服30/配20（D15）',
    note          VARCHAR(60)
);
"""


def write_dw_sql(spus: list[dict], skus: list[dict], dates: list[dict],
                 sales: list[dict], snapshots: list[dict], flows: list[dict],
                 staff: list[dict]) -> None:
    """组装 8 表 DDL + 分批 INSERT，整体写入 docker/mysql/dw.sql（覆盖写，幂等）"""
    parts = [DDL]
    parts.append(_inserts("dim_product",
                          ["product_id", "product_name", "category_l1", "category_l2",
                           "gender", "series", "season", "prod_year", "sale_attr", "list_price"], spus))
    parts.append(_inserts("dim_staff",
                          ["staff_id", "name", "role_codes", "credential_hash", "status",
                           "valid_from", "valid_to", "hire_date", "leave_date",
                           "created_by", "created_at", "updated_at"], staff))
    parts.append(_inserts("dim_sku", ["sku_id", "product_id", "color", "barcode"], skus))
    parts.append(_inserts("dim_date",
                          ["date_id", "year", "quarter", "month", "day", "weekday",
                           "is_weekend", "is_holiday"], dates))
    parts.append(_inserts("dim_pricing_rule",
                          ["rule_type", "year_code", "season_code", "base_rate",
                           "threshold_qty", "add_per_unit", "cap_rate"], build_pricing_rule()))
    parts.append(_inserts("fact_sales",
                          ["sale_id", "order_id", "line_no", "date_id", "sku_id", "quantity",
                           "list_price", "base_rate", "order_total_qty", "final_rate",
                           "actual_amount", "is_member", "member_level"], sales))
    parts.append(_inserts("dim_inventory_snapshot",
                          ["snapshot_date", "sku_id", "book_qty", "available_qty"], snapshots))
    parts.append(_inserts("fact_inventory_flow",
                          ["flow_id", "doc_no", "flow_type", "sku_id", "quantity",
                           "date_id", "operator"], flows))
    parts.append(_inserts("dim_replenish_policy",
                          ["scope", "category", "coverage_days", "safety_days", "moq", "note"],
                          build_replenish_policy()))
    OUTPUT.write_text("\n\n".join(parts) + "\n", encoding="utf-8", newline="\n")
    print("已写入", OUTPUT)

# ============================================================================
# 自检：规模/满件折扣/库存一致性（规则 5 的量化基线来源；题库核对依据）
# ============================================================================
def self_check(spus, skus, sales, orders, snapshots, flows) -> None:
    bad_checksum = sum(1 for s in spus
                       if check_digit(s["product_id"][:7]) != int(s["product_id"][7]))
    total_qty = sum(s["quantity"] for s in sales)
    gmv = sum(s["actual_amount"] for s in sales)
    tagged = sum(s["list_price"] * s["quantity"] for s in sales)
    full = len([o for o in orders if o["total_qty"] >= 4])
    capped = len([s for s in sales if s["final_rate"] >= 1.0])
    neg = len([s for s in snapshots if s["book_qty"] < 0])
    sold_out = len([k for k in skus if k["sold_out"]])
    print("[自检] SPU:", len(spus), "校验位错误:", bad_checksum,
          "SKU:", len(skus), "断码SKU:", sold_out)
    print("[自检] 销售:", len(sales), "行 / 订单:", len(orders),
          "单 / 总件数:", total_qty, "连带率:", round(total_qty / len(orders), 2))
    print("[自检] 吊牌额:", round(tagged), "实收:", round(gmv),
          "整体折扣率:", round(gmv / tagged, 3))
    print("[自检] 满件订单(>=4件):", full, "触及10折封顶行:", capped)
    print("[自检] 员工:", len(build_staff()), "（12在职+2异常演示账号）")
    print("[自检] 快照:", len(snapshots), "库存负值:", neg,
          "流水:", len(flows), "行")


if __name__ == "__main__":
    _spus = parse_catalog()
    _spus_by_id = {s["product_id"]: s for s in _spus}
    _skus = expand_skus(_spus)
    _orders, _pool = build_sales(_skus, _spus_by_id)
    _sales, _flat = [], []
    for _o in _orders:
        _total = sum(l["qty"] for l in _o["lines"])
        _add = 0.0 if _total >= 4 else round((4 - _total) * 0.10, 2)
        _flat.append({"order_id": _o["order_id"], "total_qty": _total})
        for _li, _l in enumerate(_o["lines"], 1):
            _br = base_rate(_l["spu"]["prod_year"], _l["spu"]["season"])
            _fr = min(_br + _add, 1.0)
            _sales.append({"sale_id": len(_sales) + 1, "order_id": _o["order_id"],
                           "line_no": _li, "date_id": _o["date_id"],
                           "sku_id": _l["sku"]["sku_id"], "quantity": _l["qty"],
                           "list_price": _l["spu"]["list_price"], "base_rate": _br,
                           "order_total_qty": _total, "final_rate": _fr,
                           "actual_amount": round(_l["spu"]["list_price"] * _l["qty"] * _fr, 2),
                           "is_member": 1 if _o["is_member"] else 0,
                           "member_level": _o["level"]})
    _snaps, _flows = build_inventory(_skus, _sales, _spus_by_id)
    write_dw_sql(_spus, _skus, build_dim_date(), _sales, _snaps, _flows, build_staff())
    self_check(_spus, _skus, _sales, _flat, _snaps, _flows)
