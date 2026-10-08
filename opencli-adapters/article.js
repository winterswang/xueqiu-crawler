import { cli, Strategy } from '@jackwener/opencli/registry';
import { ArgumentError, CliError } from '@jackwener/opencli/errors';
import { htmlToMarkdown } from '@jackwener/opencli/utils';

// 正文容器候选（按"精确度"排序，先命中先用）。
// 与 xueqiu-monitor/src/detail_fetcher.py 的 _ARTICLE_SELECTORS 逐项一致 ——
// 那份是 2026-09-20 实测出来的：新浪 div.article = 正文、.article-content =
// 整站菜单堆、东财只有 #ContentBody 命中。
// 2026-10-08 复核雪球：专栏页 div.article / #artibody / .article-content 全空，
// article 命中且与 opencli「无 selector」的默认选择（main||article||body）结果
// 逐字相同（短帖 414 字 / 长文 19780 字），所以这条链对雪球是等价的。
const ARTICLE_SELECTORS = [
    'div.article', '#artibody', '.article-content', 'article',
    '.article-content-detail', '#ContentBody', '.content', 'body',
];

const HTTP_URL_PATTERN = /^https?:\/\//i;

// 去噪 JS：与 opencli 自带 `browser extract` 的实现
// (@jackwener/opencli/dist/src/browser/extract.js buildExtractHtmlJs) 保持一致，
// 因为 markdown 转换用的是同一个 htmlToMarkdown，只有 HTML 清洗等价，输出才等价。
// drop 列表与属性剥离逐项照抄，勿随手增删。
const DROP_SELECTORS = [
    'script', 'style', 'noscript', 'template',
    'nav', 'header', 'footer', 'aside',
    'iframe', 'svg', 'canvas',
    'form', 'button', 'input', 'select', 'textarea',
    '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]', '[role="complementary"]',
    '[aria-hidden="true"]',
];

function buildCloneHtmlJs(selector) {
    return '(() => {' +
        'const sel = ' + JSON.stringify(selector) + ';' +
        'let root = null;' +
        'try { root = document.querySelector(sel); } catch (e) { return null; }' +
        'if (!root) return null;' +
        'const clone = root.cloneNode(true);' +
        'const drop = ' + JSON.stringify(DROP_SELECTORS) + ';' +
        'for (const q of drop) {' +
            'for (const n of clone.querySelectorAll(q)) n.remove();' +
        '}' +
        'const walker = document.createTreeWalker(clone, NodeFilter.SHOW_ELEMENT);' +
        'let n = walker.currentNode;' +
        'while (n) {' +
            'if (n.nodeType === 1) {' +
                'const el = n;' +
                'for (const a of [...el.attributes]) {' +
                    'if (a.name.startsWith("on") || a.name === "style" || a.name.startsWith("data-")) el.removeAttribute(a.name);' +
                '}' +
            '}' +
            'n = walker.nextNode();' +
        '}' +
        'return clone.outerHTML || "";' +
    '})()';
}

const READ_TITLE_JS = '(function(){return document.title || "";})()';

const BLOCK_GUARD = "(function(){var t=(document.title||'')+' '+((document.body&&document.body.innerText)||'').slice(0,300);return /您的访问被阻断|request has been blocked|可能对网站造成安全威胁|potential threats to the server|访问被拦截|滑动验证|请按住滑块|访问验证|访问触发保护|完成人机验证|安全限制|访问频繁|website-login|405 forbidden|http 405|405 not allowed/.test(t)?'BLOCKED':'OK';})()";

cli({
    site: 'web',
    name: 'article',
    access: 'read',
    description: '读取任意网页正文（雪球 / 新浪 / 东财 / 公告页通用），输出干净 markdown',
    strategy: Strategy.COOKIE,
    // func 里自己 goto 目标 URL，跳过 opencli 的默认预导航（没有 domain 也不该预导航）。
    navigateBefore: false,
    browser: true,
    args: [
        { name: 'url', positional: true, required: true, help: '网页绝对 URL（http/https）' },
        { name: 'min-chars', type: 'int', default: 300, help: '容器命中阈值：正文达到该字数即停止探测更宽的选择器' },
        { name: 'selector', help: '可选。只用这一个选择器（调试用）' },
    ],
    columns: ['url', 'title', 'chars', 'selector'],
    func: async (page, kwargs) => {
        const rawUrl = String(kwargs.url ?? '').trim();
        if (!HTTP_URL_PATTERN.test(rawUrl)) {
            throw new ArgumentError('web article requires an absolute http(s) URL: ' + rawUrl);
        }
        // http → https：沿用 xueqiu-monitor detail_fetcher 的既有行为（cninfo 的
        // 公告链接是 http://，站点本身支持 https）。
        const url = rawUrl.replace(/^http:\/\//i, 'https://');
        const minChars = Math.max(1, Number(kwargs['min-chars']) || 300);
        const onlySelector = String(kwargs.selector ?? '').trim();
        const selectors = onlySelector ? [onlySelector] : ARTICLE_SELECTORS;

        await page.goto(url);
        const guard = await page.evaluate(BLOCK_GUARD);
        if (guard === 'BLOCKED') {
            // 用专属错误码而不是 CommandExecutionError(COMMAND_EXEC)：调用方需要
            // 把「风控」和「普通抓取失败」分开处理，前者要标 waf_detected / 走兜底，
            // 后者要重试。同码就只能靠中文字串区分，太脆。
            throw new CliError(
                'BLOCKED_WAF',
                '风控验证页（滑动验证 / 访问频繁）——这不是"没有数据"',
                '请在已连上 opencli 的 Chrome 里打开雪球完成滑块验证，然后重跑',
            );
        }
        await page.wait(3);

        const title = String((await page.evaluate(READ_TITLE_JS)) || '').trim();

        let best = '';
        let bestSel = '';
        for (const sel of selectors) {
            const html = await page.evaluate(buildCloneHtmlJs(sel));
            if (!html) continue;
            const content = htmlToMarkdown(html).trim();
            if (content.length > best.length) {
                best = content;
                bestSel = sel;
            }
            // 先命中先赢：精确容器优先，不再试后面更宽的选择器。
            if (best.length >= minChars) break;
        }

        return [{
            url: url,
            title: title,
            content: best,
            selector: bestSel,
            chars: best.length,
        }];
    },
});
