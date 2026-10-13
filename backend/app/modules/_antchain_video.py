"""蚂蚁数科 AIGC（antchain OpenAPI 网关）视频生成 —— 0.7.3 渠道商 Seedance 2.0 通道。

接口（网关 action 码，由官方 Python SDK `antchain_aigc` 封装）：
  - antdigital.aigc.video.createtask.push  → push_video_createtask
  - antdigital.aigc.video.querytask.query  → query_video_querytask
  - antdigital.aigc.asset.upload           → upload_asset   （真人参考素材"洗白"通道）
  - antdigital.aigc.asset.query            → query_asset

认证：Config(access_key_id, access_key_secret, endpoint)，HmacSHA1 由 SDK do_request 内部签名。

⚠ 骨架状态（2026-06-15）：账号(AK/SK)未到、SDK 未 pip 安装、未真调。
  - SDK import 延迟到调用时，缺包抛清晰错误（不影响后端启动 / 其它 provider）。
  - 字段分歧待实测确认：Python SDK models 用 `ratio`，Java 示意用 `size`（这里 ratio 主传、size 放 ext 备用）。
  - duration 文档 4-12 秒 STRING。
  - 🔴 状态枚举：蚂蚁 20260807《视频创作 OpenAPI 切换手册》把枚举全改了——
    视频任务 submitted/processing/succeeded/failed → SUBMITTED/RUNNING/SUCCEEDED/FAILED；
    资产 available/failed → ACTIVE/PROCESSING/FAILED（**可用态叫 ACTIVE 不叫 SUCCEEDED**）。
    本文件新旧并收，灰度切换期两边都不炸。AK/SK、白名单、endpoint、接口码均不变。
  详见记忆 octopus-073-antchain-seedance。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

import httpx

from app.core.paths import videos_dir

logger = logging.getLogger(__name__)

# 同 ark/dashscope：同一 AK 多任务洪水会被网关限流，提交并发节流
ANTCHAIN_SUBMIT_SEMAPHORE = asyncio.Semaphore(2)

# ─── 状态枚举 ───
# 🔴 视频任务状态与资产状态**不是一套**，蚂蚁的切换手册专门提醒过别混用
#    （PROCESSING 在旧版是视频"运行中"，在新版是资产"预处理中"）。
#    这里两套分开定义，且同时收旧值与新值——蚂蚁 20260807 的切换手册把枚举全改了，
#    新旧并收才能在他们灰度切换期间两边都不炸。

# 视频任务：旧 submitted/processing/succeeded/failed → 新 SUBMITTED/RUNNING/SUCCEEDED/FAILED
# 非终态（SUBMITTED / RUNNING / PROCESSING / 其它）一律继续轮询，所以只需列终态。
_DONE_OK = {"SUCCESS", "SUCCEEDED"}
_DONE_FAIL = {"FAILED", "CANCELLED", "CANCELED", "EXPIRED", "ERROR"}

# 资产：旧 available/failed → 新 ACTIVE/PROCESSING/FAILED
# 🔴 新版可用态叫 ACTIVE，不叫 SUCCEEDED。漏了它会**静默失效**：
#    轮询永远等不到终态 → 超时返回 None → 锁脸降级、真人素材没洗白，
#    但一条错误日志都不会有，只是成片里的脸开始漂。
_ASSET_OK = {"ACTIVE", "AVAILABLE", "SUCCESS", "SUCCEEDED"}
_ASSET_FAIL = {"FAILED", "ERROR", "CANCELLED", "CANCELED", "EXPIRED"}
# PROCESSING（预处理中）不属于任何终态，落到"继续等"分支即可——
# 千万别因为它在旧版视频枚举里出现过就当成终态。


# ─── SDK 客户端（延迟 import，缺包优雅报错） ───

def _build_client(cfg):
    """构造 antchain SDK Client。SDK 未安装时抛 RuntimeError（提示 pip 安装）。"""
    try:
        from antchain_sdk_aigc.client import Client
        from antchain_sdk_aigc import models as ac_models
    except ImportError as e:
        raise RuntimeError(
            "未安装蚂蚁数科 AIGC SDK。请运行：pip install antchain_aigc"
            "（依赖 antchain_alipay_util / alibabacloud_tea_util / alibabacloud_rpc_util）"
        ) from e
    if not cfg.ak or not cfg.sk:
        raise ValueError("未配置蚂蚁数科 AccessKey（antchain_ak / antchain_sk）")
    config = ac_models.Config(
        access_key_id=cfg.ak,
        access_key_secret=cfg.sk,
        endpoint=cfg.endpoint,
    )
    return Client(config), ac_models


async def _emit(progress_cb, progress: float, message: str) -> None:
    if not progress_cb:
        return
    try:
        r = progress_cb(progress, message)
        if asyncio.iscoroutine(r):
            await r
    except Exception:
        pass


def _res_to_ratio(resolution: str, ratio_hint: str = "") -> str:
    """resolution(如 720x1280) → ratio(如 9:16)。已是 a:b 形式则原样。"""
    if ratio_hint and ":" in ratio_hint:
        return ratio_hint
    r = (resolution or "").lower()
    if ":" in r:
        return r
    try:
        w, h = (int(x) for x in r.split("x"))
        if w > h:
            return "16:9"
        if h > w:
            return "9:16"
        return "1:1"
    except Exception:
        return "16:9"


def _resp_field(resp, *names, default=None):
    """SDK response 对象取字段（容错属性名 / dict 两种形态）。"""
    for n in names:
        if hasattr(resp, n) and getattr(resp, n) is not None:
            return getattr(resp, n)
        if isinstance(resp, dict) and resp.get(n) is not None:
            return resp[n]
    return default


# ─── 底层：提交 / 轮询 / 下载（SDK 同步调用 → to_thread 包装不阻塞事件循环） ───

def _submit_sync(cfg, model, prompt, image_urls, video_urls, duration, ratio, request_id, size):
    client, m = _build_client(cfg)
    req = m.PushVideoCreatetaskRequest(
        model=model,
        prompt=prompt or "",
        image_url=image_urls or None,
        video_url=video_urls or None,
        duration=str(duration),
        ratio=ratio or None,
        size=size or None,   # patch models.py 加了 size 顶层字段（实测网关接受，比塞 ext 强；重装 SDK 需重打 patch）
        request_id=request_id,
        # 0.7.3：开音频生成（Seedance 自带对白/音效）。蚂蚁 ext 字段名是**驼峰 generateAudio**（非下划线）
        ext=json.dumps({"generateAudio": True}),
    )
    return client.push_video_createtask(req)


def _query_sync(cfg, task_id):
    client, m = _build_client(cfg)
    return client.query_video_querytask(m.QueryVideoQuerytaskRequest(task_id=task_id))


async def _poll_task(cfg, task_id: str, progress_cb=None,
                     max_attempts: int = 180, interval: float = 5.0) -> str:
    """轮询任务直到 SUCCESS → 返回远程 video_url；FAILED/超时抛错。"""
    last_status = ""
    not_found = 0
    for i in range(max_attempts):
        resp = await asyncio.to_thread(_query_sync, cfg, task_id)
        # query 失败（result_code != OK，此时无 status 字段）分两类处理：
        rc = str(_resp_field(resp, "result_code", default="") or "").upper()
        if rc and rc != "OK":
            # ① TASK_NOT_FOUND：任务刚提交、网关可能还没注册好 → 容忍继续轮询（连续多次才算真没建成）
            if "NOT_FOUND" in rc or "NOTFOUND" in rc:
                not_found += 1
                if not_found >= 8:
                    raise ValueError(
                        f"antchain 任务持续不存在({not_found}次查询): req_msg_id={_resp_field(resp, 'req_msg_id')}"
                    )
                last_status = rc
                await asyncio.sleep(interval)
                continue
            # ② 权限/参数等其它错误 → 立即致命（否则把空 status 当"生成中"傻轮询到超时）
            raise ValueError(
                f"antchain 查询失败: result_code={rc}, msg={_resp_field(resp, 'result_msg')}, "
                f"req_msg_id={_resp_field(resp, 'req_msg_id')}"
            )
        not_found = 0
        status = str(_resp_field(resp, "status", default="") or "").upper()
        last_status = status
        if status in _DONE_OK:
            video_url = _resp_field(resp, "video_url")
            if not video_url:
                raise ValueError(f"antchain 任务 SUCCESS 但无 video_url (task_id={task_id})")
            await _emit(progress_cb, 100.0, "任务完成，正在下载视频...")
            return video_url
        if status in _DONE_FAIL:
            raise ValueError(
                f"antchain 任务失败: status={status}, "
                f"code={_resp_field(resp, 'error_code')}, msg={_resp_field(resp, 'error_message')}"
            )
        # SUBMITTED / PROCESSING / 其它 → 继续
        prog = min(90.0, 5.0 + (i / max_attempts) * 80.0)
        await _emit(progress_cb, prog, f"{status or '生成中'} ({i+1} 次轮询)")
        await asyncio.sleep(interval)
    raise TimeoutError(f"antchain 轮询超时 (task_id={task_id}, last_status={last_status})")


async def _download(video_remote_url: str) -> str:
    """下载远程视频到 /output/videos/，返回本地 public 路径。"""
    out_path = os.path.join(videos_dir(), f"antchain_{uuid.uuid4().hex[:8]}.mp4")
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.get(video_remote_url)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
    return f"/output/videos/{os.path.basename(out_path)}"


async def _create_and_wait(
    *, prompt: str, image_urls: List[str], video_urls: List[str],
    duration: int, resolution: str, ratio: str, progress_cb,
) -> Dict[str, Any]:
    """提交 + 轮询 + 下载 的公共流程，返回对齐 ark/dashscope 的结果 dict。"""
    from app.core.runtime_config import antchain_video
    cfg = antchain_video()
    model = cfg.model
    dur = max(4, min(12, int(duration) or 5))   # Seedance 2.0：duration ∈ [4,12]
    ratio_final = _res_to_ratio(resolution, ratio)
    size = resolution if (resolution and "x" in resolution.lower()) else None
    request_id = uuid.uuid4().hex

    await _emit(progress_cb, 2.0, "蚂蚁数科 Seedance 任务提交中...")
    logger.info("[antchain createtask] model=%s | image_url×%d=%s | ratio=%s size=%s dur=%s",
                model, len(image_urls), [str(u)[:70] for u in image_urls], ratio_final, size, dur)
    async with ANTCHAIN_SUBMIT_SEMAPHORE:
        resp = await asyncio.to_thread(
            _submit_sync, cfg, model, prompt, image_urls, video_urls,
            dur, ratio_final, request_id, size,
        )
    task_id = _resp_field(resp, "task_id")
    if not task_id:
        raise ValueError(
            f"antchain 提交未返回 task_id: code={_resp_field(resp, 'result_code')}, "
            f"msg={_resp_field(resp, 'result_msg')}"
        )

    video_remote_url = await _poll_task(cfg, task_id, progress_cb)
    local_url = await _download(video_remote_url)
    return {
        "video_url": local_url,
        "origin_url": video_remote_url,     # 渠道远程 url 透传（W5 尾帧 / 信任产物链路用）
        "task_id": task_id,
        "duration": dur,
        "resolution": resolution,
        "prompt": prompt,
        "mock": False,
        "model": model,
        "provider": "antchain",
        "usage": {
            "provider": "antchain", "model": model, "kind": "video",
            "seconds": dur, "resolution": "1080p" if "1080" in (resolution or "") else "720p",
        },
    }


# ─── 公开契约（对齐 _dashscope_video / ark：上游 text_to_video / image_to_video 无感分发） ───

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
    """文生视频（无首帧）。reference_images 走 image_url LIST（多图参考，0.7.3 新能力）。"""
    return await _create_and_wait(
        prompt=prompt,
        image_urls=list(reference_images or []),
        video_urls=[],
        duration=duration, resolution=resolution, ratio=ratio, progress_cb=progress_cb,
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
    """图生视频。首帧(image_url)给镜头画面 + 角色 asset 锁身份，二者共存。
    0.7.3 人物一致性：character_asset_ids = 已入 asset 库(火山私域素材库)的角色 asset_id，
    现取 asset_url 追加进 image_url LIST → 火山按注册人脸锚定角色，跨镜头脸一致。
    探针实测蚂蚁通道 image_url=[首帧, asset_url] 受理并出片（非互斥；之前"互斥"实为参考图 URL 无效）。
    reference_images(普通参考图)仍忽略——与首帧真互斥，多图参考是 t2v 能力。"""
    # image_url 可为 asset://火山id(关键帧入库后，免审+精确构图) 或普通 URL(兜底)；空则纯虚拟人像模式
    imgs = [image_url] if image_url else []
    logger.info("[antchain i2v] 首帧=%s + %d 个虚拟人像 asset: %s",
                "asset://" if str(image_url).startswith("asset://") else ("url" if image_url else "无"),
                len(character_asset_ids or []), character_asset_ids or [])
    for aid in (character_asset_ids or []):
        # aid=火山格式 assetId(register_asset 返回)→ asset://虚拟人像：**强锚定人脸 + 免真人审核**(07-02 双角色实测)。
        # 若 aid 仍是蚂蚁 0a2a 格式(蚂蚁 query bug 未修)→ 火山报 not found，需蚂蚁修 query 返回火山 id。
        imgs.append(f"asset://{aid}")
        logger.info("[antchain i2v] 虚拟人像 asset://%s", aid)
    return await _create_and_wait(
        prompt=prompt,
        image_urls=imgs,
        video_urls=[],
        duration=duration, resolution=resolution, ratio="", progress_cb=progress_cb,
    )


# ─── 资产通道：真人参考素材"洗白"（Seedance 真人审核解药） ───
# 流程：真人图 file_url → upload_asset 拿 asset_id → 轮询 query_asset 拿 asset_url
#       → 用 asset_url 替换原图喂 createtask，规避 Seedance 真人限制。
# 骨架已就绪；接线进关键帧→视频链路放账号到位后（见 octopus-073 接入清单）。

def _upload_asset_sync(cfg, file_url, asset_name, asset_type):
    client, m = _build_client(cfg)
    return client.upload_asset(m.UploadAssetRequest(
        asset_type=asset_type, file_url=file_url, asset_name=asset_name,
    ))


def _query_asset_sync(cfg, asset_id):
    client, m = _build_client(cfg)
    return client.query_asset(m.QueryAssetRequest(asset_id=asset_id))


async def _resolve_asset_url(asset_id: str) -> Optional[str]:
    """asset_id → 最新 asset_url（每次现取；asset_url 带 ~12h 时效签名，不宜缓存）。"""
    from app.core.runtime_config import antchain_video
    cfg = antchain_video()
    q = await asyncio.to_thread(_query_asset_sync, cfg, asset_id)
    return _resp_field(q, "asset_url")


def _extract_volc_asset_id(query_resp, antchain_asset_id: str) -> str:
    """从 query_asset 提取【火山格式 assetId】(asset-日期-后缀；asset:// 强锚定+免真人审核要用)。
    ❗蚂蚁 SDK 当前 bug：query 返回的 asset_id 是 0a2a 网关格式、ext_info 空，**不含真实火山 id**。
    蚂蚁修复后火山 id 应出现在 asset_id 或 ext_info → **修好后只改这一处**即可全链路生效。
    拿不到火山 id 时 fallback 蚂蚁 id（asset:// 会 not found，等蚂蚁修 query）。"""
    import json as _json
    aid = str(_resp_field(query_resp, "asset_id", default="") or "")
    if aid.startswith("asset-"):          # 蚂蚁修好后 asset_id 直接是火山格式
        return aid
    ext = str(_resp_field(query_resp, "ext_info", default="") or "")
    try:
        obj = _json.loads(ext) if ext and ext != "{}" else {}
        for k in ("volcAssetId", "assetId", "arkAssetId", "volc_asset_id"):
            v = obj.get(k)
            if v and str(v).startswith("asset-"):
                return str(v)
    except Exception:
        pass
    logger.warning("⚠️ query_asset 无火山格式 assetId(蚂蚁 SDK bug)，fallback 蚂蚁 id=%s；"
                   "asset:// 强锚定需蚂蚁修 query 接口后才生效", antchain_asset_id)
    return antchain_asset_id


async def register_asset(src: str, name: str = "", *, max_wait: int = 300, interval: float = 8.0) -> Optional[str]:
    """0.7.3 通用资产入库(角色/关键帧/场景/服装/道具) → 返回【火山格式 assetId】(asset://强锚定+免真人审核)。
    src：本地 /output 图 / 火山 origin_url / 任意公网图。
    流程：src → **传 OSS 短签名 URL**(base64 炸网关、origin_url 24h 过期，OSS 稳定) → 蚂蚁 upload_asset → 轮询 → 火山 assetId。
    入库慢(数十秒~分钟)→ 异步、一次性；失败/超时返 None。"""
    from app.core.runtime_config import antchain_video
    from app.core import media_gateway
    cfg = antchain_video()
    nm = name or f"asset_{uuid.uuid4().hex[:8]}"
    # 1) 图源统一转短签名公网 URL（蚂蚁 upload_asset 只认短公网 URL）
    #    0.8 缝2：走数据面。**rehost_remote=True** —— 这里是少数要把远程图也重新托管的地方：
    #    火山 origin_url 24h 就过期，蚂蚁异步下载会 403 DownloadFailed，必须先落到我们自己的存储。
    #    数据面不可用时 to_model_input 自动回退 base64（可能卡网关）/ 远程原图。
    try:
        file_url = await media_gateway.to_model_input(src, rehost_remote=True)
    except Exception as e:
        logger.warning("register_asset 图源公网化失败 %s: %s", str(src)[:60], e)
        return None
    if not file_url:
        return None
    # 2) upload_asset
    try:
        up = await asyncio.to_thread(_upload_asset_sync, cfg, file_url, nm, "Image")
    except Exception as e:
        logger.warning("register_asset upload 异常: %s", e)
        return None
    asset_id = _resp_field(up, "asset_id")
    if not asset_id or str(_resp_field(up, "result_code", default="") or "").upper() != "OK":
        logger.warning("register_asset upload 失败: %s / %s",
                       _resp_field(up, "result_code"), _resp_field(up, "result_msg"))
        return None
    # 3) 轮询 SUCCEEDED → 提取火山 assetId
    waited = 0.0
    while waited < max_wait:
        await asyncio.sleep(interval)
        waited += interval
        q = await asyncio.to_thread(_query_asset_sync, cfg, asset_id)
        st = str(_resp_field(q, "status", default="") or "").upper()
        if st in _ASSET_OK:
            volc_id = _extract_volc_asset_id(q, asset_id)
            logger.info("register_asset 成功 %s → 蚂蚁id=%s 火山id=%s (%.0fs, status=%s)",
                        nm, asset_id, volc_id, waited, st)
            return volc_id
        if st in _ASSET_FAIL:
            logger.warning("register_asset 失败 %s: %s", nm, _resp_field(q, "error_message"))
            return None
    logger.warning("register_asset 超时 %s (asset_id=%s, %ds)", nm, asset_id, max_wait)
    return None


# 向后兼容别名（旧调用 register_character_asset(image_url, name) 位置参数一致）
register_character_asset = register_asset


async def upload_asset_get_url(
    file_url: str, *, asset_name: str = "", asset_type: str = "Image",
    max_attempts: int = 60, interval: float = 3.0,
) -> Optional[str]:
    """上传真人参考素材 → 返回可替换原图的 asset_url（失败/超时返回 None，调用方回退原图）。"""
    from app.core.runtime_config import antchain_video
    cfg = antchain_video()
    name = asset_name or f"asset_{uuid.uuid4().hex[:8]}"
    resp = await asyncio.to_thread(_upload_asset_sync, cfg, file_url, name, asset_type)
    asset_id = _resp_field(resp, "asset_id")
    if not asset_id:
        logger.warning("antchain 资产上传未返回 asset_id: %s", _resp_field(resp, "result_msg"))
        return None
    for _ in range(max_attempts):
        q = await asyncio.to_thread(_query_asset_sync, cfg, asset_id)
        status = str(_resp_field(q, "status", default="") or "").upper()
        if status in _ASSET_OK:
            return _resp_field(q, "asset_url")
        if status in _ASSET_FAIL:
            # 新版响应在 error_message 前多了 error_code，一并记上便于排查
            logger.warning("antchain 资产处理失败: %s / %s",
                           _resp_field(q, "error_code", default=""), _resp_field(q, "error_message"))
            return None
        await asyncio.sleep(interval)
    logger.warning("antchain 资产轮询超时 (asset_id=%s)", asset_id)
    return None
