import { cli } from '@jackwener/opencli/registry';
import { CommandExecutionError } from '@jackwener/opencli/errors';
import { ArgumentError } from '@jackwener/opencli/errors';

const POST_URL_PATTERN = /xueqiu\.com\/(\d+)\/(\d+)/;

function buildExtractJs(targetCount, maxScrolls) {
    return ' (async () => {' +
        'const items = () => document.querySelectorAll("div.comment__item").length;' +
        'for (let i = 0; i < ' + maxScrolls + '; i++) {' +
            'if (items() >= ' + targetCount + ') break;' +
            'const before = items();' +
            'const height = document.body.scrollHeight;' +
            'window.scrollTo(0, height);' +
            'await new Promise((resolve) => {' +
                'let timer = null;' +
                'const ob = new MutationObserver(() => {' +
                    'if (document.body.scrollHeight > height || items() > before) {' +
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
            'const m = String(raw || "").replace(/[^0-9.万亿]/g, "").match(/^([0-9]+(?:\\.[0-9]+)?)(万|亿)?/);' +
            'if (!m) return 0;' +
            'const v = parseFloat(m[1]);' +
            'if (m[2] === "万") return Math.round(v * 10000);' +
            'if (m[2] === "亿") return Math.round(v * 100000000);' +
            'return Math.round(v);' +
        '};' +
        'const out = [];' +
        'const seen = {};' +
        'document.querySelectorAll("div.comment__item").forEach((item) => {' +
            'const id = item.getAttribute("data-id") || item.id || "";' +
            'if (!id || seen[id]) return;' +
            'seen[id] = 1;' +
            'const authorEl = item.querySelector(".user-name");' +
            'const bodyEl = item.querySelector("p");' +
            'const timeEl = item.querySelector(".time");' +
            'const likeEl = item.querySelector(".comment__item__like");' +
            'const text = bodyEl ? (bodyEl.textContent || "").trim() : "";' +
            'let replyTo = "";' +
            'const m = text.match(/^回复@([^:：]+)[:：]/);' +
            'if (m) replyTo = m[1].trim();' +
            'out.push({' +
                'id: id,' +
                'author: authorEl ? (authorEl.textContent || "").trim() : "",' +
                'text: text,' +
                'likes: toNumber(likeEl ? likeEl.textContent : ""),' +
                'created_at: timeEl && timeEl.firstChild ? (timeEl.firstChild.textContent || "").trim() : "",' +
                'reply_to: replyTo,' +
            '});' +
        '});' +
        'return JSON.stringify(out);' +
    ' })()';
}

const BLOCK_GUARD = "(function(){var t=(document.title||'')+' '+((document.body&&document.body.innerText)||'').slice(0,300);return /您的访问被阻断|request has been blocked|可能对网站造成安全威胁|potential threats to the server|访问被拦截|滑动验证|请按住滑块|访问验证|安全限制|访问频繁|website-login|405 forbidden|http 405|405 not allowed/.test(t)?'BLOCKED':'OK';})()";

cli({
    site: 'xueqiu',
    name: 'replies',
    access: 'read',
    description: '获取单条帖子下的回复列表（DOM 抓取）',
    domain: 'xueqiu.com',
    browser: true,
    args: [
        { name: 'post', positional: true, required: true, help: '帖子 URL，如 https://xueqiu.com/<uid>/<status_id>' },
        { name: 'limit', type: 'int', default: 20, help: '返回条数' },
    ],
    columns: ['id', 'author', 'text', 'likes', 'created_at', 'reply_to'],
    func: async (page, kwargs) => {
        const post = String(kwargs.post ?? '').trim();
        const m = post.match(POST_URL_PATTERN);
        if (!m) {
            throw new ArgumentError('xueqiu replies expects a post URL like https://xueqiu.com/<uid>/<status_id>, got: ' + post);
        }
        const limit = Math.max(1, Number(kwargs.limit) || 20);
        const url = 'https://xueqiu.com/' + m[1] + '/' + m[2];

        await page.goto(url);
        const guard = await page.evaluate(BLOCK_GUARD);
        if (guard === 'BLOCKED') {
            throw new CommandExecutionError(
                '风控验证页（滑动验证 / 访问频繁）——这不是"没有数据"',
                '请在已连上 opencli 的 Chrome 里打开雪球完成滑块验证，然后重跑',
            );
        }
        await page.wait(3);

        const raw = await page.evaluate(buildExtractJs(limit + 2, Math.min(30, Math.ceil(limit / 10) + 6)));
        let items = [];
        try {
            items = typeof raw === 'string' ? JSON.parse(raw) : (raw || []);
        } catch {
            items = [];
        }
        return (Array.isArray(items) ? items : []).slice(0, limit);
    },
});

