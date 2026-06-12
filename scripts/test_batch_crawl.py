#!/usr/bin/env python3
"""手动端到端测试：5人/组共享session，组间重启，统计WAF触发率"""

import asyncio, json, sys, os, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crawler_nodriver import XueqiuCrawlerNodriver

GROUPS = [
    # 第1组: Elon, czy710, 海豚, MZInvest, 林氪
    ['5739488179', '6308001210', '6249637706', '9951187724', '1156957441'],
    # 第2组: Waterzzz, 逸修1, 超级鹿鼎公, 林文丰, 永庆好公司
    ['4641860462', '1936609590', '8790885129', '3181890538', '6865675576'],
    # 第3组: 港股解码, 价值投资新经济
    ['3075122481', '7680894870'],
]

CONFIG_PATH = str(Path(__file__).resolve().parent / "config" / "config.yaml")

async def run_group(accounts: list[str], group_num: int):
    """运行一组用户，每个用户独立统计"""
    crawler = XueqiuCrawlerNodriver(CONFIG_PATH)
    
    # Override accounts
    all_accounts = crawler.accounts
    crawler.accounts = [a for a in all_accounts if a.get('id') in accounts]
    
    print(f"\n{'='*60}")
    print(f"📦 第{group_num}组: {', '.join(a['name'] for a in crawler.accounts)}")
    print(f"{'='*60}")
    
    t0 = time.time()
    results = []
    
    for i, account in enumerate(crawler.accounts):
        uid, name = account['id'], account['name']
        t1 = time.time()
        result = await crawler._crawl_one_user_opencli(account, 20)
        elapsed = time.time() - t1
        
        status = "✅" if result['saved_articles'] > 0 else "🔴"
        print(f"  {status} {name}: 发现{result['new_articles_available']}篇, 保存{result['saved_articles']}篇 ({elapsed:.0f}s)")
        results.append(result)
    
    group_time = time.time() - t0
    saved = sum(r['saved_articles'] for r in results)
    waf_users = sum(1 for r in results if r.get('error') or (r['saved_articles']==0 and r['new_articles_available']>0))
    
    print(f"  → 组耗时{group_time:.0f}s | 保存{saved}篇 | WAF用户{waf_users}/{len(accounts)}")
    return results

async def main():
    all_results = []
    for i, group_ids in enumerate(GROUPS, 1):
        results = await run_group(group_ids, i)
        all_results.extend(results)
        if i < len(GROUPS):
            print(f"\n  ⏳ 组间休息30s（session冷却）...")
            await asyncio.sleep(30)
    
    print(f"\n{'='*60}")
    total_saved = sum(r['saved_articles'] for r in all_results)
    total_waf = sum(1 for r in all_results if r.get('error') or (r['saved_articles']==0 and r['new_articles_available']>0))
    print(f"📊 总计: 保存{total_saved}篇 | WAF用户{total_waf}/{len(all_results)}")

if __name__ == '__main__':
    asyncio.run(main())
