"""聊天附件上传 API

POST /api/uploads/{project_id}
  multipart files=[...]
  → 存到 settings.OUTPUT_DIR/uploads/{project_id}/{uuid}.{ext}
  → 返回 [{url, name, mime, size, kind, parsed_text?}]

支持三类附件（同一 endpoint，按 mime 自动分流）：
  - 图片：image/png / jpeg / webp / gif，≤ 8 MB，单次 ≤ 5 张
  - 剧本：text/plain / text/markdown / application/(...docx)，≤ 5 MB，单次仅 1 个（防混淆）
    剧本会即时 parse 成纯文本，前 2000 字预览随响应回，便于前端展示。
  - 音频：mp3 / wav / m4a / flac / ogg，≤ 80 MB，单次仅 1 个（1.1.0 MV 模式的曲目）
    这里只落盘，不做转码/识别——歌词识别在 mv_audio 阶段跑（要切片，耗时以十秒计）。
"""
import logging
import os
import uuid
from typing import List

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.core.config import settings
from app.core.script_parser import parse_script_bytes, preview_text

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/uploads", tags=["uploads"])


# ─── 图片限制 ───
MAX_IMAGE_SIZE = 8 * 1024 * 1024     # 8 MB
MAX_IMAGES_PER_REQUEST = 5
IMAGE_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
IMAGE_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# ─── 剧本/文档限制（0.7.0 专家模式）───
MAX_SCRIPT_SIZE = 5 * 1024 * 1024    # 5 MB
SCRIPT_MIME = {
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/msword",  # .doc — 接收但 parse 会抛 400
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/octet-stream",  # 浏览器对 .md 偶尔回这个
}
SCRIPT_EXT_FALLBACK = {".txt", ".md", ".markdown", ".doc", ".docx"}

# ─── 音频限制（1.1.0 MV 模式）───
# 80 MB：4 分钟 44.1kHz 立体声 wav 约 42 MB，留足余量给更长的曲子和无损格式
MAX_AUDIO_SIZE = 80 * 1024 * 1024
AUDIO_MIME = {
    "audio/mpeg", "audio/mp3",
    "audio/wav", "audio/wave", "audio/x-wav",
    "audio/mp4", "audio/x-m4a", "audio/aac",
    "audio/flac", "audio/x-flac",
    "audio/ogg",
}
AUDIO_EXT_FALLBACK = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def _uploads_dir(project_id: str) -> str:
    path = os.path.join(settings.OUTPUT_DIR, "uploads", project_id)
    os.makedirs(path, exist_ok=True)
    return path


def _classify(filename: str, mime: str) -> str:
    """返回 'image' / 'script' / 'audio' / 'unknown'，按 mime 优先 + 扩展名兜底。"""
    if mime in IMAGE_MIME:
        return "image"
    if mime in AUDIO_MIME:
        return "audio"
    if mime in SCRIPT_MIME:
        # octet-stream 必须靠扩展名再确认：浏览器对 .md / .flac / .m4a 都可能回这个
        if mime == "application/octet-stream":
            ext = os.path.splitext(filename or "")[1].lower()
            if ext in AUDIO_EXT_FALLBACK:
                return "audio"
            return "script" if ext in SCRIPT_EXT_FALLBACK else "unknown"
        return "script"
    # mime 不可信时再看扩展名
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in IMAGE_EXT_BY_MIME.values() or ext in (".jpeg",):
        return "image"
    if ext in AUDIO_EXT_FALLBACK:
        return "audio"
    if ext in SCRIPT_EXT_FALLBACK:
        return "script"
    return "unknown"


