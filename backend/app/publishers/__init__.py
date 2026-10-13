"""发布平台 adapter — 把成片视频上传到 YouTube / TikTok 等

设计：
- 每个平台一个 publisher 类，继承 PublisherBase
- 凭证（client_id/secret + OAuth token）通过 user_settings 存（加密）
- 上传流程：先 OAuth 拿 token → 用 token 上传 → 写回新 token（refresh）

不绑死 SDK：用 httpx 走 raw OAuth + REST，避免 google-api-python-client 这种重依赖。
"""
from app.publishers.base import PublisherBase, PublishResult, AuthStatus
from app.publishers.registry import get_publisher, list_publishers, available_platforms

__all__ = [
    "PublisherBase",
    "PublishResult",
    "AuthStatus",
    "get_publisher",
    "list_publishers",
    "available_platforms",
]
