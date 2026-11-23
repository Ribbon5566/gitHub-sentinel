# GitHub Sentinel

自托管的 GitHub 仓库动态哨兵：订阅仓库 → 定时增量采集 commit / issue / PR / release → AI 归纳成摘要报告 → 邮件推送 → 看板回看。

v2.0 起另有一条独立的「每日动线」：按项目 + 日期落一份每日进展 Markdown，再让 DeepSeek 汇总成正式的项目每日报告。

```
定时 tick(60s) → 抢占到期订阅 → 增量采集(GitHub REST) → 过滤去重(SQLite)
              → 规则预压缩 → LLM 摘要(失败降级 Mock) → 渲染 HTML → 通知(SMTP) → 重排下一次

每日动线（手动 / 外部 cron 触发）：
progress → GitHub 当日 issues/PR → data/daily/{YYYY}/{MM}/{repo}/{date}.md
report   → 读上面的 md → deepseek-flash → ...-report.md（失败降级规则模板）
```

**当前状态：v2.0**。周期简报链路可运行（默认 Fake GitHub + Mock LLM + dry-run 通知）；
真实 GitHub REST（四资源、分页、ETag、限流退避）与 DeepSeek `deepseek-flash` 已接通，
每日进展与每日报告落 `data/daily/`。`pytest` 163 个用例全绿，分层检查与 ruff 通过。

版本变更见 [`docs/releases/v2.0.md`](docs/releases/v2.0.md)（上一版：[`v1.0.md`](docs/releases/v1.0.md)）。

---

## 快速开始

```bash
cd /d/project/GitHubSentinel

# 1. 装依赖（国内网络建议加镜像，否则 pypi.org 可能卡住）
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -U pip
./.venv/Scripts/python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[dev]"

# 2. 配置（可选，默认值即可跑：fake 数据 + mock 摘要 + 不真发通知）
cp .env.example .env

# 3. 造演示数据并跑通全链路
./.venv/Scripts/python.exe -m sentinel.cli seed-demo

# 4. 起服务
./.venv/Scripts/python.exe -m uvicorn sentinel.main:app --port 8000 --loop asyncio

# 5. 每日进展 + 每日报告（默认 fake 数据；没配 key 时报告走规则兜底）
./.venv/Scripts/python.exe -m sentinel.cli daily --repo fastapi/fastapi
```

打开：

- 看板 <http://127.0.0.1:8000/>（Basic Auth，用户名随意，密码 = `.env` 里的 `SENTINEL_API_KEY`）
- API 文档 <http://127.0.0.1:8000/docs>
- 生成的报告在 `data/reports/YYYY/MM/{订阅ID}/` 下

API 调用示例（curl 不可用时用 `/docs` 或 Python `urllib`）：

```bash
curl -s localhost:8000/healthz
curl -s -H "X-API-Key: dev-key" localhost:8000/api/v1/subscriptions
curl -s -X POST -H "X-API-Key: dev-key" localhost:8000/api/v1/subscriptions/1/collect
curl -s -X POST -H "X-API-Key: dev-key" localhost:8000/api/v1/subscriptions/1/digest
curl -s -H "X-API-Key: dev-key" localhost:8000/api/v1/reports
```

命令行：

| 命令 | 用途 |
|---|---|
| `sentinel.cli seed-demo` | 造演示订阅 + 数据，跑完采集→报告→通知 |
| `sentinel.cli subscribe owner/repo --cron "0 9 * * *"` | 加订阅 |
| `sentinel.cli collect [--id N]` | 只采集，验证增量与去重 |
| `sentinel.cli digest [--id N] [--force]` | 只出报告 |
| `sentinel.cli tick --once` | 只跑一轮调度，验证抢占与重排 |
| `sentinel.cli list` | 列订阅与最近报告 |
| `sentinel.cli progress [--repo R] [--id N] [--date D] [--force]` | 只生成每日进展 md（访问 GitHub，不调模型） |
| `sentinel.cli report [--repo R] [--id N] [--date D] [--force]` | 只从已有进展生成每日报告（调模型，不访问 GitHub） |
| `sentinel.cli daily [...]` | 上面两步连着跑 |

