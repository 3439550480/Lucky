#!/usr/bin/env bash
# ============================================================================
# Lucky 一键启动脚本（v1.1 PRD FR-01）
#
# 一条命令完成：环境自检 → 依赖服务拉起 → 元数据建库（幂等）→ 后端 → 前端
# 目标（PRD GWT）：全新 Ubuntu 机器（已装 docker/uv/pnpm）克隆仓库、填好 .env 后，
#                ≤10 分钟内浏览器完成一次成功问数，全程零人工排障。
#
# 用法：
#   bash bootstrap.sh            # 全流程启动
#   bash bootstrap.sh --check    # 只跑环境自检（排障用）
#   make up                      # 等价于 bash bootstrap.sh（Makefile 薄封装）
#
# 幂等性（FR-01 业务规则 3）：
#   - 建库前探测 Qdrant 两个 collection（column_info/metric_info），已存在则跳过
#   - 后端/前端先探测端口是否已在服务，已服务则跳过（不重复拉起进程）
#   - 依赖服务 docker compose up -d 天然幂等（已运行则 no-op）
# ============================================================================
set -uo pipefail

# ---- 常量：与 conf/app_config.yaml、docker/docker-compose.yaml 保持一致 ----
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker/docker-compose.yaml"
LOG_DIR="$ROOT_DIR/logs"
BACKEND_PORT=8000
FRONTEND_PORT=5173
QDRANT_URL="http://localhost:6333"
ES_URL="http://localhost:9200"
EMBEDDING_URL="http://localhost:8081"
WAIT_TIMEOUT=120   # 依赖服务健康等待上限（秒）

CHECK_ONLY=false
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=true

