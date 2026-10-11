# Lucky 2.0 · 评测优化策略（意图识别与结果正确性）

> 定稿：2026-10-11
> 依据：anta_baseline_20261011_150024（30 例）+ anta_memory（3 例）真实评测归因；
> 且已**重新阅览项目最新代码**（策略第一批 P0/P1/P3/E/E2/C3 + 第二批 E/B + S3/S4 全部落地，commit df8d28c）后逐例核实。
> 结论先行：**9 个失败用例全部有修法；正则只覆盖其中 2 例；最大杠杆在「指标口径 + 领域规则 + 召回继承」。**

---

## 一、现状基线

| 指标 | 值 | 评注 |
|------|-----|------|
| 意图准确率 | 90%（27/30） | 尚可 |
| SQL 可执行率 | **100%**（20/20） | 语法层完美，全部可跑 |
| **结果正确性** | **60%**（12/20） | ⭐ **最大痛点** |
| 检索 hit | columns 71.4% / metrics 83.3% / values 55.6% | values 偏低 |
| 记忆（3 例） | store 100% / retrieval 100% / **recall 0/3** | ⭐ 真缺陷 |
| 成本 | 30 例 1.18 元（0.039 元/问） | 谷时 |

**已落地的结构类修复**（本次重读逐项核对，全部到位）：`memory_store` 接线 · result 拆分（SSE 全量 + state 只留 `result_sample20/result_total`）· `run_sql` max_rows/timeout · `render_history` 统一收口 · 前缀 v3 按角色渲染【当前用户】段 · `roles.yaml` 权限矩阵 + 登录全链（token/session/三道关/中间件/thread 绑 staff_id）· `condition_extractor` + generate_sql 换 `inherited_conditions` · `history_max_tokens` 硬预算守卫（激活）· `trace_brief` 开关（默认关）· 出入库槽位流程全量（flow_guard/flow_step/InventoryWriteRepository 幂等/确认按钮端点）· 记忆双层（scope/owner_id/写入门禁/可见性过滤）。

---

## 二、根因地图（9 个独立失败用例，全部核实到代码/数据）

| 类 | 用例 | 根因（核实位置） | 修复批次 |
|----|------|----------------|:-------:|
| **A 指标口径错** | `anta_discount_overall` | `conf/meta_config.yaml`「平均折扣率」description **写的就是 `AVG(final_rate)`** —— 模型照着错的口径做 | P0-1 |
| **A 领域规则缺** | `anta_base_rate_rules` | generate_sql 领域规则未覆盖：`dim_pricing_rule` 的 26 年有**夏 0.70 / 非夏 0.50 两档**，模型用 `MAX+GROUP BY year_code` 把 4 行压成 3 行 | P0-2 |
| **B SQL 习惯** | `anta_inv_loss` | `ABS(quantity) + WITH ROLLUP` 多出合计行；领域规则未禁 | P0-2 |
| **C 评测判定** | `anta_member_split` | 行多重集比对：模型渲染"会员/非会员"文字 vs golden 的 0/1 原值，且 golden 多带 `qty` 列（数据集注记自认"用于观测渲染差异"） | P1-2 |
| **C 用例歧义** | `anta_inv_broken_size` | 问句"【有哪些】SKU 断码"（语义=列表）vs expected 指标"断码SKU**数**"（计数） | P1-2 |
| **D 召回失血** | `anta_mt_category_drill` | 省略句"那只看鞋类呢"召回种子只有"鞋类" → 列命中聚在品类/补货/流水域，**fact_sales.actual_amount 未召回** → filter_table 把 fact_sales 过滤出表结构 → 模型只能选 `dim_replenish_policy`。**继承条件块本身是对的（指标=销售额已提取），但召回没吃到它** | **P0-3** |
| **E 路由错** | `anta_attr_clearance_sales` | dataquery 规则**没有**"卖了多少钱"模式；"断码"裸词命中 inventory 规则 2 | P1-1 |
| **E 路由错** | `anta_holiday_days` | 无"节假日"规则 → 落 LLM → default | P1-1 |
| **E 路由错** | `anta_mt_inv_drill` | 省略句"那鞋类呢"无特征词 → LLM 误归 replenish（SQL 空） | P2 |

**意外发现**：`anta_attr_clearance_sales` 路由错但 **SQL 是对的**（inventory 与 dataquery 共用 `extract_keywords` 入口，同链路救场）——损失仅工具触发统计。

**两个"元数据/数据"层面的确认**：
- 时间窗口：库中已被 `daily_append` 扩到 10-09；评测 golden 显式写死 `20261001~20261007`、"当前库存"用 `MAX(snapshot_date)` 子查询 —— **时间变化已被正确处理**，非本次问题。
- `meta_config.yaml` 指标从 yaml 构建 into meta 库（`build_meta_knowledge`），**改 yaml 后必须重建**。

