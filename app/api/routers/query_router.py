"""
问数查询接口路由（02/04 文档）

1. POST /api/query      —— 问数/对话，SSE 流式返回
2. GET  /api/models     —— 可用 LLM provider 列表（前端模型下拉数据源，配置驱动）
3. GET  /api/capabilities —— 可选能力芯片列表（capability_config 驱动，前端零硬编码）
路由层只处理请求体、依赖声明和响应类型，不直接创建 Repository 或执行图节点。
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from starlette.responses import StreamingResponse

from app.agent.capabilities.registry import registry
from app.api.dependencies import get_query_service
from app.api.schemas.query_schema import QuerySchema
from app.conf.app_config import app_config
from app.services.query_service import QueryService

# 当前模块只维护查询相关接口，避免后续所有 API 都挤在 main.py 中
query_router = APIRouter()


def _group_of(provider_name: str) -> str:
    """从 provider 名推导厂商分组（前端下拉 optgroup 用）；新增厂商组时改这一处"""
    if provider_name.startswith("deepseek"):
        return "DeepSeek"
    if provider_name.startswith("qwen"):
        return "Qwen"
    if provider_name.startswith("glm"):
        return "GLM"
    return "Other"


@query_router.get("/api/models")
async def models_handler():
    """可用 LLM provider 列表（来自 app_config.llm，前端不硬编码）。
    不返回价格/计费时段 —— DeepSeek 峰谷是计费口径而非用户选项（02 文档 §3.1）"""
    providers = [
        {"name": name, "model": cfg.get("model", ""), "group": _group_of(name)}
        for name, cfg in app_config.llm.providers.items()
    ]
    return {"default": app_config.llm.default, "providers": providers}


@query_router.get("/api/capabilities")
async def capabilities_handler():
    """可选能力芯片列表（selectable=true 的能力；capability_chip 开关关闭时返回空）"""
    if not app_config.features.capability_chip:
        return {"capabilities": []}
    return {
        "capabilities": [
            {"name": cap.name, "description": cap.description}
            for cap in registry.selectable_capabilities()
        ]
    }


@query_router.post("/api/query")
async def query_handler(
    # 请求体参数：FastAPI 会把前端 JSON 自动解析成 QuerySchema
    query: QuerySchema,
    # 服务依赖：FastAPI 会调用 get_query_service，递归组装它所需的仓储和客户端
    query_service: Annotated[QueryService, Depends(get_query_service)],
):
    """接收用户自然语言问题，并流式返回 LangGraph 工作流输出"""

    return StreamingResponse(
        # model/capability 均为可选字段，None 时由后端兜底（宽松校验，02/04 文档）
        query_service.query(
            query.query, query.thread_id,
            model=query.model, capability=query.capability,
        ),
        media_type="text/event-stream",
    )
