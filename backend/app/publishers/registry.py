"""Publisher 注册中心 — 按 platform 字符串拿对应 publisher 实例"""
from typing import Dict, List, Optional, Type

from app.publishers.base import PublisherBase
from app.publishers.youtube import YouTubePublisher
from app.publishers.tiktok import TikTokPublisher


_REGISTRY: Dict[str, Type[PublisherBase]] = {
    YouTubePublisher.platform: YouTubePublisher,
    TikTokPublisher.platform: TikTokPublisher,
}


def get_publisher(platform: str) -> Optional[PublisherBase]:
    cls = _REGISTRY.get(platform)
    if not cls:
        return None
    return cls()


def list_publishers() -> List[PublisherBase]:
    return [cls() for cls in _REGISTRY.values()]


def available_platforms() -> List[str]:
    return list(_REGISTRY.keys())