# ---- 输出辅助：分层日志（FR-01 要求"分层输出日志"），失败即给原因与修复指引 ----
info()  { printf '\033[1;34m[启动]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[  ✓ ]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[警告]\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&- 3<&-; return 0; } || return 1; }

# ============================================================================
# 阶段 1：环境自检（FR-01 规则 1/2 —— 自检失败禁止继续，缺啥给啥指引，衔接 FR-04）
# ============================================================================
self_check() {
  info "阶段 1/5：环境自检"
  local missing=()

  # -- 工具存在性：缺什么列什么，统一在末尾给安装指引（不逐项中断）--
  command -v docker  >/dev/null 2>&1 || missing+=("docker")
  command -v uv      >/dev/null 2>&1 || missing+=("uv")
  command -v pnpm    >/dev/null 2>&1 || missing+=("pnpm")
  command -v curl    >/dev/null 2>&1 || missing+=("curl")
  if ((${#missing[@]})); then
    die "缺少工具：${missing[*]}
  修复指引：
  - docker  → https://docs.docker.com/engine/install/（Linux 需把当前用户加入 docker 组）
  - uv      → curl -LsSf https://astral.sh/uv/install.sh | sh
  - pnpm    → corepack enable && corepack prepare pnpm@latest --activate（需先装 Node.js 20+）"
  fi
  ok "docker / uv / pnpm / curl 已安装"

  docker compose version >/dev/null 2>&1 || die "docker compose 插件不可用
  修复指引：apt install docker-compose-plugin 或升级 Docker Desktop"

  # -- 端口占用：被占用时可能是本项目旧进程（幂等跳过）或其它程序（需人工处理）--
  local p bad=()
  for p in 3307 6333 9200 8081 "$BACKEND_PORT" "$FRONTEND_PORT"; do
    port_in_use "$p" && bad+=("$p")
  done
  if ((${#bad[@]})); then
    warn "以下端口已被占用：${bad[*]}"
    warn "若是本项目的旧进程/容器则可直接复用（脚本幂等，会自动跳过）；"
    warn "若是其它程序占用，请先停掉：sudo lsof -i :<端口> 后 kill 对应进程"
  else
    ok "关键端口无冲突（3307/6333/9200/8081/$BACKEND_PORT/$FRONTEND_PORT）"
  fi

  # -- .env 校验（FR-04 三段式指引：缺什么 / 去哪申请 / 填哪里）--
  local env_file="$ROOT_DIR/.env"
  [[ -f "$env_file" ]] || die "缺少 .env 配置文件
  缺什么   ：项目根目录下的 .env（模型 API Key 唯一来源，已被 gitignore 排除）
  去哪申请 ：模板与三家申请链接见 .env.example（DeepSeek / Qwen / GLM 任选一家即可）
  填哪里   ：cp .env.example .env 后，把申请到的 Key 粘贴到对应 = 号后面，重新运行本脚本"
  if ! grep -Eq '^(DEEPSEEK_API_KEY|DASHSCOPE_API_KEY|ZHIPU_API_KEY)=.+' "$env_file"; then
    die ".env 中没有任何一个有效的模型 Key
  缺什么   ：DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / ZHIPU_API_KEY 至少填一个（非空）
  去哪申请 ：DeepSeek https://platform.deepseek.com/api_keys ｜
             Qwen   https://bailian.console.aliyun.com/?apiKey=1 ｜
             GLM    https://open.bigmodel.cn/usercenter/apikeys
  填哪里   ：编辑项目根目录 .env，粘贴到对应 = 号后面（不要加引号），重新运行本脚本"
  fi
  ok ".env 存在且至少有一个模型 Key"

  # -- uv 依赖：首次 clone 后 .venv 不存在，提前同步（依赖清单锁在 pyproject/uv.lock）--
  if [[ ! -d "$ROOT_DIR/.venv" ]]; then
    info "首次运行：uv sync 安装 Python 依赖（约 1-3 分钟）"
    (cd "$ROOT_DIR" && uv sync) || die "uv sync 失败，请检查网络后重试"
  fi
  ok "Python 依赖就绪（.venv）"
}

# ============================================================================
# 阶段 2：依赖服务拉起（docker compose）+ 健康等待
# ============================================================================
start_deps() {
  info "阶段 2/5：拉起依赖服务（mysql:3307 / qdrant:6333 / es:9200 / embedding:8081）"
  docker compose -f "$COMPOSE_FILE" up -d || die "docker compose 启动失败
  常见原因：docker daemon 未运行（sudo systemctl start docker）；镜像拉取失败（检查网络/代理）"

  # -- 逐项等待就绪：超时给出针对性排查指引（避免陌生人卡在"连不上"无头绪）--
  local waited=0
  info "等待依赖服务健康（最长 ${WAIT_TIMEOUT}s）..."
  until curl -sf "$QDRANT_URL/collections" >/dev/null 2>&1 \
     && curl -sf "$ES_URL" >/dev/null 2>&1 \
     && curl -sf "$EMBEDDING_URL" >/dev/null 2>&1 \
     && docker exec mysql mysqladmin ping -h localhost --silent >/dev/null 2>&1; do
    waited=$((waited + 3))
    if ((waited >= WAIT_TIMEOUT)); then
      die "依赖服务 ${WAIT_TIMEOUT}s 内未全部就绪
  排查：docker compose -f docker/docker-compose.yaml ps 查看哪个容器异常；
        docker compose -f docker/docker-compose.yaml logs <服务名> 看具体报错；
        首次启动 elasticsearch/embedding 需拉取镜像与预热，机器慢时可重跑本脚本（幂等）"
    fi
    sleep 3
  done
  ok "mysql / qdrant / elasticsearch / embedding 全部就绪（${waited}s）"
}

# ============================================================================
# 阶段 3：元数据知识库构建（幂等：collection 已存在则跳过）
# ============================================================================
build_kb() {
  info "阶段 3/5：构建元数据知识库（Text2SQL 的 schema 召回底座）"
  # -- 幂等标记：两个业务 collection 都存在 = 已建库，跳过（重复执行不产生脏数据）--
  local cols
  cols="$(curl -sf "$QDRANT_URL/collections" 2>/dev/null || echo '')"
  if echo "$cols" | grep -q 'column_info_collection' && echo "$cols" | grep -q 'metric_info_collection'; then
    ok "知识库已存在（column_info/metric_info collection），跳过构建"
    return 0
  fi
  info "首次建库：uv run python -m app.scripts.build_meta_knowledge（约 1-2 分钟）"
  (cd "$ROOT_DIR" && uv run python -m app.scripts.build_meta_knowledge) \
    || die "建库失败：常见原因是依赖服务未就绪（重跑本脚本）或 embedding 模型加载失败
  排查：docker logs embedding；确认 docker/embedding/bge-small-zh 模型文件完整"
  ok "元数据知识库构建完成"
}

# ============================================================================
# 阶段 4/5：后端与前端（后台进程 + 健康探测；已在服务则跳过 = 幂等）
# ============================================================================
start_backend() {
  info "阶段 4/5：启动后端（uvicorn main:app :$BACKEND_PORT）"
  if curl -sf "http://localhost:$BACKEND_PORT/api/models" >/dev/null 2>&1; then
    ok "后端已在运行，跳过（http://localhost:$BACKEND_PORT）"
    return 0
  fi
  mkdir -p "$LOG_DIR"
  (cd "$ROOT_DIR" && nohup uv run uvicorn main:app --host 0.0.0.0 --port "$BACKEND_PORT" \
      > "$LOG_DIR/backend.log" 2>&1 &)
  local waited=0
  until curl -sf "http://localhost:$BACKEND_PORT/api/models" >/dev/null 2>&1; do
    waited=$((waited + 2))
    if ((waited >= 60)); then
      die "后端 60s 未就绪
  排查：tail -50 logs/backend.log（常见：端口被占、.env Key 无效、依赖服务未就绪）"
    fi
    sleep 2
  done
  ok "后端就绪（http://localhost:$BACKEND_PORT，日志 logs/backend.log）"
}

start_frontend() {
  info "阶段 5/5：启动前端（vite :$FRONTEND_PORT）"
  if port_in_use "$FRONTEND_PORT"; then
    ok "前端已在运行，跳过（http://localhost:$FRONTEND_PORT）"
    return 0
  fi
  cd "$ROOT_DIR/frontend" || die "frontend/ 目录缺失"
  if [[ ! -d node_modules ]]; then
    info "首次运行：pnpm install（约 1-2 分钟）"
    pnpm install || die "pnpm install 失败，请检查网络后重试"
  fi
  mkdir -p "$LOG_DIR"
  nohup pnpm dev > "$LOG_DIR/frontend.log" 2>&1 &
  local waited=0
  until port_in_use "$FRONTEND_PORT"; do
    waited=$((waited + 2))
    if ((waited >= 60)); then
      die "前端 60s 未就绪，排查：tail -50 logs/frontend.log"
    fi
    sleep 2
  done
  ok "前端就绪（http://localhost:$FRONTEND_PORT，日志 logs/frontend.log）"
}

# ============================================================================
# 主流程：自检必过；--check 只体检不启动；任一层失败立即终止（die 已给指引）
# ============================================================================
self_check
if $CHECK_ONLY; then
  ok "自检通过（--check 模式，未启动任何服务）"
  exit 0
fi
start_deps
build_kb
start_backend
start_frontend
echo ""
ok "全部就绪！浏览器打开 http://localhost:$FRONTEND_PORT 开始问数"
info "停止服务：kill 后端/前端进程（日志在 logs/）；依赖服务：docker compose -f docker/docker-compose.yaml down"
