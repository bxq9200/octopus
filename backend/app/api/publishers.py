"""发布平台 API

GET  /api/publishers                            列出所有平台 + 授权状态
GET  /api/publishers/{platform}/auth-url        生成 OAuth 跳转 URL
GET  /api/publishers/{platform}/callback?code=  OAuth 回调（浏览器从授权服务器跳回这里）
POST /api/publishers/{platform}/revoke          撤销当前授权
POST /api/publishers/{platform}/upload          上传单个视频（独立测试用，正常应该走 workflow/publish）

设计：
- callback 不需要前端，浏览器直接被 OAuth 提供商重定向到这里 → 后端处理完返回一个 HTML 提示页
- state 用 secrets.token_urlsafe 生成，落到内存（重启失效，但 OAuth 流程通常 < 5 分钟）
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.publishers import get_publisher, list_publishers, available_platforms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/publishers", tags=["publishers"])

# state -> platform 映射（防止 CSRF + 区分回调是哪个平台的）
_PENDING_STATES: Dict[str, str] = {}


@router.get("")
async def list_all() -> Dict[str, Any]:
    """列出所有平台 + 当前授权状态"""
    out = []
    for pub in list_publishers():
        status = await pub.auth_status()
        out.append({
            "platform": pub.platform,
            "label": pub.display_label,
            "description": pub.description,
            "register_url": pub.register_url,
            "credential_fields": pub.credential_fields,
            "configured": status.configured,
            "authorized": status.authorized,
            "user_label": status.user_label,
            "expires_at": status.expires_at,
        })
    return {"platforms": out}


@router.get("/{platform}/auth-url")
async def get_auth_url(platform: str) -> Dict[str, str]:
    pub = get_publisher(platform)
    if not pub:
        raise HTTPException(404, f"未知平台: {platform}")
    if not pub.is_configured():
        raise HTTPException(400, f"{platform} 尚未配置 client_id / client_secret，请先在系统设置里填好")
    state = secrets.token_urlsafe(32)
    _PENDING_STATES[state] = platform
    # 清理过期 state（简单版：保留最近 32 个）
    if len(_PENDING_STATES) > 32:
        for k in list(_PENDING_STATES.keys())[:-32]:
            _PENDING_STATES.pop(k, None)
    return {"auth_url": pub.auth_url(state), "state": state}


@router.get("/{platform}/callback")
async def oauth_callback(
    platform: str,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
):
    """OAuth 回调 — 浏览器从授权服务器跳回这里。返回一个简单 HTML 给用户看。"""
    if error:
        return _callback_html(False, platform, f"{error}: {error_description or ''}")
    if not code:
        return _callback_html(False, platform, "没有 code 参数")
    if not state or _PENDING_STATES.get(state) != platform:
        return _callback_html(False, platform, "state 校验失败（可能已超时，请重新点'连接'）")
    _PENDING_STATES.pop(state, None)

    pub = get_publisher(platform)
    if not pub:
        return _callback_html(False, platform, f"未知平台: {platform}")
    try:
        result = await pub.exchange_code(code)
    except Exception as e:
        logger.exception("OAuth callback 失败 %s", platform)
        return _callback_html(False, platform, str(e))
    return _callback_html(True, platform, f"已连接：{result.get('user_label') or '账号信息已保存'}")


@router.post("/{platform}/revoke")
async def revoke(platform: str) -> Dict[str, Any]:
    pub = get_publisher(platform)
    if not pub:
        raise HTTPException(404, f"未知平台: {platform}")
    await pub.revoke()
    return {"ok": True, "platform": platform}


class UploadRequest(BaseModel):
    video_path: str
    title: str
    description: str = ""
    tags: list[str] = []
    visibility: str = "private"  # private | unlisted | public | draft


@router.post("/{platform}/upload")
async def upload_video(platform: str, payload: UploadRequest) -> Dict[str, Any]:
    """独立上传 API（开发期测试用，正常发布走 /workflow/publish/export）"""
    pub = get_publisher(platform)
    if not pub:
        raise HTTPException(404, f"未知平台: {platform}")
    if not os.path.exists(payload.video_path):
        raise HTTPException(400, f"视频文件不存在: {payload.video_path}")
    result = await pub.upload(payload.video_path, {
        "title": payload.title,
        "description": payload.description,
        "tags": payload.tags,
        "visibility": payload.visibility,
    })
    return {
        "ok": result.ok,
        "platform": result.platform,
        "video_id": result.video_id,
        "video_url": result.video_url,
        "visibility": result.visibility,
        "error": result.error,
    }


# ─── HTML 回调页 ───────────────────────────────

def _callback_html(success: bool, platform: str, message: str) -> HTMLResponse:
    color = "#22d3ee" if success else "#ef4444"
    icon = "✓" if success else "✗"
    status = "授权成功" if success else "授权失败"
    body = f"""
    <!doctype html>
    <html lang="zh">
    <head>
      <meta charset="utf-8">
      <title>octopus · {platform} 授权</title>
      <style>
        body {{ margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
                background: #0a0a0f; color: #e5e7eb; font-family: 'Menlo','Consolas',monospace; }}
        .card {{ max-width: 460px; padding: 32px; border: 1px solid rgba(255,255,255,0.08);
                 border-radius: 12px; background: rgba(255,255,255,0.02); text-align: center; }}
        .icon {{ font-size: 48px; color: {color}; }}
        .status {{ font-size: 16px; margin-top: 12px; color: {color}; }}
        .msg {{ font-size: 13px; margin-top: 14px; color: rgba(255,255,255,0.6); line-height: 1.6; word-break: break-all; }}
        .hint {{ margin-top: 24px; font-size: 11px; color: rgba(255,255,255,0.3); }}
        a {{ color: #22d3ee; text-decoration: none; }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="icon">{icon}</div>
        <div class="status">{platform} {status}</div>
        <div class="msg">{message}</div>
        <div class="hint">可关闭此页面，回到 octopus 主界面。</div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(content=body, status_code=200 if success else 400)
