"""
IP 级限流中间件（v1.1 PRD FR-05 规则 2 / E-03 防滥用）

链路位置：main.py 注册后位于所有路由之前，仅拦截 POST /api/query（问数入口，
唯一会产生 LLM 费用的接口）；超限返回 429 + 友好文案，不含堆栈与内部路径。

设计决策（why）：
  1. 限流放应用层而非 Caddy 插件 —— Caddy 原生无 rate_limit 模块（需 xcaddy 自编译，
     升级维护成本高）；应用层实现可测试、可配置、随 Feature Flag 关闭即旧行为，
     且与费用熔断（cost_guard）同层协作语义清晰。反代只负责 HTTPS 与转发。
  2. 内存滑动窗口而非计数器 —— 计数器有边界突刺（窗口切换瞬间可双倍通过），
     滑动窗口对"11 次/分钟收到 429"的 PRD GWT 语义更精确；单 IP 只存时间戳 deque，
     内存占用可忽略。
  3. 取 IP 优先读 X-Forwarded-For 首段 —— 公网走 Caddy 反代后 request.client.host
     是 127.0.0.1，不读转发头则全站共享一个窗口（等于全局限流，误伤所有人）。
     仅信任自建反代（部署文档约束防火墙只放行 80/443，攻击者无法伪造直连）。
  4. 锁粒度到"每 IP 校验"整体临界区 —— check+record 必须原子，否则并发下窗口
     边界可能超放；每分钟 10 次的量级下锁竞争可忽略。
"""
import threading
import time
from collections import deque

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.conf.app_config import app_config

# 每 IP 一个时间戳队列（epoch 秒）；进程重启清零 = 限流重置，可接受（防滥用非记账）
_windows: dict[str, deque[float]] = {}
_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    """取真实客户端 IP：X-Forwarded-For 首段（自建反代注入）→ 兜底 socket 地址"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # 多级代理时首段是原始客户端，后续是途经代理（自建链路下首段可信）
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> bool:
    """滑动窗口判定：窗口内请求数 < per_minute 则放行并记录，否则拒绝。

    必须整体持锁：check 与 append 是同一临界区，拆开会有并发超放窗口。
    """
    cfg = app_config.security.rate_limit
    now = time.time()
    with _lock:
        window = _windows.setdefault(ip, deque())
        # step 1: 惰性清理 —— 只在访问该 IP 时弹出过期时间戳，避免后台定时扫描
        while window and now - window[0] >= 60.0:
            window.popleft()
        # step 2: 判定 + 记录（放行才 append，被拒的请求不占窗口额度）
        if len(window) >= cfg.per_minute:
            return False
        window.append(now)
        return True


async def demo_guard_middleware(request: Request, call_next) -> Response:
    """公网防护总中间件：费用熔断前置检查 → IP 限流 → 放行到路由。

    只对 POST /api/query 生效（/api/models、/api/capabilities 是免费只读接口，
    不限流——前端模型下拉若被限流会误伤正常浏览）。
    """
    # step 0: 开关短路 —— 两个防护都关闭时零开销直通（默认本地行为 = v1.0）
    security = app_config.security
    guard_active = security.rate_limit.enabled or security.cost_guard.enabled
    if not (guard_active and request.method == "POST" and request.url.path == "/api/query"):
        return await call_next(request)

    # step 1: 费用熔断前置 —— 当日硬上限已触发 → 全部拒绝并返回公告文案（E-04）
    from app.services.cost_guard import cost_guard
    allowed, notice = cost_guard.check_allowed()
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": notice},
            headers={"Retry-After": "3600"},
        )

    # step 2: IP 限流 —— 超限 429（估算初值 10 次/分钟，阈值可按费用校准）
    if security.rate_limit.enabled and not _check_rate_limit(_client_ip(request)):
        return JSONResponse(
            status_code=429,
            content={
                "detail": (
                    f"提问有点太频繁啦，每分钟最多 {security.rate_limit.per_minute} 次，"
                    "请稍等片刻再试。"
                )
            },
            headers={"Retry-After": "60"},
        )

    # step 3: 全部通过 → 进入正常路由（SSE 流式返回）
    return await call_next(request)


# ---- 使用示例（验证方式）----
# 1) 默认关闭回归：uv run uvicorn main:app 后连发 20 次 POST /api/query，无 429（= v1.0 行为）
# 2) 开启验证（conf/app_config.yaml: security.rate_limit.enabled=true）：
#    1 分钟内第 11 次请求 → 429 {"detail": "提问有点太频繁啦..."}（PRD FR-05 GWT）
# curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/api/query \
#   -H "Content-Type: application/json" -d '{"query":"你好","thread_id":"t1"}'
