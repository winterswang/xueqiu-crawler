#!/usr/bin/env python3
"""
WAF Bypass 验证 — Phase 1: Playwright Stealth 增强
测试多种 stealth 策略是否能绕过阿里云 WAF 滑块验证

用法: python scripts/test_waf_bypass.py [--strategy all|stealth|profile|stealth+extras]
"""
import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
COOKIES_FILE = PROJECT_DIR / "config" / "xueqiu_cookies.json"
MON_COOKIES_FILE = Path("/root/.xueqiu_crawler/cookies.json")

TARGET_URLS = [
    ("用户主页", "https://xueqiu.com/u/5739488179"),
    ("股票页", "https://xueqiu.com/S/SH600519"),
]


def load_cookies():
    """加载 cookies 文件"""
    cookies_list = []

    # 尝试 crawler cookies
    if COOKIES_FILE.exists():
        with open(COOKIES_FILE) as f:
            d = json.load(f)
        if isinstance(d.get("cookies"), dict):
            cookies_list = [{"name": k, "value": v, "domain": ".xueqiu.com", "path": "/"}
                            for k, v in d["cookies"].items()]

    # 合并 monitor cookies (更丰富)
    if MON_COOKIES_FILE.exists():
        with open(MON_COOKIES_FILE) as f:
            mon_cookies = json.load(f)
        if isinstance(mon_cookies, list):
            for c in mon_cookies:
                if not any(x["name"] == c["name"] for x in cookies_list):
                    cookies_list.append(c)

    logger.info(f"加载 {len(cookies_list)} 个 cookies")
    return cookies_list


def check_page_state(page, label):
    """检测页面状态: 正常 / WAF / 其他"""
    try:
        title = page.title()
        content = page.content()
    except Exception:
        return {"state": "error", "title": "N/A"}

    has_waf = "aliyun_waf" in content or "_waf_" in content
    has_slider = "滑动验证" in title or "滑动验证" in content[:2000]
    has_timeline = "timeline__item" in content
    has_login = "登录" in title and "雪球" in title

    if has_slider:
        state = "WAF_SLIDER"
    elif has_waf:
        state = "WAF_CHALLENGE"
    elif has_timeline or has_login or "雪球" in title:
        state = "PASSED"
    else:
        state = "UNKNOWN"

    return {
        "state": state,
        "title": title[:60],
        "size": len(content),
        "has_waf": has_waf,
        "has_timeline": has_timeline,
    }


