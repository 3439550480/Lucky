> ⚠️ 域示例说明（2026-10-09）：本文档中的电商/奶茶店业务示例已随项目定位切换（安踏特卖店 2.0）过时；架构条款、接口契约与机制定义仍然有效，以本文档为准。

# 01 · LLM 多模型封装与用量采集（M1）

> 状态：`final`（已评审定稿）　|　上位文档：[00_overview.md](00_overview.md)（final）
> 修订记录：2026-09-19 初稿；同日补充 KV cache 技术债登记与轨迹术语对齐；2026-09-21 定价结构修订（3 模型 + DeepSeek tiers 档位，provider 不按峰谷拆分）
> 本章所有改动点均基于 2026-09-19 对代码库的精确核对（9 个 LLM 引用点已逐一确认），核对明细见 §4。

---

## 1. 目标与边界

### 1.1 做什么

1. **多厂商 LLM 工厂**：`create_llm(provider)` 统一创建 DeepSeek / Qwen / GLM 的聊天模型实例（全部走 OpenAI 兼容端点），参数来自 `app_config.llm.providers`
2. **用量自动采集**：`LLMUsageTracker`（LangChain callback）挂载在工厂产物上，零侵入地记录每次调用的 token 用量、延迟、所属环节
3. **按请求注入**：LLM 实例经 `DataAgentContext.llm` 注入图，节点从 `runtime.context["llm"]` 取用——支撑前端模型选择（02）与评估框架的成本指标（03）
4. **Feature Flags 骨架**：`AppConfig` 新增 `FeatureFlags` dataclass（全部开关的落地载体，本文先实现 `usage_tracking` 的消费，其余开关由后续模块消费）

### 1.2 不做什么

- 不实现前端模型下拉与 `/api/models` 端点（02 文档）
- 不做成本折算（`price_*` 只是配置，费用计算在 03 文档的 `cost_metrics.py`）
- 不改动任何 prompt 文件、解析器选择、SSE 事件结构
- 不引入流式 token 输出（现状全部 `chain.ainvoke`，保持）
- 不处理 `simple_answer.py` 三节点（它们不调用 LLM，04 文档处理其收编）

### 1.3 依赖模块

- 上游：无（本模块是地基）
- 下游消费方：02（前端选择模型 → 传入 provider 名）、03（tracker 明细 → 成本指标）、04（default_answer 节点经 context 取 llm）、05（无直接依赖）

---

## 2. 配置 Schema

### 2.1 `conf/app_config.yaml` 的 `llm:` 段最终形态

> 完整文件见 00 文档 §5.1，此处仅展示本模块相关的 `llm` 段（已定稿）。

```yaml
llm:
  default: deepseek           # 默认 provider 名；请求未指定/非法时兜底
  providers:
    # 物理模型 3 个；DeepSeek 的峰谷是 pricing.tiers 计费档位（按调用时间自动判定），不是可选模型
    deepseek:
      base_url: https://api.deepseek.com
      api_key: ${DEEPSEEK_API_KEY}
      model: deepseek-flash
      pricing:
        tiers:
          peak:    {input: 2.0, input_cache_hit: 0.04, output: 8.0}
          offpeak: {input: 1.0, input_cache_hit: 0.02, output: 4.0}
        tier_rules: beijing_workweek            # 时段判定器在 03 文档 cost 模块实现
    qwen:
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key: ${DASHSCOPE_API_KEY}
      model: qwen3.8-flash
      pricing:
        tiers:
          standard: {input: 0.8, input_cache_hit: 0.1, output: 2.7}
    glm:
      base_url: https://open.bigmodel.cn/api/paas/v4
      api_key: ${ZHIPU_API_KEY}
      model: glm-5.3-flash
      pricing:
        tiers:
          standard: {input: 0.8, input_cache_hit: 0.23, output: 2.8}
```

### 2.2 本模块新增的**可选** provider 字段（在 00 基础上补充）

