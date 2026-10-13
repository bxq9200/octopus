"""YouTube Publisher

OAuth 2.0 + YouTube Data API v3 的 videos.insert 上传。

注意事项：
- 开发者需要在 Google Cloud Console 注册 OAuth 客户端 (Web application 类型)，
  并把 http://localhost:8000/api/publishers/youtube/callback 加进 Authorized redirect URIs
- 未通过 Google App Verification 的 OAuth 客户端，上传的视频会被强制设为 private（即使我们请求 public）
- 上传走 resumable upload protocol，单文件即 PUT 一次足够

字段：
- client_id / client_secret    （开发者注册 OAuth app 时拿到）
- access_token / refresh_token / token_expires_at （用户授权后拿到）
- user_label                   （已授权的 YouTube 频道名，仅显示用）
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from typing import Any, Dict, List

import httpx

from app.publishers.base import PublisherBase, PublishResult

logger = logging.getLogger(__name__)


class YouTubePublisher(PublisherBase):
    platform = "youtube"
    settings_category = "publish_youtube"
    display_label = "YouTube"
    description = "上传到 YouTube（首次上传需在 Google Cloud Console 注册 OAuth App）"

    auth_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    scope = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
    register_url = "https://console.cloud.google.com/apis/credentials"

    credential_fields: List[Dict[str, Any]] = [
        {"key": "client_id", "label": "OAuth Client ID", "sensitive": False,
         "placeholder": "xxxx.apps.googleusercontent.com",
         "hint": "Google Cloud Console → 凭据 → 创建 OAuth 客户端 ID (Web 类型)"},
        {"key": "client_secret", "label": "OAuth Client Secret", "sensitive": True,
         "placeholder": "GOCSPX-...",
         "hint": "与 client_id 同时获取"},
        {"key": "redirect_uri", "label": "Redirect URI（可选）",
         "placeholder": "http://localhost:8000/api/publishers/youtube/callback",
         "hint": "留空使用默认；必须与 Google Cloud Console 注册的完全一致"},
    ]

    # ─── OAuth ─────────────────────────────────────────────

    def auth_url(self, state: str) -> str:
        params = {
            "client_id": self.client_id(),
            "redirect_uri": self.redirect_uri(),
            "response_type": "code",
            "scope": self.scope,
            "access_type": "offline",   # 拿 refresh_token
            "prompt": "consent",        # 强制再次同意，保证拿到 refresh_token
            "state": state,
        }
        return f"{self.auth_endpoint}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self.token_endpoint, data={
                "code": code,
                "client_id": self.client_id(),
                "client_secret": self.client_secret(),
                "redirect_uri": self.redirect_uri(),
                "grant_type": "authorization_code",
            })
        if resp.status_code != 200:
            raise RuntimeError(f"YouTube token 交换失败: {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        await self._set("access_token", data["access_token"], sensitive=True)
        if data.get("refresh_token"):
            await self._set("refresh_token", data["refresh_token"], sensitive=True)
        if data.get("expires_in"):
            await self._set("token_expires_at", str(int(time.time()) + int(data["expires_in"])))
        # 取一下频道名做 user_label
        try:
            label = await self._fetch_channel_label(data["access_token"])
            if label:
                await self._set("user_label", label)
        except Exception as e:
            logger.warning("拉 YouTube 频道名失败: %s", e)
        return {"ok": True, "user_label": self._get("user_label")}

    async def _fetch_channel_label(self, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code != 200:
            return ""
        items = resp.json().get("items") or []
        if not items:
            return ""
        return items[0].get("snippet", {}).get("title", "")

    async def _ensure_token(self) -> str:
        """确保 access_token 可用，过期就自动 refresh。返回当前 access_token。

        0.6.4：refresh 失败时直接 raise（不再返回 stale token 让 upload 跑 401）。
        典型 refresh 失败原因：
        - Google OAuth App 未通过 Verification，refresh_token 7 天自动失效
        - 用户在 Google 账号 → 安全 → 第三方权限里撤销了授权
        - 超过 6 个月未使用
        """
        token = self.access_token()
        if not token:
            raise RuntimeError("尚未授权 YouTube，请先在系统设置 → 发布：YouTube → 点'连接 YouTube'")
        exp_str = self._get("token_expires_at")
        if exp_str:
            try:
                exp = int(exp_str)
                # 60s 内将过期 → 提前刷新
                if exp - time.time() < 60:
                    if await self.refresh_access_token():
                        token = self.access_token()
                    else:
                        raise RuntimeError(
                            "YouTube 凭证已过期，且自动刷新失败（refresh_token 被 Google 撤销）。"
                            "去系统设置 → 发布：YouTube → 重新点'连接 YouTube'授权。"
                            "提示：未审核的 OAuth App refresh_token 7 天自动失效，"
                            "把你的 Google 账号加到 Cloud Console → OAuth consent screen → Test users 能延长有效期。"
                        )
            except ValueError:
                pass
        return token

    # ─── 上传 ─────────────────────────────────────────────

    async def upload(self, video_path: str, metadata: Dict[str, Any]) -> PublishResult:
        if not os.path.exists(video_path):
            return PublishResult(ok=False, platform=self.platform, error=f"视频文件不存在: {video_path}")
        try:
            token = await self._ensure_token()
        except Exception as e:
            return PublishResult(ok=False, platform=self.platform, error=str(e))

        title = (metadata.get("title") or "Untitled").strip()[:100]
        description = (metadata.get("description") or "").strip()[:5000]
        tags = metadata.get("tags") or []
        if isinstance(tags, list):
            tags = [str(t)[:30] for t in tags][:30]
        else:
            tags = []
        visibility = (metadata.get("visibility") or "private").lower()
        if visibility not in ("private", "unlisted", "public"):
            visibility = "private"

        snippet = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": "22",  # People & Blogs；YouTube 要求必填
            },
            "status": {
                "privacyStatus": visibility,
                "selfDeclaredMadeForKids": False,
            },
        }

        try:
            file_size = os.path.getsize(video_path)
            async with httpx.AsyncClient(timeout=None) as client:
                # 1. 初始化 resumable session
                init_resp = await client.post(
                    "https://www.googleapis.com/upload/youtube/v3/videos",
                    params={"uploadType": "resumable", "part": "snippet,status"},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=UTF-8",
                        "X-Upload-Content-Length": str(file_size),
                        "X-Upload-Content-Type": "video/*",
                    },
                    content=json.dumps(snippet).encode("utf-8"),
                )
                if init_resp.status_code not in (200, 201):
                    return PublishResult(
                        ok=False, platform=self.platform,
                        error=f"YouTube 初始化上传失败 {init_resp.status_code}: {init_resp.text[:300]}",
                    )
                upload_url = init_resp.headers.get("Location")
                if not upload_url:
                    return PublishResult(
                        ok=False, platform=self.platform,
                        error="YouTube 未返回 Location upload URL",
                        raw={"init_response": init_resp.text[:500]},
                    )

                # 2. PUT 视频内容（一次性上传，大文件其实应该 chunked，这里先简化）
                with open(video_path, "rb") as f:
                    put_resp = await client.put(
                        upload_url,
                        content=f.read(),
                        headers={"Content-Type": "video/*"},
                    )
            if put_resp.status_code not in (200, 201):
                return PublishResult(
                    ok=False, platform=self.platform,
                    error=f"YouTube 上传失败 {put_resp.status_code}: {put_resp.text[:300]}",
                )
            data = put_resp.json()
            video_id = data.get("id", "")
            return PublishResult(
                ok=True, platform=self.platform,
                video_id=video_id,
                video_url=f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
                visibility=visibility,
                raw=data,
            )
        except Exception as e:
            return PublishResult(ok=False, platform=self.platform, error=f"YouTube 上传异常: {e}")