def test_strategy(strategy_name, setup_fn, cookies_list):
    """测试一种策略"""
    logger.info(f"\n{'='*60}")
    logger.info(f"🧪 测试策略: {strategy_name}")
    logger.info(f"{'='*60}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

        # 添加 cookies
        context.add_cookies(cookies_list)

        # 应用自定义 setup（stealth 等）
        setup_fn(context)

        results = {}

        # 先访问首页预热
        page = context.new_page()
        page.goto("https://xueqiu.com", timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        hp_state = check_page_state(page, "首页")
        logger.info(f"  首页: {hp_state['state']} | {hp_state['title']}")
        page.close()

        # 测试每个目标 URL
        for label, url in TARGET_URLS:
            page = context.new_page()
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
                state = check_page_state(page, label)
                logger.info(f"  {label}: {state['state']} | {state['title']} | {state['size']}B")
                results[label] = state
            except Exception as e:
                logger.error(f"  {label}: ERROR {str(e)[:80]}")
                results[label] = {"state": "ERROR", "error": str(e)[:80]}
            finally:
                page.close()

        browser.close()
        return results


def setup_baseline(context):
    """基线: 无额外反检测"""
    pass


def setup_stealth(context):
    """使用 playwright-stealth 包"""
    # stealth_sync 作用于 page, 我们需要通过 init_script 方式应用
    # playwright-stealth 主要用于 page 级别, 在 context 级别加基础 init_script
    context.add_init_script("""
        // 基础反检测 (stealth 核心)
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
    """)


def setup_stealth_full(context):
    """完整 stealth: playwright-stealth + 额外反检测"""
    context.add_init_script("""
        // === 1. Navigator 属性伪装 ===
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const plugins = [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
                ];
                plugins.item = (i) => plugins[i] || null;
                plugins.namedItem = (n) => plugins.find(p => p.name === n) || null;
                plugins.refresh = () => {};
                return plugins;
            }
        });
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
        Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

        // === 2. WebGL 指纹伪装 ===
        try {
            const getParam = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParam.call(this, parameter);
            };
            const getParam2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParam2.call(this, parameter);
            };
        } catch(e) {}

        // === 3. 权限 API 伪装 ===
        const originalQuery = window.navigator.permissions?.query;
        if (originalQuery) {
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
            );
        }

        // === 4. chrome.runtime 伪装 ===
        window.chrome = {
            runtime: {},
            loadTimes: () => {},
            csi: () => {},
            app: {}
        };

        // === 5. iframe 检测绕过 ===
        Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
            get: function() {
                return window;
            }
        });

        // === 6. 覆盖 headless 检测 ===
        const originalUserAgent = navigator.userAgent;
        Object.defineProperty(navigator, 'userAgent', {
            get: () => originalUserAgent.replace('Headless', '')
        });

        // === 7. 屏幕/窗口尺寸一致性 ===
        Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
        Object.defineProperty(screen, 'availHeight', { get: () => 1080 });
        Object.defineProperty(screen, 'colorDepth', { get: () => 24 });

        // === 8. Canvas 指纹微干扰 ===
        try {
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type, ...args) {
                // 轻微噪声，不影响视觉但改变 hash
                if (type === 'image/png' && this.width > 10 && this.height > 10) {
                    const ctx = this.getContext('2d');
                    if (ctx) {
                        const imageData = ctx.getImageData(0, 0, 1, 1);
                        imageData.data[3] = imageData.data[3] ^ 1; // flip alpha last bit
                        ctx.putImageData(imageData, 0, 0);
                    }
                }
                return origToDataURL.apply(this, [type, ...args]);
            };
        } catch(e) {}
    """)


def setup_stealth_package(context):
    """使用 playwright-stealth 包的完整配置（hook context）"""
    stealth = Stealth(
        navigator_languages=True,
        navigator_plugins=True,
        navigator_permissions=True,
        webgl_vendor=True,
        navigator_webdriver=True,
        navigator_hardware_concurrency=True,
        navigator_user_agent=True,
        chrome_app=True,
        chrome_csi=True,
        chrome_load_times=True,
        chrome_runtime=True,
        iframe_content_window=True,
        media_codecs=True,
        hairline=True,
    )
    stealth.hook_playwright_context(context)


def main():
    parser = argparse.ArgumentParser(description="WAF Bypass 验证")
    parser.add_argument("--strategy", choices=["all", "baseline", "stealth", "stealth_full", "profile"],
                        default="all", help="测试策略")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--show", action="store_true", help="有头模式")
    args = parser.parse_args()

    if args.show:
        args.headless = False

    cookies = load_cookies()

    strategies = {
        "baseline": ("基线 (无反检测)", setup_baseline),
        "stealth": ("基础 Stealth", setup_stealth),
        "stealth_pkg": ("playwright-stealth 包", setup_stealth_package),
        "stealth_full": ("完整手动 Stealth", setup_stealth_full),
    }

    if args.strategy != "all":
        strategies = {args.strategy: strategies[args.strategy]}

    all_results = {}
    for name, (label, setup_fn) in strategies.items():
        results = test_strategy(label, setup_fn, cookies)
        all_results[name] = results
        time.sleep(2)

    # 汇总
    print(f"\n{'='*60}")
    print("📊 汇总结果")
    print(f"{'='*60}")
    print(f"{'策略':<20} {'用户页':<15} {'股票页':<15}")
    print("-" * 50)
    for strategy, results in all_results.items():
        user_state = results.get("用户主页", {}).get("state", "?")
        stock_state = results.get("股票页", {}).get("state", "?")
        user_i = "✅" if user_state == "PASSED" else "❌"
        stock_i = "✅" if stock_state == "PASSED" else "❌"
        print(f"{strategy:<20} {user_i} {user_state:<13} {stock_i} {stock_state}")

    passed = any(
        r.get("用户主页", {}).get("state") == "PASSED"
        for r in all_results.values()
    )
    if passed:
        print("\n✅ 有策略成功绕过 WAF！")
    else:
        print("\n❌ Stealth 增强无法绕过滑块验证，需 Phase 2 (打码平台)")


if __name__ == "__main__":
    main()
