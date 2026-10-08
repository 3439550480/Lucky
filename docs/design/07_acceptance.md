# 07 · 验收清单与联调手册（原 06_acceptance，随文档拆分顺延）

> 状态：`final`（验收执行完毕）　|　上位文档：[00_overview.md](00_overview.md)（final）及其余全部模块文档
> 执行记录：2026-10-03 开关矩阵 S1-S10 全部通过（S10 修复 apply_feature_overrides 严格布尔解析后通过）；§4.1 后端联调核对通过（/api/models、SSE capability 注入、usage 日志开关语义）；§6.1 清理清单核对通过（start_recall/intent_classify/simple_answer/langchain_deepseek 清零，es.index_name 标注废弃，import_to_qdrant 加废弃指引）；记录表见 acceptance_log.md；README 已更新
> 本文是编码阶段的**总验收依据**：开关矩阵冒烟、端到端联调、实验执行手册、清理清单。各模块的过程验收见 01~06 文档各自的 §6，本文只做汇总 gates 与全局项。

---

## 1. 目标与边界

### 1.1 做什么

1. **开关矩阵冒烟测试**：Feature Flags 的代表性组合下，应用可启动、主链路可用、行为符合各文档声明的开关语义（§3 用例表）
2. **端到端联调步骤**：从零环境到前后端完整跑通的标准化流程（§4）
3. **实验执行手册**：baseline 与逐模块对比实验的命令清单与预期产出（§5）——设计文档价值的最终兑现环节
4. **清理清单**：教学遗留代码/配置的清除确认与文档收尾（§6）

### 1.2 不做什么

- 不做自动化测试框架搭建（冒烟以脚本化命令 + 人工断言为主；pytest 单测仅限 03 的指标纯函数，见 03 §6.3）
- 不做性能压测 / 长稳测试
- 不覆盖多用户、多实例部署形态（v1 单用户假设，06 §1.2）

### 1.3 依赖

全部模块文档（00~06）定稿；编码 todo（llm-factory → … → memory-module）全部完成。

---

## 2. 验收环境基线

| 项 | 要求 | 检查命令 |
|----|------|---------|
| Docker 服务 | mysql(3307)/elasticsearch(9200)/kibana/qdrant(6333)/embedding(8081) 全部 Up | `docker compose ps` |
| Python 环境 | `uv sync` 成功；`langchain-openai` 显式声明 | `uv run python -c "import langchain_openai"` |
| API Keys | `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` / `ZHIPU_API_KEY` 环境变量就绪 | 三家各发一次最小请求（01 §6.10） |
| 知识库 | meta/dw 两库 + Qdrant 两 collection + ES value_index 已构建 | `uv run python -m app.scripts.build_meta_knowledge -c conf/meta_config.yaml` |
| 前端 | `pnpm install && pnpm dev` 启动无错 | 浏览器访问 vite 地址 |
| 配置基线 | `conf/app_config.yaml` 含 `llm.providers`(3 模型+tiers) / `features:` 全段 / `session:` 段 / `memory:` 段；`conf/capability_config.yaml` 两能力 + `entry` | 人工核对 00 §5.1 / 04 §2.1 |
| 记忆库 | `data/memory/` 目录存在且入 gitignore | `Test-Path data/memory` |

---

## 3. 开关矩阵冒烟测试用例表

### 3.1 用例格式

每个用例 = 开关组合 + 三步断言：**启动成功 → 标准问数 query 走通 → 闲聊 query 走通**，另附该组合特有的行为断言。

标准 query：`统计华北地区的销售额`；闲聊 query：`你好，你能做什么`。

### 3.2 代表性组合（10 组，覆盖全部 7 个行为开关的每个取值）

