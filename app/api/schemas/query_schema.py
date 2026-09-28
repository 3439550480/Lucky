"""
问数接口请求体定义

集中声明 API 层输入输出的数据结构，让路由函数只处理业务流程，
字段校验和 OpenAPI 文档生成交给 Pydantic 与 FastAPI 完成。
"""

from typing import Optional

from pydantic import BaseModel, Field


class QuerySchema(BaseModel):
    """`/api/query` 请求体，承载用户输入的自然语言问题"""

    # 前端请求体中的 query 字段，例如 {"query": "统计华北地区销售额"}
    query: str
    thread_id: str  # 新增，前端需传递唯一会话ID（如 UUID 或用户ID+时间戳）
    # [02 文档] 用户选择的 LLM provider 名；None/非法 → 后端兜底 default（宽松校验）
    model: Optional[str] = Field(default=None, max_length=64)
    # [04 文档] 前端能力芯片显式选择（tier-0）；None → 自动路由；非法值后端忽略
    capability: Optional[str] = Field(default=None, max_length=64)