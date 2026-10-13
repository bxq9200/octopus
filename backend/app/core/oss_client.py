"""阿里云 OSS 客户端 —— asset 入库中转（本地图 / 火山 origin_url → OSS 短公网 URL）。

为什么需要：蚂蚁 `asset.upload` 的 file_url 必须是**短公网 URL**——
- 大 base64 塞请求体 → 蚂蚁网关 HmacSHA1 签名炸（INVALID_SIGNATURE）
- 火山 origin_url 24h 签名过期 → CreateAsset 403 DownloadFailed
OSS 上传后用 `sign_url` 拿 1h 签名链接，够蚂蚁下载入库；火山把图拉进自己 media-asset 后即不再依赖 OSS。
"""
import asyncio
import logging
import os
import uuid
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_SIGN_EXPIRES = 3600  # 签名 URL 有效期（秒）；够 upload_asset 下载即可


def _bucket_and_dir():
    """返回 (oss2.Bucket, base_dir)；未配置返回 (None, None)。"""
    try:
        import oss2
    except ImportError:
        logger.warning("oss2 未安装，OSS 上传不可用")
        return None, None
    from app.core.runtime_config import oss_config
    cfg = oss_config()
    if not (cfg.access_key_id and cfg.access_key_secret and cfg.bucket and cfg.endpoint):
        return None, None
    endpoint = cfg.endpoint if cfg.endpoint.startswith("http") else f"https://{cfg.endpoint}"
    auth = oss2.Auth(cfg.access_key_id, cfg.access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, cfg.bucket)
    base_dir = (cfg.base_dir or "octopus").strip("/")
    return bucket, base_dir


def is_configured() -> bool:
    b, _ = _bucket_and_dir()
    return b is not None


async def upload_bytes(data: bytes, ext: str = "png", content_type: str = "image/png",
                       public: bool = False) -> Optional[str]:
    """上传字节 → 返回公网 URL。默认返回 1h **签名** URL；`public=True` 时把对象设 public-read
    并返回**无签名干净 URL**（无 query 串）。未配置 / 失败返 None。

    为何要 public：蚂蚁 IMS 视频翻译对 OSS **签名 URL**（`?...Signature=...%3D`）取用时会弄坏 query
    串 → 它那边下载 403 → 回 `MaterialNotExist`。改喂 public-read 干净 URL 即受理（已实测 createtask OK）。
    图片 / Seedance asset 入库仍走签名 URL，不受影响。
    """
    bucket, base_dir = _bucket_and_dir()
    if bucket is None:
        logger.warning("OSS 未配置，upload_bytes 跳过")
        return None
    key = f"{base_dir}/{uuid.uuid4().hex}.{ext}"

    def _put_and_url():
        headers = {"Content-Type": content_type}
        if public:
            headers["x-oss-object-acl"] = "public-read"   # 建对象即设 public-read
        bucket.put_object(key, data, headers=headers)
        if public:
            from app.core.runtime_config import oss_config
            cfg = oss_config()
            # 优先自定义 CDN 域名：蚂蚁 IMS 视频翻译**只认 CDN 域名**（你绑定的自定义域名），
            # 不认原始 *.oss-cn-beijing.aliyuncs.com（隔离测试实证：CDN 域名过审、原始域名 MaterialNotExist）。
            dom = (cfg.domain or "").replace("https://", "").replace("http://", "").strip("/")
            if dom:
                return f"https://{dom}/{key}"
            host = (cfg.endpoint or "").replace("https://", "").replace("http://", "").strip("/")
            return f"https://{cfg.bucket}.{host}/{key}"   # 无自定义域名时回退虚拟主机式无签名 URL
        # 默认：sign_url 基于 endpoint 生成带签名的 GET 链接（私有桶也可访问）
        url = bucket.sign_url("GET", key, _SIGN_EXPIRES, slash_safe=True)
        return url if url.startswith("http") else f"https://{url}"

    try:
        return await asyncio.to_thread(_put_and_url)
    except Exception as e:
        logger.warning("OSS upload_bytes 失败: %s", e)
        return None


async def upload_local(path: str, public: bool = False) -> Optional[str]:
    """本地文件（/output/... 或绝对路径）→ OSS URL。`public=True` 返回无签名 public-read 干净 URL（蚂蚁视频翻译要）。"""
    from app.core.paths import resolve_output_path
    local = resolve_output_path(path) if isinstance(path, str) and path.startswith("/output/") else path
    if not local or not os.path.isfile(local):
        logger.warning("OSS upload_local: 文件不存在 %s", local)
        return None
    ext = (os.path.splitext(local)[1].lstrip(".") or "png").lower()
    # 0.7.7：正确 MIME —— 之前一律 image/{ext}，把 mp4 标成 image/mp4，蚂蚁 IMS 视频翻译判"非视频素材"拒收（MaterialNotExist）。
    _MIME = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif",
        "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "m4v": "video/x-m4v", "mkv": "video/x-matroska",
        "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "aac": "audio/aac",
    }
    ct = _MIME.get(ext, "application/octet-stream")
    try:
        with open(local, "rb") as f:
            data = f.read()
    except OSError as e:
        logger.warning("OSS upload_local 读文件失败 %s: %s", local, e)
        return None
    return await upload_bytes(data, ext=ext, content_type=ct, public=public)


async def upload_from_url(url: str, ext: str = "png") -> Optional[str]:
    """远程 URL（如火山 origin_url，会过期）→ 下载后传 OSS → 稳定签名 URL。"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
    except Exception as e:
        logger.warning("OSS upload_from_url 下载失败 %s: %s", str(url)[:80], e)
        return None
    return await upload_bytes(data, ext=ext)


async def upload_image_any(src: str) -> Optional[str]:
    """通用入口：本地 /output 路径 → upload_local；http(s) → upload_from_url；返回 OSS 签名 URL。"""
    if not src:
        return None
    if src.startswith(("http://", "https://")):
        return await upload_from_url(src)
    return await upload_local(src)