| # | 开关组合 | 特有断言（在通用三步之外） | 依据文档 |
|---|---------|--------------------------|---------|
| S1 | 全默认（capability_routing/rules_fast_path/embedding_route/context_management/memory.short_term=true，memory.long_term=false，usage_tracking=true） | 路由命中链路正常；`LLM usage` 日志含 by_stage | 00/01 |
| S2 | `capability_routing=false` | 无规则/embedding/LLM 路由记录；全事件 `capability=dataquery`；闲聊 query 也进问数链路（旧行为） | 04 §5 |
| S3 | `capability_routing=true, rules_fast_path=false` | 标准问数 query 走 embedding 或 LLM 分发（无规则命中日志） | 04 §5 |
| S4 | `capability_routing=true, embedding_route=false` | 规则未命中的 query 直接走 LLM 分类 | 04 §5 |
| S5 | `capability_routing=true, rules_fast_path=false, embedding_route=false` | 纯 LLM 分类基线 | 04 §5 |
| S6 | `features.context_management=false` | 历史读取回退 `state["messages"][-10:]` 旧行为；无 prefix 构建日志；多轮对话仍正确（正确性不变，仅无 cache 优化） | 05 §5 |
| S7 | `features.memory.long_term=true` | 运行后 `data/memory/notes.json` 有写入（含提取事实）；by_stage 出现 `memory_extract`；`{memory_block}` 注入生效 | 06 §5 |
| S8 | `features.usage_tracking=false` | 无 `LLM usage` 日志；链路行为与 S1 完全一致 | 01 §5 |
| S9 | 评估子模块全关（`evaluation.*=false` ×5） | 评测报告全区域 `disabled`，runner 正常完成退出码 0 | 03 §5.1 |
| S10 | 非法值组合（`--features memory.short_term=abc` 等） | 配置转换报明确错误或安全回退，不产生静默错误行为 | 03 §3.6 |

> 执行方式：每组改 `conf/app_config.yaml`（或 runner 用 `--features`）→ 重启后端 → 三步断言 → 记录。**冒烟期间禁止修改固定前缀**（prefix 变更会使缓存数据不可比）。

### 3.3 结果记录

按 §6.2 的验收记录表逐组登记（通过/失败/备注）；任何失败须定位到对应模块文档的验收条目并修复后重跑。

---

## 4. 端到端联调步骤

### 4.1 后端联调

1. 环境基线检查（§2 全部通过）
2. 启动：`uv run uvicorn main:app --reload --port 8000` → 日志依次完成各 manager init，无 ERROR
3. `GET /api/models` → 3 个 provider（DeepSeek/Qwen/GLM），default=deepseek
4. `POST /api/query`（curl 或 /docs）三次：问数 query / 闲聊 query / 不命中规则的问数 query → 分别验证 dataquery 链路、default_answer、embedding→LLM 递进路由
5. SSE 事件流逐帧核对（00 §4.2）：progress 步骤序列完整、result 为结构化数据、explanation 文本、无裸异常
6. `LLM usage` 日志核对：by_stage 含 route/扩词×3/过滤×2/生成/执行/解释 等环节、DeepSeek 请求出现 `prompt_cache_hit_tokens` 字段（05 §3.4）
7. 模型切换：分别以 qwen/glm 发起请求 → usage 日志 provider 对应变化、结果正常
8. 多轮会话：同 thread_id 三轮对话 → 第三轮回答引用前两轮上下文；换 thread_id → 互不可见

### 4.2 前端联调

1. 模型选择按钮（上拉弹层）→ 3 模型分组展示、选中 ✓、localStorage 持久化、流式中禁用
2. 发送问数问题 → StepRail 步骤条实时推进 → ResultTable 渲染 → 解释气泡
3. 发送闲聊 → 不出现问数步骤条，直接文本回复（default 能力）
4. 切换模型后再次提问 → 新请求用新模型（后端日志佐证），旧消息渲染不受影响
5. 刷新页面 → 模型选择保留、thread_id 保留（会话延续）；`resetThreadId`（新对话）→ 上下文清空

### 4.3 评估链路联调

1. `uv run python -m app.scripts.run_evaluation -d evaluation/datasets/eval_v1.json -e smoke_test`（含 enabled 用例后）→ 报告 JSON+MD 生成
2. `--compare` 两份报告 → 对比表输出
3. `--features` 覆盖 → 报告 meta flags 快照正确

---

## 5. 实验执行手册（baseline 与逐模块对比）

> 前提：评测集 `eval_v1` 已填充（建议 ≥20 条：问数 12 / default 4 / memory 探针 4），标注口径按 03 §2.1。

### 5.1 baseline（对应编码 todo：eval-runner-report 完成后、intent-routing 之前）

```powershell
uv run python -m app.scripts.run_evaluation -d evaluation/datasets/eval_v1.json -e baseline --provider deepseek
```

此时能力路由/上下文/记忆尚未编码——报告记录的是**改造框架下的现状能力**。另跑一组 `baseline_legacy`（`--features capability_routing=false context_management=false`）作为"旧路径"对照组。

