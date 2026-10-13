import logging
import os
import sys
import uuid
import base64
import asyncio
import random
import httpx
from typing import Any, Dict, List

from app.modules.base import BaseModule
from app.core.config import settings
from app.core.paths import images_dir

logger = logging.getLogger(__name__)

# ─── 智谱 GLM-Image 全局并发节流 + 限速 ───
# Zhipu glm-image 有 RPM/QPS 限制（错误码 1302 "速率限制"）。并发=2 仍会被频控打爆（美术一次发 ~12 张图）→ 大量降级 mock。
# 改成**串行（并发=1）+ 相邻请求最小间隔**，从根上避免 1302；residual 由重试退避兜底。
_ZHIPU_SEMAPHORE = asyncio.Semaphore(1)
_ZHIPU_MIN_INTERVAL = 1.5          # 相邻智谱请求最小间隔（秒），防 1302 频控
_ZHIPU_PACE_LOCK = asyncio.Lock()
_ZHIPU_LAST_TS = 0.0


async def _zhipu_pace():
    """保证相邻智谱请求间隔 ≥ _ZHIPU_MIN_INTERVAL 秒（全局，防 1302 频控）。"""
    global _ZHIPU_LAST_TS
    async with _ZHIPU_PACE_LOCK:
        import time as _t
        wait = _ZHIPU_MIN_INTERVAL - (_t.monotonic() - _ZHIPU_LAST_TS)
        if wait > 0:
            await asyncio.sleep(wait)
        _ZHIPU_LAST_TS = _t.monotonic()


_FONT_CANDIDATES = (
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",          # 微软雅黑
    r"C:\Windows\Fonts\simsun.ttc",        # 宋体
    r"C:\Windows\Fonts\arial.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial.ttf",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)


def _load_default_font(size: int = 20):
    """跨平台加载一个可用字体；找不到时返回 PIL 默认字体"""
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


