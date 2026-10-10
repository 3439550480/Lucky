"""
出入库流程接口路由（2.0 上下文策略 3.6 确认门）

POST /api/flow/confirm —— 把"确认/取消"做成按钮的独立端点。
前端点击按钮 → 结构化决定（confirm/cancel）→ state.confirm_decision → flow_step 确认门。
与 /api/query 的关键差异：没有自由文本进入判定环节（既不进 LLM，也不进正则），
S3"误写库"缺陷在协议层被消除；返回值仍是 SSE，与问数链路同构，前端复用同一套事件处理。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import StreamingResponse

from app.agent.auth.auth_session_store import thread_bindings
from app.agent.auth.staff_identity import StaffIdentity
from app.api.dependencies import get_current_staff, get_query_service
from app.api.schemas.flow_schema import FlowConfirmSchema
from app.services.query_service import QueryService

flow_router = APIRouter()


@flow_router.post("/api/flow/confirm")
async def flow_confirm_handler(
    body: FlowConfirmSchema,
    query_service: Annotated[QueryService, Depends(get_query_service)],
    staff: StaffIdentity = Depends(get_current_staff),
):
    """提交/放弃一次待确认的出入库操作（SSE 流式返回，事件协议与 /api/query 一致）"""

    # 权限前置（策略 3.7）：与 flow_guard 同口径——无 inventory.write 直接 403。
    # 端点层拒绝比 SSE 里回一句"没权限"更好：前端能拿到明确状态码
    if not staff.has("inventory.write"):
        raise HTTPException(status_code=403, detail="当前角色无出入库权限")

    # 会话归属校验（与 /api/query 同口径）：会话绑定登录人，换人必须开新会话
    if not thread_bindings.check_and_bind(body.thread_id, staff.staff_id):
        raise HTTPException(status_code=403, detail="该会话属于其他员工，请开新会话后再操作")

    return StreamingResponse(
        query_service.confirm(body.thread_id, body.decision,
                              model=body.model, staff=staff),
        media_type="text/event-stream",
    )
