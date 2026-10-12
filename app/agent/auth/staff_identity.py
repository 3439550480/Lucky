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


def build_staff_identity(row: dict) -> StaffIdentity:
    """dim_staff 行 → 身份快照（在线登录与评测注入的唯一共用入口）

    谁调用：
    - api/routers/auth_router.login —— 在线：PIN 校验通过后组装身份
    - evaluation/runner.EvaluationRunner._prepare_staff —— 评测：按用例 staff_id 注入
    链路位置：dim_staff 行 →（本函数）→ StaffIdentity →（在线）AuthSession /（评测）DataAgentContext
    设计 why：评测升级方案 R1 的风险 #1 是"评测注入与在线不同构 → 评测失真"，
    同构唯一可靠的保证方式是**同一个函数**组装：role_codes 的分割与权限点解析
    （role_registry.resolve）若两处各写一遍迟早漂移——最典型的漂移是评测侧漏掉
    resolve，权限点恒为空集，F4 权限矩阵会以"全部被拒"的形式假通过。

    示例（评测侧）：
        row = await StaffAuthRepository(session).get_staff("S001")
        identity = build_staff_identity(row)
        identity.has("inventory.read")   # True（S001 = STAFF 角色）
    """
    role_codes = [c.strip() for c in (row["role_codes"] or "").split(",") if c.strip()]
    # 局部导入：roles 模块导入时即读 conf/roles.yaml（模块级单例）。放在函数内让身份值对象
    # 保持"零配置依赖"——不把配置文件加载挂到每一个 import 本模块的脚本上
    from app.agent.auth.roles import role_registry

    return StaffIdentity(
        staff_id=row["staff_id"],
        name=row["name"],
        role_codes=role_codes,
        permissions=role_registry.resolve(role_codes),
    )
