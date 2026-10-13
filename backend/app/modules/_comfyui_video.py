"""ComfyUI 本地视频 provider（0.7.x 开发期替代 Seedance，省算力）。

连本地 ComfyUI（默认 127.0.0.1:8188，可在 Linux/WSL）跑 Wan 2.2：
- 单首帧 I2V（TI2V-5B，默认）—— 对齐当前分镜→视频 pipeline
- 首尾帧 FLF2V（WanFirstLastFrameToVideo，备 0.7.1 连贯性）

跨机：后端在 Windows、ComfyUI 可能在 WSL/Linux，故首帧通过 /upload/image 传过去（不依赖共享盘）。
失败时抛异常，由分发模块（image_to_video / text_to_video）降级到 ffmpeg mock。
"""
import asyncio
import logging
import os
import random
import time
import uuid
from typing import Any, Dict, Optional

import httpx

from app.core.paths import resolve_output_path, videos_dir
from app.core.runtime_config import comfyui_config

from app.modules import _comfyui_h3 as _h3

logger = logging.getLogger(__name__)

# 🔴 单卡串行锁：一张 GPU 同时跑两个任务只会互相拖慢，更要命的是
#    ——内存余量检查必须在**上一镜真正跑完之后**做才有意义。
#    2026-09-08 实测：并发触发两镜时，两个请求几乎同时检查内存，那一刻第一镜
#    还没加载模型、内存看着是空的，于是**两个都通过了检查**（守卫命中 0 次）；
#    第一镜跑完留下 41GB 占用，第二镜就在脏环境里换页，1 分钟变 30 分钟。
#    串起来之后，第二镜检查时才能看到真实占用。
_COMFY_GPU_LOCK = asyncio.Semaphore(1)

# Wan 官方推荐负面提示词（中文）
WAN_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，"
    "低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，"
    "毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)

_POLL_TIMEOUT_S = 2700   # 10s/121帧横屏片在 4090 上可达 ~24 分钟；放宽避免超时丢结果（横屏被降级成竖屏 mock）
_POLL_INTERVAL_S = 2.0


def _snap32(v: int) -> int:
    return max(32, int(round(v / 32)) * 32)


def _parse_resolution(resolution: str, fallback=(704, 1280)) -> tuple:
    try:
        w, h = str(resolution).lower().split("x")
        return _snap32(int(w)), _snap32(int(h))
    except Exception:
        return fallback


def _res_from_ratio(ratio, default=(704, 1280)) -> tuple:
    """降级链路（text_to_video）只传 '720p' + ratio；据 ratio 给横/竖屏分辨率，
    避免 '720p' 解析失败时误用竖屏默认（横屏项目被降级成竖屏视频的根因）。"""
    r = str(ratio or "").strip()
    if r == "16:9":
        return (1280, 704)
    if r == "9:16":
        return (704, 1280)
    return default


def _frames(duration: int, fps: int, max_frames: int) -> int:
    """Wan length 需为 4n+1。duration 秒 × fps → 贴合的帧数，封顶 max_frames。"""
    n = max(1, int(round((duration or 5) * fps / 4)))
    length = n * 4 + 1
    return max(5, min(length, max_frames))


def _vae_for(model: str) -> str:
    m = (model or "").lower()
    # 5B/ti2v 用 wan2.2_vae；14B i2v 用 wan_2.1_vae
    if "ti2v" in m or "5b" in m:
        return "wan2.2_vae.safetensors"
    return "wan_2.1_vae.safetensors"


_FIVEB_DEFAULT = "wan2.2_ti2v_5B_fp16.safetensors"
# 14B I2V 高/低噪双模型（fp8，定稿用，质量高但慢）—— 文件名对齐 Wan2.2 官方
_14B_HIGH = "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
_14B_LOW = "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"


def _is_14b(model: str) -> bool:
    """开发期 5B / 定稿 14B 的切换开关：comfyui_model 含 '14b' → 走 14B 高/低噪 MoE 图（质量高但慢），
    否则 5B 单模型图（快）。在 系统配置→视频→comfyui_model 填带 '14B' 的名字即切 14B。"""
    return "14b" in (model or "").lower()


def _safe_5b_model(model: str) -> str:
    """5B 路径用：model 名是 ti2v/5b 就用它，否则回退默认 5B 文件。"""
    m = (model or "").lower()
    if "ti2v" in m or "5b" in m:
        return model
    return _FIVEB_DEFAULT