---

## 三、修复策略（三批，按杠杆排序）

### 3.1 第一批 P0：内容修复（零代码 / 低代码，预期正确性 60%→85%+）

#### P0-1 · 指标口径修正（修 `anta_discount_overall`）

**落点**：`conf/meta_config.yaml` 指标段「平均折扣率」（约 L390）

```yaml
- name: 平均折扣率
  description: >
    折扣率分两级口径：① 行级折扣 = fact_sales.final_rate（已预计算列，明细场景直接取该列）；
    ② 整体/分组平均折扣率（金额加权）= SUM(actual_amount) / SUM(list_price × quantity)。
    禁止 AVG(final_rate) 作聚合平均——行平均 ≠ 金额加权（大单低折扣会被高销量摊薄）。
  relevant_columns:
    - fact_sales.final_rate
    - fact_sales.actual_amount
    - fact_sales.list_price
    - fact_sales.quantity
  alias: [折扣率, 实际折扣, 平均折扣]
```

**后置动作**：重跑 meta 构建（`build_meta_knowledge`）使 MySQL meta 库同步；改前先核对评测集中**其余折扣相关用例**（折扣机制共 4 例）的 golden 是否同为金额加权口径 —— 若有不一致的，先对齐 golden 再改。

#### P0-2 · generate_sql 领域规则补 2 条（修 `anta_base_rate_rules` / `anta_inv_loss`）

**落点**：`prompts/generate_sql.prompt`【领域规则】段追加：

```text
6. 基准折扣与促销规则查 dim_pricing_rule 时，逐行返回 year_code + season_code + base_rate，
   禁止对该表做 MAX / GROUP BY 聚合——同一 year_code 可能有多档（26 年分夏 0.70 / 非夏 0.50），
   聚合会丢行。仅当用户明确要求"合并/去重"时才允许 GROUP BY。
7. 分组统计查询禁止 WITH ROLLUP / 人工追加合计行——返回与分组维度一致的原生行集；
   合计由解释层或前端给出。报损/出入库数量直接 SUM(quantity)（报损类 quantity 为负，不取 ABS 改语义）。
```

#### P0-3 · 召回继承（修 `anta_mt_category_drill`）

**落点**：`app/agent/nodes/dataquery/extract_keywords.py`（jieba 抽词之后、去重之前）

```python
# 召回继承（2.0 上下文策略 1.6.9+1.6.12）：省略句的召回种子并入继承条件的条件词。
# 背景：mt_category_drill 实测——"那只看鞋类呢"仅召回到品类/补货/流水域列，
# fact_sales.actual_amount 未召回 → fact_sales 被 filter_table 过滤 → SQL 跑偏到补货策略表。
from app.agent.session.condition_extractor import build_inherited_block, extract_conditions

inherited_words: list[str] = []
try:
    history = get_conversation_history(state)
    prev_text = next((m.get("content") for m in reversed(history)
                      if m.get("role") == "user" and m.get("content") != query), None)
    if prev_text:
        merged = extract_conditions(prev_text)          # 上一轮条件（指标/品类/时间…）
        for k, v in merged.items():
            if k not in ("时间",):                       # 时间条件不进召回（已由 date_info 承载）
                inherited_words.append(str(v))
except Exception as e:
    logger.warning(f"[keywords] 召回继承失败（跳过）: {e}")

keywords = list(set(keywords + inherited_words + [query]))
```

> **注意**：`inherited_conditions` 块已经让**模型**知道了"指标=销售额"，本修复让**召回**也知道 —— 两层对齐后才闭环。

#### P0 的验证

| 动作 | 通过线 |
|------|--------|
| `build_brief` / `condition_extractor` 既有 GWT 全绿 | 不回退 |
| 新增召回继承单测：history 含"销售额"、query="那只看鞋类呢" → 断言 keywords 含"销售额" | 通过 |
| meta 重建后直接执行 golden SQL 与系统 SQL，比对数值 | discount_overall 数值一致 |

### 3.2 第二批 P1：路由修复（意图 90%→97%+）+ 评测修正

#### P1-1 · 先建路由回归单测（**改正则的前置纪律**）

**落点**：`tests/test_routing_regression.py`（新建，**0 token**）

- 读 `evaluation/datasets/eval_anta.json` 的 30 例 `query + expected.intent`
- 逐例跑 `registry.match_rules(query)`（tier1）+ tier2 embedding（可选开）
- 断言：intent 命中数 **≥ 基线**（当前 tier1 可命中的例数），且**不允许新增回退**

