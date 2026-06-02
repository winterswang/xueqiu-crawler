#!/usr/bin/env python3
"""
Phase 2: 阿里云智能滑块验证识别
策略: 人类轨迹模拟 → 拖动滑块到最右端

用法: python scripts/slider_solver.py [--debug]
"""
import argparse
import json
import logging
import math
import random
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent


def load_all_cookies():
    """加载所有可用的 cookies"""
    all_cookies = []

    # crawler cookies
    cf = PROJECT_DIR / "config" / "xueqiu_cookies.json"
    if cf.exists():
        with open(cf) as f:
            d = json.load(f)
        if isinstance(d.get("cookies"), dict):
            for k, v in d["cookies"].items():
                all_cookies.append({"name": k, "value": v, "domain": ".xueqiu.com", "path": "/"})

    # monitor cookies
    mf = Path("/root/.xueqiu_crawler/cookies.json")
    if mf.exists():
        with open(mf) as f:
            mon = json.load(f)
        if isinstance(mon, list):
            existing = {c["name"] for c in all_cookies}
            for c in mon:
                if c["name"] not in existing:
                    all_cookies.append(c)

    return all_cookies


def human_like_slide(page, slider_el, track_width, debug=False):
    """
    模拟人类拖动滑块

    阿里云智能验证: 滑块需要从 0 拖动到 track_width 位置
    关键: 轨迹必须像人类（先加速后减速，微抖动）
    """
    box = slider_el.bounding_box()
    if not box:
        logger.error("无法获取滑块位置")
        return False

    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    end_x = start_x + track_width

    logger.info(f"  滑块: ({start_x:.0f}, {start_y:.0f}) -> ({end_x:.0f}, {start_y:.0f}), 距离={track_width:.0f}px")

    # Step 1: 鼠标移动到滑块（带随机偏移）
    page.mouse.move(
        start_x + random.uniform(-5, 5),
        start_y + random.uniform(-3, 3),
    )
    time.sleep(random.uniform(0.1, 0.2))

    # Step 2: 按下鼠标
    page.mouse.down()
    time.sleep(random.uniform(0.05, 0.1))

    # Step 3: 分三段模拟人类拖拽
    # 阶段A: 加速 (0-50%)
    # 阶段B: 匀速/微减速 (50-90%)
    # 阶段C: 减速到停止 (90-100%)

    total_steps = random.randint(60, 100)
    current_x = start_x

    for i in range(total_steps):
        progress = i / total_steps

        # 人类轨迹: 先快后慢 (ease-out)
        if progress < 0.5:
            # 快速移动
            eased_progress = progress * 1.3
        elif progress < 0.9:
            # 中速
            eased_progress = 0.5 + (progress - 0.5) * 0.8
        else:
            # 减速到目标
            eased_progress = 0.82 + (progress - 0.9) * 1.8

        eased_progress = min(eased_progress, 1.0)
        target_x = start_x + (track_width * eased_progress)

        # Y轴微抖动
        jitter_y = random.uniform(-1.5, 1.5)

        # 随机回退（人类特征）
        if random.random() < 0.03:  # 3% 概率轻微回退
            target_x -= random.uniform(1, 3)

        page.mouse.move(
            max(start_x, min(target_x, end_x)),
            start_y + jitter_y,
        )
        time.sleep(random.uniform(0.005, 0.02))

    # Step 4: 最后精确移动到终点
    for _ in range(random.randint(3, 8)):
        page.mouse.move(
            end_x + random.uniform(-0.5, 0.5),
            start_y + random.uniform(-0.5, 0.5),
        )
        time.sleep(random.uniform(0.01, 0.03))

    # Step 5: 释放
    time.sleep(random.uniform(0.05, 0.15))
    page.mouse.up()

    if debug:
        logger.info(f"  拖拽完成: {total_steps} 步, 终点={end_x:.0f}px")

    return True


def detect_slider(page):
    """检测滑块验证并返回滑块元素"""
    # 检查是否是阿里云智能滑块
    slider = page.locator("#aliyunCaptcha-sliding-slider")
    if slider.count() > 0 and slider.first.is_visible():
        return slider.first, "aliyun_smart"

    # 检查传统NC滑块
    nc_btn = page.locator("#nc_1_n1z")
    if nc_btn.count() > 0 and nc_btn.first.is_visible():
        return nc_btn.first, "nc_slider"

    return None, None


