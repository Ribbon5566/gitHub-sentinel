from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from sentinel.domain.errors import GitHubError, RateLimitError, ValidationError
from sentinel.infrastructure.github.rest_client import RestGitHub
from sentinel.ports.github import FetchQuery, ProgressQuery

BASE = "https://api.github.com"
REPO = "acme/widgets"
WINDOW_START = datetime(2026, 11, 22, 16, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 11, 23, 16, 0, tzinfo=UTC)


def issue_payload(number: int, *, updated: str = "2026-11-23T09:00:00Z", **overrides):
    payload = {
        "number": number,
        "title": f"议题 {number}",
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "user": {"login": "alice"},
        "state": "open",
        "labels": [{"name": "bug"}],
        "created_at": "2026-11-20T01:00:00Z",
        "updated_at": updated,
        "closed_at": None,
        "comments": 1,
        "body": "现象：偶发中断。",
    }
    payload.update(overrides)
    return payload


def pull_payload(number: int, *, updated: str, **overrides):
    payload = {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "user": {"login": "bob"},
        "state": "open",
        "draft": False,
        "labels": [],
        "created_at": "2026-11-21T01:00:00Z",
        "updated_at": updated,
        "closed_at": None,
        "merged_at": None,
        "comments": 0,
        "review_comments": 0,
        "additions": 10,
        "deletions": 2,
        "changed_files": 1,
        "body": "说明。",
    }
    payload.update(overrides)
    return payload


def commit_payload(sha: str, *, date: str = "2026-11-23T08:00:00Z"):
    return {
        "sha": sha,
        "html_url": f"https://github.com/{REPO}/commit/{sha}",
        "commit": {"message": f"fix: {sha[:4]}", "author": {"name": "Carol", "date": date}},
        "author": {"login": "carol"},
    }


def release_payload(tag: str, *, published: str = "2026-11-23T07:00:00Z"):
    return {
        "tag_name": tag,
        "name": f"Release {tag}",
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "author": {"login": "dave"},
        "published_at": published,
        "body": "发版说明。",
    }


class Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))


def build(sleeper: Sleeper | None = None, **kwargs) -> RestGitHub:
    params = {
        "token": "test-token",
        "per_page": 2,
        "max_pages": 2,
        "retry_attempts": 2,
        "max_retry_wait": 10.0,
    }
    params.update(kwargs)
    if sleeper is not None:
        params["sleep"] = sleeper
    return RestGitHub(**params)


async def test_issues_pagination_drops_pulls_and_sends_since() -> None:
    """issues 分页必须带 since，且把混进来的 PR 丢掉。"""
    with respx.mock(base_url=BASE) as router:
        route = router.get(f"/repos/{REPO}/issues").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=[issue_payload(1), pull_marker(2)],
                    headers={
                        "ETag": '"e1"',
                        "Link": f'<{BASE}/repos/{REPO}/issues?page=2>; rel="next"',
                    },
                ),
                httpx.Response(200, json=[issue_payload(3)]),
            ]
        )
        github = build()
        try:
            page = await github.fetch(
                FetchQuery(repo_full_name=REPO, resource="issues", since=WINDOW_START)
            )
        finally:
            await github.aclose()

    assert [event.external_id for event in page.events] == ["1", "3"]
    assert page.etag == '"e1"'
    assert page.truncated is False
    first = route.calls[0].request
    assert first.url.params["state"] == "all"
    assert first.url.params["sort"] == "updated"
    assert first.url.params["since"] == "2026-11-22T16:00:00Z"
    assert first.headers["Authorization"] == "Bearer test-token"
    assert route.calls[1].request.url.params["page"] == "2"


async def test_pulls_has_no_since_and_is_cut_off_client_side() -> None:
    """GitHub 的 /pulls 不支持 since，只能倒序取后客户端截断。"""
    with respx.mock(base_url=BASE) as router:
        route = router.get(f"/repos/{REPO}/pulls").mock(
            return_value=httpx.Response(
                200,
                json=[
                    pull_payload(10, updated="2026-11-23T10:00:00Z"),
                    pull_payload(11, updated="2026-11-20T10:00:00Z"),
                ],
            )
        )
        github = build()
        try:
            result = await github.fetch_progress(
                ProgressQuery(
                    repo_full_name=REPO,
                    window_start=WINDOW_START,
                    window_end=WINDOW_END,
                    resources=("pulls",),
                )
            )
        finally:
            await github.aclose()

    assert [item.number for item in result.pulls] == [10]
    params = route.calls[0].request.url.params
    assert "since" not in params
    assert params["sort"] == "updated"
    assert params["direction"] == "desc"


