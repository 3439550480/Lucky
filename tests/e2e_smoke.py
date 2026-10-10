"""临时 E2E：登录 → 主界面稳定观察 12 秒（复现"一瞬后清空"）→ 权限拒绝 → 补货（跑完即删）"""
from playwright.sync_api import sync_playwright

errors = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))
    page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text[:200]}") if m.type == "error" else None)
    page.goto("http://localhost:5173")
    page.wait_for_load_state("networkidle")

    page.fill("#staff-id", "M001")
    page.fill("#pin", "123456")
    page.click("button[type=submit]")

    # 关键观察：登录后主界面是否稳定（用户报"出现一瞬然后清空"）
    try:
        page.wait_for_selector("text=王志远", timeout=20000)
        print("1. 登录成功，身份区出现")
    except Exception:
        print("1. FAIL: 登录后 20s 未出现身份区")
        print("body:", page.locator("body").inner_text()[:300])

    page.wait_for_timeout(12000)   # 观察 12 秒——复现"清空"
    still_there = page.locator("text=王志远").count()
    print(f"2. 12 秒后身份区仍在: {still_there > 0}")
    if still_there == 0:
        print("   body 此刻:", page.locator("body").inner_text()[:200])
        print("   *** 复现了'清空'！***")

    hot = page.locator("text=热卖排行").count()
    print(f"3. 热卖面板存在: {hot > 0}")

    if still_there > 0:
        page.fill("textarea", "生成鞋类的补货计划")
        page.keyboard.press("Enter")
        try:
            page.wait_for_selector("text=补货建议", timeout=60000)
            print("4. 补货表格出现（全链路通）")
        except Exception:
            print("4. FAIL: 补货表格未出现")
        page.wait_for_timeout(2000)
        still = page.locator("text=王志远").count()
        print(f"5. 补货后主界面仍在: {still > 0}")

    page.screenshot(path="d:/Code/VScode/shopkeeper-agent/_e2e_final.png")
    browser.close()

print("=== pageerror/console.error ===")
for e in errors[:10]:
    print(e)
if not errors:
    print("（无）")