```yaml
  providers:
    deepseek:
      # ...上述已定稿字段之外，工厂额外支持（均可缺省）：
      temperature: 0.7              # 缺省 0.7（与现状 llm.py 一致）
      extra_params: {}              # 透传给 ChatOpenAI 的 model_kwargs，见 §2.3
```

### 2.3 DeepSeek 思考模式开关方案

- DeepSeek-V4.1-Flash 思考模式**默认开启**。关闭方式：经 `extra_params` 透传厂商专有参数（OpenAI 兼容端点放在请求 body 的额外字段）
- ⚠️ **具体参数键名以 DeepSeek 官方文档为准，编码时实测验证**（候选形态如 `extra_params: {thinking: {type: disabled}}`），本文档不锁定键名，只锁定机制：
  - 机制：`create_llm` 将 `provider.extra_params` 整体作为 `model_kwargs` 传给 `ChatOpenAI`，不做任何白名单过滤
  - 这样 Qwen 的 `enable_thinking`、GLM 的专有参数同样经此通道透传，一套机制覆盖三厂商
- 验证结果回填到本文档 §2.3（编码阶段任务，见 §6 验收标准第 8 条）

### 2.4 `AppConfig` dataclass 变更（`app/conf/app_config.py`）

```python
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class PriceTier:                                # 单个计费档位的单价（元/百万 token，可缺省）
    input: Optional[float] = None
    input_cache_hit: Optional[float] = None
    output: Optional[float] = None

@dataclass
class PricingConfig:                            # DeepSeek 含 peak/offpeak 两档；Qwen/GLM 仅 standard 一档
    tiers: dict = field(default_factory=dict)   # dict[str, PriceTier]
    tier_rules: Optional[str] = None            # "beijing_workweek" → 03 文档的时段判定器；None = 恒用唯一档

@dataclass
class LLMProviderConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.7
    pricing: PricingConfig = None               # 成本评估用，可缺省
    # 厂商专有参数透传（如 DeepSeek 思考模式开关、Qwen enable_thinking）
    extra_params: dict = field(default_factory=dict)

@dataclass
class LLMConfig:                                # 替换原 LLMConfig（model_name/api_key/base_url 三字段）
    default: str
    providers: dict                             # dict[str, LLMProviderConfig]

@dataclass
class FeatureFlags:                             # [NEW] 全局开关载体（00 文档 §3 定义表）
    usage_tracking: bool = True
    capability_routing: bool = True
    rules_fast_path: bool = True
    context_management: bool = True
    memory_short_term: bool = True              # 对应 yaml: features.memory.short_term（扁平化命名见下）
    memory_long_term: bool = False

@dataclass
class AppConfig:
    # ...原有字段不变...
    llm: LLMConfig
    features: FeatureFlags = None               # [NEW]；yaml 缺省时用默认值（全部 = 旧行为语义）
```

> **实现细节**：`features.memory.short_term` 嵌套结构在 OmegaConf structured + dataclass 合并时处理为 `FeatureFlags` 的扁平字段（或改用嵌套 `MemoryFlags` dataclass，编码时择一，倾向嵌套以贴合 yaml）。
> **兼容性**：旧 yaml 无 `features:` 段时，`OmegaConf.merge` 使用 dataclass 默认值——注意默认值语义必须是"开 = 新行为、关 = 旧行为"的反向问题：**yaml 必须显式写全 features 段**（00 文档 §5.1 已包含），dataclass 默认值仅作兜底。

---

## 3. 接口与数据结构

### 3.1 工厂 `app/agent/llm_factory.py` [NEW]

```python
from langchain_core.language_models.chat_models import BaseChatModel
from app.conf.app_config import app_config
from app.agent.usage import LLMUsageTracker

def create_llm(
    provider: str | None = None,
    *,
    usage_tracker: LLMUsageTracker | None = None,
) -> BaseChatModel:
    """按 provider 配置创建聊天模型实例。

    - provider 为 None 或不在 providers 中时，回退到 app_config.llm.default 并打 warning 日志
    - usage_tracker 非 None 时，作为 callback 挂载到实例上（configurable_callbacks）
    - 统一走 model_provider="openai"（三家均为 OpenAI 兼容端点），base_url/api_key/model 来自配置
    - provider.extra_params 经 model_kwargs 透传（§2.3 机制）
    """
```

