"""
员工认证仓储（2.0 上下文策略 0.1/0.2）

职责：dim_staff 表的登录校验——三道关（策略 0.2 + 用户画像文档 5.7）：
  ① 账号存在
  ② status = 启用 且 当前日期 ∈ [valid_from, valid_to]（NULL = 不设限）
     —— 两关独立：S007 停用 / T005 有效期已过，都进不来
  ③ 凭据哈希比对：credential_hash 格式 "sha256$<salt>$<digest>"，
     digest = sha256(salt + PIN)（salt 与 PIN 明文直接拼接，UTF-8）——
     与 generate_anta_data.py 的生成算法逐字对应，严禁明文比对
"""
import hashlib
import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class LoginRejected(Exception):
    """登录被拒（含原因）。API 层映射为 401 + 中文提示，不向模型/前端泄露内部细节"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason      # 机器可读：not_found / disabled / expired / bad_credentials
        self.message = message    # 用户可读


class StaffAuthRepository:
    """基于 dw 库 dim_staff 表的登录校验"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_staff(self, staff_id: str) -> dict | None:
        result = await self.session.execute(
            text("SELECT staff_id, name, role_codes, credential_hash, status, "
                 "valid_from, valid_to FROM dim_staff WHERE staff_id = :sid"),
            {"sid": staff_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    @staticmethod
    def _check_hash(credential_hash: str, pin: str) -> bool:
        """校验 "sha256$salt$digest"（格式不符一律 False——宁拒勿错）"""
        parts = (credential_hash or "").split("$")
        if len(parts) != 3 or parts[0] != "sha256":
            return False
        _, salt, digest = parts
        computed = hashlib.sha256((salt + pin).encode("utf-8")).hexdigest()
        return computed == digest

    @staticmethod
    def _check_validity(row: dict, today: datetime.date) -> str | None:
        """状态与有效期双关。通过返回 None，否则返回原因码"""
        if row["status"] != "启用":
            return "disabled"
        valid_from, valid_to = row.get("valid_from"), row.get("valid_to")
        if valid_from is not None and today < valid_from:
            return "expired"
        if valid_to is not None and today > valid_to:
            return "expired"
        return None

    async def authenticate(self, staff_id: str, pin: str) -> dict:
        """登录校验。通过返回 staff 行 dict；失败抛 LoginRejected（原因不区分细节——
        账号不存在与密码错误统一报"工号或 PIN 不正确"，避免账号枚举）"""
        staff_id = (staff_id or "").strip()
        pin = (pin or "").strip()
        if not staff_id or not pin:
            raise LoginRejected("bad_credentials", "请输入工号和 PIN")

        row = await self.get_staff(staff_id)
        if row is None:
            raise LoginRejected("bad_credentials", "工号或 PIN 不正确")

        reason = self._check_validity(row, datetime.date.today())
        if reason == "disabled":
            raise LoginRejected("disabled", "该账号已停用，请联系店长")
        if reason == "expired":
            raise LoginRejected("expired", "该账号已过有效期，请联系店长")

        if not self._check_hash(row["credential_hash"], pin):
            raise LoginRejected("bad_credentials", "工号或 PIN 不正确")

        return row
