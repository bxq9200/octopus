"""
Mubert 音乐生成模块（0.6.1）

封装 Mubert API 的 BGM 生成 + 异步轮询 + 落本地。

Mubert 认证两步走：
1. 公司级凭证（company_id + license_token，邮件给的）
2. 终端用户凭证（customer_id + access_token），用公司凭证调 POST /service/customers 换出来
3. 实际生成调用走 POST /public/tracks，header 用 customer-id + access-token

本模块对外接口 generate_track 接收 company_id + license_token；
内部 _ensure_customer 透明处理凭证换取与缓存（落 user_settings 加密存）。

设计原则：
- 失败不抛异常（返回 {error}）；调用方判断后决定是否 fallback 到无 BGM
- 401 自动 invalidate 缓存重试一次（应对 customer token 过期 / 缓存陈旧）
- 完整下载 mp3 到 OUTPUT_DIR/audio/ 后返回本地 + /output/... URL
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from app.core.paths import audio_dir, to_public_url
from app.core.runtime_config import (
    _CACHE_ACCESS_TOKEN_KEY,
    _CACHE_CUSTOMER_ID_KEY,
    _CACHE_EXPIRES_AT_KEY,
)
from app.core.user_settings_service import user_settings

logger = logging.getLogger(__name__)


# ─── mood → Mubert playlist_index 映射 ───
MOOD_TO_PLAYLIST: Dict[str, str] = {
    "ambient":        "1.0.0",
    "cinematic":      "0.0.2",
    "default":        "1.0.0",
    "mystery":        "0.4.0",
    "tense":          "0.4.1",
    "calm":           "1.0.0",
    "uplifting":      "2.1.0",
    "epic":           "2.1.0",
    "sad":            "1.2.0",
    "happy":          "2.0.0",
    "warm":           "1.1.0",
    "playful":        "2.0.0",
    "build_up":       "2.3.0",
    "drop":           "2.3.1",
    "release":        "1.0.0",
    "outro":          "1.0.0",
    "lofi":           "1.3.0",
    "electronic":     "2.2.0",
    "orchestral":     "0.0.2",
    "ethnic":         "1.4.0",
}

DEFAULT_PLAYLIST = "1.0.0"
POLL_INTERVAL = 5.0
POLL_TIMEOUT = 180.0
DOWNLOAD_TIMEOUT = 60.0

# 单用户应用，所有调用复用同一个 custom_id（Mubert 用它去 dedup 同一个终端用户）
LOCAL_CUSTOM_ID = "octopus_local"


def resolve_playlist_index(mood: str, hint: Optional[str] = None) -> str:
    """优先用 LLM 给的 playlist_index hint；否则按 mood 查表；都没有用 default。"""
    if hint:
        h = str(hint).strip()
        if all(p.isdigit() for p in h.split(".")):
            return h
    key = (mood or "").strip().lower()
    return MOOD_TO_PLAYLIST.get(key, DEFAULT_PLAYLIST)


# ═══════════════════════════════════════════════════════
# Customer 凭证换取 + 缓存
# ═══════════════════════════════════════════════════════

async def _ensure_customer(
    company_id: str,
    license_token: str,
    base_url: str,
    force_refresh: bool = False,
) -> tuple[str, str]:
    """获取有效的 (customer_id, access_token) 对。

    顺序：
    1. 非 force_refresh 时优先读 user_settings 缓存（含简单过期检查）
    2. 缓存不可用 / force_refresh → POST /service/customers 换新凭证 → 写回缓存

    任何步骤失败抛异常（调用方 catch）。
    """
    if not force_refresh:
        cached_id = user_settings.get_decrypted("audio", _CACHE_CUSTOMER_ID_KEY)
        cached_token = user_settings.get_decrypted("audio", _CACHE_ACCESS_TOKEN_KEY)
        cached_exp = user_settings.get("audio", _CACHE_EXPIRES_AT_KEY)
        if cached_id and cached_token and not _is_expired(cached_exp):
            return cached_id, cached_token

    # 换凭证
    url = f"{base_url.rstrip('/')}/service/customers"
    headers = {
        "company-id": company_id,
        "license-token": license_token,
        "Content-Type": "application/json",
    }
    body = {"custom_id": LOCAL_CUSTOM_ID}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=body, headers=headers)
        r.raise_for_status()
        payload = r.json() or {}

    data = payload.get("data") or {}
    access = data.get("access") or {}
    customer_id = str(access.get("customer_id") or "")
    token = str(access.get("token") or "")
    expires_at = str(access.get("expired_at") or "")
    if not customer_id or not token:
        raise RuntimeError(f"Mubert /customers 响应缺字段（data.access.customer_id / token）: {payload}")

    # 写缓存（敏感字段加密）
    await user_settings.set("audio", _CACHE_CUSTOMER_ID_KEY, customer_id, sensitive=True)
    await user_settings.set("audio", _CACHE_ACCESS_TOKEN_KEY, token, sensitive=True)
    await user_settings.set("audio", _CACHE_EXPIRES_AT_KEY, expires_at, sensitive=False)

    return customer_id, token


def _is_expired(expires_at: str) -> bool:
    """expires_at 形如 "2025-02-01T00:00:00.000000Z"；解析失败/为空 → 视为已过期（保守）"""
    if not expires_at:
        return False  # 没有过期信息，假设还有效（避免每次都换）
    try:
        # 兼容带 Z 和带 +00:00 两种
        s = expires_at.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        exp = datetime.fromisoformat(s)
        return exp <= datetime.now(timezone.utc)
    except Exception:
        return False  # 解析失败也视为有效（避免无限刷新）


async def _invalidate_customer_cache() -> None:
    await user_settings.delete("audio", _CACHE_CUSTOMER_ID_KEY)
    await user_settings.delete("audio", _CACHE_ACCESS_TOKEN_KEY)
    await user_settings.delete("audio", _CACHE_EXPIRES_AT_KEY)


# ═══════════════════════════════════════════════════════
# Track 生成
# ═══════════════════════════════════════════════════════

async def generate_track(
    duration: int,
    mood: str,
    company_id: str,
    license_token: str,
    base_url: str,
    playlist_index_hint: Optional[str] = None,
    prompt: Optional[str] = None,
    intensity: str = "medium",
) -> Dict[str, Any]:
    """生成一段 Mubert BGM。

    优先使用 prompt（text-to-music，最贴合剧情）；缺失时回落 playlist_index。

    返回成功：{
        "track_id": str, "url": str, "local_path": str, "public_url": str,
        "duration": int, "mood": str, "playlist_index": str, "prompt": str,
    }
    返回失败：{"error": str, ...}
    """
    if not company_id or not license_token:
        return {"error": "Mubert company_id / license_token 未配置", "mood": mood}

    duration = max(5, min(1500, int(duration)))
    if intensity not in ("low", "medium", "high"):
        intensity = "medium"
    prompt = (prompt or "").strip()
    if len(prompt) > 200:
        prompt = prompt[:200].rstrip()
    playlist_index = resolve_playlist_index(mood, playlist_index_hint)
    base_url = (base_url or "").rstrip("/") or "https://music-api.mubert.com/api/v3"

    # Step 1: 拿 customer 凭证（缓存优先）
    try:
        customer_id, access_token = await _ensure_customer(company_id, license_token, base_url)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        text = e.response.text[:300] if e.response is not None else ""
        return {"error": f"Mubert 换 customer 凭证失败 ({status}): {text}",
                "mood": mood, "playlist_index": playlist_index}
    except Exception as e:
        logger.exception("Mubert _ensure_customer 失败")
        return {"error": f"Mubert 换 customer 凭证失败: {str(e)[:200]}",
                "mood": mood, "playlist_index": playlist_index}

    # Step 2: 请求 /tracks；若 401 自动 invalidate 缓存重试一次
    headers = _track_headers(customer_id, access_token)
    try:
        track_id, immediate_url = await _request_track(
            base_url, headers, playlist_index, duration,
            prompt=prompt, intensity=intensity,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            logger.warning("Mubert /tracks 401，invalidate 缓存并重试一次")
            await _invalidate_customer_cache()
            try:
                customer_id, access_token = await _ensure_customer(
                    company_id, license_token, base_url, force_refresh=True,
                )
                headers = _track_headers(customer_id, access_token)
                track_id, immediate_url = await _request_track(
                    base_url, headers, playlist_index, duration,
                    prompt=prompt, intensity=intensity,
                )
            except Exception as ee:
                logger.exception("Mubert /tracks 重试仍失败")
                return {"error": f"Mubert 请求失败（重试 401）: {str(ee)[:200]}",
                        "mood": mood, "playlist_index": playlist_index, "prompt": prompt}
        else:
            text = e.response.text[:300] if e.response is not None else ""
            return {"error": f"Mubert 请求失败 ({e.response.status_code}): {text}",
                    "mood": mood, "playlist_index": playlist_index, "prompt": prompt}
    except Exception as e:
        logger.exception("Mubert 请求失败")
        return {"error": f"Mubert 请求失败: {str(e)[:200]}",
                "mood": mood, "playlist_index": playlist_index, "prompt": prompt}

    # Step 3: 轮询直到生成完成
    track_url = immediate_url
    if not track_url:
        try:
            track_url = await _poll_track(base_url, headers, track_id)
        except asyncio.TimeoutError:
            return {"error": f"Mubert 轮询超时（{POLL_TIMEOUT}s）",
                    "mood": mood, "playlist_index": playlist_index, "track_id": track_id}
        except Exception as e:
            logger.exception("Mubert 轮询失败")
            return {"error": f"Mubert 轮询失败: {str(e)[:200]}",
                    "mood": mood, "playlist_index": playlist_index, "track_id": track_id}

    if not track_url:
        return {"error": "Mubert 返回空 URL（生成失败或被审核）",
                "mood": mood, "playlist_index": playlist_index, "track_id": track_id}

    # Step 4: 下载到本地
    try:
        local_path = await _download(track_url, track_id)
    except Exception as e:
        logger.exception("Mubert BGM 下载失败")
        return {"error": f"BGM 下载失败: {str(e)[:200]}", "url": track_url,
                "mood": mood, "playlist_index": playlist_index, "track_id": track_id}

    return {
        "track_id": track_id,
        "url": track_url,
        "local_path": local_path,
        "public_url": to_public_url(local_path),
        "duration": duration,
        "mood": mood,
        "intensity": intensity,
        # 实际用于生成的 prompt（text-to-music）；playlist_index 仅当无 prompt 时回退使用
        "prompt": prompt,
        "playlist_index": playlist_index if not prompt else "",
        "provider": "mubert",
        # 0.6.5: 成本统计用
        "usage": {
            "provider": "mubert",
            "model": "default",
            "kind": "audio",
            "seconds": int(duration or 0),
        },
    }


def _track_headers(customer_id: str, access_token: str) -> Dict[str, str]:
    return {
        "customer-id": customer_id,
        "access-token": access_token,
        "Content-Type": "application/json",
    }


async def _request_track(
    base_url: str,
    headers: Dict[str, str],
    playlist_index: str,
    duration: int,
    prompt: str = "",
    intensity: str = "medium",
) -> tuple[str, Optional[str]]:
    """POST /public/tracks 请求生成。返回 (track_id, immediate_url_if_any)。

    优先用 prompt（text-to-music，最贴合剧情）；空 prompt 时回落 playlist_index。
    """
    url = f"{base_url}/public/tracks"
    body: Dict[str, Any] = {
        "duration": duration,
        "bitrate": 128,
        "format": "mp3",
        "intensity": intensity,
        "mode": "track",
    }
    if prompt:
        body["prompt"] = prompt
    else:
        body["playlist_index"] = playlist_index
    # 完整记录给 Mubert 的实际 body（不截断），方便排查 LLM 给的 prompt 到底什么
    logger.info("Mubert /tracks request body: %s", body)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=body, headers=headers)
        r.raise_for_status()
        payload = r.json() or {}
    data = payload.get("data") or {}
    track_id = str(data.get("id") or "")
    if not track_id:
        raise RuntimeError(f"Mubert response 缺 data.id: {payload}")
    gens = data.get("generations") or []
    immediate = ""
    if gens and isinstance(gens, list):
        g0 = gens[0] or {}
        if (g0.get("status") or "").lower() == "done":
            immediate = g0.get("url") or ""
    return track_id, immediate or None


async def _poll_track(
    base_url: str,
    headers: Dict[str, str],
    track_id: str,
) -> str:
    """GET /public/tracks/{id} 轮询直到 generations[0].status == done。"""
    url = f"{base_url}/public/tracks/{track_id}"
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                payload = r.json() or {}
                data = payload.get("data") or {}
                gens = data.get("generations") or []
                if gens:
                    g0 = gens[0] or {}
                    status = (g0.get("status") or "").lower()
                    if status == "done":
                        return g0.get("url") or ""
                    if status in ("failed", "error", "rejected"):
                        raise RuntimeError(f"Mubert generation {status}: {g0}")
            elif r.status_code == 404:
                pass  # 偶尔需要稍等才能 GET 到
            else:
                logger.warning("Mubert poll %s 非 200: %s", url, r.status_code)
            if asyncio.get_event_loop().time() >= deadline:
                raise asyncio.TimeoutError()
            await asyncio.sleep(POLL_INTERVAL)


async def _download(track_url: str, track_id: str) -> str:
    target = os.path.join(audio_dir(), f"bgm_{(track_id or uuid.uuid4().hex)[:8]}.mp3")
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        r = await client.get(track_url)
        r.raise_for_status()
        await asyncio.to_thread(_write_bytes, target, r.content)
    return target


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
