> ⚠️ 域示例说明（2026-10-09）：本文档中的电商/奶茶店业务示例已随项目定位切换（安踏特卖店 2.0）过时；架构条款、接口契约与机制定义仍然有效，以本文档为准。

# 04 · 能力路由体系（M4）

> 状态：`draft`（评审中）　|　上位文档：[00_overview.md](00_overview.md)（final）、[01_llm_factory.md](01_llm_factory.md)（final）
> 修订记录：2026-09-21 初稿；同日**路由策略经项目所有者确认后重构**——识别通道定为"规则（高确定性）→ embedding 安全网 → LLM（暂定 deepseek）"四级递进，兜底统一为 default；2026-09-23 新增 **tier-0 用户显式选择**（前端能力芯片、持续选中、selectable 字段、capability_source 来源标记）
> 改造对象 `app/agent/graph.py`（19 节点 + 3 组条件边）已逐行核对（行号见 §4 清单）；03 文档的 `tool_metrics` 依赖本章的 `state["tool_calls"]` 数据源。

---

## 1. 目标与边界

### 1.1 做什么

1. **能力注册表**：`conf/capability_config.yaml` —— v1 只有两个能力 `dataquery` / `default`（00 §5.2 定稿），每个条目含 `entry`（图入口节点）字段；新增能力 = 配置加条目 + 实现处理节点，**路由层零改动**
2. **三级路由**：规则快路径（0 token）→ LLM 分类（配置注入式提示词）→ 兜底（无法归类 → default；LLM 调用失败 → dataquery，与现状降级行为一致）
3. **graph.py 改造**：`intent_classify` 节点被 `route_capability` 取代；`route_by_intent` 五分流被 `route_by_capability` 两分流取代；`default_answer` 新节点吸收 recap/chitchat/help 语义；删除 start_recall 空节点、simple_answer 三节点与注释残留
4. **SSE 协议 capability 字段落地**（00 §4.3）：路由之后的所有事件附加 `capability`
5. **state 扩展**：新增 `capability` / `tool_calls` 字段（后者为 03 工具指标与 skill 化预留的数据源）

### 1.2 不做什么

- **不拆子图**：问数 19 节点链路保持注册在当前 graph 中，能力"入口"以配置中的 `entry` 节点名表达（§7.1 说明理由与演进路径）
- 不实现 skill+工具调用的运行时（tool_calls 在 v1 由路由结果直接写入，是标记而非真实工具执行；skill 化为预留方向）
- 不新增第三个能力
- 不改问数链路 19 节点的内部逻辑（红线）
- 不在本章处理历史供给（default_answer 的历史读取沿用现状 `state["messages"]` 模式，05 接管）

### 1.3 依赖模块

- 上游：01（llm 经 `runtime.context["llm"]` 取用）
- 下游：05（context_store 接管历史供给；capability 已就位才有隔离场景）、03（tool_metrics 读 `tool_calls`）、02（前端展示，无耦合）

---

## 2. 配置 Schema

### 2.1 `conf/capability_config.yaml` 最终形态（在 00 §5.2 基础上补充 `entry` 字段）

```yaml
# 能力注册表：新增能力 = 此文件加一个条目 + 实现一个统一签名的处理节点
capabilities:
  - name: dataquery                   # 电商问数（现有 19 节点链路）
    description: 基于电商数仓的指标查询与SQL问答
    examples: ["上个月GMV多少", "华北地区的复购率", "查询数据：各地区销售额排行", "统计一下近7天的订单量"]
    entry: extract_keywords           # [NEW] 图中入口节点名——路由命中后分发到该节点
    selectable: true                  # [NEW] 前端能力芯片可见（用户可显式选择）
    rules:                            # 规则快路径：只放"高度确定"的明确表述，不放模糊词（误命中代价分析见 §7.3）
      - "数据查询|查询数据|查一下数据"
      - "(帮我|给我)?(统计|查询|查一下).{0,12}(GMV|销售额|订单量|复购率|客单价|转化率|销量)"
  - name: default                     # 默认通用对话（兜底）
    description: 通用助手对话：闲聊、使用帮助、回顾上次结果、无法归类问题的兜底
    examples: ["你好", "你能做什么", "刚才的结果是什么意思", "今天天气不错"]
    entry: default_answer
    selectable: false                 # 兜底能力不出芯片——不选芯片 = 自动识别（含 default 兜底）
    rules:
      - "^(你好|hi|hello|在吗)"

routing:
  default_capability: default         # LLM 分类成功但结果不在注册表时兜底
  error_capability: default           # LLM 调用/解析失败时兜底（通用对话节点向用户致歉并建议重试）
  classifier_provider: deepseek       # 路由分类专用 LLM（暂定 deepseek；后续按评估的精准度+速度指标敲定）
  embedding_threshold: 0.85           # embedding 安全网的命中阈值（余弦相似度，可调）
```