def _i2v_workflow(cfg, image_name: str, prompt: str, width: int, height: int,
                  length: int, seed: int) -> Dict[str, Any]:
    """按 comfyui_model 选档：含 '14B' → 高/低噪 MoE 图（定稿）；否则 5B 单模型图（开发）。"""
    if _is_14b(cfg.model):
        return _i2v_workflow_14b(cfg, image_name, prompt, width, height, length, seed)
    return _i2v_workflow_5b(cfg, image_name, prompt, width, height, length, seed)


def _i2v_workflow_5b(cfg, image_name: str, prompt: str, width: int, height: int,
                     length: int, seed: int) -> Dict[str, Any]:
    """Wan 2.2 TI2V-5B 单首帧 I2V 工作流（API 格式，已对 ComfyUI 实例验证）。"""
    model = _safe_5b_model(cfg.model)
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": model, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": _vae_for(model)}},
        "4": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt or "", "clip": ["2", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": WAN_NEGATIVE, "clip": ["2", 0]}},
        "7": {"class_type": "Wan22ImageToVideoLatent",
              "inputs": {"vae": ["3", 0], "width": width, "height": height,
                         "length": length, "batch_size": 1, "start_image": ["4", 0]}},
        "8": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
        "9": {"class_type": "KSampler",
              "inputs": {"model": ["8", 0], "seed": seed, "steps": 20, "cfg": 5.0,
                         "sampler_name": "euler", "scheduler": "simple",
                         "positive": ["5", 0], "negative": ["6", 0],
                         "latent_image": ["7", 0], "denoise": 1.0}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "fps": float(cfg.fps)}},
        "12": {"class_type": "SaveVideo",
               "inputs": {"video": ["11", 0], "filename_prefix": "octopus_wan/clip",
                          "format": "mp4", "codec": "h264"}},
    }