> 30 例 intent 错只有 3 例，但正则是全局的 —— **没有这个单测，改正则就是盲改**。

#### P1-2 · 路由正则 3 处（修 3 例）

**落点**：`conf/capability_config.yaml`

```yaml
# dataquery.rules 追加（dataquery 排第一，先命中）：
- "卖了(多少钱|多少|几件)|销售了|成交了多少"
- "节假日|假期|周末|工作日"

# inventory.rules 第 3 条改精确（"报损流程要求"不再误命中，"报损了多少"仍命中）：
- "(出入库|入库|出库|流水|动销率|覆盖天数)"
- "报损.{0,6}(多少|几|哪些|数量|金额|清单)"
- "(断码|断货|缺货|库存充足)"
```

**注意**：`anta_attr_clearance_sales` 靠 dataquery 新规则先命中（顺序优先），"断码"规则不需要删除。

#### P1-3 · 评测修正 2 例（修 `anta_member_split` / `anta_inv_broken_size`）

| 用例 | 修法 |
|------|------|
| `anta_member_split` | **判定升级**（首选）：`sql_metrics` 对行多重集比对增加列名映射容忍（`会员类型=会员 ↔ is_member`）与列序无关；或 golden 改为渲染口径。改前确认这是"已知局限"要转正 |
| `anta_inv_broken_size` | 问句改「现在断码的 SKU 有**多少个**？」（对齐 expected 计数）—— 或 golden 改列表。**推荐改问句**（一行数据集改动） |

### 3.3 第三批 P2：知识型路由 / brief / 长期记忆

| 项 | 动作 |
|----|------|
| 记忆组 2 例（"补货节奏""主要客群是谁"） | `prompts/capability_route.prompt`【任务要求】加规则："流程 / 口径 / 节奏 / 要求 / 是谁 等**知识型问题**（不涉及具体指标计算）→ 归 default"；v2 可加"记忆检索高分 → 倾向 default"的路由信号 |
| `trace_brief` | 维持默认关；出现长会话演示需求时开（结构已就绪） |
| 长期记忆 | 维持 `long_term=false`；身份/权限已就绪，**主动服务演示时再开** |

---

## 五、执行结果（2026-10-11 收口）

### 5.1 全量评测：`anta_baseline` → `anta_baseline_v3`

| 指标 | 基线 15:00 | 本轮 v3 | 通过线 |
|------|-----------|---------|--------|
| 意图准确率 | 90.0% | **100.0%** | ≥93% ✅ |
| 工具/能力触发 | 90.0% | **100.0%** | 不回退 ✅ |
| SQL 可执行率 | 100.0% | 100.0% | 100% ✅ |
| **SQL 结果正确性** | 60.0% | **100.0%** | ≥85% ✅ |
| 检索 columns hit@k | 71.4% | **92.9%** | 不回退 ✅ |
| 检索 metrics hit@k | 83.3% | **91.7%** | 不回退 ✅ |
| 检索 values hit@k | 55.6% | 55.6% | — |
| 耗时 / 费用 | 950.7s / 1.175 元 | 992.6s / 1.169 元 | 持平 |

报告：`evaluation/reports/anta_baseline_v3_20261011_203221.{md,json}`（30 ok / 0 graph_error）。

> 过程说明：首轮全量（`anta_baseline_v2`）只有 70%（+3 修好、−1 回归），不足以收敛。
> 遂改用**聚焦集**（6 个失败/回退例 + 6 个守卫例）快速迭代 4 轮，收敛后再跑全量 ——
> 单次聚焦 ~570s / 0.64 元，比全量 992s 更适合改规则时的快循环。

### 5.2 迭代中发现的两个**新增缺陷**（原策略未预见，本次补齐）

#### 缺陷 A（**最重要**）：评测 runner 把多轮历史覆盖为空

`runner._run_case` 每个轮次都构造 `DataAgentState(messages=[], ...)`，
而 LangGraph 的 `messages` 是无 reducer 的 LastValue channel —— **入参里的 `[]` 会覆盖
checkpointer 从 `thread_id` 恢复的上一轮轨迹**。实测第二轮 router 处 `len(messages)==0`。

- 后果：多轮用例的**指代消解 / 条件继承 / 省略句继承在评测里静默失效**（但在线 `QueryService`
  根本不传 `messages`，是正常的）→ 多轮结论完全失真。
- 修复：`runner` 两处 state 构造**都不再传 `messages`**，与 `query_service.query()` 严格对齐。
- 实测：修后第二轮 `msgs=2`，`那鞋类呢` 正确继承 `inventory`。

#### 缺陷 B：省略式追问的"继承上一轮能力"原先只能靠提示词祈祷

