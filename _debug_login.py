"""临时：登录提交后的页面状态 dump"""
from playwright.sync_api import sync_playwright

errors = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))
    page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}") if m.type == "error" else None)
    page.on("requestfailed", lambda r: errors.append(f"[reqfail] {r.url} {r.failure}"))
    page.goto("http://localhost:5173")
    page.wait_for_load_state("networkidle")
    page.fill("#staff-id", "M001")
    page.fill("#pin", "123456")
    page.click("button[type=submit]")
    page.wait_for_timeout(8000)
    print("=== 提交后页面文本 ===")
    print(page.locator("body").inner_text()[:500])
    print("=== 错误 ===")
    for e in errors[:12]:
        print(e)
    page.screenshot(path="d:/Code/VScode/shopkeeper-agent/_debug_login.png")
    browser.close()
