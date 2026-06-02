# WAF Bypass 验证报告

**分支**: `fix/waf-slider-bypass`  
**日期**: 2026-06-02  
**状态**: ✅ 方案验证完成

---

## 问题

雪球 (`xueqiu.com`) 于 2026-06-02 凌晨启用了阿里云 WAF 滑动验证（"滑动验证页面"），导致 `xueqiu-crawler` 定时任务全部失败。

## 验证过程

### 测试矩阵

| # | 方案 | 工具 | 结果 | 备注 |
|:--:|------|------|:--:|------|
| 1 | 基础 Playwright | Playwright headless | ❌ | 滑动验证页，无法自动通过 |
| 2 | 多 cookies (45条) | Playwright | ❌ | cookie 数量和质量无关 |
| 3 | playwright-stealth 包 | Playwright + stealth | ❌ | 基础反检测无效 |
| 4 | 完整手动 stealth (8项) | Playwright | ❌ | 无法绕过指纹检测 |
| 5 | 人类轨迹滑块拖拽 | Playwright + OpenCV 替代 | ❌ | WAF 判定为 bot 轨迹 |
| 6 | **nodriver (real Chrome)** | nodriver 0.50.3 | ✅ | 3/3 用户全部成功 |

### 关键发现

1. **WAF 在浏览器指纹层面检测 bot**，而非单纯检测 cookies 或 IP
2. **滑块验证拒绝所有 headless 浏览器**的拖拽操作，即使轨迹完美
3. **nodriver 的底层指纹伪装**（CDP 级别 patch）能完全避免触发 WAF

### nodriver 测试结果

```
用户 5739488179: Elon翻开每一页 - 雪球 ✅
用户 6308001210: czy710 - 雪球 ✅
用户 4641860462: Waterzzz - 雪球 ✅
```

## 推荐方案

**将爬虫从 Playwright 迁移到 nodriver**

- nodriver 使用真实 Chrome，通过 CDP 级别 patch 消除了所有自动化检测点
- 无需滑块识别、无需第三方打码平台、无需 cookie 管理
- API 与 Playwright 类似（async），迁移成本可控

### 后续工作

- [ ] 将 `scripts/crawler.py` 迁移到 nodriver async API
- [ ] 保留文章解析逻辑，替换 Playwright 页面操作
- [ ] 测试 12 用户全量爬取稳定性
- [ ] 更新 cron 任务
