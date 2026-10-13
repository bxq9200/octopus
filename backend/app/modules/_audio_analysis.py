"""音频分析 —— MV 模式的时间轴基础设施（1.1.0）

给一首歌，产出后续所有环节要用的时间信息：
  - 精确时长（ffprobe）
  - 静音点（ffmpeg silencedetect）→ 安全切点
  - 单声道 16k 切片（喂 ASR 用）

🔴 为什么要按静音点切、而不是每 30 秒等分：
   等分会把一句歌词从中间劈开，两片各转出半句，拼回来就是碎的。
   在停顿处切，句子能完整落在片内。实测 254 秒音频 → 10 片 → 68 句，零重叠零空隙。

🔴 两条硬约束来自智谱 GLM-ASR（文档没写，实测撞出来的，见 _zhipu_asr.py 顶部注释）：
   ① 只收单声道 —— 歌曲都是立体声，必须先 -ac 1 降混
   ② 单次只收 30 秒 —— 3~5 分钟的歌必须切片后逐片转写
"""
import asyncio
import logging
import os
import re
import subprocess
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── ASR 侧约束 ───
ASR_HARD_LIMIT = 30.0      # 智谱的硬上限
ASR_MAX_SLICE = 28.0       # 留 2 秒余量，避免边界抖动被拒
ASR_MIN_SLICE = 5.0        # 切点不能太密，否则片太碎、请求次数暴涨
ASR_SAMPLE_RATE = 16000    # ASR 惯例采样率，同时大幅缩小上传体积

# 静音检测阈值阶梯：从严到松。歌曲是连续伴奏，-35dB 可能一个点都找不到，
# 于是逐级放松；越松的阈值越可能切在乐句中间，所以只在严的那档找不够时才降级。
_SILENCE_LADDER = [
    (-35, 0.30),
    (-30, 0.25),
    (-25, 0.20),
]


async def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    """ffmpeg/ffprobe 是阻塞的，丢线程池，别卡住事件循环。"""
    return await asyncio.to_thread(
        subprocess.run, cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # Windows 上 ffmpeg 输出可能带非 UTF-8 字节，errors='replace' 防解码崩
        text=True, errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


async def probe_duration(path: str) -> float:
    """音频精确时长（秒）。拿不到返回 0.0。"""
    cp = await _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0", path,
    ])
    try:
        return float((cp.stdout or "").strip())
    except (TypeError, ValueError):
        logger.warning("ffprobe 拿不到时长: %s / %s", path, (cp.stderr or "")[:200])
        return 0.0


