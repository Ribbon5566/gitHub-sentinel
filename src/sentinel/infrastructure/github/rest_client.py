"""真实 GitHub REST 客户端。

实现要点（与文件头的原始约束逐条对应）：

  1. 认证与配额：`Authorization: Bearer <token>`；未认证只有 60 次/小时，基本不可用。
  2. 三层增量：`since` 服务端过滤 + `If-None-Match`（304 不扣配额）+ 本地按键去重兜底。
     只对第 1 页发 ETag：四类资源都按 updated/created 倒序，首页不变就意味着没有新对象。
  3. `/issues` 会混入 PR，映射时丢弃带 `pull_request` 字段的条目，否则 issue 计数虚高。
  4. `/pulls` 与 `/releases` 都不支持 since，只能倒序取后客户端截断——这是
     `max_pages` 必须存在的理由，否则老仓库第一次跑就会翻几百页。
  5. 403/429 读 `X-RateLimit-Remaining` 与 `Retry-After` 退避；主限流等待时间可能
     长达半小时，超过 `max_retry_wait` 就直接抛错，不让进程干等。

`fetch_progress` 不带 ETag：每日窗口每次都不同，命不中 304，带上是纯粹的噪音。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from tenacity import AsyncRetrying, RetryCallState, stop_after_attempt

from ...domain.errors import GitHubError, RateLimitError
from ...domain.models import BODY_EXCERPT_LIMIT, RepoMeta
from ...domain.progress import CommitRecord, IssueRecord, PullRecord, ReleaseRecord
from ...ports.github import FetchQuery, Page, ProgressQuery, ProgressResult, RateStatus
from .mapping import (
    commit_to_event,
    has_next_link,
    issue_to_event,
    parse_commit,
    parse_issue,
    parse_pull,
    parse_release,
    parse_ts,
    pull_to_event,
    release_to_event,
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


class _TransientGitHubError(GitHubError):
    """5xx：重试有意义，和 4xx 区分开，避免把 401 也重试三遍。"""


@dataclass(slots=True)
class _Fetched:
    items: list[Any] = field(default_factory=list)
    etag: str | None = None
    not_modified: bool = False
    truncated: bool = False
    skipped: int = 0


class RestGitHub:
    def __init__(
        self,
        *,
        token: str | None,
        api_url: str = "https://api.github.com",
        timeout: float = 30.0,
        per_page: int = 50,
        max_pages: int = 5,
        retry_attempts: int = 3,
        max_retry_wait: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._per_page = per_page
        self._max_pages = max_pages
        self._retry_attempts = max(1, retry_attempts)
        self._max_retry_wait = max_retry_wait
        # 注入 sleep 是让限流退避可测：否则每个重试用例都要真等 2 秒
        self._sleep = sleep or asyncio.sleep

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-sentinel",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self._api_url, timeout=timeout, headers=headers
        )

    # ------------------------------------------------------------- 对外接口
    async def get_repo_meta(self, repo_full_name: str) -> RepoMeta:
        data, _ = await self._get_json(f"/repos/{repo_full_name}", resource="repo")
        if not isinstance(data, Mapping):
            raise GitHubError("仓库信息格式异常", resource="repo")
        return RepoMeta(
            full_name=str(data.get("full_name") or repo_full_name),
            description=data.get("description"),
            stars=_int(data.get("stargazers_count")),
            forks=_int(data.get("forks_count")),
            default_branch=str(data.get("default_branch") or "main"),
            private=bool(data.get("private")),
            html_url=str(data.get("html_url") or ""),
            pushed_at=parse_ts(data.get("pushed_at")),
        )

    async def fetch(self, query: FetchQuery) -> Page:
        repo = query.repo_full_name
        resource = query.resource
        per_page = query.per_page or self._per_page

        if resource == "commits":
            fetched = await self._paginate(
                path=f"/repos/{repo}/commits",
                params={"since": _iso(query.since)} if query.since else {},
                resource=resource,
                per_page=per_page,
                max_pages=self._max_pages,
                etag=query.etag,
                stop_before=query.since,
                mapper=lambda payload: parse_commit(
                    payload, excerpt_chars=BODY_EXCERPT_LIMIT
                ),
                time_of=_commit_time,
                key_of=lambda record: record.sha,
            )
            events = [
                commit_to_event(item, repo_full_name=repo, subscription_id=query.subscription_id)
                for item in fetched.items
            ]

        elif resource == "issues":
            params: dict[str, Any] = {
                "state": "all",
                "sort": "updated",
                "direction": "desc",
            }
            if query.since:
                params["since"] = _iso(query.since)
            fetched = await self._paginate(
                path=f"/repos/{repo}/issues",
                params=params,
                resource=resource,
                per_page=per_page,
                max_pages=self._max_pages,
                etag=query.etag,
                stop_before=query.since,
                mapper=lambda payload: parse_issue(
                    payload, excerpt_chars=BODY_EXCERPT_LIMIT
                ),
                time_of=_issue_time,
                key_of=lambda record: str(record.number),
            )
            events = [
                issue_to_event(item, repo_full_name=repo, subscription_id=query.subscription_id)
                for item in fetched.items
            ]

        elif resource == "pulls":
            fetched = await self._paginate(
                path=f"/repos/{repo}/pulls",
                params={"state": "all", "sort": "updated", "direction": "desc"},
                resource=resource,
                per_page=per_page,
                max_pages=self._max_pages,
                etag=query.etag,
                stop_before=query.since,
                mapper=lambda payload: parse_pull(
                    payload, excerpt_chars=BODY_EXCERPT_LIMIT
                ),
                time_of=_pull_time,
                key_of=lambda record: str(record.number),
            )
            events = [
                pull_to_event(item, repo_full_name=repo, subscription_id=query.subscription_id)
                for item in fetched.items
            ]

        elif resource == "releases":
            fetched = await self._paginate(
                path=f"/repos/{repo}/releases",
                params={},
                resource=resource,
                per_page=per_page,
                max_pages=self._max_pages,
                etag=query.etag,
                stop_before=query.since,
                mapper=lambda payload: parse_release(
                    payload, excerpt_chars=BODY_EXCERPT_LIMIT
                ),
                time_of=_release_time,
                key_of=lambda record: record.tag,
            )
            events = [
                release_to_event(item, repo_full_name=repo, subscription_id=query.subscription_id)
                for item in fetched.items
            ]

        else:
            raise GitHubError(f"不支持采集的资源：{resource}", resource=resource)

        if fetched.skipped:
            logger.warning(
                "github.skip_malformed resource=%s count=%s", resource, fetched.skipped
            )
        if fetched.truncated:
            logger.info(
                "github.truncated resource=%s repo=%s pages=%s",
                resource,
                repo,
                self._max_pages,
            )
        return Page(
            resource=resource,
            events=events,
            etag=fetched.etag,
            not_modified=fetched.not_modified,
            truncated=fetched.truncated,
            next_page=(self._max_pages + 1) if fetched.truncated else None,
        )

    async def fetch_progress(self, query: ProgressQuery) -> ProgressResult:
        repo = query.repo_full_name
        per_page = query.per_page or self._per_page
        result = ProgressResult(
            repo_full_name=repo,
            window_start=query.window_start,
            window_end=query.window_end,
        )

        if "issues" in query.resources:
            # 完成口径：先让服务端只返回 closed（PR 混入的在映射时丢弃），
            # 并且**不能**按 updated_at 裁右端——区间内关闭、之后又被评论的对象
            # 的 updated_at 会超出窗口，裁掉就漏报了。
            completed = query.issue_axis == "closed"
            fetched = await self._paginate(
                path=f"/repos/{repo}/issues",
                params={
                    "state": "closed" if completed else "all",
                    "sort": "updated",
                    "direction": "desc",
                    # issues 支持 since（按 updated_at 过滤，闭区间）
                    "since": _iso(query.window_start),
                },
                resource="issues",
                per_page=per_page,
                max_pages=query.max_pages,
                etag=None,
                # 停止键必须仍是 updated_at：它才是服务端的排序键
                stop_before=query.window_start,
                skip_at_or_after=None if completed else query.window_end,
                mapper=lambda payload: parse_issue(
                    payload, excerpt_chars=query.body_excerpt_chars
                ),
                time_of=_issue_time,
                key_of=lambda record: str(record.number),
            )
            result.issues = fetched.items
            result.truncated["issues"] = fetched.truncated
            result.skipped["issues"] = fetched.skipped

        if "pulls" in query.resources:
            completed = query.pull_axis == "merged"
            fetched = await self._paginate(
                path=f"/repos/{repo}/pulls",
                # pulls 没有 since 参数：只能按 updated 倒序取，再在客户端判断。
                # REST 也不支持 state=merged，合并只能看 merged_at，故用 closed 缩小范围。
                params={
                    "state": "closed" if completed else "all",
                    "sort": "updated",
                    "direction": "desc",
                },
                resource="pulls",
                per_page=per_page,
                max_pages=query.max_pages,
                etag=None,
                stop_before=query.window_start,
                skip_at_or_after=None if completed else query.window_end,
                mapper=lambda payload: parse_pull(
                    payload, excerpt_chars=query.body_excerpt_chars
                ),
                time_of=_pull_time,
                key_of=lambda record: str(record.number),
            )
            result.pulls = fetched.items
            result.truncated["pulls"] = fetched.truncated
            result.skipped["pulls"] = fetched.skipped

        if "commits" in query.resources:
            fetched = await self._paginate(
                path=f"/repos/{repo}/commits",
                params={
                    "since": _iso(query.window_start),
                    "until": _iso(query.window_end),
                },
                resource="commits",
                per_page=per_page,
                max_pages=query.max_pages,
                etag=None,
                stop_before=None,
                skip_at_or_after=query.window_end,
                mapper=lambda payload: parse_commit(
                    payload, excerpt_chars=query.body_excerpt_chars
                ),
                time_of=_commit_time,
                key_of=lambda record: record.sha,
            )
            result.commits = fetched.items
            result.truncated["commits"] = fetched.truncated
            result.skipped["commits"] = fetched.skipped

        if "releases" in query.resources:
            fetched = await self._paginate(
                path=f"/repos/{repo}/releases",
                # releases 没有时间参数，且服务端按 created_at 排序、我们取的是
                # published_at：草稿先建后发布会让这两个顺序不一致，
                # 因此左端只能逐条丢弃（skip_before），不能用 stop_before 早停。
                params={},
                resource="releases",
                per_page=per_page,
                max_pages=query.max_pages,
                etag=None,
                stop_before=None,
                skip_before=query.window_start,
                skip_at_or_after=query.window_end,
                mapper=lambda payload: parse_release(
                    payload, excerpt_chars=query.body_excerpt_chars
                ),
                time_of=_release_time,
                key_of=lambda record: record.tag,
            )
            result.releases = fetched.items
            result.truncated["releases"] = fetched.truncated
            result.skipped["releases"] = fetched.skipped

        truncated = [name for name, flag in result.truncated.items() if flag]
        if truncated:
            logger.warning(
                "github.progress_truncated repo=%s resources=%s", repo, ",".join(truncated)
            )
        return result

    async def rate_status(self) -> RateStatus:
        data, _ = await self._get_json("/rate_limit", resource="rate")
        resources = (data or {}).get("resources") if isinstance(data, Mapping) else None
        core = resources.get("core") if isinstance(resources, Mapping) else None
        if not isinstance(core, Mapping):
            return RateStatus()
        reset = core.get("reset")
        return RateStatus(
            remaining=_int(core.get("remaining")),
            limit=_int(core.get("limit")),
            reset_at=datetime.fromtimestamp(reset, UTC) if isinstance(reset, int) else None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -------------------------------------------------------------- 分页实现
    async def _paginate(
        self,
        *,
        path: str,
        params: Mapping[str, Any],
        resource: str,
        per_page: int,
        max_pages: int,
        etag: str | None,
        mapper: Callable[[Mapping[str, Any]], Any | None],
        time_of: Callable[[Any], datetime],
        key_of: Callable[[Any], str],
        stop_before: datetime | None = None,
        skip_at_or_after: datetime | None = None,
        skip_before: datetime | None = None,
    ) -> _Fetched:
        """按页取到 max_pages，遇窗口下界或没有下一页为止。

        `stop_before` 只在服务端确保证结果倒序时使用（四类资源都满足），
        因此「遇到更早的记录」可以安全地整体停止，而不是继续翻剩余页。

        `skip_before` 是「左端过滤但不停」：服务端排序键与被过滤字段不一致时
        （releases 按 created_at 排序、我们看 published_at），早停会漏，
        只能继续翻页并逐条丢弃。
        """
        fetched = _Fetched()
        seen: set[str] = set()
        page = 1
        limit = max(1, max_pages)

        while page <= limit:
            data, headers = await self._get_json(
                path,
                params={**params, "per_page": per_page, "page": page},
                etag=etag if (etag and page == 1) else None,
                resource=resource,
            )
            if data is None:  # 304
                fetched.etag = headers.get("ETag")
                fetched.not_modified = True
                return fetched
            if page == 1:
                fetched.etag = headers.get("ETag")
            if not isinstance(data, list) or not data:
                break

            stop = False
            for payload in data:
                if not isinstance(payload, Mapping):
                    fetched.skipped += 1
                    continue
                record = mapper(payload)
                if record is None:
                    fetched.skipped += 1
                    continue
                stamp = time_of(record)
                if stop_before is not None and stamp < stop_before:
                    stop = True
                    break
                if skip_before is not None and stamp < skip_before:
                    continue
                if skip_at_or_after is not None and stamp >= skip_at_or_after:
                    continue
                key = key_of(record)
                if key in seen:  # 分页边界重复的兜底
                    continue
                seen.add(key)
                fetched.items.append(record)

            if stop:
                break

            link = headers.get("Link")
            has_more = (
                has_next_link(link) if link is not None else len(data) >= per_page
            )
            if not has_more:
                break
            if page >= limit:
                fetched.truncated = True
                break
            page += 1

        return fetched

    # -------------------------------------------------------------- HTTP 层
    async def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        etag: str | None = None,
        resource: str,
    ) -> tuple[Any, httpx.Headers]:
        retrying = AsyncRetrying(
            sleep=self._sleep,
            stop=stop_after_attempt(self._retry_attempts),
            wait=self._wait_strategy,
            retry=self._should_retry,
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                response = await self._client.get(
                    path,
                    params=params,
                    headers={"If-None-Match": etag} if etag else None,
                )
                if response.status_code == 304:
                    return None, response.headers
                if response.status_code >= 400:
                    raise _error_for(response, resource)
                try:
                    return response.json(), response.headers
                except ValueError as exc:
                    raise GitHubError(
                        f"GitHub 返回非 JSON 响应（{response.status_code}）",
                        status=response.status_code,
                        resource=resource,
                    ) from exc
        raise GitHubError("GitHub 请求未完成", resource=resource)  # pragma: no cover

    def _should_retry(self, state: RetryCallState) -> bool:
        outcome = state.outcome
        exc = outcome.exception() if outcome is not None else None
        if isinstance(exc, RateLimitError):
            wait = exc.retry_after
            return wait is not None and wait <= self._max_retry_wait
        return isinstance(exc, _TransientGitHubError)

    def _wait_strategy(self, state: RetryCallState) -> float:
        outcome = state.outcome
        exc = outcome.exception() if outcome is not None else None
        if isinstance(exc, RateLimitError) and exc.retry_after:
            return float(exc.retry_after)
        return min(2.0 ** (state.attempt_number - 1), 8.0)


# -------------------------------------------------------------------- helpers
def _iso(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error_for(response: httpx.Response, resource: str) -> GitHubError:
    status = response.status_code
    body = " ".join(response.text[:300].split())
    remaining = response.headers.get("x-ratelimit-remaining")
    retry_after = _header_int(response, "retry-after")
    reset = _header_int(response, "x-ratelimit-reset")

    if status in (403, 429):
        if remaining == "0" or retry_after is not None:
            delay = retry_after
            if delay is None and reset is not None:
                delay = max(0, reset - int(datetime.now(UTC).timestamp()))
            return RateLimitError(
                f"GitHub 限流（{status}，剩余 {remaining or '未知'}）：{body}",
                status=status,
                reset_at=reset,
                retry_after=delay,
                resource=resource,
            )
        return GitHubError(f"GitHub 拒绝访问（403）：{body}", status=403, resource=resource)

    if status in _RETRYABLE_STATUS:
        return _TransientGitHubError(
            f"GitHub 上游错误（{status}）：{body}", status=status, resource=resource
        )

    hint = {
        401: "token 无效或已过期",
        404: "仓库不存在，或 token 没有该仓库权限",
    }.get(status, "")
    suffix = f"（{hint}）" if hint else ""
    return GitHubError(
        f"GitHub 请求失败（{status}）{suffix}：{body}", status=status, resource=resource
    )


def _header_int(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _issue_time(record: IssueRecord) -> datetime:
    return record.updated_at


def _pull_time(record: PullRecord) -> datetime:
    return record.updated_at


def _commit_time(record: CommitRecord) -> datetime:
    return record.committed_at


def _release_time(record: ReleaseRecord) -> datetime:
    return record.published_at
