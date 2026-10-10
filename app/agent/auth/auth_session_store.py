"""
AuthSession 存储（2.0 上下文策略 0.2）

为什么不用 JWT：JWT 签发后在过期前无法撤销——临时工提前离职时只能等自然过期，
在有权限分级的场景不可接受。服务端查表可随时吊销（destroy）。

存在哪里：服务端内存 dict(token → AuthSession)，与 InMemorySessionStore 抽象同款风格；
Redis 实现预留接口（v1 单进程演示，内存即可）。

生命周期：
  签发  create()        —— 登录成功
  校验  get()           —— token 存在 且 未过绝对期 且 未过闲置期；闲置未过则 touch 刷新
  销毁  destroy()       —— 主动登出 / 超时（惰性清除）/ 服务重启（内存态）
"""
import secrets
import threading
import time
from dataclasses import dataclass

from app.agent.auth.staff_identity import StaffIdentity
from app.conf.app_config import app_config
from app.core.log import logger


@dataclass(frozen=True)
class AuthSession:
    """一次登录的身份凭据（服务端持有全量，客户端只拿 token）"""

    token: str
    identity: StaffIdentity
    issued_at: float
    expires_at: float       # 绝对过期 = issued_at + token_ttl
    last_active_at: float   # 闲置判定基准，每次 get 命中刷新


class AuthSessionStore:
    """token → AuthSession 内存表（线程锁保护；FastAPI 单进程多协程共用）"""

    def __init__(self, ttl_seconds: int, idle_seconds: int):
        self._ttl_seconds = ttl_seconds
        self._idle_seconds = idle_seconds
        self._sessions: dict[str, AuthSession] = {}
        self._lock = threading.Lock()

    def create(self, identity: StaffIdentity) -> AuthSession:
        """签发：登录成功后调用。同一员工重复登录不互踢（多端/多标签页可用）"""
        now = time.time()
        session = AuthSession(
            token=secrets.token_urlsafe(32),
            identity=identity,
            issued_at=now,
            expires_at=now + self._ttl_seconds,
            last_active_at=now,
        )
        with self._lock:
            self._sessions[session.token] = session
        logger.info(f"[auth] 签发会话: {identity.staff_id}（{identity.role_codes}）")
        return session

    def get(self, token: str) -> AuthSession | None:
        """校验 + 活跃刷新。失效（不存在/绝对过期/闲置超时）一律返回 None 并惰性清除"""
        if not token:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if now >= session.expires_at:
                self._sessions.pop(token, None)
                logger.info(f"[auth] 会话绝对过期: {session.identity.staff_id}")
                return None
            if now - session.last_active_at >= self._idle_seconds:
                self._sessions.pop(token, None)
                logger.info(f"[auth] 会话闲置超时: {session.identity.staff_id}")
                return None
            refreshed = AuthSession(
                token=session.token, identity=session.identity,
                issued_at=session.issued_at, expires_at=session.expires_at,
                last_active_at=now,          # touch：活跃刷新
            )
            self._sessions[token] = refreshed
            return refreshed

    def destroy(self, token: str) -> bool:
        """主动登出。token 不存在也返回 False（幂等，不报错）"""
        with self._lock:
            removed = self._sessions.pop(token, None)
        if removed:
            logger.info(f"[auth] 会话销毁: {removed.identity.staff_id}")
        return removed is not None

    def destroy_by_staff(self, staff_id: str) -> int:
        """按员工吊销全部会话（停用/过期账号处理用；策略 0.2 可吊销性的落地）"""
        with self._lock:
            tokens = [t for t, s in self._sessions.items() if s.identity.staff_id == staff_id]
            for t in tokens:
                self._sessions.pop(t, None)
        return len(tokens)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


# 模块级单例（与 capability registry 同款"导入即生效"模式；参数来自 conf/app_config.yaml auth 段）
auth_session_store = AuthSessionStore(
    ttl_seconds=app_config.auth.token_ttl_hours * 3600,
    idle_seconds=app_config.auth.idle_timeout_hours * 3600,
)


class ThreadBindingStore:
    """thread_id → staff_id 绑定表（2.0 上下文策略 1.1）。

    策略：thread_id 必须绑定登录人——同 thread 出现不同 staff_id 即判定异常并拒绝
    （换登录人必须开新 thread）。门店多人共用一台设备时，若换人不换 thread，
    轨迹会串成一个人的对话——既破坏语义又产生越权读取路径。
    内存态（与 AuthSession 同生命周期）；重启后绑定清空，首次提问重新绑定。
    """

    def __init__(self):
        self._bindings: dict[str, str] = {}
        self._lock = threading.Lock()

    def check_and_bind(self, thread_id: str, staff_id: str) -> bool:
        """校验并绑定。已绑定他人 → False（调用方拒绝）；未绑定/同人 → 绑定并返回 True"""
        with self._lock:
            owner = self._bindings.get(thread_id)
            if owner is not None and owner != staff_id:
                logger.warning(f"[auth] thread 归属冲突: {thread_id} 属于 {owner}，被 {staff_id} 访问")
                return False
            if owner is None:
                self._bindings[thread_id] = staff_id
            return True


thread_bindings = ThreadBindingStore()
