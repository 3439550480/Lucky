# Lucky

基于 FastAPI + LangGraph 的安踏特卖店问数 Agent 工程范本——**三种能力形状**（问数/库存共用 14 节点 Text2SQL 链路，补货计划为固化算法单节点）/ 五级能力路由 / KV cache 友好上下文 / 长期记忆 / 多轮对话 / 量化评估与 Feature Flags 对照实验的完整落地。

自然语言提问 → 能力路由（芯片 → 规则 → embedding → LLM → 兜底 五级递进，可插拔）→ 按能力分发：

- **问数 / 库存（inventory）**：共用问数链路——多路召回（Qdrant 向量 + ES 全文）→ LLM 生成 SQL → 校验修正闭环 → 执行 → SSE 流式推送结果与解释；库存域知识全部沉淀在元数据与提示词层（零新增节点）
- **补货计划（replenish）**：固化算法单节点——固定 SQL 取数 → `max(0, ceil(日均×(覆盖+安全)) − 可用 − 在途)` 纯 Python 计算 → LLM 只解释不碰数字；业务数学不进 LLM，数字可审计、可评估

> 演示数据为程序生成的安踏特卖店数据集（2026 国庆 7 天：120 货号 / 1,635 SKU / 77,607 行销售 / 11,445 行库存快照 / 14,542 行出入库流水，含满件折扣与断码情节），非真实经营数据；商品名/吊牌价来自安踏官方商城公开信息（仅学习用途），货号/库存/销售均为仿真数据。由 `app/scripts/generate_anta_data.py` 一条命令重建（SEED 固定，可复现）。

## 快速启动

1. 依赖服务：`docker compose -f docker/docker-compose.yaml up -d`（mysql 映射 **3307**，首次启动自动执行 docker/mysql/*.sql 初始化）
2. 配置：`.env` 填入至少一家模型 Key（DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / ZHIPU_API_KEY 任一；.env 是唯一密钥来源，已 gitignore）
3. 建库：`uv run python -m app.scripts.build_meta_knowledge` → 后端：`uv run uvicorn main:app --host 0.0.0.0 --port 8000` → 前端：`cd frontend && pnpm install && pnpm dev`

## 演示链（演示题库详见 docs/演示题库.md）

1. **满件折扣（头号种子题）**：账龄基准折扣 + 满件前每缺 1 件加折、10 折封顶——跨行业务规则在 SQL 生成与解释中的落地
2. **库存**：最新快照日口径（"现在" = `MAX(snapshot_date)`）、断码 SKU、出入库流水、动销率
3. **补货计划**：品类策略（鞋 7 / 服 3 / 配 14 覆盖天数 + 3 天安全库存）→ 结构化补货表格

## 评估（Agent 质量量化）

- 快速回归：`uv run python -m app.scripts.run_evaluation -d evaluation/datasets/eval_quick.json -e quick`
- 全量基线：`-d evaluation/datasets/eval_v1.json -e baseline`
- 对照实验：`--features context_management=false` 等开关覆盖；`--compare a.json b.json` 并排对比

指标：检索三通道 hit@k/MRR/P/R、意图准确率、工具触发（漏调/误调）、SQL 可执行率/结果正确性、成本（token/费用/环节分布，DeepSeek 峰谷自动判档）、基础回忆（store/retrieval/recall/persistence）。
（注：评估数据集尚为上一代场景，安踏版 eval_anta 与干净基线随 final-verify 一并重跑。）

## 设计文档（docs/design/，全部 final）

00_overview 总体架构·事件协议契约·Feature Flags；01_llm_factory LLM 多模型工厂与用量采集；02_model_selection 模型选择；03_evaluation 评估框架；04_capability_routing 能力路由；05_context_management 上下文管理与 KV cache；06_memory 记忆管理；07_acceptance 验收手册；s3b-architecture / s3b-capability-nodes 三能力架构图。Bug 档案见 docs/bugs/。

## 项目结构

app/agent（LangGraph 图·能力路由·nodes/{dataquery,replenish,default,common}·session 上下文·memory 记忆）、app/api、app/clients、app/conf、app/evaluation（评估框架）、app/repositories、app/scripts（建库·评估 CLI·安踏/奶茶数据生成器）、app/services；conf/（三份 YAML：app/capability/meta）；docker/（mysql 初始化 SQL·es·embedding）；docs/（design·bugs·演示题库）；evaluation/（datasets·reports）；frontend/（React19+Vite+Tailwind）；prompts/（legacy/ 为关闭上下文管理的对照组）；tests/（固化算法 GWT 单测）。
