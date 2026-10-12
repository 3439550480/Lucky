
"""
元数据知识构建服务（完整版，可直接替换）

负责组织元数据知识库构建的核心业务流程，位于脚本入口和仓储层之间。
当前主线覆盖：
- 表字段入库
- 字段向量索引（拆分语义入口）
- 字段取值全文索引（可选，依赖 ES）
- 指标入库
- 指标向量索引
"""

import uuid
import datetime
import decimal
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from sqlalchemy import text

from omegaconf import OmegaConf

from app.conf.meta_config import MetaConfig
from app.core.log import logger
from app.entities.column_info import ColumnInfo
from app.entities.column_metric import ColumnMetric
from app.entities.metric_info import MetricInfo
from app.entities.table_info import TableInfo
from app.entities.value_info import ValueInfo
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository


def _point_id(entity_id: str, text: str) -> str:
    """生成确定性向量点 id —— 同一实体 + 同一语义文本永远映射到同一个 id

    谁调用：_save_column_info_to_qdrant / _save_metrics_to_qdrant（构建知识库时）
    设计 why：原实现用 uuid.uuid4()，每次构建都产生新 id，而 upsert 只增不减 →
    集合随构建次数线性膨胀（实测字段 1060 点、指标 164 点，各约 10 次构建的残留）。
    膨胀点会占满 search(limit=N) 的额度，把其他实体的点挤出召回结果。
    改用 uuid5 定名空间哈希后：同一份 meta_config 重复构建 = 幂等覆盖，集合不膨胀；
    配合仓储层 recreate()，索引内容与配置严格一致。
    必须是 UUID 或 uint64 形式（Qdrant 拒绝其他字符串 id），故走 uuid5。

    示例：_point_id("fact_sales.actual_amount", "销售金额")
          -> "6f0b2c0e-...（同一入参每次结果一致）"
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{entity_id}::{text}"))


class MetaKnowledgeService:
    """负责串联元数据知识库构建流程的应用服务"""

    def __init__(
        self,
        meta_mysql_repository: MetaMySQLRepository,
        dw_mysql_repository: DWMySQLRepository,
        column_qdrant_repository: ColumnQdrantRepository,
        embedding_client,  # 实际是 HuggingFaceEmbeddings，通过异步封装调用
        value_es_repository: Optional[ValueESRepository] = None,
        metric_qdrant_repository: Optional[MetricQdrantRepository] = None,
    ):
        self.meta_mysql_repository = meta_mysql_repository
        self.dw_mysql_repository = dw_mysql_repository
        self.column_qdrant_repository = column_qdrant_repository
        self.embedding_client = embedding_client
        self.value_es_repository = value_es_repository
        self.metric_qdrant_repository = metric_qdrant_repository

    # ========== 表与字段入库 ==========

    @staticmethod
    def _normalize_example(v) -> object:
        """DB 原生对象 → JSON 可序列化值。

        [规则3 留档] 原版：examples 原样透传 DB 驱动对象。
        缺陷：数据集引入 DATE 列（open_date 等）后 datetime.date 无法 JSON 序列化，
        DECIMAL 列（order_amount）同理，建库直接失败（2026-10-08 实测）。
        修复：入库前统一归一化 —— 日期转 ISO 字符串、Decimal 转 float、bytes 解码。
        """
        if isinstance(v, (datetime.datetime, datetime.date)):
            return v.isoformat()
        if isinstance(v, decimal.Decimal):
            return float(v)
        if isinstance(v, bytes):
            return v.decode("utf-8", errors="replace")
        return v

    async def _save_tables_to_meta_db(self, meta_config: MetaConfig) -> list[ColumnInfo]:
        table_infos: list[TableInfo] = []
        column_infos: list[ColumnInfo] = []

        for table in meta_config.tables:
            table_info = TableInfo(
                id=table.name,
                name=table.name,
                role=table.role,
                description=table.description,
            )
            table_infos.append(table_info)

            column_types = await self.dw_mysql_repository.get_column_types(table.name)

            for column in table.columns:
                column_values = await self.dw_mysql_repository.get_column_values(
                    table.name, column.name
                )
                column_info = ColumnInfo(
                    id=f"{table.name}.{column.name}",
                    name=column.name,
                    type=column_types[column.name],
                    role=column.role,
                    examples=[self._normalize_example(v) for v in column_values],
                    description=column.description,
                    alias=column.alias,
                    table_id=table.name,
                )
                column_infos.append(column_info)

        # 清空旧数据，避免主键冲突
        session = self.meta_mysql_repository.session
        await session.execute(text("DELETE FROM column_info"))
        await session.execute(text("DELETE FROM table_info"))
        await session.commit()

        async with self.meta_mysql_repository.session.begin():
            self.meta_mysql_repository.save_table_infos(table_infos)
            self.meta_mysql_repository.save_column_infos(column_infos)

        return column_infos

    # ========== 字段向量索引（拆点模式，提高召回率） ==========
    async def _save_column_info_to_qdrant(self, column_infos: list[ColumnInfo]):
        # [S5 索引一致性] 删除重建：保证集合内容 = 本次 meta_config（清掉历史重复点）
        await self.column_qdrant_repository.recreate()

        points: list[dict] = []
        for col in column_infos:
            # 拆成多条语义入口：字段名、描述、别名各为一个点
            # 因为这三个字段是用户自然语言问题中最可能直接提到的内容，而其他字段不适合做语义检索：
            points.append({
                "id": _point_id(col.id, col.name),
                "embedding_text": col.name,
                "payload": asdict(col),
            })
            points.append({
                "id": _point_id(col.id, col.description),
                "embedding_text": col.description,
                "payload": asdict(col),
            })
            for alia in col.alias:
                points.append({
                    "id": _point_id(col.id, alia),
                    "embedding_text": alia,
                    "payload": asdict(col),
                })

        # 分批向量化
        texts = [p["embedding_text"] for p in points]
        batch_size = 20
        embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            batch_emb = await self.embedding_client.aembed_documents(batch)
            embeddings.extend(batch_emb)

        ids = [p["id"] for p in points]
        payloads = [p["payload"] for p in points]
        await self.column_qdrant_repository.upsert(ids, embeddings, payloads)



    async def _save_value_info_to_es(
            self, meta_config: MetaConfig, column_infos: list[ColumnInfo]
    ):
        await self.value_es_repository.ensure_index()

        value_infos = []

        for col_info in column_infos:
            # ======================
            # ✅ 直接从配置里取 sync，永不丢失！
            # ======================
            sync = False
            for table in meta_config.tables:
                for col in table.columns:
                    full_id = f"{table.name}.{col.name}"
                    if full_id == col_info.id:
                        sync = col.sync
                        break

            if not sync:
                continue

            try:
                raw_values = await self.dw_mysql_repository.get_column_values(
                    col_info.table_id, col_info.name, 100000
                )
                # print(f"表={col_info.table_id} 字段={col_info.name} → 数量：{len(raw_values)}")
                # appended_count = 0
                for val in raw_values:
                    if val is None or (isinstance(val, str) and val.strip() == ''):
                        continue  # 跳过空值
                    v = val[0] if isinstance(val, (tuple, list)) else val
                    clean_v = str(v).strip()
                    value_infos.append(ValueInfo(
                        id=f"{col_info.id}.{clean_v}",
                        value=clean_v,
                        column_id=col_info.id
                    ))
                    # appended_count += 1
                # print(f"字段 {col_info.id} 实际 append 数量：{appended_count}")

            except Exception as e:
                continue

        # print(f"最终准备写入 ES 总数：{len(value_infos)}")
        await self.value_es_repository.index(value_infos)
    #
    # ========== 指标入库 ==========
    async def _save_metrics_to_meta_db(self, meta_config: MetaConfig) -> list[MetricInfo]:
        metric_infos = []
        column_metrics = []

        for metric in meta_config.metrics:
            m = MetricInfo(
                id=metric.name,
                name=metric.name,
                description=metric.description,
                relevant_columns=metric.relevant_columns,
                alias=metric.alias,
            )
            metric_infos.append(m)
            for col in metric.relevant_columns:
                column_metrics.append(ColumnMetric(column_id=col, metric_id=metric.name))

        # 清空旧数据
        session = self.meta_mysql_repository.session
        await session.execute(text("DELETE FROM column_metric"))
        await session.execute(text("DELETE FROM metric_info"))
        await session.commit()

        async with self.meta_mysql_repository.session.begin():
            self.meta_mysql_repository.save_metric_infos(metric_infos)
            self.meta_mysql_repository.save_column_metrics(column_metrics)

        return metric_infos



    # ========== 指标向量索引 ==========
    async def _save_metrics_to_qdrant(self, metric_infos: list[MetricInfo]):
        # [S5 索引一致性] 删除重建：保证集合内容 = 本次 meta_config（清掉历史重复点）
        await self.metric_qdrant_repository.recreate()

        points = []
        for m in metric_infos:
            points.append({
                "id": _point_id(m.id, m.name),
                "embedding_text": m.name,
                "payload": asdict(m),
            })
            points.append({
                "id": _point_id(m.id, m.description),
                "embedding_text": m.description,
                "payload": asdict(m),
            })
            for alias in m.alias:
                points.append({
                    "id": _point_id(m.id, alias),
                    "embedding_text": alias,
                    "payload": asdict(m),
                })

        texts = [p["embedding_text"] for p in points]
        batch_size = 20
        embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            batch_emb = await self.embedding_client.aembed_documents(batch)
            embeddings.extend(batch_emb)

        ids = [p["id"] for p in points]
        payloads = [p["payload"] for p in points]
        await self.metric_qdrant_repository.upsert(ids, embeddings, payloads)

    # ========== 总控构建 ==========
    async def build(self, config_path: Path):
        # config_path:.yaml,MetaConfig:.py
        context = OmegaConf.load(config_path)
        schema = OmegaConf.structured(MetaConfig)
        meta_config: MetaConfig = OmegaConf.to_object(OmegaConf.merge(schema, context))

        if meta_config.tables:
            column_infos = await self._save_tables_to_meta_db(meta_config)
            logger.info("表字段信息已保存到 Meta MySQL")

            await self._save_column_info_to_qdrant(column_infos)
            logger.info("字段向量索引构建完成")

            await self._save_value_info_to_es(meta_config, column_infos)
            logger.info("字段取值全文索引处理完成（如已启用）")

        if meta_config.metrics:
            metric_infos = await self._save_metrics_to_meta_db(meta_config)
            logger.info("指标信息已保存到 Meta MySQL")

            await self._save_metrics_to_qdrant(metric_infos)
            logger.info("指标向量索引构建完成")