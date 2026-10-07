#!/usr/bin/env bash
# Lucky 服务器一键部署脚本（deploy 分支，Ubuntu 22.04+）
# 前提：仓库已 clone 到 /opt/lucky，且已在 /opt/lucky/.env 填入 3 个 LLM key
# 用法：cd /opt/lucky && sudo bash deploy/setup-server.sh
set -euo pipefail

REPO=/opt/lucky
cd "$REPO"

echo "==== [1/8] Docker 环境 ===="
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

echo "==== [2/8] 启动依赖服务（mysql/qdrant/es/embedding，无 kibana）===="
docker compose -f docker/docker-compose.prod.yaml up -d
echo "等待 MySQL 初始化（首次约 40s）..."
for i in $(seq 1 30); do
  if docker exec mysql mysqladmin ping -ulzs -pLzs666 --silent >/dev/null 2>&1; then break; fi
  sleep 3
done
echo "等待 ES 就绪..."
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:9200 >/dev/null 2>&1; then break; fi
  sleep 3
done

echo "==== [3/8] embedding 模型文件 ===="
if [ ! -f docker/embedding/bge-small-zh/model.safetensors ]; then
  echo "模型文件不存在，从 hf-mirror.com 下载（约 100MB）..."
  if ! command -v uv >/dev/null; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi
  export PATH="$HOME/.local/bin:$PATH"
  uv run --with huggingface_hub python - <<'EOF'
import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
from huggingface_hub import snapshot_download
snapshot_download("BAAI/bge-small-zh-v1.5", local_dir="docker/embedding/bge-small-zh")
EOF
  # 重启 embedding 以挂载新下载的模型
  docker compose -f docker/docker-compose.prod.yaml up -d --force-recreate embedding
  sleep 10
fi

echo "==== [4/8] .env 检查 ===="
if [ ! -f .env ]; grep -q "填入" .env; then
  echo "❌ .env 未就绪（不存在或含'填入'占位符）。请先：cp deploy/env.example .env 并填入 3 个 key，再重跑本脚本"
  exit 1
fi

echo "==== [5/8] 后端依赖安装 ===="
if ! command -v uv >/dev/null; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi
export PATH="$HOME/.local/bin:$PATH"
uv sync

echo "==== [6/8] 重建元数据知识库（meta 数据行 + Qdrant + ES 取值索引）===="
uv run python -m app.scripts.build_meta_knowledge

echo "==== [7/8] nginx + 访问控制 ===="
apt-get update -qq && apt-get install -y -qq nginx apache2-utils >/dev/null
if [ ! -f /etc/nginx/lucky.htpasswd ]; then
  read -rp "设置 Lucky 访问用户名（API 鉴权）: " AUTH_USER
  htpasswd -c /etc/nginx/lucky.htpasswd "$AUTH_USER"
fi
cp deploy/nginx-lucky.conf /etc/nginx/sites-available/lucky
ln -sf /etc/nginx/sites-available/lucky /etc/nginx/sites-enabled/lucky
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "==== [8/8] 后端 systemd 服务 ===="
cp deploy/lucky.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lucky
sleep 3

echo "---- 验证 ----"
curl -sf http://127.0.0.1:8000/api/models >/dev/null && echo "✅ 后端 /api/models 正常" || echo "❌ 后端异常：journalctl -u lucky -n 50"
systemctl is-active --quiet lucky && echo "✅ lucky 服务运行中"
echo "完成。浏览器访问 http://<服务器公网IP>/（记得腾讯云安全组放行 80 端口）"
