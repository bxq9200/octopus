"""蚂蚁数科 antchain 视频翻译（本地化）—— 复用 Seedance 同一网关(AK/SK/endpoint/SDK/签名)。

流程：合成的成片本地文件 → OSS 公网 URL（视频翻译要求 http/https 公网）→
      videotranslate.createtask → 轮询 querytask → 下载各目标语言的 output_video_url。

前置：接口要单独找蚂蚁加白 `videotranslate.createtask.push` + `querytask.query`（同 Seedance 白名单流程）。
未开通 → PERMISSION_DENIED，调用方（本地化节点 runner）捕获后降级、不阻断发布。
"""
import asyncio
import logging
import os
import uuid
from typing import Any, Dict, List

import httpx

from app.core.paths import videos_dir, resolve_output_path
from app.core.runtime_config import antchain_video
# 复用现有 antchain 通道的 client 构建 + 响应字段提取（同 SDK/AK-SK/endpoint）
from app.modules._antchain_video import _build_client, _resp_field

logger = logging.getLogger(__name__)

_DONE_OK = {"COMPLETED"}
_DONE_FAIL = {"FAILED"}
_TRANSLATE_SEM = asyncio.Semaphore(1)  # 翻译任务串行


def _create_sync(cfg, video_url, video_name, translate_type, source_language,
                 target_languages, erase_subtitle, embed_subtitle):
    client, m = _build_client(cfg)
    req = m.PushVideotranslateCreatetaskRequest(
        video_url=video_url,
        video_name=(video_name or "octopus")[:100],
        translate_type=translate_type or "VOICE",   # VOICE / SUBTITLE / FACE
        source_language=source_language or "zh",
        target_languages=target_languages,           # 逗号分隔字符串，最多 10
        erase_subtitle=bool(erase_subtitle),
        embed_subtitle=bool(embed_subtitle),
    )
    return client.push_videotranslate_createtask(req)


def _query_sync(cfg, task_id):
    client, m = _build_client(cfg)
    return client.query_videotranslate_querytask(
        m.QueryVideotranslateQuerytaskRequest(task_id=str(task_id))
    )


def _subs_of(resp) -> List[Dict[str, Any]]:
    out = []
    for st in (_resp_field(resp, "sub_tasks", default=None) or []):
        out.append({
            "sub_task_id": _resp_field(st, "sub_task_id"),
            "target_language": _resp_field(st, "target_language"),
            "status": str(_resp_field(st, "status", default="") or "").upper(),
            "output_video_url": _resp_field(st, "output_video_url"),
            "translated_subtitle_url": _resp_field(st, "translated_subtitle_url"),
            "error_message": _resp_field(st, "error_message"),
        })
    return out


async def _poll_translate(cfg, task_id, progress_cb=None, max_attempts=240, interval=6.0) -> List[Dict[str, Any]]:
    """轮询直到 COMPLETED → 返回 sub_tasks[]（含 target_language + output_video_url）。FAILED/超时抛。"""
    not_found = 0
    for i in range(max_attempts):
        resp = await asyncio.to_thread(_query_sync, cfg, task_id)
        rc = str(_resp_field(resp, "result_code", default="") or "").upper()
        if rc and rc != "OK":
            if "NOT_FOUND" in rc or "NOTFOUND" in rc:  # 任务刚提交、网关未注册好 → 容忍重试
                not_found += 1
                if not_found >= 8:
                    raise ValueError(f"翻译任务持续不存在: req_msg_id={_resp_field(resp, 'req_msg_id')}")
                await asyncio.sleep(interval)
                continue
            raise ValueError(f"翻译查询失败: code={rc}, msg={_resp_field(resp, 'result_msg')}")
        not_found = 0
        status = str(_resp_field(resp, "status", default="") or "").upper()
        subs = _subs_of(resp)
        if status in _DONE_OK:
            return subs
        if status in _DONE_FAIL:
            errs = "；".join(f"{s['target_language']}:{s.get('error_message')}" for s in subs if s.get("error_message"))
            raise ValueError(f"翻译任务失败: {errs or status}")
        if progress_cb:
            done_n = sum(1 for s in subs if s["status"] in _DONE_OK)
            try:
                await progress_cb(f"🌐 视频翻译中...（{done_n}/{len(subs) or '?'} 语言完成，第 {i+1} 次查询）")
            except Exception:
                pass
        await asyncio.sleep(interval)
    raise TimeoutError(f"翻译任务轮询超时 (task_id={task_id})")