实现要点：

1. `ChatOpenAI(model=cfg.model, base_url=cfg.base_url, api_key=cfg.api_key, temperature=cfg.temperature, model_kwargs=cfg.extra_params, callbacks=[tracker] if tracker else None)`
2. `callbacks` 参数在构造时传入（实例级 callback），比每次调用传 config 更省心，且节点调用 `chain.ainvoke` 时无需感知——**这是"零侵入"的关键**
3. 每次调用 `create_llm` 都产生**新实例**（不缓存单例）：因为 tracker 是 Request 级的，实例必须随请求走。模型对象的构造成本可忽略（无网络 IO，连接在首次调用时建立）
4. 非法 provider 的回退行为：`logger.warning(f"未知 provider '{provider}'，回退到默认 '{default}'")`，不抛异常（保证前端传错值不炸请求）

### 3.2 用量采集 `app/agent/usage.py` [NEW]

```python
from dataclasses import dataclass, field
from langchain_core.callbacks import BaseCallbackHandler

@dataclass
class LLMCallRecord:
    stage: str                  # 归属环节（节点名，见下方"环节归属机制"）
    model: str                  # 模型名（如 deepseek-flash）
    input_tokens: int | None    # usage_metadata.input_tokens（None = 端点未返回）
    output_tokens: int | None
    total_tokens: int | None
    latency_ms: int             # 本次调用耗时
    success: bool
    error: str | None           # 失败时的异常摘要
    ts: float                   # time.time()，调用开始时间

class LLMUsageTracker(BaseCallbackHandler):
    """Request 级用量采集器。随请求创建，经 create_llm 挂到模型实例上。

    采集机制（LangChain callback，节点代码零改动）：
    - on_llm_start：记录开始时间与 run_id
    - on_llm_end：从 response.usage_metadata 提取 input/output/total_tokens（OpenAI 兼容端点
      均返回 usage 字段），计算 latency，追加 LLMCallRecord
    - on_llm_error：记录失败调用（success=False）
    - 环节归属：通过 on_chain_start / on_chain_end 维护"链式运行栈"——LangGraph 的每个节点
      执行会以节点函数名（如 "intent_classify"）出现为 chain run，LLM run 的最近祖先 chain
      名即归属环节；无法解析祖先时 stage="unknown"（不失败、不阻塞）
    """

    def records(self) -> list[LLMCallRecord]: ...
    def summary(self) -> dict:
        """聚合输出，供 QueryService 请求结束时打日志、供 03 评估 runner 逐用例收集。
        结构：
        {
          "provider": str, "model": str,
          "calls": int, "errors": int,
          "input_tokens": int|None, "output_tokens": int|None, "total_tokens": int|None,
          "total_latency_ms": int,
          "by_stage": {stage: {"calls": int, "input_tokens":..., "latency_ms":...}},
          "note": "未获取到 usage 字段"   # 当端点未返回 usage 时提示
        }
        """
```

设计约束：

1. **token 缺失容错**：某厂商端点未返回 usage 时，对应字段记 `None`，`summary.note` 说明；绝不因缺字段抛异常
2. **缓存命中价不在 tracker 处理**：`price_input_cache_hit` 的折算在 03 `cost_metrics.py`（它拿到的输入 token 无法区分命中与否，v1 一律按未命中价计算，报告中注明该口径）
3. **线程安全**：`run_in_executor` 的 embedding 不经过本 tracker（tracker 只挂 LLM）；LangChain callback 在同一事件循环内回调，list append 足够
4. **空 usage_tracking 时不创建 tracker**（见 §5 开关语义），`create_llm(usage_tracker=None)`

### 3.3 注入链路

