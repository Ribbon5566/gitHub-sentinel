"""每日报告：读取每日进展 Markdown -> DeepSeek -> 正式管理层报告。

这个服务**不访问 GitHub、也不访问数据库**，只读一个文件、调一次模型、写一个文件。
好处是采集挂了也能重跑报告，改提示词也能拿历史数据反复对比。

幂等（直接关系到 token 账单）：
  * 报告头里记了 `source_fingerprint`（进展内容指纹）与 `source_sha256`（全文指纹）；
  * 两个都对得上、且上次不是降级产物，才跳过模型调用；
  * 上次是降级产物就一定重试——否则一次临时故障会把「规则兜底版」永久缓存下来。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from ..config import Settings
from ..domain.completion import CompletionRequest
from ..domain.errors import LLMError, NotFoundError, ValidationError
from ..domain.frontmatter import meta_bool, meta_int, meta_list
from ..domain.models import normalize_repo_full_name
from ..domain.progress import STAT_KEYS
from ..domain.report import (
    REPORT_SYSTEM_PROMPT,
    build_report_user_prompt,
    normalize_report_body,
    source_sha256,
    strip_section_markers,
    truncate_for_llm,
)
from ..ports.clock import Clock
from ..ports.daily import DailyStorePort
from ..ports.llm import LLMProvider
from ..ports.renderer import DailyRendererPort, DailyReportContext
from .dto import DailyReportOutcome

logger = logging.getLogger(__name__)


class DailyReportService:
    def __init__(
        self,
        *,
        store: DailyStorePort,
        llm: LLMProvider,
        renderer: DailyRendererPort,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._store = store
        self._llm = llm
        self._renderer = renderer
        self._clock = clock
        self._settings = settings

    async def build(
        self, *, repo_full_name: str, day: date, force: bool = False
    ) -> DailyReportOutcome:
        repo = normalize_repo_full_name(repo_full_name)
        document = self._store.read_progress(repo, day)
        if document is None:
            raise NotFoundError(
                f"未找到 {repo} 在 {day} 的每日进展，"
                f"请先运行：sentinel progress --repo {repo} --date {day}"
            )

        meta = document.meta
        if meta.get("repo") and meta["repo"] != repo:
            raise ValidationError(
                f"进展文件里的仓库是 {meta['repo']}，与请求的 {repo} 不一致"
            )

        fingerprint = meta.get("fingerprint", "")
        digest = source_sha256(document.markdown)

        if not force:
            previous = self._store.read_report(repo, day)
            if previous is not None and self._reusable(previous.meta, fingerprint, digest):
                logger.info("daily.report_reused repo=%s day=%s path=%s", repo, day, previous.relative_path)
                return DailyReportOutcome(
                    repo_full_name=repo,
                    day=day,
                    path=previous.relative_path,
                    model=previous.meta.get("model", ""),
                    degraded=False,
                    reused=True,
                    source_fingerprint=fingerprint,
                )

        stats = {key: meta_int(meta, key) for key in STAT_KEYS}
        context = DailyReportContext(
            repo_full_name=repo,
            repo_title=meta.get("repo_title") or repo,
            day=day,
            timezone=meta.get("timezone") or self._settings.timezone,
            stats=stats,
            highlights=meta_list(meta, "highlights_json"),
            risks=meta_list(meta, "risks_json"),
        )

        prompt_md, truncated = truncate_for_llm(
            strip_section_markers(document.body), self._settings.report_max_input_chars
        )
        request = CompletionRequest(
            system=REPORT_SYSTEM_PROMPT,
            user=build_report_user_prompt(
                repo_full_name=repo,
                repo_title=context.repo_title,
                day=day,
                timezone=context.timezone,
                stats=stats,
                highlights=context.highlights,
                risks=context.risks,
                progress_md=prompt_md,
                truncated=truncated,
            ),
            max_tokens=self._settings.report_max_output_tokens,
            temperature=self._settings.llm_temperature,
            json_mode=False,
        )

        degraded = False
        try:
            result = await self._llm.complete(request)
            body = normalize_report_body(result.text)
            if body is None:
                raise LLMError("模型输出缺少必备章节")
            model = result.model or self._llm.name
        except LLMError as exc:
            logger.warning("daily.report_llm_failed repo=%s day=%s err=%s", repo, day, exc)
            body = self._renderer.render_fallback_body(context)
            model = "rules-fallback"
            degraded = True

        report_meta = {
            "repo": repo,
            "repo_title": context.repo_title,
            "date": f"{day:%Y-%m-%d}",
            "timezone": context.timezone,
            "generated_at": _iso(self._clock.now()),
            "model": model,
            "degraded": "true" if degraded else "false",
            "source_fingerprint": fingerprint,
            "source_sha256": digest,
            "progress_file": document.relative_path,
            "input_truncated": "true" if truncated else "false",
        }
        markdown = self._renderer.render_report(
            meta=report_meta, body_md=body, sections=document.sections
        )
        path = self._store.write_report(repo_full_name=repo, day=day, content=markdown)
        logger.info(
            "daily.report_written repo=%s day=%s model=%s degraded=%s path=%s",
            repo,
            day,
            model,
            degraded,
            path,
        )
        return DailyReportOutcome(
            repo_full_name=repo,
            day=day,
            path=path,
            model=model,
            degraded=degraded,
            source_fingerprint=fingerprint,
        )

    @staticmethod
    def _reusable(meta: dict[str, str], fingerprint: str, digest: str) -> bool:
        if not fingerprint or meta.get("source_fingerprint") != fingerprint:
            return False
        if meta.get("source_sha256") != digest:
            return False
        return not meta_bool(meta, "degraded")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