def solve_slider(page, debug=False):
    """尝试解决滑块验证"""
    slider_el, slider_type = detect_slider(page)

    if not slider_el:
        logger.info("  未检测到滑块，可能已通过")
        return "no_slider"

    logger.info(f"  检测到滑块: {slider_type}")

    if slider_type == "aliyun_smart":
        # 阿里云智能验证: 滑块需要拖到最右端
        # 计算滑块轨道宽度
        body = page.locator("#aliyunCaptcha-sliding-body")
        if body.count() > 0:
            body_box = body.first.bounding_box()
            slider_box = slider_el.bounding_box()
            track_width = body_box["width"] - slider_box["width"]
            logger.info(f"  轨道宽={body_box['width']:.0f}, 滑块宽={slider_box['width']:.0f}, 距离={track_width:.0f}")
        else:
            track_width = 320  # 默认
            logger.info(f"  使用默认距离={track_width}")

        human_like_slide(page, slider_el, track_width, debug)
        return "slid"

    elif slider_type == "nc_slider":
        # 传统 NC 滑块: 需要计算背景图缺口位置
        # TODO: OpenCV gap detection
        logger.warning("  NC滑块暂不支持自动解决")
        return "unsupported"

    return "unknown"


def wait_for_result(page, timeout=15):
    """等待滑块结果"""
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(0.5)

        # 检查是否通过了验证
        title = page.title()
        if "滑动验证" not in title and "验证" not in title:
            # 检查是否有实际内容
            has_timeline = page.locator(".timeline__item").count() > 0
            has_content = "雪球" in title or has_timeline
            if has_content:
                elapsed = time.time() - start
                logger.info(f"  ✅ 验证通过! ({elapsed:.1f}s), title={title[:50]}")
                return True

        # 检查是否有新的验证（多阶段）
        text = page.locator("#aliyunCaptcha-sliding-text").inner_text() if page.locator("#aliyunCaptcha-sliding-text").count() > 0 else ""
        if "验证通过" in text:
            logger.info(f"  ✅ 滑块通过检测!")
            # 等页面刷新
            time.sleep(2)
            return True
        elif "验证失败" in text or "再试一次" in text:
            logger.warning("  ❌ 滑块被拒绝！")
            return False

        # 检查是否有新的验证类型弹出
        nc = page.locator("#nc_1_n1z")
        if nc.count() > 0 and nc.first.is_visible():
            logger.info("  🔄 出现新验证层（NC滑块）")
            return False

    logger.warning(f"  ⏱️ {timeout}s 超时，状态不明")
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--show", action="store_true", help="有头模式")
    parser.add_argument("--max-tries", type=int, default=3)
    args = parser.parse_args()

    if args.show:
        args.headless = False

    cookies = load_all_cookies()
    logger.info(f"加载 {len(cookies)} 个 cookies")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        context.add_cookies(cookies)
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => false });")

        page = context.new_page()
        page.goto("https://xueqiu.com", timeout=30000)
        page.wait_for_timeout(3000)

        # 目标页面
        page.goto("https://xueqiu.com/u/5739488179", timeout=30000)
        page.wait_for_timeout(5000)

        logger.info(f"页面标题: {page.title()}")

        passed = False
        for attempt in range(1, args.max_tries + 1):
            logger.info(f"\n{'='*50}")
            logger.info(f"🔄 第 {attempt}/{args.max_tries} 次尝试")

            # 检查是否已经通过
            if page.locator(".timeline__item").count() > 0:
                logger.info("  页面已有内容，无需滑块")
                passed = True
                break

            result = solve_slider(page, debug=args.debug)
            logger.info(f"  滑块操作: {result}")

            if result == "no_slider":
                passed = True
                break

            if result in ("slid",):
                passed = wait_for_result(page, timeout=15)
                if passed:
                    break
                else:
                    # 刷新重试
                    logger.info("  刷新重试...")
                    page.goto("https://xueqiu.com/u/5739488179", timeout=30000)
                    page.wait_for_timeout(5000)

        if passed:
            logger.info("\n✅ 验证成功！")
            # 验证实际数据
            content = page.content()
            name_el = page.query_selector(".user-name, .username, .profile__name")
            name = name_el.inner_text().strip() if name_el else "?"
            logger.info(f"  用户名: {name}")
            logger.info(f"  页面大小: {len(content)} 字节")
        else:
            logger.warning(f"\n❌ {args.max_tries} 次尝试后仍未通过验证")

        page.screenshot(path="/tmp/slider_solver_result.png")
        browser.close()


if __name__ == "__main__":
    main()
