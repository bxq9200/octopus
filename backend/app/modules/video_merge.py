import asyncio
import logging
import os
import subprocess
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.modules.base import BaseModule
from app.core.config import settings
from app.core.paths import videos_dir, resolve_output_path

logger = logging.getLogger(__name__)


_TRANSITIONS = {
    "none": None,
    "fade": "fade",
    "dissolve": "dissolve",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
    "wipeleft": "wipeleft",
    "wiperight": "wiperight",
    "wipeup": "wipeup",
    "wipedown": "wipedown",
    "slideleft": "slideleft",
    "slideright": "slideright",
    "slideup": "slideup",
    "slidedown": "slidedown",
    "circleopen": "circleopen",
    "circleclose": "circleclose",
    "radial": "radial",
    "smoothleft": "smoothleft",
    "smoothright": "smoothright",
    "smoothup": "smoothup",
    "smoothdown": "smoothdown",
    "zoomin": "zoomin",
    "pixelize": "pixelize",
}

# 0.6.1 storyboard 用 snake_case 输出，这里建立到 ffmpeg xfade 名的别名映射
_TRANSITION_ALIAS = {
    "slide": "slideleft",
    "wipe": "wipeleft",
    "circle": "circleopen",
    "smooth": "smoothleft",
    "zoom": "zoomin",
    # snake_case → ffmpeg camelCase
    "wipe_left": "wipeleft",
    "wipe_right": "wiperight",
    "wipe_up": "wipeup",
    "wipe_down": "wipedown",
    "slide_left": "slideleft",
    "slide_right": "slideright",
    "slide_up": "slideup",
    "slide_down": "slidedown",
    "smooth_left": "smoothleft",
    "smooth_right": "smoothright",
    "smooth_up": "smoothup",
    "smooth_down": "smoothdown",
    "circle_open": "circleopen",
    "circle_close": "circleclose",
    # hard_cut 在 _build_xfade_cmd 里被识别为"极短 fade"，视觉上接近无转场
    "hard_cut": "fade",
}

# 标记为"硬切"的来源值（_build_xfade_cmd 单独处理：用 0.05s fade 模拟）
_HARD_CUT_SOURCES = {"hard_cut", "hardcut", "none", "cut"}

DEFAULT_RESOLUTION = "720x1280"
DEFAULT_FPS = 24
AUDIO_SR = 44100



# ═══════════════════════════════════════════════════════
# 1.1.0 MV：把每段视频精确对齐到它该占的时间窗
# ═══════════════════════════════════════════════════════
# 🔴 为什么必须做这件事：
#    Seedance 只接受**整数秒**，而 MV 的镜头窗口是按歌词算出来的小数（7.20s / 4.50s）。
#    一个镜头差 0.2 秒不明显，三十个镜头累积下来就是好几秒——画面和歌整段脱节，
#    而且是那种"看着别扭但说不出哪里不对"的坏。所以合成前逐段校正，误差不累积。
_FIT_SPEED_TOLERANCE = 0.18   # 时长差在 ±18% 以内用变速吸收（肉眼无感），超出就裁/补
_FIT_SKIP_EPS = 0.02          # 差距小于这个就不动它。别放宽到一帧(24fps≈0.042s)以上，
                              # 否则每段都"差一点点"被跳过，几十段累起来就是可见的漂移
_MV_TAIL_MARGIN = 1.2         # 末段多留的定格余量，配合 -shortest 保证歌不被切


