"""0.8 缝5：账号 API —— 给前端登录页一个真实的后端，替掉纯前端 mock。

GET  /api/auth/status   账号 + 门控状态（不回 token 本体）
POST /api/auth/login    登录
POST /api/auth/logout   登出

**local 模式的登录语义与拆分前完全一致**：任意非空账号密码通过。
差别只在于——现在这个判断在后端、会话有真实落点，而不是前端自己写个 mock 串进 localStorage。
cloud_client 的真实登录（打云端 `/v1/auth/login`）在 0.9。
"""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import identity

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class SendCodeRequest(BaseModel):
    email: str
    invite_code: str = ""


class VerifyRequest(BaseModel):
    email: str
    code: str


_CLOUD_TIMEOUT = 40.0


async def _cloud_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """转发 POST 到云端。

    前端不直连云端 —— 只有本地后端知道网关地址，也只有它能把返回的 token/授权
    落进 user_settings。前端因此只面对一套 API，顺带免了 CORS。

    **有 token 就带上 Authorization** —— 登录类端点（send-code/verify）不需要它、多带无害；
    但需要鉴权的端点（如 /v1/pay/create）缺了就会被云端 401「缺少 Authorization」
    （2026-07-30 踩过：这个 helper 原本只服务登录，复用做支付下单时漏了鉴权头）。
    """
    from app.core.runtime_config import octopus_cloud_config
    from app.core import cloud_http, identity

    cfg = octopus_cloud_config()
    if not cfg.gateway_url:
        raise HTTPException(400, "未配置 Octopus 网关地址（设置 → 视频生成 → Octopus 网关地址）")

    headers: Dict[str, str] = {}
    tok = identity.cloud_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    # 连接失败自动重试（安全：请求还没发出去）—— 弱网/VPN 丢包时登录不至于随机失败
    try:
        resp = await cloud_http.post(f"{cfg.gateway_url}{path}", json=body,
                                     headers=headers, timeout=_CLOUD_TIMEOUT)
    except Exception as e:
        raise HTTPException(502, f"连不上 Octopus 云端（{cfg.gateway_url}）：{str(e)[:140]}")

    try:
        data = resp.json()
    except Exception:
        raise HTTPException(502, f"云端返回异常 HTTP {resp.status_code}")

    if resp.status_code != 200:
        # 把云端的话术原样透传给用户（"发送太频繁"/"验证码不正确，还可尝试 N 次" 等）
        raise HTTPException(resp.status_code, str(data.get("detail") or data)[:300])
    return data


