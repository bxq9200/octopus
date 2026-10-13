"""视频中转 —— 平台核心收入（Seedance 中转 + credits 计费）。

    POST /v1/video/generate   {kind, prompt, first_frame_url?, reference_image_urls?,
                               duration, resolution, ratio, seed}
      → {task_id, est_credits, status}          # 202 语义：任务已排队
    GET  /v1/video/tasks/{id}
      → {status, video_url?(远程), error?, usage?, credits_charged?}
    GET  /v1/billing/balance  → {credits}

═══ 三条钱规矩（都是刻意保守）═══

1. **提交时查余额、成功才扣费。** 失败/超时不扣（对用户公平）。扣费**幂等**——靠 credit_ledger
   的 (reason=video, ref=task_id) 唯一性，重复轮询绝不重复扣。
2. **字节不过服务器。** 云端只把厂商的远程 URL 交给客户端，客户端直连下载。云端产生的临时
   下载（若厂商模块顺手下了）立即清掉。
3. **企业 key 只在云端。** 客户端从不持火山/蚂蚁 key，只报"我要生成"，云端用 secrets.env 里的
   企业凭据转发厂商。没配企业凭据 → 用 stub 出片（只验钱的流转，不产真视频）。
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import config
import db
from auth import current_user

logger = logging.getLogger("octopus.cloud.video")

router = APIRouter(tags=["video"])


class GenerateRequest(BaseModel):
    kind: str = "i2v"                     # t2v | i2v
    prompt: str = ""
    first_frame_url: str = ""
    reference_image_urls: list = []
    duration: int = 5
    resolution: str = "720x1280"
    ratio: str = ""
    seed: int = -1
    # 跨镜一致：角色参考图 [{key: 稳定标识, url: 公网URL}]。云端 register_asset 成 asset://火山id 锚脸。
    character_refs: list = []


# 角色 asset 缓存：{ref_key → 火山 asset_id}。同一角色跨镜/多任务只注册一次（register_asset 慢，~30-60s）。
# 进程内缓存，重启后重注册（可接受）。并发同 key 用锁避免重复注册。
_ASSET_CACHE: Dict[str, str] = {}
_ASSET_LOCKS: Dict[str, "asyncio.Lock"] = {}


async def _get_or_register_asset(key: str, url: str) -> str:
    """按 key 缓存地把角色参考图注册进蚂蚁/火山 asset 库，返回火山 asset_id（asset:// 强锚定用）。"""
    if not key or not url:
        return ""
    if key in _ASSET_CACHE:
        return _ASSET_CACHE[key]
    lock = _ASSET_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        if key in _ASSET_CACHE:           # 拿到锁后再查一次（并发双检）
            return _ASSET_CACHE[key]
        sys.path.insert(0, "/octopus/app/backend")
        from app.modules._antchain_video import register_asset
        try:
            aid = await register_asset(url, f"char_{key.split('/')[-1][:16]}")
        except Exception as e:
            logger.warning("[asset] 注册失败 key=%s: %s", key[:40], e)
            aid = ""
        if aid:
            _ASSET_CACHE[key] = aid
            logger.info("[asset] 角色入库 key=%s → 火山id=%s", key.split('/')[-1], aid)
        return aid or ""


def _res_bucket(resolution: str) -> str:
    return "1080p" if "1080" in (resolution or "") else "720p"


def estimate_credits(duration: int, resolution: str) -> int:
    dur = max(4, min(12, int(duration) or 5))
    per = (config.CREDITS_PER_SEC_1080P if _res_bucket(resolution) == "1080p"
           else config.CREDITS_PER_SEC_720P)
    return dur * per


# ═══════════════════════════════════════════════════════
# 厂商转发（企业 key 在云端）
# ═══════════════════════════════════════════════════════