async def _probe_seconds(path: str) -> float:
    """读视频时长；读不到返 0。"""
    proc = await asyncio.to_thread(
        subprocess.run,
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        return float((proc.stdout or "").strip())
    except (TypeError, ValueError):
        return 0.0


async def _fit_clip_to_window(path: str, target: float, work_dir: str, idx: int) -> str:
    """把一段视频变成正好 target 秒。返回新文件路径；失败则原样返回。

    策略按差距大小分档：
      · 差 ≤18%  → 调整播放速度（setpts）。运动连续，观感最自然，MV 里完全看不出来。
      · 太长     → 直接裁掉尾巴。
      · 太短很多 → 定格最后一帧补足（tpad），比让画面变超慢动作强。
    """
    actual = await _probe_seconds(path)
    if actual <= 0 or target <= 0 or abs(actual - target) < _FIT_SKIP_EPS:
        return path

    out = os.path.join(work_dir, f"fit_{idx:03d}.mp4")
    ratio = target / actual
    if abs(ratio - 1.0) <= _FIT_SPEED_TOLERANCE:
        # setpts 只改视频轨；音频这里不用管——MV 会整轨替换成原曲
        vf = f"setpts={ratio:.6f}*PTS"
        cmd = ["ffmpeg", "-y", "-i", path, "-vf", vf, "-an",
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", out]
    elif actual > target:
        cmd = ["ffmpeg", "-y", "-i", path, "-t", f"{target:.3f}", "-an",
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", out]
    else:
        cmd = ["ffmpeg", "-y", "-i", path, "-vf", f"tpad=stop_mode=clone:stop_duration={target - actual:.3f}",
               "-t", f"{target:.3f}", "-an",
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", out]

    proc = await asyncio.to_thread(
        subprocess.run, cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0 or not os.path.exists(out):
        logger.warning("片段 %d 对齐失败（%.2fs → %.2fs），用原片: %s",
                       idx, actual, target, (proc.stderr or "")[-200:])
        return path
    logger.info("片段 %d 对齐 %.2fs → %.2fs（比例 %.3f）", idx, actual, target, ratio)
    return out


class VideoMergeModule(BaseModule):
    """视频拼接模块：将多个视频片段拼接成一个完整视频，支持转场效果

    - 自动 ffprobe 探测音轨；缺失音轨用 anullsrc 补齐，全部缺失则不映射音轨。
    - 视频统一 scale + pad + setsar + fps + yuv420p，避免编码不一致导致拼接失败。
    """

    name = "video_merge"
    description = "将多个视频片段拼接成一个完整视频，支持 fade/dissolve/wipe/slide/circle 等多种电影常用转场效果。"
    category = "advanced"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "videos": {
                    "type": "array",
                    "items": {"type": "string", "description": "视频文件路径（本地路径或 /output/...）"},
                    "description": "要拼接的视频片段列表",
                },
                "transition": {
                    "type": "string",
                    "enum": list(_TRANSITIONS.keys()) + list(_TRANSITION_ALIAS.keys()),
                    "default": "fade",
                    "description": "全局转场效果（兜底；优先级低于 transitions 数组）",
                },
                "transitions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "每段独立转场（length=N-1）；不足以全局 transition 兜底；hard_cut=极短 fade",
                },
                "transition_duration": {
                    "type": "number",
                    "default": 0.6,
                    "minimum": 0.1,
                    "maximum": 3.0,
                    "description": "单次转场时长（秒）",
                },
                "output_format": {"type": "string", "default": "mp4"},
                "resolution": {
                    "type": "string",
                    "default": DEFAULT_RESOLUTION,
                    "description": "输出分辨率（默认竖屏，与视频生成保持一致）",
                },
                # 0.6.1: BGM 混入
                "bgm_path": {
                    "type": "string",
                    "description": "BGM 本地路径或 /output/... URL；缺失则不混 BGM",
                },
                "bgm_volume_db": {
                    "type": "number",
                    "default": -12.0,
                    "minimum": -40.0,
                    "maximum": 0.0,
                    "description": "BGM 基线音量（dB）；对白触发 ducking 时会再衰减（仅 bgm_only=False 时有效）",
                },
                "bgm_only": {
                    "type": "boolean",
                    "default": False,
                    "description": "True 时丢弃 Seedance 视频原音轨，最终音轨完全由 BGM 提供（避免 Seedance 自动 BGM 与 Mubert 叠加）",
                },
                "audio_segments": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "audio_director 的 segments（仅元数据，便于排查）",
                },
            },
            "required": ["videos"],
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "video_url": {"type": "string"},
                "duration": {"type": "number"},
                "resolution": {"type": "string"},
            },
            "required": ["video_url"],
        }

    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        raw_videos = input_data.get("videos", [])
        if not raw_videos:
            return {"video_url": "", "duration": 0, "error": "未提供视频片段"}

        video_paths: List[str] = []
        for v in raw_videos:
            if isinstance(v, dict):
                url = v.get("video_url") or ""
            else:
                url = str(v)
            if not url:
                continue
            local = resolve_output_path(url)
            if local.startswith(("http://", "https://")):
                logger.warning("video_merge 暂不支持远程 URL: %s", local)
                continue
            if os.path.exists(local) and os.path.getsize(local) > 1024:
                video_paths.append(local)

        if not video_paths:
            return {"video_url": "", "duration": 0, "error": "没有有效的本地视频片段可合并"}

        resolution = input_data.get("resolution") or DEFAULT_RESOLUTION

        # 1.1.0 MV：把每段裁/补/变速成它该占的时长，让画面严格跟着歌走。
        # 只有传了 target_durations 才生效 —— 普通项目一行行为都不变。
        _fit_dir: Optional[str] = None
        targets = input_data.get("target_durations") or []
        if targets and len(targets) == len(video_paths):
            _fit_dir = os.path.join(os.path.dirname(video_paths[0]), f"_mvfit_{uuid.uuid4().hex[:8]}")
            os.makedirs(_fit_dir, exist_ok=True)
            # 🔴 逐段吸收累积误差，而不是各修各的。
            #    变速后的时长会被量化到帧边界（24fps 下一帧 0.042s），单段差一帧看不出来，
            #    但三十个镜头各差一帧就是一秒多——到歌尾画面明显跑在音乐前面。
            #    做法：每段的目标 = 本段应有时长 - 此前累积的偏差，让误差每段归零。
            fitted, drift = [], 0.0
            _last = len(video_paths) - 1
            for i, (vp, tgt) in enumerate(zip(video_paths, targets)):
                tgt = float(tgt)
                try:
                    adjusted = max(0.5, tgt - drift)      # 下限兜底，别把某段压成 0
                    # 🔴 末段留余量：ffmpeg 用 -shortest，成片取「画面/音轨」里短的那个。
                    #    画面哪怕只短 0.3 秒，歌的结尾就会被生生切掉——而 MV 里歌是产品本身。
                    #    让末段多定格 1.2 秒，画面必然长于歌，-shortest 就把画面尾巴削平：
                    #    成片长度恒等于歌长，残余漂移也一并被吃掉，还省一遍整片重编码。
                    if i == _last:
                        adjusted += _MV_TAIL_MARGIN
                    out = await _fit_clip_to_window(vp, adjusted, _fit_dir, i)
                    real = await _probe_seconds(out)
                    if real > 0:
                        drift += real - tgt
                    fitted.append(out)
                except Exception as e:
                    logger.warning("片段 %d 对齐异常，用原片: %s", i, e)
                    fitted.append(vp)
            logger.info("时间窗对齐完成：%d 段，末段留余量 %.1fs（残余漂移 %.3fs 由 -shortest 吃掉）",
                        len(fitted), _MV_TAIL_MARGIN, drift - _MV_TAIL_MARGIN)
            video_paths = fitted
        elif targets:
            logger.warning("target_durations 数量(%d)与视频段数(%d)对不上，跳过时间窗对齐",
                           len(targets), len(video_paths))

        # 0.6.1: BGM 混音参数（来自 audio 链路；缺失就走原逻辑不混 BGM）
        raw_bgm = input_data.get("bgm_path") or ""
        bgm_path: Optional[str] = None
        if raw_bgm:
            resolved = resolve_output_path(raw_bgm)
            if os.path.exists(resolved) and os.path.getsize(resolved) > 1024:
                bgm_path = resolved
            else:
                logger.warning("video_merge bgm_path 无效，跳过 BGM 混音: %s", raw_bgm)
        try:
            bgm_volume_db = float(input_data.get("bgm_volume_db", -12.0))
        except (TypeError, ValueError):
            bgm_volume_db = -12.0
        bgm_volume_db = max(-40.0, min(0.0, bgm_volume_db))
        # 0.6.2: BGM 开关语义重做
        # - True：Seedance 原音（含对白/口型/环境音）+ BGM 叠加（sidechain ducking 自动压 BGM）
        # - False：完全不混 BGM，只保留 Seedance 原音
        # 之前 0.6.1 末期粗暴丢 Seedance 音轨是误判（当时以为 Seedance auto-audio 是噪声）；
        # 0.6.2 起 Seedance 会按 storyboard.dialogue/narration 做对口型 + 含对白，必须保留
        # 1.1.0 MV：audio_mode="replace" —— 成片的声音**只有那首歌**。
        # 片段自带的环境音/人声在 MV 里全是噪声，必须整轨丢掉；
        # 音量也不能按 BGM 的 -12dB 压，它是主音轨不是背景。
        #
        # 🔴 这段必须排在下面 bgm_only 那道门**之前**：那道门在
        #    bgm_only=False 时会把 bgm_path 直接置成 None，而 MV 的合成节点参数里
        #    恰好带着 bgm_only=False（MV 不建 BGM 节点，那个开关是关的）。
        #    先判 replace 才不会被它误伤——2026-09-08 实测：顺序反了会让整支 MV 没有音轨，
        #    而且时间窗对齐全都正常，日志只有一行"跳过 BGM 混入"，很难联想到是音乐丢了。
        replace_audio = (input_data.get("audio_mode") or "keep") == "replace"

        # 0.6.2: BGM 开关语义重做
        # - True：Seedance 原音（含对白/口型/环境音）+ BGM 叠加（sidechain ducking 自动压 BGM）
        # - False：完全不混 BGM，只保留 Seedance 原音
        # 之前 0.6.1 末期粗暴丢 Seedance 音轨是误判（当时以为 Seedance auto-audio 是噪声）；
        # 0.6.2 起 Seedance 会按 storyboard.dialogue/narration 做对口型 + 含对白，必须保留
        use_bgm = bool(input_data.get("bgm_only", True))
        if not use_bgm and not replace_audio:
            if bgm_path:
                logger.info("video_merge bgm_only=False → 跳过 BGM 混入，仅保留 Seedance 原音")
            bgm_path = None
        # 不再强制丢 Seedance 音轨（_bgm_filters with any_audio=True 会启用 ducking）
        bgm_only = False  # 内部 helper 字段保留语义为"是否丢原音"，普通项目永远 False

        if replace_audio:
            if not bgm_path:
                logger.warning("audio_mode=replace 但没有可用的外部音轨（bgm_path 为空或文件无效），"
                               "成片将没有声音")
            else:
                bgm_only = True
                bgm_volume_db = float(input_data.get("bgm_volume_db", 0.0))
                logger.info("video_merge audio_mode=replace → 丢弃片段原音，只用外部音轨 %s", bgm_path)

        if len(video_paths) == 1:
            return await self._single_video_process(
                video_paths[0], resolution,
                bgm_path=bgm_path, bgm_volume_db=bgm_volume_db,
                bgm_only=bgm_only,
            )

        transition = (input_data.get("transition") or "fade").strip().lower()
        transition = _TRANSITION_ALIAS.get(transition, transition)
        trans_dur = float(input_data.get("transition_duration", 0.6))
        trans_dur = max(0.1, min(3.0, trans_dur))

        # 0.6.1: 每段独立转场（来自 storyboard 的 transition_to_next）
        # 长度对齐 N-1；不足补全局 transition；过长截断；hard_cut 走极短 fade
        raw_transitions = input_data.get("transitions") or []
        per_pair_transitions: List[str] = []
        for idx in range(len(video_paths) - 1):
            raw = raw_transitions[idx] if idx < len(raw_transitions) else transition
            raw = (str(raw or "") or transition).strip().lower()
            if raw in _HARD_CUT_SOURCES:
                per_pair_transitions.append("hard_cut")  # 标记，循环里特殊处理
            else:
                normalized = _TRANSITION_ALIAS.get(raw, raw)
                if normalized not in _TRANSITIONS:
                    normalized = "fade"
                per_pair_transitions.append(normalized)

        # 探测每段视频的时长与是否有音轨
        probes = await asyncio.gather(*(self._probe(p) for p in video_paths))
        durations = [d for d, _ in probes]
        has_audios = [a for _, a in probes]
        any_audio = any(has_audios)

        # 0.6.1+: bgm_only=True → 假装所有视频都无音轨，让 _build_xfade_cmd / _build_concat_filter_cmd
        # 跳过 Seedance 音轨处理；最终音轨完全由 _bgm_filters 用 Mubert 输出
        if bgm_only:
            logger.info("video_merge bgm_only=True → 丢弃 Seedance 音轨，仅用 Mubert BGM")
            has_audios = [False] * len(video_paths)
            any_audio = False

        output_path = os.path.join(videos_dir(), f"merged_{uuid.uuid4().hex[:8]}.mp4")
        xfade_type = _TRANSITIONS.get(transition)

        # 任一片段 ≤ 2*trans_dur 时 xfade 会越界，降级为硬拼接（per-pair 也一起降级，保持简单）
        if xfade_type is not None and any(d <= 2 * trans_dur for d in durations):
            logger.warning(
                "视频片段过短（最短 %.2fs），转场时长 %.2fs 过长，降级为硬拼接",
                min(durations), trans_dur,
            )
            xfade_type = None

        if xfade_type is None:
            cmd = self._build_concat_filter_cmd(
                video_paths, durations, has_audios, any_audio, output_path, resolution,
                bgm_path=bgm_path, bgm_volume_db=bgm_volume_db,
            )
            used_transition = "none"
        else:
            cmd = self._build_xfade_cmd(
                video_paths, durations, has_audios, any_audio,
                output_path, xfade_type, trans_dur, resolution,
                per_pair_transitions=per_pair_transitions,
                bgm_path=bgm_path, bgm_volume_db=bgm_volume_db,
            )
            # 用 per-pair 时返回首段的实际类型当代表（前端展示用）
            used_transition = per_pair_transitions[0] if per_pair_transitions else xfade_type

        proc = await asyncio.to_thread(
            subprocess.run, cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        if proc.returncode != 0:
            error_msg = proc.stderr.decode("utf-8", errors="replace")[-800:]
            return {
                "video_url": "",
                "duration": 0,
                "error": f"FFmpeg 错误: {error_msg}",
                "cmd_head": " ".join(cmd[:12]) + " ...",
            }

        duration = await self._get_duration(output_path)
        return {
            "video_url": f"/output/videos/{os.path.basename(output_path)}",
            "duration": duration,
            "resolution": resolution,
            "transition": used_transition,
            "transition_duration": trans_dur if used_transition != "none" else 0,
            "segments": len(video_paths),
            "has_audio": any_audio or bool(bgm_path),
            "bgm_mixed": bool(bgm_path),
            "bgm_only": bgm_only,
            "bgm_volume_db": bgm_volume_db if bgm_path else None,
        }

    async def _single_video_process(
        self, video_path: str, resolution: str,
        bgm_path: Optional[str] = None, bgm_volume_db: float = -12.0,
        bgm_only: bool = False,
    ) -> Dict[str, Any]:
        """单段：仅做格式/分辨率统一；可选 BGM 混入"""
        output_path = os.path.join(videos_dir(), f"merged_{uuid.uuid4().hex[:8]}.mp4")
        w, h = resolution.split("x")
        _, has_audio = await self._probe(video_path)
        # bgm_only：假装无原音轨，让 _bgm_filters 用 BGM 当唯一音源
        if bgm_only and bgm_path:
            has_audio = False

        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={DEFAULT_FPS},format=yuv420p"
        )

        if bgm_path:
            # 单段视频 + BGM：用 filter_complex 做 ducking
            cmd = ["ffmpeg", "-y", "-i", video_path, "-i", bgm_path]
            v_chain = f"[0:v]{vf}[vout]"
            audio_parts, final_audio = self._bgm_filters(
                any_audio=has_audio, bgm_input_idx=1, bgm_volume_db=bgm_volume_db,
                main_audio_label="[0:a]" if has_audio else None,
            )
            filter_complex = ";".join([v_chain] + audio_parts)
            cmd += [
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-map", final_audio,
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-profile:v", "high",
                "-movflags", "+faststart",
                output_path,
            ]
        else:
            cmd = ["ffmpeg", "-y", "-i", video_path, "-vf", vf]
            if has_audio:
                cmd += ["-c:a", "aac", "-b:a", "128k"]
            else:
                cmd += ["-an"]
            cmd += [
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-profile:v", "high",
                "-movflags", "+faststart",
                output_path,
            ]

        proc = await asyncio.to_thread(
            subprocess.run, cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            return {
                "video_url": "",
                "duration": 0,
                "error": f"FFmpeg 错误: {proc.stderr.decode('utf-8', errors='replace')[-800:]}",
            }

        duration = await self._get_duration(output_path)
        return {
            "video_url": f"/output/videos/{os.path.basename(output_path)}",
            "duration": duration,
            "resolution": resolution,
            "segments": 1,
            "has_audio": has_audio,
        }

    def _v_norm(self, resolution: str) -> str:
        w, h = resolution.split("x")
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={DEFAULT_FPS},format=yuv420p"
        )

    @staticmethod
    def _bgm_filters(
        any_audio: bool,
        bgm_input_idx: int,
        bgm_volume_db: float,
        main_audio_label: Optional[str] = "[aout]",
    ) -> Tuple[List[str], str]:
        """0.6.1: 生成 BGM 混音 filter_complex 片段（含对白触发的 ducking）。

        Returns: (filter_parts_to_append, final_audio_label_for_map)
        - 调用方需自己把 ["-i", bgm_path] 加到 ffmpeg inputs
        - bgm_input_idx 是 bgm 在 -i 序列里的 0-based 位置（通常 == 视频数）
        - main_audio_label = 已有的合并对白音轨标签（多段：[aout]；单段：[0:a]）
          若 any_audio=False 则忽略，BGM 直接作为最终音轨
        """
        vol_clause = f"volume={bgm_volume_db}dB"
        if any_audio and main_audio_label:
            return (
                [
                    f"[{bgm_input_idx}:a]{vol_clause}[bgm_v]",
                    # asplit 让 [aout] 复制：一份触发 ducking，一份参与最终混音
                    f"{main_audio_label}asplit=2[aout_trig][aout_main]",
                    # primary=BGM, sidechain=对白 → 输出 ducked BGM（对白时 BGM 自动衰减）
                    f"[bgm_v][aout_trig]sidechaincompress=threshold=0.05:ratio=8:attack=5:release=200[bgm_ducked]",
                    f"[aout_main][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=0[aout_mix]",
                ],
                "[aout_mix]",
            )
        # 无主音轨 → BGM 即最终音轨
        return (
            [f"[{bgm_input_idx}:a]{vol_clause}[aout_mix]"],
            "[aout_mix]",
        )

    @staticmethod
    def _a_norm() -> str:
        return f"aresample=async=1000,aformat=channel_layouts=stereo:sample_rates={AUDIO_SR}"

    @classmethod
    def _silent_audio_src(cls, duration: float) -> str:
        # 用源滤镜 anullsrc 合成静音，时长精确到毫秒
        return f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}:d={duration:.3f}"

    def _build_concat_filter_cmd(
        self,
        videos: List[str],
        durations: List[float],
        has_audios: List[bool],
        any_audio: bool,
        output: str,
        resolution: str,
        bgm_path: Optional[str] = None,
        bgm_volume_db: float = -12.0,
    ) -> List[str]:
        """用 concat 滤镜拼接（替代 concat demuxer），所有片段先归一化"""
        inputs: List[str] = []
        for v in videos:
            inputs.extend(["-i", v])

        v_norm = self._v_norm(resolution)
        parts: List[str] = []
        for i in range(len(videos)):
            parts.append(f"[{i}:v]{v_norm}[v{i}n]")
            if any_audio:
                if has_audios[i]:
                    parts.append(f"[{i}:a]{self._a_norm()}[a{i}n]")
                else:
                    parts.append(f"{self._silent_audio_src(durations[i])}[a{i}n]")

        concat_inputs = "".join(
            f"[v{i}n][a{i}n]" if any_audio else f"[v{i}n]"
            for i in range(len(videos))
        )
        a_flag = 1 if any_audio else 0
        if any_audio:
            parts.append(f"{concat_inputs}concat=n={len(videos)}:v=1:a={a_flag}[vout][aout]")
        else:
            parts.append(f"{concat_inputs}concat=n={len(videos)}:v=1:a={a_flag}[vout]")

        # 0.6.1: BGM 混入（如有）
        if bgm_path:
            bgm_idx = len(videos)
            inputs = list(inputs) + ["-i", bgm_path]
            bgm_parts, final_audio = self._bgm_filters(
                any_audio=any_audio, bgm_input_idx=bgm_idx,
                bgm_volume_db=bgm_volume_db, main_audio_label="[aout]",
            )
            parts.extend(bgm_parts)
            audio_map = final_audio
        else:
            audio_map = "[aout]" if any_audio else None

        filter_complex = ";".join(parts)
        cmd = [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
        ]
        if audio_map:
            cmd += ["-map", audio_map, "-c:a", "aac", "-b:a", "128k"]
            if bgm_path:
                cmd += ["-shortest"]
        cmd += [
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-movflags", "+faststart",
            output,
        ]
        return cmd

    def _build_xfade_cmd(
        self,
        videos: List[str],
        durations: List[float],
        has_audios: List[bool],
        any_audio: bool,
        output: str,
        xfade_type: str,
        trans_dur: float,
        resolution: str,
        per_pair_transitions: List[str] = None,
        bgm_path: Optional[str] = None,
        bgm_volume_db: float = -12.0,
    ) -> List[str]:
        """xfade 串联：第 i 段尾 trans_dur 秒与第 i+1 段头 trans_dur 秒交叉淡变。

        0.6.1: per_pair_transitions 是 length=N-1 数组（来自分镜导演 transition_to_next）。
        - "hard_cut" 元素用 0.05s 极短 fade（视觉接近无转场）
        - 其它元素用 trans_dur 默认时长
        - 不提供 / 元素缺失 → 该段用全局 xfade_type
        """
        per_pair_transitions = per_pair_transitions or []
        inputs: List[str] = []
        for v in videos:
            inputs.extend(["-i", v])

        v_norm = self._v_norm(resolution)
        parts: List[str] = []
        for i in range(len(videos)):
            parts.append(f"[{i}:v]{v_norm}[v{i}n]")
            if any_audio:
                if has_audios[i]:
                    parts.append(f"[{i}:a]{self._a_norm()}[a{i}n]")
                else:
                    parts.append(f"{self._silent_audio_src(durations[i])}[a{i}n]")

        prev_v = "v0n"
        prev_a = "a0n"
        cumulative = durations[0]
        for i in range(1, len(videos)):
            # 0.6.1: 该 pair 的转场类型与时长
            pair_idx = i - 1
            raw_pair = per_pair_transitions[pair_idx] if pair_idx < len(per_pair_transitions) else None
            if raw_pair == "hard_cut":
                # ffmpeg xfade duration < 0.1s 会静默失败把后面整条链废掉
                # （亲测：8 段视频里有 1 个 hard_cut=0.05s，输出只剩前 2 段约 13s）
                # 0.2s 视觉上仍接近硬切但 ffmpeg 稳
                pair_trans = "fade"
                pair_dur = 0.2
            elif raw_pair:
                pair_trans = raw_pair
                pair_dur = trans_dur
            else:
                pair_trans = xfade_type
                pair_dur = trans_dur

            offset = max(0.0, cumulative - pair_dur)
            # xfade 内部会升级到 yuv444p；倒数第二段输出后我们追加一次 format=yuv420p
            # 让最终 vout 落在 420（多数播放器/浏览器只支持 420）
            is_last = i == len(videos) - 1
            xfade_out = "vxfade_last" if is_last else f"vx{i}"
            parts.append(
                f"[{prev_v}][v{i}n]xfade=transition={pair_trans}:"
                f"duration={pair_dur}:offset={offset:.3f}[{xfade_out}]"
            )
            if is_last:
                # 末段把 xfade 升级出来的 yuv444p 拉回 yuv420p
                parts.append(f"[{xfade_out}]format=yuv420p[vout]")
                prev_v = "vout"
            else:
                prev_v = xfade_out
            if any_audio:
                out_a = f"ax{i}" if i < len(videos) - 1 else "aout"
                parts.append(f"[{prev_a}][a{i}n]acrossfade=d={pair_dur}[{out_a}]")
                prev_a = out_a
            cumulative += durations[i] - pair_dur

        # 0.6.1: BGM 混入（如有）
        if bgm_path:
            bgm_idx = len(videos)  # bgm -i 紧跟在视频后
            inputs = list(inputs) + ["-i", bgm_path]
            bgm_parts, final_audio = self._bgm_filters(
                any_audio=any_audio, bgm_input_idx=bgm_idx,
                bgm_volume_db=bgm_volume_db, main_audio_label="[aout]",
            )
            parts.extend(bgm_parts)
            audio_map = final_audio
        else:
            audio_map = "[aout]" if any_audio else None

        filter_complex = ";".join(parts)
        cmd = [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
        ]
        if audio_map:
            cmd += ["-map", audio_map, "-c:a", "aac", "-b:a", "128k"]
            if bgm_path:
                cmd += ["-shortest"]
        cmd += [
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p",   # 双保险：encoder 强制 420
            "-profile:v", "high",
            "-movflags", "+faststart",
            output,
        ]
        return cmd

    async def _probe(self, video_path: str) -> Tuple[float, bool]:
        """返回 (duration_seconds, has_audio)"""
        if not os.path.exists(video_path):
            return 0.0, False
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type:format=duration",
                "-of", "default=noprint_wrappers=1", video_path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        text = proc.stdout.decode("utf-8", errors="replace")
        duration = 0.0
        has_audio = False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("duration="):
                try:
                    duration = float(line.split("=", 1)[1])
                except ValueError:
                    pass
            elif line == "codec_type=audio":
                has_audio = True
        return duration, has_audio

    async def _get_duration(self, video_path: str) -> float:
        d, _ = await self._probe(video_path)
        return d