---

## 每日进展与每日报告（v2.0）

```bash
# 默认今天、全部启用订阅；--repo 可绕过订阅直接指定仓库（可重复）
sentinel.cli progress --repo fastapi/fastapi
sentinel.cli report   --repo fastapi/fastapi --date 2026-11-23
sentinel.cli daily    --id 1 --force
```

产物布局（按「项目 + 日期」命名）：

```
data/daily/2026/11/fastapi__fastapi/
├─ 2026-11-23.md          # 每日进展：front matter + 统计表 + 议题/PR 明细表
└─ 2026-11-23-report.md   # 每日报告：一句话结论 / 关键进展 / 风险与阻塞 / 下步计划 + 附录
```

口径与约定：

- **选题口径**：issues / PR 的 `updated_at` 落在当日窗口（订阅时区的自然日），
  包含当日新建、有新活动、状态变为 `closed` / `merged` 的对象；机器人账号按订阅规则过滤。
- **采集哪些资源**：默认 `SENTINEL_DAILY_RESOURCES=issues,pulls`。REST 客户端四种资源都已实现，
  想要提交与发布明细就改成 `issues,pulls,commits,releases`（模板会多渲染两节）。
- **报告章节由代码兜底**：四节缺一节就判定模型失败，改用规则模板，不会落一份内容空白的报告。
- **幂等**：进展头里记内容指纹，同日同数据重跑不重写；报告头里记 `source_fingerprint` +
  `source_sha256`，两者都对得上且上次不是降级产物才跳过模型调用（`--force` 强制重跑）。
- **降级可见**：报告 front matter 的 `degraded: true` 与 `model: rules-fallback` 标明这是规则产物。
- 每日动线**不写数据库**、不进 tick 调度：需要明确「报告日 + 时区 + 重跑」语义，先用 CLI / 外部 cron 触发。

已知限制：

- `--date` 回溯是「当前 `updated_at` 落在该日窗口」的查询，不是历史快照；窗口后又被更新的对象会漏。
  默认只建议跑今天/昨天。
- 单资源最多 `SENTINEL_DAILY_MAX_ITEMS_PER_RESOURCE` 条，超出会在文档里标 `truncated`（不是错误）。
- 报告输入超过 `SENTINEL_REPORT_MAX_INPUT_CHARS` 会被按行截断，并在 front matter 标 `input_truncated: true`。

---

## 目录与分层

```
src/sentinel/
├─ main.py         FastAPI 应用工厂 + lifespan
├─ worker.py       只跑调度、不跑 HTTP 的进程（生产推荐单独部署）
├─ cli.py          命令行
├─ container.py    组合根：唯一同时依赖 api 与 infrastructure 的地方
├─ config.py       pydantic-settings，SENTINEL_* 环境变量
│
├─ domain/         纯领域：enums / models / dedup / filters / schedule / digest / errors
│                  / progress(每日进展) / frontmatter / report(报告契约) / completion
├─ ports/          7 个 Protocol + 跨层 DTO（github / llm / notifier / renderer / daily / clock / repositories）
├─ application/    用例编排：subscription / collect / digest / notify / pipeline
│                  / daily_progress / daily_report
├─ infrastructure/ 端口实现：github(fake,rest,mapping) / llm(mock,openai_compat) / notify(null,smtp)
│                  / persistence(SQLAlchemy+SQLite) / rendering(Jinja + daily_store/daily_renderer)
│                  / scheduling(APScheduler)
└─ api/            FastAPI 路由 + 看板页面
tests/             unit(纯函数 + respx) / integration(文件型 SQLite) / e2e(ASGITransport)
scripts/           check_layers.py 分层守护
```

依赖方向单向向内，由 `scripts/check_layers.py` 在 CI/测试里强制：

```
api → application → ports → domain
       ↑            ↑
  infrastructure ───┘        （infrastructure 只实现 ports，禁止 import application/api）
```