**字段定义**：

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | 能力唯一标识（全小写），state/SSE/评估共用 |
| `description` | ✅ | LLM 分类提示词中展示给模型的能力说明 |
| `examples` | ✅ | 双重用途：LLM 提示词示例 + **embedding 安全网的向量化语料**（见 §3.2） |
| `entry` | ✅ | 该能力在 graph 中的入口节点名（registry 校验其已注册） |
| `selectable` | ✅ | 是否在前端能力芯片中展示（`true`=用户可显式选择；兜底能力恒 false，经项目所有者确认） |
| `rules` | ❌ | **高确定性**正则快路径（如"查询数据""数据查询"）；刻意不放"多少""统计"这类模糊词 |
| `routing.default_capability` | ✅ | 分类结果未知时兜底 |
| `routing.error_capability` | ✅ | LLM 异常兜底（**统一为 default**：由通用对话节点致歉并建议重试） |
| `routing.classifier_provider` | ❌ | 分类专用 LLM 的 provider 名，缺省 `deepseek`；后续按评估结果（精准度+速度）调整 |
| `routing.embedding_threshold` | ❌ | embedding 安全网命中阈值，缺省 0.85 |

### 2.2 加载校验（registry.py 启动时执行）

- `name` 唯一；`entry` 必须存在于 graph 已注册节点集合（启动时校验，失败 fail-fast）；`default_capability`/`error_capability` 必须指向已注册能力；正则可编译
- 校验失败 → 应用启动抛异常（配置错误尽早暴露）

---

## 3. 接口与数据结构

### 3.1 能力注册表（`app/agent/capabilities/registry.py`）[NEW]

```python
@dataclass
class Capability:
    name: str
    description: str
    entry: str
    rules: list[re.Pattern]
    examples: list[str]
    example_vectors: list | None = None   # [NEW] embedding 安全网语料向量（registry 初始化时批量计算缓存）

def load_capabilities(path: Path = CONFIG_PATH) -> CapabilityRegistry: ...

class CapabilityRegistry:
    capabilities: dict[str, Capability]         # name → Capability（保持配置顺序）
    default_capability: str
    error_capability: str
    classifier_provider: str                    # 分类专用 LLM（暂定 deepseek）
    embedding_threshold: float                  # 安全网命中阈值（0.85）

    def match_rules(self, query: str) -> str | None:
        """按注册顺序逐能力尝试 rules，第一个命中的能力名；无命中 None"""

    async def match_embedding(self, query_vector: list[float]) -> tuple[str, float] | None:
        """[NEW] query 向量与各能力 example_vectors 做余弦相似度，
        返回 (能力名, 最高分)；无超阈值命中返回 None"""

    def build_llm_context(self) -> str:
        """把能力清单（name/description/examples）渲染为提示词片段（配置注入式）"""

    def validate_entries(self, registered_nodes: set[str]) -> None:
        """entry/default/error 能力与图节点的一致性校验（§2.2）"""
```

### 3.2 路由节点（`app/agent/capabilities/router.py`）[NEW]