async def _cloud_upload_to_public_url(src: str) -> str:
    """把 base64 data URL / 本地路径 上传到平台 OSS，返回无签名公网 URL（厂商能抓）。
    云端跑在 local 模式，OSS 凭据由 startup 注入 user_settings，故 oss_client 直接可用。"""
    if not config.oss_configured():
        return ""
    sys.path.insert(0, "/octopus/app/backend")
    from app.core import oss_client
    import base64 as _b64
    if src.startswith("data:"):
        try:
            head, b64 = src.split(",", 1)
            ext = "png"
            if "image/" in head:
                ext = head.split("image/", 1)[1].split(";", 1)[0] or "png"
            data = _b64.b64decode(b64)
        except Exception as e:
            logger.warning("[video] base64 首帧解码失败: %s", e)
            return ""
        return await oss_client.upload_bytes(data, ext=ext, content_type=f"image/{ext}", public=True) or ""
    # 本地路径：云端没这文件，无能为力（客户端应发 base64 或公网 URL）
    logger.warning("[video] 首帧是本地路径但云端无此文件: %s", src[:60])
    return ""


class MediaUploadRequest(BaseModel):
    data: str            # data:image/...;base64,xxx 或裸 base64
    ext: str = "png"


@router.post("/v1/media/upload")
async def media_upload(req: MediaUploadRequest,
                       user: db.User = Depends(current_user)) -> Dict[str, Any]:
    """把客户端图片（base64）上传到**平台 OSS**，返回公网 URL。
    让本地执行器（BYOK 图片生成）无需自建 OSS，也能给 Seedream 5.0 等厂商喂公网参考图。
    图片很小、走一趟云端换用户零配置；企业 OSS 凭据只在云端。"""
    if not config.oss_configured():
        raise HTTPException(503, "平台 OSS 未配置")
    src = req.data if req.data.startswith("data:") else f"data:image/{req.ext};base64,{req.data}"
    url = await _cloud_upload_to_public_url(src)
    if not url:
        raise HTTPException(500, "上传平台 OSS 失败")
    logger.info("[media] user=%s 上传平台 OSS → %s", getattr(user, "email", "?"), url[:80])
    return {"public_url": url}


async def _generate_via_vendor(task: db.VideoTask, params: Dict[str, Any]) -> str:
    """调厂商生成 → 返回**远程视频 URL**（不落地云端）。

    有 antchain 企业凭据 → 走真的（复用客户端那套 _antchain_video，凭据由 startup 注入 user_settings）。
    没配 → stub：返回一个占位远程 URL（只为把 credits/任务流转验通，客户端不会真下载）。
    """
    if not config.video_vendor_configured():
        # stub：模拟厂商耗时 + 返回占位 URL
        await asyncio.sleep(0.2)
        return f"stub://octopus-cloud/video/{task.id}.mp4"

    # 真实：复用 backend 的 antchain 模块（startup 已把企业 AK/SK 注入 user_settings）
    sys.path.insert(0, "/octopus/app/backend")
    from app.modules import _antchain_video

    kind = params.get("kind") or "i2v"
    # 云端自给自足：客户端要是发了 base64/本地路径（没有自己的 OSS），云端用平台 OSS 传成公网 URL。
    # 客户端已发 http(s) 公网 URL（有自己 OSS 的情况）→ 原样透传。厂商只认公网 URL。
    ff = params.get("first_frame_url") or ""
    if ff and not ff.startswith(("http://", "https://")):
        pub = await _cloud_upload_to_public_url(ff)
        if pub:
            params = {**params, "first_frame_url": pub}
        else:
            raise RuntimeError("首帧非公网 URL 且云端 OSS 未配，无法转公网")
    # 关键：给厂商传**完整分辨率**（如 720x1280）而不是 task.resolution（=计费档位 "720p"，丢了横竖屏）。
    # antchain 从 "720x1280" 能推出 9:16 竖屏；只给 "720p" 会默认横屏（用户实测竖屏出成横屏的根因）。
    res_full = params.get("resolution_full") or task.resolution
    ratio = params.get("ratio") or ""
    if kind == "t2v":
        result = await _antchain_video.generate_text_to_video(
            params.get("prompt") or "",
            duration=task.seconds, resolution=res_full, ratio=ratio,
            reference_images=params.get("reference_image_urls") or [],
        )
    else:
        # 跨镜一致：把角色参考图注册进 asset 库（按 key 缓存，只注册一次）→ asset://火山id 强锚定人脸。
        # 缓存 key 必须带 user_id 隔离，否则不同用户撞了同名本地路径会张冠李戴。
        asset_ids = []
        for ref in (params.get("character_refs") or []):
            ck = f"{task.user_id}:{ref.get('key') or ''}"
            aid = await _get_or_register_asset(ck, ref.get("url") or "")
            if aid:
                asset_ids.append(aid)
        if asset_ids:
            logger.info("[video] i2v 锚定 %d 个角色 asset: %s", len(asset_ids), asset_ids)

        # 首帧免真人审核：关键帧/尾帧含真人脸，客户端转成 OSS 公网 URL 后丢了「火山信任产物」身份
        # → 蚂蚁真人审核会拦（"input image may contain real person" → i2v 失败降级 t2v，丢一致性）。
        # 入 asset 库成 asset://火山id 即免审 + 精确构图。首帧每镜不同（缓存 key=URL 本身，重跑复用）。
        ff = params.get("first_frame_url") or ""
        if ff and ff.startswith(("http://", "https://")):
            ff_vid = await _get_or_register_asset(f"{task.user_id}:ff:{ff}", ff)
            if ff_vid:
                ff = f"asset://{ff_vid}"
                logger.info("[video] i2v 首帧入库免审 → asset://%s", ff_vid)
            else:
                logger.warning("[video] i2v 首帧入库失败，用原图（可能被真人审核拦）")
        result = await _antchain_video.generate_image_to_video(
            ff, params.get("prompt") or "",
            duration=task.seconds, resolution=res_full,
            reference_images=params.get("reference_image_urls") or [],
            character_asset_ids=asset_ids,
        )
    remote = result.get("origin_url") or ""
    # 字节不过服务器：厂商模块顺手下到本地的文件立即清掉
    local = result.get("video_url") or ""
    if local.startswith("/output/"):
        try:
            os.remove(os.path.join("/octopus/app/backend/output", local[len("/output/"):]))
        except OSError:
            pass
    if not remote:
        raise RuntimeError("厂商未返回远程视频 URL")
    return remote


