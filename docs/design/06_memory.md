# 06 · 记忆管理（M6）

> 状态：`final`（已实现并回归通过）　|　上位文档：[00_overview.md](00_overview.md)（final）、[05_context_management.md](05_context_management.md)（final）
> 记忆策略三要素（**已经项目所有者确认**）：①评估体系含"基础回忆"；②存放位置 = 轨迹 + 长期记忆；③存储结构 = Simple Notes + Advanced JSON Cards。
> Agent 定位：**管家角色**——记忆的价值不止"答对"，更在于支撑主动性（主动使用已知偏好与关系）。
> 修订记录：初稿；2026-10-02 §3.3 修订——正则预筛废弃（语义盲区/职责重复/归一化缺失，项目所有者确认），提取收敛为单通道 LLM 上下文压缩（四条格式硬约束：原子化/规范化/值保真/确定性），验收标准 3 同步改写；2026-10-03 实现完成标记 final。实现期修订：① §3.2 search 回归 MemoryStore 接口（评审确认——换 Qdrant 后端 retriever 零改动）+ numpy 向量化余弦（维度守卫/模长预计算/零向量防御）；② §3.5 指标分层 v1.1——recall 拆为 retrieval_rate（检索层，确定性，暴露写入端漏向量 bug）与 recall_accuracy（输出层，LLM flaky 维度）+ 值匹配数字边界防御；③ 阈值校准 0.80→0.60（bge-small-zh 实测回忆对 0.7535 被漏）；④ §3.3 提取输入 = 当前轮 query+answer（每轮提取，不读全量轨迹）；每轮运行都提取（answer 可空）；LLM 失败跳过不重试。回归证据：eval_memory 3 用例——store_rate/retrieval_rate/recall_accuracy/persistence 全 1.0（support=2），重复陈述去重、纯查询零提取、偏好卡分流均验证通过

---

## 1. 目标与边界

### 1.1 做什么

1. **长期记忆体系**：跨会话、跨实例的持久化记忆——**Simple Notes**（原子事实）+ **Advanced JSON Cards**（知识卡片）双结构
2. **记忆提取**：每次 agent 运行结束后，从用户输入中提取可记忆信息（会员号/邮箱/偏好/人物关系等）写入记忆库
3. **记忆检索注入**：新请求到来时按相关性检索记忆，注入提示词**动态尾部**（固定前缀不动，KV cache 约束）
4. **基础回忆评估**：评估体系新增"基础回忆"维度——agent 能否准确存储并检索用户直接提供的结构化、无歧义信息（如"我的会员号是123456"）
5. **为状态栏做准备**：Simple Notes 是后续上下文管理加入"状态栏"（提示词内固定展示用户已知事实摘要）的数据基础

### 1.2 不做什么

- 不做摘要压缩与记忆遗忘/衰减策略（后续迭代）
- 不做知识图谱/多跳推理（Cards 只记录关系，不做图查询）
- 不做自动的卡片聚合维护（v1 卡片由提取直接产出；合并/更新策略后续迭代）
- **不做多用户隔离**：v1 教学场景单用户，记忆库全局共享；`user_id` 维度在数据结构中预留字段，多用户体系另立计划
- 不改 05 的轨迹机制与固定前缀

### 1.3 与 05 的职责分界

| 层 | 归属 | 内容 |
|----|------|------|
| 轨迹（05） | 上下文管理 | 单次会话（thread_id）的完整历史，跨多次 agent 运行持续追加，会话内完整注入提示词（05 §3.4），随 checkpointer 存活 |
| **长期记忆（本文）** | 记忆管理 | **跨会话、跨实例**的持久化存储——会话结束、进程重启后仍在；Simple Notes + Advanced JSON Cards |

> 短期记忆的原始设计（"会话内相关历史检索注入"）已被 05 的轨迹完整注入**吸收**——运行内历史全部可见，无需再检索。因此 `features.memory.short_term` 开关 v1 无独立行为（保留配置位防断裂），本模块聚焦 `long_term`。

