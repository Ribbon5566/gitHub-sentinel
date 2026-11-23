# GitHub Sentinel

自托管的 GitHub 仓库动态哨兵：订阅仓库 → 定时增量采集 commit / issue / PR / release → AI 归纳成摘要报告 → 邮件推送 → 看板回看。

```
定时 tick(60s) → 抢占到期订阅 → 增量采集(GitHub REST) → 过滤去重(SQLite)
              → 规则预压缩 → LLM 摘要(失败降级 Mock) → 渲染 HTML → 通知(SMTP) → 重排下一次
```

**当前状态：Stage 1 已完成**。全链路可运行，GitHub 用内置 Fake、LLM 用规则 Mock、通知走 dry-run，
`pytest` 48 个用例全绿，分层检查通过。

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
├─ ports/          6 个 Protocol + 跨层 DTO（github / llm / notifier / renderer / clock / repositories）
├─ application/    用例编排：subscription / collect / digest / notify / pipeline
├─ infrastructure/ 端口实现：github(fake,rest) / llm(mock,openai_compat) / notify(null,smtp)
│                  / persistence(SQLAlchemy+SQLite) / rendering(Jinja) / scheduling(APScheduler)
└─ api/            FastAPI 路由 + 看板页面
tests/             unit(纯函数) / integration(文件型 SQLite) / e2e(ASGITransport)
scripts/           check_layers.py 分层守护、seed_demo
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
./.venv/Scripts/python.exe -m pytest -q            # 48 passed
./.venv/Scripts/python.exe scripts/check_layers.py # 分层检查
./.venv/Scripts/python.exe -m ruff check src tests # 静态检查
```

---

## 路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 全链路骨架（Fake GitHub + Mock LLM + dry-run 通知） | ✅ 完成 |
| 2 | 真实 GitHub REST：端点、映射、分页、限流退避、多 token | 待做 |
| 3 | 真实 LLM：预压缩、分块、结构化解析、失败降级 | 待做 |
| 4 | SMTP 实际投递 + 调度实跑 + alembic 迁移 + 多进程租约 | 待做 |
| 5 | 看板细化（表单建订阅、筛选、分页）+ 覆盖率门槛 | 待做 |
