# docs 目录索引

> 收口日期：2026-10-13。文档分四层：行为规范 / 设计定稿 / 专题方案 / 运维记录。

## 一、根目录（规范与运维）

| 文件 | 内容 | 状态 |
|---|---|---|
| `编码技术规范.md` | 开发行为约束（函数增量 / 落盘确认 / 缺陷处理 / 注释三段式 / 动改先论证） | 生效中，规则变更记录在文内 |
| `演示题库.md` | 演示用问答基线（12 项）；**口径必须带日期区间**（库被 `daily_append` 持续追加） | 维护中 |
| `安全清理清单.md` | 演示数据/环境的清理约定 | 生效中 |

## 二、design/（设计定稿，接口冲突以此为准）

| 文件 | 内容 |
|---|---|
| `00_overview.md` | 总览与事件协议契约（§4） |
| `01_llm_factory.md` | LLM 工厂与多 provider |
| `02_model_selection.md` | 模型选型与峰谷档位 |
| `03_evaluation.md` | 评测体系（数据集/指标/报告 §3.7） |
| `04_capability_routing.md` | 能力路由 |
| `05_context_management.md` | 上下文管理（前缀协议/历史区） |
| `06_memory.md` | 记忆（双层 scope / 门禁 / 提取） |
| `07_acceptance.md` | 验收标准 |
| `acceptance_log.md` | 验收记录 |
| `s3b-architecture.html` / `s3b-capability-nodes.svg` | S3b 架构图源文件 |

## 三、specs/（Lucky2.0 专题方案）

| 文件 | 内容 | 状态 |
|---|---|---|
| `Lucky2.0-安踏特卖店Agent-方案-v5.md` | 总方案（v4 为历史版本留档） | 定稿 |
| `Lucky2.0-商品主数据清单-120SPU.md` | 120 SPU 主数据 | 定稿 |
| `Lucky2.0-用户画像与权限设计.md` | 角色/权限点矩阵（F4 场的依据） | 定稿 |
| `Lucky2.0-上下文与记忆策略.md` | S2 上下文与记忆策略 | 已验收（2026-10-09） |
| `Lucky2.0-评测优化策略.md` | SQL 正确性 60%→100% 的 9 修法 | 已验收（2026-10-11） |
| `Lucky2.0-评测体系升级方案.md` | R1/R2 + F1~F9 分场升级（S0~S5） | 落地中，全量测评见 `evaluation/reports/` |

## 四、bugs/（缺陷档案）

按日期命名的缺陷记录（如 `2026-09-30_gmv-top-region-groupby-granularity.md`），修复版落盘、原版注释留档（编码技术规范规则 3）。

## 五、相关入口

- 评测数据集八场清单与机制覆盖矩阵：`evaluation/datasets/README.md`
- 评测运行方式：`uv run python -m app.scripts.run_evaluation -d <数据集> -e <标签>`
- 全量测评：逐场跑 9 个数据集（F3/F8 记忆组需 `--features memory.long_term=true`）
