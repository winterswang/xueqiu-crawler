#!/usr/bin/env python3
"""爬取指定用户组 — 每组独立 session，降低 WAF 触发率"""

import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crawler_nodriver import XueqiuCrawlerNodriver

GROUPS = {
    '1': ['5739488179', '6308001210', '6249637706', '9951187724', '3966435964'],
    '2': ['1156957441', '4641860462', '1936609590', '8790885129', '9507152383'],
    '3': ['3181890538', '6865675576', '3075122481', '7680894870'],
}

async def main():
    group = sys.argv[1] if len(sys.argv) > 1 else '1'
    ids = GROUPS[group]

    config_path = str(Path(__file__).resolve().parent.parent / "config" / "config.yaml")
    crawler = XueqiuCrawlerNodriver(config_path)

    # Filter accounts
    crawler.accounts = [a for a in crawler.accounts if a.get('id') in ids]
    print(f"📦 第{group}组: {', '.join(a['name'] for a in crawler.accounts)}")

    result = await crawler.crawl_all_users(max_articles=20)
    print(f"\n📊 结果: 发现{result['total_new']}篇, 保存{result['total_saved']}篇, ",
          f"WAF用户{sum(1 for u in result.get('users',[]) if u.get('saved_articles',0)==0 and u.get('new_articles_available',0)>0)}/{len(result.get('users',[]))}")


if __name__ == '__main__':
    asyncio.run(main())