@router.post("/{project_id}")
async def upload_attachments(project_id: str, files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, "未上传任何文件")

    # 预读 + 分类
    parsed = []
    for uf in files:
        data = await uf.read()
        if not data:
            raise HTTPException(400, f"文件 {uf.filename} 是空的")
        mime = (uf.content_type or "").lower()
        kind = _classify(uf.filename or "", mime)
        parsed.append({"uf": uf, "data": data, "mime": mime, "kind": kind})

    # 计数校验
    images = [x for x in parsed if x["kind"] == "image"]
    scripts = [x for x in parsed if x["kind"] == "script"]
    audios = [x for x in parsed if x["kind"] == "audio"]
    unknowns = [x for x in parsed if x["kind"] == "unknown"]
    if unknowns:
        names = "、".join(x["uf"].filename or "?" for x in unknowns)
        raise HTTPException(
            400, f"不支持的文件类型：{names}（仅 png/jpg/webp/gif、txt/md/docx 或 mp3/wav/m4a/flac）"
        )
    if len(images) > MAX_IMAGES_PER_REQUEST:
        raise HTTPException(400, f"单次最多上传 {MAX_IMAGES_PER_REQUEST} 张图片，当前 {len(images)}")
    if len(scripts) > 1:
        raise HTTPException(400, f"单次仅能上传 1 个剧本/文档文件，当前 {len(scripts)}（防混淆）")
    if len(audios) > 1:
        raise HTTPException(400, f"单次仅能上传 1 个音频文件，当前 {len(audios)}（一支 MV 对应一首曲子）")

    out_dir = _uploads_dir(project_id)
    results = []

    for item in parsed:
        uf, data, mime, kind = item["uf"], item["data"], item["mime"], item["kind"]
        size = len(data)

        if kind == "image":
            if size > MAX_IMAGE_SIZE:
                raise HTTPException(
                    400, f"图片 {uf.filename} 大小 {size/1024/1024:.1f}MB 超限 {MAX_IMAGE_SIZE/1024/1024:.0f}MB"
                )
            ext = IMAGE_EXT_BY_MIME.get(mime, os.path.splitext(uf.filename or "")[1].lower() or ".png")
            fname = f"{uuid.uuid4().hex[:12]}{ext}"
            fpath = os.path.join(out_dir, fname)
            with open(fpath, "wb") as f:
                f.write(data)
            results.append({
                "url": f"/output/uploads/{project_id}/{fname}",
                "name": uf.filename or fname,
                "mime": mime,
                "size": size,
                "kind": "image",
            })

        elif kind == "audio":
            if size > MAX_AUDIO_SIZE:
                raise HTTPException(
                    400,
                    f"音频 {uf.filename} 大小 {size/1024/1024:.1f}MB 超限 "
                    f"{MAX_AUDIO_SIZE/1024/1024:.0f}MB（建议转成 mp3 再传）"
                )
            ext = os.path.splitext(uf.filename or "")[1].lower() or ".mp3"
            fname = f"{uuid.uuid4().hex[:12]}{ext}"
            fpath = os.path.join(out_dir, fname)
            with open(fpath, "wb") as f:
                f.write(data)
            # 时长只用于前端立刻回显（"4:14"），拿不到也不该挡住上传
            try:
                from app.modules._audio_analysis import probe_duration
                seconds = await probe_duration(fpath)
            except Exception as e:
                logger.warning("音频时长探测失败 %s: %s", uf.filename, e)
                seconds = 0.0
            results.append({
                "url": f"/output/uploads/{project_id}/{fname}",
                "name": uf.filename or fname,
                "mime": mime,
                "size": size,
                "kind": "audio",
                "duration": round(seconds, 2),
            })
            logger.info("音频上传 %s (%d KB, %.1f 秒)", uf.filename, size // 1024, seconds)

        else:  # script
            if size > MAX_SCRIPT_SIZE:
                raise HTTPException(
                    400, f"文档 {uf.filename} 大小 {size/1024/1024:.1f}MB 超限 {MAX_SCRIPT_SIZE/1024/1024:.0f}MB"
                )
            # 先 parse（.doc 会抛 ValueError），失败就不落盘
            try:
                full_text = parse_script_bytes(data, uf.filename or "")
            except ValueError as e:
                raise HTTPException(400, str(e))
            ext = os.path.splitext(uf.filename or "")[1].lower() or ".txt"
            fname = f"{uuid.uuid4().hex[:12]}{ext}"
            fpath = os.path.join(out_dir, fname)
            with open(fpath, "wb") as f:
                f.write(data)
            results.append({
                "url": f"/output/uploads/{project_id}/{fname}",
                "name": uf.filename or fname,
                "mime": mime,
                "size": size,
                "kind": "script",
                "parsed_text": preview_text(full_text, 2000),  # 前端预览用
                "parsed_chars": len(full_text),  # 让前端知道总字数（决定是否分段处理）
            })
            logger.info("剧本上传 %s (%d KB) → 解析 %d 字符", uf.filename, size // 1024, len(full_text))

    return {"attachments": results}
