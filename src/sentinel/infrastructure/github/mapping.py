"""GitHub REST 原始 JSON -> 领域记录 / RepoEvent。

全是纯函数，不碰网络：字段映射的坑（labels 既可能是字符串数组也可能是对象数组、
merged PR 的 state 仍是 closed、release 可能没有 published_at、issues 里混着 PR）
都能用固定 payload 单测，不需要 respx 也能覆盖。

映射原则：宁可丢一条脏数据（返回 None 让调用方计数跳过），也不要因为一条
缺字段的记录让整页失败——真实仓库里 draft release 与迁移过来的老 issue 都可能缺时间。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ...domain.enums import EventKind
from ...domain.models import RepoEvent, excerpt
from ...domain.progress import CommitRecord, IssueRecord, PullRecord, ReleaseRecord

DEFAULT_EXCERPT_CHARS = 160


def parse_ts(value: Any) -> datetime | None:
    """GitHub 的时间一律是 `2026-11-23T12:34:56Z`；解析失败返回 None 由调用方决定丢弃。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def has_next_link(link_header: str | None) -> bool:
    """`Link: <...&page=2>; rel="next", <...>; rel="last"` —— 只关心 next。"""
    if not link_header:
        return False
    return any('rel="next"' in part for part in link_header.split(","))