async def test_commits_use_since_and_until() -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.get(f"/repos/{REPO}/commits").mock(
            return_value=httpx.Response(
                200,
                json=[
                    commit_payload("a" * 40),
                    commit_payload("b" * 40, date="2026-11-23T20:00:00Z"),
                ],
            )
        )
        github = build()
        try:
            result = await github.fetch_progress(
                ProgressQuery(
                    repo_full_name=REPO,
                    window_start=WINDOW_START,
                    window_end=WINDOW_END,
                    resources=("commits",),
                )
            )
        finally:
            await github.aclose()

    assert [item.sha for item in result.commits] == ["a" * 40]
    params = route.calls[0].request.url.params
    assert params["since"] == "2026-11-22T16:00:00Z"
    assert params["until"] == "2026-11-23T16:00:00Z"


async def test_releases_are_filtered_by_published_at() -> None:
    """releases 只能按 published_at 在客户端过滤——服务端按 created_at 排序，
    所以不能用 stop_before 早停（草稿先建后发布会让两个顺序不一致）。"""
    with respx.mock(base_url=BASE) as router:
        route = router.get(f"/repos/{REPO}/releases").mock(
            return_value=httpx.Response(
                200,
                json=[
                    release_payload("v2.0.0"),
                    release_payload("v1.9.0", published="2026-11-01T00:00:00Z"),
                ],
            )
        )
        github = build()
        try:
            result = await github.fetch_progress(
                ProgressQuery(
                    repo_full_name=REPO,
                    window_start=WINDOW_START,
                    window_end=WINDOW_END,
                    resources=("releases",),
                )
            )
        finally:
            await github.aclose()

    assert [item.tag for item in result.releases] == ["v2.0.0"]
    assert route.call_count == 1


async def test_etag_304_short_circuits_without_parsing() -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.get(f"/repos/{REPO}/issues").mock(
            side_effect=[
                httpx.Response(200, json=[issue_payload(1)], headers={"ETag": '"e1"'}),
                httpx.Response(304, headers={"ETag": '"e1"'}),
            ]
        )
        github = build()
        try:
            first = await github.fetch(FetchQuery(repo_full_name=REPO, resource="issues"))
            second = await github.fetch(
                FetchQuery(repo_full_name=REPO, resource="issues", etag=first.etag)
            )
        finally:
            await github.aclose()

    assert second.not_modified is True
    assert second.events == []
    assert route.calls[1].request.headers["If-None-Match"] == '"e1"'


async def test_retry_after_is_honoured() -> None:
    sleeper = Sleeper()
    with respx.mock(base_url=BASE) as router:
        route = router.get(f"/repos/{REPO}/issues").mock(
            side_effect=[
                httpx.Response(
                    403,
                    headers={"Retry-After": "2", "x-ratelimit-remaining": "0"},
                    text="secondary rate limit",
                ),
                httpx.Response(200, json=[issue_payload(1)]),
            ]
        )
        github = build(sleeper, retry_attempts=3)
        try:
            page = await github.fetch(FetchQuery(repo_full_name=REPO, resource="issues"))
        finally:
            await github.aclose()

    assert sleeper.calls == [2.0]
    assert route.call_count == 2
    assert [event.external_id for event in page.events] == ["1"]


async def test_primary_rate_limit_does_not_block_the_process() -> None:
    """主限流可能要等半小时，超过上限必须立刻失败，不能干等。"""
    sleeper = Sleeper()
    reset = int(datetime.now(UTC).timestamp()) + 3600
    with respx.mock(base_url=BASE) as router:
        router.get(f"/repos/{REPO}/issues").mock(
            return_value=httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
                text="API rate limit exceeded",
            )
        )
        github = build(sleeper, retry_attempts=3, max_retry_wait=10.0)
        try:
            with pytest.raises(RateLimitError) as excinfo:
                await github.fetch(FetchQuery(repo_full_name=REPO, resource="issues"))
        finally:
            await github.aclose()

    assert sleeper.calls == []
    assert excinfo.value.retry_after is not None