```python
async def route_capability(state: DataAgentState, runtime: Runtime[DataAgentContext]) -> dict:
    """取代 intent_classify。**五级递进通道**（tier 0 用户显式选择，已经项目所有者确认）：

    0. 用户显式选择（tier-0，最高优先级）：
       前端能力芯片选中（注册表中 selectable=true 的能力）→ 请求携带 capability 字段
       → 直接分发，跳过全部推断（确定性 100%）；选择在前端持续保持（模式开关式），
       再次点击取消。非法值（不在注册表/selectable=false）→ 忽略 + warning，落入 1 级
    1. 规则快路径（features.rules_fast_path 开启时）：
       registry.match_rules(query) —— 只放"高度确定"的正则（如"查询数据""数据查询"），
       命中即分发，0 token、0 延迟
    2. embedding 安全网（features.embedding_route 开启时）：
       query 向量化（runtime.context["embedding_client"].aembed_query），
       与各能力 examples 向量做余弦相似度，最高分 >= routing.embedding_threshold(0.85) → 分发该能力
       - examples 向量在 registry 初始化时批量计算并内存缓存（规模仅数条/能力，无需 Qdrant
         collection；未来 examples 规模化时升级为 Qdrant 检索，接口不变）
       - embedding 服务异常 -> 打 warning 跳过本级落入 LLM（安全网故障不阻断路由）
    3. LLM 分类：classifier_provider（暂定 deepseek）——
       prompts/capability_route.prompt（registry 注入能力清单）+ JsonOutputParser
       输出 {"capability": "<name>"}；值不在注册表 -> default_capability
    4. 兜底：上述任一环节异常且无结果 -> error_capability=default，
       由 default_answer 向用户致歉并建议重试

    capability_source 记录路由来源："user" / "rules" / "embedding" / "llm" / "fallback"。
    capability_routing=false（总开关关闭）时：忽略用户显式选择，恒 dataquery（关=旧行为）。

    返回 {"capability": str, "intent": str(=capability, 兼容字段),
          "capability_source": str, "tool_calls": list[str], "intent_reply": ""}
    副作用：写入 runtime.context 的 capability_holder（SSE 注入用，§3.4）
    progress 事件：step="理解用户意图"，status running/success（保持前端步骤条兼容）
    """
```

**提示词**（`prompts/capability_route.prompt`）[NEW]：模板骨架 + registry 注入的能力清单，要求模型只输出 JSON `{"capability": "..."}`，不输出解释。模板变量：`{capabilities}`（registry.build_llm_context() 产物）、`{query}`、`{last_assistant_msg}`（沿用现状取最近 assistant 消息的逻辑，05 阶段移交 history_provider）。

**分类专用 LLM**：`create_llm(routing.classifier_provider, usage_tracker)`——与用户选择的生成模型**解耦**（用户选 glm 不影响分类用 deepseek）。暂定 deepseek，后续以评估框架跑"精准度 + 速度"两指标敲定最终分类模型，届时只改 `classifier_provider` 配置。tracker 正常采集（by_stage 归属路由环节，分类调用的成本计入实验报告）。

**tool_calls 写入规则（v1）**：`capability == "dataquery"` → `["dataquery.search"]`；否则 `[]`。命名规范 `{capability}.{action}` 与 03 §3.9 一致；skill 化后由真实工具执行覆写此字段（03 的评估数据源无缝切换）。

### 3.3 兜底节点（`app/agent/nodes/default_answer.py`）[NEW]

```python
async def default_answer(state: DataAgentState, runtime: Runtime[DataAgentContext]) -> dict:
    """default 能力的统一处理节点，吸收原 recap/chitchat/help 三节点语义：
    - 取用 llm = runtime.context["llm"]
    - 历史读取沿用现状模式（state["messages"] 倒序取最近 assistant 内容），05 移交 history_provider
    - prompt：prompts/default_answer.prompt [NEW]——通用助手人设 + 三类场景指引
      （闲聊简短回应 / 使用说明 / 回顾上一轮结果，prompt 中按用户输入自行适配）
    - writer：{"type": "progress", "step": "生成回复", "status": ...} + {"type": "explanation", "text": ...}
    - 返回 {"intent_reply": text, "messages": [...]}（写回对话历史，与现状 explain_result 模式一致）
    - LLM 失败降级：输出静态兜底文案（含使用提示），不抛异常
    """
```

原 `recap_answer`/`chitchat_answer`/`help_answer` 为**无 LLM 的静态回复**（核对：recap 拼"您上一次问的是..."、chitchat 回放 intent_reply、help 硬编码文案），语义全部可由 default_answer 的 LLM + 历史覆盖，故三个节点直接删除而非保留调用。

### 3.4 SSE capability 字段注入（00 §4.3 落地机制）

```python
# app/agent/capabilities/capability_holder.py [NEW]
class CapabilityHolder:
    """Request 级可变容器：路由节点写入，QueryService 读取。
    放入 DataAgentContext（非 state——不参与图状态合并）"""
    value: str | None = None
```

- **写入**：`route_capability` 判定后执行 `runtime.context["capability_holder"].value = capability`（新节点内的 1 行，不触碰问数链路节点）
- **读取注入**：`QueryService.query()` 序列化每个 SSE chunk 时：`chunk.setdefault("capability", holder.value)` —— 路由之后的事件带 capability；路由自身 progress 事件发出时 holder 已写入（running 事件先于写入 → 该事件无 capability 字段，前端按协议容忍缺失）
- **开关关闭时**（capability_routing=false）：QueryService 对所有事件注入 `capability="dataquery"`（00 §4.3 规则 2：字段恒在）
- 前端 `types/agent.ts` 各事件类型加 `capability?: string`（02 文档类型改动清单同步项）

