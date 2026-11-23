from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request, status

from ..container import Container

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _container(request: Request) -> Container:
    return request.app.state.container


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """单用户鉴权。

    两种模式：
      * 配了 SENTINEL_API_KEY：必须带 X-API-Key，用 compare_digest 防时序侧信道；
      * 没配：只允许本机访问。这是「默认安全」——不会因为忘了配密钥就把
        采集到的仓库动态暴露到公网。
    """
    settings = _container(request).settings
    expected = settings.api_key
    if expected:
        if not x_api_key or not secrets.compare_digest(x_api_key, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="X-API-Key 无效",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        return

    client_host = request.client.host if request.client else ""
    if client_host not in _LOCAL_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="未配置 SENTINEL_API_KEY，仅允许本机访问",
        )