def _i2v_workflow_14b(cfg, image_name: str, prompt: str, width: int, height: int,
                      length: int, seed: int) -> Dict[str, Any]:
    """Wan 2.2 I2V-14B 高/低噪双模型 MoE 图（对齐官方 wan22_i2v_14B）。
    high-noise 专家跑前半步、low-noise 接力后半步；WanImageToVideo 把首帧编码进条件并出 latent。"""
    steps = 20
    split = steps // 2  # high 跑 0→split，low 跑 split→end
    return {
        "1":  {"class_type": "UNETLoader", "inputs": {"unet_name": _14B_HIGH, "weight_dtype": "default"}},
        "2":  {"class_type": "UNETLoader", "inputs": {"unet_name": _14B_LOW, "weight_dtype": "default"}},
        "3":  {"class_type": "CLIPLoader",
               "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan"}},
        "4":  {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
        "5":  {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "6":  {"class_type": "CLIPTextEncode", "inputs": {"text": prompt or "", "clip": ["3", 0]}},
        "7":  {"class_type": "CLIPTextEncode", "inputs": {"text": WAN_NEGATIVE, "clip": ["3", 0]}},
        "8":  {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
        "9":  {"class_type": "ModelSamplingSD3", "inputs": {"model": ["2", 0], "shift": 8.0}},
        "10": {"class_type": "WanImageToVideo",
               "inputs": {"positive": ["6", 0], "negative": ["7", 0], "vae": ["4", 0],
                          "width": width, "height": height, "length": length, "batch_size": 1,
                          "start_image": ["5", 0]}},
        "11": {"class_type": "KSamplerAdvanced",
               "inputs": {"model": ["8", 0], "add_noise": "enable", "noise_seed": seed,
                          "steps": steps, "cfg": 3.5, "sampler_name": "euler", "scheduler": "simple",
                          "positive": ["10", 0], "negative": ["10", 1], "latent_image": ["10", 2],
                          "start_at_step": 0, "end_at_step": split, "return_with_leftover_noise": "enable"}},
        "12": {"class_type": "KSamplerAdvanced",
               "inputs": {"model": ["9", 0], "add_noise": "disable", "noise_seed": seed,
                          "steps": steps, "cfg": 3.5, "sampler_name": "euler", "scheduler": "simple",
                          "positive": ["10", 0], "negative": ["10", 1], "latent_image": ["11", 0],
                          "start_at_step": split, "end_at_step": 10000, "return_with_leftover_noise": "disable"}},
        "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["4", 0]}},
        "14": {"class_type": "CreateVideo", "inputs": {"images": ["13", 0], "fps": float(cfg.fps)}},
        "15": {"class_type": "SaveVideo",
               "inputs": {"video": ["14", 0], "filename_prefix": "octopus_wan/clip",
                          "format": "mp4", "codec": "h264"}},
    }


def _is_ltx(model: str) -> bool:
    """comfyui_model 含 'ltx' 或 'sulphur' → 走 LTX-2.3 i2v 两段式工作流（自带音频，质量高、较快）。
    在 系统配置→视频→comfyui_model 填带 'ltx' 的名字即切 LTX。"""
    m = (model or "").lower()
    return "ltx" in m or "sulphur" in m


def _i2v_workflow_ltx(cfg, image_name: str, prompt: str, width: int, height: int,
                      duration: int, fps: int, seed: int, t2v: bool = False) -> Dict[str, Any]:
    """LTX-2.3 两段式（底模半分辨率 → x2 潜空间上采样 → 精修），**自带音频生成**。
    对齐用户实测可用的工作流；length 由模板内 duration*fps+1 计算。
    t2v=True：bypass 两段 ImgToVideoInplace 的首帧注入 = 纯文生视频（用户 t2v 工作流与 i2v 同图，
    仅 Switch 布尔翻 true；LoadImage 喂占位 example.png——ComfyUI 自带该图，被 bypass 不影响画面）。"""
    seed2 = (int(seed) + 17) & 0x7FFFFFFF
    return {
        "75": {"class_type": "SaveVideo", "inputs": {
            "filename_prefix": "octopus_ltx/clip", "format": "auto", "codec": "auto",
            "video-preview": "", "video": ["320:310", 0]}},
        "269": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "320:276": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "320:277": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed2}},
        "320:278": {"class_type": "LTXVConcatAVLatent", "inputs": {
            "video_latent": ["320:288", 0], "audio_latent": ["320:307", 1]}},
        "320:279": {"class_type": "LTXVAudioVAELoader", "inputs": {"ckpt_name": "sulphur_dev_fp8mixed.safetensors"}},
        "320:280": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler_cfg_pp"}},
        "320:281": {"class_type": "ManualSigmas", "inputs": {"sigmas": "0.85, 0.7250, 0.4219, 0.0"}},
        "320:282": {"class_type": "CFGGuider", "inputs": {
            "cfg": 1, "model": ["320:285", 0], "positive": ["320:284", 0], "negative": ["320:284", 1]}},
        "320:283": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["320:277", 0], "guider": ["320:314", 0], "sampler": ["320:291", 0],
            "sigmas": ["320:306", 0], "latent_image": ["320:318", 0]}},
        "320:284": {"class_type": "LTXVCropGuides", "inputs": {
            "positive": ["320:304", 0], "negative": ["320:304", 1], "latent": ["320:307", 0]}},
        "320:285": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "lora_name": "ltx-2.3-22b-distilled-lora-384.safetensors", "strength_model": 0.5, "model": ["320:316", 0]}},
        "320:286": {"class_type": "ResizeImagesByLongerEdge", "inputs": {"longer_edge": 1536, "images": ["320:290", 0]}},
        "320:287": {"class_type": "LTXVLatentUpsampler", "inputs": {
            "samples": ["320:307", 0], "upscale_model": ["320:311", 0], "vae": ["320:316", 2]}},
        "320:288": {"class_type": "LTXVImgToVideoInplace", "inputs": {
            "strength": 1, "bypass": ["320:302", 0], "vae": ["320:316", 2], "image": ["320:289", 0], "latent": ["320:287", 0]}},
        "320:289": {"class_type": "LTXVPreprocess", "inputs": {"img_compression": 18, "image": ["320:286", 0]}},
        "320:290": {"class_type": "ResizeImageMaskNode", "inputs": {
            "resize_type": "scale dimensions", "resize_type.width": ["320:312", 0], "resize_type.height": ["320:299", 0],
            "resize_type.crop": "center", "scale_method": "lanczos", "input": ["269", 0]}},
        "320:291": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler_ancestral_cfg_pp"}},
        "320:292": {"class_type": "ComfyMathExpression", "inputs": {"expression": "a/2", "values.a": ["320:312", 0]}},
        "320:294": {"class_type": "ComfyMathExpression", "inputs": {"expression": "a/2", "values.a": ["320:299", 0]}},
        "320:295": {"class_type": "EmptyLTXVLatentVideo", "inputs": {
            "width": ["320:292", 1], "height": ["320:294", 1], "length": ["320:323", 1], "batch_size": 1}},
        "320:296": {"class_type": "LTXVImgToVideoInplace", "inputs": {
            "strength": 0.7, "bypass": ["320:302", 0], "vae": ["320:316", 2], "image": ["320:289", 0], "latent": ["320:295", 0]}},
        "320:297": {"class_type": "LTXVAudioVAEDecode", "inputs": {"samples": ["320:309", 1], "audio_vae": ["320:279", 0]}},
        "320:298": {"class_type": "ComfyMathExpression", "inputs": {"expression": "a", "values.a": ["320:300", 0]}},
        "320:299": {"class_type": "PrimitiveInt", "inputs": {"value": int(height)}},
        "320:300": {"class_type": "PrimitiveInt", "inputs": {"value": int(fps)}},
        "320:301": {"class_type": "PrimitiveInt", "inputs": {"value": int(duration)}},
        "320:302": {"class_type": "PrimitiveBoolean", "inputs": {"value": bool(t2v)}},
        "320:303": {"class_type": "CLIPTextEncode", "inputs": {"text": ["320:319", 0], "clip": ["320:317", 0]}},
        "320:304": {"class_type": "LTXVConditioning", "inputs": {
            "frame_rate": ["320:298", 0], "positive": ["320:303", 0], "negative": ["320:313", 0]}},
        "320:305": {"class_type": "LTXVEmptyLatentAudio", "inputs": {
            "frames_number": ["320:323", 1], "frame_rate": ["320:298", 1], "batch_size": 1, "audio_vae": ["320:279", 0]}},
        "320:306": {"class_type": "ManualSigmas", "inputs": {
            "sigmas": "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"}},
        "320:307": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["320:283", 0]}},
        "320:308": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["320:276", 0], "guider": ["320:282", 0], "sampler": ["320:280", 0],
            "sigmas": ["320:281", 0], "latent_image": ["320:278", 0]}},
        "320:309": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["320:308", 0]}},
        "320:310": {"class_type": "CreateVideo", "inputs": {
            "fps": ["320:298", 0], "images": ["320:315", 0], "audio": ["320:297", 0]}},
        "320:311": {"class_type": "LatentUpscaleModelLoader", "inputs": {"model_name": "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"}},
        "320:312": {"class_type": "PrimitiveInt", "inputs": {"value": int(width)}},
        "320:313": {"class_type": "CLIPTextEncode", "inputs": {
            "text": "pc game, console game, video game, cartoon, childish, ugly, text, watermark, subtitle", "clip": ["320:317", 0]}},
        "320:314": {"class_type": "CFGGuider", "inputs": {
            "cfg": 1, "model": ["320:285", 0], "positive": ["320:304", 0], "negative": ["320:304", 1]}},
        "320:315": {"class_type": "VAEDecodeTiled", "inputs": {
            # temporal_size 对齐用户实测工作流：4096 = 一次解码整段、不做时间分块，
            # 消除原 temporal_size=32 每 ~24 帧一个分块接缝导致的周期性模糊。
            "tile_size": 768, "overlap": 64, "temporal_size": 4096, "temporal_overlap": 4,
            "samples": ["320:309", 0], "vae": ["320:316", 2]}},
        "320:316": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sulphur_dev_fp8mixed.safetensors"}},
        "320:317": {"class_type": "LTXAVTextEncoderLoader", "inputs": {
            "text_encoder": "gemma_3_12B_it_fp4_mixed.safetensors", "ckpt_name": "sulphur_dev_fp8mixed.safetensors", "device": "default"}},
        "320:318": {"class_type": "LTXVConcatAVLatent", "inputs": {
            "video_latent": ["320:296", 0], "audio_latent": ["320:305", 0]}},
        "320:319": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": prompt or ""}},
        "320:323": {"class_type": "ComfyMathExpression", "inputs": {
            "expression": "a * b + 1", "values.a": ["320:301", 0], "values.b": ["320:300", 0]}},
    }


