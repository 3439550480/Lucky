# 03 · Agent 评估模块（M3）

> 状态：`final`（已评审定稿）　|　上位文档：[00_overview.md](00_overview.md)（final）、[01_llm_factory.md](01_llm_factory.md)（final）
> 修订记录：2026-09-21 初稿；同日定价口径修订——3 个物理模型，DeepSeek 峰谷为计费时段（`resolve_price_tier` 按调用时间自动判定，评测可 `--pricing-tier` 强制固定）；同日新增工具/能力触发指标（§3.9，skill+工具化预留）与评估子模块开关（`features.evaluation.*`）；2026-09-30 §3.3 比较口径修订为 v1.1（忽略列名，行内值排序比较——首版列名对齐导致正确性恒 0）；§3.8 退出码 3 与验收标准 1 的冲突按验收标准 1 执行（全 disabled → 空报告 + 退出码 0）
> 评估对象的数据结构已逐一核对：`DataAgentState`（retrieved_*/sql/error/result/intent）、`DWMySQLRepository.run()`（返回 `list[dict]`，自动 LIMIT 1000）。

---

## 1. 目标与边界

### 1.1 做什么

1. **评测集框架**：定义评测集 JSON Schema + 加载校验器（本轮交付骨架，样例条目留空占位、`enabled=false`，后续人工填充）
2. **三层质量指标**：检索（字段/指标/取值三路召回的 hit@k、MRR、precision@k、recall@k）、意图分类（准确率 + 混淆矩阵）、SQL（可执行率 + 结果集正确性）
3. **工具/能力触发指标**（§3.9）：量化"用户输入 → agent 是否正确调用数据搜寻能力"——v1 度量能力路由命中，skill+工具化（04 预留方向）落地后度量真实 tool_calls
4. **成本指标**：基于 01 的 `LLMUsageTracker` 明细——token 用量、按 provider 定价折算费用（DeepSeek 峰谷按调用时间自动判档）、环节级耗时分布
5. **评估子模块开关**：每个指标模块（retrieval/intent/sql/cost/tool）均可经 `features.evaluation.*` 配置独立开关（§5），关闭的模块在报告中标记 `disabled`
6. **评测执行器 runner**：逐用例 `graph.ainvoke`，提取最终 state + tracker 明细；支持 `--features` 运行时覆盖开关（含评估子模块开关，00 §3 联动机制）
7. **报告**：JSON + Markdown 双格式输出；`compare` 子命令支持多报告并排对比（模型 × 开关组合）
8. **CLI**：`app/scripts/run_evaluation.py`

### 1.2 不做什么

- 不自动生成评测数据（标注集人工构建；本轮骨架仅含格式与 1 条 `enabled=false` 的格式示例）
- 不引入 LLM-as-judge 类主观评分（v1 只做可判定的客观指标）
- 不做并发评测（LLM 限流风险，串行 + 用例间隔）
- 不改动主链路任何节点——runner 只读最终 state

### 1.3 依赖模块

- 上游：01（`create_llm` / `LLMUsageTracker` / providers 定价配置）
- 评估对象：现有问数子图（retrieved_* / sql / result / intent 字段）；04/05 完成后经 `--features` 对比新模块贡献

---

## 2. 配置 Schema

### 2.1 评测集 JSON Schema（`evaluation/datasets/eval_v1.json`）

```json
{
  "dataset_id": "eval_v1",
  "version": "1.0",
  "description": "电商问数评测集（骨架，样例待填充）",
  "defaults": {
    "match_mode": "exact",
    "k": 5
  },
  "cases": [
    {
      "id": "case_001_format_example",
      "enabled": false,
      "query": "上个月华北地区的GMV是多少",
      "capability": "dataquery",
      "expected": {
        "intent": "dataquery",
        "tools": ["dataquery.search"],
        "columns": ["dim_region.region_name", "fact_order.amount"],
        "metrics": ["GMV"],
        "values": ["华北"],
        "tables": ["fact_order", "dim_region"]
      },
      "golden_sql": "SELECT SUM(fo.amount) AS gmv FROM fact_order fo JOIN dim_region dr ON fo.region_id = dr.region_id WHERE dr.region_name = '华北' AND DATE_FORMAT(fo.order_date, '%Y-%m') = DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m')",
      "notes": "格式示例：enabled=false 时 runner 跳过；填充后删除此注释性说明"
    }
  ]
}
```

