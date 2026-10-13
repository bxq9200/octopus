"""0.8 缝2：媒体数据面抽象 —— "把本地媒体变成模型/厂商能取到的 URL"。

统一了此前散在各 module 里的 "试 OSS，不行就 base64" 手写逻辑，并按部署模式分叉：

- `local`（默认）= 静态 OSS key 直传（现状 `oss_client`），行为与拆分前一致
- `cloud_client` = 服务端签发 STS 直传（**0.9 实现**；0.8 是 stub，见 `_CloudSTSBackend`）

═══ 两条铁律（都是踩出来的，改之前先读 ═══

**铁律1：远程 http(s) URL 默认原样透传，绝不重新托管。**
Seedance 2.0 的真人审核靠"同账号信任产物"放行——关键帧是火山 Seedream 出的图、喂回去时必须是
**火山原始 TOS URL**；一旦下载下来重传到我们自己的 OSS，就变成"第三方图"被重新拦，写实人脸直接
生产阻断。所以 `rehost_remote=False` 是默认值。
例外：`register_asset`（蚂蚁 asset 入库）要 `rehost_remote=True`，因为火山 origin_url 24h 过期、
蚂蚁那边异步下载会 403 DownloadFailed，必须先落到我们自己的 OSS 拿稳定链接。

**铁律2：拿不到公网 URL 时回退 base64，不要直接失败。**
OSS 没配的开发机必须还能跑。`to_model_input()` 因此永不返回 None。
但注意 base64 有真实代价：大图会炸蚂蚁网关的 HmacSHA1 签名；Seedream 5.0 多图参考不认 base64
（会串脸）。所以能拿 URL 就别用 base64——这也正是 0.8 把首帧从 base64 改走数据面的原因。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_REMOTE_PREFIXES = ("http://", "https://")


def _is_remote(src: str) -> bool:
    return isinstance(src, str) and src.startswith(_REMOTE_PREFIXES)


def _is_data_url(src: str) -> bool:
    return isinstance(src, str) and src.startswith("data:")


# ═══════════════════════════════════════════════════════
# 后端实现
# ═══════════════════════════════════════════════════════

class _LocalOSSBackend:
    """local：用户自己的静态 OSS key（现状）。"""

    name = "local_oss"

    def is_available(self) -> bool:
        from app.core.oss_client import is_configured
        return is_configured()

    async def put_local(self, path: str, *, public: bool = False) -> Optional[str]:
        from app.core.oss_client import upload_local
        return await upload_local(path, public=public)

    async def put_remote(self, url: str) -> Optional[str]:
        from app.core.oss_client import upload_from_url
        return await upload_from_url(url)


class _CloudSTSBackend:
    """cloud_client：把媒体（图片）经云端上传到**平台 OSS**，返回公网 URL。

    让本地执行器（BYOK 图片生成）**无需自建 OSS** 也能给 Seedream 5.0 等厂商喂公网参考图。
    图片很小（几 MB），字节走一趟云端换用户零配置；企业 OSS 凭据只在云端、客户端拿不到。
        客户端 base64 → POST {gateway}/v1/media/upload（带用户 token）→ 云端传平台 OSS → 回 public_url
    没登录/没网关 → is_available()=False → 调用方自动回退 base64（铁律2），不会硬崩。
    """

    name = "cloud_oss"

    def is_available(self) -> bool:
        try:
            from app.core.runtime_config import octopus_cloud_config, deployment_mode
            from app.core import identity
            if deployment_mode() != "cloud_client":
                return False
            cfg = octopus_cloud_config()
            return bool(getattr(cfg, "gateway_url", "") and identity.cloud_token())
        except Exception:
            return False

    async def _upload_data_url(self, data_url: str) -> Optional[str]:
        from app.core.runtime_config import octopus_cloud_config
        from app.core import identity, cloud_http
        cfg = octopus_cloud_config()
        try:
            r = await cloud_http.post(
                f"{cfg.gateway_url.rstrip('/')}/v1/media/upload",
                json={"data": data_url},
                headers={"Authorization": f"Bearer {identity.cloud_token()}"},
                timeout=60.0,
            )
            if r.status_code == 200:
                return (r.json() or {}).get("public_url") or None
            logger.warning("media_gateway[cloud_oss]: 上传失败 %s %s", r.status_code, r.text[:150])
        except Exception as e:
            logger.warning("media_gateway[cloud_oss]: 上传异常 %s", e)
        return None

    async def put_local(self, path: str, *, public: bool = False) -> Optional[str]:
        import os
        import base64
        from app.core.paths import resolve_output_path
        local = resolve_output_path(path) if isinstance(path, str) and path.startswith("/output/") else path
        if not local or not os.path.isfile(local):
            logger.warning("media_gateway[cloud_oss]: 文件不存在 %s", local)
            return None
        ext = (os.path.splitext(local)[1].lstrip(".") or "png").lower()
        with open(local, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return await self._upload_data_url(f"data:image/{ext};base64,{b64}")

    async def put_remote(self, url: str) -> Optional[str]:
        # 远程 URL 已是公网，厂商能直接抓 → 原样透传
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
        return None


_LOCAL_BACKEND = _LocalOSSBackend()
_CLOUD_BACKEND = _CloudSTSBackend()


def _backend():
    # 2026-07-22：OSS 改判 BYOK（用户自己的存储）→ **只要本地 OSS 配了就用它**（不论 local/cloud 模式）。
    # 原来 cloud 模式强选 cloud_sts（STS 数据面 stub、不可用）→ 首帧传不上公网、回退 base64/本地路径
    # → 厂商报 image_url 无效。cloud_sts 留作 OSS 未配时的未来出路（STS 建好再启用）。
    try:
        if _LOCAL_BACKEND.is_available():
            return _LOCAL_BACKEND
    except Exception:
        pass
    return _CLOUD_BACKEND


def backend_name() -> str:
    """当前数据面后端名（诊断/日志用）。"""
    return _backend().name


def is_available() -> bool:
    """数据面是否可用（= 能不能拿到公网 URL）。不可用时调用方应走 base64 兜底。"""
    try:
        return _backend().is_available()
    except Exception as e:
        logger.warning("media_gateway.is_available 异常: %s", e)
        return False


# ═══════════════════════════════════════════════════════
# 公开 API
# ═══════════════════════════════════════════════════════

async def to_public_url(src: str, *, public: bool = False,
                        rehost_remote: bool = False) -> Optional[str]:
    """把 `src` 变成公网可取的 URL；拿不到返回 None（**不**回退 base64）。

    src：本地路径 / `/output/...` / http(s) URL / data: URL
    public：`True` 返回无签名 public-read 干净 URL（蚂蚁 IMS 视频翻译只认这种），否则 1h 签名 URL
    rehost_remote：远程 URL 是否也下载后重新托管。**默认 False**（铁律1）。
    """
    if not src or not isinstance(src, str):
        return None
    if _is_data_url(src):
        return None                      # data: 本来就不是公网 URL，交给调用方兜底
    if _is_remote(src) and not rehost_remote:
        return src                       # 铁律1：原样透传
    if not is_available():
        return None

    b = _backend()
    try:
        if _is_remote(src):
            return await b.put_remote(src)
        return await b.put_local(src, public=public)
    except Exception as e:
        logger.warning("media_gateway.to_public_url 失败(%s) %s: %s", b.name, str(src)[:80], e)
        return None


async def to_model_input(src: str, *, public: bool = False,
                         rehost_remote: bool = False) -> str:
    """喂给模型/厂商的引用串：优先公网 URL，拿不到才回退 base64 data URL（铁律2，永不返 None）。

    这是绝大多数调用方该用的入口 —— 首帧、Seedream 参考图、蚂蚁 asset 入库都走它。
    """
    if not src or not isinstance(src, str):
        return src
    if _is_data_url(src):
        return src

    url = await to_public_url(src, public=public, rehost_remote=rehost_remote)
    if url:
        return url

    # 回退：本地文件 → base64；远程 URL 原样（重托管失败时至少还能用原 URL）
    if _is_remote(src):
        return src
    from app.core.paths import image_to_data_url
    return await image_to_data_url(src)
