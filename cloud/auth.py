"""账号 API —— 邮箱验证码注册/登录。

    POST /v1/auth/send-code   {email, invite_code?}  → 发 6 位验证码
    POST /v1/auth/verify      {email, code}          → 登录，回 {token, user, license}
    GET  /v1/auth/me          Bearer                 → 当前用户 + 一张新鲜授权

═══ 几个刻意的设计选择 ═══

**1. 不分注册和登录。**
同一个入口：验证码通过时，邮箱没注册过就自动建号。这样既少一半接口，又天然免疫
**邮箱枚举**（攻击者无法通过接口反应差异判断某个邮箱是否注册过）。

**2. 验证码而不是 magic link。**
Octopus 是桌面端。magic link 点开是浏览器，还得把登录态回传给 App（deep link 或轮询），
平白多一层复杂度和失败点。验证码用户直接粘进 App，简单可靠。

**3. 每一封信都是钱，所以限频是硬需求。**
三道闸：同邮箱 60s 冷却、同邮箱每日 10 封、同 IP 每小时 20 封。
内测期还可以设 `INVITE_CODE` 只放邀请者进来。

**4. 验证码只存哈希、限 5 次尝试、10 分钟过期。**
库被读到也拿不到能用的码；暴力猜 6 位数在 5 次内成功的概率是 5/1e6。
"""

import logging
import re
import secrets as pysecrets
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import config
import db
import licensing
import mailer

logger = logging.getLogger("octopus.cloud.auth")

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SendCodeRequest(BaseModel):
    email: str
    invite_code: str = ""


class VerifyRequest(BaseModel):
    email: str
    code: str


def _client_ip(request: Request) -> str:
    # nginx 传的是 X-Forwarded-For；取第一个（最原始的客户端）
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def _validate_email(email: str) -> str:
    e = db.norm_email(email)
    if not e or not _EMAIL_RE.match(e) or len(e) > 320:
        raise HTTPException(400, "邮箱格式不正确")
    if config.EMAIL_DOMAIN_ALLOWLIST:
        domain = e.rsplit("@", 1)[-1]
        if domain not in config.EMAIL_DOMAIN_ALLOWLIST:
            raise HTTPException(403, "该邮箱域名暂不支持注册")
    return e


def make_jwt(user: db.User) -> str:
    from jose import jwt
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "plan": user.plan,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=config.JWT_TTL_DAYS)).timestamp()),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_jwt(token: str) -> Dict[str, Any]:
    from jose import jwt, JWTError
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except JWTError as e:
        raise HTTPException(401, f"登录态无效或已过期（{type(e).__name__}）")


async def current_user(request: Request,
                       session: AsyncSession = Depends(db.get_session)) -> db.User:
    """Bearer → User。给需要登录的端点用。"""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "缺少 Authorization: Bearer <token>")
    claims = decode_jwt(auth[7:].strip())
    r = await session.execute(select(db.User).where(db.User.id == int(claims.get("sub") or 0)))
    user = r.scalar_one_or_none()
    if not user:
        raise HTTPException(401, "用户不存在")
    if user.status != "active":
        raise HTTPException(403, "账号已停用")
    return user


def _user_public(user: db.User) -> Dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "plan": user.plan,
        "credits": user.credits,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.post("/send-code")