**字段定义与校验规则**：

| 字段 | 类型 | 必填 | 校验 |
|------|------|------|------|
| `dataset_id` | str | ✅ | 非空，报告引用 |
| `cases[].id` | str | ✅ | 数据集内唯一 |
| `cases[].enabled` | bool | ✅ | `false` 时 runner 跳过（骨架占位机制） |
| `cases[].query` | str | ✅ | 非空 |
| `cases[].capability` | str | ❌ | 缺省视为 `dataquery`（04 落地后用于按能力过滤统计） |
| `expected.intent` | str | ❌ | 缺省则该用例不计入意图指标 |
| `expected.tools` | list[str] | ❌ | 期望被调用的工具/能力标识（v1 取值如 `dataquery.search`，即"应触发问数能力"；缺省则不计入工具触发指标，§3.9） |
| `expected.columns` | list[str] | ❌ | 元素格式 `表名.字段名`（与 `ColumnInfo.id` 一致）；缺省则不计入字段召回指标 |
| `expected.metrics` | list[str] | ❌ | 与 `MetricInfo.name` 匹配 |
| `expected.values` | list[str] | ❌ | 字段真实取值（如"华北"），与 `ValueInfo` 的取值内容匹配 |
| `expected.tables` | list[str] | ❌ | 用于表级命中率（可选维度） |
| `golden_sql` | str | ❌ | 缺省则该用例不计入 SQL 正确性指标（仍计入可执行率） |
| `defaults.match_mode` | `exact` \| `contains` | ❌ | 缺省 `exact`；检索匹配规则见 §3.1 |
| `defaults.k` | int | ❌ | 缺省 5，hit@k / precision@k 的 k |

校验失败（缺 id、重复 id、类型错误）→ 加载器抛出带行号的明确错误，不静默跳过。

### 2.2 目录约定（新建）

```
evaluation/
├── datasets/
│   └── eval_v1.json        # 骨架（本轮交付）
└── reports/                # 报告输出（runner 自动创建）
```

### 2.3 CLI 参数定义（`app/scripts/run_evaluation.py`）

```powershell
uv run python -m app.scripts.run_evaluation `
  -d evaluation/datasets/eval_v1.json `     # 评测集路径（必填）
  -e baseline_5intent `                     # 实验标签（必填，报告文件名与对比维度）
  --provider deepseek `                     # LLM provider（缺省 = app_config.llm.default）
  --pricing-tier auto `                     # DeepSeek 计费档位：auto=按调用时间判定（缺省）/peak/offpeak；standard 档 provider 忽略此参数
  --features memory.short_term=false `      # 开关覆盖，可多次出现（key=value）
  --k 5 `                                   # 覆盖 defaults.k
  --limit 20 `                              # 只跑前 N 个 enabled 用例（调试用）
  --interval 2 `                            # 用例间隔秒数（限流保护，缺省 2）
  --compare evaluation/reports/xxx.json     # 可选：与已有报告并排对比输出
```

---

## 3. 接口与数据结构

### 3.1 检索指标（`app/evaluation/retrieval_metrics.py`）[NEW]

三路召回各自独立计算，被检集合来源（从最终 state 提取）：

| 通道 | state 字段 | 元素标识（与期望值匹配的键） |
|------|-----------|---------------------------|
| 字段 | `retrieved_column_infos` | `id`（格式 `表名.字段名`） |
| 指标 | `retrieved_metric_infos` | `name` |
| 取值 | `retrieved_value_infos` | `value`（取值内容；实体字段名编码时以 `entities/value_info.py` 为准） |

**匹配规则**：

- `exact`：期望元素与召回标识完全相等（大小写敏感）
- `contains`：召回标识包含期望串即命中（应对"GMV"vs"gmv（近30日）"这类命名差异；是否采用由数据集逐用例/全局声明，报告必须注明所用模式）

**指标定义**（设召回序列 `R = [r1, r2, ...]`（按相关性排序），期望集合 `E`，`hit(i)` = ri ∈ E）：

```
hit@k        = 1 if |{r1..rk} ∩ E| > 0 else 0
MRR          = 1/rank(第一个命中的期望元素)；无命中 = 0
precision@k  = |{r1..rk} ∩ E| / min(k, |R|)
recall@k     = |{r1..rk} ∩ E| / |E|
```

数据集级聚合 = 逐用例的宏平均（macro，每用例等权）。**任一通道期望集为空的用例不计入该通道指标**（分母只含有标注的用例）。

```python
def hit_at_k(retrieved: list[str], expected: set[str], k: int) -> float: ...
def mrr(retrieved: list[str], expected: set[str]) -> float: ...
def precision_at_k(retrieved: list[str], expected: set[str], k: int) -> float: ...
def recall_at_k(retrieved: list[str], expected: set[str], k: int) -> float: ...

