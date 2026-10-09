> ⚠️ 域示例说明（2026-10-09）：本文档中的电商/奶茶店业务示例已随项目定位切换（安踏特卖店 2.0）过时；架构条款、接口契约与机制定义仍然有效，以本文档为准。

# 05 · 上下文管理（M5）

> 状态：`final`（已实现并回归通过）　|　上位文档：[00_overview.md](00_overview.md)（final）、[04_capability_routing.md](04_capability_routing.md)（final）
> 修订记录：2026-09-21 初稿；同日按项目所有者确认修订 KV cache 策略——固定前缀 = **system prompt + 工具定义**，对话过程**完整放入**（截断/压缩策略后续优化，本期预留）；2026-10-02 实现完成标记 final。实现期修订三处：① §4#16 的 usage 缓存字段在 01 阶段已预先落地；② 缓存字段读取路径实测修正——`additional_kwargs.usage` 为 None，实际取 `usage_metadata.input_token_details.cache_read`（langchain 标准层，多厂商通用），DeepSeek 原始字段 `response_metadata.token_usage.prompt_cache_hit_tokens` 作兜底；③ summary() 增加命中率聚合。回归证据：eval_quick 10 用例全绿（意图/工具/SQL 均 100%，BUG-01 哨兵用例通过）；同会话第二轮 cache hit 2688 tokens（命中率 35%，命中量为前缀区体积）；`context_management=false` legacy 回归通过
> 职责分界（00 §1.4）：本文管**轨迹的存储与生命周期**（记账、截断、隔离、KV cache 结构）；轨迹之上的**使用策略**（相关历史检索注入）与长期记忆接口在 [06_memory.md](06_memory.md)。
> 改造点基于对 `messages` 全部读写点的核对（intent_classify 已删、extract_keywords L25-26/L52、explain_result L49-51、generate_sql L34-35、simple_answer 已删）。

---

## 1. 目标与边界

### 1.1 做什么

1. **轨迹的统一记账**：`context_store` 提供消息写入/读取的唯一入口，替换散落在各节点里的 `state["messages"]` 直接操作
2. **窗口策略**：v1 对话过程**完整放入提示词**（保 KV cache 前缀一致性，§3.4 注记）；按轮数/token 截断与摘要压缩策略后续优化——参数与钩子接口本期预留
3. **能力间上下文隔离**：共享层只保留"用户消息 + 最终回答"，能力内部中间态永不进入轨迹（当前已满足，本模块固化为不变量 + 测试）
4. **历史供给接口**：`history_provider` —— 需要历史的节点（generate_sql / default_answer）统一从这里取，不再各自拼
5. **KV cache 友好提示词结构改造**（00 红线 #9 登记的技术债）：全部 LLM 提示词重构为"稳定前缀 + 动态内容尾部追加"
6. **checkpointer 存储抽象**：`BaseSessionStore` 接口 + InMemory 实现（v1），Redis/DB 留接口

### 1.2 不做什么

- 不实现摘要压缩的具体算法（钩子接口 + "不启用"缺省，算法后续迭代）
- 不做跨会话的任何记忆（06 的领域）
- 不做历史持久化到 Redis/DB（仅抽象 + InMemory）
- 不引入 LangChain 原生 MessagesPlaceholder 改造（保持 `{"role","content"}` dict 结构，最小扰动）

### 1.3 依赖模块

- 上游：04（default_answer 节点消费历史供给；capability 字段用于隔离标注）
- 下游：06（记忆检索的语料 = 本模块管理的轨迹）

---

## 2. 配置 Schema

### 2.1 `conf/app_config.yaml` 新增 `session:` 段

```yaml
session:                        # [NEW] 上下文管理配置（features.context_management 开关见 00 §3）
  history_max_turns: 10         # [预留] v1 对话完整放入提示词，此参数暂不生效（压缩策略后续优化时启用）
  history_max_tokens: 4000      # [预留] token 预算上限（同上）
  summary_trigger_turns: 20     # [预留] 轨迹超过该轮数触发摘要压缩钩子（v1 仅记录事件不压缩）
  summary_keep_recent_turns: 5  # [预留] 压缩时保留最近轮数
```

> **截断策略状态说明**：v1 对话**完整放入提示词**（保 KV cache 前缀一致性，§3.4）；上述截断/摘要参数全部预留、可配置但不生效——"用成本换缓存命中"的明确取舍（§3.4 注记），后续压缩策略优化时逐项启用。

### 2.2 `AppConfig` dataclass 变更（`app/conf/app_config.py`）

