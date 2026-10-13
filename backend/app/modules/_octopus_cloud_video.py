"""0.8 缝1：`octopus_cloud` 视频 provider —— 平台视频中转（Seedance 中转 + credits 计费）。

**这是 0.8 的骨架，端点在 0.9 填。** 0.8 只保证：配置访问器、函数签名、返回契约、
落地下载、计量 payload 全部就位，且分发链路已经能选到它；真正发请求的地方明确抛
`NotImplementedError`，**不假装能出片**（上游分发有 try/except → 会降级 mock 并带上原因）。

与其它 provider 的根本区别
────────────────────────
本机**不持任何企业 key**。企业火山 key 只在云端；客户端只报"我要生成"，网关负责
验 token → 查余额 → 用企业 key 转发火山 → 按秒扣 credits → 回视频 URL。
所以这里只读 `gateway_url` + 用户账号 `token`（见 `runtime_config.octopus_cloud_config`）。

0.9 要填的契约（已在 docs/0.8_decoupling_plan.md 定稿）
────────────────────────────────────────────────
    POST {gateway}/v1/video/generate
      headers: Authorization: Bearer {token}
      body:    {kind: "t2v"|"i2v", prompt, first_frame_url?, reference_image_urls?,
                duration, resolution, ratio, seed}
      → 202 {task_id}
    GET  {gateway}/v1/video/tasks/{task_id}
      → {status: queued|running|succeeded|failed, video_url?, error?, usage?}

**字节不过平台服务器**：网关只回视频 URL，客户端直连下载（`_download` 就是干这个的）。
首帧 URL 由缝2 的 `media_gateway` 提供（本地图 → STS 直传 → 公网 URL），不塞 base64。
"""

import logging
import os
import uuid
from typing import Any, Dict, List, Optional

import httpx

from app.core.paths import videos_dir

logger = logging.getLogger(__name__)

# 与 ark/antchain 同构的提交节流（云端网关同样有并发/配额限制）
import asyncio
OCTOPUS_CLOUD_SUBMIT_SEMAPHORE = asyncio.Semaphore(2)


async def _emit(progress_cb, progress: float, message: str) -> None:
    if not progress_cb:
        return
    try:
        r = progress_cb(progress, message)
        if asyncio.iscoroutine(r):
            await r
    except Exception as e:
        logger.debug("progress_cb 异常忽略: %s", e)


def _require_config():
    """读配置并校验 —— 缺什么就说什么，别让用户对着 NotImplementedError 猜。"""
    from app.core.runtime_config import octopus_cloud_config
    cfg = octopus_cloud_config()
    if not cfg.gateway_url:
        raise ValueError(
            "Octopus 云端中转未配置网关地址（设置 → 视频生成 → Octopus 网关地址）"
        )
    if not cfg.token:
        raise ValueError(
            "Octopus 云端中转缺少账号 Token（设置 → 视频生成 → Octopus 账号 Token；登录后自动填充）"
        )
    return cfg


def _auth_headers(cfg) -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if cfg.token:
        h["Authorization"] = f"Bearer {cfg.token}"
    return h


async def _submit(cfg, *, kind: str, prompt: str, first_frame_url: str,
                  reference_image_urls: List[str], duration: int,
                  resolution: str, ratio: str, seed: int,
                  character_refs: Optional[List[Dict[str, str]]] = None) -> str:
    """POST /v1/video/generate → task_id。"""
    from app.core import cloud_http
    body = {
        "kind": kind, "prompt": prompt, "first_frame_url": first_frame_url,
        "reference_image_urls": reference_image_urls, "duration": duration,
        "resolution": resolution, "ratio": ratio, "seed": seed,
        # 跨镜一致：角色参考图（稳定 key + 公网 URL）；云端 register_asset 成 asset://火山id 锚脸
        "character_refs": character_refs or [],
    }
    resp = await cloud_http.post(f"{cfg.gateway_url}/v1/video/generate",
                                 json=body, headers=_auth_headers(cfg), timeout=60.0)
    if resp.status_code != 200:
        detail = ""
        try:
            detail = (resp.json() or {}).get("detail") or ""
        except Exception:
            detail = (resp.text or "")[:200]
        # 402 = credits 不足，话术直接透传给用户
        raise RuntimeError(f"云端视频提交失败 HTTP {resp.status_code}：{str(detail)[:240]}")
    return (resp.json() or {}).get("task_id") or ""


async def _poll(cfg, task_id: str, progress_cb,
                max_attempts: int = 150, interval: float = 5.0) -> str:
    """GET /v1/video/tasks/{id} 轮询到 succeeded → 远程 video_url。"""
    from app.core import cloud_http
    url = f"{cfg.gateway_url}/v1/video/tasks/{task_id}"
    for i in range(max_attempts):
        resp = await cloud_http.get(url, headers=_auth_headers(cfg), timeout=30.0)
        if resp.status_code == 200:
            data = resp.json() or {}
            status = data.get("status")
            if status == "succeeded":
                await _emit(progress_cb, 95.0, "云端出片完成，下载中...")
                vurl = data.get("video_url")
                if vurl:
                    return vurl
                raise ValueError("云端任务成功但未返回视频 URL")
            if status == "failed":
                raise ValueError(f"云端视频生成失败：{data.get('error') or '未知错误'}")
            prog = min(90.0, 5.0 + (i / max_attempts) * 80.0)
            await _emit(progress_cb, prog, f"云端生成中（{status or '排队'}，第 {i+1} 次轮询）")
        await asyncio.sleep(interval)
    raise TimeoutError(f"云端视频任务轮询超时：{task_id}")


