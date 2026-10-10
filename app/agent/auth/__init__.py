"""
员工身份与认证（2.0 上下文与记忆策略 0.1/0.2/0.3）

三个组件：
  StaffIdentity      —— 身份值对象（进 DataAgentContext，不进 State；策略 0.4）
  roles.py           —— conf/roles.yaml 的内存形态（角色→权限点并集解析）
  auth_session_store —— AuthSession 存储（token 查表可吊销，不用 JWT）
"""
from app.agent.auth.staff_identity import StaffIdentity

__all__ = ["StaffIdentity"]