---

## 2. 配置 Schema

### 2.1 `conf/app_config.yaml` 的 `memory:` 段（扩展 00 §5.1）

```yaml
memory:                          # [MODIFY] 在 features.memory 开关之外新增运行配置
  store_backend: json_file       # v1: json_file（data/memory/ 目录）；预留 sqlite/qdrant
  store_path: data/memory        # notes.json / cards.json 落盘目录
  extract_after_run: true        # 每次 agent 运行结束后执行记忆提取
  extraction_provider: deepseek  # 提取用 LLM（与分类同理：解耦于用户选择的生成模型）
  retrieval_top_k: 5             # 注入提示词的记忆条数上限（notes+cards 合计）
  similarity_threshold: 0.60     # 检索相似度阈值（复用 embedding 能力；bge-small-zh 校准值——
                                 # 相关非改写短文本对典型 0.6~0.8，0.80 会漏掉回忆对，实测 0.7535）
```

### 2.2 `AppConfig` dataclass（`app/conf/app_config.py`）

```python
@dataclass
class MemoryConfig:
    store_backend: str = "json_file"
    store_path: str = "data/memory"
    extract_after_run: bool = True
    extraction_provider: str = "deepseek"
    retrieval_top_k: int = 5
    similarity_threshold: float = 0.80

@dataclass
class AppConfig:
    # ...原有字段...
    memory: MemoryConfig = None    # [NEW]
```

---

## 3. 接口与数据结构

### 3.1 双存储结构（核心设计，**已经项目所有者确认**）

```python
@dataclass
class SimpleNote:
    """Simple Note：最小、不可再分的原子事实。
    优点：极低开销（一行一事实）；缺点：丢失信息关联性。
    用途：(1) 承接大量但非关键的日常信息；(2) 为上下文管理加入"状态栏"做准备"""
    id: str                  # uuid
    content: str             # 原子事实，如 "用户会员号是123456"
    ts: float
    source_thread_id: str    # 溯源（哪个会话提供的）

@dataclass
class MemoryCard:
    """Advanced JSON Card：从信息存储升级到知识管理。
    每张卡片 = 事实 + 叙事背景 + 主体身份 + 与用户的关系。
    用途：关键且少量的数据（用户偏好、关键人物关系），支撑管家式主动服务"""
    id: str
    subject: str             # 主体身份，如 "用户本人" / "用户的母亲" / "常买品牌：某品牌"
    relation_to_user: str    # 与用户的关系及服务含义，如 "会员身份 → 查询时优先会员价口径"
    facts: list[str]         # 该卡片下的事实列表
    narrative: str           # 叙事背景：这条知识怎么来的、agent 应如何主动使用
    ts: float
    updated_at: float | None
    source_thread_id: str
```

**分流规则（写入时判定）**：关键且少量（偏好、人物关系、会员/身份类）→ MemoryCard；大量非关键（临时提及的地址、一次性的事实）→ SimpleNote。判定由提取 LLM 在输出中直接给出（prompt 中写明分流标准）。

### 3.2 存储抽象（`app/agent/memory/store.py`）[NEW]

```python
class MemoryStore(ABC):
    def save_note(self, note: SimpleNote) -> None: ...
    def save_card(self, card: MemoryCard) -> None: ...
    def all_notes(self) -> list[SimpleNote]: ...
    def all_cards(self) -> list[MemoryCard]: ...
    def search(self, query_vector: list[float], top_k: int) -> list[SimpleNote | MemoryCard]:
        """向量检索（语义相关）；供检索注入使用"""

class JsonFileMemoryStore(MemoryStore):
    """v1 实现：data/memory/notes.json + cards.json，原子写（tmp+rename）；
    向量与条目同文件缓存（note 内容的 embedding 向量随写随存，避免重启重算）"""

class SqlMemoryStore(QdrantMemoryStore):
    """仅类骨架 + NotImplementedError（接口预留，对应 store_backend 配置位）"""

def build_memory_store() -> MemoryStore:
    """按 app_config.memory.store_backend 构造"""
```

