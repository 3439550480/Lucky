"""
经营洞察仓储（2.0）—— 热卖排行侧边栏的固定查询

定位：不走能力路由与 LLM（用户拍板：热卖排行移出对话能力，改前端侧边栏固定面板）。
口径：国庆档全窗口按销量排序的 Top N 商品（货号级聚合）。
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class InsightsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def hot_products(self, limit: int = 5) -> list[dict]:
        """热卖排行：按销量降序的商品 Top N（含销售额，供条形与文字展示）"""
        sql = """
        SELECT p.product_name AS product_name,
               SUM(f.quantity) AS quantity,
               SUM(f.actual_amount) AS amount
        FROM fact_sales f
        JOIN dim_sku s ON f.sku_id = s.sku_id
        JOIN dim_product p ON s.product_id = p.product_id
        GROUP BY p.product_name
        ORDER BY quantity DESC
        LIMIT :limit
        """
        result = await self.session.execute(text(sql), {"limit": limit})
        rows = [dict(r) for r in result.mappings().fetchall()]
        # Decimal → float（前端直接渲染）；行数少，逐行转换开销可忽略
        for row in rows:
            for key in ("quantity", "amount"):
                if row.get(key) is not None:
                    row[key] = float(row[key])
        return rows
