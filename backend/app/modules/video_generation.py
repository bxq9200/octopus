import asyncio
import logging
import os
import uuid
import base64
import httpx
from typing import Any, Dict, Optional, List

from app.modules.base import BaseModule
from app.core.config import settings
from app.core.paths import videos_dir

logger = logging.getLogger(__name__)


class VideoGenerationModule(BaseModule):
    """视频生成模块（executor 主用）：根据图片 + 文本生成视频片段。

    多 provider 调度（按 user_settings.video.provider）：
      - ark       → 火山方舟 Seedance 2.0 多模态（支持首帧 / 尾帧 / 自动音频）
      - dashscope → 阿里云 HappyHorse i2v（首帧 + prompt）或 wanx t2v（纯 prompt）；
                    DashScope 路径不支持尾帧 / 自动音频，相关字段会被忽略

    用于：分镜视频生成、片段创作、关键帧动画化等
    """

    name = "video_generation"
    description = (
        "根据图片和文本描述生成高质量视频片段。"
        "多 provider：火山方舟 Seedance / 阿里云 HappyHorse·Wanx。"
        "Seedance 路径支持首帧/尾帧 + 自动音频；HappyHorse 路径仅支持首帧 + prompt。"
    )
    category = "advanced"

    # 轮询配置（运行时从 settings 动态读取 API 配置，避免类加载时冻结）
    POLL_INTERVAL = 5  # 秒
    MAX_POLL_ATTEMPTS = 120  # 最多轮询 10 分钟

    @property
    def API_KEY(self) -> str:
        from app.core.runtime_config import ark_config
        return ark_config().api_key

    @property
    def BASE_URL(self) -> str:
        from app.core.runtime_config import ark_config
        return ark_config().base_url

    @property
    def MODEL(self) -> str:
        from app.core.runtime_config import ark_config
        return ark_config().video_model

    @property
    def FAST_MODEL(self) -> str:
        from app.core.runtime_config import ark_config
        return ark_config().video_fast_model

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "视频内容描述，包含场景、动作、氛围、声音等"
                },
                "first_frame": {
                    "type": "string",
                    "description": "首帧图片路径或 URL（可选）",
                    "default": ""
                },
                "last_frame": {
                    "type": "string",
                    "description": "尾帧图片路径或 URL（可选）",
                    "default": ""
                },
                "ratio": {
                    "type": "string",
                    "enum": ["16:9", "9:16", "1:1", "4:3", "3:4"],
                    "default": "9:16",
                    "description": "视频宽高比"
                },
                "duration": {
                    "type": "integer",
                    "enum": [5, 6, 10],
                    "default": 6,
                    "description": "视频时长（秒）"
                },
                "resolution": {
                    "type": "string",
                    "enum": ["480p", "720p"],
                    "default": "720p",
                    "description": "视频分辨率"
                },
                "generate_audio": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否自动生成音频"
                },
                "watermark": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否添加水印"
                },
                "seed": {
                    "type": "integer",
                    "default": -1,
                    "description": "随机种子，-1 表示随机"
                },
                "use_fast_model": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否使用快速模型（seedance 1.0 pro fast）"
                }
            },
            "required": ["prompt"]
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "video_url": {"type": "string", "description": "生成的视频文件本地路径"},
                "video_download_url": {"type": "string", "description": "视频下载链接（24小时有效）"},
                "task_id": {"type": "string", "description": "Seedance 任务ID"},
                "duration": {"type": "integer"},
                "ratio": {"type": "string"},
                "resolution": {"type": "string"},
                "has_audio": {"type": "boolean"},
                "prompt": {"type": "string"},
                "model": {"type": "string"}
            },
            "required": ["video_url", "task_id"]
        }

    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        prompt = input_data["prompt"]
        first_frame = input_data.get("first_frame", "")
        last_frame = input_data.get("last_frame", "")
        ratio = input_data.get("ratio", "9:16")
        duration = input_data.get("duration", 6)
        resolution = input_data.get("resolution", "720p")
        generate_audio = input_data.get("generate_audio", True)
        watermark = input_data.get("watermark", False)
        seed = input_data.get("seed", -1)
        use_fast_model = input_data.get("use_fast_model", False)
        progress_cb = input_data.get("_progress_callback")

        # ─── 按 user_settings.video.provider 分发 ───
        from app.core.runtime_config import active_video_provider_name
        if active_video_provider_name() == "dashscope":
            from app.modules._dashscope_video import (
                generate_image_to_video as ds_i2v,
                generate_text_to_video as ds_t2v,
            )
            # DashScope 路径不支持尾帧 / 多模态参考；last_frame 会被忽略
            if last_frame:
                logger.info("DashScope 路径不支持 last_frame，已忽略")
            if first_frame:
                result = await ds_i2v(
                    first_frame, prompt,
                    duration=int(duration) or 5,
                    resolution=resolution,
                    seed=seed,
                    progress_cb=progress_cb,
                )
            else:
                # 纯文生视频 — 需配置 dashscope_t2v_model
                result = await ds_t2v(
                    prompt,
                    duration=int(duration) or 5,
                    resolution=resolution,
                    seed=seed,
                    progress_cb=progress_cb,
                )
            # 统一 output 字段到 VideoGenerationModule.output_schema
            return {
                "video_url": result.get("video_url", ""),
                "video_download_url": result.get("video_url", ""),
                "task_id": result.get("task_id", ""),
                "duration": result.get("duration", duration),
                "ratio": ratio,
                "resolution": resolution,
                "has_audio": False,  # HappyHorse / Wanx 当前实现不带音频
                "prompt": prompt,
                "model": result.get("model", ""),
                "provider": "dashscope",
            }

        if active_video_provider_name() == "comfyui":
            from app.modules._comfyui_video import (
                generate_image_to_video as cu_i2v,
                generate_text_to_video as cu_t2v,
            )
            if last_frame:
                logger.info("ComfyUI(单首帧) 路径暂忽略 last_frame（首尾帧留 0.7.1）")
            if first_frame:
                result = await cu_i2v(first_frame, prompt, duration=int(duration) or 5,
                                      resolution=resolution, seed=seed, progress_cb=progress_cb)
            else:
                result = await cu_t2v(prompt, duration=int(duration) or 5,
                                      resolution=resolution, seed=seed, progress_cb=progress_cb)
            return {
                "video_url": result.get("video_url", ""),
                "video_download_url": result.get("video_url", ""),
                "task_id": result.get("task_id", ""),
                "duration": result.get("duration", duration),
                "ratio": ratio,
                "resolution": resolution,
                "has_audio": False,
                "prompt": prompt,
                "model": result.get("model", ""),
                "provider": "comfyui",
            }

        # ─── 默认 provider == "ark"：Seedance 多模态 ───
        model = self.FAST_MODEL if use_fast_model else self.MODEL

        # 构建 content 数组
        content = []

        # 首帧图片
        if first_frame:
            first_frame_b64 = await self._get_image_base64(first_frame)
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{first_frame_b64}"},
                "role": "first_frame"
            })

        # 文本提示词
        content.append({
            "type": "text",
            "text": prompt
        })

        # 尾帧图片
        if last_frame:
            last_frame_b64 = await self._get_image_base64(last_frame)
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{last_frame_b64}"},
                "role": "last_frame"
            })

        # 构建请求体
        payload = {
            "model": model,
            "content": content,
            "ratio": ratio,
            "duration": duration,
            "resolution": resolution,
            "generate_audio": generate_audio,
            "watermark": watermark,
            "seed": seed
        }

        headers = {
            "Authorization": f"Bearer {self.API_KEY}",
            "Content-Type": "application/json"
        }

        # 1. 创建任务
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.BASE_URL}/contents/generations/tasks",
                headers=headers,
                json=payload
            )
            resp.raise_for_status()
            data = resp.json()

        task_id = data.get("id")
        if not task_id:
            raise ValueError(f"Seedance API 未返回任务ID: {data}")

        # 2. 轮询任务状态
        video_url = await self._poll_task(task_id, headers)

        # 3. 下载视频到本地
        local_path = await self._download_video(video_url)

        return {
            "video_url": local_path,
            "video_download_url": video_url,
            "task_id": task_id,
            "duration": duration,
            "ratio": ratio,
            "resolution": resolution,
            "has_audio": generate_audio,
            "prompt": prompt,
            "model": model,
            "provider": "ark",
        }

    async def _poll_task(self, task_id: str, headers: Dict[str, str]) -> str:
        """轮询查询任务状态，直到完成或失败"""
        async with httpx.AsyncClient(timeout=30) as client:
            for i in range(self.MAX_POLL_ATTEMPTS):
                await asyncio.sleep(self.POLL_INTERVAL)

                resp = await client.get(
                    f"{self.BASE_URL}/contents/generations/tasks/{task_id}",
                    headers=headers
                )
                resp.raise_for_status()
                data = resp.json()

                status = data.get("status", "unknown")

                if status == "succeeded":
                    content = data.get("content", {})
                    if isinstance(content, dict):
                        video_url = content.get("video_url")
                        if video_url:
                            return video_url
                    raise ValueError(f"任务完成但未返回视频URL: {data}")

                elif status in ["failed", "cancelled"]:
                    raise RuntimeError(f"任务失败: {data}")

            # 超时
            raise TimeoutError(f"任务轮询超时 ({self.MAX_POLL_ATTEMPTS * self.POLL_INTERVAL}秒)")

    async def _download_video(self, video_url: str) -> str:
        """下载视频到本地输出目录，返回前端可访问的 /output/videos/... URL"""
        output_path = os.path.join(videos_dir(), f"seedance_{uuid.uuid4().hex[:8]}.mp4")

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(video_url)
            resp.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(resp.content)

        return f"/output/videos/{os.path.basename(output_path)}"

    async def _get_image_base64(self, image_path: str) -> str:
        """获取图片的 base64 编码，支持本地路径和 URL"""
        if image_path.startswith("http://") or image_path.startswith("https://"):
            # 下载远程图片
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(image_path)
                resp.raise_for_status()
                return base64.b64encode(resp.content).decode()
        else:
            # 本地文件
            if not os.path.exists(image_path):
                raise FileNotFoundError(f"图片文件不存在: {image_path}")
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode()

    @classmethod
    async def generate_from_storyboard(
        cls,
        prompt: str,
        first_frame_path: Optional[str] = None,
        last_frame_path: Optional[str] = None,
        ratio: str = "9:16",
        duration: int = 6,
        **kwargs
    ) -> Dict[str, Any]:
        """便捷方法：从分镜信息生成视频"""
        module = cls()
        input_data = {
            "prompt": prompt,
            "first_frame": first_frame_path or "",
            "last_frame": last_frame_path or "",
            "ratio": ratio,
            "duration": duration,
            **kwargs
        }
        return await module.run(input_data)