### 5.2 逐模块对比（对应模块编码完成后）

| 实验 | 命令（增量 flags） | 验证的假设 |
|------|-------------------|-----------|
| E1 路由贡献 | `-e route_on`（默认全开）vs `-e route_off --features capability_routing=false` | 能力路由的分流正确性与成本差异 |
| E2 规则通道 | `-e rules_off --features rules_fast_path=false` vs baseline | 规则快路径的 token 节省 |
| E3 embedding 安全网 | `-e embed_off --features embedding_route=false` vs baseline | 安全网的召回补充价值 |
| E4 上下文/KV cache | `-e ctx_on` vs `-e ctx_off --features context_management=false` | 缓存命中带来的每问成本下降（对照 `prompt_cache_hit_tokens`） |
| E5 记忆 | `-e memory_on --features memory.long_term=true` vs `-e memory_off` | 基础回忆三指标 + 记忆注入的成本代价 |
| E6 分类模型选型 | `-e cls_deepseek / cls_qwen / cls_glm --features …`（配合 `routing.classifier_provider` 临时配置） | 精准度+速度两指标敲定分类模型 |

### 5.3 对比输出

```powershell
uv run python -m app.scripts.run_evaluation -e compare `
  --compare evaluation/reports/baseline_xxx.json evaluation/reports/route_on_xxx.json `
            evaluation/reports/memory_on_xxx.json
```

产出 `compare.md`：行 = 实验标签（含 flags 快照摘要），列 = 检索 hit@5/MRR、意图准确率、工具触发准确率、SQL 可执行率/正确性、基础回忆三指标、每问成本、总耗时。

---

## 6. 清理清单（教学遗留 + 文档收尾）

### 6.1 代码/配置清理（随各模块编码完成，此处总核对）

| 项 | 处理 | 核对方式 |
|----|------|---------|
| `graph.py` start_recall 空节点 + 注释残留边 | 04 删除 | `grep start_recall` 零结果 |
| `intent_classify.py` / `simple_answer.py` / 旧 prompt | 04 删除 | `grep intent_classify\|simple_answer` 零结果 |
| `llm.py` 死导入（AIMessage 等 7 个） | 01 清理 | 人工核对 |
| `app_config.yaml` 的 `es.index_name` 无效配置 | 04 删除（ES 索引名改为配置注入 `ValueESRepository`） | grep index_name |
| `import_to_qdrant.py` 老脚本 | 头部加 deprecated 注释指向 build_meta_knowledge（不删除，留作教学对照） | 人工核对 |
| `main.py` 注释的调试 lifespan、`mysql_client_manager.py` 注释代码 | 编码期间顺手清理 | 人工核对 |
| `uv.lock` 中 langchain-deepseek | 04 阶段评估是否仍有引用，无则移除声明（保留亦不阻塞） | `grep langchain_deepseek app/` |

### 6.2 验收记录表模板（随附于 PR 描述或 `docs/design/acceptance_log.md`）

```
| 用例/实验 | 日期 | commit | 结果(通过/失败) | 备注 |
|----------|------|--------|----------------|------|
| S1 全默认冒烟 | | | | |
| ... | | | | |
```

### 6.3 文档收尾

1. 各文档状态最终核对：00~06 final、07 本文 final
2. `README.md` 更新：新架构说明（能力路由/上下文/记忆/评估/开关体系）、启动步骤（含 3307 端口与三个 API Key）、设计文档索引
3. 全部文档 + 代码推送 GitHub（`3439550480/Lucky`）
4. `.env` / `data/memory/` / 日志确认不在推送内容中

---

## 7. 总验收 Gate（编码阶段完成定义）

编码 todo（llm-factory → model-selection-api → frontend-model-selector → eval-framework → eval-runner-report → intent-routing → context-management → memory-module）逐个完成时：

1. 对应文档 §6 验收条目**全部通过**（文档与实现冲突时先改文档再继续——00 §7.2 纪律）
2. 冒烟：S1（全默认）+ 该模块相关的 S 组用例通过
3. 已有实验对比不回归（baseline 指标结构一致，数值波动属 LLM 非确定性）

全部 todo 完成后：跑完 §3 全矩阵 + §5 实验手册 + §6 清理核对 → **总验收通过**，项目进入下一迭代周期（skill+工具化、前端通用化、压缩策略等预留方向）。