### 3.3 记忆提取（`app/agent/memory/extractor.py`）[NEW]

```python
async def extract_memories(query: str, answer: str, store: MemoryStore,
                           llm, tracker, embedding_client,
                           thread_id: str) -> dict:
    """agent 运行结束后调用（QueryService finally 阶段，异步不阻塞响应已发送）。
    单通道设计（2026-10-02 修订，废弃正则预筛）：
    1. LLM 上下文压缩（memory_extract.prompt）：读当前轮 query+answer，
       压缩为规范化事实语料——prompt 四条硬约束：原子化（一事实一条）/规范化（第三人称陈述，去口语）/
       值保真（号码邮箱逐字符保真）/只提取用户主动提供的确定性信息（查询类数字禁止入库）
    2. 内容级去重：写入前对照 store 已有内容集合，同内容跳过（重复陈述不累积）
    3. 条目 embedding 随写随存（vector 字段，失败置 None 不阻断）
    4. 提取/写入/向量化失败只打 warning，绝不影响主链路；LLM 不可用时本轮跳过
      （已存记忆的检索不受影响——检索不依赖提取通道）
    返回 {"notes_added": int, "cards_added": int}（供日志与评估）
    """
```

> **正则预筛废弃理由（2026-10-02，项目所有者确认）**：① 语义盲区——正则匹配字符模式而非"陈述行为"，"查订单123456的物流"会被误存为永久错误记忆（错误写入比漏写严重得多）；② 职责重复——LLM 通道本就通读上下文，覆盖正则全部能力且零维护成本泛化新事实类型；③ 归一化缺失——正则只做字符捕获，做不了"金卡→黄金级会员"式的规范化压缩，而归一化才是事实可检索可注入的前提。"LLM 挂了也能存"的保底价值不成立：LLM 不可用时主对话已不可用，且已存记忆的检索不受影响。

`prompts/memory_extract.prompt` [NEW]：管家定位 + 分流标准（关键少量→card，大量非关键→note）+ 四条格式硬约束（原子化/规范化/值保真/确定性）+ 输出 JSON Schema。

### 3.4 记忆检索注入（`app/agent/memory/retriever.py`）[NEW]

```python
async def retrieve_memory_block(query: str, store: MemoryStore,
                                embedding_client, top_k: int) -> str:
    """按 query 语义检索相关记忆，渲染为注入块（纯文本）：
    【用户已知信息】
    - 会员号是123456（来源：用户本人提供）
    - 偏好：偏好华北地区数据视角（card.subject: 用户本人）
    无相关记忆 → 返回 ""（不注入空块）
    """
```

**注入点与 KV cache 纪律**：注入块只允许出现在提示词**动态尾部**（05 §3.4 三段式结构的当前输入区之前）——固定前缀永不因记忆内容变化。消费节点：`default_answer` 与 `generate_sql` 的 prompt 增加 `{memory_block}` 变量（无记忆时传空串，prompt 结构不变）。

> 注入块属于动态内容，**会破坏该次请求的缓存前缀延续**——权衡：记忆命中时上下文价值 > 缓存收益；无相关记忆时传空串，缓存照常命中。此取舍写入 §7。

### 3.5 评估扩展——基础回忆（`app/evaluation/memory_metrics.py`）[NEW]

```python
def compute_memory_metrics(cases_result: list[dict]) -> dict:
    """基础回忆三指标（00 §1.1 确认的评估要求）：
    - store_rate      存储成功率：提供事实后，记忆库中可检索到该事实的用例占比
    - recall_accuracy 回忆准确率：跨会话追问时，回答包含正确事实值的用例占比
    - persistence     跨实例存活：runner 重启场景下（模拟：分两次进程跑）回忆仍正确
    """
```

