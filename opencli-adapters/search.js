import { cli } from '@jackwener/opencli/registry';
import { Strategy } from '@jackwener/opencli/registry';
import { ArgumentError } from '@jackwener/opencli/errors';

const NON_DEFAULT = [
    { name: 'note-type', def: 'all' },
    { name: 'publish-time', def: 'anytime' },
    { name: 'scope', def: 'all' },
    { name: 'location', def: 'all' },
];

function buildExtractJs(targetCount, maxScrolls) {
    return ' (async () => {' +
        'const countCards = () => document.querySelectorAll("section.note-item").length;' +
        'const target = ' + targetCount + ';' +
        'for (let i = 0; i < ' + maxScrolls + '; i++) {' +
            'if (countCards() >= target) break;' +
            'const before = countCards();' +
            'const height = document.body.scrollHeight;' +
            'window.scrollTo(0, height);' +
            'await new Promise((resolve) => {' +
                'let timer = null;' +
                'const ob = new MutationObserver(() => {' +
                    'if (document.body.scrollHeight > height || countCards() > before) {' +
                        'if (timer) clearTimeout(timer);' +
                        'ob.disconnect();' +
                        'setTimeout(resolve, 400);' +
                    '}' +
                '});' +
                'timer = setTimeout(() => { ob.disconnect(); resolve(); }, 1800);' +
                'ob.observe(document.body, { childList: true, subtree: true });' +
            '});' +
        '}' +
        'const out = [];' +
        'const seen = {};' +
        'document.querySelectorAll("section.note-item").forEach((card) => {' +
            'let url = "";' +
            'card.querySelectorAll("a[href*=xsec_token]").forEach((a) => {' +
                'if (url) return;' +
                'const href = a.getAttribute("href") || "";' +
                'const m = href.match(/^\\/(?:search_result|explore)\\/([0-9a-zA-Z]+)\\?(.*)$/);' +
                'if (m) url = "https://www.xiaohongshu.com/explore/" + m[1] + "?" + m[2];' +
            '});' +
            'if (!url) {' +
                'const a2 = card.querySelector("a[href^=\\"/explore/\\"]");' +
                'if (a2) url = "https://www.xiaohongshu.com" + a2.getAttribute("href");' +
            '}' +
            'if (!url) return;' +
            'const m2 = url.match(/\\/explore\\/([0-9a-zA-Z]+)/);' +
            'const id = m2 ? m2[1] : url;' +
            'if (seen[id]) return;' +
            'seen[id] = 1;' +
            'const q = (sel) => { const e = card.querySelector(sel); return e ? (e.textContent || "").trim() : ""; };' +
            'out.push({ id: id, title: q(".title"), author: q(".name"), likes: q(".count"), published_at: q(".time"), url: url });' +
        '});' +
        'return JSON.stringify(out);' +
    ' })()';
}

cli({
    site: 'xiaohongshu',
    name: 'search',
    access: 'read',
    description: '搜索小红书笔记（搜索结果页 DOM 抓取，URL 自带 xsec_token）',
    domain: 'www.xiaohongshu.com',
    strategy: Strategy.COOKIE,
    navigateBefore: false,
    browser: true,
    args: [
        { name: 'query', required: true, positional: true, help: '搜索关键词' },
        { name: 'limit', type: 'int', default: 20, help: '返回条数' },
        { name: 'sort', type: 'string', default: 'comprehensive', choices: ['comprehensive', 'latest', 'most-liked', 'most-commented', 'most-collected'], help: '排序（本覆盖只支持 comprehensive）' },
        { name: 'note-type', type: 'string', default: 'all', choices: ['all', 'video', 'image'], help: '笔记类型（本覆盖只支持 all）' },
        { name: 'publish-time', type: 'string', default: 'anytime', choices: ['anytime', 'day', 'week', 'half-year'], help: '发布时间（本覆盖只支持 anytime）' },
        { name: 'scope', type: 'string', default: 'all', choices: ['all', 'seen', 'unseen', 'following'], help: '搜索范围（本覆盖只支持 all）' },
        { name: 'location', type: 'string', default: 'all', choices: ['all', 'same-city', 'nearby'], help: '位置距离（本覆盖只支持 all）' },
    ],
    columns: ['rank', 'title', 'author', 'likes', 'published_at', 'url'],
    func: async (page, kwargs) => {
        const query = String(kwargs.query ?? '').trim();
        if (!query) throw new ArgumentError('xiaohongshu search requires a non-empty query');

        if (String(kwargs.sort ?? 'comprehensive') !== 'comprehensive') {
            throw new ArgumentError('this local xiaohongshu search override only supports --sort comprehensive; run `opencli adapter reset xiaohongshu` to use the packaged adapter instead');
        }
        for (const spec of NON_DEFAULT) {
            const value = kwargs[spec.name] ?? spec.def;
            if (String(value) !== spec.def) {
                throw new ArgumentError('this local xiaohongshu search override does not support --' + spec.name + ' ' + value + ' (only ' + spec.def + ')');
            }
        }

        const limit = Math.max(1, Number(kwargs.limit) || 20);
        const url = 'https://www.xiaohongshu.com/search_result?keyword=' + encodeURIComponent(query) + '&source=web_search_result_notes';

        await page.goto(url);
        await page.waitFor ? await page.waitFor(3) : await page.wait(5);

        const js = buildExtractJs(limit + 2, Math.min(40, Math.ceil(limit / 10) + 8));
        const raw = await page.evaluate(js);
        let items = [];
        try {
            items = typeof raw === 'string' ? JSON.parse(raw) : (raw || []);
        } catch {
            items = [];
        }
        return (Array.isArray(items) ? items : [])
            .filter((row) => row.title)
            .slice(0, limit)
            .map((row, i) => ({ rank: i + 1, title: row.title, author: row.author, likes: row.likes, published_at: row.published_at, url: row.url }));
    },
});

