"""智谱 GLM-ASR 歌词转写（1.1.0 MV 模式）

复用用户已配的智谱 key（图片生成那一份），不额外要用户配置。

🔴 三条实测撞出来的约束，文档一条都没写（2026-08-26 探针）：

  ① 模型名必须用 "glm-asr"，**不能用 "glm-asr-2512"**
     反直觉：带版本号的那个只返回整段 text，**不返回 segments**（没有时间戳）；
     不带版本号的反而默认就给逐句 segments{start,end,text}。
     按文档抄版本号会直接丢掉整个时间轴，而且不报错——只是悄悄少个字段。

  ② 只收单声道。歌曲都是立体声，直传报 "transcriptions文件只支持单声道"。
     → 交给 _audio_analysis.extract_mono_slice 统一降混。

  ③ 单次只收 30 秒（"transcriptions文件时长限制为0-30秒"）。
     → 必须切片后逐片转写，再把每片的时间戳加上该片起点偏移，拼回全局时间轴。

串行发请求：智谱有 429 频控（跟额度无关），历史上图片生成就是因为并发被打回、
最后改串行+限速才稳的。一首 4 分钟的歌约 10 片、18 秒左右，是一次性成本，不值当冒险。
"""
import asyncio
import logging
import os
import shutil
import uuid
from typing import Callable, Dict, List, Optional

import httpx

from app.core.config import settings
from app.core.paths import resolve_output_path
from app.core.runtime_config import zhipu_image
from app.modules import _audio_analysis as aa
from app.modules._vocal_separation import separate_vocals

logger = logging.getLogger(__name__)

ASR_MODEL = "glm-asr"          # 🔴 别改成 glm-asr-2512，见文件头 ①
_TIMEOUT = 180.0
_MAX_RETRY = 3

# 幻听过滤：切片边界的碎音常被转成「嗯。」「啊」这类一两个字的短句
_NOISE_MAX_CHARS = 2
_NOISE_MAX_DUR = 0.6

# 🔴 模型幻觉过滤（2026-08-26 实测撞到）：GLM-ASR 偶尔不转写、改而吐出助手话术，
#    实测在一首儿歌里凭空冒出整段「抱歉，作为一个文本交互的AI，我无法直接处理或转录
#    音频文件。不过，我可以帮助你理解如何进行音频转录：1. **使用音频编辑软件**…」
#    这种东西不拦就会被当成歌词喂给编剧，整支 MV 的剧本都会歪掉。
#
#    主判据是**字数密度**：那段 200 字塞在 1.4 秒里 ≈ 143 字/秒，人类唱不出来。
#    正常演唱 3~6 字/秒，最快的说唱也就 10 字/秒左右，15 是很宽松的上限。
_MAX_CHARS_PER_SECOND = 15
# 副判据：元话语关键词。短一点的幻觉（「请将这段音频转录成文字。」）密度不够高，
# 靠这条兜住。这些词组在歌词里出现的概率极低。
_META_MARKERS = (
    "作为一个", "作为一名", "我无法", "无法直接", "抱歉", "对不起，我",
    "转录成文字", "转录音频", "音频文件", "语音识别", "AI助手", "人工智能助手",
    "请将这段", "以下是", "希望这些", "如果你需要",
)


class AsrError(RuntimeError):
    pass


def _clean(text: str) -> str:
    return (text or "").strip()


def _is_noise(text: str, start: float, end: float) -> bool:
    t = _clean(text).strip("。，、.,!?！？ ")
    return len(t) <= _NOISE_MAX_CHARS and (end - start) < _NOISE_MAX_DUR


def _is_hallucination(text: str, start: float, end: float) -> Optional[str]:
    """判断这条是不是模型幻觉。是就返回原因（用于日志），不是返回 None。"""
    t = _clean(text)
    if not t:
        return None
    span = max(0.05, end - start)
    cps = len(t) / span
    if cps > _MAX_CHARS_PER_SECOND:
        return f"字数密度 {cps:.0f} 字/秒（上限 {_MAX_CHARS_PER_SECOND}）"
    # 换行/markdown 列表是模型在「写回答」而不是「转写歌词」的强信号
    if "\n" in t or "**" in t:
        return "含换行或 markdown 排版"
    for m in _META_MARKERS:
        if m in t:
            return f"含元话语「{m}」"
    return None


async def _post_slice(client: httpx.AsyncClient, url: str, key: str, path: str) -> List[Dict]:
    """转写单个切片，返回该片内的 segments（相对时间，未加偏移）。"""
    last_err = ""
    for attempt in range(1, _MAX_RETRY + 1):
        try:
            with open(path, "rb") as f:
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": (os.path.basename(path), f.read(), "audio/wav")},
                    data={"model": ASR_MODEL},
                )
        except Exception as e:                       # 网络抖动
            last_err = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1.5 * attempt)
            continue

        if r.status_code == 200:
            return (r.json() or {}).get("segments") or []

        # 🔴 "no audio segment found" 在歌里是常态不是错误：前奏、间奏、尾奏都是纯器乐，
        #    整片没有人声。当空结果处理，别重试也别报错，否则每首歌都刷一堆假警报。
        if r.status_code == 400 and "no audio segment found" in r.text:
            return []

        last_err = f"HTTP {r.status_code} {r.text[:200]}"
        if r.status_code == 429:                     # 频控：退避后重试
            await asyncio.sleep(2.0 * attempt)
            continue
        break                                        # 其他错误重试也没用

    raise AsrError(last_err)


