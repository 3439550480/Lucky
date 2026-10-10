"""
角色权限注册表（2.0 上下文策略 0.3）

单一事实源：conf/roles.yaml（改配置不改代码）。
职责：加载角色定义 + 把员工的 role_codes 解析为权限点并集（登录时解析一次）+
为前缀【当前用户】段提供"可执行/不可执行"能力名渲染数据。

校验语义：permissions 集合中的权限点字符串是唯一判定依据（StaffIdentity.has），
本模块不做任何业务判断。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.core.log import logger

# 与 capabilities/registry.py 同款定位逻辑：本文件三层深，parents[3] 为项目根
CONFIG_PATH = Path(__file__).resolve().parents[3] / "conf" / "roles.yaml"


@dataclass(frozen=True)
class PermissionDef:
    name: str          # 权限点，如 "inventory.read"
    label: str         # 前缀渲染用的能力名，如 "库存查询"
    description: str


@dataclass(frozen=True)
class RoleDef:
    code: str                      # 角色码，如 "MANAGER"
    name: str                      # 显示名，如 "店长"
    permissions: frozenset[str] = field(default_factory=frozenset)


class RoleRegistry:
    """角色定义内存形态：解析并集 + 前缀渲染数据"""

    def __init__(self, permissions: list[PermissionDef], roles: list[RoleDef]):
        self.permissions: dict[str, PermissionDef] = {p.name: p for p in permissions}
        self.roles: dict[str, RoleDef] = {r.code: r for r in roles}
        self._order: list[str] = [p.name for p in permissions]

    def resolve(self, role_codes: list[str]) -> frozenset[str]:
        """role_codes → 权限点并集（策略 0.3 解析规则）。
        未知角色码忽略并告警（不抛——登录不能因配置脏数据整体失败，fail-fast 在 load 时做）"""
        merged: set[str] = set()
        for code in role_codes or []:
            role = self.roles.get(code)
            if role is None:
                logger.warning(f"[roles] 未知角色码 '{code}'，忽略（staff 配置与 roles.yaml 不一致）")
                continue
            merged |= role.permissions
        return frozenset(merged)

    def role_name(self, code: str) -> str:
        """角色显示名；未知角色码原样返回（不美化不猜测）"""
        role = self.roles.get(code)
        return role.name if role else code

    def display_role_name(self, role_codes: list[str]) -> str:
        """多角色员工的主显示名：取权限最多的角色（如 K001 [STAFF,KEEPER] → 库管）"""
        best = None
        for code in role_codes or []:
            role = self.roles.get(code)
            if role and (best is None or len(role.permissions) > len(best.permissions)):
                best = role
        return best.name if best else "员工"

    def render_role_block(self, permissions: frozenset[str]) -> str:
        """前缀【当前用户】段的可执行/不可执行两行文本（策略 1.2：进前缀区，按角色 4 份分裂）"""
        allowed = [self.permissions[p].label for p in self._order if p in permissions]
        denied = [self.permissions[p].label for p in self._order if p not in permissions]
        return (f"可执行：{'、'.join(allowed) if allowed else '无'}\n"
                f"不可执行：{'、'.join(denied) if denied else '无'}")


def load_roles(path: Path = CONFIG_PATH) -> RoleRegistry:
    """加载 yaml → RoleRegistry。结构错误抛 ValueError（fail-fast，应用启动即失败）"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    permissions = []
    for i, item in enumerate(raw.get("permissions") or []):
        if not item.get("name"):
            raise ValueError(f"roles.yaml permissions[{i}] 缺少 name")
        permissions.append(PermissionDef(
            name=item["name"], label=item.get("label", item["name"]),
            description=item.get("description", "")))
    if not permissions:
        raise ValueError("roles.yaml 未定义任何权限点")
    roles = []
    for i, item in enumerate(raw.get("roles") or []):
        code = item.get("code")
        if not code:
            raise ValueError(f"roles.yaml roles[{i}] 缺少 code")
        unknown = [p for p in (item.get("permissions") or []) if p not in {x.name for x in permissions}]
        if unknown:
            raise ValueError(f"roles.yaml 角色 '{code}' 引用了未定义的权限点：{unknown}")
        roles.append(RoleDef(
            code=code, name=item.get("name", code),
            permissions=frozenset(item.get("permissions") or [])))
    if not roles:
        raise ValueError("roles.yaml 未定义任何角色")
    registry = RoleRegistry(permissions=permissions, roles=roles)
    logger.info(f"角色权限表加载完成: {[r.code for r in roles]} / {len(permissions)} 权限点")
    return registry


# 模块级单例（与 capability registry 同款"导入即生效"模式）
role_registry = load_roles()
