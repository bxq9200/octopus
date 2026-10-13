"""TikTok Publisher

TikTok 开放平台 Login Kit (OAuth 2.0) + Content Posting API。

注意事项：
- 开发者需要在 https://developers.tiktok.com/ 注册应用，
  开通 Login Kit + Content Posting API 权限
- 未审核的应用 (Sandbox) 只能让授权账号本人测试，且 PUBLISH_VIDEO 通常会被强制为 SELF_ONLY 可见
- 上传走 Upload Init → PUT 视频 → Status Query 三步异步流程
- TikTok 的 access_token 短时 (24h)，需要用 refresh_token 刷新

字段：
- client_key / client_secret   （TikTok 开发者后台的 App Key / App Secret）
- access_token / refresh_token / token_expires_at
- open_id                      （已授权用户的 TikTok open_id）
- user_label                   （昵称，显示用）
"""
from __future__ import annotations

import logging
import os
import time
import urllib.parse
from typing import Any, Dict, List

import httpx

from app.publishers.base import PublisherBase, PublishResult

logger = logging.getLogger(__name__)


class TikTokPublisher(PublisherBase):
    platform = "tiktok"
    settings_category = "publish_tiktok"
    display_label = "TikTok"
    description = "上传到 TikTok（需在 TikTok for Developers 注册 App + 开通 Content Posting API）"

    auth_endpoint = "https://www.tiktok.com/v2/auth/authorize/"
    token_endpoint = "https://open.tiktokapis.com/v2/oauth/token/"
    scope = "user.info.basic,video.publish,video.upload"
    register_url = "https://developers.tiktok.com/apps"

    credential_fields: List[Dict[str, Any]] = [
        {"key": "client_id", "label": "Client Key", "sensitive": False,
         "placeholder": "awxxxxxxxxxxxxxx",
         "hint": "TikTok 开发者后台 → 应用 → Client Key"},
        {"key": "client_secret", "label": "Client Secret", "sensitive": True,
         "hint": "与 Client Key 同时获取"},
        {"key": "redirect_uri", "label": "Redirect URI（可选）",
         "placeholder": "http://localhost:8000/api/publishers/tiktok/callback",
         "hint": "必须与开发者后台注册的 Redirect URI 完全一致"},
    ]

    # ─── OAuth ─────────────────────────────────────────────

    def auth_url(self, state: str) -> str:
        params = {
            "client_key": self.client_id(),
            "scope": self.scope,
            "response_type": "code",
            "redirect_uri": self.redirect_uri(),
            "state": state,
        }
        return f"{self.auth_endpoint}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.token_endpoint,
                data={
                    "client_key": self.client_id(),
                    "client_secret": self.client_secret(),
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.redirect_uri(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"TikTok token 交换失败 {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        # TikTok 的响应是 { access_token, refresh_token, open_id, expires_in, scope, token_type }
        if "error" in data:
            raise RuntimeError(f"TikTok OAuth 错误: {data.get('error_description') or data}")
        if not data.get("access_token"):
            raise RuntimeError(f"TikTok 未返回 access_token: {data}")
        await self._set("access_token", data["access_token"], sensitive=True)
        if data.get("refresh_token"):
            await self._set("refresh_token", data["refresh_token"], sensitive=True)
        if data.get("expires_in"):
            await self._set("token_expires_at", str(int(time.time()) + int(data["expires_in"])))
        if data.get("open_id"):
            await self._set("open_id", data["open_id"])
        # 取昵称做 user_label
        try:
            label = await self._fetch_user_label(data["access_token"])
            if label:
                await self._set("user_label", label)
        except Exception as e:
            logger.warning("拉 TikTok 用户名失败: %s", e)
        return {"ok": True, "user_label": self._get("user_label"), "open_id": data.get("open_id")}

    async def _fetch_user_label(self, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://open.tiktokapis.com/v2/user/info/",
                params={"fields": "display_name"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        return ((data.get("data") or {}).get("user") or {}).get("display_name", "") or ""

    async def refresh_access_token(self) -> bool:
        """TikTok 用 client_key 而不是 client_id 字段名"""
        rt = self.refresh_token()
        if not rt:
            logger.warning("TikTok refresh_access_token: 无 refresh_token")
            return False
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(self.token_endpoint, data={
                    "client_key": self.client_id(),
                    "client_secret": self.client_secret(),
                    "refresh_token": rt,
                    "grant_type": "refresh_token",
                })
            if resp.status_code != 200:
                # 0.6.4：记录 TikTok 返回的真实失败原因（如 invalid_grant / refresh_token expired）
                logger.warning(
                    "TikTok refresh_access_token 失败 status=%d body=%s",
                    resp.status_code, resp.text[:300],
                )
                return False
            data = resp.json()
            if not data.get("access_token"):
                logger.warning("TikTok refresh: 无 access_token body=%s", str(data)[:300])
                return False
            await self._set("access_token", data["access_token"], sensitive=True)
            if data.get("refresh_token"):
                await self._set("refresh_token", data["refresh_token"], sensitive=True)
            if data.get("expires_in"):
                await self._set("token_expires_at", str(int(time.time()) + int(data["expires_in"])))
            return True
        except Exception as e:
            logger.warning("TikTok refresh 异常: %s", e)
            return False

    async def _ensure_token(self) -> str:
        """0.6.4：refresh 失败时不再返回 stale token 让 upload 跑 401，直接 raise 友好提示。"""
        token = self.access_token()
        if not token:
            raise RuntimeError("尚未授权 TikTok，请先在系统设置 → 发布：TikTok → 点'连接 TikTok'")
        exp_str = self._get("token_expires_at")
        if exp_str:
            try:
                exp = int(exp_str)
                if exp - time.time() < 60:
                    if await self.refresh_access_token():
                        token = self.access_token()
                    else:
                        raise RuntimeError(
                            "TikTok 凭证已过期，且自动刷新失败（refresh_token 被撤销）。"
                            "去系统设置 → 发布：TikTok → 重新点'连接 TikTok'授权。"
                            "提示：TikTok refresh_token 一般 365 天有效，但 Sandbox 模式 / 用户撤销授权 / 应用 secret 改了 都会让它失效。"
                        )
            except ValueError:
                pass
        return token

    # ─── 上传（Content Posting API: PULL_FROM_URL 不可用本地视频，必须 FILE_UPLOAD 走 PUT）
    # 流程：
    #   1. POST /v2/post/publish/inbox/video/init/  拿 publish_id + upload_url
    #   2. PUT upload_url 上传视频字节
    #   3. （可选）轮询 /v2/post/publish/status/fetch/  看 publish_id 状态
    # ─────────────────────────────────────────────

    async def upload(self, video_path: str, metadata: Dict[str, Any]) -> PublishResult:
        if not os.path.exists(video_path):
            return PublishResult(ok=False, platform=self.platform, error=f"视频文件不存在: {video_path}")
        try:
            token = await self._ensure_token()
        except Exception as e:
            return PublishResult(ok=False, platform=self.platform, error=str(e))

        file_size = os.path.getsize(video_path)
        # TikTok 上传分片：< 64MB 直接整传，>= 64MB 要分片（这里先支持整传）
        if file_size > 64 * 1024 * 1024:
            return PublishResult(
                ok=False, platform=self.platform,
                error=f"TikTok 当前实现仅支持 64MB 以内视频（当前 {file_size/1024/1024:.1f}MB），分片上传后续支持",
            )

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                # 1. init upload
                init_resp = await client.post(
                    "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=UTF-8",
                    },
                    json={
                        "source_info": {
                            "source": "FILE_UPLOAD",
                            "video_size": file_size,
                            "chunk_size": file_size,
                            "total_chunk_count": 1,
                        }
                    },
                )
                if init_resp.status_code != 200:
                    return PublishResult(
                        ok=False, platform=self.platform,
                        error=f"TikTok init 失败 {init_resp.status_code}: {init_resp.text[:300]}",
                    )
                init_data = init_resp.json()
                err = (init_data.get("error") or {}).get("code")
                if err and err != "ok":
                    return PublishResult(
                        ok=False, platform=self.platform,
                        error=f"TikTok init 业务失败: {init_data.get('error', {}).get('message', err)}",
                        raw=init_data,
                    )
                payload = init_data.get("data") or {}
                publish_id = payload.get("publish_id", "")
                upload_url = payload.get("upload_url", "")
                if not upload_url:
                    return PublishResult(
                        ok=False, platform=self.platform,
                        error="TikTok 未返回 upload_url", raw=init_data,
                    )

                # 2. PUT 视频
                with open(video_path, "rb") as f:
                    put_resp = await client.put(
                        upload_url,
                        content=f.read(),
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Range": f"bytes 0-{file_size-1}/{file_size}",
                        },
                    )
                if put_resp.status_code not in (200, 201):
                    return PublishResult(
                        ok=False, platform=self.platform,
                        error=f"TikTok PUT 失败 {put_resp.status_code}: {put_resp.text[:300]}",
                        raw={"publish_id": publish_id},
                    )

            return PublishResult(
                ok=True, platform=self.platform,
                video_id=publish_id,
                video_url="",  # TikTok 没有立刻可访问的 URL，需要进入 inbox 编辑
                visibility="draft",  # 进入用户草稿箱（inbox）
                raw={"publish_id": publish_id, "note": "已进入 TikTok 收件箱，请在 App 内完成发布"},
            )
        except Exception as e:
            return PublishResult(ok=False, platform=self.platform, error=f"TikTok 上传异常: {e}")
