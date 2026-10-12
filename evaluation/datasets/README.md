# 评测数据集 · 八场清单与机制覆盖矩阵

> 设计源：`docs/specs/Lucky2.0-评测体系升级方案.md`（S3 分场）
> 索引口径日期：2026-10-12
> 核心约定：**一场 = 一个独立数据集文件，独立通过线**。这样"机制有没有生效"是报告里的一个达标结论，而不是只能靠人读表格。

## 一、场清单

| 场 | 数据集文件 | 用例 | 通过线 | 本场在守什么 |
|---|---|---|---|---|
| F1 路由对抗场 | `f1_routing.json` | 12 | 0.90 | 能力路由是否被"关键词诱导"带偏（问句里有"库存/补货"字样但真意是查数） |
| F2 多轮上下文场 | `f2_context.json` | 12 | 0.83 | 指代/省略是否被正确继承；**条件被推翻后旧约束是否被清掉** |
| F3 记忆双层场 | `f3_memory.json` | 10 | 0.80 | 长期记忆的注入、跨用户隔离、不该记时的零提取、记忆服务降级 |
| F4 权限矩阵场 | `f4_permission_matrix.json` | 10 | 1.00 | 5 权限点 × 4 角色矩阵的**放行与拒绝两侧** |
| F5 出入库流程场 | `f5_inventory_flow.json` | 10 | 1.00 | 写库真记账 / 不越界 / 幂等 / 槽位补齐与取消 |
| F6 长会话压缩场 | `f6_long_session.json` | 4 | 0.75 | 历史被压缩后，关键约束（时间区间、过滤条件）不丢 |
| F7 注入对抗场 | `f7_injection.json` | 4 | 1.00 | 提示注入 / 冒充身份 / 越权索要数字 / 套取系统提示词 |
| F8 常规回归场 | `eval_anta.json` | 30 | 1.00 | 功能基线：折扣机制 / 销售与维度 / 库存 / 补货 / 多轮 / 兜底边界 |
| F8 常规回归场（记忆组） | `eval_anta_memory.json` | 3 | 1.00 | 跨会话记忆回忆（**需 `memory.long_term=true`**，否则整组 `skipped` 不计分） |
| F9 答案质量场 | `f9_answer_quality.json` | 8（+1 标定） | 0.75 | 答复文本里的每个数字都能对到真实来源（防幻觉对账） |

> - F8 拆成「主组 30 + 记忆组 3」两个文件，合计 33 例：记忆组依赖 `memory.long_term` 开关，故单独成场判定。主组通过线 1.00 = 回归门（任何功能回退必须当场暴露）。
> - 另附 `eval_upgrade_verify.json`（11 例，**无场、不设线**）：S2/S3 升级期的临时验证集，不参与八场判定；`_tmp_probe_s0.json` / `_tmp_probe_s1.json` 为 S1 单点探测留档，同样无场。

## 二、机制覆盖矩阵（首版）

统计口径：八场数据集（不含 `eval_upgrade_verify.json`）中，声明了该断言的 **用例数**。断言字段名即数据集字段名，无需翻译；写错名字会被加载器拒绝（`_reject_unknown_expect_keys`）。

| 断言字段 | 覆盖用例数 | 主要分布 |
|---|---|---|
| `expect_sql_tables` | 33 | F1/F2/F3/F4/F6/F7/F9（SQL 命中表） |
| `expect_sql_not_tables` | 13 | F1/F2/F6/F7（必须**不**出现的表） |
| `expect_pending_slots` | 12 | F5 为主，F1/F2/F4/F7 |
| `expect_permission_allowed` | 9 | F4/F5/F9/F1/F2（放行侧） |
| `expect_explanation_grounded` | 9 | F9(8) + F7(1)（答复数字可溯源，防幻觉） |
| `expect_inherited` | 7 | F2（多轮继承） |
| `expect_permission_denied` | 6 | F4/F5/F7/F9（拒绝侧） |
| `expect_memory_scope` | 6 | F3（跨用户隔离） |
| `expect_inherited_absent` | 5 | F2（条件推翻后旧约束须消失） |
| `expect_memory_injected` | 4 | F3（记忆是否真被注入上下文） |
| `expect_flow_doc` | 4 | F5（写库单据真实落库） |
| `expect_login_rejected` | 2 | F4（TEMP 越权登录即拒） |
| `expect_zero_extract` | 2 | F3（不该记时不得提取记忆） |
| `expect_history_len_le` | 1 | F6（压缩后历史长度上限） |

合计 **113 处机制断言**。矩阵的作用：新增机制断言时按此表补用例，避免"机制有了、没人测"。

## 三、怎么跑

```bash
# 单场（例：F5 出入库流程场）
uv run python -m app.scripts.run_evaluation -d evaluation/datasets/f5_inventory_flow.json -e f5_smoke

# F8 记忆组必须带开关，否则整组 skipped（报告里显示"跳过 3"，不计入通过率）
uv run python -m app.scripts.run_evaluation -d evaluation/datasets/eval_anta_memory.json \
    -e f8_memory --features memory.long_term=true

# 全量八场：逐场跑（各场独立报告，逐场对照通过线）
```

报告正文的「场判定」一节会直接给出 `用例通过 x/y（通过率）通过线 达标/未达标`，控制台同步打印。

## 四、口径纪律

1. **一场一线**：通过线写在本场数据集顶层 `pass_line`（0~1）；不写 = 不判达标。加载器会拒绝写错的越界值（如 `90`）。
2. **跳过 ≠ 不通过**：`status="skipped"`（依赖开关未开）的用例不进分子分母，场判定显示"有效 x，跳过 y"。
3. **只加不改**：新增用例往下加；修改既有用例的口径（golden/断言）必须同步改 `description` 并注明日期——口径必须带日期区间（库中被 `daily_append` 持续追加，参见 `docs/演示题库.md` 维护约定 4）。
4. **断言名 = 字段名**：新断言器注册到 `app/evaluation/assertions.py::_CHECKS` 后，数据集里用同名键即可；未注册的 `expect_*` 会被加载器拒绝（防拼错导致断言静默失效）。
5. **数字可溯源**：F9 的 `expect_explanation_grounded` 是零 LLM 的字符串对账——答复文本里的每个数字/日期都必须能在白名单来源（结果行、总数、SQL、query、date_info、补货计划）里找到。白名单故意包含 `query`/`date_info`，避免"用户自己说的数字"被误判为幻觉。
