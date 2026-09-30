# BUG-2026-09-30-01 · GMV最高地区查询的 GROUP BY 粒度错误

> 状态：**已确认，待修复**　|　发现方式：评估框架（baseline_50）　|　严重度：中（单点语义错误，不影响链路稳定性）

## 1. 基本信息

| 项 | 值 |
|---|---|
| 发现用例 | `dq_gmv_top_region`（"GMV最高的地区是哪个"） |
| 发现批次 | baseline_50_20260930_141914（50 用例全量，唯一一个 SQL 正确性失败） |
| 影响 | 涉及"按大区聚合"的查询可能返回错误排名；同模式（region_name 聚合）的其它用例未受影响（LLM 非确定性，同类错误随机出现） |
| 复现 | `uv run python -m app.scripts.run_evaluation -d evaluation/datasets/eval_v1.json -e repro_bug01 --limit` 后查该用例；或直接执行下方两条 SQL 对比 |

## 2. 现象

```sql
-- agent 生成（语义错误）：
SELECT r.region_name AS 地区, SUM(o.order_amount) AS GMV
FROM fact_order o JOIN dim_region r ON o.region_id = r.region_id
GROUP BY r.region_id, r.region_name      -- ← 按 region_id（省级）分组
ORDER BY GMV DESC LIMIT 1;
-- 结果：华南 97395.0   ← 错误答案

-- golden（正确口径）：
SELECT dr.region_name, SUM(fo.order_amount) AS gmv ...
GROUP BY dr.region_name                  -- ← 按 region_name（大区）分组
-- 结果：华东 203840.7   ← 正确答案
```

## 3. 根因链

1. **数据模型粒度陷阱**：`dim_region` 的行粒度是**省级**（region_id 主键，如 R002=浙江），`region_name` 是**大区**标签（华东 = 浙/沪/苏/闽/鲁/皖 6 个 region_id 共享）。表结构本身没有任何字段/备注提示这一粒度语义
2. **用户语义**："地区" = 大区（region_name），与表结构的物理粒度不一致
3. **LLM 的防御性习惯**：模型按"GROUP BY 覆盖所有非聚合选中列 + 主键列"的常见 SQL 惯例写了 `GROUP BY region_id, region_name`——在 region_id 与 region_name 一一对应的表里这是对的，在本表（多对一）里导致大区被碎片化
4. **validate_sql 无法拦截**：EXPLAIN 只校验语法与对象存在性，`GROUP BY` 粒度是语义层错误，完全合法地通过
5. **run_sql 正常执行**：合法 SQL → 有结果
6. **explain_result 被骗**：它只看到 `[华南 97395]` 这一行结果，无真值对照，自然地叙述了一个"看起来合理"的错误答案（幻觉式解释）

## 4. 为什么评估框架能抓住（而传统测试不能）

golden 对照执行 + 结果集比对不依赖任何中间环节的自查——它度量的是**最终语义正确性**。此 bug 链路上每个环节（语法校验、执行、解释）都"正常"，只有端到端对照能暴露。

## 5. 修复方向（候选，未实施）

| 方向 | 改动点 | 预期效果 |
|---|---|---|
| A. 元数据补粒度备注（首选） | `conf/meta_config.yaml` 给 `dim_region.region_name` 的 description 补"大区标签，一区对应多省，按大区统计时仅按此列分组"；`region_id` 补"省级粒度主键" → 重建元数据知识库 | 召回与 SQL 提示词自带粒度语义，模型不再猜 |
| B. generate_sql 提示词加范例 | `prompts/generate_sql.prompt` 增加一条"按大区统计"的 few-shot | 直接示范正确写法；与 A 二选一或叠加 |
| C. 修复验证 | 修复后重跑 `baseline_50`，观察 `dq_gmv_top_region` 是否转绿（注意 LLM 非确定性，建议跑 2-3 次确认稳定） | 量化修复效果 |

## 6. 关联

- 评估口径：03 文档 §3.3 v1.1（本次修复验证将复用）
- 报告证据：`evaluation/reports/baseline_50_20260930_141914.json` → cases → `dq_gmv_top_region`
