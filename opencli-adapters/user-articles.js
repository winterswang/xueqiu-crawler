import { cli } from '@jackwener/opencli/registry';
import { CommandExecutionError } from '@jackwener/opencli/errors';
import { ArgumentError } from '@jackwener/opencli/errors';

const USER_ID_PATTERN = /^\d+$/;

function buildExtractJs(userId, targetCount, maxScrolls) {
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
        'const toNumber = (raw) => {' +
            'const text = String(raw || "").replace(/[^0-9.万亿]/g, "");' +
            'const m = text.match(/^([0-9]+(?:\\.[0-9]+)?)(万|亿)?/);' +
            'if (!m) return 0;' +
            'const value = parseFloat(m[1]);' +
            'if (m[2] === "万") return Math.round(value * 10000);' +
            'if (m[2] === "亿") return Math.round(value * 100000000);' +
            'return Math.round(value);' +
        '};' +
        'const out = [];' +
        'document.querySelectorAll("article.timeline__item").forEach((item) => {' +
            'const ownEl = item.querySelector("a.date-and-source[href]");' +
            'const ownMatch = ownEl ? (ownEl.getAttribute("href") || "").match(/\\/(\\d+)$/) : null;' +
            'const statusId = ownMatch ? ownMatch[1] : "";' +
            'if (!statusId) return;' +
            'const contentEl = item.querySelector(".timeline__item__content");' +
            'const text = contentEl ? (contentEl.textContent || "").trim() : "";' +
            'const firstLine = text.split("\\n")[0].trim().slice(0, 100);' +
            'const authorEl = item.querySelector(".user-name");' +
            'const replyEl = item.querySelector(".replay-count");' +
            'const likeEl = item.querySelector(".like-count");' +
            'const forwardEl = item.querySelector(".retweet-count");' +
            'const timeText = ownEl ? (ownEl.textContent || "").split("·")[0].trim() : "";' +
            'out.push({' +
                'article_id: statusId,' +
                'title: firstLine,' +
                'author: authorEl ? (authorEl.textContent || "").trim() : "",' +
                'time: timeText,' +
                'created_at: timeText,' +
                'likes: toNumber(likeEl ? likeEl.textContent : ""),' +
                'replies: toNumber(replyEl ? replyEl.textContent : ""),' +
                'forwards: toNumber(forwardEl ? forwardEl.textContent : ""),' +
                'text: text,' +
                'is_column: !!item.querySelector(".timeline__item__content--longtext"),' +
                'url: "https://xueqiu.com/' + userId + '/" + statusId,' +
            '});' +
        '});' +
        'return JSON.stringify(out);' +
    '})()';
}

const BLOCK_GUARD = "(function(){var t=(document.title||'')+' '+((document.body&&document.body.innerText)||'').slice(0,300);return /您的访问被阻断|request has been blocked|可能对网站造成安全威胁|potential threats to the server|访问被拦截|滑动验证|请按住滑块|访问验证|安全限制|访问频繁|website-login|405 forbidden|http 405|405 not allowed/.test(t)?'BLOCKED':'OK';})()";

cli({
    site: 'xueqiu',
    name: 'user-articles',
    access: 'read',
    description: '获取指定用户的最新动态（专栏文章 / 帖子）列表',
    domain: 'xueqiu.com',
    browser: true,
    args: [
        { name: 'user_id', required: true, help: '雪球用户 ID（纯数字）' },
        { name: 'count', type: 'int', default: 20, help: '期望返回的条数' },
        { name: 'page', type: 'int', default: 1, help: '页码，1 表示最新一屏' },
    ],
    columns: ['article_id', 'title', 'author', 'time', 'likes', 'replies', 'forwards', 'url'],
    func: async (page, kwargs) => {
        const userId = String(kwargs.user_id ?? '').trim();
        if (!USER_ID_PATTERN.test(userId)) {
            throw new ArgumentError('xueqiu user-articles received an invalid user_id: ' + userId);
        }
        const count = Math.max(1, Number(kwargs.count) || 20);
        const pageNo = Math.max(1, Number(kwargs.page) || 1);
        const target = count * pageNo + 4;
        const maxScrolls = Math.min(40, pageNo * 6 + 4);

        await page.goto('https://xueqiu.com/u/' + userId);
        const guard = await page.evaluate(BLOCK_GUARD);
        if (guard === 'BLOCKED') {
            throw new CommandExecutionError(
                '风控验证页（滑动验证 / 访问频繁）——这不是"没有数据"',
                '请在已连上 opencli 的 Chrome 里打开雪球完成滑块验证，然后重跑',
            );
        }
        await page.wait(3);

        const raw = await page.evaluate(buildExtractJs(userId, target, maxScrolls));
        let items = [];
        try {
            items = typeof raw === 'string' ? JSON.parse(raw) : (raw || []);
        } catch {
            items = [];
        }
        return (Array.isArray(items) ? items : []).slice(0, count * pageNo);
    },
});