### 3.5 state 变更（`app/agent/state.py`）

```python
class DataAgentState(TypedDict):
    # ...现有字段不变...
    intent: str              # [语义变更→兼容字段] 旧五分类废弃；值 = capability 名
    intent_reply: str        # [不变] default_answer 的回复文本
    capability: str          # [NEW] 本次请求路由选中的能力名（v1: dataquery|default）
    capability_source: str   # [NEW] 路由来源：user/rules/embedding/llm/fallback（03 评估按 source=user 过滤）
    tool_calls: list[str]    # [NEW] 本次请求调用的工具标识（v1 由路由写入；skill 化后为真实调用序列）
```

---

## 4. 对现有代码的改动点清单（graph.py 已逐行核对）

| # | 文件 | 位置 | 操作 | 内容 |
|---|------|------|------|------|
| 1 | `conf/capability_config.yaml` | 全文件 | [NEW] | §2.1（含 entry 字段，00 §5.2 同步补充） |
| 2 | `app/agent/capabilities/__init__.py` | 全文件 | [NEW] | 包声明 |
| 3 | `app/agent/capabilities/registry.py` | 全文件 | [NEW] | §3.1（含启动校验） |
| 4 | `app/agent/capabilities/router.py` | 全文件 | [NEW] | §3.2 路由节点（三级通道） |
| 5 | `app/agent/capabilities/capability_holder.py` | 全文件 | [NEW] | §3.4 |
| 6 | `app/agent/nodes/default_answer.py` | 全文件 | [NEW] | §3.3 |
| 7 | `prompts/capability_route.prompt` | 全文件 | [NEW] | 配置注入式路由提示词 |
| 8 | `prompts/default_answer.prompt` | 全文件 | [NEW] | default 能力提示词（吸收 recap/chitchat/help 语义指引） |
| 9 | `app/agent/graph.py` | L51 | [DELETE] | `start_recall` 空节点注册 |
| 10 | `app/agent/graph.py` | L30、L67-69、L102-104 | [DELETE] | simple_answer 三节点 import、注册、END 边 |
| 11 | `app/agent/graph.py` | L106-111 | [DELETE] | start_recall 相关注释残留边 |
| 12 | `app/agent/graph.py` | L24（import）、L66（注册）、L77-99（route_by_intent）、L90-99（条件边） | [MODIFY] | `intent_classify` → `route_capability`；`route_by_intent` 五分流 → `route_by_capability` 两分流（path_map 由 registry 动态构建：每个能力 entry → entry；非 hardcode，新增能力不改此函数） |
| 13 | `app/agent/graph.py` | L39 后新增 | [MODIFY] | import registry/router/default_answer；`load_capabilities()` 启动加载 + `validate_entries`（对 graph_builder 已注册节点集合校验，在 compile 之前） |
| 14 | `app/agent/graph.py` | L172-248（`__main__` 调试） | [MODIFY] | context 补 `llm`（01 已列）与 `capability_holder`；调试 query 不变 |
| 15 | `app/agent/nodes/intent_classify.py` | 全文件 | [DELETE] | 被 router.py 取代（其"取最近 assistant 消息"逻辑迁入 router/default_answer） |
| 16 | `prompts/intent_classify.prompt` | 全文件 | [DELETE] | 被 capability_route.prompt 取代（五分类定义废弃） |
| 17 | `app/agent/nodes/simple_answer.py` | 全文件 | [DELETE] | 三节点语义由 default_answer 吸收（§3.3） |
| 18 | `app/agent/state.py` | L85-86 后 | [MODIFY] | 新增 `capability` / `tool_calls` 字段；`intent` 注释更新为兼容字段说明（§3.5） |
| 19 | `app/agent/context.py` | L20-34 | [MODIFY] | `DataAgentContext` 新增 `capability_holder: CapabilityHolder` |
| 20 | `app/services/query_service.py` | SSE 序列化处（L78-81） | [MODIFY] | chunk 注入 capability（§3.4）；context 构建处新增 holder 实例 |
| 21 | `frontend/src/types/agent.ts` | AgentEvent 各成员 | [MODIFY] | 加 `capability?: string`（00 §4.3；与 02 的类型改动同文件，编码时合并提交） |

