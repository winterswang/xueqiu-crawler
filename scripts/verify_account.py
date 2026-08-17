#!/usr/bin/env python3
"""临时验证单个雪球账号能否正常爬取。用法: python scripts/verify_account.py <user_id>"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crawler_nodriver import XueqiuCrawlerNodriver

async def main():
    user_id = sys.argv[1] if len(sys.argv) > 1 else '4111857140'
    config_path = str(Path(__file__).resolve().parent.parent / "config" / "config.yaml")
    crawler = XueqiuCrawlerNodriver(config_path)
    crawler.accounts = [a for a in crawler.accounts if a.get('id') == user_id]
    if not crawler.accounts:
        print(f"❌ 账号 {user_id} 不在配置中")
        return
    account = crawler.accounts[0]
    print(f"🎯 验证账号: {account.get('name')} ({user_id})")
    print(f"   模式: {'OpenCLI' if crawler._use_opencli else 'nodriver'}")
    try:
        await crawler._start_browser()
        await crawler._warmup()
    except Exception as e:
        print(f"❌ 浏览器启动失败: {e}")
        return
    result = await crawler._crawl_one_user(account, max_articles=5)
    print(f"\n📊 结果:")
    print(f"   可用文章: {result.get('new_articles_available', '?')}")
    print(f"   新文章: {result.get('new_articles', '?')}")
    print(f"   保存: {result.get('saved_articles', '?')}")
    name = result.get('user_name') or result.get('name')
    if name:
        print(f"   抓取到昵称: {name}")

if __name__ == '__main__':
    asyncio.run(main())