def compute_retrieval_metrics(state: dict, expected: dict, k: int, match_mode: str) -> dict:
    """返回 {"columns": {...四指标...}, "metrics": {...}, "values": {...}, "tables": {...}}"""
```

纯函数、无 IO，可单测。

### 3.2 意图指标（`app/evaluation/intent_metrics.py`）[NEW]

```python
def compute_intent_metrics(cases_result: list[dict]) -> dict:
    """输入逐用例的 {"expected_intent": str, "actual_intent": str}（仅含双方都有的用例）
    返回 {"accuracy": float, "support": int, "confusion_matrix": {expected: {actual: count}}}"""
```

- 准确率 = 分类正确数 / 有标注用例数（support 为 0 时返回 `null` 而非 0，报告中显示"—"）
- 混淆矩阵按预期→实际计数输出，Markdown 报告渲染为表格
- **⚠️ v1 口径对齐（与能力路由一致）**：能力路由 v1 只有两个分流方向——`dataquery`（数据查询）/ `default`（其它）。评测集的 `expected.intent` 取值与两分流对齐；现状五分类体系跑 baseline 时按映射归并：`data_query/follow_up → dataquery`，`recap/chitchat/help → default`。因此 **v1 阶段意图指标与工具触发指标（§3.9）数值上等价**（同一次分流决策的两种视角），分化发生在 skill+工具化之后

### 3.3 SQL 指标（`app/evaluation/sql_metrics.py`）[NEW]

```python
def compute_sql_metrics(case_result: list[dict]) -> dict:
    """逐用例输入 {"has_golden": bool, "executed": bool, "exec_error": str|None,
                  "golden_rows": list[dict]|None, "actual_rows": list[dict]|None,
                  "retry_count": int, "sql": str}
    返回数据集级 {"executability": float, "correctness": float|null,
                 "avg_retries": float, "support_exec": int, "support_correct": int}"""
```

- **可执行率** = `run()` 成功的用例数 / 全部 enabled 且走完链路的用例数
- **结果集正确性**（仅有 `golden_sql` 的用例参与）：
  1. 分别执行 `golden_sql` 与 `state["sql"]`（均经 `DWMySQLRepository.run()`，受同样的 LIMIT 保护）
  2. **比较规则（v1.1 口径，2026-09-30 修订，报告中注明）**：**忽略列名与列序，行多重集相等**——行签名 = 行内值归一化后排序的元组（LLM 生成 SQL 的列别名不可控，首版"列名集合相等 + 列按名对齐"导致正确性恒为 0，已废弃）；值归一化——`Decimal/float` 按 `round(x, 4)` 比较、`datetime/date` 统一 `isoformat()`、其余 `str()`；**行序无关**；两结果集均为空视为正确（等价语义）。已知取舍：多列结果存在"两列语义互换仍判对"的理论误判可能，v1 接受
  3. 两结果集均为空视为正确（等价语义）；行数上限受 run() 的 1000 行保护，超出时报告标注"截断"
- **辅助统计**：平均重试次数（`retry_count`）、典型失败 SQL 摘录（每报告前 5 条 error）

### 3.4 成本指标（`app/evaluation/cost_metrics.py` + `app/evaluation/pricing.py`）[NEW]

```python
# ---- pricing.py：计费档位判定器 ----
def resolve_price_tier(ts: datetime, pricing: PricingConfig) -> str:
    """按调用时间戳判定计费档位。
    - pricing.tiers 仅一档（standard）→ 直接返回该档
    - tier_rules == "beijing_workweek" → 内置判定器：
        高峰(peak)   = 周一至周五 9:00–12:00、14:00–18:00（Asia/Shanghai 时区）
        空闲(offpeak) = 其余全部：工作日 12:00–14:00、18:00–次日9:00、周末全天、法定节假日全天
      节假日表从 evaluation/holidays.json 读取（人工维护，格式 {"2026": ["2026-01-01", ...]}）；
      当年未维护节假日表时按普通周末规则判定并打 warning
    - 强制档位（runner --pricing-tier peak/offpeak）优先于时间判定
    """