async def test_server_error_is_retried_with_backoff() -> None:
    sleeper = Sleeper()
    with respx.mock(base_url=BASE) as router:
        route = router.get(f"/repos/{REPO}/issues").mock(
            side_effect=[httpx.Response(502), httpx.Response(200, json=[issue_payload(1)])]
        )
        github = build(sleeper, retry_attempts=3)
        try:
            page = await github.fetch(FetchQuery(repo_full_name=REPO, resource="issues"))
        finally:
            await github.aclose()

    assert sleeper.calls == [1.0]
    assert route.call_count == 2
    assert len(page.events) == 1


@pytest.mark.parametrize("status", [401, 404])
async def test_auth_errors_are_not_retried(status: int) -> None:
    sleeper = Sleeper()
    with respx.mock(base_url=BASE) as router:
        route = router.get(f"/repos/{REPO}/issues").mock(
            return_value=httpx.Response(status, text="nope")
        )
        github = build(sleeper, retry_attempts=3)
        try:
            with pytest.raises(GitHubError) as excinfo:
                await github.fetch(FetchQuery(repo_full_name=REPO, resource="issues"))
        finally:
            await github.aclose()

    assert excinfo.value.status == status
    assert sleeper.calls == []
    assert route.call_count == 1


async def test_max_pages_marks_truncated() -> None:
    link = f'<{BASE}/repos/{REPO}/issues?page=3>; rel="next"'
    with respx.mock(base_url=BASE) as router:
        route = router.get(f"/repos/{REPO}/issues").mock(
            return_value=httpx.Response(
                200, json=[issue_payload(1), issue_payload(2)], headers={"Link": link}
            )
        )
        github = build(max_pages=2)
        try:
            page = await github.fetch(FetchQuery(repo_full_name=REPO, resource="issues"))
        finally:
            await github.aclose()

    assert page.truncated is True
    assert page.next_page == 3
    assert route.call_count == 2


async def test_fetch_progress_aggregates_all_resources() -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(f"/repos/{REPO}/issues").mock(
            return_value=httpx.Response(200, json=[issue_payload(1), pull_marker(2)])
        )
        router.get(f"/repos/{REPO}/pulls").mock(
            return_value=httpx.Response(
                200,
                json=[
                    pull_payload(10, updated="2026-11-23T10:00:00Z"),
                    pull_payload(11, updated="2026-11-19T10:00:00Z"),
                ],
            )
        )
        router.get(f"/repos/{REPO}/commits").mock(
            return_value=httpx.Response(200, json=[commit_payload("c" * 40)])
        )
        router.get(f"/repos/{REPO}/releases").mock(
            return_value=httpx.Response(200, json=[release_payload("v3.0.0")])
        )
        github = build()
        try:
            result = await github.fetch_progress(
                ProgressQuery(
                    repo_full_name=REPO,
                    window_start=WINDOW_START,
                    window_end=WINDOW_END,
                    resources=("issues", "pulls", "commits", "releases"),
                )
            )
        finally:
            await github.aclose()

    assert [item.number for item in result.issues] == [1]
    assert [item.number for item in result.pulls] == [10]
    assert [item.sha for item in result.commits] == ["c" * 40]
    assert [item.tag for item in result.releases] == ["v3.0.0"]
    assert result.truncated == {
        "issues": False,
        "pulls": False,
        "commits": False,
        "releases": False,
    }


async def test_rate_status_parses_core_bucket() -> None:
    with respx.mock(base_url=BASE) as router:
        router.get("/rate_limit").mock(
            return_value=httpx.Response(
                200,
                json={"resources": {"core": {"limit": 5000, "remaining": 42, "reset": 1700000000}}},
            )
        )
        github = build()
        try:
            status = await github.rate_status()
        finally:
            await github.aclose()

    assert (status.limit, status.remaining) == (5000, 42)
    assert status.reset_at is not None


