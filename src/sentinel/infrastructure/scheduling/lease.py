"""租约参数与多进程扩展点。

Stage 1 的租约完全落在 SQL 里：claim_due 把 next_run_at 推到「现在 + lease」，
同一订阅在租约期内不会被第二个 tick 抢到，覆盖了「手动 tick 与定时 tick 并发」的场景。

Stage 4 引入多 worker 时需要补两层，届时在此实现：
  1. 全局租约表 job_locks(name PRIMARY KEY, owner, expires_at)，抢到才允许跑 tick；
  2. 每订阅独立租约（claim_due 的 lease_seconds 即为此），失败时主动释放而非等超时。
"""

from __future__ import annotations

from ...domain.schedule import lease_seconds_for

__all__ = ["lease_seconds_for"]
