# Lucky 公网部署清单（v1.1 PRD FR-05 · M1 里程碑）

> 目标：`https://<IP>.sslip.io` 浏览器无告警可访问，限流/熔断生效，冒烟 10 问通过。
> 前提：服务器可公网访问、80/443 可开放（PRD 13.2 阻塞级依赖）。

## 0. W1 首日验证（E-10 触发点，先行）

```bash
# 服务器上验证 sslip.io 解析 + 80/443 可达性（5 分钟定性，失败即启动 E-10 购域名）
dig +short $(curl -s ifconfig.me).sslip.io   # 应回解析到本机公网 IP
curl -I http://$(curl -s ifconfig.me).sslip.io   # 确认 80 端口可入站（证书签发依赖）
```
失败 → 按 PRD E-10 购入正式域名（约 ¥30-80/年），Caddyfile 中域名替换即可，其余步骤不变。

## 1. 基础环境（一次性）

```bash
sudo apt update && sudo apt install -y caddy git curl
curl -LsSf https://astral.sh/uv/install.sh | sh
corepack enable && corepack prepare pnpm@latest --activate
```

## 2. 代码与密钥

```bash
sudo useradd -m -s /bin/bash lucky && sudo mkdir -p /opt/lucky && sudo chown lucky /opt/lucky
# clone 仓库到 /opt/lucky（remote 按实际仓库地址）
cd /opt/lucky
cp .env.example .env && vim .env        # 填模型 Key；chmod 600 .env（十一节：密钥权限 600）
uv sync
```

## 3. 依赖服务与建库（幂等，可重跑）

```bash
bash bootstrap.sh          # 或 make up；自检失败按提示修复后重跑
```

## 4. 前端构建产物 + 进程守护

```bash
cd /opt/lucky/frontend && pnpm install && pnpm build   # 产物 dist/（Caddy 托管）
sudo cp /opt/lucky/deploy/lucky-backend.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lucky-backend
# systemd 单元内路径按实际用户名调整（User=lucky、WorkingDirectory=/opt/lucky）
```

## 5. Caddy（HTTPS + 反代）

```bash
sudo vim /etc/caddy/Caddyfile   # 粘贴 deploy/Caddyfile，<SERVER_IP> 替换为公网 IP
sudo systemctl reload caddy
curl -I https://<IP>.sslip.io   # 200 + 有效证书；http:// 应 301 到 https
```

## 6. 公网防护开启（关键，本地默认全关）

`conf/app_config.yaml` → `security:` 段：

```yaml
security:
  rate_limit:
    enabled: true          # 单 IP ≤10 次/分钟，超限 429（PRD FR-05 规则 2）
    per_minute: 10
  cost_guard:
    enabled: true          # 日费用软上限 ¥5 告警 / 硬上限 ¥15 自动暂停（E-04）
    soft_limit: 5.0
    hard_limit: 15.0
```
改完 `sudo systemctl restart lucky-backend`。关闭任一开关即回退旧行为（Feature Flags 纪律）。

## 7. 数据库只读账号（十一节第一道硬防线）

```bash
mysql -h 127.0.0.1 -P 3307 -u root -p < /opt/lucky/deploy/mysql_readonly.sql
# 然后把 conf/app_config.yaml db_dw 段 user/password 改为 lucky_ro/<强密码>
# 验证：用 lucky_ro 执行 UPDATE/DELETE 必须被拒绝；防火墙仅放行 80/443/SSH，3307 不对公网开放
```

## 8. 主机加固（FR-03 衔接）

```bash
# SSH 密钥登录、禁密码（先确认自己的 authorized_keys 可用再执行！）
sudo vim /etc/ssh/sshd_config   # PasswordAuthentication no，然后 systemctl restart sshd
# 防火墙
sudo ufw allow 22/tcp && sudo ufw allow 80,443/tcp && sudo ufw enable
```

## 9. 日志轮转（E-06 磁盘满预防）

```bash
sudo tee /etc/logrotate.d/lucky > /dev/null <<'EOF'
/opt/lucky/logs/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF
# docker 日志限额（可选）：/etc/docker/daemon.json
# {"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}
```

## 10. 上线验证（13.3 口径）

- **冒烟**：题库前 10 题 + 1 次兜底输入（"你好，你能做什么"），全过 = 部署成功
- **限流**：1 分钟内连发 11 次 → 第 11 次 429 友好文案（PRD GWT）
- **守护**：`sudo systemctl kill -s KILL lucky-backend` → 30s 内 /api/models 恢复 200
- **熔断**：临时把 hard_limit 调成 0.01 重启 → 下一次请求 429 公告文案；验证后改回
- **一周观察**：每日探测 HTTPS 200（K2 起算）+ 日费用对账（对照 soft_limit）

## 11. 回滚

- 行为回滚：security 开关改 false → 重启（= v1.0 行为）
- 部署回滚：`git checkout <上一 tag>` + `sudo systemctl restart lucky-backend` + Caddy 不动
