"""DashScope 视频生成（阿里云 HappyHorse / Wanx）

提交流程：
1. POST {base_url}/services/aigc/video-generation/video-synthesis
   Headers: Authorization, X-DashScope-Async: enable, Content-Type: application/json
   Body: {"model", "input": {"img_url", "prompt"}, "parameters": {...}}
   → output.task_id

2. 轮询 GET {base_url}/tasks/{task_id}
   → output.task_status ∈ {PENDING, RUNNING, SUCCEEDED, FAILED, CANCELED, UNKNOWN}
   → output.video_url（或 results[0].video_url，按响应实际形态兜底）

3. 下载视频到 /output/videos/

图片传输策略：
- 远程 URL 直接用
- 本地图片转 base64 data URL（DashScope 多数模型接受 data URL；不接受时改走 /api/v1/uploads 公网上传）
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Dict, Optional

import httpx

from app.core.paths import videos_dir, image_to_data_url

logger = logging.getLogger(__name__)

# 与 ark 一样的提交并发节流（同 key 多任务洪水会被限流）
DASHSCOPE_SUBMIT_SEMAPHORE = asyncio.Semaphore(2)


def _pick_video_url(output: Dict[str, Any]) -> Optional[str]:
    """兼容几种可能的响应结构。"""
    if not output:
        return None
    if output.get("video_url"):
        return output["video_url"]
    results = output.get("results")
    if isinstance(results, dict) and results.get("video_url"):
        return results["video_url"]
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            return first.get("video_url") or first.get("url")
    return None


async def _emit(progress_cb, progress: float, message: str) -> None:
    if not progress_cb:
        return
    try:
        r = progress_cb(progress, message)
        if asyncio.iscoroutine(r):
            await r
    except Exception:
        pass


async def generate_image_to_video(
    image_url: str,
    prompt: str,
    *,
    duration: int = 5,
    resolution: str = "720x1280",
    seed: int = -1,
    progress_cb=None,
) -> Dict[str, Any]:
    """图生视频（happyhorse-1.0-i2v / wanx i2v）"""
    from app.core.runtime_config import dashscope_video
    cfg = dashscope_video()
    if not cfg.api_key:
        raise ValueError("未配置 DashScope API Key")

    base_url = cfg.base_url.rstrip("/")
    submit_endpoint = f"{base_url}/services/aigc/video-generation/video-synthesis"

    img_payload = await image_to_data_url(image_url)

    # 标准化 resolution: 720x1280 / 1280x720 → 720P；含 1080 → 1080P
    res_norm = "1080P" if "1080" in resolution else "720P"
    clamped_duration = max(3, min(15, int(duration) or 5))

    payload: Dict[str, Any] = {
        "model": cfg.i2v_model,
        "input": {
            "img_url": img_payload,
            "prompt": prompt or "",
        },
        "parameters": {
            "duration": clamped_duration,
            "resolution": res_norm,
        },
    }
    if seed and seed > 0:
        payload["parameters"]["seed"] = int(seed)

    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "X-DashScope-Async": "enable",
        "Content-Type": "application/json",
    }

    # 1. 提交任务
    async with DASHSCOPE_SUBMIT_SEMAPHORE:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(submit_endpoint, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise ValueError(
                    f"DashScope 提交失败 {resp.status_code}: {resp.text[:500]} | "
                    f"model={payload['model']} resolution={res_norm} duration={clamped_duration}"
                )
            data = resp.json()

    output0 = data.get("output") or {}
    task_id = output0.get("task_id")
    if not task_id:
        raise ValueError(f"DashScope 未返回 task_id: {data}")

    await _emit(progress_cb, 2.0, "DashScope 任务已提交，等待排队...")

    # 2. 轮询任务
    poll_endpoint = f"{base_url}/tasks/{task_id}"
    auth_only = {"Authorization": f"Bearer {cfg.api_key}"}
    max_attempts, interval = 180, 5.0
    video_remote_url: Optional[str] = None
    last_status = ""

    async with httpx.AsyncClient(timeout=30.0) as client:
        for i in range(max_attempts):
            r = await client.get(poll_endpoint, headers=auth_only)
            r.raise_for_status()
            d = r.json()
            out = d.get("output") or {}
            status = (out.get("task_status") or "").upper()
            last_status = status

            if status == "SUCCEEDED":
                video_remote_url = _pick_video_url(out)
                if not video_remote_url:
                    raise ValueError(f"DashScope SUCCEEDED 但未找到 video_url: {d}")
                await _emit(progress_cb, 100.0, "任务完成，正在下载视频...")
                break
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise ValueError(
                    f"DashScope 任务失败: status={status}, code={out.get('code')}, "
                    f"message={out.get('message')}"
                )

            if status == "PENDING":
                progress = min(15.0, 2.0 + i * 1.0)
                msg = f"排队中 ({i+1} 次轮询)"
            else:  # RUNNING / 其它
                progress = min(90.0, 15.0 + (i / max_attempts) * 75.0)
                msg = f"{status or '生成中'} ({i+1} 次轮询)"
            await _emit(progress_cb, progress, msg)
            await asyncio.sleep(interval)

    if not video_remote_url:
        raise TimeoutError(f"DashScope 轮询超时 (task_id={task_id}, last_status={last_status})")

    # 3. 下载视频
    output_path = os.path.join(videos_dir(), f"happyhorse_{uuid.uuid4().hex[:8]}.mp4")
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.get(video_remote_url)
        r.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(r.content)

    return {
        "video_url": f"/output/videos/{os.path.basename(output_path)}",
        "task_id": task_id,
        "duration": clamped_duration,
        "resolution": resolution,
        "prompt": prompt,
        "mock": False,
        "model": cfg.i2v_model,
        "provider": "dashscope",
    }


async def generate_text_to_video(
    prompt: str,
    *,
    duration: int = 5,
    resolution: str = "720x1280",
    seed: int = -1,
    progress_cb=None,
) -> Dict[str, Any]:
    """文生视频（如果 dashscope_t2v_model 已配置）— 同样的异步流程，仅去掉 img_url"""
    from app.core.runtime_config import dashscope_video
    cfg = dashscope_video()
    if not cfg.api_key:
        raise ValueError("未配置 DashScope API Key")
    if not cfg.t2v_model:
        raise ValueError("未配置 DashScope 文生视频模型（dashscope_t2v_model）")

    base_url = cfg.base_url.rstrip("/")
    # 注意：通义万相 t2v 的提交 endpoint 也是 video-synthesis（同一个 endpoint，不同 model 名分发）
    submit_endpoint = f"{base_url}/services/aigc/video-generation/video-synthesis"

    res_norm = "1080P" if "1080" in resolution else "720P"
    clamped_duration = max(3, min(15, int(duration) or 5))

    payload: Dict[str, Any] = {
        "model": cfg.t2v_model,
        "input": {"prompt": prompt or ""},
        "parameters": {"duration": clamped_duration, "resolution": res_norm},
    }
    if seed and seed > 0:
        payload["parameters"]["seed"] = int(seed)

    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "X-DashScope-Async": "enable",
        "Content-Type": "application/json",
    }

    async with DASHSCOPE_SUBMIT_SEMAPHORE:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(submit_endpoint, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise ValueError(
                    f"DashScope T2V 提交失败 {resp.status_code}: {resp.text[:500]}"
                )
            data = resp.json()

    task_id = (data.get("output") or {}).get("task_id")
    if not task_id:
        raise ValueError(f"DashScope T2V 未返回 task_id: {data}")
    await _emit(progress_cb, 2.0, "DashScope T2V 已提交，等待排队...")

    poll_endpoint = f"{base_url}/tasks/{task_id}"
    auth_only = {"Authorization": f"Bearer {cfg.api_key}"}
    video_remote_url: Optional[str] = None
    last_status = ""
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i in range(180):
            r = await client.get(poll_endpoint, headers=auth_only)
            r.raise_for_status()
            d = r.json()
            out = d.get("output") or {}
            status = (out.get("task_status") or "").upper()
            last_status = status
            if status == "SUCCEEDED":
                video_remote_url = _pick_video_url(out)
                if not video_remote_url:
                    raise ValueError(f"T2V SUCCEEDED 但未找到 video_url: {d}")
                await _emit(progress_cb, 100.0, "任务完成，下载视频...")
                break
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise ValueError(
                    f"DashScope T2V 任务失败: {status} / code={out.get('code')} / message={out.get('message')}"
                )
            if status == "PENDING":
                await _emit(progress_cb, min(15.0, 2.0 + i), f"排队中 ({i+1})")
            else:
                await _emit(progress_cb, min(90.0, 15.0 + i * 0.5), f"{status or '生成中'} ({i+1})")
            await asyncio.sleep(5.0)

    if not video_remote_url:
        raise TimeoutError(f"DashScope T2V 超时 (task_id={task_id}, last={last_status})")

    output_path = os.path.join(videos_dir(), f"wanx_{uuid.uuid4().hex[:8]}.mp4")
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.get(video_remote_url)
        r.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(r.content)

    return {
        "video_url": f"/output/videos/{os.path.basename(output_path)}",
        "task_id": task_id,
        "duration": clamped_duration,
        "resolution": resolution,
        "prompt": prompt,
        "mock": False,
        "model": cfg.t2v_model,
        "provider": "dashscope",
    }
