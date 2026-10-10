"""
StaffIdentity —— 员工身份值对象（2.0 上下文策略 0.4）

链路位置：鉴权中间件查 AuthSession → 组装 DataAgentContext(staff=identity)
→ 图内节点经 runtime.context["staff"].has(权限点) 做代码层硬校验。

为什么进 Context 不进 State（策略 0.4 三条理由）：
1. State 被 checkpointer 持久化进轨迹快照 —— 身份不该留在历史快照里
2. TypedDict 是覆盖语义，节点 return 不带身份字段会意外丢失
3. 身份是"运行时环境"，不是"业务数据"
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StaffIdentity:
    """一次登录的身份快照（由 AuthSession 持有，每请求经 Context 注入）"""

    staff_id: str                          # 工号（登录名 + 经办人标识 + 记忆作用域键）
    name: str                              # 姓名（展示/审计用；不进 KV cache 前缀）
    role_codes: list[str]                  # 角色码，如 ["STAFF", "KEEPER"]
    permissions: frozenset[str] = field(default_factory=frozenset)

    def has(self, permission: str) -> bool:
        """代码层权限点判定（字符串比对，不由模型判断）"""
        return permission in self.permissions

    @property
    def primary_role_name(self) -> str:
        """主要角色显示名（前缀【当前用户】段用；多角色取第一个）"""
        return self.role_codes[0] if self.role_codes else ""