def labels_of(payload: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    raw = payload.get("labels")
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if isinstance(item, Mapping):
            name = item.get("name")
            if name:
                names.append(str(name))
        elif isinstance(item, str) and item:
            names.append(item)
    return tuple(names)


def parse_issue(
    payload: Mapping[str, Any], *, excerpt_chars: int = DEFAULT_EXCERPT_CHARS
) -> IssueRecord | None:
    """/issues 端点会把 PR 也返回，带 pull_request 字段的直接丢弃。"""
    if "pull_request" in payload:
        return None
    number = _as_int(payload.get("number"))
    created = parse_ts(payload.get("created_at"))
    updated = parse_ts(payload.get("updated_at"))
    if number is None or created is None or updated is None:
        return None
    return IssueRecord(
        number=number,
        title=_title(payload, number),
        url=str(payload.get("html_url") or ""),
        author=_login(payload),
        state=str(payload.get("state") or "open").lower(),
        labels=labels_of(payload),
        created_at=created,
        updated_at=updated,
        closed_at=parse_ts(payload.get("closed_at")),
        comments=_as_int(payload.get("comments")) or 0,
        body_excerpt=excerpt(_text(payload.get("body")), excerpt_chars) or "",
    )


def parse_pull(
    payload: Mapping[str, Any], *, excerpt_chars: int = DEFAULT_EXCERPT_CHARS
) -> PullRecord | None:
    number = _as_int(payload.get("number"))
    created = parse_ts(payload.get("created_at"))
    updated = parse_ts(payload.get("updated_at"))
    if number is None or created is None or updated is None:
        return None
    return PullRecord(
        number=number,
        title=_title(payload, number),
        url=str(payload.get("html_url") or ""),
        author=_login(payload),
        state=str(payload.get("state") or "open").lower(),
        draft=bool(payload.get("draft")),
        labels=labels_of(payload),
        created_at=created,
        updated_at=updated,
        closed_at=parse_ts(payload.get("closed_at")),
        merged_at=parse_ts(payload.get("merged_at")),
        comments=_as_int(payload.get("comments")) or 0,
        review_comments=_as_int(payload.get("review_comments")) or 0,
        additions=_as_int(payload.get("additions")),
        deletions=_as_int(payload.get("deletions")),
        changed_files=_as_int(payload.get("changed_files")),
        body_excerpt=excerpt(_text(payload.get("body")), excerpt_chars) or "",
    )


def parse_commit(
    payload: Mapping[str, Any], *, excerpt_chars: int = DEFAULT_EXCERPT_CHARS
) -> CommitRecord | None:
    sha = payload.get("sha")
    commit = payload.get("commit")
    if not isinstance(sha, str) or not sha or not isinstance(commit, Mapping):
        return None
    author_block = commit.get("author")
    committed = parse_ts(author_block.get("date")) if isinstance(author_block, Mapping) else None
    if committed is None:
        return None
    message = _text(commit.get("message")) or ""
    lines = message.splitlines()
    title = (lines[0].strip() if lines else "") or f"提交 {sha[:7]}"
    body = "\n".join(lines[1:]).strip()
    actor = _login(payload)
    if not actor and isinstance(author_block, Mapping):
        actor = str(author_block.get("name") or "")
    return CommitRecord(
        sha=sha,
        title=title[:300],
        url=str(payload.get("html_url") or ""),
        author=actor,
        committed_at=committed,
        body_excerpt=excerpt(body, excerpt_chars) or "",
    )


def parse_release(
    payload: Mapping[str, Any], *, excerpt_chars: int = DEFAULT_EXCERPT_CHARS
) -> ReleaseRecord | None:
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        return None
    published = parse_ts(payload.get("published_at")) or parse_ts(payload.get("created_at"))
    if published is None:
        return None
    return ReleaseRecord(
        tag=tag,
        name=str(payload.get("name") or ""),
        url=str(payload.get("html_url") or ""),
        author=_login(payload),
        published_at=published,
        prerelease=bool(payload.get("prerelease")),
        draft=bool(payload.get("draft")),
        body_excerpt=excerpt(_text(payload.get("body")), excerpt_chars) or "",
    )


# --------------------------------------------------------------- 事件视图
def issue_to_event(
    record: IssueRecord, *, repo_full_name: str, subscription_id: int | None = None
) -> RepoEvent:
    return RepoEvent(
        subscription_id=subscription_id,
        repo_full_name=repo_full_name,
        kind=EventKind.ISSUE,
        external_id=str(record.number),
        title=record.title,
        url=record.url,
        author=record.author,
        authored_at=record.created_at,
        source_updated_at=record.updated_at,
        state=record.state,
        labels=record.labels,
        body_excerpt=record.body_excerpt or None,
    )


def pull_to_event(
    record: PullRecord, *, repo_full_name: str, subscription_id: int | None = None
) -> RepoEvent:
    return RepoEvent(
        subscription_id=subscription_id,
        repo_full_name=repo_full_name,
        kind=EventKind.PULL_REQUEST,
        external_id=str(record.number),
        title=record.title,
        url=record.url,
        author=record.author,
        authored_at=record.created_at,
        source_updated_at=record.updated_at,
        # 已合并的 PR 在 GitHub 上 state 也是 closed，统一成 merged 才能和去重键对上
        state="merged" if record.is_merged else record.state,
        labels=record.labels,
        body_excerpt=record.body_excerpt or None,
    )


def commit_to_event(
    record: CommitRecord, *, repo_full_name: str, subscription_id: int | None = None
) -> RepoEvent:
    return RepoEvent(
        subscription_id=subscription_id,
        repo_full_name=repo_full_name,
        kind=EventKind.COMMIT,
        external_id=record.sha,
        title=record.title,
        url=record.url,
        author=record.author,
        authored_at=record.committed_at,
        body_excerpt=record.body_excerpt or None,
    )


def release_to_event(
    record: ReleaseRecord, *, repo_full_name: str, subscription_id: int | None = None
) -> RepoEvent:
    return RepoEvent(
        subscription_id=subscription_id,
        repo_full_name=repo_full_name,
        kind=EventKind.RELEASE,
        external_id=record.tag,
        title=record.name or f"Release {record.tag}",
        url=record.url,
        author=record.author,
        authored_at=record.published_at,
        state="published",
        labels=("release",),
        body_excerpt=record.body_excerpt or None,
    )


# ------------------------------------------------------------------ helpers
def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _login(payload: Mapping[str, Any]) -> str:
    """issue/PR 用 user.login，commit/release 用 author.login，两种都认。"""
    for key in ("user", "author"):
        block = payload.get(key)
        if isinstance(block, Mapping):
            login = block.get("login")
            if login:
                return str(login)
    return ""


def _title(payload: Mapping[str, Any], number: int) -> str:
    title = _text(payload.get("title"))
    return (title or "").strip() or f"#{number}"
