"""
出入库写仓储（2.0 上下文策略 3.6 / D21）

职责：确认门之后的唯一写库入口。
  - 幂等：事务内复查 fact_inventory_flow.idempotency_key（单据号:SKU:操作类型）——
    重复提交（双击/网络重试/同单据再录）零副作用，返回已处理标记
  - 留痕：经办人 = staff_id（dim_staff 工号），operator 姓名展示用
  - 联动：fact_inventory_flow 插入 + dim_inventory_snapshot 最新日快照可用/账面同步
  - 事务：同一 AsyncSession 上先写后 commit，失败整体回滚（调用方 finally 不 commit）
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class FlowAlreadyProcessed(Exception):
    """幂等拦截：该 idempotency_key 已存在（事务内复查命中）"""


class InventoryWriteRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def doc_exists(self, doc_no: str) -> bool:
        """单据号幂等预检（槽位校验阶段，写库前拦截）"""
        result = await self.session.execute(
            text("SELECT COUNT(*) AS n FROM fact_inventory_flow WHERE doc_no = :d"),
            {"d": doc_no},
        )
        return result.scalar() > 0

    async def sku_exists(self, sku_id: str) -> bool:
        result = await self.session.execute(
            text("SELECT COUNT(*) AS n FROM dim_sku WHERE sku_id = :s"),
            {"s": sku_id},
        )
        return result.scalar() > 0

    async def latest_available(self, sku_id: str) -> int | None:
        """最新快照日可用库存（出库超量校验 + 入库后回显）；无快照返回 None"""
        result = await self.session.execute(
            text("SELECT available_qty FROM dim_inventory_snapshot "
                 "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM dim_inventory_snapshot) "
                 "AND sku_id = :s"),
            {"s": sku_id},
        )
        return result.scalar()

    async def commit_flow(self, *, direction: str, doc_no: str, sku_id: str,
                          qty: int, operator_id: str, operator_name: str) -> dict:
        """确认后的写库：flow 插入 + 快照联动，单事务提交。
        返回 {flow_id, available_after, date_id}；幂等命中抛 FlowAlreadyProcessed

        [规则3 留档 + 实测缺陷修复 2026-10-10] 原版 date_id 取真实今天（20261010）——
        但快照联动更新的是最新快照日（10-07），导致"快照含这笔 +20、按 10-07 查流水
        却找不到"的口径分裂（用户实测抓出：70→100 对不上流水）。修复：写操作的
        date_id 归入**最新快照日**（单店演示数据窗口固定的必然口径），流水与快照永远一致。
        """
        flow_type = "补货入库-早班" if direction == "in" else "销售出库"
        sign = qty if direction == "in" else -qty
        snap_date = await self.session.execute(
            text("SELECT MAX(snapshot_date) FROM dim_inventory_snapshot"))
        date_id = int(snap_date.scalar())
        idempotency_key = f"{doc_no}:{sku_id}:{'IN' if direction == 'in' else 'OUT'}"

        # 事务内幂等复查（预检与提交之间可能被并发写入——D21 防重复记账）
        if await self.doc_exists(doc_no):
            raise FlowAlreadyProcessed(doc_no)

        next_id = await self.session.execute(
            text("SELECT COALESCE(MAX(flow_id), 0) + 1 FROM fact_inventory_flow"))
        flow_id = next_id.scalar()

        await self.session.execute(text(
            "INSERT INTO fact_inventory_flow "
            "(flow_id, doc_no, flow_type, sku_id, quantity, date_id, operator_id, idempotency_key, operator) "
            "VALUES (:fid, :doc, :ftype, :sku, :qty, :date_id, :op_id, :idem, :op_name)"),
            {"fid": flow_id, "doc": doc_no, "ftype": flow_type, "sku": sku_id,
             "qty": sign, "date_id": date_id, "op_id": operator_id,
             "idem": idempotency_key, "op_name": operator_name},
        )
        await self.session.execute(text(
            "UPDATE dim_inventory_snapshot "
            "SET available_qty = available_qty + :sign, book_qty = book_qty + :sign "
            "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM (SELECT MAX(snapshot_date) AS snapshot_date "
            "FROM dim_inventory_snapshot) t) AND sku_id = :s"),
            {"sign": sign, "s": sku_id},
        )
        await self.session.commit()   # 单事务：flow 与快照同生共死

        available_after = await self.latest_available(sku_id)
        return {"flow_id": flow_id, "flow_type": flow_type,
                "available_after": available_after, "date_id": date_id}
