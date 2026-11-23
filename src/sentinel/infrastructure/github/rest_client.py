"""真实 GitHub REST 客户端（Stage 2 实现）。

这里先把签名和关键约束钉死，避免 Stage 2 再改上层契约：

  1. 认证与配额：Authorization: Bearer <token>，未认证 60 次/小时基本不可用。
  2. 三层增量：
       - since 参数：只拿水位之后的记录（服务端过滤，最省流量）
       - If-None-Match：带上次的 ETag，304 时不消耗配额
       - 本地去重键：兜底，防分页边界重复
  3. /issues 端点会混入 PR，映射时必须丢弃「带 pull_request 字段的条目」。
  4. /pulls 按 updated 倒序，遇到早于水位的记录可以提前停止翻页。
  5. 403/429 读 X-RateLimit-Remaining 与 Retry-After，用 tenacity 退避，不要裸重试。
"""

from __future__ import annotations

from ...domain.models import RepoMeta
from ...ports.github import FetchQuery, Page, RateStatus


class RestGitHub:
    """TODO(Stage 2)：基于 httpx.AsyncClient 实现。"""

    def __init__(
        self,
        *,
        token: str | None,
        api_url: str = "https://api.github.com",
        timeout: float = 30.0,
        per_page: int = 50,
        max_pages: int = 5,
    ) -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._per_page = per_page
        self._max_pages = max_pages

    async def get_repo_meta(self, repo_full_name: str) -> RepoMeta:
        raise NotImplementedError("Stage 2: GET /repos/{owner}/{repo}")

    async def fetch(self, query: FetchQuery) -> Page:
        raise NotImplementedError("Stage 2: 分层端点 + ETag + 分页 + 退避")

    async def rate_status(self) -> RateStatus:
        raise NotImplementedError("Stage 2: GET /rate_limit")

    async def aclose(self) -> None:
        return None