**明确不改**：extract_keywords 及问数链路其余 18 个节点内部逻辑、`route_after_validate`/`route_after_correct` 两组条件边、`InMemorySaver` checkpointer、`messages` 写入节点（extract_keywords L25-26/L52、explain_result L49-51——05 接管）。

---

## 5. Feature Flags 开关语义

| 开关组合 | 行为 |
|---------|------|
| `capability_routing=true`（任意子开关）+ **用户芯片选中** | **tier-0 直接分发**：跳过规则/embedding/LLM，capability_source=user——芯片是模式开关（前端持续保持），确定性 100% |
| `capability_routing=true, rules_fast_path=true, embedding_route=true`（默认，未选芯片） | **四级通道全开**：规则命中 → 直接分发（0 token）；未命中 → embedding 安全网（≥0.85 分发）；仍未命中 → LLM 分类（deepseek）；任何异常 → default 兜底 |
| `capability_routing=true, rules_fast_path=false` | 跳过规则通道：embedding → LLM → 兜底（用于量化规则通道贡献——03 对比实验） |
| `capability_routing=true, embedding_route=false` | 跳过安全网：规则 → LLM → 兜底（用于量化 embedding 层贡献） |
| `capability_routing=true, rules_fast_path=false, embedding_route=false` | 纯 LLM 分类基线 |
| `capability_routing=false` | **路由节点直通**：四级通道全不执行，恒返回 `capability="dataquery"`，SSE 全部事件注入 `capability="dataquery"`——与改造前行为完全等价（关 = 旧行为，00 红线） |

实现方式说明：开关判断在 `route_capability` 节点内部首行（这是"模块边界"——路由层本身），**不改变 graph 拓扑**（避免为开关维护两张图）。

---

## 6. 验收标准

1. **规则快路径**：“查询数据：各地区销售额排行”（命中高确定性规则）→ 不触发 embedding/LLM（tracker by_stage 无路由环节的 LLM/embedding 记录）→ 进入 extract_keywords；“你好” → 命中 default 规则 → default_answer；“你今年多少岁了” → **不命中**规则（模糊词已移除）→ 落入下一级
2. **embedding 安全网**：“看看各地区的销售额”（不命中规则）→ 向量相似度 ≥0.85 → dataquery（日志记录命中分数）；“今天天气不错” → 相似度低于阈值 → 落入 LLM；**临时停掉 embedding 容器** → warning + 跳过本级走 LLM（不阻断）
3. **LLM 分类与兜底**：不命中规则与 embedding 的 query → LLM 分类；临时注入 LLM 异常 → **请求走通 default**（default_answer 致歉并建议重试，不再指向 dataquery）；LLM 返回 `"capability": "nonexistent"` → default
4. **开关关闭 = 旧行为**：`capability_routing=false` 后，问数 query 正常走通、无 LLM 分类记录、SSE 事件全部带 `capability: "dataquery"`、default 不可达
5. **SSE 协议**：路由后任一事件含 `capability` 字段且值与实际走的分支一致（0 §4.3）；路由自身的首个 running 事件允许缺失该字段（前端容忍）
6. **default 语义覆盖**（人工用例）：问候语得到简短回应；"你能做什么"得到含问数能力介绍的帮助；连续两轮对话后问“刚才那个结果是什么意思” → 能基于历史回答（recap 语义）
7. **评估数据源**：跑 03 评测（≥3 条 enabled 用例，含 1 条 default 期望）→ `tool_metrics` 的 invocation_accuracy/tool_recall/tool_precision 正常产出
8. **新能力接入演练**：临时在注册表加第三个能力条目（entry 指向一个 stub 节点）→ 不改任何路由代码即可被路由到（演练后撤销）——这是本模块的核心验收
9. **启动校验**：capability_config.yaml 的 entry 指向不存在节点 → 应用启动即失败并报出能力名与节点名
10. **遗留清理确认**：`grep -r "start_recall\|simple_answer\|intent_classify" app/` 零结果（prompt 文件名除外）
11. **通道贡献实验**：`rules_fast_path=false` / `embedding_route=false` 两种组合各跑一次 03 评测，报告可对比三级识别通道各自贡献（token 节省与命中率）
12. **能力芯片（tier-0）**：前端选中 [数据查询] 芯片 → 发送含 capability=dataquery 的请求 → 不触发规则/embedding/LLM（tracker 无路由推断记录）→ capability_source=user；芯片持续选中跨多条消息生效，再次点击取消后恢复自动路由；请求携带非法 capability 值 → 忽略并走自动通道（不报错）
13. **tool_metrics 口径**：03 评测只统计自动路由（source≠user）——芯片选中不影响评估数据集的运行结果