def _t2v_workflow(cfg, prompt: str, width: int, height: int, length: int, seed: int) -> Dict[str, Any]:
    """文生视频（非 LTX 配置的回退路径，Wan 5B）：同 5B I2V 图，去掉 LoadImage + start_image。
    LTX 配置不走这里 —— generate_text_to_video 直接用 _i2v_workflow_ltx(t2v=True)。"""
    wf = _i2v_workflow_5b(cfg, "", prompt, width, height, length, seed)
    wf.pop("4", None)
    wf["7"]["inputs"].pop("start_image", None)
    return wf


async def _upload_image(client: httpx.AsyncClient, base: str, image_url: str) -> str:
    """把首帧上传到 ComfyUI input 目录，返回其引用名（含 subfolder）。
    支持本地 /output 路径与远程 URL（如火山 Seedream 关键帧的 TOS 地址）——
    ComfyUI 只认本地 input 文件，远程图必须先下载到内存再上传，否则 i2v 会因
    "首帧文件不存在"失败、降级到 t2v(Wan)。"""
    if image_url.startswith(("http://", "https://")):
        resp = await client.get(image_url)
        resp.raise_for_status()
        img_bytes = resp.content
        fname = f"octopus_remote_{uuid.uuid4().hex[:10]}.png"
    else:
        disk = resolve_output_path(image_url)
        if not disk or not os.path.exists(disk):
            raise FileNotFoundError(f"首帧文件不存在: {image_url} -> {disk}")
        with open(disk, "rb") as f:
            img_bytes = f.read()
        fname = os.path.basename(disk)
    files = {"image": (fname, img_bytes, "image/png")}
    r = await client.post(f"{base}/upload/image", files=files, data={"overwrite": "true"})
    r.raise_for_status()
    d = r.json()
    name = d.get("name") or fname
    sub = d.get("subfolder") or ""
    return f"{sub}/{name}" if sub else name