async def _run_task(task_id: str, params: Dict[str, Any]) -> None:
    """后台生成。完成后写 status + remote_url；成功则扣 credits（幂等）。"""
    async with db.SessionLocal() as session:
        r = await session.execute(select(db.VideoTask).where(db.VideoTask.id == task_id))
        task = r.scalar_one_or_none()
        if not task:
            return
        task.status = "running"
        task.updated_at = db.utcnow()
        await session.commit()

    try:
        remote = await _generate_via_vendor(task, params)
    except Exception as e:
        logger.warning("[video] 任务 %s 生成失败: %s", task_id, e)
        async with db.SessionLocal() as session:
            r = await session.execute(select(db.VideoTask).where(db.VideoTask.id == task_id))
            t = r.scalar_one_or_none()
            if t:
                t.status = "failed"
                t.error = str(e)[:400]
                t.updated_at = db.utcnow()
                await session.commit()
        return

    # 成功：写 remote_url + 扣费（幂等）
    async with db.SessionLocal() as session:
        r = await session.execute(select(db.VideoTask).where(db.VideoTask.id == task_id))
        t = r.scalar_one_or_none()
        if not t:
            return
        t.status = "succeeded"
        t.remote_url = remote
        t.updated_at = db.utcnow()
        await session.commit()
        # 扣费：成功才扣，幂等
        charged = await db.charge_for_task(session, t, t.est_credits)
        logger.info("[video] 任务 %s 成功，扣费=%s credits=%s", task_id, charged, t.est_credits)


# ═══════════════════════════════════════════════════════
# 端点
# ═══════════════════════════════════════════════════════

