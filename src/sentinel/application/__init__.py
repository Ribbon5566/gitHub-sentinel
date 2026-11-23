"""应用层：用例编排。

这一层只认 ports 里的 Protocol，不认识 SQLAlchemy、httpx、Jinja。
所以每个用例都能用内存 Fake 在毫秒级跑完，不需要数据库和网络。
"""

from .collect_service import CollectService
from .digest_service import DigestService
from .dto import CollectOutcome, DigestOutcome, NotifyOutcome, PipelineOutcome
from .notify_service import NotifyService
from .pipeline import PipelineService
from .subscription_service import SubscriptionService

__all__ = [
    "CollectOutcome",
    "CollectService",
    "DigestOutcome",
    "DigestService",
    "NotifyOutcome",
    "NotifyService",
    "PipelineOutcome",
    "PipelineService",
    "SubscriptionService",
]