```python
@dataclass
class SessionConfig:
    history_max_turns: int = 10
    history_max_tokens: int = 4000
    summary_trigger_turns: int = 20
    summary_keep_recent_turns: int = 5

@dataclass
class AppConfig:
    # ...原有字段...
    session: SessionConfig = None    # [NEW]
```

---

## 3. 接口与数据结构

### 3.1 轨迹数据结构（保持现状消息格式）

```python
# 轨迹条目 = 现有 state["messages"] 的元素格式（{"role": "user"|"assistant", "content": str}）
# [MODIFY] 新增可选元数据字段（向后兼容：旧条目无此字段）：
TrajectoryEntry = dict  # {"role": str, "content": str,
                        #  "capability": str | None,   # 该条消息产生时所在能力（04 落地后写入）
                        #  "ts": float | None}         # 写入时间戳
```

**存储介质决策（v1，重要）**：轨迹仍以 `state["messages"]` 为存储介质、由 LangGraph checkpointer 持久化——**不引入第二份存储**。理由：(1) 消除双写一致性风险；(2) `关 = 旧行为` 语义天然成立（数据本来就在那里）；(3) checkpointer 已解决跨请求恢复。context_store 因此是**无状态的策略层**（读写都发生在 state 上），而非独立存储——这是对 00 中"context_store 负责'对话语义层'"的具体化。

### 3.2 记账入口（`app/agent/session/context_store.py`）[NEW]

```python
def append_user_message(state: DataAgentState, query: str,
                        capability: str | None = None) -> list[dict]:
    """替代 extract_keywords L25-26 的内联 append；
    返回完整新列表（LangGraph TypedDict 字段为覆盖语义，须整体返回）"""

def append_assistant_message(state: DataAgentState, content: str,
                             capability: str | None = None) -> list[dict]:
    """替代 explain_result L49-51、供 default_answer 使用的写入入口"""

def get_trajectory(state: DataAgentState) -> list[dict]:
    """读全量轨迹（缺省 [] 兜底——现状 query_service 未初始化 messages 的容错保留）"""

def clear_trajectory(state: DataAgentState) -> list[dict]:
    """前端"新对话"场景预留（v1 前端 resetThreadId 即新 thread_id，无需调用）"""

def summarize_trajectory_hook(state: DataAgentState) -> None:
    """[预留接口，v1 空实现] 轮数超 summary_trigger_turns 时被 runner/QueryService 调用；
    摘要算法后续迭代，接口签名锁定：无返回值，直接修改 state 传入的轨迹引用"""
```

### 3.3 历史供给（`app/agent/session/history_provider.py`）[NEW]

```python
def get_conversation_history(
    state: DataAgentState,
    *,
    max_turns: int | None = None,       # [预留] v1 传 None 且不截断（§3.4：对话完整放入）
    max_tokens: int | None = None,
) -> list[dict]:
    """统一历史供给入口（generate_sql / default_answer / 路由提示词均走此函数）：
    v1 返回全量轨迹（旧→新）——截断参数保留签名但不生效，压缩策略优化时启用；
    未来启用时逻辑：倒序取 max_turns 轮 → token 预算粗估截断 → 正序返回
    """

def get_recent_assistant_content(state: DataAgentState, max_items: int = 5) -> str:
    """现状 intent_classify/recap 的"倒序找最近 assistant 消息"模式收敛于此
    （router 提示词的 last_assistant_msg 使用）"""

def capability_view(history: list[dict], capability: str) -> list[dict]:
    """[隔离视图] 按能力过滤轨迹——共享层只含 user/assistant 消息（本就如此，函数用于
    固化不变量 + 06 记忆检索的语料过滤）；capability 中间态数据（retrieved_* 等）不在轨迹中，
    本函数是防御性实现 + 单测载体"""
```

### 3.4 KV cache 友好提示词结构（核心改造）

**现状问题**（01 §3.4 登记的技术债）：各节点把动态内容混入 prompt 中部/前部，导致每次请求发给厂商的 prompt 前缀不稳定，隐式前缀缓存无法命中。

**目标结构**（全部 5 个含 prompt 的链路统一，**前缀策略已经项目所有者确认**）：

```
[固定前缀区]  system prompt（简洁人设+规则）+ 工具定义 —— 逐字面恒定
[对话区]      对话过程（历史，旧→新）—— v1 暂定【完整放入，不做截断】
[当前输入区]  本次 query / SQL / error 等本次请求特有的动态内容 —— 永远最末尾
```

> ⚠️ **明确注记**：对话过程 v1 **完整放入提示词**，不做截断与压缩；压缩策略（何时压、压什么、保留什么）后续持续优化，届时只动"对话区"的处理逻辑，固定前缀区不受影响。长会话的 token 成本因此上升，这是**用成本换缓存命中的明确取舍**（DeepSeek 缓存命中输入价 0.02 vs 未命中 1.0 元/百万 token，长对话下缓存收益远大于全量放入的代价）。

