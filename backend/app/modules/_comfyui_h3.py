"""MiniMax H3 本地视频生成（ComfyUI，1.1.0）

蚂蚁的渠道商通道停了（旧接口码被撤权、新接口码未加白，见记忆 octopus_073_antchain_seedance），
这里接本地 H3 顶上。跑在用户 WSL 里的 ComfyUI，4090 24GB。

三种模式对应我们已有的两个入口：
  · 有首帧（关键帧 / 上一镜尾帧）      → MiniMaxH3ImageToVideo(first_frame)
  · 有首帧 + 尾帧                     → 同一个节点再给 last_frame
  · 无首帧但有角色参考图              → MiniMaxH3ReferenceToVideo(ref_images)
  · 什么都没有                        → i2v 节点不传帧 = 纯文生视频

🔴 为什么 i2v 是主路径、ref2v 只做补充：
   我们的分镜每镜都带关键帧（风格与角色已经锁在图里），而且**连续镜头靠
   「上一镜尾帧 → 下一镜首帧」串联** —— ref2v 没有首帧概念，走它这条链就断了。
   ref2v 的价值是替代已失效的蚂蚁资产锁脸：没关键帧但有角色图时用它。

🔴 三个实测约束（2026-09-08，社区参考工作流 + 节点 tooltip）：
   ① length 必须满足 5 + 17k（节点 step=17）。按秒数换算后要向上取到合法值，
      所以真实时长会略长于请求时长——返回值里给的是**真实**时长，合成那步按它裁。
   ② SLA 注意力节点（H3SLAAttention）是 ComfyUI 原本没有的推理路径，
      turbo LoRA 只是让模型"容忍"稀疏注意力，不装节点跑不出加速。
      节点除 3 个必填外还有 12 个可选入参（engine / min_seq_len / protect_audio …）。
      🔴 它默认 `engine="comfy_kitchen"`，需要 **comfy-kitchen ≥ 0.2.32**；
         版本低了会报 `module 'comfy_kitchen' has no attribute 'sol_attn'`，
         然后**静默回退稠密注意力**——不中断、照样出片，只是加速全没了，极难发现。
         另有 `engine="triton"`（节点包自带核，不依赖 comfy-kitchen）可选。
   ③ 步数影响语音质量（节点作者原话：杂音不是稀疏度造成的、是步数）。
      MV 模式音轨会被原曲替换，4 步够用；要保留 H3 自带音频就调到 6。
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── 默认配置（都可被 user_settings 的 comfyui 分类覆盖）───
_DEF = {
    "h3_clip":        "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "h3_video_vae":   "minimax_h3_video_vae_int8_convrot.safetensors",
    "h3_audio_vae":   "minimax_h3_audio_vae_fp32.safetensors",
    # 🔴 下面三个的取值来自 2026-09-08 的 A/B 实测（同首帧同 seed，4090）：
    #      A  4步 / 0.7MP / block64 → 1120x608，0.8 分钟
    #      B  6步 / 1.0MP / block32 → 1280x704，1.0 分钟
    #    B 画质明显更好而**只慢 1.15 倍**（31 镜的片子 26 → 30 分钟），故默认取 B。
    #    原以为 1.0MP 会明显更慢，实际被 SLA 稀疏注意力吃掉了大部分开销。
    "h3_steps":       "6",       # 4 是参考工作流的值；6 是节点作者建议（语音杂音由步数决定）
    "h3_sparsity":    "0.9",     # 0.85 是 LoRA 蒸馏值，0.9 快约 15%；低于 0.6 反而比稠密慢
    "h3_block_size":  "32",      # 128 会让语音变机器人音；64 干净只慢 2%；32 质量再好一点
    "h3_shift_video": "12.0",
    "h3_shift_audio": "3.0",
    "h3_megapixels":  "1.0",     # 显存吃紧就调回 0.7（参考配置 1152x640）
    "h3_sampler":     "res_multistep",
    "h3_scheduler":   "simple",
    # 🔴 保护音频不被稀疏注意力啃掉。节点默认 False，参考工作流是 true。
    #    不开的话人物说的话会糊成听不清的音节（见 _base_graph 里的注释）。
    "h3_protect_audio": "true",
    "h3_min_seq_len":   "8192",   # 节点默认 12228，对短镜偏高；参考工作流用 8192
}

H3_FPS = 24                      # H3 固定 24fps
_LENGTH_BASE, _LENGTH_STEP = 5, 17   # 节点约束：length ∈ {5, 22, 39, ...}
_MAX_REF_IMAGES = 4              # ref2v 参考图上限，多了显存吃不消也容易互相干扰


def _get(key: str) -> str:
    from app.core.user_settings_service import user_settings
    return (user_settings.get("comfyui", key) or _DEF[key]).strip()


def _f(key: str) -> float:
    try:
        return float(_get(key))
    except (TypeError, ValueError):
        return float(_DEF[key])


def _i(key: str) -> int:
    try:
        return int(float(_get(key)))
    except (TypeError, ValueError):
        return int(float(_DEF[key]))


def is_h3_model(model: str) -> bool:
    """按模型文件名判断走不走 H3（与 _is_ltx / _is_14b 同样的分派方式）。"""
    m = (model or "").lower()
    return "minimax" in m and "h3" in m


# ─── 显存/内存余量守卫 ───
# 🔴 ComfyUI 的 "RAM pressure cache" 跑完**不还内存**（2026-09-08 实测）：
#    连跑两镜后进程 RSS 涨到 41GB、系统可用只剩 5.6GB、显存只剩 4.2GB，
#    第三镜开始在 24GB 磁盘 swap 上换页 —— 同规格镜头从 0.7 分钟变成 30 分钟。
#    最坏的是**它不报错、只是慢**，日志里一个字都没有，极难定位。
#    POST /free 能把 RSS 打回 4.2GB、可用内存回到 44.9GB，**耗时 3 毫秒**。
#    所以跑之前先看余量，不够就先卸载——有余量时零开销，不够时省下几十分钟。
_MIN_RAM_GB = 20.0     # H3 一套模型要 staged 约 37GB，低于这个数基本必然换页
_MIN_VRAM_GB = 8.0


async def ensure_headroom(base_url: str, *, progress_cb=None) -> bool:
    """生成前确保 ComfyUI 有足够内存余量。已卸载返回 True，无需卸载返回 False。

    任何一步失败都只记日志、不抛异常——这是优化不是必要条件，
    不该因为查不到内存就把出片流程弄挂。
    """
    import httpx as _hx
    base = (base_url or "").rstrip("/")
    try:
        async with _hx.AsyncClient(timeout=_hx.Timeout(15.0)) as c:
            r = await c.get(f"{base}/system_stats")
            r.raise_for_status()
            d = r.json() or {}
            ram_free = float((d.get("system") or {}).get("ram_free") or 0) / 1e9
            devs = d.get("devices") or []
            vram_free = float((devs[0].get("vram_free") if devs else 0) or 0) / 1e9

            if ram_free >= _MIN_RAM_GB and vram_free >= _MIN_VRAM_GB:
                return False

            logger.info("ComfyUI 内存吃紧（可用 %.1fGB / 显存 %.1fGB），先卸载模型", ram_free, vram_free)
            if progress_cb:
                try:
                    await progress_cb(f"🧹 显存吃紧（{vram_free:.1f}GB），正在卸载上一镜的模型…")
                except Exception:
                    pass
            await c.post(f"{base}/free", json={"unload_models": True, "free_memory": True})

            r2 = await c.get(f"{base}/system_stats")
            d2 = r2.json() or {}
            ram2 = float((d2.get("system") or {}).get("ram_free") or 0) / 1e9
            dv2 = d2.get("devices") or []
            vram2 = float((dv2[0].get("vram_free") if dv2 else 0) or 0) / 1e9
            logger.info("卸载完成：可用内存 %.1f→%.1fGB，显存 %.1f→%.1fGB",
                        ram_free, ram2, vram_free, vram2)
            return True
    except Exception as e:
        logger.warning("内存余量检查失败（不影响出片）: %s", e)
        return False


# ─── 尺寸与帧数 ───

def h3_length(duration_s: float) -> int:
    """秒数 → 合法 length（5 + 17k，向上取）。

    节点 step=17 是硬约束，取错会直接被校验拒掉。向上取意味着真实时长略长于请求，
    这是有意的——合成那步会把每段裁到时间窗，宁可多给也别短。
    """
    want = max(_LENGTH_BASE, int(round(max(0.1, duration_s) * H3_FPS)))
    k = max(0, -(-(want - _LENGTH_BASE) // _LENGTH_STEP))   # ceil 除法
    return _LENGTH_BASE + k * _LENGTH_STEP


def h3_size(width: int, height: int, megapixels: Optional[float] = None) -> tuple:
    """把请求分辨率压到像素预算内，并对齐到 32 的倍数。

    🔴 为什么要压：H3 的显存开销随像素数暴涨，而 VAE 解码是最容易爆的一步
       （社区实测 1344x768 每个时间块要串行 28 次 tile）。参考配置用 0.7MP，
       我们照抄；上层传 1280x720（0.92MP）会被自动降到安全档。
    """
    mp = megapixels if megapixels is not None else _f("h3_megapixels")
    w, h = max(32, int(width or 0)), max(32, int(height or 0))
    budget = max(0.05, mp) * 1_000_000
    cur = w * h
    if cur > budget:
        scale = (budget / cur) ** 0.5
        w, h = int(w * scale), int(h * scale)
    snap = lambda v: max(32, int(round(v / 32)) * 32)   # noqa: E731
    return snap(w), snap(h)


# ─── 提示词契约 ───

_CONTRACT_MARKERS = ("overall_soundscape", "detailed_description",
                     "integrated_multimodal_description", "subject_definitions")


def _looks_structured(prompt: str) -> bool:
    return any(m in (prompt or "") for m in _CONTRACT_MARKERS)


# 🔴 H3 是音画联合生成：提示词里凡是**祈使句/元指令**（"全程保持…""不要出现…"）
#    都可能被它当台词念出来，人物嘴型跟着那段话动，画面上像在说胡话。
#    实测 2026-09-09：锁脸前缀被原样念出，转写回来是
#    「全程同一早宴无换脸无串脸无人脸漂移」。
#    这些约束是给纯文本条件模型（Seedance）准备的，对 H3 既无用（它靠参考图锁脸）又有害。
#    上游已按模型跳过，这里再兜一道——提示词还可能从别处流进来。
_META_SENTENCE_MARKERS = (
    "五官骨相", "无换脸", "无串脸", "人脸漂移", "全程严格参照", "保持完全一致",
    "不出现任何文字", "无 BGM", "无BGM", "字幕", "水印", "永久一致", "固定不变",
    # 「发型、妆容、年龄感固定」这句不含上面任何词，得单列。「年龄感」是纯元词汇，
    # 不会出现在正常的画面描述里，拿它当锚点很安全。
    "年龄感",
    # 0.6.2 给 Seedance 拼的三条音频提示（"角色对白：…（请生成对应口型与说话动作）"）
    # 对 H3 是有害的：它会把这段**指令本身**译过去当台词念。台词另走 `<d>` 通道
    # （见 build_dialogue_line），所以这里直接剥掉，不是丢内容。
    "角色对白", "画外旁白", "环境音效", "请生成对应口型",
)

# 「不要有人」这类否定句要剥掉 —— 但**别指望剥掉就不长人了**。
#
# 🔴 2026-09-10 SHOT09（果篮空镜）实测，8 次全部长出人，一次都没躲过：
#       否定式措辞    × 3 个 seed  → 3/3 有人
#       正向落地措辞  × 2 个 seed  → 2/2 有人（「竹篮稳稳搁在草地上，静物特写，镜头锁定」）
#       正向 + 首尾帧锁同一张关键帧 × 3 → 首尾两端干净，**中段照样有人**
#
#    真正的原因是**关键帧构图**：那张图是胸高、正对镜头、背景虚化的果篮，
#    这就是"有人把篮子端给你看"的典型画面，模型每次都在同一个位置把那个人补出来。
#    措辞层面怎么改都盖不过构图先验。空镜要治本只有两条：换一张没有人立足空间的关键帧
#    （低机位落地 / 微距怼满不留背景），或者干脆不生成视频、用静帧缓推（见 SHOT09 的处理）。
#
# 那为什么还要剥？因为这段文字会被翻译后进入 H3 的提示词契约，**是会被念出来的**
# （H3 音画联合生成，描述里的文字会变成台词）。剥掉是为了音轨干净，不是为了画面。
_NO_PEOPLE_MARKERS = (
    "没有任何人物", "没有人物", "无人物", "不出现人物", "无人出现", "没有人出现",
    "人影", "身体部位", "no people", "no person", "no human", "no hands",
    "without people", "no body parts",
)


def scrub_meta_sentences(prompt: str) -> str:
    """剥掉提示词里的祈使/元指令句与「不要有人」否定句，只留下对画面的描述。

    按中文句读切分，逐句判断——整段丢掉会连有用的画面描述一起丢。
    """
    text = (prompt or "").strip()
    if not text:
        return ""
    kept, dropped, negated = [], 0, 0
    for raw in re.split(r"(?<=[。；;\n])", text):
        seg = raw.strip()
        if not seg:
            continue
        if any(m in seg for m in _META_SENTENCE_MARKERS):
            dropped += 1
            continue
        low = seg.lower()
        if any(m in low for m in _NO_PEOPLE_MARKERS):
            negated += 1
            continue
        kept.append(seg)
    if dropped:
        logger.info("[h3] 从提示词里剥掉 %d 句元指令（防止被当台词念出来）", dropped)
    if negated:
        logger.warning(
            "[h3] 剥掉 %d 句「不要有人」否定（会被当台词念出来）。⚠️ 剥掉≠画面就不长人了——"
            "空镜长人是关键帧构图决定的，改措辞没用，得换关键帧或改用静帧缓推", negated)
    return "".join(kept).strip() or text


# ─── 中文 → 英文（🔴 这是"人物说胡话"的根治手段）───
#
# 2026-09-09 三变体实测（同首帧同 seed，只换描述语言）：
#     中文描述          音轨 mean -16.2 dB，ASR 转出「…脸上笑容灿烂」——它在念我们的描述
#     英文描述          音轨 mean -31.4 dB，ASR 报 no audio segment found —— 不念了
#     英文 + 嘴部状态    音轨 mean -42.0 dB，同样无人声，且更安静
#
# H3 是音画联合生成，中文自由文本会被它当成"要念出来的内容"，于是人物张嘴说胡话、
# 嘴型跟歌词完全对不上。换成英文就不会。
#
# 🔴 注意这与出图相反：Seedream / 智谱出图**必须中文**（国产模型对中文理解更准，
#    见记忆 octopus_image_gen_and_ops）。同一份分镜描述，两个下游要不同语言，
#    所以翻译放在这里做、只影响 H3 这一条链路。
_CJK_RE = re.compile(r"[一-鿿]")
# 明确交代嘴在干什么（正向描述）。否定式「不说话」实测无效，
# 但给一个具体可执行的面部状态能把残余人声再压一档（-31 → -42 dB）。
_QUIET_MOUTH = ("The subject keeps their lips softly closed in a gentle, closed-mouth "
                "expression throughout, breathing calmly. No one in the scene speaks.")
_TRANSLATE_CACHE: dict = {}

# 台词两侧常见的引号/书名号，喂进 <d> 之前要剥掉——标签里多一个字符它就多念一个音。
_QUOTE_CHARS = '"\'“”‘’「」『』＂'


def _lang_tag(text: str) -> str:
    """`<d>` 里的语言标记。混排时以是否含中文为准（中文占主导就按中文发音）。"""
    return "Chinese" if _CJK_RE.search(text or "") else "English"


def build_dialogue_line(dialogue: str, *, speaker: str = "", singing: bool = False) -> str:
    """按官方对白契约拼一行台词：`Speaker (S1) says: <d>[Language] 逐字台词</d>`

    🔴 三条铁律（实测踩出来的）：
       1. **说话人 ID `(S1)` 写在 `<d>` 外面**，标签内只有 `[语言]` + 一字不差的台词。
          往标签里塞任何解释、引号、括号，它都会照着念出来。
       2. **台词不翻译**。描述必须是英文（中文描述会被整段当台词念，实测 -16dB
          响亮人声），但 `<d>` 里的内容要保持原文——中文歌词就得是中文。
       3. 有台词时**不要再加 `_QUIET_MOUTH`**，那句话是专门用来闭嘴的，
          和让它开口说话直接冲突。
    """
    d = " ".join((dialogue or "").split()).strip(_QUOTE_CHARS).strip()
    if not d:
        return ""
    # 「金果：你怎么才来呀」——名字必须剥掉，否则连名带姓一起唱出来。
    # 放在这里而不是调用方：这是 `<d>` 契约自身的规则，谁调都得守。
    for sep in ("：", ":"):
        head, found, rest = d.partition(sep)
        if found and rest.strip() and len(head) <= 12 and not any(
                ch in head for ch in "，。,.！？ "):
            d = rest.strip().strip(_QUOTE_CHARS).strip()
            break
    if not d:
        return ""
    who = (speaker or "").strip() or "The person in frame"
    verb = "sings" if singing else "says"
    return f"{who} (S1) {verb}: <d>[{_lang_tag(d)}] {d}</d>"


def _needs_english(text: str) -> bool:
    """含中文就要翻。阈值放宽到 1 个字——夹带几个中文词照样会被念出来。"""
    return bool(_CJK_RE.search(text or ""))


async def to_english(text: str) -> str:
    """把镜头描述翻成英文（只为 H3 用）。失败就原样返回，不中断出片。"""
    src = (text or "").strip()
    if not src or not _needs_english(src):
        return src
    if src in _TRANSLATE_CACHE:
        return _TRANSLATE_CACHE[src]

    ask = ("Translate this shot description into natural English for a video "
           "generation model. Keep every visual detail: subject, action, camera "
           "movement, lighting, setting.\n"
           "Rules: output ONLY the translated description, one paragraph, no quotes, "
           "no preamble. Do NOT invent dialogue or any spoken lines.\n\n" + src)
    try:
        from app.core.runtime_config import active_llm
        from openai import AsyncOpenAI
        cfg = active_llm()
        client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
        # 🔴 max_tokens 必须给足。原来是 400，在**推理模型**（deepseek-v4-pro 等）上
        #    会被思考过程吃光，content 回空串、finish_reason=length。
        #    2026-09-10 实测：同一句话四次里有一次空手而归，而空返回**不走 except**，
        #    于是一声不吭退回中文 —— H3 收到中文就把整段描述念出来。最难查的那种 bug。
        for attempt in (1, 2):
            resp = await client.chat.completions.create(
                model=cfg.model, temperature=0.2, max_tokens=2000,
                messages=[{"role": "user", "content": ask}],
            )
            out = (resp.choices[0].message.content or "").strip()
            if not out:
                logger.warning("[h3] 翻译返回空（第 %d 次，finish=%s）",
                               attempt, resp.choices[0].finish_reason)
                continue
            if _CJK_RE.search(out) and attempt == 1:
                logger.warning("[h3] 译文里仍有中文，重试一次: %s", out[:60])
                continue
            _TRANSLATE_CACHE[src] = out
            logger.info("[h3] 描述已译为英文（防止被念出来）: %s", out[:70])
            return out
    except Exception as e:
        logger.warning("[h3] 描述翻译失败: %s: %s", type(e).__name__, e)
    # 走到这儿一定是出问题了，必须留痕 —— 别再静默退回。
    logger.warning("[h3] 🔴 退回中文原文，H3 很可能把这段描述念出来: %s", src[:60])
    return src


# 环境音：给它一个具体的东西去生成，比让它"别出声"靠谱。
# MV 的成片音轨会被原曲整轨替换，这里生成什么其实不进片子——
# 真正要的是**别让它说话**，因为一开口嘴型就跟歌词对不上，画面像在说胡话。
_DEFAULT_SOUNDSCAPE = ("Soft natural ambience: light wind through leaves, "
                       "distant birdsong, faint rustling of fabric.")


def format_prompt(prompt: str, *, ref_count: int = 0,
                  dialogue: str = "", speaker: str = "", singing: bool = False) -> str:
    """按 **MiniMax H3 官方提示词契约** 组织提示词。

    🔴 两套字段集，官方要求「字段名、顺序、标签完全一致」，不能混用：
       base（T2VA / I2VA / FL2VA）：
           integrated_multimodal_description / overall_soundscape / non_diegetic_music
       ref2va（多参考图）：
           subject_definitions / summary / retention_analysis /
           detailed_description / overall_soundscape / non_diegetic_music

    🔴 怎么让人物**不说话**（2026-09-09 实测踩了两轮才搞清）：
       官方的做法是——**不写说话人 ID（S1）、不写 `<d>` 对白标签**，就这样。
       官方 Case 3（骑车人撑伞）整段无对白，靠的就是"只描述动作与运镜"。
       ❌ 别写 "no speech / not speaking"：否定式实测完全无效，
          而且 "speech" 本身就是个强提示词，等于在提醒它说话。
       ❌ 字段名写错（我一开始给 i2v 用了 ref2v 的 detailed_description）会让模型
          压根不把它当契约解析，转而自由发挥成台词——实测它把画面描述原句念了出来
          （「脸上笑容灿烂」被一字不差地说出口）。

    已经是契约格式的原样放行（用户自己写了就别动他的）。
    """
    p = scrub_meta_sentences(prompt) or "a continuous cinematic shot"
    if _looks_structured(p):
        return p

    # 有台词就让它开口念，没台词才闭嘴。两者互斥——同时给会自相矛盾。
    line = build_dialogue_line(dialogue, speaker=speaker, singing=singing)
    mouth = line if line else _QUIET_MOUTH

    # 官方无配乐一律写 N/A（不是 none / 空）
    tail = [f"overall_soundscape: {_DEFAULT_SOUNDSCAPE}", "", "non_diegetic_music: N/A"]

    if ref_count > 0:
        subs = " ".join(
            f"<Subject {i + 1}> is the subject in <Picture {i + 1}>; "
            f"their face, hair and clothing follow <Picture {i + 1}>."
            for i in range(ref_count)
        )
        who = " and ".join(f"<Subject {i + 1}>" for i in range(ref_count))
        return "\n".join([
            f"subject_definitions: {subs}", "",
            f"summary: [reference generation] one continuous shot; "
            f"appearance taken from the reference picture(s).", "",
            f"retention_analysis: {who} fully preserved from the reference picture(s).", "",
            f"detailed_description: [Shot 1] {p} {mouth}", "",
            *tail,
        ])

    return "\n".join([f"integrated_multimodal_description: [Shot 1] {p} {mouth}", "", *tail])


# ─── 工作流构造 ───

def _base_graph(*, unet: str, prompt_node_id: str, seed: int, filename_prefix: str) -> Dict[str, Any]:
    """i2v / ref2v 共用的骨架：加载 → SLA → SigmaShift → 采样 → 解码 → 存片。

    分辨率、帧数全部在 Python 里算好后以字面量传入，**不依赖 ResolutionSelector /
    ComfyMathExpression 这两个自定义节点** —— 少两个外部依赖，装机环境差异也少一处。
    """
    return {
        "127": {"class_type": "UNETLoader",
                "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        # 🔴 protect_audio 必须开（默认是 False，参考工作流里是 true，我一开始漏了）。
        #    节点 tooltip 原话：稀疏注意力**对音频的影响远大于视频**——H3 的音频是每秒 80 行，
        #    一个块就能压掉一大段语音，而同样行数只占视频一帧的 3%。
        #    不开这个开关，人物说的话会糊成听不清的音节：实测让她唱「一颗两颗，三颗四颗」，
        #    出来是「一车辆。喝啊请颗四颗」——轮廓在、字全糊。
        # min_seq_len 跟参考工作流对齐（默认 12228 对我们这种短镜偏高，SLA 可能整段不生效）。
        "900": {"class_type": "H3SLAAttention",
                "inputs": {"model": ["127", 0],
                           "sparsity_ratio": _f("h3_sparsity"),
                           "block_size": _get("h3_block_size"),
                           "protect_audio": _get("h3_protect_audio").lower() == "true",
                           "min_seq_len": _i("h3_min_seq_len")}},
        "142": {"class_type": "MiniMaxH3SigmaShift",
                "inputs": {"model": ["900", 0],
                           "shift_video": _f("h3_shift_video"),
                           "shift_audio": _f("h3_shift_audio")}},
        "128": {"class_type": "CLIPLoader",
                "inputs": {"clip_name": _get("h3_clip"), "type": "minimax", "device": "default"}},
        "119": {"class_type": "VAELoader", "inputs": {"vae_name": _get("h3_video_vae")}},
        "120": {"class_type": "VAELoader", "inputs": {"vae_name": _get("h3_audio_vae")}},
        "129": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "135": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": _get("h3_sampler")}},
        "124": {"class_type": "BasicScheduler",
                "inputs": {"model": ["142", 0], "scheduler": _get("h3_scheduler"),
                           "steps": _i("h3_steps"), "denoise": 1.0}},
        "126": {"class_type": "BasicGuider",
                "inputs": {"model": ["142", 0], "conditioning": [prompt_node_id, 0]}},
        "125": {"class_type": "SamplerCustomAdvanced",
                "inputs": {"noise": ["129", 0], "guider": ["126", 0], "sampler": ["135", 0],
                           "sigmas": ["124", 0], "latent_image": [prompt_node_id, 1]}},
        "122": {"class_type": "VAEDecode", "inputs": {"samples": ["125", 0], "vae": ["119", 0]}},
        "121": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["125", 0], "vae": ["120", 0]}},
        "130": {"class_type": "CreateVideo",
                "inputs": {"images": ["122", 0], "audio": ["121", 0],
                           "fps": H3_FPS, "bit_depth": 8}},
        "92": {"class_type": "SaveVideo",
               "inputs": {"video": ["130", 0], "format": "auto", "codec": "auto",
                          "filename_prefix": filename_prefix}},
    }


def build_i2v(*, unet: str, prompt: str, width: int, height: int, length: int, seed: int,
              first_frame: Optional[str] = None, last_frame: Optional[str] = None,
              dialogue: str = "", speaker: str = "",
              singing: bool = False) -> Dict[str, Any]:
    """图生视频 / 首尾帧生视频 / 纯文生视频（都不给帧就是 t2v）。

    first_frame / last_frame 是**已上传到 ComfyUI input 的文件名**，不是本地路径。
    """
    wf = _base_graph(unet=unet, prompt_node_id="131", seed=seed,
                     filename_prefix="video/octopus_h3_i2v")
    node: Dict[str, Any] = {
        "clip": ["128", 0], "vae": ["119", 0],
        "prompt": format_prompt(prompt, dialogue=dialogue, speaker=speaker, singing=singing),
        "width": width, "height": height, "length": length,
    }
    if first_frame:
        wf["901"] = {"class_type": "LoadImage", "inputs": {"image": first_frame}}
        node["first_frame"] = ["901", 0]
    if last_frame:
        wf["902"] = {"class_type": "LoadImage", "inputs": {"image": last_frame}}
        node["last_frame"] = ["902", 0]
    wf["131"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": node}
    return wf


def build_ref2v(*, unet: str, prompt: str, width: int, height: int, length: int, seed: int,
                ref_images: List[str], dialogue: str = "", speaker: str = "",
                singing: bool = False) -> Dict[str, Any]:
    """多参考图生视频。ref_images 是已上传的文件名列表。

    ref_images 在节点里是 COMFY_AUTOGROW_V3，API 格式按 `ref_images.ref_image_N` 逐个给。
    """
    refs = [r for r in (ref_images or []) if r][:_MAX_REF_IMAGES]
    if not refs:
        raise ValueError("ref2v 至少需要一张参考图")

    wf = _base_graph(unet=unet, prompt_node_id="131", seed=seed,
                     filename_prefix="video/octopus_h3_ref2v")
    node: Dict[str, Any] = {
        "clip": ["128", 0], "vae": ["119", 0], "audio_vae": ["120", 0],
        "prompt": format_prompt(prompt, ref_count=len(refs), dialogue=dialogue,
                                speaker=speaker, singing=singing),
        "width": width, "height": height, "length": length,
        "ref_image_size": "match",
    }
    for i, name in enumerate(refs):
        nid = str(910 + i)
        wf[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        node[f"ref_images.ref_image_{i}"] = [nid, 0]
    wf["131"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": node}
    return wf


def describe(width: int, height: int, length: int) -> str:
    """给日志/进度用的一句话摘要。"""
    return (f"{width}x{height} · {length}帧 ≈ {length / H3_FPS:.1f}s @ {H3_FPS}fps · "
            f"{_i('h3_steps')}步 · SLA {_f('h3_sparsity')}/{_get('h3_block_size')}")
