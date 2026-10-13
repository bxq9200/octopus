import asyncio
import logging
import os
import uuid
import httpx
from typing import Any, Dict

from app.modules.base import BaseModule
from app.core.config import settings
from app.core.paths import videos_dir, resolve_output_path
# 复用 text_to_video 的提交 semaphore，让两个模块共享同一份 ARK 任务创建配额
from app.modules.text_to_video import SEEDANCE_SUBMIT_SEMAPHORE

logger = logging.getLogger(__name__)


class ImageToVideoModule(BaseModule):
    """图生视频模块：根据图片 + 动画描述生成短视频片段。

    多 provider 调度（按 user_settings.video.provider）：
      - ark       → 火山方舟 Seedance 2.0（_generate_with_seedance）
      - dashscope → 阿里云 HappyHorse（_dashscope_video.generate_image_to_video）
    任一 provider 失败都降级到 mock (FFmpeg Ken Burns)。
    """

    name = "image_to_video"
    description = "根据图片生成短视频片段。多 provider 支持：火山方舟 Seedance / 阿里云 HappyHorse。"
    category = "basic"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": "输入图片路径或 URL"
                },
                "prompt": {
                    "type": "string",
                    "description": "动画描述（如：镜头缓慢推进，人物微笑）"
                },
                "duration": {
                    "type": "number",
                    "default": 5.0,
                    "description": "视频时长（秒）"
                },
                "fps": {
                    "type": "integer",
                    "default": 24,
                    "description": "帧率"
                },
                "resolution": {
                    "type": "string",
                    "default": "720x1280",
                    "description": "输出分辨率"
                },
                "original_image_prompt": {
                    "type": "string",
                    "default": "",
                    "description": "首帧图片的原始生成 prompt；触发 PrivacyInformation 审核时用它重新生成去写实化首帧后重试"
                }
            },
            "required": ["image_url", "prompt"]
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "video_url": {"type": "string", "description": "生成的视频文件路径"},
                "duration": {"type": "number"},
                "fps": {"type": "integer"}
            },
            "required": ["video_url"]
        }

    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        image_url = input_data.get("image_url", "")
        prompt = input_data.get("prompt", "")
        duration = input_data.get("duration", 5.0)
        fps = input_data.get("fps", 24)
        resolution = input_data.get("resolution", "720x1280")
        # 上游可显式传入角色原始 image_prompt，用于审核拦截时重新生成去写实化首帧
        original_image_prompt = input_data.get("original_image_prompt") or ""
        # 0.7.1 #5：角色参考图（composite）—— 仅 Seedance(ark) 多模态全能参考用，其它 provider 忽略
        reference_images = input_data.get("reference_images") or []
        # 0.7.3 人物一致性：角色 asset_id 列表（已入蚂蚁/火山 asset 库）→ antchain i2v 用它锁角色身份
        character_asset_ids = input_data.get("character_asset_ids") or []
        # 进度回调：上游 (executor) 注入。签名 cb(progress: float 0-100, message: str)
        progress_cb = input_data.get("_progress_callback")

        # 按 user_settings.video.provider 分发：ark 走 Seedance，dashscope 走 HappyHorse
        from app.core.runtime_config import active_video_provider_name
        provider = active_video_provider_name()
        if provider == "dashscope":
            try:
                from app.modules._dashscope_video import generate_image_to_video
                return await generate_image_to_video(
                    image_url, prompt,
                    duration=int(round(duration)) or 5,
                    resolution=resolution,
                    progress_cb=progress_cb,
                )
            except Exception as e:
                logger.warning("DashScope 图生视频失败，降级 mock: %s", e)
                mock_result = await self._mock_generate(image_url, prompt, duration, fps, resolution)
                mock_result["mock_reason"] = f"DashScope 调用失败: {str(e)[:300]}"
                return mock_result

        if provider == "comfyui":
            try:
                from app.modules._comfyui_video import generate_image_to_video as _cu_i2v
                # 参考图与尾帧一并透传：H3 用得上（ref2v 锁脸 / fl2v 衔接），
                # Wan / LTX 分支会忽略它们，行为不变。
                return await _cu_i2v(
                    image_url, prompt,
                    duration=int(round(duration)) or 5,
                    resolution=resolution,
                    progress_cb=progress_cb,
                    reference_images=reference_images,
                    last_frame=input_data.get("last_frame") or input_data.get("last_frame_url"),
                    # 1.1.0：台词/歌词走 H3 的 `<d>` 标签，让嘴型对得上内容。
                    # 其它 provider 收不到这两个键（各自分支不传），行为不变。
                    dialogue=input_data.get("dialogue") or "",
                    singing=bool(input_data.get("singing")),
                )
            except Exception as e:
                logger.warning("ComfyUI 图生视频失败，降级 mock: %s", e)
                mock_result = await self._mock_generate(image_url, prompt, duration, fps, resolution)
                mock_result["mock_reason"] = f"ComfyUI 调用失败: {str(e)[:300]}"
                return mock_result

        if provider == "antchain":
            try:
                from app.modules._antchain_video import generate_image_to_video as _ac_i2v
                return await _ac_i2v(
                    image_url, prompt,
                    duration=int(round(duration)) or 5,
                    resolution=resolution,
                    reference_images=reference_images,
                    character_asset_ids=character_asset_ids,
                    progress_cb=progress_cb,
                )
            except Exception as e:
                logger.warning("蚂蚁数科图生视频失败，降级 mock: %s", e)
                mock_result = await self._mock_generate(image_url, prompt, duration, fps, resolution)
                mock_result["mock_reason"] = f"蚂蚁数科调用失败: {str(e)[:300]}"
                return mock_result

        # 0.8 缝1：Octopus 云端中转（本机不持企业 key，网关验 token → 转发 → 扣 credits）
        if provider == "octopus_cloud":
            try:
                from app.modules._octopus_cloud_video import generate_image_to_video as _oc_i2v
                return await _oc_i2v(
                    image_url, prompt,
                    duration=int(round(duration)) or 5,
                    resolution=resolution,
                    reference_images=reference_images,
                    character_asset_ids=character_asset_ids,
                    progress_cb=progress_cb,
                )
            except Exception as e:
                logger.warning("Octopus 云端中转图生视频失败，降级 mock: %s", e)
                mock_result = await self._mock_generate(image_url, prompt, duration, fps, resolution)
                mock_result["mock_reason"] = f"Octopus 云端中转调用失败: {str(e)[:300]}"
                return mock_result

        # 默认 provider == "ark"：Seedance；失败走 PrivacyInformation 重试 → mock
        try:
            return await self._generate_with_seedance(
                image_url, prompt, duration, fps, resolution,
                reference_images=reference_images, progress_cb=progress_cb,
            )
        except Exception as e:
            err_msg = str(e)
            # 0.7.1 #5：带角色参考图失败（400 role 不被接受/超图数 或 413 payload 过大）→ 退回纯首帧重试一次，
            # 不至于因参考图直接掉 mock；保底等同改前行为。
            if reference_images and ("400" in err_msg or "413" in err_msg):
                logger.warning("Seedance 带角色参考图返回 400，退回纯首帧重试：%s", err_msg[:160])
                try:
                    return await self._generate_with_seedance(
                        image_url, prompt, duration, fps, resolution,
                        reference_images=None, progress_cb=progress_cb,
                    )
                except Exception as e2:
                    err_msg = str(e2)
            mock_reason = f"Seedance 调用失败: {err_msg[:300]}"
            # 命中 PrivacyInformation 审核：自动重试一次
            if self._is_privacy_block(err_msg):
                logger.warning("Seedance 触发 PrivacyInformation，启动自动规避重试")
                # 路径 1：上游显式传入了首帧的原始 prompt → 重新生成去写实化首帧
                if original_image_prompt:
                    try:
                        new_image = await self._regenerate_dephoto_first_frame(
                            original_image_prompt, resolution,
                        )
                        if new_image:
                            result = await self._generate_with_seedance(
                                new_image, prompt, duration, fps, resolution,
                            )
                            result["dephoto_retry"] = "regen_first_frame"
                            result["dephoto_first_frame"] = new_image
                            return result
                    except Exception as retry_err:
                        logger.warning("重新生成首帧重试失败: %s", retry_err)
                        mock_reason = f"PrivacyInformation 重试失败: {str(retry_err)[:200]}"
                # 路径 2：没有原始 prompt（如首帧是场景图）→ 仅追加 prompt 修饰重试
                try:
                    dephoto_prompt = self._dephoto_video_prompt(prompt)
                    result = await self._generate_with_seedance(
                        image_url, dephoto_prompt, duration, fps, resolution,
                    )
                    result["dephoto_retry"] = "prompt_only"
                    result["dephoto_prompt"] = dephoto_prompt
                    return result
                except Exception as retry_err:
                    logger.warning("修饰 prompt 重试失败，降级到 mock: %s", retry_err)
                    mock_reason = f"PrivacyInformation 重试也失败: {str(retry_err)[:200]}"
            else:
                logger.warning("Seedance API 调用失败，降级到 mock: %s", e)
            mock_result = await self._mock_generate(image_url, prompt, duration, fps, resolution)
            mock_result["mock_reason"] = mock_reason
            return mock_result

    @staticmethod
    def _is_privacy_block(err_msg: str) -> bool:
        """识别 Seedance 审核拦截类错误"""
        if not err_msg:
            return False
        return ("PrivacyInformation" in err_msg
                or "Sensitive" in err_msg
                or "moderation" in err_msg.lower())

    async def _regenerate_dephoto_first_frame(
        self, original_image_prompt: str, resolution: str,
    ) -> str:
        """触发 PrivacyInformation 时，用去写实化的 prompt 重新生成首帧。
        返回新的本地图片路径（/output/... 形式），失败返回空字符串。
        """
        from app.modules.text_to_image import TextToImageModule
        dephoto = self._dephoto_prompt(original_image_prompt)
        try:
            w_str, h_str = resolution.split("x")
            w, h = int(w_str), int(h_str)
        except Exception:
            w, h = 1024, 1024
        t2i = TextToImageModule()
        result = await t2i.run({
            "prompt": dephoto,
            "width": w, "height": h,
            "style": "3d",
        })
        return result.get("image_url") or ""

    _PHOTO_REAL_KEYWORDS = (
        "photorealistic", "photo realistic", "photo-realistic",
        "professional photography", "studio photography", "portrait photography",
        "8k photo", "8k uhd", "real person", "real human", "raw photo",
        "DSLR", "shot on", "candid photograph",
    )

    @classmethod
    def _dephoto_prompt(cls, prompt: str) -> str:
        """剔除照片级关键词，追加 stylized 3D 后缀"""
        import re
        out = prompt
        low = out.lower()
        for kw in cls._PHOTO_REAL_KEYWORDS:
            i = low.find(kw.lower())
            while i >= 0:
                out = out[:i] + out[i + len(kw):]
                low = out.lower()
                i = low.find(kw.lower())
        prev = None
        while prev != out:
            prev = out
            out = re.sub(r",\s*,", ",", out)
        out = re.sub(r"\s+", " ", out).strip().rstrip(",").strip()
        if "stylized 3d character" not in out.lower():
            out = out + ", stylized 3D character render, cinematic CG, soft painterly texture, non-photorealistic character"
        return out

    @classmethod
    def _dephoto_video_prompt(cls, prompt: str) -> str:
        """视频 prompt 的去写实化：剔除照片级词 + 追加动画风格修饰（区别于角色图，这里强调动态）"""
        import re
        out = prompt
        low = out.lower()
        for kw in cls._PHOTO_REAL_KEYWORDS:
            i = low.find(kw.lower())
            while i >= 0:
                out = out[:i] + out[i + len(kw):]
                low = out.lower()
                i = low.find(kw.lower())
        prev = None
        while prev != out:
            prev = out
            out = re.sub(r",\s*,", ",", out)
        out = re.sub(r"\s+", " ", out).strip().rstrip(",").strip()
        if "stylized 3d animation" not in out.lower() and "non-photorealistic" not in out.lower():
            out = out + ", stylized 3D animation, cinematic CG animation, non-photorealistic characters"
        return out

    async def _generate_with_seedance(self, image_url: str, prompt: str, duration: float, fps: int, resolution: str, reference_images: list = None, progress_cb=None) -> Dict[str, Any]:
        """使用 Seedance 2.0 多模态 API 生成视频（与 text_to_video 协议一致）。
        reference_images：角色参考图(composite 脸+三视图)，作 role=reference_image 喂 Seedance 多模态全能参考锁主体身份。"""
        from app.core.runtime_config import ark_config
        cfg = ark_config()
        api_key = cfg.api_key
        base_url = cfg.base_url

        if not api_key or not base_url:
            raise ValueError("未配置 Seedance API Key 或 Base URL")

        # 准备首帧图片：URL 直接用，本地路径转 base64 data URL
        first_frame_url = await self._prepare_image_url(image_url)

        # 推断比例
        w_str, h_str = resolution.split("x")
        w_val, h_val = int(w_str), int(h_str)
        if w_val > h_val:
            ratio = "16:9"
        elif w_val < h_val:
            ratio = "9:16"
        else:
            ratio = "1:1"

        # Seedance 2.0 要求 duration ∈ [4, 12]，分镜里 1.5/2.5s 这种短镜头会被直接 400 拒
        clamped_duration = max(4, min(12, int(round(duration)) or 5))

        content = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": first_frame_url},
                "role": "first_frame",
            },
        ]
        # 0.7.1 #5：⚠️ 实测 Seedance 2.0 **首帧 与 reference_image 互斥**
        # （400 "first/last frame content cannot be combined with..."），且写实人脸 ref 同样被隐私审核拦。
        # 故仅在"无首帧(纯参考图模式)"时才送角色参考图；有首帧的 i2v 一律不送，避免必然 400 + 浪费一次调用。
        ref_count = 0
        if not first_frame_url:
            for ref in (reference_images or []):
                if ref_count >= 9:
                    break
                try:
                    ref_url = await self._prepare_image_url(ref)
                except Exception:
                    continue
                if ref_url:
                    content.append({"type": "image_url", "image_url": {"url": ref_url}, "role": "reference_image"})
                    ref_count += 1

        payload = {
            "model": cfg.video_model,
            "content": content,
            "ratio": ratio,
            "duration": clamped_duration,
            "resolution": "720p" if max(w_val, h_val) <= 1280 else "1080p",
            "generate_audio": True,
            "watermark": False,
        }

        # 1. 创建任务（与 text_to_video 共用 semaphore，避免双模块加起来打挂 ARK）
        async with SEEDANCE_SUBMIT_SEMAPHORE:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{base_url}/contents/generations/tasks",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    }
                )
                if response.status_code >= 400:
                    body = response.text[:800]
                    raise ValueError(
                        f"Seedance 返回 {response.status_code}: {body} | "
                        f"model={payload.get('model')} ratio={payload.get('ratio')} "
                        f"duration={payload.get('duration')} resolution={payload.get('resolution')} "
                        f"first_frame={'data:' if first_frame_url.startswith('data:') else first_frame_url[:80]}"
                    )
                create_data = response.json()

        task_id = create_data.get("id")
        if not task_id:
            raise ValueError(f"Seedance API 未返回任务ID: {create_data}")

        # 2. 轮询任务
        video_remote_url = await self._poll_task(task_id, api_key, base_url, progress_cb=progress_cb)

        # 3. 下载视频
        output_path = os.path.join(videos_dir(), f"seedance_{uuid.uuid4().hex[:8]}.mp4")
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.get(video_remote_url)
            resp.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(resp.content)

        return {
            "video_url": f"/output/videos/{os.path.basename(output_path)}",
            "task_id": task_id,
            "duration": duration,
            "fps": fps,
            "resolution": resolution,
            "prompt": prompt,
            "mock": False,
            "model": cfg.video_model,
            # 0.6.5: 成本统计用
            "usage": {
                "provider": "ark",
                "model": cfg.video_model,
                "kind": "video",
                "seconds": int(duration or 0),
                "resolution": str(resolution or "720p"),
            },
        }

    async def _prepare_image_url(self, image_path: str) -> str:
        """首帧/参考图 → 厂商可取的引用串。

        0.8 缝2：本地图优先走数据面拿公网 URL（替掉原来无条件 base64 —— 大图会把请求体撑爆，
        且 Seedream 5.0 多图参考不认 base64），数据面没配才回退 base64（行为同拆分前）。
        远程 URL 一律原样透传：火山原始 TOS URL 是真人审核的"信任产物"，重新托管会被重新拦。

        **public=True（无签名干净 URL，走 CDN 域名）**——视频任务会排队，签名 URL 只有 1h 有效期，
        排久了厂商去抓首帧就 404；干净 URL 无有效期、也没有 query 串被网关弄坏的风险
        （蚂蚁 IMS 就在签名串上翻过车 → MaterialNotExist）。代价是对象 public-read，
        但 key 是 uuid4、URL 猜不到，且成片本来就在用同一条通道。
        """
        from app.core import media_gateway
        return await media_gateway.to_model_input(image_path, public=True)

    async def _poll_task(self, task_id: str, api_key: str, base_url: str,
                         max_attempts: int = 120, interval: float = 5.0,
                         progress_cb=None) -> str:
        """轮询 Seedance 任务状态。

        Seedance API 不直接给百分比，所以基于"轮询次数 / max_attempts"和状态做粗估：
        - queued/pending: 0-15%（卡在排队）
        - in_progress / running / processing: 15-90%（线性增长）
        - succeeded: 100%
        每次回调推 (progress, message) 给上层 → WS。
        """
        query_url = f"{base_url}/contents/generations/tasks/{task_id}"
        async def _emit(progress: float, message: str):
            if not progress_cb:
                return
            try:
                cb_result = progress_cb(progress, message)
                if asyncio.iscoroutine(cb_result):
                    await cb_result
            except Exception as cb_err:
                logger.debug("progress_cb 异常忽略: %s", cb_err)

        await _emit(2.0, "提交任务成功，等待排队...")

        async with httpx.AsyncClient(timeout=30.0) as client:
            for i in range(max_attempts):
                resp = await client.get(query_url, headers={"Authorization": f"Bearer {api_key}"})
                resp.raise_for_status()
                data = resp.json()
                status = data.get("status")

                if status == "succeeded":
                    await _emit(100.0, "任务完成，正在下载视频...")
                    video_url = (data.get("content") or {}).get("video_url")
                    if video_url:
                        return video_url
                    raise ValueError("任务成功但未返回视频URL")
                if status in ("failed", "expired", "cancelled"):
                    raise ValueError(f"任务异常: {status}, {data.get('error')}")

                # 估算进度
                if status in ("queued", "pending"):
                    # 0-15% 缓慢爬，让用户知道还在排队
                    progress = min(15.0, 2.0 + i * 1.0)
                    msg = f"排队中 ({i+1} 次轮询)"
                else:
                    # 进行中：15% → 90%
                    progress = min(90.0, 15.0 + (i / max_attempts) * 75.0)
                    msg = f"{status or '生成中'} ({i+1} 次轮询)"
                await _emit(progress, msg)

                await asyncio.sleep(interval)
        raise TimeoutError(f"Seedance 任务轮询超时: {task_id}")

    async def _mock_generate(self, image_url: str, prompt: str, duration: float, fps: int, resolution: str) -> Dict[str, Any]:
        """Mock 图生视频 — 用 FFmpeg 从单图生成带 Ken Burns 效果的视频；图片不可用时降级为纯色"""
        output_path = os.path.join(videos_dir(), f"img2vid_{uuid.uuid4().hex[:8]}.mp4")

        local_image = resolve_output_path(image_url)
        has_local_image = os.path.exists(local_image)

        try:
            import subprocess
            w, h = resolution.split("x")

            if has_local_image:
                cmd = [
                    "ffmpeg", "-y",
                    "-loop", "1",
                    "-i", local_image,
                    "-vf", (
                        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                        f"zoompan=z='min(zoom+0.0015,1.5)':d={int(duration * fps)}:s={w}x{h}:fps={fps}"
                    ),
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "23",
                    "-t", str(duration),
                    "-pix_fmt", "yuv420p",
                    output_path
                ]
            else:
                # 没有可用图片，画一个带文字的纯色视频
                safe_prompt = prompt[:40].replace("'", "").replace(":", "").replace("\\", "")
                cmd = [
                    "ffmpeg", "-y",
                    "-f", "lavfi",
                    "-i", f"color=c=darkslategray:s={w}x{h}:d={duration}",
                    "-vf", (
                        f"drawtext=text='[Mock Image-to-Video] {safe_prompt}'"
                        f":fontsize=20:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2"
                    ),
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "23",
                    "-t", str(duration),
                    "-pix_fmt", "yuv420p",
                    output_path
                ]

            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            
        except Exception as e:
            # 降级：创建空白视频
            logger.warning("FFmpeg 失败，使用空白视频降级: %s", e)
            try:
                import subprocess
                w, h = resolution.split("x")
                cmd = [
                    "ffmpeg", "-y",
                    "-f", "lavfi",
                    "-i", f"color=c=black:s={w}x{h}:d={duration}",
                    "-c:v", "libx264",
                    "-t", str(duration),
                    "-pix_fmt", "yuv420p",
                    output_path
                ]
                subprocess.run(cmd, check=True, capture_output=True, timeout=30)
            except Exception:
                pass

        return {
            "video_url": f"/output/videos/{os.path.basename(output_path)}",
            "duration": duration,
            "fps": fps,
            "resolution": resolution,
            "prompt": prompt,
            "mock": True
        }