async def _cloud_get(path: str) -> Dict[str, Any]:
    """带用户 token GET 云端（billing 余额/流水用）。"""
    from app.core import cloud_http, identity
    from app.core.runtime_config import octopus_cloud_config
    cfg = octopus_cloud_config()
    if not cfg.gateway_url:
        raise HTTPException(400, "未配置 Octopus 网关地址")
    headers = {}
    tok = identity.cloud_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        resp = await cloud_http.get(f"{cfg.gateway_url}{path}", headers=headers, timeout=20.0)
    except Exception as e:
        raise HTTPException(502, f"连不上 Octopus 云端：{str(e)[:120]}")
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(502, f"云端返回异常 HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, str(data.get("detail") or data)[:300])
    return data


@router.get("/status")
async def auth_status() -> Dict[str, Any]:
    from app.core import entitlements
    return {**identity.status(), "entitlements": entitlements.status()}


@router.post("/login")
async def login(payload: LoginRequest) -> Dict[str, Any]:
    from app.core.runtime_config import is_cloud_client

    user = (payload.username or "").strip()
    pwd = (payload.password or "").strip()
    if not user or not pwd:
        raise HTTPException(400, "请输入账号和密码")

    if is_cloud_client():
        # 0.9：POST {gateway}/v1/auth/login → {token, user, plan, entitlement}
        # 拿到后 identity.set_session(token, user) + 把 entitlement 落到 license.token
        raise HTTPException(
            501, "云端登录尚未上线（0.9）——当前请把部署模式切回 local 使用"
        )

    # local：与拆分前一致，任意非空即通过。本地后端本就不鉴权，这里只是给会话一个落点。
    token = identity.issue_local_token(user)
    await identity.set_session(token, user)
    return {"ok": True, "token": token, "user": user, "mode": "local"}


@router.post("/logout")
async def logout() -> Dict[str, Any]:
    await identity.clear_session()
    # 授权一并清掉 —— 否则登出后本地还留着一张有效授权，能力照开
    from app.core.user_settings_service import user_settings
    await user_settings.set("license", "token", "", sensitive=False)
    return {"ok": True}


# ─── 云端邮箱验证码登录（cloud_client 模式）───

@router.post("/send-code")
async def send_code(payload: SendCodeRequest) -> Dict[str, Any]:
    """请云端给这个邮箱发验证码。"""
    from app.core.runtime_config import is_cloud_client
    if not is_cloud_client():
        raise HTTPException(400, "本地模式不需要邮箱登录（部署模式切到 cloud_client 后再用）")
    email = (payload.email or "").strip()
    if not email:
        raise HTTPException(400, "请输入邮箱")
    data = await _cloud_post("/v1/auth/send-code",
                             {"email": email, "invite_code": payload.invite_code or ""})
    return {"ok": True, "expires_in": data.get("expires_in", 600)}


@router.post("/verify")
async def verify(payload: VerifyRequest) -> Dict[str, Any]:
    """验证码校验通过 → 落地登录态 + 授权。

    云端一次回三样：`token`（JWT，之后调 video/agent 都带它）、`user`、`license`（Ed25519 签名授权）。
    这里把 token 交给 identity、把 license 存进 user_settings —— 之后 entitlements 就能离线验签放行。
    """
    from app.core.runtime_config import is_cloud_client
    if not is_cloud_client():
        raise HTTPException(400, "本地模式不需要邮箱登录")

    email = (payload.email or "").strip()
    code = (payload.code or "").strip()
    if not email or not code:
        raise HTTPException(400, "请输入邮箱和验证码")

    data = await _cloud_post("/v1/auth/verify", {"email": email, "code": code})

    token = data.get("token") or ""
    user = data.get("user") or {}
    lic = data.get("license")
    if not token:
        raise HTTPException(502, "云端未返回登录凭据")

    await identity.set_session(token, user.get("email") or email)

    from app.core.user_settings_service import user_settings
    if lic:
        import json as _json
        await user_settings.set("license", "token", _json.dumps(lic), sensitive=False)

    return {
        "ok": True,
        "created": bool(data.get("created")),
        "token": token,
        "user": user,
        "plan": user.get("plan") or "free",
    }


# ─── credits 余额 / 流水（代理到云端）───

@router.get("/billing/balance")
async def billing_balance() -> Dict[str, Any]:
    """credits 余额。local 模式返回 unlimited（本地用自己的 key、不计 credits）。"""
    from app.core.runtime_config import is_cloud_client
    if not is_cloud_client():
        return {"credits": None, "plan": "local", "unlimited": True}
    return await _cloud_get("/v1/billing/balance")


@router.get("/billing/history")
async def billing_history() -> Dict[str, Any]:
    from app.core.runtime_config import is_cloud_client
    if not is_cloud_client():
        return {"credits": None, "entries": []}
    return await _cloud_get("/v1/billing/history")


# ─── 充值（支付宝，代理到云端）───

@router.get("/pay/tiers")
async def pay_tiers() -> Dict[str, Any]:
    """充值档位。local 模式不需要充值（用自己的 key）。"""
    from app.core.runtime_config import is_cloud_client
    if not is_cloud_client():
        return {"enabled": False, "tiers": [], "local": True}
    return await _cloud_get("/v1/pay/tiers")


@router.post("/pay/create")
async def pay_create(body: Dict[str, Any]) -> Dict[str, Any]:
    """下单 → 返回支付宝收银台 URL（前端用系统浏览器打开它）。"""
    from app.core.runtime_config import is_cloud_client
    if not is_cloud_client():
        raise HTTPException(400, "本地模式不需要充值")
    return await _cloud_post("/v1/pay/create", body or {})


@router.get("/pay/orders/{order_id}")
async def pay_order(order_id: str) -> Dict[str, Any]:
    """轮询订单状态：pending → paid 即已到账。"""
    from app.core.runtime_config import is_cloud_client
    if not is_cloud_client():
        raise HTTPException(400, "本地模式无订单")
    return await _cloud_get(f"/v1/pay/orders/{order_id}")