# ---- cost_metrics.py ----
def compute_case_cost(tracker_summary: dict, provider_cfg: LLMProviderConfig,
                      forced_tier: str | None) -> dict:
    """单用例成本。
    逐条 LLMCallRecord 按 record.ts 判定档位（forced_tier 非空则恒用该档），
    tier 单价：cost_input = input_tokens/1e6 * tier.input
              cost_output = output_tokens/1e6 * tier.output
    输出：{"input_tokens","output_tokens","total_tokens","latency_ms",
           "cost_input","cost_output","cost_total","tier_distribution": {"peak": n, "offpeak": m}}
    档位单价缺失（null）→ 对应费用字段 None 并标注"未配置单价"（00 §5 约定）
    """

def aggregate_costs(case_costs: list[dict]) -> dict:
    """数据集级：total_cost、avg_cost_per_case、token 分布、
    by_stage 聚合（意图/扩词×3/过滤×2/生成/修正/解释 各环节 calls 与 latency）、
    tier_distribution 汇总（峰谷调用占比）"""
```

- 定价来自 `app_config.llm.providers[provider]`；`price_*` 为 null 时费用字段输出 `null` 并标注"未配置单价"（00 §5 约定）
- **环节归属**直接使用 tracker 的 `by_stage`（chain 祖先名），聚合时对齐节点名

### 3.5 评测集加载（`app/evaluation/dataset.py`）[NEW]

```python
@dataclass
class EvaluationCase:
    id: str
    query: str
    capability: str
    expected: dict          # intent/columns/metrics/values/tables，缺省项为空
    golden_sql: str | None
    match_mode: str         # 继承 defaults
    k: int                  # 继承 defaults
    notes: str

@dataclass
class EvaluationDataset:
    dataset_id: str
    version: str
    description: str
    cases: list[EvaluationCase]     # 仅含 enabled=True 的用例；disabled 仅计数

def load_dataset(path: Path) -> EvaluationDataset:
    """json.load + 全量校验（§2.1 规则），错误带行号抛 ValueError"""
```

### 3.6 评测执行器（`app/evaluation/runner.py`）[NEW]

```python
@dataclass
class CaseResult:
    case: EvaluationCase
    status: str                     # "ok" | "graph_error" | "skipped"
    final_state: dict | None        # graph.ainvoke 返回的最终 state
    tracker_summary: dict | None
    sql_metrics_input: dict
    error: str | None

class EvaluationRunner:
    def __init__(self, dataset: EvaluationDataset, provider: str | None,
                 feature_overrides: dict[str, str], interval: int,
                 pricing_tier: str | None = None): ...   # None=auto 按时间判定；peak/offpeak=强制固定

    async def run(self) -> list[CaseResult]:
        """流程：
        1. apply_feature_overrides(feature_overrides)   # 见 §3.7
        2. init 五个客户端 manager（复用 build_meta_knowledge.py 的模式）
        3. 逐用例：
           - 新建 AsyncSession → 组装 repositories → create_llm(provider, tracker) → DataAgentContext
           - state 初始值与 query_service.query() 保持一致（messages=[] 显式初始化）
           - thread_id = f"eval_{dataset_id}_{case.id}"（会话隔离，用例间零污染）
           - graph.ainvoke(input=state, context=context, config={"configurable": {"thread_id": ...}})
           - 提取 final_state 的 intent / retrieved_* / sql / result / error / retry_count
           - tracker.summary() → CaseResult
           - await asyncio.sleep(interval)
        4. finally：关闭全部 manager 连接"""

def apply_feature_overrides(overrides: dict[str, str]) -> None:
    """把 --features key=value 应用到 app_config.features（运行时 mutation，
    仅存在于评测进程，不影响在线服务与配置文件——00 §3.4 约定）。
    key 用点路径（如 memory.short_term），value 按目标字段类型转换（bool/int/str）。
    约定：runner 始终强制 features.usage_tracking=True（成本指标数据源）"""