```python
# app/agent/context.py [MODIFY]
class DataAgentContext(TypedDict):
    column_qdrant_repository: ColumnQdrantRepository
    embedding_client: HuggingFaceEndpointEmbeddings
    metric_qdrant_repository: MetricQdrantRepository
    value_es_repository: ValueESRepository
    meta_mysql_repository: MetaMySQLRepository
    dw_mysql_repository: DWMySQLRepository
    llm: BaseChatModel                       # [NEW] 按请求创建的 LLM 实例（已挂 tracker）
```

```python
# app/services/query_service.py [MODIFY]
class QueryService:
    def __init__(self, /*6 个依赖不变*/): ...

    async def query(self, query: str, thread_id: str, model: str | None = None):   # [MODIFY] 新增 model 参数
        tracker = LLMUsageTracker(provider=model or app_config.llm.default) \
                  if app_config.features.usage_tracking else None
        context = DataAgentContext(
            ...,                               # 原 6 项不变
            llm=create_llm(model, usage_tracker=tracker),
        )
        try:
            async for chunk in graph.astream(...):
                yield f"data: {json.dumps(chunk, ensure_ascii=False, default=str)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ...)}\n\n"
        finally:
            if tracker:
                logger.info(f"LLM usage | {json.dumps(tracker.summary(), ensure_ascii=False)}")
```

- **`model` 参数来源**：`query_router.py` 从 `QuerySchema.model` 取（02 文档定义字段，本模块先行支持参数）
- **`dependencies.py` 不需要改动**：QueryService 直接调用 `create_llm`（无状态函数），无需新增依赖 provider——比原计划（dependencies 新增 provider）改动更小
- `query_router.py` [MODIFY]：`query_handler` 调用处透传 `model=schema.model`（schema 字段在 02 落地，本模块先把参数链打通）

### 3.4 节点取用模式（9 个节点统一改法）

```python
# 改造前（以 intent_classify.py 为例）
from app.agent.llm import llm          # [DELETE] 此行
...
chain = prompt | llm | output_parser   # [MODIFY] llm 来源变化

# 改造后
...
llm = runtime.context["llm"]           # [NEW] 与 qdrant_repository 等取用模式一致
chain = prompt | llm | output_parser   # 此行结构不变
```

- 所有 9 个节点均为 LCEL chain（`PromptTemplate | llm | parser`）+ `await chain.ainvoke(...)`，**chain 构建与调用行除 `llm` 标识符来源外完全不动**
- 实例级 callback 使得 `chain.ainvoke` 无需传 config/callbacks——节点改动就是"换一个变量来源"

> ⚠️ **KV cache 技术债登记（本模块不改，05 统一修）**：现状每个节点独立拼 `PromptTemplate`，动态内容（query/历史/表结构）与静态指令混排，导致每次请求发给厂商的 prompt 前缀不稳定——厂商端的隐式前缀缓存（KV cache）无法命中。以 DeepSeek 为例，缓存命中输入价 0.02 元 vs 未命中 1.0 元（50 倍差距），这笔浪费目前完全不可见。**本模块严格保持 chain 结构原样**（改造会污染 baseline 实验），改造方案（稳定前缀 + 追加式历史的提示词架构）在 05 文档的上下文管理策略中设计，并以成本报告中的缓存命中数据验证效果。

---

## 4. 对现有代码的改动点清单（基于逐文件核对）

