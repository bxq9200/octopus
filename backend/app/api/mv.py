"""MV 模式 API（1.1.0）

  POST /api/mv/{project_id}/start           传歌 + 传风格图 + 选类型 → 开跑
  PUT  /api/mv/{project_id}/lyrics          校对界面回写歌词时间轴
  POST /api/mv/{project_id}/preview-shots   按当前歌词轴**本地**试算镜头切分

🔴 preview-shots 为什么必须是本地算的：
   _mv_timeline 是纯算法、不含 prompt，被有意留在客户端（没进护城河剥离名单）。
   用户在校对界面拖一句词的时间，镜头切分要**立刻**跟着变——为这个往返一次云端
   既慢又费钱，而且断网就完全不能用。
"""
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.workflow import _ensure_project_hydrated
from app.core.websocket import ws_manager
from app.engine.interactive_executor import InteractiveExecutor
from app.engine.workflow_state import workflow_state
from app.modules._audio_analysis import detect_vocal_phrases
from app.modules._lyrics_align import import_lyrics
from app.modules._mv_timeline import plan_shot_windows, verify_windows

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mv", tags=["mv"])


class StartMVRequest(BaseModel):
    audio_path: str                                  # /output/uploads/{pid}/xxx.mp3
    type: str = "narrative"                          # narrative（叙事型）/ visual（意象型）
    style_refs: List[str] = []                       # 风格参考图 URL
    style_preset: str = ""                           # 风格预设文字
    user_brief: str = ""                             # 创作者额外要求
    aspect_ratio: str = "16:9"                       # 画幅；MV 默认横屏


class LyricLine(BaseModel):
    start: float
    end: float
    text: str


class UpdateLyricsRequest(BaseModel):
    lines: List[LyricLine]


class ImportLyricsRequest(BaseModel):
    content: str                 # LRC 全文，或纯文本歌词（一行一句）
    format: str = "text"         # "lrc" | "text"


class PreviewShotsRequest(BaseModel):
    lines: List[LyricLine]
    sections: Optional[List[Dict[str, Any]]] = None
    duration: float


async def _resolve_vocals(result: Dict[str, Any], project_id: str) -> str:
    """拿到人声轨路径。按「结果里记的 → 音频旁边的 → 现分离」三级找。

    第三级是为了兼容**加人声轨留存之前跑出来的老节点**：那种节点没有 vocals_path，
    没有兜底的话纯文本歌词就只能均分时间，对齐质量白白掉一档。
    GPU 上分离一首歌只要几秒，值得现做。
    """
    from app.core.paths import resolve_output_path
    from app.modules._vocal_separation import separate_vocals

    recorded = result.get("vocals_path") or ""
    if recorded and os.path.exists(recorded):
        return recorded

    audio = resolve_output_path(result.get("audio_path") or "")
    if not audio or not os.path.exists(audio):
        logger.warning("[mv] 项目 %s 连原音频都找不到，纯文本歌词只能靠锚点均摊", project_id)
        return ""

    beside = os.path.splitext(audio)[0] + ".vocals.wav"
    if os.path.exists(beside) and os.path.getsize(beside) > 1024:
        return beside

    logger.info("[mv] 项目 %s 没有留存的人声轨，现场分离一次", project_id)
    got = await separate_vocals(audio, os.path.dirname(audio))
    if not got:
        return ""
    try:
        if got != beside:
            import shutil
            shutil.copyfile(got, beside)
            return beside
    except OSError as e:
        logger.warning("[mv] 人声轨留存失败（不影响本次对齐）: %s", e)
    return got


def _executor(project_id: str) -> InteractiveExecutor:
    return InteractiveExecutor(
        project_id=project_id,
        ws_callback=lambda msg: ws_manager.broadcast(project_id, msg),
    )


@router.post("/{project_id}/start")
async def start_mv(project_id: str, req: StartMVRequest):
    """开始生成 MV。"""
    if not req.audio_path:
        raise HTTPException(400, "请先上传歌曲")
    if req.type not in ("narrative", "visual"):
        raise HTTPException(400, f"未知的 MV 类型：{req.type}")
    if req.aspect_ratio not in ("16:9", "9:16", "1:1", "4:3", "3:4"):
        raise HTTPException(400, f"不支持的画幅：{req.aspect_ratio}")

    await _ensure_project_hydrated(project_id)
    result = await _executor(project_id).start_mv({
        "audio_path": req.audio_path,
        "type": req.type,
        "style_refs": req.style_refs or [],
        "style_preset": req.style_preset or "",
        "user_brief": req.user_brief or "",
        "aspect_ratio": req.aspect_ratio,
    })
    result["project_id"] = project_id
    return result