```

- **每次 ainvoke 后**：`graph` 的 `InMemorySaver` 会积累 checkpointer 状态——runner 在长评测中无需清理（内存级、进程退出即释放），但用例数 >100 时建议分批（文档注明，v1 不实现）
- 用例失败（图抛异常）不中断整场评测：记 `status="graph_error"` + error 摘要，继续下一用例

### 3.7 报告（`app/evaluation/report.py`）[NEW]

```python
def render_json(results: list[CaseResult], dataset: EvaluationDataset,
                meta: dict) -> Path: ...
    # 输出 evaluation/reports/{exp_label}_{YYYYMMDD_HHMMSS}.json
    # 结构：{meta: {exp_label, provider, dataset_id, feature_overrides, started_at, duration_s,
    #                app_version_commit},   # git commit 用 subprocess 获取，失败则为 null
    #        aggregate: {quality: {...三节 §3.1-3.3}, cost: {...§3.4}},
    #        cases: [{id, status, quality_*, cost_*, sql, error 摘要}]}

def render_markdown(...) -> Path:
    # 同名 .md，人类可读版：
    # 1. 实验信息表（标签/provider/开关覆盖/用例数）
    # 2. 质量指标表（检索三通道 × 4 指标 + 意图准确率 + SQL 两率）
    # 3. 成本表（总费用/每问费用/token 分布）
    # 4. 环节成本表（by_stage：calls/tokens/latency）
    # 5. 失败用例清单（error 摘录）

def compare_reports(paths: list[Path]) -> Path:
    # 读取多份报告 JSON，输出对比报告（同名 _compare.md + .json）：
    # 行 = 实验标签（含 feature_overrides 摘要），列 = 关键指标
    # （检索 hit@5/MRR、意图准确率、SQL 可执行率/正确性、每问成本、总耗时）
```

### 3.8 `app/scripts/run_evaluation.py` [NEW]

argparse（§2.3 参数）→ `load_dataset` → `EvaluationRunner.run()` → `render_json/render_markdown` →（可选）`compare_reports` → 控制台打印指标摘要表。退出码：0 = 完成（含有用例失败也算完成）；2 = 数据集加载失败；3 = 无 enabled 用例。

### 3.9 工具/能力触发指标（`app/evaluation/tool_metrics.py`）[NEW]

**度量的问题**：给定用户输入，agent 是否**主动、正确地**调用了数据搜寻能力（而非漏调或误调）——这是 skill+工具形态下 agent 自主性的核心量化指标。

```python
def compute_tool_metrics(cases_result: list[dict]) -> dict:
    """输入逐用例 {"expected_tools": list[str]|None, "invoked_tools": list[str]}
    返回 {"invocation_accuracy": float,   # 期望与实际工具集完全一致的用例占比（v1 主指标）
          "tool_recall": float,           # |期望∩实际| / |期望|（漏调率 = 1 - recall，如该问数却闲聊）
          "tool_precision": float,        # |期望∩实际| / |实际|（误调率视角，如该闲聊却问数）
          "support": int,
          "confusion": {"missed": int, "false_positive": int, "correct": int}}
    """