**评测集扩展**（03 数据集格式追加可选字段，向后兼容）：

```json
{
  "id": "mem_case_001",
  "enabled": true,
  "query": "我的会员号是多少",
  "memory_setup": {
    "setup_runs": ["我的会员号是123456"],   # 先行运行：注入事实（可多条）
    "expected_recall": ["123456"],           # 回答中必须出现的值
    "expect_note": true                      # 断言 SimpleNote 已落库
  }
}
```

runner 执行顺序：先跑 `setup_runs`（各自 ainvoke，等提取完成）→ 校验 store → 清空对话状态（新 thread_id，**跨会话**）→ 跑 probe query → 从 `final_state` 的 explanation/result 中匹配 `expected_recall`。03 的 runner 增加对 `memory_setup` 的处理分支（03 文档登记为 06 驱动的扩展）。

---

## 4. 对现有代码的改动点清单

| # | 文件 | 操作 | 内容 |
|---|------|------|------|
| 1 | `app/agent/memory/__init__.py` | [NEW] | 包声明 |
| 2 | `app/agent/memory/store.py` | [NEW] | §3.2（抽象 + JsonFile 实现） |
| 3 | `app/agent/memory/extractor.py` | [NEW] | §3.3（正则预筛 + LLM 提取） |
| 4 | `app/agent/memory/retriever.py` | [NEW] | §3.4（检索注入块渲染） |
| 5 | `prompts/memory_extract.prompt` | [NEW] | 提取提示词（管家定位 + 分流标准） |
| 6 | `data/memory/.gitkeep` | [NEW] | 记忆库落盘目录（notes/cards JSON 入 .gitignore） |
| 7 | `app/conf/app_config.py` + `conf/app_config.yaml` | [MODIFY] | `MemoryConfig` + `memory:` 段（§2） |
| 8 | `app/services/query_service.py` | finally 块 | [MODIFY] | `extract_after_run` 开启且 `long_term` 开启时调用 `extract_memories`（llm=create_llm(extraction_provider, tracker)） |
| 9 | `app/agent/nodes/default_answer.py` | prompt 组装 | [MODIFY] | 增加 `{memory_block}` 变量（retriever 注入） |
| 10 | `app/agent/nodes/generate_sql.py` | prompt 组装 | [MODIFY] | 同上（如会员号过滤条件个性化） |
| 11 | `app/agent/context.py` | [MODIFY] | `DataAgentContext` 新增 `memory_store: MemoryStore`（retriever 消费） |
| 12 | `app/api/lifespan.py` + `dependencies.py` | [MODIFY] | lifespan init/build memory_store（json_file 无需连接，仅加载）；dependencies 注入 |
| 13 | `app/evaluation/memory_metrics.py` | [NEW] | §3.5 |
| 14 | `app/evaluation/runner.py` | [MODIFY] | 处理 `memory_setup` 分支（setup runs → store 校验 → 新 thread probe） |
| 15 | `.gitignore` | [MODIFY] | `data/memory/`（用户数据不入库） |
| 16 | `pyproject.toml` | **不变** | 无新依赖（json_file + 正则 + 既有 embedding） |

**明确不改**：05 的轨迹机制、固定前缀（prefix.py / system_prompt.prompt 零改动）、04 的路由三级通道、问数链路节点业务逻辑。

---

## 5. Feature Flags 开关语义

| 开关 | 开 | 关（= 旧行为） |
|------|-----|---------------|
| `features.memory.long_term`（默认 **false**） | 运行后提取 + 检索注入 + 基础回忆评估可用 | 不提取、不注入（`{memory_block}` 恒空串）、评测跳过 memory 用例——05 的轨迹行为完全不变 |
| `features.memory.short_term` | **v1 无独立行为**（原"会话内检索注入"被 05 轨迹完整注入吸收，00 §5 开关表已注记） | 同左 |
| `features.evaluation.tool_metrics` 等 | 03 定义不变；`memory_metrics` 是否纳入 03 的 `features.evaluation.*` 开关体系？——**纳入**：新增 `features.evaluation.memory_metrics: true`（00 开关表追加一行） | 关闭时报告 memory 区 `disabled` |

