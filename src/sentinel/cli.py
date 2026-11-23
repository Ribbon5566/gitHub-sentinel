"""命令行入口。

命令设计围绕「调试一个订阅」的实际动线：
    seed-demo  → 造数据并跑通全链路
    subscribe  → 加订阅
    collect    → 只采集（验证增量与去重）
    digest     → 只出报告（验证摘要与模板）
    tick --once→ 只跑一轮调度（验证抢占与重排）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

from .config import Settings, load_settings
from .container import Container
from .domain.models import FetchCursor, Subscription
from .logging_setup import configure_logging

DEMO_REPO = "fastapi/fastapi"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentinel", description="GitHub Sentinel 命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 Web 服务")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")

    sub.add_parser("seed-demo", help="创建演示订阅并跑通采集→报告→通知")

    subscribe = sub.add_parser("subscribe", help="新建订阅")
    subscribe.add_argument("repo", help="owner/repo")
    subscribe.add_argument("--cron", default=None, help="5 段 cron，默认取自配置")
    subscribe.add_argument("--title", default=None)

    collect = sub.add_parser("collect", help="立即采集（默认全部启用订阅）")
    collect.add_argument("--id", type=int, default=None)

    digest = sub.add_parser("digest", help="生成报告")
    digest.add_argument("--id", type=int, default=None)
    digest.add_argument("--force", action="store_true", help="忽略内容指纹，强制重新摘要")

    tick = sub.add_parser("tick", help="跑一轮调度")
    tick.add_argument("--once", action="store_true", help="只跑一次（默认即一次）")

    sub.add_parser("list", help="列出订阅与最近报告")

    send = sub.add_parser("send-test", help="把指定报告再发一次")
    send.add_argument("--report-id", type=int, required=True)

    return parser


async def _open(settings: Settings) -> Container:
    container = Container(settings)
    await container.startup()
    return container


async def _seed_demo(settings: Settings) -> int:
    container = await _open(settings)
    try:
        existing = await container.subs.list(search=DEMO_REPO)
        if existing:
            sub = existing[0]
            print(f"复用已有订阅 #{sub.id} {sub.repo_full_name}")
        else:
            sub = await container.subscription_service.create(
                Subscription(
                    repo_full_name=DEMO_REPO,
                    title="FastAPI",
                    cron=settings.default_cron,
                    timezone=settings.timezone,
                )
            )
            print(f"已创建订阅 #{sub.id} {sub.repo_full_name}（cron={sub.cron}）")

        # 把水位拨到 26 小时前，让首次采集能覆盖一个完整日报窗口
        now = container.clock.now()
        for resource in ("commits", "issues", "pulls", "releases"):
            await container.cursors.upsert(
                FetchCursor(
                    subscription_id=sub.id or 0,
                    resource=resource,
                    watermark=now - timedelta(hours=26),
                    etag=None,
                    updated_at=now,
                )
            )

        outcome = await container.pipeline.run(sub.id or 0)
        print(
            f"采集: 新增 {outcome.collect.inserted} 条 / 去重跳过 {outcome.collect.skipped} 条 / "
            f"304 {outcome.collect.not_modified} 个"
        )
        if outcome.digest and outcome.digest.report:
            report = outcome.digest.report
            print(f"报告: #{report.id} 事件 {report.event_count} 条 → data/{report.html_path}")
            print(f"          纯文本 → data/{report.text_path}")
        else:
            reason = outcome.digest.reason if outcome.digest else "未知"
            print(f"报告: 未生成（{reason}）")
            print(
                "      提示：本次采集的事件都不在报告窗口内。"
                f"窗口 = 上一次 cron 触发（{settings.default_cron}）到现在，"
                "可以过一段时间再跑，或调大 SENTINEL_BOOTSTRAP_LOOKBACK_DAYS。"
            )
        if outcome.notify:
            print(f"通知: {outcome.notify.status} {outcome.notify.detail}")
        if outcome.error:
            print(f"警告: {outcome.error}")
        return 0
    finally:
        await container.shutdown()


async def _subscribe(settings: Settings, args: argparse.Namespace) -> int:
    container = await _open(settings)
    try:
        sub = await container.subscription_service.create(
            Subscription(
                repo_full_name=args.repo,
                title=args.title,
                cron=args.cron or settings.default_cron,
                timezone=settings.timezone,
            )
        )
        print(f"已创建 #{sub.id} {sub.repo_full_name} cron={sub.cron} 下次 {sub.next_run_at}")
        return 0
    finally:
        await container.shutdown()


async def _collect(settings: Settings, args: argparse.Namespace) -> int:
    container = await _open(settings)
    try:
        ids = [args.id] if args.id else [s.id for s in await container.subs.list_active()]
        for sub_id in [i for i in ids if i]:
            outcome = await container.collect_service.collect(sub_id)
            print(
                f"#{sub_id} {outcome.subscription.repo_full_name}: "
                f"新增 {outcome.inserted} / 跳过 {outcome.skipped} / 304 {outcome.not_modified} "
                f"{outcome.resources}"
            )
        return 0
    finally:
        await container.shutdown()


async def _digest(settings: Settings, args: argparse.Namespace) -> int:
    container = await _open(settings)
    try:
        ids = [args.id] if args.id else [s.id for s in await container.subs.list_active()]
        code = 0
        for sub_id in [i for i in ids if i]:
            outcome = await container.digest_service.build(sub_id, force=args.force)
            if outcome.report is None:
                print(f"#{sub_id}: 未生成（{outcome.reason}）")
                code = 1
                continue
            print(
                f"#{sub_id}: 报告 #{outcome.report.id} reused={outcome.reused} "
                f"事件 {outcome.report.event_count} → data/{outcome.report.html_path}"
            )
        return code
    finally:
        await container.shutdown()


async def _tick(settings: Settings, args: argparse.Namespace) -> int:
    container = await _open(settings)
    try:
        outcomes = await container.tick_once()
        if not outcomes:
            print("没有到期订阅")
            return 0
        for item in outcomes:
            print(
                f"#{item.subscription_id}: 新增 {item.collect.inserted} 条, "
                f"报告 {item.digest.report.id if item.digest and item.digest.report else '-'}"
            )
        return 0
    finally:
        await container.shutdown()


async def _list(settings: Settings, args: argparse.Namespace) -> int:
    container = await _open(settings)
    try:
        subs = await container.subs.list(limit=100)
        if not subs:
            print("还没有订阅")
            return 0
        for sub in subs:
            latest = await container.reports.latest_for(sub.id or 0)
            next_run = (
                sub.next_run_at.astimezone(settings.tz).strftime("%Y-%m-%d %H:%M")
                if sub.next_run_at
                else "-"
            )
            print(
                f"#{sub.id:<3} {sub.repo_full_name:<40} cron={sub.cron:<14} "
                f"启用={'是' if sub.enabled else '否'} "
                f"下次={next_run} "
                f"最近报告={latest.id if latest else '-'}"
            )
        return 0
    finally:
        await container.shutdown()


async def _send_test(settings: Settings, args: argparse.Namespace) -> int:
    container = await _open(settings)
    try:
        outcome = await container.notify_service.notify(args.report_id, resend=True)
        print(f"通知: {outcome.status} {outcome.detail}")
        return 0
    finally:
        await container.shutdown()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(
        settings.log_level,
        secrets=(settings.api_key, settings.github_token, settings.llm_api_key, settings.smtp_password),
    )

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "sentinel.main:app",
            host=args.host or settings.host,
            port=args.port or settings.port,
            loop="asyncio",
            reload=args.reload,
        )
        return 0

    # CLI 都是短任务，不要连带把调度器也启动起来
    cli_settings = load_settings(enable_scheduler=False)
    handlers = {
        "seed-demo": lambda: _seed_demo(cli_settings),
        "subscribe": lambda: _subscribe(cli_settings, args),
        "collect": lambda: _collect(cli_settings, args),
        "digest": lambda: _digest(cli_settings, args),
        "tick": lambda: _tick(cli_settings, args),
        "list": lambda: _list(cli_settings, args),
        "send-test": lambda: _send_test(cli_settings, args),
    }
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover
        print(f"未知命令: {args.command}", file=sys.stderr)
        return 2
    return asyncio.run(handler())


if __name__ == "__main__":
    raise SystemExit(main())
