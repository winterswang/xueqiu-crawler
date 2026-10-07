"""Site adapters for Crawl Gateway."""

from crawl_gateway.adapters.opencli_client import OpencliArticleClient
from crawl_gateway.adapters.xueqiu import ArticleStore, XueqiuAdapter, load_accounts
from crawl_gateway.adapters.xueqiu_backend import XueqiuBackend
from crawl_gateway.adapters.xueqiu_nodriver import XueqiuNodriverAdapter

__all__ = [
    "ArticleStore",
    "OpencliArticleClient",
    "XueqiuAdapter",
    "XueqiuBackend",
    "XueqiuNodriverAdapter",
    "load_accounts",
]