async def detect_silence_starts(path: str, *, noise_db: int, min_dur: float) -> List[float]:
    """用 ffmpeg silencedetect 找静音起点（秒）。"""
    cp = await _run([
        "ffmpeg", "-i", path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
        "-f", "null", "-",
    ])
    # silencedetect 把结果写在 stderr
    return [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", cp.stderr or "")]


async def find_cut_points(path: str, duration: float) -> Tuple[List[float], int]:
    """找安全切点。返回 (切点列表, 实际生效的 noise_db)。

    走阈值阶梯：用**能凑够数量**的最严那一档。数量标准是「平均每个 max_slice 窗口
    至少有一个候选」，够了就不再放松——越松越可能切在乐句中间。
    """
    need = max(1, int(duration / ASR_MAX_SLICE))
    loosest: Tuple[List[float], int] = ([], _SILENCE_LADDER[-1][0])
    for noise_db, min_dur in _SILENCE_LADDER:
        pts = await detect_silence_starts(path, noise_db=noise_db, min_dur=min_dur)
        logger.info("silencedetect noise=%ddB d=%.2f → %d 个点（需要 ≥%d）", noise_db, min_dur, len(pts), need)
        if len(pts) >= need:
            return pts, noise_db
        if len(pts) > len(loosest[0]):
            loosest = (pts, noise_db)
    # 一档都没凑够：拿最多的那档凑合，plan_slices 会用硬切兜底
    logger.warning("静音点不足（最多 %d 个），部分切片将走硬切", len(loosest[0]))
    return loosest


def plan_slices(duration: float, cut_points: List[float]) -> List[Tuple[float, float]]:
    """规划切片区间。贪心：每片尽量长（少发请求），但切点必须落在静音处。

    找不到可用静音点时硬切在 max_slice —— 宁可切碎一句，也不能超 30 秒被整片拒掉。
    """
    if duration <= ASR_MAX_SLICE:
        return [(0.0, duration)]

    pts = sorted(cut_points)
    bounds, cur = [0.0], 0.0
    while cur < duration - 0.5:
        window_lo, window_hi = cur + ASR_MIN_SLICE, cur + ASR_MAX_SLICE
        cands = [p for p in pts if window_lo < p <= window_hi]
        nxt = max(cands) if cands else min(window_hi, duration)
        if nxt <= cur:                      # 兜底：防止极端情况下原地踏步转成死循环
            nxt = min(cur + ASR_MAX_SLICE, duration)
        bounds.append(round(min(nxt, duration), 2))
        cur = bounds[-1]

    # 尾片太短（歌末静音尾巴常造成 1~2 秒的碎片）：并回上一片重新对半分。
    # 不能直接拼接——上一片最长 28 秒，加上尾巴就可能破 30 秒上限被整片拒掉。
    if len(bounds) >= 3 and (bounds[-1] - bounds[-2]) < ASR_MIN_SLICE:
        lo, hi = bounds[-3], bounds[-1]
        del bounds[-2]
        if hi - lo > ASR_MAX_SLICE:
            bounds.insert(-1, round((lo + hi) / 2, 2))

    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


# 人声乐句检测的阈值阶梯：从粗到细。段数随阈值放松而增多
# （实测一首 3.5 分钟儿歌：-32dB/0.35s → 22 段；-26dB/0.12s → 44 段）
_PHRASE_LADDER = [
    (-32, 0.35), (-30, 0.20), (-28, 0.15), (-26, 0.12), (-24, 0.10),
]
_PHRASE_MIN_LEN = 0.5      # 短于此的段当噪声丢掉


async def detect_vocal_phrases(
    path: str, duration: float, *, target_count: Optional[int] = None,
) -> List[Tuple[float, float]]:
    """从**人声轨**检测「哪些时刻在唱」，返回乐句区间。

    静音检测的反面：静音之间就是演唱。这件事比 ASR 可靠得多——
    同一首歌 ASR 只覆盖 32%，而这里能检出 89% 的演唱时长。

    target_count 给了就按它挑粒度：贴进来多少行歌词，就找多少个乐句。
    行数本身是最好的先验，比任何固定阈值都准。
    """
    best: List[Tuple[float, float]] = []
    best_gap = None

    for noise_db, min_sil in _PHRASE_LADDER:
        cp = await _run([
            "ffmpeg", "-i", path,
            "-af", f"silencedetect=noise={noise_db}dB:d={min_sil}",
            "-f", "null", "-",
        ])
        err = cp.stderr or ""
        starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", err)]
        ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", err)]

        spans: List[Tuple[float, float]] = []
        cur = 0.0
        for s, e in zip(starts, ends + [duration]):
            if s - cur > _PHRASE_MIN_LEN:
                spans.append((round(cur, 2), round(min(s, duration), 2)))
            cur = e
        if duration - cur > _PHRASE_MIN_LEN:
            spans.append((round(cur, 2), round(duration, 2)))

        if not spans:
            continue
        if target_count is None:
            best = spans
            break
        gap = abs(len(spans) - target_count)
        if best_gap is None or gap < best_gap:
            best, best_gap = spans, gap
        if gap == 0:
            break

    logger.info("检出 %d 个人声乐句（目标 %s）", len(best), target_count or "不限")
    return best


async def extract_mono_slice(src: str, start: float, end: float, dst: str) -> bool:
    """截 [start, end) 一段，转单声道 16k wav。成功返 True。"""
    cp = await _run([
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src,
        "-ac", "1", "-ar", str(ASR_SAMPLE_RATE),
        "-vn",                              # 有些 mp3 带封面图，当视频流会让 wav 封装失败
        dst,
    ])
    if cp.returncode != 0 or not os.path.exists(dst):
        logger.warning("切片失败 [%.1f-%.1f]: %s", start, end, (cp.stderr or "")[-300:])
        return False
    return True