async def _run_workflow(cfg, wf: Dict[str, Any], progress_cb=None) -> Dict[str, str]:
    """提交工作流 → 轮询 → 返回视频文件信息 {filename, subfolder, type}。"""
    base = cfg.base_url.rstrip("/")
    client_id = uuid.uuid4().hex
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        r = await client.post(f"{base}/prompt", json={"prompt": wf, "client_id": client_id})
        r.raise_for_status()
        data = r.json()
        if data.get("node_errors"):
            raise RuntimeError(f"ComfyUI 工作流校验失败: {data['node_errors']}")
        pid = data.get("prompt_id")
        if not pid:
            raise RuntimeError(f"ComfyUI 未返回 prompt_id: {data}")

        start = time.time()
        while time.time() - start < _POLL_TIMEOUT_S:
            await asyncio.sleep(_POLL_INTERVAL_S)
            hr = await client.get(f"{base}/history/{pid}")
            hist = hr.json()
            if pid in hist:
                entry = hist[pid]
                status = (entry.get("status") or {}).get("status_str")
                if status == "error":
                    raise RuntimeError(f"ComfyUI 执行失败: {entry.get('status')}")
                for _nid, out in (entry.get("outputs") or {}).items():
                    for v in (out.get("images") or []) + (out.get("videos") or []):
                        fn = str(v.get("filename", ""))
                        if fn.endswith((".mp4", ".webm", ".webp", ".gif")):
                            return v
                raise RuntimeError(f"ComfyUI 完成但无视频输出: {list((entry.get('outputs') or {}).keys())}")
            if progress_cb:
                try:
                    await progress_cb(f"🎞️ ComfyUI 生成中... {int(time.time() - start)}s")
                except Exception:
                    pass
        raise RuntimeError(f"ComfyUI 轮询超时（>{_POLL_TIMEOUT_S}s）")