| # | 文件 | 位置 | 操作 | 内容 |
|---|------|------|------|------|
| 1 | `app/agent/llm_factory.py` | 全文件 | [NEW] | `create_llm()` 工厂（§3.1） |
| 2 | `app/agent/usage.py` | 全文件 | [NEW] | `LLMCallRecord` + `LLMUsageTracker`（§3.2） |
| 3 | `app/agent/llm.py` | L17-23 | [MODIFY] | 删除模块级单例 `llm = init_chat_model(...)`，改为兼容层：模块级 `__getattr__` 拦截 `llm` 访问，抛 `RuntimeError("llm 单例已移除：节点请使用 runtime.context['llm']，应用层请使用 llm_factory.create_llm()")`——保留文件使遗漏迁移的引用**立即报错**而非静默用错模型；L26 `__main__` 自测改为调用 `create_llm()`；清理 L6、L12-14 死导入（`app_config`、`AIMessage` 等 7 个未使用导入） |
| 4 | `app/conf/app_config.py` | L55-73 | [MODIFY] | `LLMConfig` 重构为 `{default, providers}`；新增 `LLMProviderConfig`、`FeatureFlags`；`AppConfig` 加 `features` 字段（§2.4） |
| 5 | `conf/app_config.yaml` | llm 段 + 文件尾 | [MODIFY] | llm 改 providers 结构（00 §5.1 定稿）；追加 `features:` 段 |
| 6 | `app/agent/context.py` | L20-34 | [MODIFY] | `DataAgentContext` 新增 `llm: BaseChatModel` 字段；补 import |
| 7 | `app/services/query_service.py` | L36（签名）、L58-65（context 构建）、新增 finally | [MODIFY] | `query()` 加 `model` 参数；context 注入 `llm`；finally 打印 tracker summary |
| 8 | `app/api/routers/query_router.py` | query_handler 内 | [MODIFY] | 调用 `query(query, thread_id, model=...)`（schema.model 字段 02 文档定义，本模块先透传） |
| 9 | `app/agent/nodes/intent_classify.py` | L7 import；L33 chain 行 | [MODIFY] | 删 `from app.agent.llm import llm`；函数体内 `llm = runtime.context["llm"]`（置于 L18 附近，writer 声明之后） |
| 10 | `app/agent/nodes/recall_column.py` | L14；L44 | [MODIFY] | 同上 |
| 11 | `app/agent/nodes/recall_metric.py` | L14；L43 | [MODIFY] | 同上 |
| 12 | `app/agent/nodes/recall_value.py` | L14；L44 | [MODIFY] | 同上 |
| 13 | `app/agent/nodes/filter_table.py` | L14；L39 | [MODIFY] | 同上 |
| 14 | `app/agent/nodes/filter_metric.py` | L14；L39 | [MODIFY] | 同上 |
| 15 | `app/agent/nodes/generate_sql.py` | L14；L50 | [MODIFY] | 同上 |
| 16 | `app/agent/nodes/correct_sql.py` | L14；L53 | [MODIFY] | 同上 |
| 17 | `app/agent/nodes/explain_result.py` | L7；L36 | [MODIFY] | 同上（解析器 StrOutputParser 不变） |
| 18 | `app/agent/graph.py` | L212-219（`__main__` 调试 context） | [MODIFY] | 调试用 `DataAgentContext` 补 `llm=create_llm()` |
| 19 | `pyproject.toml` | dependencies | [MODIFY] | 显式声明 `langchain-openai>=1.0`（当前 1.2.2 已作为 langchain-deepseek 传递依赖锁在 uv.lock，显式声明以固化契约）；`langchain-deepseek` 保留（不删，避免 lock 抖动，04 阶段评估清理） |
| 20 | `app/api/dependencies.py` | — | **不变** | 与原计划不同：QueryService 直调 create_llm，无需新增 provider（核对后确认改动面更小） |

**明确不改**：全部 prompt 文件、6 个 JsonOutputParser / 3 个 StrOutputParser、SSE 事件结构、`messages` 读写逻辑（intent_classify L19-26 等 5 处，归 05 文档）、`simple_answer.py` 三节点、repositories/clients/entities/models。

---

## 5. Feature Flags 开关语义

| 开关 | 开（默认） | 关 |
|------|-----------|-----|
| `features.usage_tracking` | `create_llm` 挂 tracker；请求结束 `logger.info` 输出 summary；评估 runner 可收集明细 | `create_llm(usage_tracker=None)`：LLM 调用行为完全不变、无任何计量开销；QueryService finally 块不执行 |

- 本模块只消费 `usage_tracking` 一个开关；`capability_routing` 等其余开关在 00 定义、由 04/05 消费
- 关闭 tracker **不影响** 03 评估的成本指标可用性吗？——影响：tracker 关闭则无明细。**约定：评估运行时 runner 强制以 override 打开 usage_tracking**（03 文档落地），在线链路尊重配置

