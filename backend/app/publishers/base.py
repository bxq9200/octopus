"""PublisherBase — 发布平台的统一接口

每个平台子类需要实现：
- platform: 平台标识（"youtube" / "tiktok" / ...）
- settings_category: 在 user_settings 里用哪个 category 存凭证（"publish_youtube" / ...）
- credential_fields: 凭证字段列表（用于 SettingsModal 渲染）
- auth_url(state) -> str: 生成 OAuth 授权 URL
- exchange_code(code) -> dict: 用 code 换 token，token 存到 user_settings
- upload(video_path, metadata) -> PublishResult: 上传视频
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AuthStatus:
    """每个平台的当前授权状态 — 给前端显示用"""
    platform: str
    configured: bool          # 是否填了 client_id/secret
    authorized: bool          # 是否拿到了 access_token
    user_label: str = ""      # 已授权用户名/频道名（如果能取）
    expires_at: Optional[str] = None  # token 过期时间（ISO）
    error: Optional[str] = None


@dataclass
class PublishResult:
    """上传结果"""
    ok: bool
    platform: str
    video_id: str = ""        # 平台侧的视频 ID
    video_url: str = ""       # 平台侧 watch URL
    visibility: str = ""      # private/public/unlisted/draft
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)  # 平台返回的原始响应（调试用）


class PublisherBase(ABC):
    platform: str = ""
    settings_category: str = ""
    display_label: str = ""           # 中文显示名
    description: str = ""             # 一句话说明
    # OAuth 配置
    auth_endpoint: str = ""           # 授权端点
    token_endpoint: str = ""          # token 交换端点
    scope: str = ""                   # 需要的权限范围
    register_url: str = ""            # 开发者注册 app 的链接

    # 凭证字段（SettingsModal 用），(key, label, sensitive, hint)
    credential_fields: List[Dict[str, Any]] = []

    def __init__(self):
        from app.core.user_settings_service import user_settings
        self.user_settings = user_settings

    # ─── 凭证读写 ────────────────────────────────────────

    def _get(self, key: str, default: str = "") -> str:
        return self.user_settings.get_decrypted(self.settings_category, key, default)

    async def _set(self, key: str, value: str, *, sensitive: bool = False) -> None:
        await self.user_settings.set(self.settings_category, key, value, sensitive=sensitive)

    def client_id(self) -> str:
        return self._get("client_id")

    def client_secret(self) -> str:
        return self._get("client_secret")

    def redirect_uri(self) -> str:
        # 显式存覆盖；否则用默认（后端拼）
        return self._get("redirect_uri") or self._default_redirect_uri()

    def _default_redirect_uri(self) -> str:
        # 单用户本地部署，端口固定 8000
        return f"http://localhost:8000/api/publishers/{self.platform}/callback"

    def access_token(self) -> str:
        return self._get("access_token")

    def refresh_token(self) -> str:
        return self._get("refresh_token")

    def is_configured(self) -> bool:
        return bool(self.client_id() and self.client_secret())

    def is_authorized(self) -> bool:
        return bool(self.access_token())

    # ─── OAuth ────────────────────────────────────────

    @abstractmethod
    def auth_url(self, state: str) -> str:
        """生成跳转给用户的授权 URL"""

    @abstractmethod
    async def exchange_code(self, code: str) -> Dict[str, Any]:
        """用 authorization code 换 access_token + refresh_token，写回 user_settings"""

    async def refresh_access_token(self) -> bool:
        """用 refresh_token 刷新 access_token。子类可覆盖（不同平台 API 不一样）。
        默认实现假定符合标准 OAuth 2.0 token endpoint。返回是否刷新成功。
        """
        import logging
        _lg = logging.getLogger(__name__)
        rt = self.refresh_token()
        if not rt or not self.token_endpoint:
            _lg.warning("refresh_access_token: %s 缺 refresh_token 或 token_endpoint", self.platform)
            return False
        import httpx
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    self.token_endpoint,
                    data={
                        "client_id": self.client_id(),
                        "client_secret": self.client_secret(),
                        "refresh_token": rt,
                        "grant_type": "refresh_token",
                    },
                )
            if resp.status_code != 200:
                # 0.6.4：详细记录 Google/平台返回的失败原因（如 invalid_grant / Token revoked），方便排查
                _lg.warning(
                    "refresh_access_token: %s 失败 status=%d body=%s",
                    self.platform, resp.status_code, resp.text[:300],
                )
                return False
            data = resp.json()
            if data.get("access_token"):
                await self._set("access_token", data["access_token"], sensitive=True)
                if data.get("refresh_token"):
                    await self._set("refresh_token", data["refresh_token"], sensitive=True)
                if data.get("expires_in"):
                    import time
                    expires_at = int(time.time()) + int(data["expires_in"])
                    await self._set("token_expires_at", str(expires_at))
                return True
        except Exception as e:
            _lg.warning("refresh_access_token: %s 异常 %s", self.platform, e)
            return False
        return False

    # ─── 上传 ────────────────────────────────────────

    @abstractmethod
    async def upload(self, video_path: str, metadata: Dict[str, Any]) -> PublishResult:
        """上传视频。
        metadata: {title, description, tags, cover_path?, visibility?}
        """

    # ─── 状态 ────────────────────────────────────────

    async def auth_status(self) -> AuthStatus:
        return AuthStatus(
            platform=self.platform,
            configured=self.is_configured(),
            authorized=self.is_authorized(),
            user_label=self._get("user_label"),
            expires_at=self._get("token_expires_at"),
        )

    async def revoke(self) -> None:
        """撤销当前 token（让用户切账号 / 清理）"""
        for key in ("access_token", "refresh_token", "token_expires_at", "user_label"):
            await self.user_settings.delete(self.settings_category, key)