@router.post("/v1/video/generate")
async def generate(payload: GenerateRequest,
                   user: db.User = Depends(current_user),
                   session: AsyncSession = Depends(db.get_session)) -> Dict[str, Any]:
    if payload.kind not in ("t2v", "i2v"):
        raise HTTPException(400, f"未知 kind: {payload.kind}")

    dur = max(4, min(12, int(payload.duration) or 5))
    est = estimate_credits(dur, payload.resolution)

    # 余额门控（video = pay per use）：不够就 402，并告诉差多少
    balance = int(user.credits or 0)
    if balance < est:
        raise HTTPException(
            402, f"credits 不足：本次约需 {est}，当前余额 {balance}。请充值后再试。")

    task_id = uuid.uuid4().hex
    params = {
        "kind": payload.kind, "prompt": payload.prompt,
        "first_frame_url": payload.first_frame_url,
        "reference_image_urls": payload.reference_image_urls or [],
        "character_refs": payload.character_refs or [],
        "ratio": payload.ratio, "seed": payload.seed,
        # 保留完整分辨率（720x1280）给厂商推横竖屏；task.resolution 只存计费档位（720p）
        "resolution_full": payload.resolution,
    }
    task = db.VideoTask(
        id=task_id, user_id=user.id, status="queued", kind=payload.kind,
        params=json.dumps(params, ensure_ascii=False),
        seconds=dur, resolution=_res_bucket(payload.resolution), est_credits=est,
    )
    session.add(task)
    await session.commit()

    asyncio.create_task(_run_task(task_id, params))
    logger.info("[video] 用户 %s 提交任务 %s（%s, %ds, %s, est=%s）",
                user.id, task_id, payload.kind, dur, task.resolution, est)
    return {"task_id": task_id, "status": "queued", "est_credits": est}


@router.get("/v1/video/tasks/{task_id}")
async def get_task(task_id: str,
                   user: db.User = Depends(current_user),
                   session: AsyncSession = Depends(db.get_session)) -> Dict[str, Any]:
    r = await session.execute(select(db.VideoTask).where(db.VideoTask.id == task_id))
    task = r.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.user_id != user.id:
        raise HTTPException(403, "无权访问该任务")   # 任务归属校验
    out: Dict[str, Any] = {"status": task.status}
    if task.status == "succeeded":
        out["video_url"] = task.remote_url          # 远程 URL，客户端直连下载
        out["usage"] = {
            "provider": "octopus_cloud", "model": "seedance", "kind": "video",
            "seconds": task.seconds, "resolution": task.resolution,
        }
        out["credits_charged"] = task.credits_charged
    elif task.status == "failed":
        out["error"] = task.error or "生成失败"
    return out


@router.get("/v1/billing/balance")
async def balance(user: db.User = Depends(current_user)) -> Dict[str, Any]:
    # 顺带回账号标识 —— 客户端「账户」页要显示当前登录的是谁（避免多账号搞混），
    # 挂在这个已有的轮询接口上，省一次请求。
    return {"credits": int(user.credits or 0), "plan": user.plan,
            "email": user.email, "user_id": user.id}


@router.get("/v1/billing/history")
async def history(user: db.User = Depends(current_user),
                  session: AsyncSession = Depends(db.get_session)) -> Dict[str, Any]:
    """当前用户的 credits 流水（最近 50 条）。"""
    r = await session.execute(
        select(db.CreditLedger).where(db.CreditLedger.user_id == user.id)
        .order_by(db.CreditLedger.id.desc()).limit(50))
    rows = r.scalars().all()
    return {"credits": int(user.credits or 0), "entries": [{
        "delta": e.delta, "balance_after": e.balance_after, "reason": e.reason,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    } for e in rows]}


class AdminGrantRequest(BaseModel):
    email: str
    amount: int
    reason: str = "topup"


@router.post("/v1/billing/grant")
async def admin_grant(payload: AdminGrantRequest, request: Request,
                      session: AsyncSession = Depends(db.get_session)) -> Dict[str, Any]:
    """管理员手动给用户加/减 credits（自动支付上线前的过渡充值）。

    用 X-Admin-Token 头鉴权（secrets.env 的 ADMIN_TOKEN）——**不是用户 JWT**，
    这是平台方运维接口。真实自动支付（微信/支付宝）是独立阶段，回调里也调 grant_credits。
    """
    if not config.admin_configured():
        raise HTTPException(503, "未配置 ADMIN_TOKEN")
    tok = request.headers.get("x-admin-token", "")
    if not tok or tok != config.ADMIN_TOKEN:
        raise HTTPException(403, "管理员 token 无效")
    email = db.norm_email(payload.email)
    user = await db.get_user_by_email(session, email)
    if not user:
        raise HTTPException(404, f"用户不存在: {email}")
    new_bal = await db.grant_credits(session, user.id, int(payload.amount),
                                     reason=payload.reason or "topup")
    logger.info("[billing] admin 授信 %s %+d → %s", email, payload.amount, new_bal)
    return {"ok": True, "email": email, "credits": new_bal}