---

## 7. 风险与备注

### 7.1 为什么不拆子图（设计取舍记录）

子图（subgraph）方案要求 state schema 映射与 context 透传，对只有两个能力、且共享同一 state 的现状是过度设计；单图 + `entry` 入口节点的映射已把"新增能力"的改动收敛为"配置条目 + 一个节点"。**演进路径**：当能力数量增多或能力需要独立 state（如对话能力不需要问数字段）时，再在 `route_by_capability` 后引入 subgraph——届时只改图组装代码，registry/router/评估接口均不变。

### 7.2 intent 字段的兼容策略

`state["intent"]` 保留且恒等于 `capability` 值：03 评估、可能的旧代码引用、日志检索均不断裂。05 重构 state 时再评估是否物理删除。

### 7.3 规则与 embedding 层的设计约定（按项目所有者确认的策略）

- **规则只放"高度确定"的表述**（如"查询数据""数据查询"），刻意不放"多少""统计"这类模糊词——杜绝"你今年多少岁了"被误判为问数的经典误命中；规则调优属于配置迭代，不入代码
- **embedding 是安全网而非主分类器**：定位在规则（太严格）与 LLM（有成本）之间，捞回语义明确但表述不在规则里的问数请求（如"看看各地区的销售额"）；阈值 0.85 起步，调优依据是 03 评估的 tool_metrics
- **兜底统一为 default**：任何一级失败或全部未命中，都交给通用对话节点致歉并建议重试——宁可让用户重发，不用错误链路猜
- **分类模型暂定 deepseek**，评估框架就绪后以精准度+速度两指标在三家模型中敲定，只改 `classifier_provider` 配置

### 7.4 LLM 分类的鲁棒性

JsonOutputParser 解析失败、返回空、返回未知值三种情况统一归入"分类失败"处理：先尝试 default_capability 兜底？——**否**：解析异常（链路抛错）走 error_capability（dataquery，与现状降级一致）；解析成功但值未知走 default_capability。两种失败语义不同，实现时须区分（03 的 tool_metrics 依赖这一区分统计误调来源）。

### 7.5 与 03 评估的衔接确认

- `state["capability"]` → 03 `intent_metrics`（v1 映射后等价）
- `state["tool_calls"]` → 03 `tool_metrics` 的 invoked_tools（v1 数据源；skill 化后数据源自动切换，评估接口不变）
- baseline 对照：`capability_routing=false` 的行为 = 五分类时代的 dataquery 主链路行为，报告可复现

---

## 8. skill 目录契约（2026-09-23 增补，预留方向激活）

**已经项目所有者确认**：能力的实体将演进为 skill 目录——每个技能一个目录，内含提示词说明 + 工具定义。这是"skill+工具化"（§1.1 预留方向）的目录级落地。

### 8.1 目录结构（顶层 `skills/`，与 `prompts/` 平级）

```
skills/
├── dataquery/
│   ├── skill.md             # 技能提示词说明：人设、边界、输出要求（进 05 固定前缀）
│   └── tools.json           # 工具注明：dataquery_search 的 JSON Schema（05 前缀 + 未来真 function-calling）
└── default/
    ├── skill.md
    └── tools.json           # v1 为空工具集 {"tools": []}
```

### 8.2 职责与演进路径

| 内容 | v1 | skill 化后 |
|------|-----|-----------|
| 路由判别 description | `capability_config.yaml`（进 LLM 分类提示词） | 不变 |
| 技能说明/人设 | `skills/<name>/skill.md`（05 prefix.py 读取，进固定前缀） | 同左，内容丰富化 |
| 工具定义 | `skills/<name>/tools.json`（仅作为前缀组成件，让模型感知能力边界） | 真实 function-calling 执行（`state["tool_calls"]` 变为真实调用序列） |
| description 双写 | yaml（路由判别，短/判别性强）与 skill.md（生成人设，完整）**并存**——受众不同，强行单一来源两头不讨好 | 合并时机随 skill 化评估 |

### 8.3 KV cache 纪律延伸

skill.md / tools.json 内容进入 05 §3.4 固定前缀——**任何变更 = 前缀变更**，须评审并递增 `prefix_version`（05 §3.4 同款纪律）。`build_llm_context` 的切换点：v1 读 yaml description 不动；skill 化时切换为聚合 skill 目录，router 与评估接口不变。