class TextToImageModule(BaseModule):
    """文生图模块：使用 Google Imagen 3 (Nano Banana 2) 生成图片

    API: Gemini 2.0 Flash Exp Image Generation (Imagen 3)
    优先调用真实 API，失败时降级到 mock 占位图
    """

    name = "text_to_image"
    description = "根据文本描述生成图片。输入为提示词，输出为生成的图片文件路径。使用 Google Imagen 3 (Nano Banana 2) 模型。"
    category = "basic"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "图片描述/提示词"
                },
                "negative_prompt": {
                    "type": "string",
                    "default": "",
                    "description": "负面提示词（不想出现的内容）"
                },
                "width": {
                    "type": "integer",
                    "default": 1024,
                    "description": "图片宽度"
                },
                "height": {
                    "type": "integer",
                    "default": 1024,
                    "description": "图片高度"
                },
                "style": {
                    "type": "string",
                    "enum": ["realistic", "anime", "3d", "oil", "sketch", "cinematic"],
                    "default": "realistic",
                    "description": "图片风格"
                },
                "seed": {
                    "type": "integer",
                    "default": 42,
                    "description": "随机种子"
                }
            },
            "required": ["prompt"]
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "description": "生成的图片文件路径"},
                "width": {"type": "integer"},
                "height": {"type": "integer"}
            },
            "required": ["image_url"]
        }

    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        prompt = input_data.get("prompt", "")
        width = input_data.get("width", 1024)
        height = input_data.get("height", 1024)
        style = input_data.get("style", "realistic")

        # 优先尝试 智谱 GLM-Image，其次是 Google Imagen 3，失败时降级到 mock
        from app.core.runtime_config import zhipu_image, google_image
        zhipu_cfg = zhipu_image()
        google_cfg = google_image()

        # 0.6.3+: 收集真实失败原因，mock 兜底时一并传给上层让用户看到
        fail_reasons: List[str] = []

        # 1. 尝试智谱 GLM-Image
        if zhipu_cfg.api_key:
            try:
                return await self._generate_with_zhipu(prompt, width, height, style)
            except Exception as e:
                err = str(e)[:300]
                # 内容审核类错误（1301 等）友好提示
                if "contentFilter" in err or "1301" in err or "content" in err.lower():
                    fail_reasons.append(
                        "智谱内容审核拒绝（可能含敏感/暴力/政治词汇）：" + err[:200]
                    )
                else:
                    fail_reasons.append("智谱失败：" + err[:200])
                logger.exception("智谱 GLM-Image 调用失败: %s", e)

        # 2. 尝试 Google Imagen 3
        if google_cfg.api_key:
            try:
                return await self._generate_with_imagen3(prompt, width, height, style)
            except Exception as e:
                err = str(e)[:200]
                fail_reasons.append("Google Imagen 失败：" + err)
                logger.warning("Imagen 3 API 调用失败，降级到mock: %s", e)

        # 3. mock 兜底 —— 把真实失败原因带回去，让上层节点 result.error 显示
        mock_result = await self._mock_generate(prompt, width, height, style)
        if fail_reasons:
            mock_result["error"] = " | ".join(fail_reasons)
            mock_result["mock_reason"] = fail_reasons[0]
        return mock_result

    async def _generate_with_imagen3(self, prompt: str, width: int, height: int, style: str) -> Dict[str, Any]:
        """使用 Google Imagen 3 (Gemini 3 Pro Image Preview) 生成图片"""
        from app.core.runtime_config import google_image
        cfg = google_image()
        api_key = cfg.api_key
        model = cfg.model
        base_url = cfg.base_url

        if not api_key:
            raise ValueError("未配置 Google API Key (GOOGLE_API_KEY)")

        # 构建增强提示词
        style_prefix = {
            "realistic": "photorealistic, highly detailed, 8k uhd, professional photography",
            "anime": "anime style, manga art, vibrant colors, cel shading",
            "3d": "3D render, octane render, blender, cinematic lighting",
            "oil": "oil painting, classical art style, rich textures",
            "sketch": "pencil sketch, hand drawn, monochrome, artistic",
            "cinematic": "cinematic shot, film grain, dramatic lighting, movie still"
        }.get(style, "")

        enhanced_prompt = f"{prompt}, {style_prefix}" if style_prefix else prompt

        # Gemini 2.0 Flash Exp Image Generation API
        url = f"{base_url}/models/{model}:generateContent?key={api_key}"

        payload = {
            "contents": [{
                "role": "user",
                "parts": [{"text": enhanced_prompt}]
            }],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "temperature": 0.7,
            }
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                )
                response.raise_for_status()
                data = response.json()
            except httpx.TimeoutException as exc:
                raise ValueError("Imagen 3 API 超时 (30s)") from exc

        # 解析返回的图片数据
        candidates = data.get("candidates", [])
        if not candidates:
            raise ValueError("Imagen 3 API 返回空 candidates")

        parts = candidates[0].get("content", {}).get("parts", [])
        image_data = None
        text_response = ""

        for part in parts:
            if "inlineData" in part:
                image_data = part["inlineData"]["data"]
            elif "text" in part:
                text_response = part["text"]

        if not image_data:
            raise ValueError("Imagen 3 API 返回结果中未找到图片数据")

        # 解码 base64 并保存
        output_path = os.path.join(images_dir(), f"img_{uuid.uuid4().hex[:8]}.png")

        image_bytes = base64.b64decode(image_data)
        with open(output_path, "wb") as f:
            f.write(image_bytes)

        return {
            "image_url": f"/output/images/{os.path.basename(output_path)}",
            "width": width,
            "height": height,
            "prompt": enhanced_prompt,
            "style": style,
            "mock": False,
            "model": "imagen-3",
            "text_response": text_response,
            # 0.6.5: 成本统计用
            "usage": {
                "provider": "google",
                "model": "imagen-3",
                "kind": "image",
                "images": 1,
                "size": f"{width}x{height}",
            },
        }

    async def _generate_with_zhipu(self, prompt: str, width: int, height: int, style: str) -> Dict[str, Any]:
        """使用智谱 GLM-Image 生成图片
        - 模型 glm-image
        - 尺寸：长宽 512-2048 之间，且为 32 的整数倍
        - prompt 长度上限约 1500 字符
        """
        from app.core.runtime_config import zhipu_image
        cfg = zhipu_image()
        api_key = cfg.api_key
        model = cfg.model
        base_url = cfg.base_url

        if not api_key:
            raise ValueError("未配置智谱 API Key (ZHIPU_API_KEY)")

        # 把宽高约束成 32 的整数倍 + 在 512-2048 范围内
        def _snap(v: int, lo: int = 512, hi: int = 2048) -> int:
            return max(lo, min(hi, (max(int(v), lo) // 32) * 32))

        w = _snap(width or 1024)
        h = _snap(height or 1024)
        size = f"{w}x{h}"

        # prompt 截断到 1400 字符内（Zhipu 上限约 1500）
        clean_prompt = (prompt or "").strip()[:1400]

        payload = {
            "model": model,
            "prompt": clean_prompt,
            "size": size,
        }

        # 通过全局 semaphore 限制并发，避免触发 Zhipu QPS 限流（错误码 1302）
        # 429 / 网络超时 / 连接错误都按指数退避重试 3 次（1s、3s、7s），失败再抛上层降级 mock
        max_retries = 5
        backoff_base = 1.0
        last_error: str = ""

        async with _ZHIPU_SEMAPHORE:
            for attempt in range(max_retries + 1):
                client = httpx.AsyncClient(timeout=120.0)
                try:
                    try:
                        await _zhipu_pace()
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
                            last_error = f"Zhipu 网络错误 ({type(net_err).__name__})，第 {attempt + 1}/{max_retries} 次退避 {wait:.1f}s"
                            logger.warning("%s: %s", last_error, net_err)
                            await asyncio.sleep(wait)
                            continue
                        raise ValueError(f"Zhipu 网络多次失败：{type(net_err).__name__}: {net_err}")

                    if response.status_code == 429 and attempt < max_retries:
                        wait = backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
                        last_error = f"Zhipu 429 限流，第 {attempt + 1}/{max_retries} 次退避 {wait:.1f}s"
                        logger.warning("%s", last_error)
                        await client.aclose()
                        await asyncio.sleep(wait)
                        continue

                    if response.status_code >= 400:
                        body = response.text[:500]
                        raise ValueError(
                            f"Zhipu 返回 {response.status_code}: {body} | "
                            f"model={model}, size={size}, prompt_len={len(clean_prompt)}"
                        )

                    data = response.json()
                    image_url = (data.get("data") or [{}])[0].get("url", "")
                    if not image_url:
                        raise ValueError(f"智谱 API 未返回图片 URL: {data}")

                    img_response = await client.get(image_url, timeout=60.0)
                    img_response.raise_for_status()
                    image_bytes = img_response.content
                    break  # 成功
                finally:
                    await client.aclose()
            else:
                # for-else：循环走完所有重试都没成功
                raise ValueError(f"Zhipu 多次重试失败：{last_error}")

        output_path = os.path.join(images_dir(), f"img_{uuid.uuid4().hex[:8]}.png")
        with open(output_path, "wb") as f:
            f.write(image_bytes)

        return {
            "image_url": f"/output/images/{os.path.basename(output_path)}",
            "width": w,
            "height": h,
            "prompt": clean_prompt,
            "style": style,
            "mock": False,
            "model": model,
            "provider": "zhipu",
            # 0.6.5: 成本统计用
            "usage": {
                "provider": "zhipu",
                "model": model,
                "kind": "image",
                "images": 1,
                "size": size,
            },
        }

    async def _mock_generate(self, prompt: str, width: int, height: int, style: str) -> Dict[str, Any]:
        """Mock 图片生成 — 用 Pillow 生成跨平台占位图"""
        output_path = os.path.join(images_dir(), f"img_{uuid.uuid4().hex[:8]}.png")

        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new("RGB", (width, height), color=(30, 30, 30))
            draw = ImageDraw.Draw(img)
            font = _load_default_font(20)
            label = f"[Mock Image]\n{prompt[:60]}"
            bbox = draw.textbbox((0, 0), label, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            x = (width - text_w) // 2
            y = (height - text_h) // 2
            draw.text((x, y), label, fill=(200, 200, 200), font=font)
            img.save(output_path)
        except Exception as e:
            logger.warning("Pillow mock 失败，写入最小有效 PNG: %s", e)
            # 至少写一个 1×1 的合法 PNG，避免 0 字节文件被下游用作图片输入而崩
            try:
                from PIL import Image as _Image
                _Image.new("RGB", (1, 1), color=(30, 30, 30)).save(output_path, format="PNG")
            except Exception:
                # 最后的兜底：写入硬编码的 1×1 灰色 PNG（67 字节合法 PNG）
                fallback_png = bytes.fromhex(
                    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
                    "53de0000000c4944415478da6364686200000000050001f0d3a8470000000049454e44ae426082"
                )
                with open(output_path, "wb") as f:
                    f.write(fallback_png)

        return {
            "image_url": f"/output/images/{os.path.basename(output_path)}",
            "width": width,
            "height": height,
            "prompt": prompt,
            "style": style,
            "mock": True
        }
