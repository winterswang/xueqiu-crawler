#!/usr/bin/env python3
"""
雪球 Playwright 自动登录 — 获取 Cookies

两种模式：
  --auto   用户名+密码自动登录（填账号密码，人工过验证码）
  --scan   扫码登录（弹出二维码截图，手机扫描）
  
成功后自动保存到 config/xueqiu_cookies.json
"""

import os, sys, json, argparse, time
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("请先安装 playwright: pip install playwright && playwright install chromium")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent
COOKIES_FILE = PROJECT_ROOT / "config" / "xueqiu_cookies.json"
SCREENSHOT_DIR = PROJECT_ROOT / "output"


def save_cookies(cookies: list, browser_context):
    """保存 cookies"""
    # 转换为字典格式
    cookies_dict = {}
    for c in cookies:
        cookies_dict[c["name"]] = c["value"]
    
    now = datetime.now()
    expires_at = now + timedelta(days=30)
    
    data = {
        "cookies": cookies_dict,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(COOKIES_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ Cookies 已保存到 {COOKIES_FILE}")
    print(f"   过期时间: {expires_at.strftime('%Y-%m-%d %H:%M')}")
    print(f"   Cookies 数量: {len(cookies_dict)}")


def login_auto(username: str, password: str, headless: bool = False):
    """账号密码登录（需人工过验证码）"""
    print(f"🌐 启动浏览器，账号: {username}, {'headless' if headless else '有头'}")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,  # 服务器环境用 headless=True
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="zh-CN"
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
        """)
        
        page = context.new_page()
        
        # Step 1: 打开雪球首页触发登录弹窗
        print("📄 打开雪球首页...")
        page.goto("https://xueqiu.com", timeout=60000)
        page.wait_for_timeout(3000)
        
        # Step 2: 点击登录按钮
        print("🔍 寻找登录入口...")
        login_selectors = [
            'a:has-text("登录")',
            'button:has-text("登录")',
            '.login-btn',
            '[data-test="login"]',
            'text=登录'
        ]
        clicked = False
        for sel in login_selectors:
            try:
                elem = page.query_selector(sel)
                if elem and elem.is_visible():
                    elem.click()
                    print(f"   点击登录: {sel}")
                    clicked = True
                    break
            except Exception:
                continue
        
        if not clicked:
            print("⚠️  未找到登录按钮，尝试直接导航到登录页...")
            page.goto("https://xueqiu.com/snowman/login", timeout=30000)
        
        page.wait_for_timeout(3000)
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / "xueqiu_login_1.png"))
        print("📸 截图已保存: output/xueqiu_login_1.png")
        
        # Step 3: 切换到密码登录模式
        print("🔐 切换到密码登录...")
        pwd_selectors = [
            'text=密码登录',
            'text=账号密码',
            '[data-type="password"]',
            'text=账号密码登录',
            '.tab-password',
        ]
        for sel in pwd_selectors:
            try:
                elem = page.query_selector(sel)
                if elem and elem.is_visible():
                    elem.click()
                    print(f"   切换到: {sel}")
                    break
            except Exception:
                continue
        
        page.wait_for_timeout(2000)
        page.screenshot(path=str(SCREENSHOT_DIR / "xueqiu_login_2.png"))
        
        # Step 4: 填入账号密码
        print(f"📝 填入账号: {username}")
        
        # 尝试各种可能的输入框选择器
        phone_input = page.query_selector('input[placeholder*="手机"], input[placeholder*="账号"], input[name="username"], input[name="telephone"], input[type="text"]')
        if phone_input:
            phone_input.fill(username)
            print("   ✅ 账号已填入")
        else:
            print("   ⚠️  未找到手机号输入框，请手动输入")
        
        page.wait_for_timeout(500)
        
        pwd_input = page.query_selector('input[type="password"], input[placeholder*="密码"]')
        if pwd_input:
            pwd_input.fill(password)
            print("   ✅ 密码已填入")
        else:
            print("   ⚠️  未找到密码输入框，请手动输入")
        
        page.screenshot(path=str(SCREENSHOT_DIR / "xueqiu_login_3.png"))
        
        # Step 5: 提交登录
        print("🚀 提交登录...")
        submit_selectors = [
            'button:has-text("登录")',
            'button:has-text("登 录")',
            '.login-submit',
            '[type="submit"]',
        ]
        for sel in submit_selectors:
            try:
                elem = page.query_selector(sel)
                if elem and elem.is_visible():
                    elem.click()
                    print(f"   点击: {sel}")
                    break
            except Exception:
                continue
        
        # Step 6: 等待登录完成（可能需要人工过验证码）
        print("\n⏳ 等待登录完成（可能需要人工过验证码）...")
        print("   浏览器窗口已打开，请：")
        print("   1. 如果出现滑块验证码 → 手动拖动完成")
        print("   2. 如果出现短信验证 → 在浏览器中输入验证码")
        print("   3. 完成后等待页面跳转到雪球首页")
        print("\n   登录成功后按 Enter 继续...")
        
        input(">>> ")
        
        # Step 7: 验证登录状态
        page.wait_for_timeout(2000)
        cookies = context.cookies()
        has_auth = any(c["name"] in ("xq_a_token", "u") for c in cookies)
        
        if has_auth:
            save_cookies(cookies, context)
        else:
            print("\n⚠️  未检测到有效的登录 cookies")
            print("   可能登录未完成，请检查浏览器页面")
            # 仍然保存，尝试使用
            save_cookies(cookies, context)
        
        browser.close()


def login_scan():
    """扫码登录"""
    print("🌐 启动浏览器，等待扫码...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="zh-CN"
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
        """)
        
        page = context.new_page()
        page.goto("https://xueqiu.com", timeout=60000)
        page.wait_for_timeout(3000)
        
        # 点击登录
        for sel in ['a:has-text("登录")', 'text=登录']:
            elem = page.query_selector(sel)
            if elem and elem.is_visible():
                elem.click()
                break
        
        page.wait_for_timeout(3000)
        
        # 截取二维码
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        qr_path = SCREENSHOT_DIR / "xueqiu_qr.png"
        
        # 尝试截取二维码区域
        qr_elem = page.query_selector('.qrcode-img, img[src*="qrcode"], .login-qrcode img')
        if qr_elem:
            qr_elem.screenshot(path=str(qr_path))
        else:
            page.screenshot(path=str(qr_path))
        
        print(f"📱 二维码已保存: {qr_path}")
        print("   请用雪球 App 扫描二维码登录")
        print("   扫码成功后等待 10 秒...")
        
        time.sleep(10)
        page.wait_for_timeout(5000)
        
        cookies = context.cookies()
        has_auth = any(c["name"] in ("xq_a_token", "u") for c in cookies)
        
        if has_auth:
            save_cookies(cookies, context)
        else:
            print("\n⚠️  未检测到有效 cookies，可能需要更长时间")
            print("   尝试再等 10 秒...")
            time.sleep(10)
            cookies = context.cookies()
            save_cookies(cookies, context)
        
        browser.close()


def main():
    parser = argparse.ArgumentParser(description="雪球 Playwright 登录获取 Cookies")
    parser.add_argument("--auto", action="store_true", help="账号密码自动登录")
    parser.add_argument("--scan", action="store_true", help="扫码登录")
    parser.add_argument("--headless", action="store_true", help="无头模式（服务器环境）")
    parser.add_argument("--username", "-u", help="雪球手机号/账号")
    parser.add_argument("--password", "-p", help="雪球密码")
    
    args = parser.parse_args()
    
    # 从环境变量读凭证
    username = args.username or os.environ.get("XUEQIU_USERNAME")
    password = args.password or os.environ.get("XUEQIU_PASSWORD")
    
    if args.auto:
        if not username or not password:
            print("❌ 需要账号和密码")
            print("   方式1: python scripts/login.py --auto -u 手机号 -p 密码")
            print("   方式2: export XUEQIU_USERNAME=手机号 && export XUEQIU_PASSWORD=密码")
            print("   方式3: python scripts/login.py --scan（扫码登录）")
            sys.exit(1)
        login_auto(username, password, headless=args.headless)
    elif args.scan:
        login_scan()
    else:
        print("雪球登录工具")
        print()
        print("   --auto -u 手机号 -p 密码   账号密码登录")
        print("   --scan                     扫码登录")
        print()
        print("示例:")
        print("  python scripts/login.py --auto -u 13800138000 -p mypassword")
        print("  python scripts/login.py --scan")

if __name__ == "__main__":
    main()