async def _download_video(cfg, vinfo: Dict[str, str]) -> str:
    base = cfg.base_url.rstrip("/")
    params = {
        "filename": vinfo["filename"],
        "subfolder": vinfo.get("subfolder", ""),
        "type": vinfo.get("type", "output"),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        r = await client.get(f"{base}/view", params=params)
        r.raise_for_status()
        content = r.content
    # 文件名带真实模型档，避免"LTX 出的片却叫 wan_"造成误判
    if _h3.is_h3_model(cfg.model):
        _tier = "h3"
    else:
        _tier = "ltx" if _is_ltx(cfg.model) else ("wan14b" if _is_14b(cfg.model) else "wan5b")
    fn = f"{_tier}_{uuid.uuid4().hex[:8]}.mp4"
    path = os.path.join(videos_dir(), fn)
    with open(path, "wb") as f:
        f.write(content)
    return f"/output/videos/{fn}"


def _seed(seed) -> int:
    if isinstance(seed, int) and seed >= 0:
        return seed
    return random.randint(0, 2**31 - 1)


async def generate_image_to_video(
    image_url: str,
    prompt: str,
    *,
    duration: int = 5,
    resolution: str = "704x1280",
    seed: int = -1,
    progress_cb=None,
    **_kwargs,
) -> Dict[str, Any]:
    """单首帧图生视频（ComfyUI / Wan 2.2）。"""
    cfg = comfyui_config()
    base = cfg.base_url.rstrip("/")
    w, h = _parse_resolution(resolution, fallback=_res_from_ratio(_kwargs.get("ratio")))
    sd = _seed(seed)
    if progress_cb:
        try:
            await progress_cb("🎞️ ComfyUI: 上传首帧...")
        except Exception:
            pass
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as up:
        image_name = await _upload_image(up, base, image_url)
        # 首尾帧：上游给了尾帧就走 fl2v（同一个 H3 节点多喂一张图），能让连续镜头衔接更稳
        last_name = None
        _lf = _kwargs.get("last_frame") or _kwargs.get("last_frame_url")
        if _h3.is_h3_model(cfg.model) and _lf:
            try:
                last_name = await _upload_image(up, base, _lf)
            except Exception as _e:
                logger.warning("H3 尾帧上传失败，退回只用首帧: %s", _e)

    if _h3.is_h3_model(cfg.model):
        w, h = _h3.h3_size(w, h)
        length = _h3.h3_length(duration or 5)
        # 🔴 中文描述会被 H3 当台词念出来（实测音轨 -16dB 全是人声，嘴型跟着乱动）；
        #    英文描述则一个字都不念。翻译只影响这条链路，出图那边照旧用中文。
        prompt = await _h3.to_english(prompt)
        # 🔴 台词/歌词：让 H3 把该说的说出来，嘴型才对得上。
        #    描述必须译成英文（否则整段被当台词念），但台词本身**保持原文不翻**。
        _spk = {"dialogue": (_kwargs.get("dialogue") or "").strip(),
                "speaker": (_kwargs.get("speaker") or "").strip(),
                "singing": bool(_kwargs.get("singing"))}
        wf = _h3.build_i2v(unet=cfg.model, prompt=prompt, width=w, height=h,
                           length=length, seed=sd,
                           first_frame=image_name, last_frame=last_name, **_spk)
        out_fps = _h3.H3_FPS
        out_dur = round(length / _h3.H3_FPS, 2)
        if progress_cb:
            try:
                await progress_cb(f"🎞️ MiniMax H3: {_h3.describe(w, h, length)}")
            except Exception:
                pass
    elif _is_ltx(cfg.model):
        # LTX-2.3：原生 25fps。22B 模型很吃显存 —— 用 max_frames 反推时长上限，
        # 别让 latent/解码爆显存被 OOM kill（开发期把 comfyui_max_frames 调低，如 121≈5s）。
        ltx_fps = 25
        max_dur = max(1, (int(cfg.max_frames) - 1) // ltx_fps)
        ltx_dur = min(int(round(duration or 5)), max_dur)
        wf = _i2v_workflow_ltx(cfg, image_name, prompt, w, h, ltx_dur, ltx_fps, sd)
        out_fps, out_dur = ltx_fps, float(ltx_dur)
    else:
        length = _frames(int(round(duration or 5)), cfg.fps, cfg.max_frames)
        wf = _i2v_workflow(cfg, image_name, prompt, w, h, length, sd)
        out_fps, out_dur = cfg.fps, round(length / cfg.fps, 2)
    # 🔴 检查内存 + 跑任务必须在同一把锁里：分开做的话并发提交时两边都会
    #    看到"内存还很空"而放行（详见 _COMFY_GPU_LOCK 的注释）。
    async with _COMFY_GPU_LOCK:
        await _h3.ensure_headroom(cfg.base_url, progress_cb=progress_cb)
        vinfo = await _run_workflow(cfg, wf, progress_cb)
    video_url = await _download_video(cfg, vinfo)
    return {
        "video_url": video_url,
        "task_id": f"comfyui-{uuid.uuid4().hex[:8]}",
        "duration": out_dur,
        "fps": out_fps,
        "resolution": f"{w}x{h}",
        "seed": sd,
        "prompt": prompt,
        "mock": False,
        "model": cfg.model,
        "provider": "comfyui",
    }


async def generate_text_to_video(
    prompt: str,
    *,
    duration: int = 5,
    resolution: str = "704x1280",
    seed: int = -1,
    progress_cb=None,
    **_kwargs,
) -> Dict[str, Any]:
    """文生视频（无首帧）。LTX 配置走 LTX t2v（bypass 首帧注入，对齐用户实测工作流）；否则 Wan 5B。"""
    cfg = comfyui_config()
    w, h = _parse_resolution(resolution, fallback=_res_from_ratio(_kwargs.get("ratio")))
    sd = _seed(seed)

    if _h3.is_h3_model(cfg.model):
        w, h = _h3.h3_size(w, h)
        length = _h3.h3_length(duration or 5)
        prompt = await _h3.to_english(prompt)   # 同上：中文会被念出来
        # 🔴 台词/歌词：让 H3 把该说的说出来，嘴型才对得上。
        #    描述必须译成英文（否则整段被当台词念），但台词本身**保持原文不翻**。
        _spk = {"dialogue": (_kwargs.get("dialogue") or "").strip(),
                "speaker": (_kwargs.get("speaker") or "").strip(),
                "singing": bool(_kwargs.get("singing"))}
        # 🔴 没有首帧但有角色参考图 → 走多参考（ref2v）。这是我们失去蚂蚁资产锁脸通道后
        #    的本地替代：把角色图当参考喂进去，H3 会保住人物长相。
        refs_in = [r for r in (_kwargs.get("reference_images") or []) if r]
        ref_names = []
        if refs_in:
            base_url = cfg.base_url.rstrip("/")
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as up:
                for r in refs_in[:4]:
                    try:
                        ref_names.append(await _upload_image(up, base_url, r))
                    except Exception as _e:
                        logger.warning("H3 参考图上传失败，跳过一张: %s", _e)
        if ref_names:
            wf = _h3.build_ref2v(unet=cfg.model, prompt=prompt, width=w, height=h,
                                 length=length, seed=sd, ref_images=ref_names, **_spk)
            _mode = f"ref2v×{len(ref_names)}"
        else:
            wf = _h3.build_i2v(unet=cfg.model, prompt=prompt, width=w, height=h,
                               length=length, seed=sd, **_spk)
            _mode = "t2v"
        out_fps = _h3.H3_FPS
        out_dur = round(length / _h3.H3_FPS, 2)
        if progress_cb:
            try:
                await progress_cb(f"🎞️ MiniMax H3 {_mode}: {_h3.describe(w, h, length)}")
            except Exception:
                pass
    elif _is_ltx(cfg.model):
        # LTX t2v = i2v 同图 bypass 注入；example.png 是 ComfyUI 自带占位图（被 bypass，不参与画面）
        ltx_fps = 25
        max_dur = max(1, (int(cfg.max_frames) - 1) // ltx_fps)
        ltx_dur = min(int(round(duration or 5)), max_dur)
        wf = _i2v_workflow_ltx(cfg, "example.png", prompt, w, h, ltx_dur, ltx_fps, sd, t2v=True)
        out_fps, out_dur = ltx_fps, float(ltx_dur)
    else:
        length = _frames(int(round(duration or 5)), cfg.fps, cfg.max_frames)
        wf = _t2v_workflow(cfg, prompt, w, h, length, sd)
        out_fps, out_dur = cfg.fps, round(length / cfg.fps, 2)
    # 🔴 检查内存 + 跑任务必须在同一把锁里：分开做的话并发提交时两边都会
    #    看到"内存还很空"而放行（详见 _COMFY_GPU_LOCK 的注释）。
    async with _COMFY_GPU_LOCK:
        await _h3.ensure_headroom(cfg.base_url, progress_cb=progress_cb)
        vinfo = await _run_workflow(cfg, wf, progress_cb)
    video_url = await _download_video(cfg, vinfo)
    return {
        "video_url": video_url,
        "task_id": f"comfyui-{uuid.uuid4().hex[:8]}",
        "duration": out_dur,
        "fps": out_fps,
        "resolution": f"{w}x{h}",
        "seed": sd,
        "prompt": prompt,
        "mock": False,
        "model": cfg.model,
        "provider": "comfyui",
    }
