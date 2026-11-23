"""去重键。

这是一个「看起来简单、做错就永久丢数据」的地方，规则定死如下：

    恒定键 query_key  = (repo, kind, external_id)   —— 用来定位「同一条东西」
    版本键   version  = 该对象当前状态的指纹          —— 用来判断「有没有新变化」
    唯一键   dedup_key = (恒定键, 版本键) 的摘要       —— 数据库 UNIQUE 索引建在它上面

为什么需要两把键：
  * 只用恒定键：issue 关闭、PR 合并这类「同一对象的状态变化」永远播不出去；
  * 只用版本键：改个错别字 updated_at 就变，报告里会重复刷同一条 issue。

所以要存成两列（external_id / version），唯一索引落在二者组合的摘要上，
报告侧再按 query_key 取最新一条。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from .enums import EventKind


def iso_second(moment: datetime | None) -> str:
    """秒级 ISO 字符串。GitHub 的时间精度就是秒，不截断会导致同一条事件被判成两次。"""
    if moment is None:
        return ""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_version(
    kind: EventKind,
    *,
    source_updated_at: datetime | None = None,
    authored_at: datetime | None = None,
) -> str:
    """不同事件类型的「版本」语义不同，这里是唯一判定处。"""
    if kind is EventKind.COMMIT:
        # commit 的 sha 本身就是不可变身份，版本恒为空
        return "sha"
    if kind is EventKind.RELEASE:
        # release 的内容不会变，发版时间即版本；退化为 tag 时间
        return iso_second(authored_at) or "release"
    # issue / PR / discussion：状态与正文都会变，取源侧 updated_at
    return iso_second(source_updated_at) or iso_second(authored_at)


def query_key(repo_full_name: str, kind: EventKind, external_id: str) -> str:
    return f"{repo_full_name}|{kind.value}|{external_id}"


def dedup_key(repo_full_name: str, kind: EventKind, external_id: str, version: str) -> str:
    raw = f"{repo_full_name}|{kind.value}|{external_id}|{version}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    # 保留可读前缀，便于排查；唯一性由 digest 保证
    return f"{kind.value}:{digest}"
