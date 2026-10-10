"""
FastAPI 应用入口

负责创建后端应用实例，并把各业务模块中的 router 挂载到同一个 app 上。
本章先只注册问数查询接口，后续 lifespan、middleware、Depends 等工程能力
也会从这里逐步接入。
"""

import uuid

from fastapi import FastAPI, Request

from app.api.lifespan import lifespan
from app.api.middleware.rate_limit import demo_guard_middleware
from app.api.routers.auth_router import auth_router
from app.api.routers.query_router import query_router
from app.core.context import request_id_ctx_var

app = FastAPI(lifespan=lifespan)

app.include_router(query_router)
app.include_router(auth_router)   # [2.0] 登录/登出/身份/热卖侧边栏

# 公网防护中间件（v1.1 FR-05）：费用熔断前置检查 → IP 限流，仅拦 POST /api/query；
# conf/app_config.yaml security 段默认全关 = 行为与 v1.0 完全一致
app.middleware("http")(demo_guard_middleware)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    # 请求被处理之前，先为当前请求生成一个唯一 ID
    request_id = uuid.uuid4()

    # 写入当前异步上下文，后续业务日志会从这里取 request_id
    request_id_ctx_var.set(request_id)

    # 继续执行后续路由处理逻辑
    response = await call_next(request)

    # 请求被处理之后，返回响应
    return response