async def send_code(payload: SendCodeRequest, request: Request,
                    session: AsyncSession = Depends(db.get_session)) -> Dict[str, Any]:
    email = _validate_email(payload.email)
    ip = _client_ip(request)

    if config.INVITE_CODE and payload.invite_code.strip() != config.INVITE_CODE:
        raise HTTPException(403, "邀请码不正确（内测期需要邀请码）")

    now = db.utcnow()

    # 闸1：同邮箱冷却
    recent = await db.count_sends(session, email=email,
                                  since=now - timedelta(seconds=config.CODE_RESEND_COOLDOWN))
    if recent:
        raise HTTPException(429, f"发送太频繁，请 {config.CODE_RESEND_COOLDOWN} 秒后再试")

    # 闸2：同邮箱每日上限
    daily = await db.count_sends(session, email=email, since=now - timedelta(days=1))
    if daily >= config.CODE_DAILY_LIMIT_EMAIL:
        raise HTTPException(429, "该邮箱今日验证码次数已达上限，请明天再试")

    # 闸3：同 IP 每小时上限
    if ip:
        hourly = await db.count_sends(session, ip=ip, since=now - timedelta(hours=1))
        if hourly >= config.CODE_HOURLY_LIMIT_IP:
            raise HTTPException(429, "请求过于频繁，请稍后再试")

    code = f"{pysecrets.randbelow(1000000):06d}"

    # 同邮箱旧码作废（只保留最新一条）
    await session.execute(delete(db.EmailCode).where(db.EmailCode.email == email))
    session.add(db.EmailCode(
        email=email,
        code_hash=db.hash_code(code, email),
        expires_at=now + timedelta(seconds=config.CODE_TTL_SECONDS),
    ))
    session.add(db.SendLog(email=email, ip=ip))
    await session.commit()

    try:
        await mailer.send_code(email, code, config.CODE_TTL_SECONDS // 60)
    except mailer.MailError as e:
        # 发信失败就把码作废，别让用户拿着一个永远收不到的码干等
        await session.execute(delete(db.EmailCode).where(db.EmailCode.email == email))
        await session.commit()
        raise HTTPException(502, str(e))

    logger.info("[auth] 验证码已发 email=%s ip=%s", email, ip)
    return {"ok": True, "expires_in": config.CODE_TTL_SECONDS}


@router.post("/verify")
async def verify(payload: VerifyRequest,
                 session: AsyncSession = Depends(db.get_session)) -> Dict[str, Any]:
    email = _validate_email(payload.email)
    code = (payload.code or "").strip()
    if not code:
        raise HTTPException(400, "请输入验证码")

    r = await session.execute(select(db.EmailCode).where(db.EmailCode.email == email))
    rec = r.scalar_one_or_none()
    if not rec or rec.consumed:
        raise HTTPException(400, "验证码不存在或已使用，请重新获取")

    exp = rec.expires_at
    if exp.tzinfo is None:                      # SQLite 取出来可能没时区
        exp = exp.replace(tzinfo=timezone.utc)
    if db.utcnow() > exp:
        await session.execute(delete(db.EmailCode).where(db.EmailCode.email == email))
        await session.commit()
        raise HTTPException(400, "验证码已过期，请重新获取")

    if rec.attempts >= config.CODE_MAX_ATTEMPTS:
        await session.execute(delete(db.EmailCode).where(db.EmailCode.email == email))
        await session.commit()
        raise HTTPException(429, "尝试次数过多，请重新获取验证码")

    if not compare_digest(rec.code_hash, db.hash_code(code, email)):
        rec.attempts += 1
        await session.commit()
        left = config.CODE_MAX_ATTEMPTS - rec.attempts
        raise HTTPException(400, f"验证码不正确，还可尝试 {max(left, 0)} 次")

    # ── 验证通过 ──
    rec.consumed = 1
    user = await db.get_user_by_email(session, email)
    created = False
    if not user:
        user = db.User(email=email, plan="free")   # 没注册过 → 自动建号
        session.add(user)
        await session.flush()
        created = True
    user.last_login_at = db.utcnow()
    await session.commit()

    # 新注册送试用 credits（config.SIGNUP_FREE_CREDITS）—— 让新用户能试一条视频
    if created and config.SIGNUP_FREE_CREDITS > 0:
        try:
            await db.grant_credits(session, user.id, config.SIGNUP_FREE_CREDITS,
                                   reason="signup_bonus")
            await session.refresh(user)
        except Exception as e:
            logger.warning("[auth] 新用户送 credits 失败 user=%s: %s", user.id, e)

    try:
        lic = licensing.issue(sub=str(user.id), plan=user.plan)
    except licensing.LicensingError as e:
        raise HTTPException(500, f"授权签发失败：{e}")

    logger.info("[auth] 登录成功 email=%s user_id=%s 新注册=%s", email, user.id, created)
    return {
        "ok": True,
        "created": created,
        "token": make_jwt(user),
        "user": _user_public(user),
        "license": lic,          # 客户端存进 user_settings("license","token")
    }


@router.get("/me")
async def me(user: db.User = Depends(current_user)) -> Dict[str, Any]:
    """当前用户 + 一张新鲜授权（客户端可定期来续，避免授权到期后功能突然锁死）。"""
    out: Dict[str, Any] = {"ok": True, "user": _user_public(user)}
    try:
        out["license"] = licensing.issue(sub=str(user.id), plan=user.plan)
    except licensing.LicensingError as e:
        out["license_error"] = str(e)
    return out