def pull_marker(number: int) -> dict:
    """GitHub 的 /issues 里 PR 的样子：只多一个 pull_request 字段。"""
    payload = issue_payload(number)
    payload["pull_request"] = {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}
    return payload


# ------------------------------------------------------- 完成口径（研发周期）
async def test_closed_axis_requests_closed_and_keeps_late_updates() -> None:
    """完成口径：先让服务端只给 closed，且**不能**按 updated_at 裁右端。"""
    late = issue_payload(
        2,
        updated="2026-11-25T09:00:00Z",  # 窗口之后又被评论
        state="closed",
        closed_at="2026-11-23T10:00:00Z",  # 但关闭时间落在窗口内
    )
    with respx.mock(base_url=BASE) as router:
        route = router.get(f"/repos/{REPO}/issues").mock(
            return_value=httpx.Response(200, json=[late])
        )
        github = build()
        try:
            result = await github.fetch_progress(
                ProgressQuery(
                    repo_full_name=REPO,
                    window_start=WINDOW_START,
                    window_end=WINDOW_END,
                    resources=("issues",),
                    issue_axis="closed",
                )
            )
        finally:
            await github.aclose()

    assert [item.number for item in result.issues] == [2]
    params = route.calls[0].request.url.params
    assert params["state"] == "closed"
    assert params["since"] == "2026-11-22T16:00:00Z"


async def test_merged_axis_does_not_drop_prs_merged_inside_window() -> None:
    """反例回归：把翻页停止键换成 merged_at，第二页里真正属于本期的 PR 就漏了。

    线上形态是：A 早合并、但最近被评论（updated 很新，排在最前）；
    B 恰在本期合并（排在其后）。停止条件必须作用在服务端的 updated_at 上。
    """
    link = f'<{BASE}/repos/{REPO}/pulls?page=2>; rel="next"'
    with respx.mock(base_url=BASE) as router:
        route = router.get(f"/repos/{REPO}/pulls").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=[
                        pull_payload(
                            10,
                            updated="2026-11-23T20:00:00Z",
                            state="closed",
                            closed_at="2026-11-10T00:00:00Z",
                            merged_at="2026-11-10T00:00:00Z",
                        )
                    ],
                    headers={"Link": link},
                ),
                httpx.Response(
                    200,
                    json=[
                        pull_payload(
                            11,
                            updated="2026-11-23T12:00:00Z",
                            state="closed",
                            closed_at="2026-11-23T12:00:00Z",
                            merged_at="2026-11-23T12:00:00Z",
                        )
                    ],
                ),
            ]
        )
        github = build(max_pages=2)
        try:
            result = await github.fetch_progress(
                ProgressQuery(
                    repo_full_name=REPO,
                    window_start=WINDOW_START,
                    window_end=WINDOW_END,
                    resources=("pulls",),
                    pull_axis="merged",
                    per_page=2,
                    max_pages=2,
                )
            )
        finally:
            await github.aclose()

    assert [item.number for item in result.pulls] == [10, 11]
    assert route.calls[0].request.url.params["state"] == "closed"
    assert "since" not in route.calls[0].request.url.params
    assert route.call_count == 2  # 必须真的翻到第二页


async def test_progress_marks_truncated_when_page_budget_exhausted() -> None:
    link = f'<{BASE}/repos/{REPO}/pulls?page=3>; rel="next"'
    with respx.mock(base_url=BASE) as router:
        router.get(f"/repos/{REPO}/pulls").mock(
            return_value=httpx.Response(
                200,
                json=[pull_payload(10, updated="2026-11-23T10:00:00Z")],
                headers={"Link": link},
            )
        )
        github = build(max_pages=2)
        try:
            result = await github.fetch_progress(
                ProgressQuery(
                    repo_full_name=REPO,
                    window_start=WINDOW_START,
                    window_end=WINDOW_END,
                    resources=("pulls",),
                    per_page=2,
                    max_pages=2,
                )
            )
        finally:
            await github.aclose()

    assert result.truncated["pulls"] is True


def test_progress_query_rejects_absurd_page_budget() -> None:
    with pytest.raises(ValidationError):
        ProgressQuery(
            repo_full_name=REPO,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            max_pages=500,
        )
