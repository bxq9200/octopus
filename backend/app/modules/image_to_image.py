import logging
import os
import uuid
import asyncio
import random
import httpx
from typing import Any, Dict, List

from app.modules.base import BaseModule
from app.core.config import settings
from app.core.paths import images_dir, image_to_data_url, resolve_output_path
from app.modules.text_to_image import _load_default_font

logger = logging.getLogger(__name__)

# ─── Seedream 4.0 全局并发 + 429 退避 ───
# 多个关键帧节点并发跑 / 用户连续确认时，避免把 ARK 配额打满
_SEEDREAM_SEMAPHORE = asyncio.Semaphore(2)


class ImageToImageModule(BaseModule):
    """图生图模块：根据参考图片生成风格化/修改后的图片

    用于：风格迁移、角色一致性保持（三视图）、分镜关键帧（多图融合：角色 + 场景）等。
    优先调用火山方舟 Seedream 4.0；失败时降级到 mock。

    支持两种输入：
    - `image_url: str` — 单图（兼容旧调用）
    - `image_urls: list[str]` — 多图融合（最多 5 张，按顺序作为参考）
    """

    name = "image_to_image"
    description = "根据一张或多张参考图片生成风格化/修改后的图片。多图模式下按顺序作为视觉参考（如：角色图+场景图融合）。"
    category = "basic"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": "单张参考图（兼容字段）。多图请用 image_urls。"
                },
                "image_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "多张参考图列表。Seedream 4.0 支持最多 5 张，按顺序融合。"
                },
                "prompt": {
                    "type": "string",
                    "description": "修改要求/构图描述。多图模式下可用 '图1/图2...' 指代参考图。"
                },
                "width": {
                    "type": "integer",
                    "default": 1024,
                    "description": "输出宽度"
                },
                "height": {
                    "type": "integer",
                    "default": 768,
                    "description": "输出高度"
                },
                "strength": {
                    "type": "number",
                    "default": 0.75,
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "修改强度（仅 mock 路径生效）"
                }
            },
            "required": ["prompt"]
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "description": "生成的新图片路径"},
                "width": {"type": "integer"},
                "height": {"type": "integer"}
            },
            "required": ["image_url"]
        }

    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        # 收集参考图：image_urls 优先，否则用 image_url 单图；都没有则报错
        image_urls = input_data.get("image_urls") or []
        if not image_urls and input_data.get("image_url"):
            image_urls = [input_data["image_url"]]
        image_urls = [u for u in image_urls if u]  # 去空
        if not image_urls:
            raise ValueError("image_to_image: 必须提供 image_url 或 image_urls")

        # Seedream 4.0 文档上限 5 张参考图
        image_urls = image_urls[:5]

        prompt = (input_data.get("prompt") or "").strip()[:1500]
        width = input_data.get("width", 1024)
        height = input_data.get("height", 768)
        strength = input_data.get("strength", 0.75)

        from app.core.runtime_config import seedream_image
        cfg = seedream_image()
        if cfg.api_key:
            try:
                return await self._generate_with_ark(image_urls, prompt, width, height, model_override=input_data.get("model"))
            except Exception as e:
                logger.warning("Ark Seedream 图生图调用失败，降级到 mock: %s", e)

        # mock 用第一张参考图作底图
        return await self._mock_generate(image_urls[0], prompt, strength, width, height)

    async def _generate_with_ark(self, image_urls: List[str], prompt: str, width: int, height: int, model_override: str = None) -> Dict[str, Any]:
        """调用火山方舟 Seedream 4.0/5.0 的 /images/generations，多图融合模式

        image 字段为数组时 Seedream 会按顺序融合多张参考图（例：角色图+场景图）。
        Seedream 5.0 (`doubao-seedream-5-0-*`) 要求图片像素 ≥ 3,686,400（≥1920×1920），
        所以这里按 model 名自动放大最小尺寸。
        """
        from app.core.runtime_config import seedream_image
        cfg = seedream_image()
        api_key = cfg.api_key
        base_url = cfg.base_url
        # model_override：关键帧专用(如 doubao-seedream-5-0-lite-260128，用于 Seedance 信任产物测试)；否则全局配置
        model = model_override or cfg.image_edit_model

        w, h = _snap_size_for_model(width or 1024, height or 1024, model)

        # 0.7.3 Seedream 5.0 多图参考**不支持 base64、必须公网 URL**(apiyi 文档 + 实测多角色串脸)。
        # 多图(关键帧多主体融合)时把本地图传 OSS 拿公网 URL；单图或 OSS 没配 → 回退 base64(单图 base64 仍可用)。
        async def _to_ref(u: str) -> str:
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                return u
            return await image_to_data_url(u)
        if len(image_urls) > 1:
            # 0.8 缝2：多图统一走数据面拿公网 URL（远程图透传、本地图上传、不可用则内部回退 base64）
            from app.core import media_gateway
            if not media_gateway.is_available():
                logger.warning("Seedream 多图(%d张)但数据面未配 → 回退 base64(5.0 多主体可能串脸)", len(image_urls))
            ref_urls = [await media_gateway.to_model_input(u) for u in image_urls]
        else:
            # 单图：base64 仍可用，省一次上传往返，维持原行为
            ref_urls = [await _to_ref(u) for u in image_urls]

        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "image": ref_urls if len(ref_urls) > 1 else ref_urls[0],
            "size": f"{w}x{h}",
            "response_format": "url",
            "watermark": False,
        }
        # 多图融合：禁用多图序列生成，确保只产出一张融合结果。
        # ⚠️ Seedream 5.0 不支持 sequential_image_generation 参数（发了直接 400 InvalidParameter，
        # 整个关键帧降级 mock→变角色图）；5.0 多图默认就是单张融合，故 5.0 跳过、仅 4.0 需要它强制单图。
        _mname = (model or "").lower()
        _is_sd5 = "seedream-5" in _mname or "seedream5" in _mname
        if len(ref_urls) > 1 and not _is_sd5:
            payload["sequential_image_generation"] = "disabled"

        max_retries = 3
        backoff_base = 1.0
        last_error = ""

        async with _SEEDREAM_SEMAPHORE:
            for attempt in range(max_retries + 1):
                async with httpx.AsyncClient(timeout=180.0) as client:
                    try:
                        response = await client.post(
                            f"{base_url}/images/generations",
                            json=payload,
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json",
                            },
                        )
                    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as net_err:
                        if attempt < max_retries:
                            wait = backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
                            last_error = f"Seedream 网络错误 ({type(net_err).__name__})，第 {attempt + 1}/{max_retries} 次退避 {wait:.1f}s"
                            logger.warning("%s: %s", last_error, net_err)
                            await asyncio.sleep(wait)
                            continue
                        raise ValueError(f"Seedream 网络多次失败：{type(net_err).__name__}: {net_err}")

                    if response.status_code == 429 and attempt < max_retries:
                        wait = backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
                        last_error = f"Seedream 429 限流，第 {attempt + 1}/{max_retries} 次退避 {wait:.1f}s"
                        logger.warning("%s", last_error)
                        await asyncio.sleep(wait)
                        continue
                    if response.status_code >= 400:
                        raise ValueError(
                            f"Ark Seedream 返回 {response.status_code}: {response.text[:500]} | "
                            f"model={model}, size={w}x{h}, refs={len(ref_urls)}"
                        )
                    remote_url = (response.json().get("data") or [{}])[0].get("url", "")
                    if not remote_url:
                        raise ValueError(f"Ark Seedream 未返回图片 URL: {response.text[:300]}")

                    img_response = await client.get(remote_url, timeout=60.0)
                    img_response.raise_for_status()
                    image_bytes = img_response.content
                    break
            else:
                raise ValueError(f"Seedream 多次重试失败：{last_error}")

        output_path = os.path.join(images_dir(), f"img2img_{uuid.uuid4().hex[:8]}.png")
        with open(output_path, "wb") as f:
            f.write(image_bytes)

        return {
            "image_url": f"/output/images/{os.path.basename(output_path)}",
            # 0.7.1 #5：保留 Seedream 原始火山产物 URL —— 喂 Seedance 时直接引用它(http 透传、不 base64),
            # 才算"信任模型产物"免真人审核;本地 image_url 仅供前端展示。勿压缩/转码(会失信任)。
            "origin_url": remote_url,
            "width": w,
            "height": h,
            "prompt": prompt,
            "references": image_urls,
            "reference_count": len(image_urls),
            "mock": False,
            "model": model,
            "provider": "ark",
            # 0.6.5: 成本统计用（一次调用 = 一张图，无论参考图多少张）
            "usage": {
                "provider": "ark",
                "model": model,
                "kind": "image",
                "images": 1,
                "size": f"{w}x{h}",
            },
        }

    async def _mock_generate(self, image_url: str, prompt: str, strength: float, width: int, height: int) -> Dict[str, Any]:
        """Mock 图生图 — 生成带参考信息的占位图"""
        output_path = os.path.join(images_dir(), f"img2img_{uuid.uuid4().hex[:8]}.png")

        try:
            from PIL import Image, ImageDraw, ImageEnhance

            ref_img = None
            local_ref = resolve_output_path(image_url)
            if local_ref and os.path.exists(local_ref):
                try:
                    ref_img = Image.open(local_ref)
                    ref_img = ref_img.resize((width, height), Image.LANCZOS)
                except Exception as e:
                    logger.debug("加载参考图失败: %s", e)

            if ref_img is None:
                img = Image.new("RGB", (width, height), color=(40, 40, 50))
            else:
                img = ImageEnhance.Brightness(ref_img.copy()).enhance(0.9 + strength * 0.2)

            draw = ImageDraw.Draw(img)
            font = _load_default_font(20)

            label = f"[Mock Image-to-Image]\nPrompt: {prompt[:50]}\nStrength: {strength}"
            bbox = draw.textbbox((0, 0), label, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            x = (width - text_w) // 2
            y = (height - text_h) // 2

            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rectangle([x-10, y-10, x+text_w+10, y+text_h+10], fill=(0, 0, 0, 128))
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

            draw = ImageDraw.Draw(img)
            draw.text((x, y), label, fill=(255, 255, 255), font=font)
            img.save(output_path)

        except Exception:
            with open(output_path, "wb") as f:
                f.write(b"")

        return {
            "image_url": f"/output/images/{os.path.basename(output_path)}",
            "width": width,
            "height": height,
            "prompt": prompt,
            "strength": strength,
            "reference": image_url,
            "mock": True,
        }


def _snap_size(v: int, lo: int = 512, hi: int = 2048, step: int = 16) -> int:
    """约束尺寸到 [lo, hi] 之内并对齐到 step 整数倍（Ark Seedream 4.0 要求 16 的整数倍）"""
    return max(lo, min(hi, (max(int(v), lo) // step) * step))


# Seedream 5.0 起最低 3,686,400 像素（≥1920×1920）
_SEEDREAM5_MIN_PIXELS = 3_686_400


def _snap_size_for_model(w: int, h: int, model: str) -> tuple:
    """按 model 决定 (w, h)。Seedream 5.0 系自动放大到满足最低像素，aspect ratio 保留。"""
    name = (model or "").lower()
    is_seedream_5 = "seedream-5" in name or "seedream5" in name
    # Seedream 5.0 文档支持到 4K；4.0 的旧上限 2048 对 4.0 仍生效
    hi = 4096 if is_seedream_5 else 2048
    w0 = _snap_size(int(w or 1024), hi=hi)
    h0 = _snap_size(int(h or 1024), hi=hi)
    if is_seedream_5 and (w0 * h0) < _SEEDREAM5_MIN_PIXELS:
        # 按总像素放大，保留宽高比 + 5% buffer 防 snap 后边界值
        import math
        scale = math.sqrt(_SEEDREAM5_MIN_PIXELS * 1.05 / max(1, w0 * h0))
        w_new = _snap_size(int(round(w0 * scale)), hi=hi)
        h_new = _snap_size(int(round(h0 * scale)), hi=hi)
        # 还不够就再加 16
        while w_new * h_new < _SEEDREAM5_MIN_PIXELS and (w_new < hi or h_new < hi):
            w_new = _snap_size(w_new + 16, hi=hi)
            h_new = _snap_size(h_new + 16, hi=hi)
        return w_new, h_new
    return w0, h0
