"""
记忆存储（06 文档 §3.1/§3.2）

双结构（项目所有者确认）：
- SimpleNote：最小原子事实，一行一条，承接大量非关键信息 + 为"状态栏"做数据准备
- MemoryCard：知识卡片 = 主体身份 + 与用户关系 + 事实列表 + 叙事背景，承接关键少量信息

v1 介质：data/memory/notes.json + cards.json（原子写 tmp+rename）；
向量随写随存（条目内容的 embedding 缓存在同文件，避免重启重算）。
⚠️ v1 单用户假设（00 §1.4）：记忆库全局共享，user_id 维度预留未启用，
评测与演示勿在记忆中放入敏感信息（06 §7.4）。

规模化路径（评审确认 2026-10-02）：search 在存储接口中（06 §3.2 原案），
条目上万时实现 QdrantMemoryStore（memory collection + ANN 检索）即可，
retriever 与调用方零改动。v1 不直上 Qdrant 的理由：①记忆是默认关闭的可选功能，
不应硬依赖向量库存活（降耦）；②json_file + numpy 向量化在万条以内 <5ms，边界清晰。
"""
import json
import os
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from app.conf.app_config import app_config
from app.core.log import logger


def new_id() -> str:
    """条目唯一标识（uuid4，提取器与调用方共用）"""
    return str(uuid.uuid4())


@dataclass
class SimpleNote:
    """Simple Note：最小、不可再分的原子事实。
    优点：极低开销（一行一事实）；缺点：丢失信息关联性。
    用途：(1) 承接大量但非关键的日常信息；(2) 为上下文管理加入"状态栏"做准备"""

    id: str
    content: str                          # 原子事实，如 "用户会员号是123456"
    ts: float
    source_thread_id: str                 # 溯源：哪个会话提供的
    vector: list[float] | None = None     # 内容 embedding（随写随存，检索用）


@dataclass
class MemoryCard:
    """Advanced JSON Card：从信息存储升级到知识管理。
    每张卡片 = 事实 + 叙事背景 + 主体身份 + 与用户的关系。
    用途：关键且少量的数据（用户偏好、关键人物关系），支撑管家式主动服务"""

    id: str
    subject: str                          # 主体身份，如 "用户本人" / "用户的母亲"
    relation_to_user: str                 # 与用户的关系及服务含义
    facts: list                           # 该卡片下的事实列表
    narrative: str                        # 叙事背景：知识怎么来、agent 如何主动使用
    ts: float
    source_thread_id: str
    updated_at: float | None = None
    vector: list[float] | None = None


class MemoryStore(ABC):
    """记忆存储抽象——存取 + 向量检索（06 §3.2 原案）。
    接口刻意收窄为「存取 + search」：换 Qdrant 后端时只需实现本接口的 search（ANN），
    retriever 与调用方零改动。store 不感知 embedding 客户端（向量由调用方算好随条目存取）"""

    @abstractmethod
    def save_note(self, note: SimpleNote) -> None: ...

    @abstractmethod
    def save_card(self, card: MemoryCard) -> None: ...

    @abstractmethod
    def all_notes(self) -> list:
        ...

    @abstractmethod
    def all_cards(self) -> list:
        ...

    @abstractmethod
    def search(self, query_vector: list, top_k: int) -> list:
        """向量检索：返回 [(条目, 相似度分数)]，按分数降序、最多 top_k 条。
        相似度阈值过滤由调用方（retriever）执行——阈值是业务参数，不属于存储层"""


