"""
checkpointer 存储抽象（05 文档 §3.5）

v1 仅装配 InMemorySaver（现状行为），Redis/Sqlite 仅接口骨架；
app_config 无存储配置项前，build_session_store() 恒返回 InMemory。
抽象边界刻意收窄：只负责"给 graph.compile 提供一个 checkpointer"，不做自研持久化。
"""
from abc import ABC, abstractmethod

from langgraph.checkpoint.memory import InMemorySaver


class BaseSessionStore(ABC):
    """checkpointer 后端抽象"""

    @abstractmethod
    def build_checkpointer(self): ...


class InMemorySessionStore(BaseSessionStore):
    """进程内存实现（现状行为；已知限制：进程重启即失、无上限——持久化后续迭代）"""

    def build_checkpointer(self) -> InMemorySaver:
        return InMemorySaver()


class RedisSessionStore(BaseSessionStore):
    """[接口预留] Redis 后端——依赖与序列化方案后续迭代定稿"""

    def build_checkpointer(self):
        raise NotImplementedError("RedisSessionStore 尚未实现（05 §3.5 接口预留）")


class SqliteSessionStore(BaseSessionStore):
    """[接口预留] Sqlite 后端——同上"""

    def build_checkpointer(self):
        raise NotImplementedError("SqliteSessionStore 尚未实现（05 §3.5 接口预留）")


def build_session_store() -> BaseSessionStore:
    """按配置构造存储实现；v1 无配置项，恒返回 InMemory"""
    return InMemorySessionStore()
