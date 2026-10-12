"""
指标向量仓储

管理指标向量集合，并把 Service 层准备好的指标 point 批量写入 Qdrant

字段和指标虽然都用向量检索，但它们是两类不同对象
所以指标单独使用 metric_info_collection，避免后续召回时和字段结果混在一起
"""

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.conf.app_config import app_config
from app.entities.metric_info import MetricInfo


class MetricQdrantRepository:
    """负责指标向量集合的创建和批量写入"""

    collection_name = "metric_info_collection"

    def __init__(self, client: AsyncQdrantClient):
        self.client = client

    async def ensure_collection(self):
        """确保指标向量集合存在，并按当前 Embedding 维度初始化"""
        if not await self.client.collection_exists(self.collection_name):
            await self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    # 向量维度必须和 Embedding 模型输出一致，否则写入时会失败
                    size=app_config.qdrant.embedding_size,
                    distance=Distance.COSINE,
                ),
            )

        # 在 class MetricQdrantRepository 内增加：

    async def search(
                self, embedding: list[float], score_threshold: float = 0.6, limit: int = 20
        ) -> list[MetricInfo]:
            """按向量相似度检索指标元数据，并还原为 MetricInfo 实体"""
            result = await self.client.query_points(
                collection_name=self.collection_name,
                query=embedding,
                limit=limit,
                score_threshold=score_threshold,
            )
            return [MetricInfo(**point.payload) for point in result.points]

    async def recreate(self):
        """重建集合：先删除已有集合，再按当前维度创建

        谁调用：MetaKnowledgeService._save_metrics_to_qdrant（构建知识库时）
        为什么必须删：与字段集合同因——历史版本用 uuid4 随机 id 且只 upsert 不删除，
        实测指标集合攒到 164 点（8 指标 × 约 2 语义文本 ≈ 16 点/次 × 约 10 次构建）。
        重复点会占满 search(limit=20) 的额度：实测搜「可用库存」20 个命中里
        只有 1 个不同指标、搜「销量」只有 4 个 → 期望指标被挤出 top-5。
        """
        if await self.client.collection_exists(self.collection_name):
            await self.client.delete_collection(self.collection_name)
        await self.ensure_collection()

    async def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        payloads: list[dict],
        batch_size: int = 10,
    ):
        """分批 upsert 指标向量点，避免一次提交过多 point"""
        # ids embeddings payloads 三个列表按相同下标组成一条完整的 Qdrant point
        points: list[PointStruct] = [
            PointStruct(id=id, vector=embedding, payload=payload)
            for id, embedding, payload in zip(ids, embeddings, payloads)
        ]
        for i in range(0, len(points), batch_size):
            await self.client.upsert(
                collection_name=self.collection_name, points=points[i : i + batch_size]
            )

