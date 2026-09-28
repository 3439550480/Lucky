"""
能力注册表（04 文档 §2.1/§3.1）

链路位置：graph.py 组装时调用 load_capabilities() 完成加载与 fail-fast 校验；
route_capability 节点运行时经 registry.match_rules（规则快路径）、
build_llm_context（LLM 分类提示词拼装）消费它。
单一事实源：capability_config.yaml —— 前端芯片（02）、评估工具命名（03）都以此为准。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from app.core.log import logger

# registry.py 位于 app/agent/capabilities/（三层深），parents[3] 才是项目根
# （对比：app_config.py 在 app/conf/ 两层深，用 parents[2]——层级不同，勿照抄）
CONFIG_PATH = Path(__file__).resolve().parents[3] / "conf" / "capability_config.yaml"


@dataclass
class Capability:
    """一个能力条目：yaml capabilities[] 的内存形态"""
    name: str
    description: str
    entry: str                        # 图中入口节点名
    selectable: bool                  # 前端芯片可见性（tier-0）
    rules: list[re.Pattern] = field(default_factory=list)   # 编译后的规则快路径
    examples: list[str] = field(default_factory=list)       # LLM 示例 + embedding 语料
    example_vectors: list | None = None  # [下一单位启用] 安全网语料向量缓存


class CapabilityRegistry:
    """注册表内存形态：按配置顺序保持能力列表，提供规则匹配与提示词拼装"""

    def __init__(self, capabilities: list[Capability],
                 default_capability: str, error_capability: str,
                 classifier_provider: str, embedding_threshold: float):
        self.capabilities: dict[str, Capability] = {c.name: c for c in capabilities}
        self._order: list[str] = [c.name for c in capabilities]   # 规则匹配按配置顺序
        self.default_capability = default_capability
        self.error_capability = error_capability
        self.classifier_provider = classifier_provider
        self.embedding_threshold = embedding_threshold

    def selectable_capabilities(self) -> list[Capability]:
        """前端芯片数据源（02 文档 /api/capabilities 消费）：只返回 selectable=true 的能力"""
        return [self.capabilities[name] for name in self._order
                if self.capabilities[name].selectable]

    def match_rules(self, query: str) -> Optional[str]:
        """规则快路径：按配置顺序逐能力尝试 rules，第一个命中的能力名；无命中 None。
        高确定性规则前置是省 token 的关键 —— 命中即分发，0 token 进入链路"""
        for name in self._order:
            for pattern in self.capabilities[name].rules:
                if pattern.search(query):
                    return name
        return None

    def build_llm_context(self) -> str:
        """把能力清单渲染为 LLM 分类的提示词片段（配置注入式，04 §3.2）。
        进程内恒定 —— 这也是 KV cache 固定前缀的一部分（05 §3.4）"""
        lines = []
        for i, name in enumerate(self._order, start=1):
            cap = self.capabilities[name]
            examples = " / ".join(cap.examples) if cap.examples else ""
            lines.append(f"{i}. {name}：{cap.description}" + (f"（例：{examples}）" if examples else ""))
        return "\n".join(lines)

    async def ensure_vectors(self, embedding_client) -> None:
        """确保各能力 examples 的向量已就绪（惰性初始化，首次路由时调用）。
        embedding_client 由 runtime.context 传入（与节点同一实例，保证向量空间一致）；
        规模仅数条/能力，内存缓存即可 —— Qdrant collection 方案留作 examples 规模化后的演进"""
        # step 1: 已就绪则跳过（幂等）—— 每次请求都会经过路由，向量只算一次
        if all(cap.example_vectors is not None for cap in self.capabilities.values()):
            return
        # step 2: 收集全部 examples 统一批量向量化 —— 一次网络往返优于逐条调用
        # （与 06 记忆检索共用同一 embedding 实例 → 同一向量空间，相似度才有意义）
        all_texts, spans = [], []
        for name in self._order:
            cap = self.capabilities[name]
            spans.append((name, len(all_texts), len(all_texts) + len(cap.examples)))
            all_texts.extend(cap.examples)
        # step 3: 批量计算并按能力切分回填 —— 每条 example 一个向量
        vectors = await embedding_client.aembed_documents(all_texts)
        for name, start, end in spans:
            self.capabilities[name].example_vectors = vectors[start:end]

    async def match_embedding(self, query_vector: list[float]) -> Optional[tuple[str, float]]:
        """查询向量与各能力语料做余弦相似度，返回 (能力名, 最高分)；无超阈值命中 None。
        评测与调优依赖：命中的阈值就是 routing.embedding_threshold（0.85 起步）"""
        # step 1: 语料未就绪 → 返回 None（上层会落入 LLM 通道，绝不阻塞路由）
        if not all(cap.example_vectors for cap in self.capabilities.values()):
            return None
        # step 2: 遍历全部语料算余弦相似度 —— 教学规模下暴力遍历即可（<20 条），
        # 不引入 numpy 依赖；纯 Python 实现 = 算法可读，这正是教学项目要的
        best_name, best_score = None, 0.0
        for name in self._order:
            cap = self.capabilities[name]
            for vec in cap.example_vectors or []:
                score = self._cosine(query_vector, vec)
                if score > best_score:
                    best_name, best_score = name, score
        # step 3: 阈值判定 —— 达标才命中，否则交给 LLM 层
        if best_name and best_score >= self.embedding_threshold:
            return best_name, best_score
        return None

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        """余弦相似度（纯 Python）：embedding 已归一化时点积=余弦，但显式实现
        归一化除法让算法对'是否归一化'不敏感 —— 教学上更稳"""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def validate_entries(self, registered_nodes: set[str]) -> None:
        """fail-fast 校验（graph compile 前调用）：
        1. entry 必须是已注册节点
        2. default/error capability 必须在注册表
        3. 注册表非空
        校验失败抛 ValueError，应用启动即失败 —— 配置错误尽早暴露（04 §2.2）"""
        for name in self._order:
            entry = self.capabilities[name].entry
            if entry not in registered_nodes:
                raise ValueError(
                    f"能力 '{name}' 的 entry '{entry}' 不是已注册节点；"
                    f"可用节点：{sorted(registered_nodes)}"
                )
        for key in ("default_capability", "error_capability"):
            if getattr(self, key) not in self.capabilities:
                raise ValueError(f"routing.{key} 指向未注册能力 '{getattr(self, key)}'")


def load_capabilities(path: Path = CONFIG_PATH) -> CapabilityRegistry:
    """加载 yaml → 构造 CapabilityRegistry（含正则编译与结构校验）。
    校验失败抛 ValueError 并带字段定位，不静默跳过（03 数据集同款纪律）"""
    # step 1: 读取 yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    # step 2: 逐条目解析 —— 正则编译失败/缺 name/缺 entry 都在此处报错
    capabilities = []
    for i, item in enumerate(raw.get("capabilities") or []):
        name = item.get("name")
        if not name:
            raise ValueError(f"capability_config 第 {i} 个条目缺少 name")
        capabilities.append(Capability(
            name=name,
            description=item.get("description", ""),
            entry=item.get("entry", ""),
            selectable=bool(item.get("selectable", False)),
            rules=[re.compile(p) for p in item.get("rules", [])],
            examples=list(item.get("examples", [])),
        ))
    if not capabilities:
        raise ValueError("capability_config 未定义任何能力")
    # step 3: 组装 routing 段 —— default/error 必须指向已注册能力（节点校验在
    # validate_entries 中做，那里才有 graph 节点集合）
    routing = raw.get("routing", {})
    registry = CapabilityRegistry(
        capabilities=capabilities,
        default_capability=routing.get("default_capability", "default"),
        error_capability=routing.get("error_capability", "default"),
        classifier_provider=routing.get("classifier_provider", "deepseek"),
        embedding_threshold=float(routing.get("embedding_threshold", 0.85)),
    )
    for key in ("default_capability", "error_capability"):
        if getattr(registry, key) not in registry.capabilities:
            raise ValueError(f"routing.{key} 指向未注册能力 '{getattr(registry, key)}'")
    logger.info(f"能力注册表加载完成: {registry._order}")
    return registry


# 模块级单例（与 app_config 同款"导入即生效"模式）：graph 组装与 QueryService 共享同一实例。
# 导入时即加载并校验 —— 配置错误在应用启动瞬间暴露（fail-fast）。
registry = load_capabilities()