@router.put("/{project_id}/lyrics")
async def update_lyrics(project_id: str, req: UpdateLyricsRequest):
    """校对界面回写歌词时间轴。

    只改 mv_audio 节点结果里的 lyrics；曲式段落与风格保持不动。
    下游 MV 分镜重跑时会主动读这里的最新值（见 _run_mv_storyboard）。
    """
    await _ensure_project_hydrated(project_id)
    nodes = workflow_state.get_nodes_by_phase(project_id, "mv_audio") or []
    if not nodes:
        raise HTTPException(404, "这个项目还没有歌曲解析结果")
    node = nodes[-1]

    lines = sorted(
        [{"start": round(l.start, 2), "end": round(l.end, 2), "text": (l.text or "").strip()}
         for l in req.lines if (l.text or "").strip()],
        key=lambda x: x["start"],
    )
    if not lines:
        raise HTTPException(400, "歌词不能全空")

    result = dict(node.get("result") or {})
    result["lyrics"] = lines
    result["lyrics_edited"] = True          # 让界面能标出「已人工校对」
    workflow_state.update_node(project_id, node["id"], {"result": result})
    logger.info("[mv] 项目 %s 歌词轴已人工校对：%d 句", project_id, len(lines))
    return {"ok": True, "count": len(lines)}


@router.post("/{project_id}/lyrics/import")
async def import_lyrics_api(project_id: str, req: ImportLyricsRequest):
    """导入歌词：LRC 直接用它自带的时间戳；纯文本按人声乐句对齐。

    🔴 这条路存在的理由：GLM-ASR 对着伴奏唱歌只能听出三成、还带错字，
       而「哪一刻在唱」从人声轨检测能到九成准。所以时间交给音频、文字交给创作者，
       模型一步都不参与猜。
    """
    if not (req.content or "").strip():
        raise HTTPException(400, "歌词内容是空的")
    if req.format not in ("lrc", "text"):
        raise HTTPException(400, f"未知的歌词格式：{req.format}")

    await _ensure_project_hydrated(project_id)
    nodes = workflow_state.get_nodes_by_phase(project_id, "mv_audio") or []
    if not nodes:
        raise HTTPException(404, "请先上传歌曲并等歌曲解析完成，再导入歌词")
    node = nodes[-1]
    result = dict(node.get("result") or {})
    duration = float(result.get("duration") or 0)
    if duration <= 0:
        raise HTTPException(400, "拿不到歌曲时长，请重跑歌曲解析")

    phrases = []
    if req.format == "text":
        vocals = await _resolve_vocals(result, project_id)
        if vocals:
            # 贴了多少行就找多少个乐句 —— 行数是最好的先验
            from app.modules._lyrics_align import split_plain_lyrics
            want = len(split_plain_lyrics(req.content))
            phrases = await detect_vocal_phrases(vocals, duration, target_count=want)
            if vocals != result.get("vocals_path"):
                result["vocals_path"] = vocals      # 补回去，下次直接复用

    try:
        # 🔴 把 ASR 那份（不全但时间准的）识别结果一并传进去当锚点，防止顺序对齐整体漂移
        asr_ref = result.get("lyrics") if result.get("lyrics_source") != "text" else None
        n_anchors = 0
        if req.format == "text" and asr_ref:
            from app.modules._lyrics_align import find_anchors, parse_structured_lyrics
            n_anchors = len(find_anchors(parse_structured_lyrics(req.content), asr_ref))
        lines, sections = import_lyrics(req.content, req.format, duration, phrases,
                                        asr_lines=asr_ref)
    except ValueError as e:
        raise HTTPException(400, str(e))

    result["lyrics"] = lines
    result["lyrics_edited"] = True
    result["lyrics_source"] = req.format
    # 🔴 歌词稿里的 [Verse]/[Chorus] 标记就是现成的曲式结构，比让 LLM 从时间戳猜准得多。
    #    直接覆盖 song.sections —— 下游卡点分镜（副歌切得密、主歌用长镜）全靠它。
    if sections:
        song = dict(result.get("song") or {})
        song["sections"] = sections
        song["sections_source"] = "lyrics_markers"
        result["song"] = song

    workflow_state.update_node(project_id, node["id"], {"result": result})
    logger.info("[mv] 项目 %s 导入歌词：%s 格式，%d 句（乐句 %d 个，段落 %d 段）",
                project_id, req.format, len(lines), len(phrases), len(sections))
    return {
        "ok": True,
        "count": len(lines),
        "lines": lines,
        "sections": sections,
        "phrases_detected": len(phrases),
        "anchors_used": n_anchors,
        # 三种定位来源，任一成立就说明时间不是瞎均分的
        "aligned_by_audio": bool(phrases) or n_anchors > 0 or req.format == "lrc",
        "saved": True,          # 导入即落库，界面别再提示「记得保存」
    }


@router.post("/{project_id}/preview-shots")
async def preview_shots(project_id: str, req: PreviewShotsRequest):
    """按当前歌词轴试算镜头切分（本地纯算法，不调模型、不花钱）。"""
    lines = [{"start": l.start, "end": l.end, "text": l.text} for l in req.lines]
    windows = plan_shot_windows(lines, req.sections, float(req.duration or 0))
    return {
        "shots": [{k: v for k, v in w.items() if k != "lyrics"} |
                  {"lyrics": [l["text"] for l in (w.get("lyrics") or [])]}
                  for w in windows],
        "total": len(windows),
        # 契约体检结果一并回给前端：万一算法出问题，界面上能直接看见，
        # 而不是等成片出来才发现画面和歌错位。
        "problems": verify_windows(windows, float(req.duration or 0)),
    }