async def _download(remote_url: str, lang: str) -> str:
    out = os.path.join(videos_dir(), f"vtrans_{lang}_{uuid.uuid4().hex[:8]}.mp4")
    async with httpx.AsyncClient(timeout=600.0) as c:
        r = await c.get(remote_url)
        r.raise_for_status()
        with open(out, "wb") as f:
            f.write(r.content)
    return f"/output/videos/{os.path.basename(out)}"


async def translate_and_wait(
    video_src: str, *, target_languages, translate_type: str = "VOICE",
    source_language: str = "zh", erase_subtitle: bool = True, embed_subtitle: bool = True,
    video_name: str = "octopus", progress_cb=None,
) -> List[Dict[str, Any]]:
    """成片（本地 /output 路径 或 公网 URL）→ 翻译成多语言 → 返回
    [{target_language, video_url(本地), remote_url, subtitle_url}]（失败项带 error）。"""
    cfg = antchain_video()
    if isinstance(target_languages, (list, tuple)):
        langs = ",".join([str(x).strip() for x in target_languages if str(x).strip()][:10])
    else:
        langs = str(target_languages or "en").strip()
    langs = langs or "en"

    # 成片公网化：已是 http(s) 直接用；本地 → OSS 上传
    if isinstance(video_src, str) and video_src.startswith(("http://", "https://")):
        public_url = video_src
    else:
        # 0.8 缝2：走数据面（local=静态 OSS key / cloud_client=服务端 STS）。
        # 这里**不能**回退 base64——蚂蚁 IMS 要真·公网可下载的成片，故用 to_public_url + 保留硬失败。
        from app.core import media_gateway
        # /output/... 伪 URL → 真磁盘路径（Windows 上 /output 会被 os.path.isabs 误判为绝对路径，故一律走 resolve）；
        # 真绝对路径 resolve_output_path 会原样返回
        disk = resolve_output_path(video_src)
        if not disk or not os.path.exists(disk):
            raise FileNotFoundError(f"合成片不存在: {video_src} -> {disk}")
        if not media_gateway.is_available():
            raise RuntimeError("视频翻译要求成片公网可访问，但数据面未配置（设置里填 OSS AK/SK + bucket）")
        # public=True：设对象 public-read + 返回无签名干净 URL —— 蚂蚁 IMS 取签名 URL 会弄坏 query 串→MaterialNotExist
        public_url = await media_gateway.to_public_url(disk, public=True)
        if not public_url:
            raise RuntimeError("成片上传失败，无法翻译")

    if progress_cb:
        try:
            await progress_cb(f"🌐 提交视频翻译（{langs} · {translate_type}）...")
        except Exception:
            pass

    async with _TRANSLATE_SEM:
        resp = await asyncio.to_thread(
            _create_sync, cfg, public_url, video_name, translate_type,
            source_language, langs, erase_subtitle, embed_subtitle,
        )
        rc = str(_resp_field(resp, "result_code", default="") or "").upper()
        if rc != "OK":
            raise ValueError(f"翻译提交失败: code={rc}, msg={_resp_field(resp, 'result_msg')}")
        task_id = _resp_field(resp, "task_id")
        if not task_id:
            raise ValueError(f"翻译提交未返回 task_id: msg={_resp_field(resp, 'result_msg')}")
        logger.info("[videotranslate] task_id=%s langs=%s type=%s", task_id, langs, translate_type)
        subs = await _poll_translate(cfg, task_id, progress_cb)

    results: List[Dict[str, Any]] = []
    for s in subs:
        lang = s.get("target_language") or "xx"
        url = s.get("output_video_url")
        if s.get("status") in _DONE_OK and url:
            try:
                local = await _download(url, lang)
            except Exception as e:
                logger.warning("译后视频下载失败 %s: %s", lang, e)
                local = None
            results.append({
                "target_language": lang, "video_url": local, "remote_url": url,
                "subtitle_url": s.get("translated_subtitle_url"),
            })
        else:
            results.append({
                "target_language": lang, "video_url": None,
                "error": s.get("error_message") or s.get("status") or "未完成",
            })
    return results
