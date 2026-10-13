"""
ElevenLabs Music 模块 (0.6.2)

封装 ElevenLabs Music API 的 BGM 生成。

跟 Mubert 比的优点：
- API 极简：POST /v1/music + xi-api-key header，body 含 prompt + music_length_ms
- **直接返二进制音频**（无 task_id / 无轮询 / 无 customer 换取）
- 是真 deep-model 生成（不是 tag 池匹配），prompt 描述能力强很多

设计原则保持跟 mubert_music 一致：
- 失败不抛异常（返回 {error}），调用方决定 fallback
- 落本地到 settings.OUTPUT_DIR/audio/
- 返回 dict 结构兼容 mubert_music.generate_track，方便上游 _run_bgm_fetch 切换
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Dict, Optional

import httpx

from app.core.paths import audio_dir, to_public_url

logger = logging.getLogger(__name__)


REQUEST_TIMEOUT = 180.0  # ElevenLabs 生成 ~30-60s，留余量


async def generate_track(
    duration: int,
    mood: str,
    api_key: str,
    base_url: str,
    model_id: str = "music_v1",
    output_format: str = "mp3_44100_128",
    prompt: Optional[str] = None,
    intensity: str = "medium",
) -> Dict[str, Any]:
    """生成一段 ElevenLabs Music BGM。

    返回成功：{
        "track_id": str (uuid，ElevenLabs 不返 id，本地生成),
        "url": "" (ElevenLabs 直接给二进制不给 url),
        "local_path": str,
        "public_url": str,
        "duration": int,
        "mood": str,
        "intensity": str,
        "prompt": str,
        "provider": "elevenlabs",
    }
    返回失败：{"error": str, ...}
    """
    if not api_key:
        return {"error": "ElevenLabs API Key 未配置", "mood": mood}
    if not prompt:
        # ElevenLabs 强依赖 prompt（不像 Mubert 有 playlist_index 兜底）
        # 没 prompt 时给一个基于 mood 的兜底句
        prompt = _fallback_prompt(mood, duration)

    duration = max(3, min(600, int(duration)))  # ElevenLabs music_length_ms 3000-600000
    music_length_ms = duration * 1000
    base_url = (base_url or "").rstrip("/") or "https://api.elevenlabs.io"

    url = f"{base_url}/v1/music"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    body: Dict[str, Any] = {
        "prompt": prompt,
        "music_length_ms": music_length_ms,
        "model_id": model_id,
        "output_format": output_format,
    }
    logger.info("ElevenLabs /v1/music request body: %s", body)

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            r = await client.post(url, json=body, headers=headers)
        if r.status_code != 200:
            text = r.text[:400] if r.text else ""
            return {
                "error": f"ElevenLabs 请求失败 ({r.status_code}): {text}",
                "mood": mood, "prompt": prompt,
            }
        audio_bytes = r.content
        if not audio_bytes or len(audio_bytes) < 1024:
            return {
                "error": f"ElevenLabs 返回空音频（{len(audio_bytes)} bytes）",
                "mood": mood, "prompt": prompt,
            }
    except httpx.TimeoutException:
        return {"error": f"ElevenLabs 请求超时（{REQUEST_TIMEOUT}s）", "mood": mood, "prompt": prompt}
    except Exception as e:
        logger.exception("ElevenLabs 请求失败")
        return {"error": f"ElevenLabs 请求失败: {str(e)[:200]}", "mood": mood, "prompt": prompt}

    # 落本地
    track_id = uuid.uuid4().hex[:8]
    ext = _ext_from_format(output_format)
    local_path = os.path.join(audio_dir(), f"bgm_el_{track_id}{ext}")
    try:
        await asyncio.to_thread(_write_bytes, local_path, audio_bytes)
    except Exception as e:
        logger.exception("ElevenLabs BGM 落盘失败")
        return {"error": f"BGM 落盘失败: {str(e)[:200]}", "mood": mood, "prompt": prompt}

    return {
        "track_id": track_id,
        "url": "",
        "local_path": local_path,
        "public_url": to_public_url(local_path),
        "duration": duration,
        "mood": mood,
        "intensity": intensity,
        "prompt": prompt,
        "provider": "elevenlabs",
        # 0.6.5: 成本统计用（按秒数）
        "usage": {
            "provider": "elevenlabs",
            "model": "music_v1",
            "kind": "audio",
            "seconds": int(duration or 0),
        },
    }


def _ext_from_format(output_format: str) -> str:
    fmt = (output_format or "").lower()
    if fmt.startswith("mp3"):
        return ".mp3"
    if fmt.startswith("pcm") or fmt.startswith("wav"):
        return ".wav"
    if fmt.startswith("opus") or fmt.startswith("ogg"):
        return ".ogg"
    return ".mp3"


def _fallback_prompt(mood: str, duration: int) -> str:
    """audio_director 没给 prompt 时按 mood 兜底（保证 ElevenLabs 至少有东西可生成）"""
    m = (mood or "").lower()
    if any(k in m for k in ("温馨", "温暖", "治愈", "亲子", "家庭")):
        return "Warm acoustic folk with soft guitar and piano, gentle and uplifting, 90 BPM, for family video"
    if any(k in m for k in ("悬疑", "紧张", "惊悚")):
        return "Dark cinematic ambient with sparse piano and bass, tense and slow, 60 BPM, for suspense scene"
    if any(k in m for k in ("燃", "史诗", "激励")):
        return "Energetic orchestral with driving drums and brass, powerful and uplifting, 140 BPM, for action highlight"
    return "Calm cinematic ambient with soft piano and pads, peaceful, 80 BPM, for video background"


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