async def _download(video_remote_url: str) -> str:
    """下载远程视频到 /output/videos/，返回本地 public 路径。

    这一步 0.9 不用改：网关只回 URL，字节直连厂商/OSS 下载，不过平台服务器。
    """
    out_path = os.path.join(videos_dir(), f"octocloud_{uuid.uuid4().hex[:8]}.mp4")
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.get(video_remote_url)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
    return f"/output/videos/{os.path.basename(out_path)}"


async def _create_and_wait(*, kind: str, prompt: str, first_frame_url: str = "",
                           reference_image_urls: Optional[List[str]] = None,
                           character_ref_paths: Optional[List[str]] = None,
                           duration: int = 5, resolution: str = "720x1280",
                           ratio: str = "", seed: int = -1,
                           progress_cb=None) -> Dict[str, Any]:
    """提交 + 轮询 + 下载的公共流程；返回对齐 ark/antchain/dashscope 的结果 dict。"""
    cfg = _require_config()
    dur = max(4, min(12, int(duration) or 5))     # Seedance 2.0：duration ∈ [4,12]

    # 关键：首帧/参考图必须转成**公网 URL** 再发云端（云端没有本机文件，厂商也不认本地路径/base64）。
    # media_gateway public=True → 传 OSS 拿无签名 CDN URL；远程 URL 原样透传。
    # 缺了这步就会把 `/output/images/xxx.png` 原样发出去 → antchain 报 image_url 无效 → i2v 失败。
    from app.core import media_gateway
    if first_frame_url:
        first_frame_url = await media_gateway.to_model_input(first_frame_url, public=True)
    ref_public: List[str] = []
    for _r in (reference_image_urls or []):
        ref_public.append(await media_gateway.to_model_input(_r, public=True))
    reference_image_urls = ref_public

    # 跨镜一致：角色参考图 → {key=原始稳定路径（云端按此缓存 asset，只注册一次）, url=公网URL}。
    character_refs: List[Dict[str, str]] = []
    for _p in (character_ref_paths or []):
        if not _p:
            continue
        pub = await media_gateway.to_model_input(_p, public=True)
        if pub:
            character_refs.append({"key": str(_p), "url": pub})

    await _emit(progress_cb, 2.0, "Octopus 云端中转任务提交中...")
    async with OCTOPUS_CLOUD_SUBMIT_SEMAPHORE:
        task_id = await _submit(
            cfg, kind=kind, prompt=prompt, first_frame_url=first_frame_url,
            reference_image_urls=list(reference_image_urls or []),
            character_refs=character_refs,
            duration=dur, resolution=resolution, ratio=ratio, seed=seed,
        )

    video_remote_url = await _poll(cfg, task_id, progress_cb)
    local_url = await _download(video_remote_url)
    return {
        "video_url": local_url,
        "origin_url": video_remote_url,     # 远程 url 透传（尾帧续接 / 信任产物链路用）
        "task_id": task_id,
        "duration": dur,
        "resolution": resolution,
        "prompt": prompt,
        "mock": False,
        "model": cfg.model,
        "provider": "octopus_cloud",
        # 缝6 计量：这份 usage 会被 _capture_usage_to_cost_log 捞走 → meter.report() 上报云端扣 credits
        "usage": {
            "provider": "octopus_cloud", "model": cfg.model, "kind": "video",
            "seconds": dur,
            "resolution": "1080p" if "1080" in (resolution or "") else "720p",
        },
    }


async def generate_text_to_video(
    prompt: str,
    *,
    duration: int = 5,
    resolution: str = "720x1280",
    ratio: str = "",
    reference_images: Optional[List[str]] = None,
    seed: int = -1,
    progress_cb=None,
    **_kw,
) -> Dict[str, Any]:
    """文生视频（无首帧）；reference_images 作多图参考上送。"""
    return await _create_and_wait(
        kind="t2v", prompt=prompt,
        reference_image_urls=list(reference_images or []),
        duration=duration, resolution=resolution, ratio=ratio, seed=seed,
        progress_cb=progress_cb,
    )


async def generate_image_to_video(
    image_url: str,
    prompt: str,
    *,
    duration: int = 5,
    resolution: str = "720x1280",
    reference_images: Optional[List[str]] = None,
    character_asset_ids: Optional[List[str]] = None,
    seed: int = -1,
    progress_cb=None,
    **_kw,
) -> Dict[str, Any]:
    """图生视频。首帧 URL 由缝2 的 media_gateway 提供（公网 URL，非 base64）。

    注：Seedance 首帧与 reference_image 互斥（0.7.1 #5 实测），这条约束由**云端**统一处理
    —— 客户端把有的都送上去，网关按厂商规则裁剪，免得每个客户端各写一套。
    """
    return await _create_and_wait(
        kind="i2v", prompt=prompt, first_frame_url=image_url,
        reference_image_urls=list(reference_images or []),
        # i2v 的 reference_images 就是角色参考图（antchain 普通 ref 会忽略）→ 同时作为
        # character_ref_paths 送云端注册 asset:// 强锚定人脸，实现跨镜一致。
        character_ref_paths=list(reference_images or []),
        duration=duration, resolution=resolution, seed=seed,
        progress_cb=progress_cb,
    )
