"""
出入库流程接口请求体（2.0 上下文策略 3.6 确认门）

[S3 结构化确认] 确认/取消不再由用户手打文本表达，而是前端按钮点击产生的结构化字段：
协议层只有 confirm/cancel 两个合法值（Pydantic Literal 校验，非法值直接 422），
"对/不对/对不上"这类子串误判从源头消失（2026-10-10 核实的误写库缺陷）。
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class FlowConfirmSchema(BaseModel):
    """`/api/flow/confirm` 请求体：提交或放弃一次待确认的出入库操作"""

    # 会话 ID：与 /api/query 同一个 thread（确认动作必须落在同一个 checkpoint 上）
    thread_id: str
    # 结构化决定：只有两个合法值，无"自由文本"通道
    decision: Literal["confirm", "cancel"]
    # 与 /api/query 同口径的 LLM provider（用量计量）；None → 后端兜底 default
    model: Optional[str] = Field(default=None, max_length=64)