- `long_term` 默认 false 的理由：记忆写入是**有副作用的持久化操作**，与其它"只读增强"开关不同；教学演示按需打开
- 提取调用的成本计入 tracker（by_stage 归属 "memory_extract" 环节，03 成本报告可见）

---

## 6. 验收标准

1. **基础回忆闭环（核心验收）**：跨两次进程——进程 A 发"我的会员号是123456" → `data/memory/notes.json` 出现该 SimpleNote → 进程 B（重启，模拟跨实例）发"我的会员号是多少"（**新 thread_id**）→ 回答包含 "123456"
2. **提取分流**："我现在用的是金牌会员，比较看重华东仓的发货速度" → 产出 MemoryCard（subject=用户本人，relation 含会员等级与发货偏好）；"帮我查一下订单" → 零提取（无可记忆信息不硬造）
3. **提取失败容错（v1.1 修订，原"正则预筛"已废弃）**：停掉 LLM 后发"我的邮箱是abc@x.com" → `extract_memories` 不抛异常、返回计数 0、主链路不受影响；**已存记忆的检索不受提取通道故障影响**（检索不依赖提取 LLM）。附带确认的决策：提取输入 = 当前轮 query+answer（每轮都有提取机会，不读全量轨迹）；每轮运行都提取（answer 可为空串）；LLM 失败跳过 + warning 不重试
4. **KV cache 纪律**：注入记忆的请求日志显示前缀部分 cache 命中数与无记忆请求一致（注入只发生在尾部动态区）；无相关记忆时传空串、缓存命中与无记忆请求完全一致
5. **开关关闭**：`memory.long_term=false` → 无提取调用（by_stage 无 memory_extract）、`{memory_block}` 恒空、评测 memory 用例跳过
6. **注入上限**：构造 20 条相关记忆 → 注入块最多 `retrieval_top_k`(5) 条
7. **基础回忆评估**：03 评测跑含 `memory_setup` 的用例 → memory_metrics 三指标（store_rate/recall_accuracy/persistence）正常产出；`features.evaluation.memory_metrics=false` 时报告 disabled
8. **落盘与隔离**：`data/memory/*.json` 在 .gitignore 中；JSON 原子写（中途 kill 进程不产生半截文件）
9. **成本可见**：tracker by_stage 出现 `memory_extract` 环节的 calls/tokens（提取成本透明）

---

## 7. 风险与备注

1. **记忆注入 vs 缓存的权衡**（§3.4 注记）：有相关记忆的请求会损失部分缓存延续（记忆块插入在尾部、其后还有本次 query，前缀到记忆块为止仍命中）；无记忆请求零损失。v1 接受此不对称，量化留给 03 报告
2. **提取误存**：用户随口说的非事实（"大概 123 吧"）可能被提取——prompt 中要求只提取明确、用户主动提供的确定性信息；`notes.json` 支持人工清理（教学场景可接受）
3. **Cards 的主动性边界**：管家式主动服务（如主动提"上次您说偏好华东仓"）依赖注入块的措辞，prompt 中约束"可参考用户已知信息，但不要生硬复述"——效果属提示词迭代范畴
4. **单用户假设**：`user_id` 字段已在数据结构预留注释，多用户/多租户另立计划；当前 store 全局共享，评测与演示勿混入敏感信息
5. **json_file 的并发写**：QueryService finally 串行调用 + 原子写（tmp+rename），教学并发规模下无竞争问题；多实例部署时须换 Sql/Qdrant 后端（接口已预留）
