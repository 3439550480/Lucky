"""
认证与洞察接口路由（2.0 上下文策略 0.2 / 用户拍板"热卖排行改侧边栏"）

1. POST /api/login        —— 工号 + PIN → AuthSession token（唯一免鉴权端点）
2. POST /api/logout       —— 吊销当前 token（幂等）
3. GET  /api/me           —— 当前身份（前端刷新后恢复登录态）
4. GET  /api/insights/hot —— 热卖排行 TopN（固定 SQL，登录即可见，不占权限点）
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from app.agent.auth.auth_session_store import auth_session_store
from app.agent.auth.roles import role_registry
from app.agent.auth.staff_identity import StaffIdentity
from app.api.dependencies import (
    get_current_staff,
    get_insights_repository,
    get_staff_auth_repository,
)
from app.api.schemas.auth_schema import LoginSchema
from app.core.context import request_id_ctx_var
from app.repositories.mysql.dw.insights_repository import InsightsRepository
from app.repositories.mysql.dw.staff_auth_repository import StaffAuthRepository

auth_router = APIRouter()


def _token_of(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


@auth_router.post("/api/login")
async def login(
    body: LoginSchema,
    repo: StaffAuthRepository = Depends(get_staff_auth_repository),
):
    """工号 + PIN 登录。失败统一 401（不存在/密码错/停用/过期文案已区分）"""
    try:
        row = await repo.authenticate(body.staff_id, body.pin)
    except Exception as e:      # LoginRejected → 401；DB 异常 → 500
        from app.repositories.mysql.dw.staff_auth_repository import LoginRejected
        if isinstance(e, LoginRejected):
            raise HTTPException(status_code=401, detail=e.message) from e
        request_id_ctx_var.set(uuid.uuid4())   # 保持日志上下文一致
        raise HTTPException(status_code=500, detail="登录服务暂不可用，请稍后重试") from e

    role_codes = [c.strip() for c in (row["role_codes"] or "").split(",") if c.strip()]
    identity = StaffIdentity(
        staff_id=row["staff_id"],
        name=row["name"],
        role_codes=role_codes,
        permissions=role_registry.resolve(role_codes),
    )
    session = auth_session_store.create(identity)
    return {
        "token": session.token,
        "staff": {
            "staff_id": identity.staff_id,
            "name": identity.name,
            "role_codes": identity.role_codes,
            "role_name": role_registry.role_name(identity.primary_role_name),
        },
    }


@auth_router.post("/api/logout")
async def logout(request: Request):
    """登出：吊销当前 token（幂等——无效 token 也返回成功）"""
    auth_session_store.destroy(_token_of(request))
    return {"ok": True}


@auth_router.get("/api/me")
async def me(staff: StaffIdentity = Depends(get_current_staff)):
    """当前身份（前端刷新后用 sessionStorage 里的 token 恢复登录态）"""
    return {
        "staff": {
            "staff_id": staff.staff_id,
            "name": staff.name,
            "role_codes": staff.role_codes,
            "role_name": role_registry.role_name(staff.primary_role_name),
        },
    }


@auth_router.get("/api/insights/hot")
async def hot(
    staff: StaffIdentity = Depends(get_current_staff),   # 登录即可见（不占权限点）
    repo: InsightsRepository = Depends(get_insights_repository),
):
    """热卖排行 TopN（侧边栏数据源；固定 SQL，与对话能力解耦）"""
    return {"items": await repo.hot_products(limit=5)}
