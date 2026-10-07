# Lucky

基于 FastAPI + LangGraph 的通用智能助手平台，核心能力为电商问数（Text2SQL）：
自然语言提问 → 能力路由 → 多路召回（Qdrant 向量 + ES 全文）→ LLM 生成 SQL → 校验执行 → SSE 流式推送结果与解释。

架构特性：能力路由（规则 → embedding → LLM 三级递进，可插拔）、上下文管理与 KV cache 友好提示词（固定前缀 + 追加式历史）、长期记忆（Simple Notes + Memory Cards）、评估框架（检索/意图/SQL/成本/基础回忆五维指标 + Feature Flags 对照实验）、模型可切换（DeepSeek / Qwen / GLM，前端选择）。

## 快速启动

1. 依赖服务：`docker compose -f docker/docker-compose.yaml up -d`（mysql 映射 **3307**；首次启动自动执行 docker/mysql/*.sql 初始化）
2. 配置：`.env` 填入 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / ZHIPU_API_KEY（.env 是唯一密钥来源，已 gitignore）；`conf/app_config.yaml`（Feature Flags 等）；`conf/capability_config.yaml`（能力注册表）
3. 构建元数据知识库：`uv run python -m app.scripts.build_meta_knowledge`
4. 后端：`uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000`
5. 前端：`cd frontend && pnpm install && pnpm dev`

## 评估（Agent 质量量化）

- 快速回归（10 用例约 3 分钟）：`uv run python -m app.scripts.run_evaluation -d evaluation/datasets/eval_quick.json -e quick`
- 全量基线（50 用例约 17 分钟）：`-d evaluation/datasets/eval_v1.json -e baseline`
- 基础回忆（需开启记忆）：`-d evaluation/datasets/eval_memory.json -e mem --features memory.long_term=true`
- 对照实验：`--features context_management=false` 等开关覆盖；`--compare a.json b.json` 并排对比

指标：检索三通道 hit@k/MRR/P/R、意图准确率、工具触发（漏调/误调）、SQL 可执行率/结果正确性、成本（token/费用/环节分布，DeepSeek 峰谷自动判档）、基础回忆（store/retrieval/recall/persistence）。

## 设计文档（docs/design/，全部 final）

00_overview 总体架构·事件协议契约·Feature Flags；01_llm_factory LLM 多模型工厂与用量采集；02_model_selection 模型选择；03_evaluation 评估框架；04_capability_routing 能力路由；05_context_management 上下文管理与 KV cache；06_memory 记忆管理；07_acceptance 验收手册。Bug 档案见 docs/bugs/。

## 项目结构

app/agent（LangGraph 图·能力路由·session 上下文·memory 记忆）、app/api、app/clients、app/conf、app/evaluation（评估框架）、app/repositories、app/scripts（建库·评估CLI）、app/services；conf/（三份 YAML）；docker/（mysql 初始化 SQL·es·embedding）；docs/（design·bugs）；evaluation/（datasets·reports）；frontend/（React19+Vite+Tailwind）；prompts/（legacy/ 为关闭上下文管理的对照组）。
