"""0.8 缝5：账号 / token 管道。

`local`（默认）= 单机单用户，`current_user_id()` 恒为 `LOCAL_USER`，**不做任何鉴权**（现状）。
`cloud_client`  = 持云端签发的 JWT，在调 `/v1/video/generate` 和 `/v1/agent/run` 时随请求转发。

═══ 这条缝要解决的实际问题 ═══

拆分前是"零鉴权 + 假登录"：`LoginPage.tsx` 任意非空账号密码就通过、把 `mock_xxx` 写进
localStorage，而 `api.ts` **从来不发这个 token**，后端也没有任何 auth 中间件。
0.8 把管道接通：前端统一带 `Authorization: Bearer`，后端有一个 `current_user` 抽象，
本地照旧放行；0.9 换成真实云端登录时，两头都不用再改结构。

═══ 本地后端为什么不需要鉴权 ═══

本地后端跑在用户自己机器上、单机单用户，给它加多租户鉴权没有意义（用户对自己的
后端天然有完全权限）。真正需要 token 的是**出站方向**：本机 → 云端网关。
所以这里的重点不是"验别人的 token"，而是"持有并转发我自己的 token"。

═══ token 的单一真相 ═══

缝1 曾让用户手填 `video.octopus_cloud_token`。0.8 起以 `account.token`（登录写入）为准，
手填字段作为兜底保留 —— 已经填过的用户不受影响（零回归）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 单机单用户假设下的固定用户 ID（user_settings 的行也用它）
LOCAL_USER = "local"

_ACCOUNT_CATEGORY = "account"
_TOKEN_KEY = "token"
_USER_KEY = "user"
# 缝1 遗留的手填字段，作为 token 兜底
_LEGACY_TOKEN = ("video", "octopus_cloud_token")


def _settings():
    from app.core.user_settings_service import user_settings
    return user_settings


def cloud_token() -> str:
    """要转发给云端的 token。优先登录写入的 account.token，兜底缝1 的手填字段。"""
    us = _settings()
    tok = us.get_with_fallback(_ACCOUNT_CATEGORY, _TOKEN_KEY, "", sensitive=True)
    if tok:
        return tok
    return us.get_with_fallback(_LEGACY_TOKEN[0], _LEGACY_TOKEN[1], "", sensitive=True)


def account_user() -> str:
    """登录的账号名（展示用）。"""
    return _settings().get(_ACCOUNT_CATEGORY, _USER_KEY) or ""


def current_user_id() -> str:
    """当前用户 ID。local 恒 LOCAL_USER —— 单机单用户，不做多租户隔离。"""
    from app.core.runtime_config import is_cloud_client
    if not is_cloud_client():
        return LOCAL_USER
    return account_user() or LOCAL_USER


def is_authenticated() -> bool:
    """cloud_client 下是否已持有 token。local 恒 True（不需要登录）。"""
    from app.core.runtime_config import is_cloud_client
    if not is_cloud_client():
        return True
    return bool(cloud_token())


def require_cloud_token() -> str:
    """取转发用 token，没有就报可操作的错。"""
    tok = cloud_token()
    if not tok:
        raise ValueError("未登录：请先登录 Octopus 账号（设置 → 账号）后再使用云端服务")
    return tok


async def set_session(token: str, user: str = "") -> None:
    """登录成功后落地会话。token 按敏感字段加密存。"""
    us = _settings()
    await us.set(_ACCOUNT_CATEGORY, _TOKEN_KEY, token or "", sensitive=True)
    if user:
        await us.set(_ACCOUNT_CATEGORY, _USER_KEY, user, sensitive=False)


async def clear_session() -> None:
    """登出：清 token 与账号名（不动其它任何配置）。"""
    us = _settings()
    await us.set(_ACCOUNT_CATEGORY, _TOKEN_KEY, "", sensitive=True)
    await us.set(_ACCOUNT_CATEGORY, _USER_KEY, "", sensitive=False)


def issue_local_token(user: str) -> str:
    """local 模式的本地会话票据。

    **不是安全凭据** —— 本地后端本来就不鉴权，它只是让"已登录"这个状态有个落点，
    好让前端 / App.tsx 的判断有真实来源。cloud_client 下的 token 一律由云端签发。
    """
    return f"local.{user}.{int(time.time())}"


# ═══════════════════════════════════════════════════════
# FastAPI 依赖：给 0.9 需要鉴权的端点用
# ═══════════════════════════════════════════════════════

async def get_current_user(authorization: Optional[str] = None) -> str:
    """当前请求的用户。

    local：恒 LOCAL_USER，**忽略 Authorization**（本地后端不鉴权，现状）。
    cloud_client：从 Bearer 取 token。0.8 只做"取出并记账"，不校验签名 ——
        真正的校验方是**云端网关**（本机后端校验自己发的 token 没有安全意义）。
    """
    from app.core.runtime_config import is_cloud_client
    if not is_cloud_client():
        return LOCAL_USER
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        token = cloud_token()
    return account_user() or (LOCAL_USER if not token else "cloud")


def status() -> Dict[str, Any]:
    """诊断/前端用的账号状态。**不回 token 本体。**"""
    from app.core.runtime_config import deployment_mode
    return {
        "mode": deployment_mode(),
        "user": account_user(),
        "user_id": current_user_id(),
        "authenticated": is_authenticated(),
        "has_token": bool(cloud_token()),
    }
