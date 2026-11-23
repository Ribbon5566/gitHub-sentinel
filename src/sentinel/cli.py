"""命令行入口。

命令设计围绕「调试一个订阅」的实际动线：
    seed-demo  → 造数据并跑通全链路
    subscribe  → 加订阅
    collect    → 只采集（验证增量与去重）
    digest     → 只出报告（验证摘要与模板）
    tick --once→ 只跑一轮调度（验证抢占与重排）

v2.0 追加的每日动线（与上面的 cron 简报是两条独立链路）：
    progress   → 拉当日 issues/PR，落一份「项目+日期」的 md
    report     → 只读那份 md，调模型出正式日报（不访问 GitHub）
    daily      → 上面两步连着跑

v3.0 在这条动线上加了「条件筛选」：用 --from/--to 给出研发周期，
默认只收区间内**已合并的 PR** 与**已关闭的议题**（完成口径），
产物按区间命名，不覆盖单日的 YYYY-MM-DD.md。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings, load_settings
from .container import Container
from .domain.errors import NotFoundError, SentinelError, ValidationError
from .domain.models import FetchCursor, Subscription, normalize_repo_full_name
from .domain.period import ReportPeriod
from .domain.selection import ReportSelection
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

    for name, help_text in (
        ("progress", "生成每日进展 Markdown（访问 GitHub，不调模型）"),
        ("report", "从已有进展生成每日报告（调模型，不访问 GitHub）"),
        ("daily", "每日进展 + 每日报告，一步跑完"),
    ):
        daily = sub.add_parser(name, help=help_text)
        daily.add_argument(
            "--repo", action="append", default=[], metavar="OWNER/REPO",
            help="直接指定仓库，可重复；不要求已订阅",
        )
        daily.add_argument(
            "--id", type=int, action="append", default=[], metavar="N",
            help="按订阅 ID 指定，可重复",
        )
        daily.add_argument("--date", default=None, help="报告日期 YYYY-MM-DD，默认今天")
        daily.add_argument(
            "--from", dest="date_from", default=None, metavar="YYYY-MM-DD",
            help="研发周期起点；给了区间就按「完成口径」筛选（默认只收已合并 PR 与已关闭议题）",
        )
        daily.add_argument(
            "--to", dest="date_to", default=None, metavar="YYYY-MM-DD",
            help="研发周期终点（含当天），默认今天；单独使用无效，必须配 --from",
        )
        daily.add_argument("--merged", action="store_true", help="只收已合并的 PR")
        daily.add_argument("--closed", action="store_true", help="只收已关闭的议题")
        daily.add_argument(
            "--activity", action="store_true",
            help="按 updated_at 口径（v2.0 语义）而不是完成时间",
        )
        daily.add_argument("--force", action="store_true", help="忽略指纹，强制重新生成")

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


# --------------------------------------------------------------- 每日动线
def _parse_day(raw: str | None, settings: Settings) -> date:
    if not raw:
        return datetime.now(settings.daily_tz).date()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(f"--date 需要 YYYY-MM-DD，收到 {raw!r}") from exc


def _parse_date(raw: str, *, flag: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(f"{flag} 需要 YYYY-MM-DD，收到 {raw!r}") from exc


def _resolve_period(
    args: argparse.Namespace, settings: Settings
) -> tuple[ReportPeriod | None, ReportSelection | None]:
    """把参数解析成 (研发周期, 筛选口径)。

    返回 (None, None) 表示走 v2.0 的单日活动口径——**模式由 flag 决定，
    不由日期形状决定**：否则 `--date 2026-11-23 --merged` 会悄悄覆盖
    单日产物 2026-11-23.md。
    """
    date_from = getattr(args, "date_from", None)
    date_to = getattr(args, "date_to", None)
    merged = bool(getattr(args, "merged", False))
    closed = bool(getattr(args, "closed", False))
    activity = bool(getattr(args, "activity", False))
    day_flag = getattr(args, "date", None)

    if not any((date_from, date_to, merged, closed, activity)):
        return None, None

    if day_flag:
        raise ValidationError("--date 与 --from/--to 不能同时使用")
    if date_to and not date_from:
        raise ValidationError("--to 需要配合 --from 使用")
    if activity and (merged or closed):
        raise ValidationError("--activity 与 --merged/--closed 不能同时使用")
    if (merged or closed or activity) and not date_from:
        raise ValidationError("--merged/--closed/--activity 需要配合 --from 使用")

    start = _parse_date(date_from, flag="--from")
    end = _parse_date(date_to, flag="--to") if date_to else datetime.now(settings.daily_tz).date()
    period = ReportPeriod(date_from=start, date_to=end)

    if activity:
        selection = ReportSelection.activity()
    elif merged and closed:
        selection = ReportSelection.completion()
    elif merged:
        selection = ReportSelection.merged_only()
    elif closed:
        selection = ReportSelection.closed_only()
    else:
        # 只给区间不给口径：默认交付清单（已合并 PR + 已关闭议题）
        selection = ReportSelection.completion()
    return period, selection


async def _resolve_targets(
    container: Container, args: argparse.Namespace
) -> list[tuple[str, str | None]]:
    """返回 [(repo, 展示名)]；--repo 不要求已订阅，--id 才查库。"""
    if args.repo and args.id:
        raise ValidationError("--repo 与 --id 不能同时使用")

    if args.repo:
        targets: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for raw in args.repo:
            repo = normalize_repo_full_name(raw)
            if repo not in seen:
                seen.add(repo)
                targets.append((repo, None))
        return targets

    if args.id:
        targets = []
        seen = set()
        for sub_id in args.id:
            sub = await container.subs.get(sub_id)
            if sub is None:
                raise NotFoundError(f"订阅 {sub_id} 不存在")
            if sub.repo_full_name not in seen:
                seen.add(sub.repo_full_name)
                targets.append((sub.repo_full_name, sub.display_name))
        return targets

    return [(sub.repo_full_name, sub.display_name) for sub in await container.subs.list_active()]


def _stats_line(stats: dict[str, int], *, completion: bool = False) -> str:
    """完成口径下 issues_total ≡ issues_closed，不必再重复两个数。"""
    if completion:
        return (
            f"关闭议题 {stats.get('issues_closed', 0)} / "
            f"合并 PR {stats.get('pulls_merged', 0)} / "
            f"提交 {stats.get('commits_total', 0)} / 发布 {stats.get('releases_total', 0)} / "
            f"贡献者 {stats.get('contributors', 0)}"
        )
    return (
        f"议题 {stats.get('issues_total', 0)}（关闭 {stats.get('issues_closed', 0)}）/ "
        f"PR {stats.get('pulls_total', 0)}"
        f"（合并 {stats.get('pulls_merged', 0)}，草稿 {stats.get('pulls_draft', 0)}）/ "
        f"提交 {stats.get('commits_total', 0)} / 发布 {stats.get('releases_total', 0)} / "
        f"贡献者 {stats.get('contributors', 0)}"
    )


def _display(path: str) -> str:
    """默认布局返回相对 data/ 的路径；用户自定义到 data 之外时就是绝对路径。"""
    return path if Path(path).is_absolute() else f"data/{path}"


async def _daily(
    settings: Settings, args: argparse.Namespace, *, do_progress: bool, do_report: bool
) -> int:
    container = await _open(settings)
    try:
        try:
            period, selection = _resolve_period(args, settings)
            if period is not None:
                # 全局参数校验前置到这里：放到服务里虽然也会在任何 GitHub 请求之前失败，
                # 但会变成「每个仓库各报一次同样的错」，批量跑时刷屏且看不出是参数问题
                period.validate(
                    today=datetime.now(settings.daily_tz).date(),
                    max_days=settings.report_range_max_days,
                )
            day = _parse_day(args.date, settings) if period is None else period.date_to
            targets = await _resolve_targets(container, args)
        except SentinelError as exc:
            print(f"参数错误: {exc}", file=sys.stderr)
            return 2

        if not targets:
            print("没有可处理的目标：先订阅仓库，或用 --repo 指定", file=sys.stderr)
            return 1

        # 区间标签放在头部打印一次，避免每个仓库重复一遍口径说明
        label = period.label() if period is not None else str(day)
        if period is not None and selection is not None:
            print(f"研发周期 {label} · {selection.scope_label()}")

        code = 0
        for index, (repo, title) in enumerate(targets, start=1):
            print(f"[{index}/{len(targets)}] {repo} {label}")
            try:
                if do_progress:
                    # 两种调用形状：单日只给 day，区间给 period+selection；
                    # 服务的签名要求二者必居其一，这里不要用「day=None」混过去。
                    if period is None:
                        progress = await container.daily_progress_service.build(
                            repo_full_name=repo, day=day, repo_title=title, force=args.force
                        )
                    else:
                        progress = await container.daily_progress_service.build(
                            repo_full_name=repo,
                            period=period,
                            selection=selection,
                            repo_title=title,
                            force=args.force,
                        )
                    suffix = "（内容未变，复用）" if progress.reused else ""
                    print(f"  进展: {_display(progress.path)}{suffix}")
                    print(f"  统计: {_stats_line(progress.stats, completion=period is not None)}")
                    # 展示裁剪：数据是全的，只是列表只印前 N 条
                    if progress.trimmed:
                        print(
                            f"  提示: 以下资源仅展示前 {settings.daily_max_items_per_resource} 条"
                            f"（统计仍按全量）：{'、'.join(progress.trimmed)}"
                        )
                    # 翻页触顶：统计本身是下界，属于要人介入的情况，走 stderr 但不影响退出码
                    if progress.truncated:
                        print(
                            "  警告: 以下资源触及翻页上限，统计为下界、可能漏报"
                            f"（可调大 SENTINEL_REPORT_RANGE_MAX_PAGES）："
                            f"{'、'.join(progress.truncated)}",
                            file=sys.stderr,
                        )

                if do_report:
                    if period is None:
                        report = await container.daily_report_service.build(
                            repo_full_name=repo, day=day, force=args.force
                        )
                    else:
                        # selection 也要传：否则 --merged 会去读默认口径那份文件，
                        # 静默把另一套数据集写成报告
                        report = await container.daily_report_service.build(
                            repo_full_name=repo, period=period, selection=selection,
                            force=args.force,
                        )
                    if report.reused:
                        print(f"  报告: {_display(report.path)}（内容未变，跳过模型调用）")
                    else:
                        note = "，规则兜底" if report.degraded else ""
                        print(f"  报告: {_display(report.path)}  模型 {report.model}{note}")
                        if report.degraded:
                            print(
                                "  警告: 模型未返回合格结果，已使用规则模板；"
                                "检查 SENTINEL_LLM_* 后可加 --force 重跑"
                            )
            except SentinelError as exc:
                # 一个仓库失败不中断其余仓库
                print(f"  失败: {exc}", file=sys.stderr)
                code = 1
        return code
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
        "progress": lambda: _daily(cli_settings, args, do_progress=True, do_report=False),
        "report": lambda: _daily(cli_settings, args, do_progress=False, do_report=True),
        "daily": lambda: _daily(cli_settings, args, do_progress=True, do_report=True),
    }
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover
        print(f"未知命令: {args.command}", file=sys.stderr)
        return 2
    return asyncio.run(handler())


if __name__ == "__main__":
    raise SystemExit(main())