---

## 6. 验收标准

1. **启动**：`uv run uvicorn main:app --reload --port 8000` 正常启动，无 import 错误；`grep -r "from app.agent.llm import llm" app/` 零结果
2. **默认模型**：不带 model 参数请求 `POST /api/query`（curl 或 /docs），流程完整走通（SSE 收到 progress/result/explanation），后端日志无"未知 provider"警告（默认 provider 生效）
3. **指定模型**：`model="glm"` 请求成功走通（若 ZHIPU_API_KEY 已配置）；`model="not_exist"` 请求**不报错**，日志出现回退 warning，使用 default provider 走通
4. **用量日志**：一次完整问数请求结束后，日志出现 `LLM usage | {...}` 且 `calls >= 1`、`by_stage` 包含 `intent_classify` 与 `generate_sql` 环节、`input_tokens/output_tokens` 为数字
5. **环节归属**：`by_stage` 中各 stage 名与节点函数名一致（如 `recall_column` 而非 `unknown`）；若出现 `unknown` 需在编码时修正祖先链解析
6. **开关关闭**：yaml 中 `features.usage_tracking: false` 重启后，同一请求无 usage 日志、无计量行为；改回 true 恢复
7. **全节点注入**：逐一在 9 个节点函数内打断点/加日志确认 `runtime.context["llm"]` 均非 None（编码时的自检步骤）
8. **思考模式验证**：用 DeepSeek 官方文档核实关闭思考模式的参数键名，实测 `extra_params` 透传生效（返回不含 reasoning 内容），并将结论回填本文 §2.3
9. **配置兼容**：`app_config.yaml` 的 `features:` 段临时删除后重启不崩溃（dataclass 默认值兜底）
10. **多厂商连通性**：三个 provider 各发一次最小请求（pytest 脚本或 /docs 手工），确认 base_url/api_key/model 组合均可用（Qwen/GLM 的确切 model 名在此步最终确认）

---

## 7. 风险与备注

1. **`callbacks` 构造参数 vs `config` 传参**：LangChain 不同版本对实例级 callbacks 的支持细节有差异（`ChatOpenAI(callbacks=...)` 为合法构造参数）。编码时若发现实例级 callback 未生效（summary 为空），降级方案：节点不改动，`QueryService` 在 `graph.astream(config={"callbacks": [tracker], ...})` 处挂载——LangGraph 会把 callbacks 传播到所有节点内部调用。此方案同样零侵入节点，已在设计中预留
2. **DeepSeek peak/offpeak 与缓存命中价的成本口径**：v1 成本计算一律按"输入=未命中价、输出=输出价"，缓存命中的节省体现在真实账单而非报告；03 文档会在报告口径说明中注明
3. **峰谷定价不在工厂处理**：`create_llm` 只负责模型实例；`pricing.tiers` 的档位判定（北京工作时间表 + 节假日）由 03 文档的成本模块按调用时间戳实现，工厂与 tracker 不感知档位
4. **`init_chat_model` vs 直接 `ChatOpenAI`**：工厂内部直接用 `langchain_openai.ChatOpenAI`（不经 init_chat_model 分发），少一层间接、报错更直白；`langchain.chat_models.init_chat_model` 不再被引用（llm.py 死导入清理）
5. **KV cache（已登记，勿在本模块修）**：见 §3.4 警示块——prompt 前缀不稳定导致厂商隐式缓存无法命中，属上下文管理（05）的改造范围；本模块若顺手改 prompt 结构会破坏 baseline 实验的对照组
6. **术语对齐**：05 文档中短期记忆的项目内命名为**轨迹（Trajectory）**（单次会话完整对话，随 thread_id 会话隔离），本模块不涉及，但 tracker 的 `by_stage` 聚合与轨迹无关（环节级 ≠ 会话级），编码时勿混淆两个统计口径
