# Lucky 生产部署指南（Ubuntu + Xshell）

本分支（`deploy`）是部署专用分支：内置生产 compose、nginx/systemd 配置、
**预构建的前端产物**（`frontend/dist`，服务器无需 Node/pnpm）。main 分支继续开发，互不干扰。

## 架构

```
浏览器 → http://<公网IP>  →  nginx(80)  ─┬─ 静态文件 frontend/dist
                                          └─ /api/ → uvicorn(127.0.0.1:8000)
Docker（全部只绑 127.0.0.1，不出公网）：mysql(3307) · qdrant(6333) · es(9200) · embedding(8081)
```

内存预算 ~2.2GB（无 kibana、ES 限堆 256m），2核4G 够用。

## 部署步骤（Xshell 里逐条执行）

### 0. 前置：腾讯云控制台
- 安全组放行 **80 端口**（TCP，0.0.0.0/0）
- 确认实例是 Ubuntu 22.04+，拥有 sudo 权限的账户

### 1. 拉代码（约 1 分钟）
```bash
sudo mkdir -p /opt && cd /opt
sudo git clone https://github.com/3439550480/Lucky.git lucky
sudo chown -R $USER:$USER /opt/lucky
cd /opt/lucky && git checkout deploy
```

### 2. 配置 API Key（不进 git，只在服务器上）
```bash
cp deploy/env.example .env
nano .env        # 填入 3 个真实 key，Ctrl+O 保存，Ctrl+X 退出
```

### 3. 一键部署（约 5-10 分钟，含模型下载与知识库重建）
```bash
sudo bash deploy/setup-server.sh
```
脚本自动完成：Docker 安装 → 4 个依赖容器 → embedding 模型下载（hf-mirror，~100MB）
→ uv 依赖安装 → **元数据知识库重建**（meta 数据行 + Qdrant 向量 + ES 取值索引）
→ nginx 安装与配置 → **API 访问密码设置**（会提示输入用户名和密码）→ 后端 systemd 服务

### 4. 验证
```bash
curl -s http://127.0.0.1:8000/api/models        # 应返回 3 个 provider 的 JSON
systemctl status lucky --no-pager                # active (running)
docker ps                                        # 4 个容器 Up
```
浏览器打开 `http://<公网IP>/` → 输入第 3 步设置的用户名密码 → 发一条测试问题。

## 日常运维

| 操作 | 命令 |
|---|---|
| 看后端日志 | `journalctl -u lucky -f` |
| 重启后端 | `sudo systemctl restart lucky` |
| 更新代码 | `cd /opt/lucky && git pull && sudo systemctl restart lucky` |
| 前端更新 | 本地 main 改完 → 同步到 deploy 分支重新构建并提交（见下）→ 服务器 `git pull` 即可（静态文件即时生效） |
| 看依赖容器 | `docker compose -f docker/docker-compose.prod.yaml ps` |

## 前端如何更新到 deploy 分支（本地操作）

```powershell
git checkout deploy
git checkout main -- frontend/src          # 取 main 最新前端源码
cd frontend; npm run build; cd ..
git add -f frontend/dist                   # dist 被 ignore，强制加入
git commit -m "前端产物更新"
git push origin deploy
git checkout main                          # 回到开发分支
```

## 安全要点（已内置）

- mysql/qdrant/es/embedding 端口全部绑定 127.0.0.1，公网扫不到
- API 经 nginx Basic Auth 保护 —— LLM key 不会被陌生人白嫖
- key 只存在服务器 `.env`（git 不跟踪）
- ⚠️ 腾讯云安全组只放行 80（和 22），不要放行 3307/6333/9200/8081/8000

## 已知取舍

- 前端产物提交进 deploy 分支（换服务器零 Node 环境，代价是前端更新多一步构建提交）
- `conf/app_config.yaml` 中 MySQL 密码沿用开发版硬编码（`lzs/Lzs666`）—— 公网不可达所以风险可控；要加固可改 compose 环境变量 + yaml 同步
- 无 HTTPS（IP 直访）。上备案域名后可加 certbot，需要时再说