```

**invoked_tools 的来源（两阶段演进，接口不变）**：

| 阶段 | invoked_tools 提取方式 | 说明 |
|------|----------------------|------|
| v1（现在） | 最终 state 的 `intent`/路由结果映射：`data_query/follow_up → ["dataquery.search"]`，其他 → `[]` | 能力路由层尚未 skill 化，"是否调用数据搜寻能力" = 路由是否选对 dataquery |
| skill+工具化后（04 预留方向落地） | 最终 state 的新增字段 `tool_calls: list[str]`（记录 agent 实际调用的工具序列，04 文档定义） | 度量升级为真实工具调用行为；期望集写法不变（`expected.tools`），数据集无需重标 |

**工具命名规范**：`{capability}.{action}` 格式（如 `dataquery.search`）；04 定义工具注册表时沿用，评估的期望集与工具注册表使用同一命名空间，避免两套名字。

**与意图指标的分工（v1 = 同源等价）**：当前确认的意图分流只有两个方向——数据查询（dataquery）/ 其它（default），所以 v1 的"主动与正确性"就是这两分流的路由命中情况：`intent_metrics` 与 `tool_metrics` 数值等价，分别从"分类视角"和"调用视角"呈现同一决策。二者真正分化发生在 skill+工具化之后：届时 agent 可能分类正确却选择不调用工具（或反之），tool_metrics 凭 `state["tool_calls"]` 独立发力，意图分类也可能演化为多级（能力选择 → 能力内子意图）。
**口径补充（04 tier-0 芯片，2026-09-23）**：runner 不携带 capability 字段（恒走自动路由），`tool_metrics` 只统计 `capability_source ≠ "user"` 的自动路由行为——用户显式选择是确定性操作，不属于"agent 主动性"的度量范围。

---

## 4. 对现有代码的改动点清单

| # | 文件 | 操作 | 内容 |
|---|------|------|------|
| 1 | `app/evaluation/__init__.py` | [NEW] | 包声明 |
| 2 | `app/evaluation/dataset.py` | [NEW] | §3.5 |
| 3 | `app/evaluation/retrieval_metrics.py` | [NEW] | §3.1（纯函数） |
| 4 | `app/evaluation/intent_metrics.py` | [NEW] | §3.2 |
| 5 | `app/evaluation/sql_metrics.py` | [NEW] | §3.3（结果集比较逻辑） |
| 6 | `app/evaluation/cost_metrics.py` | [NEW] | §3.4（费用计算） |
| 6d | `app/evaluation/tool_metrics.py` | [NEW] | §3.9（工具/能力触发指标） |
| 6b | `app/evaluation/pricing.py` | [NEW] | §3.4（`resolve_price_tier` 时段判定器，含 beijing_workweek 规则） |
| 6c | `evaluation/holidays.json` | [NEW] | 法定节假日表（人工按年维护，格式 `{"2026": ["2026-01-01", ...]}`；骨架含 2026 年占位） |
| 7 | `app/evaluation/runner.py` | [NEW] | §3.6（含 `apply_feature_overrides`） |
| 8 | `app/evaluation/report.py` | [NEW] | §3.7 |
| 9 | `app/scripts/run_evaluation.py` | [NEW] | CLI（§2.3、§3.8） |
| 10 | `evaluation/datasets/eval_v1.json` | [NEW] | 骨架（§2.1，1 条 `enabled=false` 格式示例） |
| 11 | `evaluation/reports/.gitkeep` | [NEW] | 报告目录占位 |
| 12 | `pyproject.toml` | [MODIFY] | 无新依赖（标准库 + 已有依赖即可；`matplotlib` 等可视化明确不引入） |
| 13 | `app/agent/*`、`app/services/*`、`frontend/*` | **不变** | 评估只读 state 与 tracker，零侵入（00 红线 #4） |

> 说明：评估代码与 `app/scripts/build_meta_knowledge.py` 的客户端初始化模式保持一致（同一套 manager 单例），不引入新的连接管理方式。

---

## 5. Feature Flags 开关语义

### 5.1 评估子模块开关（`features.evaluation.*`，00 §3 定义）

| 开关 | 开（默认） | 关 |
|------|-----------|-----|
| `evaluation.retrieval_metrics` | 计算检索三通道 hit@k/MRR/P/R | 报告该区标记 `"disabled": true`，不计算（被检 state 字段仍提取，供明细排查） |
| `evaluation.intent_metrics` | 计算意图准确率 + 混淆矩阵 | 同上 |
| `evaluation.sql_metrics` | 计算 SQL 可执行率/正确性 | 同上（可执行性判定跳过，golden_sql 不执行） |
| `evaluation.cost_metrics` | 计算 token 折算费用/环节耗时 | 费用区 `disabled`；**token 计数仍采集**（tracker 不受影响，后续重新打开可回算） |
| `evaluation.tool_metrics` | 计算工具触发正确性 | 同上 |

- 关闭语义 = **不计算、不呈现**，绝不输出 0 分（0 分会被误读为"表现差"）
- 所有子模块开关同样支持 `--features evaluation.cost_metrics=false` 运行时覆盖
- runner 汇总各开关状态写入报告 meta 的 flags 快照

### 5.2 Agent 行为开关

| 开关 | 评估中的行为 |
|------|-------------|
| `usage_tracking` | **runner 强制 override 为 true**（无论配置文件），保证成本指标数据源；报告 meta 记录该强制项 |
| `memory.short_term` / `capability_routing` / `rules_fast_path` / `context_management` | 由 `--features` 按实验设计覆盖；**未覆盖的开关遵循配置文件当前值**，报告 meta 必须完整记录最终生效的 flags 快照（否则对比实验不可复现） |

**典型实验命令**（Feature Flags 联动的首个实战，05 落地后执行）：

```powershell
# memory_off 组
uv run python -m app.scripts.run_evaluation -d evaluation/datasets/eval_v1.json -e memory_off --features memory.short_term=false
# memory_on 组
uv run python -m app.scripts.run_evaluation -d evaluation/datasets/eval_v1.json -e memory_on  --features memory.short_term=true
# 并排对比
uv run python -m app.scripts.run_evaluation -d evaluation/datasets/eval_v1.json -e compare --compare evaluation/reports/memory_off_xxx.json evaluation/reports/memory_on_xxx.json
```

---

## 6. 验收标准

1. **空数据集跑通**：`eval_v1.json` 仅含 `enabled=false` 示例时，全流程完成、生成 JSON + Markdown 报告（各指标 support=0，显示 null/"—"），退出码 0
2. **加载校验**：构造非法数据集（重复 id / 缺 query / 类型错误）→ 报错信息含行号与字段名，退出码 2
3. **指标正确性（单测级验证，编码时以 pytest 内联脚本验证后可删）**：
   - `hit_at_k(["a","b","c"], {"b"}, 3) == 1`；`mrr(["a","b"], {"b"}) == 0.5`
   - `precision_at_k(["a","x","b"], {"a","b"}, 3) == 2/3`；`recall_at_k(["a"], {"a","b"}, 5) == 0.5`
   - 结果集比较：行序打乱 → 正确；列序/别名不同（仅列名差异）→ 正确；`1.00001` vs `1.00002`（round 4）→ 正确；行数不同 → 不正确
4. **真实用例端到端**（数据集填充 ≥3 条 enabled 用例后）：报告 quality 区出现非 null 的三通道指标、意图准确率、SQL 可执行率；cost 区出现费用（provider 有定价时）且 `by_stage` 含 `intent_classify`/`generate_sql` 环节
5. **开关覆盖生效**：`--features memory.short_term=false` 后报告 meta 的 flags 快照中该值为 false；在线配置文件未被修改
6. **对比报告**：`--compare` 两份报告输出对比表，行 = 实验标签，列 = §3.7 定义的关键指标
7. **失败容错**：中途某用例图执行抛异常（如构造一条必失败的 query）→ 记录 `graph_error`，其余用例继续，报告含失败清单
8. **可复现性**：同一数据集 + 同一 provider + 同一 flags 快照重跑，指标结构一致（LLM 非确定性导致的数值波动属预期，报告 meta 含 dataset_id/provider/flags/commit 足以追溯环境）
9. **评估子模块开关**：`--features evaluation.cost_metrics=false` 后报告成本区显示 `disabled` 且无费用数字（token 明细仍采集）；`evaluation.retrieval_metrics=false` 后质量区检索部分标记 disabled；恢复后正常计算
10. **工具触发指标（v1）**：数据集含 `expected.tools: ["dataquery.search"]` 的用例——路由选中 dataquery 的用例 `invocation_accuracy` 计为命中；构造一条闲聊 query（期望 tools 为空）验证"误调"计数；skill 化改造后本指标自动切换到 `state["tool_calls"]` 数据源，数据集无需重标

---

## 7. 风险与备注

1. **`expected.columns` 与实体 `id` 的格式耦合**：`ColumnInfo.id` 现为 `表名.字段名`（核对自实体定义注释），若后续实体变更需同步数据集标注口径——加载器不做格式强制（避免过度耦合），匹配不到时指标自然为 0，报告的 cases 明细可排查
2. **`contains` 匹配模式是权宜方案**：解决指标命名差异（"GMV" vs "gmv（近30日）"），长期应统一标注口径或引入同义词表；报告必须展示所用模式防止误读
3. **DeepSeek 峰谷按调用时间自动判定**（物理模型仅 1 个，不计入模型对比维度）：报告 meta 记录 `tier_distribution`；实验需要固定档位对比时用 `--pricing-tier peak/offpeak` 强制（否则真实时间分布会引入成本噪声）；判定器依赖节假日表，未维护的年份按普通周末规则回退并打 warning
4. **缓存命中价 v1 不参与核算**（01 §7.2 口径）：真实账单中 DeepSeek 缓存命中的节省不会体现在报告费用里；05 改造 KV cache 后，若要量化收益需在 03 增补"按命中比例估算"逻辑（预留，不在本轮）
5. **LLM 非确定性**：temperature=0.7 下同一用例多次运行结果可能不同；做严格对比实验时建议同用例跑 3 次取多数（v1 不实现，登记为增强项）
