from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from sentinel.container import Container


async def _event_count(container: Container) -> int:
    assert container.engine is not None
    async with container.engine.connect() as conn:
        result = await conn.execute(text("select count(*) from events"))
        return int(result.scalar_one())


async def test_daily_flow_writes_both_files_without_touching_events(container: Container) -> None:
    """fake GitHub + mock LLM 也要能跑完整条链路，且不得往 events 表写数据。"""
    settings = container.settings
    today = datetime.now(settings.daily_tz).date()
    repo = "fastapi/fastapi"
    before = await _event_count(container)

    progress = await container.daily_progress_service.build(repo_full_name=repo, day=today)
    report = await container.daily_report_service.build(repo_full_name=repo, day=today)

    progress_file = settings.data_dir / progress.path
    report_file = settings.data_dir / report.path
    assert progress_file.is_file()
    assert report_file.is_file()
    # mock provider 不支持自由文本，报告必然走规则兜底，但内容仍然完整
    assert report.degraded is True
    body = report_file.read_text(encoding="utf-8")
    assert "## 一句话结论" in body
    assert "## 附录：明细数据" in body
    assert await _event_count(container) == before


async def test_rerun_reuses_files(container: Container) -> None:
    settings = container.settings
    today = datetime.now(settings.daily_tz).date()
    repo = "tiangolo/sqlmodel"

    await container.daily_progress_service.build(repo_full_name=repo, day=today)
    await container.daily_report_service.build(repo_full_name=repo, day=today)
    again = await container.daily_report_service.build(repo_full_name=repo, day=today)

    # 上次是降级产物（mock），所以这里会重试模型，不会被当成可复用结果
    assert again.degraded is True
    assert again.reused is False
