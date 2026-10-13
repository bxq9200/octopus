"""
用户设置存储模型

用户级配置（API key、模型偏好、平台凭证）持久化到 SQLite。
敏感字段（key/token）通过 SettingsCrypto 加密后落盘。
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, Boolean, Integer
from app.core.database import Base


class UserSetting(Base):
    """单用户配置表 — 单用户假设，user_id 写死 'local'。

    一行 = 一个 (category, key) 对。用 EAV (entity-attribute-value) 风格而不是
    宽列表，方便加新配置项不改表结构。
    """
    __tablename__ = "user_settings"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(64), default="local", index=True)
    # 分类：llm / image / video / publish_youtube / publish_tiktok / system / ...
    category = Column(String(64), nullable=False, index=True)
    # 该类别下的具体字段名：api_key / base_url / model / refresh_token ...
    key = Column(String(128), nullable=False)
    # 值：纯文本字段；敏感字段加密后存这里（CryptoBox 自带前缀标记 "enc:v1:..."）
    value = Column(Text, nullable=True)
    # 是否敏感（用于 GET 时是否返回明文）
    sensitive = Column(Boolean, default=False)
    # 最后更新时间
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
