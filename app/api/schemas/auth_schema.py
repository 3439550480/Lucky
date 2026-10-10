"""
认证接口请求体 schema（2.0 上下文策略 0.2）
"""
from pydantic import BaseModel, Field


class LoginSchema(BaseModel):
    """POST /api/login 请求体：工号 + PIN"""
    staff_id: str = Field(min_length=1, max_length=20, description="员工工号（登录名）")
    pin: str = Field(min_length=1, max_length=64, description="PIN 码")
