"""
输出路径工具 — 跨平台、统一的图片/视频存储位置
"""
import asyncio
import base64
import os
from app.core.config import settings


_IMAGE_MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
}


def images_dir() -> str:
    path = os.path.join(settings.OUTPUT_DIR, "images")
    os.makedirs(path, exist_ok=True)
    return path


def videos_dir() -> str:
    path = os.path.join(settings.OUTPUT_DIR, "videos")
    os.makedirs(path, exist_ok=True)
    return path


def audio_dir() -> str:
    """0.6.1: 音频（BGM 等）输出目录"""
    path = os.path.join(settings.OUTPUT_DIR, "audio")
    os.makedirs(path, exist_ok=True)
    return path


def composites_dir() -> str:
    """0.7.0 轮 2：角色 composite 参考图（face close-up + 三视图横向拼接）输出目录"""
    path = os.path.join(settings.OUTPUT_DIR, "composites")
    os.makedirs(path, exist_ok=True)
    return path


def compose_character_reference(
    face_url: str, three_view_url: str
) -> str | None:
    """0.7.0 轮 2：把 face close-up（或 frontal）+ 三视图横向拼成一张「角色参考图」喂下游模型。

    布局：左侧 face 缩到 768x768（不变形居中裁剪/留白） + 右侧 3-view 1344x768 → 总 2112x768。
    返回 /output/composites/&lt;uuid&gt;.png 的 public URL；失败返 None（调用方需 fallback）。
    """
    import io
    import logging
    import uuid as _uuid
    from PIL import Image  # 已在 requirements

    logger = logging.getLogger(__name__)
    if not face_url or not three_view_url:
        return None

    def _load(url: str):
        """支持 /output/ 内部路径 / data: URL / 远程 URL。本地优先。"""
        local = resolve_output_path(url)
        try:
            if local and os.path.isfile(local):
                return Image.open(local).convert("RGB")
        except Exception as e:
            logger.warning("compose: 本地读 %s 失败: %s", local, e)
        # data URL
        if isinstance(url, str) and url.startswith("data:"):
            try:
                import base64
                _, b64 = url.split(",", 1)
                return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
            except Exception as e:
                logger.warning("compose: data url 解码失败: %s", e)
                return None
        # 远程 URL（同步下，PIL 不直接支持，跳过 — 通常上游已经把图存本地）
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            try:
                import urllib.request
                with urllib.request.urlopen(url, timeout=10) as resp:
                    return Image.open(io.BytesIO(resp.read())).convert("RGB")
            except Exception as e:
                logger.warning("compose: 远程图 %s 拉取失败: %s", url, e)
                return None
        return None

    face_img = _load(face_url)
    tv_img = _load(three_view_url)
    if face_img is None or tv_img is None:
        logger.warning("compose: face 或 three_view 加载失败，跳过 composite")
        return None

    try:
        # 0.7.3：改竖排(face 上占满宽度=正脸最高清，三视图下)。横排 2112:768=2.75 超火山 asset
        # 上限 2.5(AspectRatioTooLarge)；竖排 1024:~1609≈0.64 在 0.4~2.5 内。
        W = 1024  # 输出统一宽度

        # 1) 上：face 正脸——缩到宽 W(占满宽度，保正脸高清)，保持纵横比
        face_w, face_h = face_img.size
        scale_f = W / face_w
        f_new_h = max(1, int(face_h * scale_f))
        face_resized = face_img.resize((W, f_new_h), Image.LANCZOS)

        # 2) 下：三视图——缩到宽 W，保持纵横比
        tv_w, tv_h = tv_img.size
        scale_tv = W / tv_w
        tv_new_h = max(1, int(tv_h * scale_tv))
        tv_resized = tv_img.resize((W, tv_new_h), Image.LANCZOS)

        # 3) 竖排拼接(face 上，三视图下)
        total_h = f_new_h + tv_new_h
        canvas = Image.new("RGB", (W, total_h), (255, 255, 255))
        canvas.paste(face_resized, (0, 0))
        canvas.paste(tv_resized, (0, f_new_h))

        # 4) 存盘
        fname = f"{_uuid.uuid4().hex[:12]}.png"
        fpath = os.path.join(composites_dir(), fname)
        canvas.save(fpath, "PNG", optimize=True)
        return f"/output/composites/{fname}"
    except Exception as e:
        logger.warning("compose: 拼接失败: %s", e)
        return None


def to_public_url(local_path: str) -> str:
    """将本地输出路径转为前端可访问的 /output/... URL"""
    abs_output = os.path.abspath(settings.OUTPUT_DIR)
    abs_local = os.path.abspath(local_path)
    if abs_local.startswith(abs_output):
        rel = os.path.relpath(abs_local, abs_output).replace(os.sep, "/")
        return f"/output/{rel}"
    return local_path


def resolve_output_path(path_or_url: str) -> str:
    """将 /output/... 形式的伪 URL 映射回本地绝对路径；其他形式（http/https/data/绝对路径）原样返回"""
    if not path_or_url:
        return path_or_url
    if path_or_url.startswith(("http://", "https://", "data:")):
        return path_or_url
    if path_or_url.startswith("/output/"):
        return os.path.join(settings.OUTPUT_DIR, path_or_url[len("/output/"):])
    return path_or_url


async def image_to_data_url(path_or_url: str) -> str:
    """图片→data URL：远程 URL/已是 data: 的原样返回；本地或 /output/... 读为 base64 data URL。

    读取与 base64 编码会阻塞事件循环，因此放到线程池中执行。
    """
    if path_or_url.startswith(("http://", "https://", "data:")):
        return path_or_url

    local_path = resolve_output_path(path_or_url)

    def _encode() -> str:
        with open(local_path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(local_path)[1].lower()
        mime = _IMAGE_MIME_BY_EXT.get(ext, "image/png")
        b64 = base64.b64encode(data).decode("utf-8")
        return f"data:{mime};base64,{b64}"

    return await asyncio.to_thread(_encode)

