"""云端 HTTP 客户端 —— 只对「连接建立失败」做安全重试。

═══ 为什么只重试 ConnectError / ConnectTimeout ═══

`httpx.ConnectError` / `ConnectTimeout` 在**请求字节发出之前**抛出 —— TCP/TLS 连接压根没建成，
请求从未到达服务器。所以重试它对**任何操作都绝对安全**，包括非幂等的 `/v1/agent/run`、
`/v1/video/generate`：既然请求没发出去，就不可能重复执行/重复扣费。

**绝不重试**：`ReadTimeout`（请求已发出、服务端可能正在处理）、`WriteError`（发到一半）、
或已经收到的 HTTP 错误响应（4xx/5xx）—— 这些盲重试会导致重复副作用。

这不只是给开发机 VPN 丢包擦屁股（实测本机→云端丢包率一度 30%）——真实用户在
手机/弱网下同样会遇到握手失败，这层重试对他们一样有价值。
"""

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 连接失败重试：3 次尝试（首发 + 2 重试），指数退避 0.4s / 0.8s。
# 若单次成功率 70%，3 次里至少一次成功 ≈ 97%；开发机 VPN 场景够用。
_CONNECT_ATTEMPTS = 3
_BACKOFF_BASE = 0.4


async def post(url: str, *, json: Dict[str, Any], headers: Optional[Dict[str, str]] = None,
               timeout: float = 60.0) -> "Any":
    """POST，带连接重试。返回 httpx.Response（调用方自行判 status_code）。

    连接始终建不起来时，抛最后一次的 httpx.ConnectError（调用方照常 catch）。
    """
    import httpx

    last_exc: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                return await client.post(url, json=json, headers=headers or {})
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # 连接没建成 = 请求没发出 = 重试安全
                last_exc = e
                if attempt < _CONNECT_ATTEMPTS - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.info("云端连接失败(%s)，%.1fs 后重试 (%d/%d)",
                                type(e).__name__, delay, attempt + 1, _CONNECT_ATTEMPTS - 1)
                    await asyncio.sleep(delay)
            # 其它异常（ReadTimeout/WriteError 等）不在这里吞 —— 直接抛给调用方，绝不盲重试
    assert last_exc is not None
    raise last_exc


async def get(url: str, *, headers: Optional[Dict[str, str]] = None,
              timeout: float = 30.0) -> "Any":
    """GET，带连接重试。GET 天然幂等，重试更无顾虑。"""
    import httpx

    last_exc: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                return await client.get(url, headers=headers or {})
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_exc = e
                if attempt < _CONNECT_ATTEMPTS - 1:
                    await asyncio.sleep(_BACKOFF_BASE * (2 ** attempt))
    assert last_exc is not None
    raise last_exc
