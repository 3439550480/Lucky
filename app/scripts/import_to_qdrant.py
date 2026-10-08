# ⚠️ [已废弃] 本脚本为早期硬编码配置的导入实现，仅留作教学对照。
# 正式建库请使用：uv run python -m app.scripts.build_meta_knowledge（配置驱动，meta_config.yaml）
import json
from typing import List, Any
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct
from langchain_huggingface import HuggingFaceEmbeddings

# ==================== 配置 ====================
QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
EMBEDDING_MODEL = "docker/embedding/bge-small-zh"   # 相对项目根（Lucky），需在项目根目录下运行
VECTOR_SIZE = 512
COLLECTION_COLUMN = "column_info_collection"
COLLECTION_METRIC = "metric_info_collection"

COLUMN_NDJSON = "column_info.ndjson"
METRIC_NDJSON = "metric_info.ndjson"


def load_ndjson(file_path: str) -> List[dict]:
    """加载 NDJSON 文件，返回字典列表"""
    data = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                data.append(json.loads(line))
    print(f"从 {file_path} 加载 {len(data)} 条记录")
    return data


def get_text_for_embedding(name: str, description: str, alias: Any) -> str:
    """构造用于生成向量的文本：name + description + alias（用空格拼接）"""
    parts = [name]
    if description:
        parts.append(description)
    if alias:
        if isinstance(alias, str):
            try:
                alias = json.loads(alias)
            except:
                alias = []
        if isinstance(alias, list):
            parts.extend(alias)
    return " ".join(parts).strip()


def recreate_collection(client: QdrantClient, collection_name: str, vector_size: int):
    """删除已存在的集合，并重新创建（确保向量维度正确）"""
    collections = client.get_collections().collections
    if any(c.name == collection_name for c in collections):
        print(f"删除已存在的 collection: {collection_name}")
        client.delete_collection(collection_name)
    print(f"创建 collection: {collection_name} (size={vector_size})")
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
    )


def main():
    print("开始从 NDJSON 文件导入元数据到 Qdrant...")

    # 1. 加载 Embedding 模型
    print(f"加载 Embedding 模型: {EMBEDDING_MODEL}")
    embeddings = HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )

    # 2. 连接 Qdrant
    print(f"连接 Qdrant: {QDRANT_HOST}:{QDRANT_PORT}")
    qdrant_client = QdrantClient(
        url=f"http://{QDRANT_HOST}:{QDRANT_PORT}",
        timeout=120.0,
        prefer_grpc=False
    )

    # 3. 重建 collections
    recreate_collection(qdrant_client, COLLECTION_COLUMN, VECTOR_SIZE)
    recreate_collection(qdrant_client, COLLECTION_METRIC, VECTOR_SIZE)

    # ==================== 导入字段（Column） ====================
    columns = load_ndjson(COLUMN_NDJSON)
    column_points = []
    for idx, col in enumerate(columns, start=1):
        # 提取字段
        col_id = col.get("id")
        if not col_id:
            print(f"警告: 第 {idx} 行字段缺少 id，跳过")
            continue

        name = col.get("name", "")
        col_type = col.get("type", "")
        role = col.get("role", "")
        description = col.get("description", "")
        alias = col.get("alias", [])
        table_id = col.get("table_id", "")
        examples = col.get("examples", [])   # NDJSON 中如果没有 examples，则为空列表

        # 构造用于 embedding 的文本
        text = get_text_for_embedding(name, description, alias)
        if not text:
            print(f"警告: 字段 {col_id} 没有有效文本，跳过")
            continue

        vector = embeddings.embed_query(text)

        # 严格按照 ColumnInfo 实体构造 payload（不包含任何多余字段）
        payload = {
            "id": col_id,
            "name": name,
            "type": col_type,
            "role": role,
            "examples": examples,
            "description": description,
            "alias": alias,
            "table_id": table_id,
        }
        # 使用自增整数作为 Qdrant 点 ID（payload 中保留业务 id）
        point = PointStruct(id=idx, vector=vector, payload=payload)
        column_points.append(point)

    if column_points:
        print(f"正在向 {COLLECTION_COLUMN} 插入 {len(column_points)} 条向量...")
        qdrant_client.upsert(collection_name=COLLECTION_COLUMN, points=column_points)
        print("字段导入完成")
    else:
        print("没有字段需要导入")

    # ==================== 导入指标（Metric） ====================
    metrics = load_ndjson(METRIC_NDJSON)
    metric_points = []
    for idx, metric in enumerate(metrics, start=1):
        metric_id = metric.get("id")
        if not metric_id:
            print(f"警告: 第 {idx} 行指标缺少 id，跳过")
            continue

        name = metric.get("name", "")
        description = metric.get("description", "")
        relevant_columns = metric.get("relevant_columns", [])
        alias = metric.get("alias", [])

        # 处理 relevant_columns 可能为字符串的情况
        if isinstance(relevant_columns, str):
            try:
                relevant_columns = json.loads(relevant_columns)
            except:
                relevant_columns = []
        # 处理 alias 可能为字符串的情况
        if isinstance(alias, str):
            try:
                alias = json.loads(alias)
            except:
                alias = []

        text = get_text_for_embedding(name, description, alias)
        if not text:
            print(f"警告: 指标 {metric_id} 没有有效文本，跳过")
            continue

        vector = embeddings.embed_query(text)

        # 严格按照 MetricInfo 实体构造 payload
        payload = {
            "id": metric_id,
            "name": name,
            "description": description,
            "relevant_columns": relevant_columns,
            "alias": alias,
        }
        point = PointStruct(id=idx, vector=vector, payload=payload)
        metric_points.append(point)

    if metric_points:
        print(f"正在向 {COLLECTION_METRIC} 插入 {len(metric_points)} 条向量...")
        qdrant_client.upsert(collection_name=COLLECTION_METRIC, points=metric_points)
        print("指标导入完成")
    else:
        print("没有指标需要导入")

    print("所有数据导入完成！")


if __name__ == "__main__":
    main()