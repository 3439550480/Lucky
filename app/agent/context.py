"""
电商问数 Agent 运行上下文

Context 用来保存一次图执行过程中不参与状态合并的外部依赖或配置
本章放入多路召回需要的 Embedding 客户端 Qdrant 仓储和 ES 仓储
召回信息合并阶段还会访问 Meta MySQL，用于按 id 补齐字段和表结构元数据
这样节点可以通过 runtime.context 复用外部工具，而不需要把连接类对象塞进 State
多模型改造（01 文档）后新增：按请求创建的 LLM 实例与 SSE 能力注入容器
"""

from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_huggingface import HuggingFaceEndpointEmbeddings

from app.agent.auth.staff_identity import StaffIdentity
from app.agent.capabilities.registry import CapabilityRegistry
from app.agent.usage import LLMUsageTracker
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.mysql.dw.inventory_write_repository import InventoryWriteRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository


class CapabilityHolder:
    """Request 级可变容器：路由节点写入选中的能力，QueryService 读取后注入 SSE 事件
    （04 文档 §3.4 的 tier-0 落地件）。

    ⚠️ 必须每个请求新建实例（DataAgentContext 组装时创建）—— 若做成模块级单例，
    并发请求会互相覆盖能力标识，导致 SSE 事件的 capability 串话（00 红线 #10）
    """
    value: str | None = None


class DataAgentContext(TypedDict):
    """LangGraph Runtime 中传递的上下文对象"""

    # 字段向量仓储，负责根据向量从 Qdrant 检索候选字段
    column_qdrant_repository: ColumnQdrantRepository
    # Embedding 客户端，负责把关键词转换成向量检索所需的 query vector
    embedding_client: HuggingFaceEndpointEmbeddings
    # 指标向量仓储，负责根据向量从 Qdrant 检索候选指标
    metric_qdrant_repository: MetricQdrantRepository
    # 字段取值全文检索仓储，负责从 Elasticsearch 检索真实字段值
    value_es_repository: ValueESRepository
    # 元数据仓储，负责在召回结果合并时补齐字段 表 主外键等结构信息
    meta_mysql_repository: MetaMySQLRepository

    dw_mysql_repository: DWMySQLRepository
    # [2.0 第三章] 出入库写仓储（flow_step 确认后的写库入口；幂等/事务/快照联动）
    inventory_write_repository: "InventoryWriteRepository"

    # [01 文档] 按请求创建的 LLM 实例（已挂 usage tracker），节点经 runtime.context 取用
    llm: BaseChatModel
    # [04 文档] SSE capability 注入容器：路由节点写入，QueryService 逐事件读取
    capability_holder: CapabilityHolder
    # [04 文档] 能力注册表（只读配置对象，graph 组装时加载，安全共享）
    capability_registry: "CapabilityRegistry"
    # [04 文档] 请求级用量采集器（路由分类专用 LLM 的调用也计入同一份账）
    usage_tracker: "LLMUsageTracker"
    # [06 文档] 长期记忆存储（retriever 注入与 extractor 落库共用；
    # features.memory.long_term 关闭时节点不消费，字段可缺省——消费方用 .get 容错）
    memory_store: "MemoryStore"
    # [2.0 上下文策略 0.4] 员工身份 —— 进 Context 不进 State（State 被 checkpointer
    # 持久化，身份不该留在历史快照里）。节点经 runtime.context["staff"].has(权限点)
    # 做代码层硬校验；写操作的经办人 = staff.staff_id
    staff: StaffIdentity