class JsonFileMemoryStore(MemoryStore):
    """v1 实现：data/memory/notes.json + cards.json。
    - 原子写：同目录临时文件 + os.replace（同分区 rename 原子，进程 kill 不留半截文件，验收标准 8）
    - 全量读写（教学场景条目量级 < 千条，O(n) 可接受；规模化时换 Sql/Qdrant 后端）
    - save 按 id 去重更新：同 id 重写即更新（为卡片合并预留的最小语义，v1 不做主动聚合）"""

    def __init__(self, store_path: str):
        self.dir = Path(store_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.notes_file = self.dir / "notes.json"
        self.cards_file = self.dir / "cards.json"

    @staticmethod
    def _load(file: Path) -> list:
        """读 JSON 数组；文件不存在/为空 → 空列表（首次启动容错）"""
        if not file.is_file():
            return []
        text = file.read_text(encoding="utf-8").strip()
        if not text:
            return []
        return json.loads(text)

    def _save_atomic(self, file: Path, items: list) -> None:
        """原子写：同目录临时文件 + os.replace"""
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            os.replace(tmp, file)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def save_note(self, note: SimpleNote) -> None:
        items = [n for n in self._load(self.notes_file) if n.get("id") != note.id]
        items.append(asdict(note))
        self._save_atomic(self.notes_file, items)
        logger.info(f"[memory] note saved: {note.content[:50]}")

    def save_card(self, card: MemoryCard) -> None:
        items = [c for c in self._load(self.cards_file) if c.get("id") != card.id]
        items.append(asdict(card))
        self._save_atomic(self.cards_file, items)
        logger.info(f"[memory] card saved: {card.subject} ({len(card.facts)} facts)")

    def all_notes(self) -> list:
        return [SimpleNote(**n) for n in self._load(self.notes_file)]

    def all_cards(self) -> list:
        return [MemoryCard(**c) for c in self._load(self.cards_file)]

    def search(self, query_vector: list, top_k: int) -> list:
        """numpy 向量化余弦检索（万条以内 <5ms；规模化路径 = Qdrant 后端，接口已预留）。
        维度守卫：与 query 不同维的向量（换 embedding 模型的遗留条目）整条跳过——
        zip 截断会静默算错分数（评审意见 1），宁可丢一条历史记忆也不返回错误相似度"""
        items: list = []
        for n in self.all_notes():
            if n.vector:
                items.append((n, n.vector))
        for c in self.all_cards():
            if c.vector:
                items.append((c, c.vector))
        if not items:
            return []

        # 维度守卫：维度不符的向量整条剔除
        dim = len(query_vector)
        valid = [(item, vec) for item, vec in items if len(vec) == dim]
        dropped = len(items) - len(valid)
        if dropped:
            logger.warning(f"[memory] {dropped} 条记忆向量维度不符（疑似换过 embedding 模型），检索时跳过")
        if not valid:
            return []

        # numpy 向量化余弦：query 模长只算一次（评审意见 2），零向量防御除零
        mat = np.asarray([vec for _, vec in valid], dtype=np.float32)
        q = np.asarray(query_vector, dtype=np.float32)
        q_norm = float(np.linalg.norm(q))
        if q_norm == 0.0:
            return []
        row_norms = np.linalg.norm(mat, axis=1)
        row_norms[row_norms == 0.0] = 1e-12
        scores = (mat @ q) / (row_norms * q_norm)

        ranked = sorted(zip([item for item, _ in valid], scores.tolist()),
                        key=lambda x: -x[1])
        return ranked[:top_k]


class SqlMemoryStore(MemoryStore):
    """[接口预留] 对应 store_backend: sqlite 配置位——表结构与序列化方案后续迭代定稿"""

    def save_note(self, note: SimpleNote) -> None:
        raise NotImplementedError("SqlMemoryStore 尚未实现（06 §3.2 接口预留）")

    def save_card(self, card: MemoryCard) -> None:
        raise NotImplementedError("SqlMemoryStore 尚未实现（06 §3.2 接口预留）")

    def all_notes(self) -> list:
        raise NotImplementedError("SqlMemoryStore 尚未实现（06 §3.2 接口预留）")

    def all_cards(self) -> list:
        raise NotImplementedError("SqlMemoryStore 尚未实现（06 §3.2 接口预留）")

    def search(self, query_vector: list, top_k: int) -> list:
        raise NotImplementedError("SqlMemoryStore 尚未实现（06 §3.2 接口预留）")


def build_memory_store() -> MemoryStore:
    """按 app_config.memory.store_backend 构造（与 05 session_store 同款工厂模式）"""
    backend = app_config.memory.store_backend
    if backend == "json_file":
        return JsonFileMemoryStore(app_config.memory.store_path)
    raise NotImplementedError(f"记忆后端 '{backend}' 尚未实现（06 §3.2 接口预留）")
