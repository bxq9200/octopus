"""
UserSettingsService — 用户配置中心

目标：
1. 模块侧 **同步** 拿值（current_llm_api_key() 这种调用）—— 内存缓存兜底
2. 写入即 hot-reload —— 写 DB 同步刷新内存
3. 模块不直接 import config.settings.XXX，统一走 user_settings_service
4. 没有该 key 时，回退到 settings 的默认值（兼容旧 .env）

实现：
- 启动时从 DB 一次性 hydrate 到内存 dict
- get(category, key) 同步从内存返回
- set(...) async 写 DB + 同步更新内存
- 敏感字段加密后落 DB，内存里**也**存密文，调 get_decrypted 才解密
  （避免明文长期驻留内存）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_if_sensitive, decrypt_if_needed
from app.core.database import async_session
from app.models.user_settings import UserSetting

logger = logging.getLogger(__name__)

LOCAL_USER = "local"  # 单用户假设


class UserSettingsService:
    """单例。模块侧只读，UI / API 写。"""

    _instance: Optional["UserSettingsService"] = None

    @classmethod
    def instance(cls) -> "UserSettingsService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        # 缓存结构：{ category: { key: {"value": str, "sensitive": bool} } }
        self._cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._hydrated = False
        self._lock = asyncio.Lock()

    # ─── hydrate ───

    async def hydrate(self) -> None:
        """启动时调一次。从 DB 把所有 user_settings 灌进内存。"""
        async with self._lock:
            async with async_session() as db:
                result = await db.execute(
                    select(UserSetting).where(UserSetting.user_id == LOCAL_USER)
                )
                rows = result.scalars().all()

            new_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for row in rows:
                cat = new_cache.setdefault(row.category, {})
                cat[row.key] = {"value": row.value or "", "sensitive": bool(row.sensitive)}
            self._cache = new_cache
            self._hydrated = True
            logger.info(
                "UserSettingsService: hydrated %d categories, %d total entries",
                len(self._cache),
                sum(len(v) for v in self._cache.values()),
            )

        # 首次启动：DB 是空的且 .env 有值 → 一键迁移过来
        if not rows:
            await self._migrate_from_env()

    async def _migrate_from_env(self) -> None:
        """从 .env (settings) 一次性迁移到 user_settings。
        只有当 DB 完全没数据时才跑，跑完之后用户后续修改都走 DB。
        """
        from app.core.config import settings as env_settings
        # 延迟 import：runtime_config 依赖本模块，模块级 import 会成环
        from app.core.runtime_config import is_cloud_client, is_enterprise_key

        # 0.8 缝3：cloud_client 下企业 key 不进本地 DB（防止分发出去的包把企业凭证带下本机）
        skip_enterprise = is_cloud_client()

        plan = [
            # (category, key, env_value, sensitive)
            ("llm", "provider", (env_settings.LLM_PROVIDER or "kimi").lower(), False),
            ("llm", "kimi_api_key", env_settings.KIMI_API_KEY, True),
            ("llm", "kimi_base_url", env_settings.KIMI_BASE_URL, False),
            ("llm", "kimi_model", env_settings.KIMI_MODEL, False),
            ("llm", "openai_api_key", env_settings.OPENAI_API_KEY, True),
            ("llm", "openai_base_url", env_settings.OPENAI_BASE_URL, False),
            ("llm", "openai_model", env_settings.OPENAI_MODEL, False),
            ("image", "zhipu_api_key", env_settings.ZHIPU_API_KEY, True),
            ("image", "zhipu_base_url", env_settings.ZHIPU_BASE_URL, False),
            ("image", "zhipu_model", env_settings.ZHIPU_IMAGE_MODEL, False),
            ("image", "google_api_key", env_settings.GOOGLE_API_KEY, True),
            ("image", "google_base_url", env_settings.GEMINI_BASE_URL, False),
            ("image", "google_model", env_settings.GEMINI_IMAGE_MODEL, False),
            ("video", "ark_api_key", env_settings.SEEDANCE_API_KEY, True),
            ("video", "ark_base_url", env_settings.SEEDANCE_BASE_URL, False),
            ("video", "seedance_model", env_settings.SEEDANCE_MODEL, False),
            ("video", "seedance_fast_model", env_settings.SEEDANCE_FAST_MODEL, False),
            ("video", "ark_image_edit_model", env_settings.ARK_IMAGE_EDIT_MODEL, False),
        ]
        migrated = 0
        skipped = 0
        for category, key, value, sensitive in plan:
            if not value:
                continue
            if skip_enterprise and is_enterprise_key(category, key):
                skipped += 1
                continue
            await self.set(category, key, value, sensitive=sensitive)
            migrated += 1
        if migrated:
            logger.info("UserSettingsService: 首次启动从 .env 迁移了 %d 个配置项", migrated)
        if skipped:
            logger.info(
                "UserSettingsService: cloud_client 模式跳过 %d 个企业配置项（企业 key 只在云端）", skipped
            )

    # ─── 同步读 ───

    def get(self, category: str, key: str, default: str = "") -> str:
        """同步读 —— 模块侧用这个。敏感字段返回密文（保护进程内存）。
        要明文调 get_decrypted()。"""
        cat = self._cache.get(category)
        if not cat:
            return default
        item = cat.get(key)
        if not item:
            return default
        return item.get("value") or default

    def get_decrypted(self, category: str, key: str, default: str = "") -> str:
        """敏感字段取明文。非敏感字段直接返回值。"""
        cat = self._cache.get(category)
        if not cat:
            return default
        item = cat.get(key)
        if not item:
            return default
        stored = item.get("value") or ""
        if not stored:
            return default
        if item.get("sensitive"):
            plain = decrypt_if_needed(stored)
            return plain or default
        return stored

    def is_set(self, category: str, key: str) -> bool:
        """非空判断（适合 'API key 是否配置了' 这种检查）"""
        v = self.get(category, key)
        return bool(v and v.strip())

    def list_category(self, category: str, *, mask_sensitive: bool = True) -> Dict[str, Any]:
        """列出某个分类下所有 (key, value) — UI 渲染用。敏感字段默认遮罩。"""
        out: Dict[str, Any] = {}
        for k, item in (self._cache.get(category) or {}).items():
            if item.get("sensitive") and mask_sensitive:
                # 给前端一个"有没有值"的信号，但不下发明文
                stored = item.get("value") or ""
                out[k] = {
                    "value": "",  # 不下发
                    "has_value": bool(stored),
                    "sensitive": True,
                }
            else:
                out[k] = {
                    "value": item.get("value") or "",
                    "has_value": bool(item.get("value")),
                    "sensitive": bool(item.get("sensitive")),
                }
        return out

    def all_categories(self) -> List[str]:
        return list(self._cache.keys())

    # ─── 异步写（DB + 同步更新缓存）───

    async def set(self, category: str, key: str, value: str, *, sensitive: bool = False) -> None:
        """写值。敏感字段会加密后落库。空字符串视为"清除该项"。"""
        if value is None:
            value = ""

        async with self._lock:
            stored_value = encrypt_if_sensitive(value, sensitive) if value else ""

            async with async_session() as db:
                # upsert
                result = await db.execute(
                    select(UserSetting).where(
                        UserSetting.user_id == LOCAL_USER,
                        UserSetting.category == category,
                        UserSetting.key == key,
                    )
                )
                row = result.scalar_one_or_none()
                if row:
                    row.value = stored_value
                    row.sensitive = sensitive
                else:
                    row = UserSetting(
                        user_id=LOCAL_USER,
                        category=category,
                        key=key,
                        value=stored_value,
                        sensitive=sensitive,
                    )
                    db.add(row)
                await db.commit()

            # 同步更新内存缓存
            cat = self._cache.setdefault(category, {})
            cat[key] = {"value": stored_value, "sensitive": sensitive}

    async def set_many(self, category: str, items: Dict[str, Dict[str, Any]]) -> None:
        """批量设置一个分类下的多个字段。items: {key: {value, sensitive}}"""
        for k, item in items.items():
            await self.set(
                category, k,
                value=item.get("value", ""),
                sensitive=bool(item.get("sensitive", False)),
            )

    async def delete(self, category: str, key: str) -> None:
        async with self._lock:
            async with async_session() as db:
                await db.execute(
                    delete(UserSetting).where(
                        UserSetting.user_id == LOCAL_USER,
                        UserSetting.category == category,
                        UserSetting.key == key,
                    )
                )
                await db.commit()
            cat = self._cache.get(category)
            if cat and key in cat:
                cat.pop(key, None)

    # ─── 便捷读取：带 fallback 到 .env settings ───

    def get_with_fallback(
        self,
        category: str,
        key: str,
        env_default: str = "",
        *,
        sensitive: bool = False,
    ) -> str:
        """有 DB 值则用 DB 的；没有则回退到 env_default（settings.XXX）。
        敏感字段自动解密。
        """
        stored = self.get(category, key)
        if not stored:
            return env_default
        if sensitive:
            return decrypt_if_needed(stored) or env_default
        return stored


# 模块顶层快捷句柄，跟 settings 那种单例风格保持一致
user_settings = UserSettingsService.instance()