加一个企微/飞书通知 = 新增一个 `Notifier` 实现 + `notify/registry.py` 加一行，其余零改动。
换真实 GitHub = `SENTINEL_GITHUB_MODE=live` + token，上层代码不动。

---

## 关键设计

**1. 增量三层**
`since` 水位（服务端过滤）+ `ETag/If-None-Match`（304 不扣配额）+ 本地唯一键（兜底分页重复）。
水位取 `max(事件源侧时间)` 而非「运行开始时间」，并回退 90 秒；只有整轮成功才推进。

**2. 去重双层键**
恒定键 `(repo, kind, external_id)` + 版本键（commit=sha、issue/PR=updated_at、release=published_at）。
既能让「issue 被关闭 / PR 被合并」再次播报，又不会让改个错别字变成一条新闻。

**3. 单 tick 调度**
不用「每订阅一个 cron job」：那需要与 CRUD 双向同步、重启全丢、多进程各跑一份。
这里只保留一个 60s 的 interval tick，到期判定与抢占都在 SQL 里（`claim_due` 用
`UPDATE ... WHERE next_run_at <= now RETURNING id`），重启后过期任务自然被捡起。

**4. LLM 只做归纳**
先用规则把事件压到 ~3k 字再调模型；统计数字由 `domain/digest.py` 算好，模型不负责计数。
输出强制 JSON + pydantic 校验；无 Key / 超时 / 解析失败自动降级到确定性 Mock。

**5. 报告与通知幂等**
报告 `UNIQUE(subscription, period_start, period_end)` + `content_hash` 复用，重跑同一周期不重复烧 token；
通知幂等键 `report_id:channel:target`，重发需显式 `resend`，键后追加序号，可审计不刷屏。

**6. 每日动线不复用周期简报**
每日进展是「某一天的对象清单」，不是事件流：硬塞进 `events` 表会污染 cron 窗口与去重键语义，
所以另开 `fetch_progress()` 端口与文件产物。统计数字仍由 `domain/progress.py` 算好，
模型只写文字；章节缺失、空内容、API 报错都退回规则模板，报告文件一定存在。

---

## 配置要点

| 变量 | 说明 |
|---|---|
| `SENTINEL_GITHUB_MODE` | `fake`（默认，离线可跑）/ `replay`（待实现）/ `live`（需 token，缺 token 启动即报错） |
| `SENTINEL_LLM_PROVIDER` | `mock`（默认）/ `openai_compat`（DeepSeek / Qwen / Ollama / OpenAI） |
| `SENTINEL_NOTIFY_CHANNEL` | `null`（默认 dry-run）/ `smtp` |
| `SENTINEL_API_KEY` | 留空则仅允许本机访问；看板 Basic 密码同此值 |
| `SENTINEL_ENABLE_SCHEDULER` | web 进程建议 false，用 `sentinel.worker` 单独跑调度 |

Windows 注意：必须装 `tzdata`（已在依赖里），否则 `ZoneInfo("Asia/Shanghai")` 直接抛错；
启动加 `--loop asyncio`。

---

## 开发

```bash
./.venv/Scripts/python.exe -m pytest -q            # 163 passed
./.venv/Scripts/python.exe scripts/check_layers.py # 分层检查
./.venv/Scripts/python.exe -m ruff check src tests # 静态检查
```

---

## 路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 全链路骨架（Fake GitHub + Mock LLM + dry-run 通知） | ✅ 完成（v1.0） |
| 2 | 真实 GitHub REST：端点、映射、分页、限流退避 | ✅ 完成（v2.0，四资源 + ETag + 退避） |
| 3 | 真实 LLM：DeepSeek `deepseek-flash` 接入 + 失败降级 | ✅ 完成（v2.0） |
| 4 | 每日进展 / 每日报告动线（手工与外部 cron 触发） | ✅ 完成（v2.0） |
| 5 | SMTP 实际投递 + 调度实跑 + alembic 迁移 + 多进程租约 | 待做 |
| 6 | 看板细化（表单建订阅、筛选、分页）+ 覆盖率门槛 | 待做 |
