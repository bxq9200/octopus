"""人声分离（1.1.0 MV）—— 把伴奏剥掉再喂 ASR

🔴 为什么必须有这一步（2026-08-26 实测）：
   `glm-asr` 是**语音**识别，不是**歌声**识别。直接喂带伴奏的歌，
   一首 3 分半的儿歌整首只出 8 句、中间有 69 秒完全空白；
   而且偶尔会把「很抱歉，作为一个文本交互的AI，我无法直接处理音频…」
   这种助手拒答话术当成歌词写进结果里。
   试过带通滤波 / 中置提取 / 压缩提亮 / 响度归一化四种 ffmpeg 预处理，**一种都救不了**。

   Demucs 分离后同一首歌：
     A段 3句20字 → 5句34字；B段 2句5字 → 9句26字；C段 7句29字 → 9句38字
     内容也更准（「老师点点头，果香飘呀」→「风儿拍拍手，果实点点头，果香飘」）

   还有个连带好处：伴奏在的时候整首歌**一个静音点都找不到**（-35dB 下 0 个），
   切片只能每 28 秒硬切、经常把一句词劈两半；剥掉伴奏后有 25 个静音点，
   切片能落在真正的乐句间隙上。

依赖是**可选**的：没装 demucs / torch 就退回原混音，识别质量差但流程不断。
"""
import asyncio
import glob
import logging
import os
import shutil
import subprocess
import sys
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_MODEL = "htdemucs"          # 默认模型，质量/速度平衡最好
_TIMEOUT = 1800.0            # CPU 上一首歌可能要几分钟


def _probe_backend() -> tuple[bool, str, str]:
    """(可用?, 设备, 说明)。不抛异常——分离是增强项，不该阻断主流程。"""
    try:
        import demucs  # noqa: F401
    except Exception:
        return False, "", "未安装 demucs（pip install demucs）"
    try:
        import torch
        if torch.cuda.is_available():
            return True, "cuda", f"GPU：{torch.cuda.get_device_name(0)}"
        return True, "cpu", "CPU（没有可用的 CUDA，一首歌可能要几分钟）"
    except Exception as e:
        return False, "", f"torch 不可用：{e}"


def available() -> tuple[bool, str]:
    ok, device, note = _probe_backend()
    return ok, note


async def _run(cmd: list, timeout: float) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(
        subprocess.run, cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


async def separate_vocals(
    audio_path: str,
    work_dir: str,
    *,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """分离人声。成功返回人声 wav 路径，任何原因失败都返回 None（调用方退回原混音）。

    产出的人声轨还做了响度归一化——分离后的人声普遍偏轻（实测 mean -19.6dB），
    直接喂 ASR 会吃亏。
    """
    def emit(msg: str):
        logger.info("[vocal] %s", msg)
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    ok, device, note = _probe_backend()
    if not ok:
        emit(f"跳过人声分离（{note}），直接识别原曲，歌词准确率会明显下降")
        return None

    os.makedirs(work_dir, exist_ok=True)
    out_root = os.path.join(work_dir, "demucs")
    emit(f"正在分离人声与伴奏（{note}）…")

    cmd = [
        sys.executable, "-m", "demucs",
        "--two-stems=vocals",      # 只要人声/非人声两轨，比出四轨快得多
        "-d", device,
        "-o", out_root,
        audio_path,
    ]
    try:
        cp = await _run(cmd, _TIMEOUT)
    except subprocess.TimeoutExpired:
        emit("人声分离超时，改用原曲识别")
        return None
    except Exception as e:
        emit(f"人声分离启动失败（{e}），改用原曲识别")
        return None

    if cp.returncode != 0:
        logger.warning("demucs 失败: %s", (cp.stderr or "")[-400:])
        emit("人声分离失败，改用原曲识别")
        return None

    # demucs 输出在 <out>/<model>/<文件名去扩展名>/vocals.wav
    hits = glob.glob(os.path.join(out_root, "**", "vocals.wav"), recursive=True)
    if not hits:
        logger.warning("demucs 跑完但找不到 vocals.wav，输出目录: %s", out_root)
        emit("人声分离没有产出，改用原曲识别")
        return None

    raw = hits[0]
    normalized = os.path.join(work_dir, "vocals_norm.wav")
    # 分离后人声偏轻，做一遍响度归一化；失败就用原始分离结果，不值得为此中断
    norm = await _run([
        "ffmpeg", "-y", "-i", raw,
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ac", "1", "-ar", "16000", "-vn", normalized,
    ], 300.0)
    if norm.returncode == 0 and os.path.exists(normalized):
        shutil.rmtree(out_root, ignore_errors=True)
        emit("人声分离完成")
        return normalized

    logger.warning("人声轨响度归一化失败，用未归一化的分离结果: %s", (norm.stderr or "")[-200:])
    emit("人声分离完成")
    return raw