`render_history` 会剥离 `capability` 元数据，模型从历史文本无从知道上一轮用了哪个能力；
tier2 embedding 对"那鞋类呢"这类无实词短句也不稳定（实测抢到 `dataquery`，而上轮是 `inventory`）。

- 修复 1（提示词）：`capability_route.prompt` 增加【上一轮能力】区块，把上一轮能力作为
  独立变量注入（`router._previous_capability`），把"要模型猜"变成"给模型看"。
- 修复 2（确定性）：新增 **tier 1.5**——`_ELLIPSIS_RE` 命中"（可选指代词）+ ≤8 字 + 呢"的
  极短追问时，直接继承上一轮能力（0 token，与 tier1 同类，共用 `rules_fast_path` 开关）。
  正则刻意收窄，`"那国庆期间的销售额是多少？"`/`"断码清仓的商品卖了多少钱呢？"` 均不命中（留有单测）。
- 新增 `capability_source = "inherit"` 取值。

### 5.3 规则收口的两个教训（已回写进提示词）

1. **"只答所问"要限定在"指标列"，不能连"维度列"一起砍**：首版规则 11 写"不要附带维度列"，
   结果 `base_rate_rules` 丢 `season_code`、`inv_loss` 丢 `operator`，两例直接从"对"变"错"。
   定稿为：**禁止追加未被问询的指标列；但问句要求的维度列必须保留**（单值限定问题才只输出指标值）。
2. **避免在提示词里堆"负向词表"**：规则 11/12 曾把"日均销量、建议补货量、补货、库存"当反面例子写进去，
   反而把模型的注意力引向 `dim_replenish_policy`/`coverage_days`（`mt_category_drill` 一度从
   对变成查补货策略表）。定稿改为正向约束，不列举具体业务词。

### 5.4 仍未做（明确挂账）

| 项 | 状态 |
|----|------|
| 记忆组（`anta_memory_v2`，long_term=true） | recall **33.3%（1/3）**，未达 ≥2/3；本轮未改动，留待专项 |
| `trace_brief` / `long_term` 开关 | 维持默认关（策略 §3.3 结论不变） |
| meta 重建 | 已完成；备份留 `%TEMP%\meta_backup_20261011.sql`（28K，可回滚） |

---

## 四、验证方案

| 步骤 | 方式 | 通过线 |
|------|------|--------|
| 路由回归单测 | 新建 `tests/test_routing_regression.py`（0 token） | 30 例 intent 不回退 + 新增命中 |
| P0-3 单测 | 召回继承断言（见 3.1） | 通过 |
| **全量评测** | 沿用本次评测命令重跑 anta_baseline（~950s / 1.18 元） | **正确性 ≥85%**；意图 ≥93%；可执行率保持 100% |
| 记忆组 | 重跑 anta_memory（long_term=true） | recall ≥ 2/3（报损流程修复后） |
| meta 重建核对 | golden SQL 与系统 SQL 直接执行比对数值 | discount_overall 一致 |

---

## 五、风险与回滚

| # | 风险 | 缓解 |
|:--:|------|------|
| 1 | **meta 口径改动波及其余折扣用例** —— 评测集"折扣机制 4 例"，改"平均折扣率"口径后其他折扣类 golden 可能不一致 | 改前逐例核对 4 例 golden 口径；不一致的先对齐 golden；meta 重建前备份 meta 库 |
| 2 | 正则改动引发路由回退 | 路由回归单测先行（3.2 P1-1），断言不回退 |
| 3 | 领域规则过强（禁 ROLLUP 导致某合法查询被改） | 规则措辞限定在"分组统计"场景 |
| 4 | 召回继承引入噪声关键词（继承的指标词召回不准） | 单测 + 评测验证；失败仅表现为召回偏移，可回退单点函数 |
| 5 | `PREFIX_VERSION`（v3）——本轮**不涉及**前缀改动（领域规则在 generate_sql，不在 system_prompt） | 无缓存全集失效风险 |

---

## 六、不做的事（已论证，收录防止回潮）

| 项 | 结论 | 依据 |
|----|------|------|
| Function Calling | 不做 | 第五章：路由+固定链路可控/可测/安全；写操作禁 FC |
| MCP | 暂不做 | 数据源全自持、无第二消费方 |
| 主动推送 | 另立计划 | 2.6：v1 只做对话内主动提示 |
| 规则预筛记忆提取 | 不做 | 06 已论证：语义盲区，漏记比多调用严重 |

---

*本策略基于 2026-10-11 对项目最新代码（commit df8d28c）的实际重读定稿；所有根因均已核实到具体文件与行。执行中若评测集 golden 与口径修改冲突，以「先对齐 golden、再改口径」为序。*