**固定前缀内容（简洁版草案，编码时落盘为 `prompts/system_prompt.prompt` + 常量）**：

```
【system prompt】
你是电商数据助手。你可以调用 dataquery_search 工具查询电商数仓
（表：dim_region/dim_customer/dim_product/dim_date/fact_order）。
规则：只执行只读查询；回答基于查询结果，不编造数据；
无法回答时说明原因并建议用户换个问法。

【工具定义】
tools = [{
  "type": "function",
  "function": {
    "name": "dataquery_search",
    "description": "查询电商数仓：输入自然语言数据问题，返回 SQL 查询结果",
    "parameters": {
      "type": "object",
      "properties": {"query": {"type": "string", "description": "用户的原始数据问题"}},
      "required": ["query"]
    }
  }
}]
```

- v1 工具定义**仅作为固定前缀的组成部分**（让模型感知能力边界），真实 function-calling 执行在 skill 化（04 预留方向）落地；后续新增能力 = 追加工具定义
- **前缀变更纪律**：前缀任何一个字节的变更都会使全部缓存失效——前缀内容的修改视为协议级变更，须经评审并登记 `prefix_version`；能力注册表的 description 若被渲染进 system prompt，同样受此约束

改造范围与要点：

| 链路 | 现状问题 | 改造 |
|------|---------|------|
| `generate_sql` | `conversation_history[-10:]` yaml.dump 混在中部（L34-35、L52）；table_infos 每次请求可能不同但同会话内稳定 | 前缀 → 对话完整放入 → 本次 query 尾部 |
| `correct_sql` | 同上 + error/sql | 前缀 → 对话完整放入 → 本次 sql/error 尾部 |
| `explain_result` | result 数据每次不同（天然无法命中，但前缀仍可稳定） | 前缀固定，动态内容全部尾部 |
| `default_answer`（04 新增） | — | 出生即按新结构 |
| `capability_route`（04 新增） | capabilities 清单来自配置（进程内恒定） | 前缀固定，query 尾部 |
| `recall_*` 三节点扩词 | 仅 query 单变量，前缀本就稳定 | 校验后标记"已符合"，不改 |

**验证口径**：DeepSeek OpenAI 兼容端点的 usage 返回 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 扩展字段——编码时扩展 01 的 `LLMCallRecord` 捕获这两个字段（`usage.extra` 或 additional_kwargs，实测确定取用路径），日志输出缓存命中率；03 报告的费用口径仍按未命中价（01 §7.2），命中数据仅作改造效果佐证。

### 3.5 checkpointer 存储抽象（`app/agent/session/session_store.py`）[NEW]

```python
class BaseSessionStore(ABC):
    """checkpointer 后端抽象。v1 仅适配 LangGraph checkpointer 的装配，不做自研持久化"""

    @abstractmethod
    def build_checkpointer(self): ...

class InMemorySessionStore(BaseSessionStore):
    """InMemorySaver（现状）"""

# RedisSessionStore / SqliteSessionStore：仅类骨架 + NotImplementedError，接口预留
def build_session_store() -> BaseSessionStore:
    """按 app_config 构造；v1 无配置项，恒返回 InMemory（配置项后续迭代）"""

# graph.py [MODIFY]：checkpointer = build_session_store().build_checkpointer()
```

---

## 4. 对现有代码的改动点清单

