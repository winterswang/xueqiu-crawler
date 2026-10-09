from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import yaml

from crawl_gateway.models import AttemptResult, AttemptScope, AttemptStatus
from crawl_gateway.orchestrator import TaskSpec


class SiteAccessError(RuntimeError):
    def __init__(self, status: AttemptStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class ArticleClient(Protocol):
    def list_user_articles(self, user_id: str, count: int) -> list[dict]:
        pass

    def get_article_content(self, url: str) -> dict:
        pass


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    enabled: bool


class ArticleStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.data_dir / "index.json"
        self.index = self._load_index()

    def known_article_ids(self, user_id: str) -> set[str]:
        known: set[str] = set()
        for article in self.index.get("articles", {}).values():
            if article.get("user_id") == user_id:
                known.add(str(article.get("article_id", "")))

        history_dir = self.data_dir / "history" / user_id
        if history_dir.exists():
            for history_file in sorted(history_dir.glob("*.json"), reverse=True)[:7]:
                data = json.loads(history_file.read_text(encoding="utf-8"))
                known.update(
                    str(article.get("article_id"))
                    for article in data.get("articles", [])
                    if article.get("article_id")
                )

        user_dir = self.data_dir / user_id
        if user_dir.exists():
            known.update(path.stem for path in user_dir.glob("*.md"))
        known.discard("")
        return known

    def save_article(self, article: dict, user_id: str, now: datetime) -> str:
        user_dir = self.data_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        article_id = str(article.get("article_id", "unknown"))
        filepath = user_dir / f"{article_id}.md"
        temporary_path = filepath.with_suffix(".md.tmp")
        temporary_path.write_text(self._render_markdown(article, now), encoding="utf-8")
        os.replace(temporary_path, filepath)

        index_key = f"{user_id}_{article_id}"
        index_record = {
            "article_id": article_id,
            "user_id": user_id,
            "title": article.get("title", ""),
            "author": article.get("author", ""),
            "publish_time": article.get("publish_time", ""),
            "crawl_time": article.get("crawl_time", now.isoformat()),
            "file_path": str(filepath),
            "filepath": str(filepath),
        }
        self.index.setdefault("articles", {})[index_key] = index_record
        self.index["last_update"] = now.isoformat()
        self._save_index()
        return str(filepath)

    def save_history(self, user_id: str, articles: list[dict], now: datetime) -> Path:
        history_dir = self.data_dir / "history" / user_id
        history_dir.mkdir(parents=True, exist_ok=True)
        history_file = history_dir / f"{now.date().isoformat()}.json"
        history_data = {
            "date": now.date().isoformat(),
            "user_id": user_id,
            "article_count": len(articles),
            "articles": [
                {
                    "article_id": article.get("article_id"),
                    "title": article.get("title", "")[:50],
                    "crawl_time": article.get("crawl_time"),
                }
                for article in articles
            ],
        }
        history_file.write_text(
            json.dumps(history_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return history_file

    def _load_index(self) -> dict:
        if not self.index_file.exists():
            return {"articles": {}, "last_update": None, "history": {}}
        data = json.loads(self.index_file.read_text(encoding="utf-8"))
        data.setdefault("articles", {})
        data.setdefault("last_update", None)
        data.setdefault("history", {})
        return data

    def _save_index(self) -> None:
        temporary_path = self.index_file.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(self.index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary_path, self.index_file)

    @staticmethod
    def _render_markdown(article: dict, now: datetime) -> str:
        return "\n".join(
            [
                f"# {article.get('title', '无标题')}",
                "",
                f"> 作者：{article.get('author', '未知')} | 发布时间：{article.get('publish_time', '未知')}",
                f"> 点赞：{article.get('likes', 0)} | 评论：{article.get('comments', 0)}",
                f"> 原文链接：{article.get('url', article.get('link', ''))}",
                "",
                "---",
                "",
                article.get("content", ""),
                "",
                "---",
                "",
                f"*爬取时间：{now.strftime('%Y-%m-%d %H:%M:%S')}*",
            ]
        )


class XueqiuAdapter:
    def __init__(
        self,
        *,
        client: ArticleClient,
        data_dir: str | Path,
        accounts_path: str | Path = "config/accounts.yaml",
        now: Callable[[], datetime] = datetime.now,
        max_articles: int = 20,
        other_probe_limit: int = 3,
        is_error_content: Callable[[str, str], bool] | None = None,
        resolve_title: Callable[[str | None, str | None], str] | None = None,
    ) -> None:
        self._client = client
        self._store = ArticleStore(data_dir)
        self._accounts = load_accounts(accounts_path)
        self._now = now
        self._max_articles = max_articles
        self._other_probe_limit = other_probe_limit
        self._is_error_content = is_error_content or _default_is_error_content
        self._resolve_title = resolve_title or _default_resolve_title

    def execute(self, task: TaskSpec, backend: str) -> AttemptResult:
        if backend != "opencli":
            return AttemptResult(
                AttemptStatus.SKIPPED,
                backend,
                error="backend_not_implemented_by_xueqiu_adapter",
            )
        if task.resource_type != "user_timeline":
            return AttemptResult(
                AttemptStatus.SKIPPED,
                backend,
                error=f"unsupported_resource_type:{task.resource_type}",
            )
        account = self._accounts.get(task.resource_id)
        if account is None or not account.enabled:
            return AttemptResult(
                AttemptStatus.SKIPPED,
                backend,
                error="account_disabled_or_missing",
            )

        try:
            article_list = self._client.list_user_articles(
                task.resource_id, self._max_articles
            )
        except SiteAccessError as exc:
            return AttemptResult(exc.status, backend, error=str(exc))

        known_ids = self._store.known_article_ids(task.resource_id)
        new_articles = [
            article
            for article in article_list
            if article.get("article_id") and str(article["article_id"]) not in known_ids
        ]
        if not new_articles:
            return AttemptResult(
                AttemptStatus.DUPLICATE if article_list else AttemptStatus.NO_UPDATE,
                backend,
                new_articles=0,
            )

        columns = [article for article in new_articles if article.get("is_column")]
        others = [article for article in new_articles if not article.get("is_column")]
        ordered_articles = columns + others
        saved_articles: list[dict] = []
        saw_empty_content = False
        other_probes = 0

        for article in ordered_articles[: self._max_articles]:
            if not article.get("url"):
                continue
            if not article.get("is_column"):
                if other_probes >= self._other_probe_limit:
                    break
                other_probes += 1

            try:
                detail = self._client.get_article_content(str(article.get("url", "")))
            except SiteAccessError as exc:
                return AttemptResult(
                    exc.status,
                    backend,
                    error=str(exc),
                    saved_articles=len(saved_articles),
                    # 取正文这一步失败 = 列表已经拿到了，属详情级；只有 BLOCKED_WAF
                    # 是真正的「被拦」。网络/HTTP 这类传输失败仍按账号级，免得半开
                    # 探测时把「发不出去」误判成「站点可达」。
                    scope=(
                        AttemptScope.DETAIL
                        if exc.status is AttemptStatus.BLOCKED_WAF
                        else AttemptScope.ACCOUNT
                    ),
                )

            # 详情标题可能是站点栏目（`雪球-聪明的投资者都在这里`）或列表占位符
            # （`展开`）—— 与 crawler 走同一条兜底链（见 scripts/title_guard.py）。
            # 不能写成 `detail.get("title") or article.get("title","")`：空串才会走
            # `or` 的右边，站点栏目那种**非空但没用**的值会直接落盘。
            title = self._resolve_title(detail.get("title"), article.get("title"))
            content = str(detail.get("content") or article.get("text", ""))
            if title.startswith("回复@"):
                continue
            # 两边都没有可用标题 → 不入库（图片帖 / 页面没渲染全）。刻意**不**置
            # `saw_empty_content`：那是「详情抓空了」的信号，会把整账号判成
            # PARSE_ERROR；一篇图片帖不该背这个锅。
            if not title:
                continue
            if not content:
                saw_empty_content = True
                continue
            if self._is_error_content(title, content):
                return AttemptResult(
                    AttemptStatus.BLOCKED_WAF,
                    backend,
                    error=f"waf_content:{article.get('article_id')}",
                    saved_articles=len(saved_articles),
                    scope=AttemptScope.DETAIL,
                    # 撞到第一篇被拦就中止该账号，所以这里恒为 1 —— 是「遇到被拦的
                    # 账号数」而非篇数。要让 gateway 引擎写出的 .last_crawl_stats.json
                    # 也有这个字段（否则切到 CRAWL_ENGINE=gateway 后 #87 的盲区回归）。
                    blocked_articles=1,
                )

            crawl_time = self._now()
            merged = {
                "article_id": str(article.get("article_id", "")),
                "title": title,
                "author": article.get("author", account.name),
                "publish_time": article.get("time", ""),
                "content": content,
                "likes": article.get("likes", 0),
                "comments": article.get("replies", 0),
                "url": article.get("url", ""),
                "link": article.get("url", ""),
                "crawl_time": crawl_time.isoformat(),
                "is_column": True,
            }
            self._store.save_article(merged, task.resource_id, crawl_time)
            saved_articles.append(merged)

        if saved_articles:
            now = self._now()
            self._store.save_history(task.resource_id, saved_articles, now)
            return AttemptResult(
                AttemptStatus.SUCCESS,
                backend,
                new_articles=len(saved_articles),
                saved_articles=len(saved_articles),
            )
        if saw_empty_content:
            return AttemptResult(
                AttemptStatus.PARSE_ERROR,
                backend,
                error="article_detail_empty",
                new_articles=len(saved_articles),
                saved_articles=len(saved_articles),
            )
        return AttemptResult(
            AttemptStatus.NO_UPDATE,
            backend,
            new_articles=len(saved_articles),
            saved_articles=len(saved_articles),
        )


def load_accounts(path: str | Path) -> dict[str, Account]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"accounts config must be a mapping: {path}")
    accounts: dict[str, Account] = {}
    for item in raw.get("accounts", []):
        account_id = str(item.get("id", ""))
        accounts[account_id] = Account(
            id=account_id,
            name=str(item.get("name", account_id)),
            enabled=bool(item.get("enabled", True)),
        )
    return accounts


def _default_is_error_content(title: str, content: str) -> bool:
    from scripts.waf_bridge import is_error_page

    return is_error_page(title, content)


def _default_resolve_title(detail_title: str | None, list_title: str | None) -> str:
    from scripts.title_guard import resolve_title

    return resolve_title(detail_title or "", list_title or "")
