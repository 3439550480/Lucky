"""E2E 冒烟（Playwright 转正）：登录稳定性 / 热卖侧边栏 / 补货全链路 / TEMP 权限矩阵

覆盖 final-verify 验收矩阵，兼作常驻回归门禁（有失败即 return 1）：
  A 店长 M001：登录后主界面稳定 12s（白屏回归）、热卖排行侧边栏、补货建议全链路
  B 临时工 T001：权限矩阵（销售额放行 / 库存放行 / 入库拒绝 / 补货拒绝）
    + 跨轮不劫持（上一轮「出入库登记」拒绝不得劫持本轮补货判定 —— final-verify 修复回归）

用法：确保后端 8000 + 前端 5173 已起，再 `uv run python tests/e2e_smoke.py`
截图：写入系统临时目录（不污染工作区），失败时按打印路径取证
"""
import random
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5173"
SHOT = Path(tempfile.gettempdir()) / "shopkeeper-e2e"
results: list[tuple[str, bool, str]] = []
errors: list[str] = []


def rec(name: str, ok: bool, extra: str = "") -> None:
    """记录一条检查：立刻打印 PASS/FAIL，末尾统一汇总并决定退出码"""
    results.append((name, ok, extra))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + extra) if extra else ""), flush=True)


def watch(page) -> None:
    """收集前端未捕获错误：pageerror 与 console.error 都是白屏类事故的前兆信号"""
    page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))
    page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text[:200]}") if m.type == "error" else None)


def shot(page, name: str) -> None:
    SHOT.mkdir(parents=True, exist_ok=True)
    path = SHOT / f"{name}.png"
    page.screenshot(path=str(path))
    print(f"   截图: {path}")


def login(page, staff_id: str, pin: str = "123456") -> None:
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    page.fill("#staff-id", staff_id)
    page.fill("#pin", pin)
    page.click("button[type=submit]")


def wait_idle(page, timeout: int = 180000) -> None:
    """等本轮问答收尾：先确认进入「运行中」，再等回到「就绪」（LLM 链路较慢，默认 180s）"""
    try:
        page.wait_for_selector("text=运行中", timeout=15000)
    except Exception:
        pass
    page.wait_for_selector("text=就绪", timeout=timeout)


def ask(page, text: str, timeout: int = 180000) -> None:
    page.fill("textarea", text)
    page.keyboard.press("Enter")
    wait_idle(page, timeout=timeout)


def run_store_manager(browser) -> None:
    """A 段：店长视角（登录稳定性 + 热卖侧边栏 + 补货全链路）"""
    ctx = browser.new_context()
    page = ctx.new_page()
    watch(page)
    login(page, "M001")

    try:
        page.wait_for_selector("text=王志远", timeout=20000)
        rec("A1 登录成功，身份区出现（店长）", True)
    except Exception:
        rec("A1 登录成功，身份区出现（店长）", False, page.locator("body").inner_text()[:200])

    # 白屏回归：用户曾报"出现一瞬然后清空"，必须静置观察而非登录即断言
    page.wait_for_timeout(12000)
    rec("A2 12 秒后身份区仍在（白屏回归）", page.locator("text=王志远").count() > 0)
    rec("A3 热卖排行侧边栏存在", page.locator("text=热卖排行").count() > 0)

    ask(page, "生成鞋类的补货计划")
    rec("A4 补货建议表格出现（全链路通）", page.locator("text=补货建议").count() > 0)
    rec("A5 补货后主界面仍在", page.locator("text=王志远").count() > 0)
    shot(page, "a-store-manager")
    ctx.close()


def run_temp(browser) -> None:
    """B 段：临时工权限矩阵（定稿 §3/§5.6：TEMP = inventory.read + dataquery.query）"""
    ctx = browser.new_context()
    page = ctx.new_page()
    watch(page)
    login(page, "T001")
    page.wait_for_timeout(2000)

    try:
        page.wait_for_selector("text=周小雨", timeout=20000)
        rec("B1 TEMP 登录成功", True)
    except Exception:
        rec("B1 TEMP 登录成功", False, page.locator("body").inner_text()[:200])
    rec("B2 角色徽标=临时工", page.locator("text=临时工").count() > 0)

    # 有 dataquery.query → 销售额问答放行（定稿对临时工开放，勿按旧计划文字断言"应拒"）
    ask(page, "上个月最后一天门店销售额是多少？")
    sales_ok = page.locator("text=没有「销售数据查询」权限").count() == 0
    rec("B3 销售额问答放行（有 dataquery.query）", sales_ok,
        "" if sales_ok else page.locator("body").inner_text()[-300:])

    # 有 inventory.read → 库存查询放行
    ask(page, "15241012-05-43 还有多少库存？")
    rec("B4 库存查询放行（有 inventory.read）",
        page.locator("text=没有「库存查询」权限").count() == 0)

    # 无 inventory.write → flow_guard 前置拒绝（不写库；单号随机使脚本可重复执行）
    doc = "RK" + str(random.randint(2026110000, 2026119999))
    ask(page, f"入库 {doc} 15241012-05-43 3 件")
    denied_write = page.locator("text=没有「出入库登记」权限").count()
    rec("B5 入库被拒（无 inventory.write）", denied_write > 0,
        "" if denied_write > 0 else page.locator("body").inner_text()[-300:])
    shot(page, "b-temp-write-denied")

    # 同会话紧接着问补货：既验矩阵另一边界，也验"一次性路由标记入口归零"
    ask(page, "生成鞋类的补货计划")
    rp_denied = page.locator("text=没有「门店补货建议」权限").count() > 0
    rec("B6 补货被拒（无 replenish.store）", rp_denied,
        "" if rp_denied else page.locator("body").inner_text()[-300:])
    # 劫持判定用"计数不增"：页面历史里 B5 的入库拒绝文案本就在，不能按全页文本有无判
    rec("B7 本轮未被上一轮入库拒绝劫持（残留修复回归）",
        page.locator("text=没有「出入库登记」权限").count() == denied_write)
    shot(page, "b-temp-replenish-denied")
    ctx.close()


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        run_store_manager(browser)
        run_temp(browser)
        browser.close()

    print("\n==== 汇总 ====")
    failed = [r for r in results if not r[1]]
    print(f"通过 {len(results) - len(failed)}/{len(results)}")
    for name, _, extra in failed:
        print(f"  FAIL {name} :: {extra}")

    print("=== pageerror/console.error ===")
    if errors:
        for e in errors[:10]:
            print(e)
    else:
        print("（无）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
