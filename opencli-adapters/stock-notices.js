import { cli } from '@jackwener/opencli/registry';
import { ArgumentError, EmptyResultError } from '@jackwener/opencli/errors';
import { fetchXueqiuJson, formatChinaDate } from './utils.js';

// Symbol normalization mirroring the packaged comments.js contract.
const XUEQIU_SYMBOL_PATTERN = /^(?:[A-Z]{2}\d{5,6}|\d{4,6}|[A-Z]{1,5}(?:\.[.-][A-Z]{1,2})?)$/;

function normalizeSymbolInput(raw) {
    const symbol = String(raw ?? '').trim().replace(/^\$/, '').toUpperCase();
    if (!symbol) throw new ArgumentError('xueqiu stock-notices requires a symbol');
    if (/^HTTPS?:\/\//.test(symbol)) throw new ArgumentError('xueqiu stock-notices only accepts a symbol, not a URL');
    if (!XUEQIU_SYMBOL_PATTERN.test(symbol)) {
        throw new ArgumentError(`xueqiu stock-notices received an invalid symbol: ${symbol}`);
    }
    if (/^\d{6}$/.test(symbol)) {
        const head = symbol[0];
        return (('69'.indexOf(head) >= 0) ? 'SH' : (('03802'.indexOf(head) >= 0) ? 'SZ' : 'BJ')) + symbol;
    }
    if (/^\d{4,5}$/.test(symbol)) {
        return symbol.padStart(5, '0');
    }
    return symbol;
}

function stripHtml(html) {
    return (html || '')
        .replace(/<a[^>]+>.*?<\/a>/g, ' ')
        .replace(/<[^>]+>/g, ' ')
        .replace(/&nbsp;/g, ' ')
        .replace(/&amp;/g, '&')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/\$[^$()]*\([^)]*\)\$/g, ' ')
        .replace(/\$/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

function firstLink(html) {
    const m = String(html || '').match(/href="(https?:\/\/[^"]+)"/);
    return m ? m[1] : '';
}

cli({
    site: 'xueqiu',
    name: 'stock-notices',
    access: 'read',
    description: '获取个股公告（statuses/stock_timeline.json?source=公告 API，需登录态）',
    domain: 'xueqiu.com',
    browser: true,
    args: [
        { name: 'symbol', positional: true, required: true, help: '股票代码，如 SH600519 / PDD / 00700' },
        { name: 'limit', type: 'int', default: 20, help: '返回条数（上限 100）' },
    ],
    columns: ['title', 'type', 'created_at', 'url'],
    func: async (page, kwargs) => {
        const symbol = normalizeSymbolInput(kwargs.symbol);
        const limit = Math.max(1, Math.min(100, Number(kwargs.limit) || 20));
        await page.goto('https://www.xueqiu.com');
        await page.wait(2);

        const rows = [];
        const pageSize = 10;
        const maxPages = Math.ceil(limit / pageSize) + 1;
        for (let pageNumber = 1; pageNumber <= maxPages; pageNumber += 1) {
            const url = `https://www.xueqiu.com/statuses/stock_timeline.json`
                + `?symbol_id=${encodeURIComponent(symbol)}&count=${pageSize}`
                + `&source=${encodeURIComponent('公告')}&page=${pageNumber}`;
            const data = await fetchXueqiuJson(page, url);
            const items = Array.isArray(data?.list) ? data.list : [];
            if (items.length === 0) break;
            for (const item of items) {
                const desc = String(item.description ?? '');
                const title = stripHtml(desc);
                if (title.length < 5) continue;
                const typeMatch = title.match(/\[(.+?)\]/);
                rows.push({
                    title,
                    type: typeMatch ? typeMatch[1] : '公告',
                    created_at: formatChinaDate(item.created_at),
                    url: firstLink(desc) || (item.target ? String(item.target) : ''),
                });
            }
            if (rows.length >= limit) break;
            if (items.length < pageSize) break;
        }
        if (rows.length === 0) {
            throw new EmptyResultError(`xueqiu/stock-notices ${symbol}`, '无公告数据（未登录时该接口不可用，请先 opencli auth login xueqiu）');
        }
        return rows.slice(0, limit);
    },
});
