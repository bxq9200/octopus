import asyncio
import logging
import os
import uuid
import httpx
from typing import Any, Dict, Optional

from app.modules.base import BaseModule
from app.core.config import settings
from app.core.paths import videos_dir

logger = logging.getLogger(__name__)

# ─── Seedance（ARK provider）任务创建并发节流 ───
# 仅作用于 ark 路径：text_to_video / image_to_video 都打同一个 ARK key 的
# contents/generations/tasks 接口，一次性提交太多任务会触发限流。提交完成后的 polling 不占名额。
# DashScope provider 有独立的 semaphore（在 _dashscope_video.py 内）。
SEEDANCE_SUBMIT_SEMAPHORE = asyncio.Semaphore(2)


class TextToVideoModule(BaseModule):
    """文生视频 / 图生视频模块：根据文本（或文本 + 首帧/尾帧/参考视频/音频）生成视频。

    多 provider 调度（按 user_settings.video.provider）：
      - ark       → 火山方舟 Seedance 2.0（_generate_with_seedance，支持首尾帧 / 参考视频 / 参考音频等多模态）
      - dashscope → 阿里云 HappyHorse i2v（首帧+prompt）或 wanx t2v（纯 prompt）
                    DashScope 路径不支持首尾帧 / 参考视频 / 参考音频，相关字段会被忽略
    所有模型 ID 从 user_settings 读，支持热切换。任一 provider 失败统一降级到 mock 占位视频。
    """

    name = "text_to_video"
    description = "文生视频 / 图生视频。多 provider 支持：火山方舟 Seedance / 阿里云 HappyHorse·Wanx。"
    category = "advanced"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "视频描述/提示词，描述期望生成的视频内容"
                },
                "first_frame_url": {
                    "type": "string",
                    "default": "",
                    "description": "首帧图片URL或本地路径（可选，用于图生视频）"
                },
                "last_frame_url": {
                    "type": "string",
                    "default": "",
                    "description": "尾帧图片URL或本地路径（可选，用于首尾帧生视频）"
                },
                "reference_video_url": {
                    "type": "string",
                    "default": "",
                    "description": "参考视频URL（可选，仅seedance 2.0支持）"
                },
                "reference_audio_url": {
                    "type": "string",
                    "default": "",
                    "description": "参考音频URL（可选，仅seedance 2.0支持，需配合图片或视频）"
                },
                "ratio": {
                    "type": "string",
                    "enum": ["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"],
                    "default": "16:9",
                    "description": "视频宽高比"
                },
                "duration": {
                    "type": "integer",
                    "default": 5,
                    "description": "视频时长（秒），seedance 2.0支持4-15秒，或-1让模型自主选择"
                },
                "resolution": {
                    "type": "string",
                    "enum": ["480p", "720p", "1080p"],
                    "default": "720p",
                    "description": "视频分辨率"
                },
                "seed": {
                    "type": "integer",
                    "default": -1,
                    "description": "随机种子，-1表示随机"
                },
                "generate_audio": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否生成同步音频（仅seedance 2.0/1.5 pro支持）"
                },
                "watermark": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否添加水印"
                },
                "model": {
                    "type": "string",
                    "default": "",
                    "description": "指定模型ID，空字符串则使用系统默认配置"
                },
                "fast_mode": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否使用fast模式（速度更快，质量略低）"
                }
            },
            "required": ["prompt"]
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "video_url": {"type": "string", "description": "生成的视频文件路径或URL"},
                "task_id": {"type": "string", "description": "任务ID"},
                "duration": {"type": "integer"},
                "ratio": {"type": "string"},
                "resolution": {"type": "string"},
                "seed": {"type": "integer"}
            },
            "required": ["video_url", "task_id"]
        }

    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        prompt = input_data.get("prompt", "")
        first_frame = input_data.get("first_frame_url", "")
        last_frame = input_data.get("last_frame_url", "")
        ref_video = input_data.get("reference_video_url", "")
        ref_audio = input_data.get("reference_audio_url", "")
        ratio = input_data.get("ratio", "16:9")
        duration = input_data.get("duration", 5)
        resolution = input_data.get("resolution", "720p")
        seed = input_data.get("seed", -1)
        generate_audio = input_data.get("generate_audio", True)
        watermark = input_data.get("watermark", False)
        model_override = input_data.get("model", "")
        fast_mode = input_data.get("fast_mode", False)
        progress_cb = input_data.get("_progress_callback")

        # Seedance 2.0 要求 duration ∈ [4, 12]
        clamped_duration = max(4, min(12, int(duration) or 5))

        # Provider 分发：ark / dashscope
        from app.core.runtime_config import active_video_provider_name
        provider = active_video_provider_name()
        if provider == "dashscope":
            try:
                from app.modules._dashscope_video import (
                    generate_image_to_video as ds_i2v,
                    generate_text_to_video as ds_t2v,
                )
                # 720x1280 / 1280x720 等供 dashscope 内部归一化用
                if first_frame:
                    return await ds_i2v(
                        first_frame, prompt,
                        duration=clamped_duration,
                        resolution=resolution,
                        seed=seed,
                        progress_cb=progress_cb,
                    )
                # 文生视频（dashscope_t2v_model 必须配置才行）
                return await ds_t2v(
                    prompt,
                    duration=clamped_duration,
                    resolution=resolution,
                    seed=seed,
                    progress_cb=progress_cb,
                )
            except Exception as e:
                logger.warning("DashScope 视频生成失败，降级 mock: %s", e)
                mock_result = await self._mock_generate(prompt, ratio, clamped_duration)
                mock_result["mock_reason"] = f"DashScope 调用失败: {str(e)[:300]}"
                return mock_result

        if provider == "comfyui":
            try:
                from app.modules._comfyui_video import (
                    generate_image_to_video as cu_i2v,
                    generate_text_to_video as cu_t2v,
                )
                if first_frame:
                    return await cu_i2v(
                        first_frame, prompt,
                        duration=clamped_duration, resolution=resolution, seed=seed,
                        ratio=ratio, progress_cb=progress_cb,
                        dialogue=input_data.get("dialogue") or "",
                        singing=bool(input_data.get("singing")),
                    )
                return await cu_t2v(
                    prompt,
                    duration=clamped_duration, resolution=resolution, seed=seed,
                    ratio=ratio, progress_cb=progress_cb,
                    dialogue=input_data.get("dialogue") or "",   # 同上：<d> 台词通道
                    singing=bool(input_data.get("singing")),
                    # 无首帧时 H3 靠这些参考图锁人物（ref2v）；其它 provider 忽略
                    reference_images=input_data.get("reference_images") or [],
                )
            except Exception as e:
                logger.warning("ComfyUI 视频生成失败，降级 mock: %s", e)
                mock_result = await self._mock_generate(prompt, ratio, clamped_duration)
                mock_result["mock_reason"] = f"ComfyUI 调用失败: {str(e)[:300]}"
                return mock_result

        if provider == "antchain":
            try:
                from app.modules._antchain_video import (
                    generate_image_to_video as ac_i2v,
                    generate_text_to_video as ac_t2v,
                )
                if first_frame:
                    return await ac_i2v(
                        first_frame, prompt,
                        duration=clamped_duration, resolution=resolution,
                        progress_cb=progress_cb,
                    )
                return await ac_t2v(
                    prompt,
                    duration=clamped_duration, resolution=resolution, ratio=ratio,
                    progress_cb=progress_cb,
                )
            except Exception as e:
                logger.warning("蚂蚁数科视频生成失败，降级 mock: %s", e)
                mock_result = await self._mock_generate(prompt, ratio, clamped_duration)
                mock_result["mock_reason"] = f"蚂蚁数科调用失败: {str(e)[:300]}"
                return mock_result

        # 0.8 缝1：Octopus 云端中转（本机不持企业 key，网关验 token → 转发 → 扣 credits）
        if provider == "octopus_cloud":
            try:
                from app.modules._octopus_cloud_video import (
                    generate_image_to_video as oc_i2v,
                    generate_text_to_video as oc_t2v,
                )
                if first_frame:
                    return await oc_i2v(
                        first_frame, prompt,
                        duration=clamped_duration, resolution=resolution, seed=seed,
                        progress_cb=progress_cb,
                    )
                return await oc_t2v(
                    prompt,
                    duration=clamped_duration, resolution=resolution, ratio=ratio, seed=seed,
                    progress_cb=progress_cb,
                )
            except Exception as e:
                logger.warning("Octopus 云端中转视频生成失败，降级 mock: %s", e)
                mock_result = await self._mock_generate(prompt, ratio, clamped_duration)
                mock_result["mock_reason"] = f"Octopus 云端中转调用失败: {str(e)[:300]}"
                return mock_result

        try:
            return await self._generate_with_seedance(
                prompt, first_frame, last_frame, ref_video, ref_audio,
                ratio, clamped_duration, resolution, seed, generate_audio, watermark,
                model_override, fast_mode, progress_cb=progress_cb,
            )
        except Exception as e:
            logger.warning("Seedance API 调用失败，降级到 mock: %s", e)
            mock_result = await self._mock_generate(prompt, ratio, clamped_duration)
            mock_result["mock_reason"] = f"Seedance 调用失败: {str(e)[:300]}"
            return mock_result

    async def _generate_with_seedance(
        self, prompt: str, first_frame: str, last_frame: str,
        ref_video: str, ref_audio: str,
        ratio: str, duration: int, resolution: str, seed: int,
        generate_audio: bool, watermark: bool,
        model_override: str, fast_mode: bool,
        progress_cb=None,
    ) -> Dict[str, Any]:
        """使用火山方舟 Seedance 2.0 生成视频"""
        from app.core.runtime_config import ark_config
        cfg = ark_config()
        api_key = cfg.api_key
        base_url = cfg.base_url

        if not api_key:
            raise ValueError("未配置火山方舟 API Key (SEEDANCE_API_KEY)")

        # 选择模型
        if model_override:
            model_id = model_override
        elif fast_mode:
            model_id = cfg.video_fast_model
        else:
            model_id = cfg.video_model

        # 构建 content 数组
        content = [{"type": "text", "text": prompt}]

        # 首帧图
        if first_frame:
            image_url = await self._prepare_media_url(first_frame)
            if last_frame:
                # 首尾帧模式
                content.append({
                    "type": "image_url",
                    "image_url": {"url": image_url},
                    "role": "first_frame"
                })
                last_url = await self._prepare_media_url(last_frame)
                content.append({
                    "type": "image_url",
                    "image_url": {"url": last_url},
                    "role": "last_frame"
                })
            else:
                # 首帧模式
                content.append({
                    "type": "image_url",
                    "image_url": {"url": image_url},
                    "role": "first_frame"
                })

        # 参考视频
        if ref_video:
            video_url = await self._prepare_media_url(ref_video)
            content.append({
                "type": "video_url",
                "video_url": {"url": video_url},
                "role": "reference_video"
            })

        # 参考音频（需配合图片或视频）
        if ref_audio and (first_frame or ref_video):
            audio_url = await self._prepare_media_url(ref_audio)
            content.append({
                "type": "audio_url",
                "audio_url": {"url": audio_url},
                "role": "reference_audio"
            })

        # 构建请求体
        payload = {
            "model": model_id,
            "content": content,
            "ratio": ratio,
            "duration": duration,
            "resolution": resolution,
            "seed": seed,
            "generate_audio": generate_audio,
            "watermark": watermark,
        }

        # 创建任务（提交受全局 semaphore 控制；polling 不占名额）
        create_url = f"{base_url}/contents/generations/tasks"
        async with SEEDANCE_SUBMIT_SEMAPHORE:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    create_url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}"
                    }
                )
                response.raise_for_status()
                create_data = response.json()

        task_id = create_data.get("id")
        if not task_id:
            raise ValueError(f"创建任务失败，未返回任务ID: {create_data}")

        logger.info("Seedance 任务创建成功: %s", task_id)

        # 轮询查询任务状态
        video_url = await self._poll_task(task_id, api_key, base_url, progress_cb=progress_cb)

        # 下载视频到本地
        output_path = os.path.join(videos_dir(), f"vid_{uuid.uuid4().hex[:8]}.mp4")

        async with httpx.AsyncClient(timeout=120.0) as client:
            video_response = await client.get(video_url)
            video_response.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(video_response.content)

        return {
            "video_url": f"/output/videos/{os.path.basename(output_path)}",
            "task_id": task_id,
            "duration": duration,
            "ratio": ratio,
            "resolution": resolution,
            "seed": seed,
            "mock": False,
            "model": model_id,
            # 0.6.5: 成本统计用（秒数按请求 duration，实际生成时长偏差留 0.6.6 用 ffprobe 修正）
            "usage": {
                "provider": "ark",
                "model": model_id,
                "kind": "video",
                "seconds": int(duration or 0),
                "resolution": str(resolution or "720p"),
            },
        }

    async def _poll_task(
        self,
        task_id: str,
        api_key: str,
        base_url: str,
        max_attempts: int = 120,
        interval: float = 5.0,
        progress_cb=None,
    ) -> str:
        """轮询查询任务状态，直到完成或失败（默认 5s × 120 = 10 分钟）"""
        query_url = f"{base_url}/contents/generations/tasks/{task_id}"

        async def _emit(progress: float, message: str):
            if not progress_cb:
                return
            try:
                cb_result = progress_cb(progress, message)
                if asyncio.iscoroutine(cb_result):
                    await cb_result
            except Exception:
                pass

        await _emit(2.0, "提交任务成功，等待排队...")

        for i in range(max_attempts):
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    query_url,
                    headers={"Authorization": f"Bearer {api_key}"}
                )
                response.raise_for_status()
                data = response.json()

            status = data.get("status")
            logger.debug("任务 %s 状态: %s (%d/%d)", task_id, status, i + 1, max_attempts)

            if status == "succeeded":
                await _emit(100.0, "任务完成，正在下载视频...")
                video_url = data.get("content", {}).get("video_url")
                if video_url:
                    return video_url
                raise ValueError("任务成功但未返回视频URL")
            if status == "failed":
                error = data.get("error", "未知错误")
                raise ValueError(f"任务失败: {error}")
            if status in ("expired", "cancelled"):
                raise ValueError(f"任务状态异常: {status}")

            if status in ("queued", "pending"):
                progress = min(15.0, 2.0 + i * 1.0)
                msg = f"排队中 ({i+1} 次轮询)"
            else:
                progress = min(90.0, 15.0 + (i / max_attempts) * 75.0)
                msg = f"{status or '生成中'} ({i+1} 次轮询)"
            await _emit(progress, msg)

            await asyncio.sleep(interval)

        raise TimeoutError(f"任务轮询超时: {task_id}")

    async def _prepare_media_url(self, media_path: str) -> str:
        """媒体（首帧/尾帧/参考视频/参考音频）→ 厂商可取的引用串。

        0.8 缝2：本地文件优先走数据面拿公网 URL，数据面没配才回退 base64（行为同拆分前）。
        参考视频/音频尤其受益 —— 整段 mp4 塞成 base64 会把请求体撑到几十 MB。
        远程 URL 一律原样透传（火山原始 TOS URL = 真人审核的信任产物，重新托管会被重新拦）。
        """
        if media_path.startswith(("http://", "https://", "data:")):
            return media_path

        from app.core.paths import resolve_output_path
        disk = resolve_output_path(media_path)
        if not disk or not os.path.exists(disk):
            raise ValueError(f"媒体文件不存在: {media_path}")

        # public=True：无签名干净 URL（CDN 域名）。视频任务排队时签名 URL 的 1h 有效期会过期，
        # 且 query 串有被厂商网关弄坏的先例（蚂蚁 IMS → MaterialNotExist）。详见 image_to_video 同名方法。
        from app.core import media_gateway
        return await media_gateway.to_model_input(disk, public=True)

    async def _mock_generate(self, prompt: str, ratio: str, duration: int) -> Dict[str, Any]:
        """Mock 视频生成 — 生成占位视频文件"""
        output_path = os.path.join(videos_dir(), f"vid_{uuid.uuid4().hex[:8]}.mp4")

        # 尝试使用 FFmpeg 生成简单的占位视频
        try:
            import subprocess
            # 生成一个纯色背景 + 文字的视频
            label_text = f"[Mock Video] {prompt[:40]}..."
            # 使用 FFmpeg 的 drawtext 滤镜
            width, height = self._parse_ratio(ratio)
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"color=c=darkslategray:s={width}x{height}:d={duration}",
                "-vf", f"drawtext=text='{label_text}':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2",
                "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-t", str(duration),
                output_path
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        except Exception:
            # FFmpeg 失败，生成空文件
            with open(output_path, "wb") as f:
                f.write(b"")

        return {
            "video_url": f"/output/videos/{os.path.basename(output_path)}",
            "task_id": f"mock-{uuid.uuid4().hex[:8]}",
            "duration": duration,
            "ratio": ratio,
            "resolution": "720p",
            "seed": -1,
            "mock": True
        }

    def _parse_ratio(self, ratio: str) -> tuple:
        """解析宽高比为像素尺寸"""
        ratio_map = {
            "16:9": (1280, 720),
            "4:3": (1024, 768),
            "1:1": (720, 720),
            "3:4": (768, 1024),
            "9:16": (720, 1280),
            "21:9": (1280, 548),
        }
        return ratio_map.get(ratio, (1280, 720))