| # | 文件 | 位置 | 操作 | 内容 |
|---|------|------|------|------|
| 1 | `app/agent/session/__init__.py` | 全文件 | [NEW] | 包声明 |
| 2 | `app/agent/session/context_store.py` | 全文件 | [NEW] | §3.2（记账入口 + 摘要钩子接口） |
| 3 | `app/agent/session/history_provider.py` | 全文件 | [NEW] | §3.3（历史供给 + 隔离视图） |
| 4 | `app/agent/session/session_store.py` | 全文件 | [NEW] | §3.5（checkpointer 抽象 + InMemory 实现） |
| 4b | `app/agent/session/prefix.py` | 全文件 | [NEW] | 固定前缀构建器：渲染 system prompt + 工具定义（§3.4），供 5 个 LLM 链路引用；`prefix_version` 常量 |
| 4c | `prompts/system_prompt.prompt` | 全文件 | [NEW] | §3.4 简洁草案落盘（评审定稿后逐字面冻结） |
| 5 | `conf/app_config.yaml` | 文件尾 | [MODIFY] | 新增 `session:` 段（§2.1） |
| 6 | `app/conf/app_config.py` | AppConfig | [MODIFY] | 新增 `SessionConfig`（§2.2） |
| 7 | `app/agent/nodes/extract_keywords.py` | L25-26、L52 | [MODIFY] | 内联 append → `append_user_message(state, query, capability=state.get("capability"))`（写入带 capability 元数据） |
| 8 | `app/agent/nodes/explain_result.py` | L49-51 | [MODIFY] | 内联 append → `append_assistant_message(...)` |
| 9 | `app/agent/nodes/generate_sql.py` | L34-35（读历史）、L52（chain 输入） | [MODIFY] | `conversation_history[-10:]` → `get_conversation_history(state)`；prompt 重构（§3.4） |
| 10 | `app/agent/nodes/default_answer.py` | 历史读取 | [MODIFY] | 04 新增的节点改走 `get_conversation_history` / `get_recent_assistant_content`（不在 04 落地，随本模块切换） |
| 11 | `app/agent/capabilities/router.py` | last_assistant_msg 获取 | [MODIFY] | 改用 `get_recent_assistant_content` |
| 12 | `app/agent/graph.py` | L167（checkpointer） | [MODIFY] | → `build_session_store().build_checkpointer()` |
| 13 | `prompts/generate_sql.prompt` | 全文件 | [MODIFY] | 重构为稳定前缀结构（§3.4；变量集不变，编排顺序变化） |
| 14 | `prompts/correct_sql.prompt` | 全文件 | [MODIFY] | 同上 |
| 15 | `prompts/explain_result.prompt` | 全文件 | [MODIFY] | 同上 |
| 16 | `app/agent/usage.py` | LLMCallRecord | [MODIFY] | 增加可选字段 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`（§3.4 验证口径；DeepSeek 特有字段，其它 provider 为 None） |
| 17 | `frontend/*` | — | **不变** | 上下文管理纯后端 |
| 18 | `app/agent/memory/`（06 包） | — | **不变** | 记忆检索在 06 实现，消费本模块的 history_provider/context_store |

**明确不改**：问数链路各节点的业务逻辑、`InMemorySaver` 本身、`messages` 的 dict 结构与 LangGraph 覆盖语义、03 评估的 runner（其 thread_id 隔离与本模块天然兼容）。

---

## 5. Feature Flags 开关语义

| 开关 | 开（默认） | 关（= 旧行为） |
|------|-----------|---------------|
| `features.context_management` | 节点历史读取走 `get_conversation_history`（v1 全量，完整放入提示词）；写入走 context_store helpers（带 capability 元数据）；prompt 为 KV cache 友好结构（固定前缀 + 对话尾部） | 节点回退现状：`generate_sql` 直接 `state["messages"][-10:]`、写入节点内联 append、prompt 维持原结构——**cache 命中收益随之消失，但行为正确性不变** |

- 本模块消费 `context_management` 一个 Agent 行为开关
- `summary_trigger_turns` 钩子在 v1 即使开关开启也不实际压缩（仅记录 debug 日志），避免摘要引入结果不确定性污染 baseline

---

## 6. 验收标准

1. **多轮对话回归**：同 thread_id 连续 3 轮问数 → 第 3 轮 generate_sql 的提示词包含前两轮历史（受 max_turns 截断）；不同 thread_id 互相不可见（会话隔离）
2. **截断参数预留验证**：`history_max_turns: 2` 配置后**行为不变**（v1 对话完整放入、截断参数不生效）——验证预留参数不会意外截断；压缩策略优化启用时本条将改写
3. **隔离不变量**：单测断言——任何能力运行后，轨迹中只有 `role ∈ {user, assistant}` 的条目；retrieved_*/table_infos 等中间态零泄漏
4. **写入元数据**：04 落地后新写入的条目带 `capability` 与 `ts` 字段；旧轨迹（无元数据）读取不报错
5. **KV cache 效果**：同 thread_id 连续两轮问数 → 第二轮日志出现 `prompt_cache_hit_tokens > 0`（改造前该值恒 0）；跨 thread_id 首轮 hit=0（前缀不同会话共享部分——同一模板前缀理论可跨会话命中，实测记录）
6. **开关关闭回归**：`context_management=false` 后全链路行为与改造前一致（对比改造前后的同一 query 输出结构），且无 provider 相关调用
7. **checkpointer 抽象**：`build_session_store()` 返回 InMemory 实现，graph 编译与运行不变；`SqliteSessionStore` 调用抛 `NotImplementedError`（接口存在性验证）
8. **评估兼容**：03 runner 的逐用例 thread_id 隔离、`expected` 对照流程不受影响（runner 不感知本模块存在）
9. **提示词回归**：重构后 5 个 prompt 的变量集合与 04/01 文档定义一致；`npm run lint` / python 侧无引用错误