async def transcribe_song(
    audio: str,
    *,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Dict:
    """把整首歌转成带时间戳的歌词轴。

    返回 {duration, lines: [{start, end, text}], text, slices, noise_db}
    lines 已按全局时间排序、已过滤幻听碎片。
    """
    cfg = zhipu_image()
    key = (cfg.api_key or "").strip()
    if not key:
        raise AsrError("智谱 API Key 没配置。请到「系统设置 → 图片生成」填入智谱 Key（歌词识别复用同一份）。")
    endpoint = (cfg.base_url or "https://open.bigmodel.cn/api/paas/v4").rstrip("/") + "/audio/transcriptions"

    src = resolve_output_path(audio)
    if not os.path.exists(src):
        raise AsrError(f"音频文件不存在：{audio}")

    def emit(msg: str) -> None:
        logger.info("[ASR] %s", msg)
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    duration = await aa.probe_duration(src)
    if duration <= 0:
        raise AsrError("读不出音频时长，文件可能损坏或不是有效的音频格式。")

    work_dir = os.path.join(settings.OUTPUT_DIR, "tmp", f"asr_{uuid.uuid4().hex[:10]}")
    os.makedirs(work_dir, exist_ok=True)
    # 人声轨要活过这次调用：后面「导入歌词」按乐句对齐时还要用它，
    # 重新分离一次要几十秒（CPU 上是几分钟），不值当。放在音频旁边、随项目一起留存。
    keep_dir = os.path.dirname(src)
    kept_vocals = os.path.join(keep_dir, os.path.splitext(os.path.basename(src))[0] + ".vocals.wav")

    lines: List[Dict] = []
    dropped: List[str] = []
    slices: List[tuple] = []
    noise_db: int = 0
    vocals: Optional[str] = None
    try:
        # 🔴 先剥伴奏。带伴奏直接识别基本听不出词——详见 _vocal_separation 顶部实测数据。
        #    分离失败会返回 None，退回原混音（质量差但流程不断）。
        #    注意：这里只影响**识别**，成片音轨用的始终是用户上传的原曲。
        if os.path.exists(kept_vocals) and os.path.getsize(kept_vocals) > 1024:
            vocals = kept_vocals               # 重跑时直接复用，不再分离一遍
            emit("复用已分离的人声轨")
        else:
            vocals = await separate_vocals(src, work_dir, progress_cb=emit)
            if vocals:
                try:
                    shutil.copyfile(vocals, kept_vocals)
                    vocals = kept_vocals
                except OSError as e:
                    logger.warning("人声轨留存失败（不影响本次识别）: %s", e)
        asr_src = vocals or src
        emit(f"音频时长 {duration:.1f} 秒，正在寻找乐句停顿点…")

        # 静音点也要在人声轨上找：伴奏在的时候整首歌一个静音点都没有，
        # 只能每 28 秒硬切、把词劈两半；剥掉伴奏后才切得到真正的乐句间隙。
        cut_points, noise_db = await aa.find_cut_points(asr_src, duration)
        slices = aa.plan_slices(duration, cut_points)
        emit(f"切成 {len(slices)} 片（静音阈值 {noise_db}dB），开始逐片识别歌词…")

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for idx, (start, end) in enumerate(slices):
                part = os.path.join(work_dir, f"s{idx:03d}.wav")
                if not await aa.extract_mono_slice(asr_src, start, end, part):
                    continue                          # 单片切失败不该毁掉整首歌
                try:
                    segs = await _post_slice(client, endpoint, key, part)
                except AsrError as e:
                    logger.warning("片%02d [%.1f-%.1f] 识别失败，跳过：%s", idx, start, end, e)
                    continue
                finally:
                    try:
                        os.remove(part)
                    except OSError:
                        pass

                for s in segs:
                    st, en = start + float(s.get("start", 0)), start + float(s.get("end", 0))
                    txt = _clean(s.get("text", ""))
                    if not txt or _is_noise(txt, st, en):
                        continue
                    why = _is_hallucination(txt, st, en)
                    if why:
                        # 记下来但别抛：这是模型的毛病，不该让整首歌失败
                        logger.warning("丢弃幻觉片段 [%.1f-%.1f] %s：%s", st, en, why, txt[:60])
                        dropped.append(why)
                        continue
                    lines.append({"start": round(st, 2), "end": round(en, 2), "text": txt})

                emit(f"已识别 {idx + 1}/{len(slices)} 片，累计 {len(lines)} 句")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    lines.sort(key=lambda x: x["start"])
    # 切片边界偶尔会让相邻两片转出同一句，去掉时间重叠的重复
    deduped: List[Dict] = []
    for ln in lines:
        if deduped and ln["text"] == deduped[-1]["text"] and ln["start"] < deduped[-1]["end"] + 0.3:
            continue
        deduped.append(ln)

    if not deduped:
        raise AsrError("没识别出任何歌词。可能是纯器乐，或人声被伴奏盖住了。")

    if dropped:
        emit(f"识别完成：{len(deduped)} 句歌词（已丢弃 {len(dropped)} 条模型幻觉）")
    else:
        emit(f"识别完成：{len(deduped)} 句歌词")
    return {
        "duration": round(duration, 2),
        "lines": deduped,
        "text": "".join(x["text"] for x in deduped),
        "slices": len(slices),
        "noise_db": noise_db,
        "vocal_separated": bool(vocals),   # False 时界面该提示「歌词准确率会明显偏低」
        "dropped_hallucinations": len(dropped),
        "vocals_path": vocals if vocals and os.path.exists(vocals) else "",
    }
