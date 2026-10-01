import { cli } from '@jackwener/opencli/registry';
import { CommandExecutionError } from '@jackwener/opencli/errors';
import { ArgumentError } from '@jackwener/opencli/errors';

const SYMBOL_PATTERN = /^[A-Z]{0,2}\d{4,6}[A-Z]?$/;

const CLICK_NEWS_TAB_JS = ' (() => {' +
    'const wanted = ["资讯", "新闻"];' +
    'const els = Array.from(document.querySelectorAll("a, div, span, li"));' +
    'const tab = els.find((e) => wanted.indexOf((e.textContent || "").trim()) >= 0 && e.children.length === 0);' +
    'if (!tab) return "TAB_NOT_FOUND";' +
    'tab.click();' +
    'return "CLICKED";' +
' })()';

function buildExtractJs(targetCount, maxScrolls) {
    return ' (async () => {' +
        'const countItems = () => document.querySelectorAll("article.timeline__item").length;' +
        'const target = ' + targetCount + ';' +
        'for (let i = 0; i < ' + maxScrolls + '; i++) {' +
            'if (countItems() >= target) break;' +
            'const before = countItems();' +
            'const height = document.body.scrollHeight;' +
            'window.scrollTo(0, height);' +
            'await new Promise((resolve) => {' +
                'let timer = null;' +
                'const ob = new MutationObserver(() => {' +
                    'if (document.body.scrollHeight > height || countItems() > before) {' +
                        'if (timer) clearTimeout(timer);' +
                        'ob.disconnect();' +
                        'setTimeout(resolve, 400);' +
                    '}' +
                '});' +
                'timer = setTimeout(() => { ob.disconnect(); resolve(); }, 1500);' +
                'ob.observe(document.body, { childList: true, subtree: true });' +
            '});' +
        '}' +
        'const out = [];' +
        'document.querySelectorAll("article.timeline__item").forEach((item) => {' +
            'let statusId = "";' +
            'item.querySelectorAll("a[href]").forEach((a) => {' +
                'if (statusId) return;' +
                'const m = (a.getAttribute("href") || "").match(/^\\/S\\/[A-Za-z0-9]+\\/(\\d+)$/);' +
                'if (m) statusId = m[1];' +
            '});' +
            'if (!statusId) return;' +
            'const titleEl = item.querySelector(".timeline__item__title");' +
            'const contentEl = item.querySelector(".timeline__item__content");' +
            'const sourceEl = item.querySelector(".source");' +
            'const dateEl = item.querySelector("a.date-and-source");' +
            'const title = titleEl ? (titleEl.textContent || "").trim() : "";' +
            'const text = contentEl ? (contentEl.textContent || "").trim() : "";' +
            'const source = sourceEl ? (sourceEl.textContent || "").replace(/^[·\\s]+/, "").trim() : "";' +
            'const dateText = dateEl ? (dateEl.textContent || "").split("·")[0].trim() : "";' +
            'out.push({' +
                'id: statusId,' +
                'title: title || text.split("\\n")[0].trim().slice(0, 100),' +
                'text: text,' +
                'source: source,' +
                'created_at: dateText,' +
                'link: "https://xueqiu.com/S/SYMBOL_PLACEHOLDER/" + statusId,' +
            '});' +
        '});' +
        'return JSON.stringify(out);' +
    ' })()';
}

const BLOCK_GUARD = "(function(){var t=(document.title||'')+' '+((document.body&&document.body.innerText)||'').slice(0,300);return /您的访问被阻断|request has been blocked|可能对网站造成安全威胁|potential threats to the server|访问被拦截|滑动验证|请按住滑块|访问验证|安全限制|访问频繁|website-login/.test(t)?'BLOCKED':'OK';})()";

cli({
    site: 'xueqiu',
    name: 'news',
    access: 'read',
    description: '获取个股资讯（行情页「资讯」标签，DOM 抓取）',
    domain: 'xueqiu.com',
    browser: true,
    args: [
        { name: 'symbol', positional: true, required: true, help: '股票代码，如 SH600519 / AAPL / 00700' },
        { name: 'limit', type: 'int', default: 20, help: '返回条数' },
    ],
    columns: ['id', 'title', 'source', 'created_at', 'link'],
    func: async (page, kwargs) => {
        const symbol = String(kwargs.symbol ?? '').trim().replace(/^\$/, '').toUpperCase();
        if (!SYMBOL_PATTERN.test(symbol)) {
            throw new ArgumentError('xueqiu news received an invalid symbol: ' + symbol);
        }
        let normalized = symbol;        if (/^\d{6}$/.test(symbol)) {            const head = symbol[0];            normalized = ('69'.indexOf(head) >= 0 ? 'SH' : ('03802'.indexOf(head) >= 0 ? 'SZ' : 'BJ')) + symbol;        }
        const limit = Math.max(1, Number(kwargs.limit) || 20);

        await page.goto('https://xueqiu.com/S/' + normalized);
        const guard = await page.evaluate(BLOCK_GUARD);
        if (guard === 'BLOCKED') {
            throw new CommandExecutionError(
                '风控验证页（滑动验证 / 访问频繁）——这不是"没有数据"',
                '请在已连上 opencli 的 Chrome 里打开雪球完成滑块验证，然后重跑',
            );
        }
        await page.wait(3);
        await page.evaluate(CLICK_NEWS_TAB_JS);
        await page.wait(3);

        const js = buildExtractJs(limit + 2, Math.min(30, Math.ceil(limit / 10) + 6));
        const raw = await page.evaluate(js.split('SYMBOL_PLACEHOLDER').join(normalized));
        let items = [];
        try {
            items = typeof raw === 'string' ? JSON.parse(raw) : (raw || []);
        } catch {
            items = [];
        }
        return (Array.isArray(items) ? items : []).slice(0, limit);
    },
});

