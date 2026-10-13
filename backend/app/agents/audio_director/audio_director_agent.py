"""
音频导演 Agent (0.6.1) — 基于已确认的分镜规划整片音频叙事

职责：
1. 拿到分镜 (storyboard) 和总时长 → 规划"时间窗 × 情绪/强度"的音频叙事
2. 严格按 storyboard 时长（segment 时间窗与 shot 时间窗对齐）
3. 风格统一（避免分镜之间割裂感）
4. 输出可被 Mubert 模块消费的 playlist_index_hint + per-segment mood

注意：本 agent 只规划，不直接调 Mubert（那是 _run_bgm_fetch 的事）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.agents.base.base_agent import BaseAgent
from app.core.agent_runtime import cloud_method  # 护城河：cloud_client 下整体路由云端

logger = logging.getLogger(__name__)


AUDIO_DIRECTOR_SYSTEM_PROMPT = """你是 Octopus Video OS 的**电影级音频导演 Agent** — 角色定位是 Hans Zimmer / Ludwig Göransson / Joe Hisaishi 级别的配乐家，
不是配乐机器。你的输出会直接驱动 Mubert text-to-music 生成最终 BGM。

## 🎵 当前 prompt 同时供 ElevenLabs Music（主推，deep model）和 Mubert（tag 池匹配）两个 provider 使用

无论用哪个 provider，**统一按下述"短句 + 通用词 + BPM + 用途"格式**即可 —— 这种格式对 Mubert 是必要约束，对 ElevenLabs 也工作良好。不要为 ElevenLabs 写很长的句子，简洁清晰永远更稳。

## ⚠ Mubert prompt 规则（基于 Mubert 官方文档与 GitHub README）

Mubert 的工作原理：你写的 prompt 被**编码成向量**，然后**匹配到 Mubert 内部预定义的 tag 池**（一组有限的 genre / mood / 乐器标签），用匹配到的 tag 去生成音乐。**音乐质量取决于你的 prompt 是否能命中正确的 tag**。

**所以 prompt 必须**：
- **写成 1 句短自然语句（10-20 词）** —— 不是关键词堆，也不是长段描述
- 用 **Mubert 认识的通用词汇**：
  - **Genre**：cinematic / ambient / classical / folk / acoustic / lo-fi / chillhop / orchestral / pop / electronic / hip hop / corporate / soundtrack
  - **Mood**：warm / calm / gentle / uplifting / dreamy / cheerful / melancholic / dark / tense / energetic / playful / peaceful
  - **通用乐器**：piano / acoustic guitar / strings / cello / violin / flute / drums / synth / pad / bass / brass
- **必须含 BPM**（slow 60 / medium 80-100 / fast 120-140）
- **结尾加用途**（"for family video" / "for vlog" / "for corporate intro"）—— Mubert 官方推荐
- ❌ **不要写**作曲家名（Hisaishi / Zimmer 都不在 tag 池）
- ❌ **不要写**冷门乐器（felt piano / pizzicato / glockenspiel / ukulele / taiko / 古筝）
- ❌ **不要写**关键词堆（"warm, pastoral, acoustic, ..."）—— 这是上一版做法，效果差

## 官方推荐模板

`"[mood] [genre] with [1-2 common instruments], [emotion words], [BPM] BPM, for [use case]"`

## 官方风格示例（必须严格模仿这种"短句 + 通用词 + BPM + 用途"）

| 剧情 | prompt |
|---|---|
| **两岁小孩蹦跳着摘果（温馨+欢快）** | `Cheerful warm acoustic folk with bouncy guitar and bright piano, playful and uplifting, 100 BPM, for family video` |
| 妈妈搂着熟睡孩子（温馨+静） | `Gentle warm acoustic with soft piano and strings, calm and tender, 70 BPM, for intimate family moment` |
| 治愈日常 vlog | `Calm lo-fi beats with mellow keys and soft pads, relaxing, 80 BPM, for vlog` |
| 悬疑深夜 | `Dark cinematic ambient with sparse piano and bass, tense and slow, 60 BPM, for suspense scene` |
| 燃系运动 | `Energetic orchestral with driving drums and brass, powerful and uplifting, 140 BPM, for sports highlight` |
| 古风山水（静） | `Peaceful classical with flute and strings, dreamy and meditative, 65 BPM, for nature documentary` |
| 商业高级 | `Minimal corporate ambient with synth pads and soft bass, sleek and modern, 110 BPM, for product intro` |
| 喜剧沙雕 | `Playful pop with bouncy bass and cheerful piano, light and comic, 115 BPM, for cartoon scene` |
| 自然纪录 | `Atmospheric cinematic with cello and soft pads, contemplative and slow, 55 BPM, for documentary` |
| 浪漫唯美 | `Warm cinematic strings with soft piano, romantic and dreamy, 75 BPM, for love scene` |
| 萌宠嬉闹 | `Lively pop with bouncy ukulele-like guitar and claps, cheerful and playful, 110 BPM, for pet video` |
| 旅行 vlog（推进） | `Uplifting indie folk with acoustic guitar and light drums, bright and energetic, 105 BPM, for travel video` |

## 🎚 BPM 档位（必读 — 决定 BPM 的是「动作活跃度」，不是「mood 是否温馨」）

| BPM | 适用场景 | 描述词 |
|---|---|---|
| 55-70 | 静态、深情、睡眠、悲伤、冥想 | slow, contemplative, intimate, sad |
| 70-90 | 温柔、日常对话、慢生活 vlog | gentle, calm, mellow, relaxing |
| **90-115** | **欢快、玩耍、跳跃、活泼日常**（亲子玩耍、宠物嬉闹、孩子摘果） | **cheerful, playful, lively, bouncy, uplifting** |
| 115-130 | 推进、励志、节奏感强 | driving, energetic, motivational |
| 130+ | 燃、紧张、动作、运动 | epic, intense, powerful |

**核心原则**：同一个 mood 下，**动作活跃度决定 BPM**。
- 妈妈搂着熟睡的孩子（温馨+静）→ 70 BPM, gentle calm
- **孩子蹦跳着摘果**（温馨+欢）→ **100 BPM, cheerful playful** ← 这是关键差别
- 老两口饭后散步（温馨+从容）→ 80 BPM, warm relaxing

读 storyboard 时**重点看 action 字段的动词**：
- 静态动词（坐、躺、抱、看）→ 慢 BPM
- 动态动词（跑、跳、踮脚、伸手、笑、摘、追）→ 中高 BPM
- 极速动词（冲、爬、攀、飞）→ 高 BPM

## 「分析→产出」流程

第 1 步：**读剧本**（title / logline / scenes 的动作和情绪）
第 2 步：**1 句话判定剧情类型 + 核心情感 + 动作活跃度（静/慢/欢快/快/极速）**
第 3 步：**按 BPM 档位表选 BPM**（不要被 mood 带偏；温馨/治愈 ≠ 慢）
第 4 步：**从 Mubert 通用词汇里挑 4-5 个 tag** 覆盖 mood + genre + 乐器
第 5 步：**按模板组装 prompt**
第 6 步：**写 reasoning** 解释"为什么这套 mood + BPM 同时贴合本片"

## 其他约束
- **segments 严格对齐时长**：t0/t1 落在 [0, total_duration]，最后一个 t1 == total_duration
- **风格统一**：overall_mood 与 style_tags 整片一致
- **段数**：≤15s 用 2-3 段；30s 用 3-5 段；60s+ 4-6 段

## 输入数据
- script: {title, logline, style, duration, structure, scenes}  ← **重点先读**
- storyboard: [{shot_id, duration_seconds, pacing_role, emotion, action}]
- total_duration: 整片秒数
- mood / style: 项目级偏好
- shot_plan_summary: 分镜整体节奏

## 输出（严格 JSON，不要 markdown 包裹）

{
  "overall_mood": "中文一两词整片基调（例：温暖治愈 / 悬疑紧张 / 燃情史诗）",
  "style_tags": ["warm", "acoustic", "folk"],
  "segments": [
    {"t0": 0,  "t1": 5,  "mood": "calm",     "intensity": "low",    "notes": "开场温馨氛围"},
    {"t0": 5,  "t1": 12, "mood": "playful",  "intensity": "medium", "notes": "推进 — 加入活泼元素"},
    {"t0": 12, "t1": 15, "mood": "warm",     "intensity": "medium", "notes": "收尾 — 暖色调收束"}
  ],
  "mubert_prompt": "Cheerful warm acoustic folk with bouncy guitar and bright piano, playful and uplifting, 100 BPM, for family video",
  "mubert_intensity": "medium",
  "reasoning": "两岁孩子蹦跳着摘果 = 温暖+欢快家庭场景（不是睡前的安静温馨）。Mubert 通用词里 cheerful + bouncy + playful + acoustic folk 最贴动作活跃度；100 BPM 匹配幼儿玩耍节奏（70 BPM 太慢像睡前）；标 'for family video' 给 Mubert 强引导。"
}

## intensity 词表
segments 用：low / medium / rising / high / falling
mubert_intensity 用（只能三选一）：low / medium / high
— 治愈/慢 = low；推进 = medium；燃/动作 = high

请直接输出 JSON。"""


class AudioDirectorAgent(BaseAgent):
    """音频导演 Agent"""

    name = "audio_director"
    description = "基于分镜规划整片音频叙事的导演 agent"
    system_prompt = AUDIO_DIRECTOR_SYSTEM_PROMPT

    @cloud_method
    async def execute(
        self, task: Dict[str, Any], context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        Args:
            task: {
                "storyboard": [shot dicts] 或 {"storyboard": [...]} 也兼容,
                "total_duration": int 秒,
                "mood": str (可选),
                "style": str (可选),
                "shot_plan_summary": str (可选，来自 storyboard 的整体节奏说明)
            }
        Returns:
            {
                "overall_mood": str,
                "style_tags": [str],
                "segments": [{t0, t1, mood, intensity, notes}],
                "mubert_playlist_index_hint": str,
                "reasoning": str,
                "total_duration": int,
            }
        """
        # 兼容 task.storyboard 是 list 或 dict 两种格式
        raw_sb = task.get("storyboard") or []
        if isinstance(raw_sb, dict):
            raw_sb = raw_sb.get("storyboard") or []
        shots: List[Dict[str, Any]] = [s for s in raw_sb if isinstance(s, dict)]

        total_duration = int(task.get("total_duration") or 0)
        if total_duration <= 0:
            total_duration = sum(int(s.get("duration_seconds") or 0) for s in shots)
        total_duration = max(5, total_duration)

        prompt = self._build_prompt(
            shots=shots,
            total_duration=total_duration,
            mood=task.get("mood", ""),
            style=task.get("style", ""),
            shot_plan_summary=task.get("shot_plan_summary", ""),
            script=task.get("script") or {},
        )
        full_prompt = f"{self.system_prompt}\n\n{prompt}"

        response = await self.llm_generate(
            prompt=full_prompt,
            system=None,
            json_mode=False,
            temperature=0.5,
        )

        plan = self._parse_json(response)
        if not plan or not isinstance(plan, dict):
            # LLM 返回不可解析 → 兜底一个默认 plan（不让 BGM 阶段直接 fail）
            logger.warning("AudioDirector LLM 输出无法解析为 JSON，使用兜底 plan。原始前 200 字: %s",
                           (response or "")[:200])
            plan = self._fallback_plan(total_duration, task.get("mood", ""))

        plan = self._normalize(plan, total_duration)
        plan["total_duration"] = total_duration
        return plan

    # ─── helpers ───

    # 0.6.1+ v3: 改用 Mubert 推荐的"短句 + 通用词 + BPM + 用途"格式
    # （之前的关键词列表和长描述风格都不行 — Mubert 内部是向量匹配 tag 池，越短越聚焦越准）
    _MOOD_TEMPLATES: List[tuple[tuple[str, ...], str]] = [
        (
            # 亲子/温馨场景大多伴随玩耍/嬉闹的活跃动作，default 给欢快版 100 BPM；
            # 真静态的（睡前/拥抱）让 LLM 自己根据 storyboard action 把 BPM 降到 70
            ("温馨", "温暖", "治愈", "亲子", "家庭", "童年", "童趣"),
            "Cheerful warm acoustic folk with bouncy guitar and bright piano, playful and uplifting, 100 BPM, for family video",
        ),
        (
            ("悬疑", "紧张", "惊悚", "压抑", "诡异"),
            "Dark cinematic ambient with sparse piano and bass, tense and slow, 60 BPM, for suspense scene",
        ),
        (
            ("燃", "史诗", "热血", "震撼", "壮阔", "激励"),
            "Energetic orchestral with driving drums and brass, powerful and uplifting, 140 BPM, for sports highlight",
        ),
        (
            ("搞笑", "喜剧", "沙雕", "幽默", "滑稽"),
            "Playful pop with bouncy bass and cheerful piano, light and comic, 110 BPM, for cartoon scene",
        ),
        (
            ("古风", "武侠", "国风", "中国风", "禅意", "山水"),
            "Peaceful classical with flute and strings, dreamy and meditative, 65 BPM, for nature documentary",
        ),
        (
            ("清新", "日常", "慢生活", "vlog", "lofi"),
            "Calm lo-fi beats with mellow keys and soft pads, relaxing, 80 BPM, for vlog",
        ),
        (
            ("浪漫", "唯美", "甜蜜", "爱情"),
            "Warm cinematic strings with soft piano, romantic and dreamy, 75 BPM, for love scene",
        ),
        (
            ("商业", "高级感", "时尚", "酷"),
            "Minimal corporate ambient with synth pads and soft bass, sleek and modern, 110 BPM, for product intro",
        ),
        (
            ("纪录片", "自然", "科普", "纪实"),
            "Atmospheric cinematic with cello and soft pads, contemplative and slow, 55 BPM, for documentary",
        ),
    ]

    def _match_mood_template(self, *texts: str) -> Optional[str]:
        """按 mood/style 关键词命中 _MOOD_TEMPLATES；命中第一个就返回 prompt 模板。"""
        combined = " ".join((t or "").lower() for t in texts)
        if not combined.strip():
            return None
        for keywords, tmpl in self._MOOD_TEMPLATES:
            if any(kw.lower() in combined for kw in keywords):
                return tmpl
        return None

    def _build_prompt(
        self,
        shots: List[Dict[str, Any]],
        total_duration: int,
        mood: str,
        style: str,
        shot_plan_summary: str,
        script: Optional[Dict[str, Any]] = None,
    ) -> str:
        # 把 shots 摘成简短的时间轴（避免把整个大 JSON 塞进 prompt）
        timeline_lines: List[str] = []
        t_cursor = 0
        for s in shots:
            dur = int(s.get("duration_seconds") or 0) or 5
            t_start = t_cursor
            t_end = t_cursor + dur
            timeline_lines.append(
                f"  [{t_start:>3}s → {t_end:>3}s] {s.get('shot_id') or '?'} · "
                f"pacing={s.get('pacing_role') or '?'} · "
                f"emotion={s.get('emotion') or '?'} · "
                f"action={(s.get('action') or '')[:40]}"
            )
            t_cursor = t_end
        timeline = "\n".join(timeline_lines) if timeline_lines else "  （无分镜数据）"

        meta_lines = []
        if mood:
            meta_lines.append(f"- 整体 mood 偏好: {mood}")
        if style:
            meta_lines.append(f"- 整体 style 偏好: {style}")
        if shot_plan_summary:
            meta_lines.append(f"- 分镜导演节奏说明: {shot_plan_summary}")
        meta = "\n".join(meta_lines) or "（无额外偏好）"

        # 剧本信息（最重要，先看 logline / structure 拿到剧情类型）
        script = script or {}
        script_block = "（无剧本上下文）"
        if script:
            sb_lines: List[str] = []
            if script.get("title"):
                sb_lines.append(f"- 标题：{script.get('title')}")
            if script.get("logline"):
                sb_lines.append(f"- Logline（一句话剧情）：{script.get('logline')}")
            if script.get("style"):
                sb_lines.append(f"- 剧本风格：{script.get('style')}")
            structure = script.get("structure") or {}
            if isinstance(structure, dict) and any(structure.values()):
                struct_parts = []
                for k in ("hook", "development", "turning_point", "ending"):
                    v = structure.get(k)
                    if v:
                        struct_parts.append(f"  · {k}: {str(v)[:120]}")
                if struct_parts:
                    sb_lines.append("- 故事结构:\n" + "\n".join(struct_parts))
            scenes = script.get("scenes") or []
            if isinstance(scenes, list) and scenes:
                sn_lines: List[str] = []
                for i, sc in enumerate(scenes[:8]):
                    if not isinstance(sc, dict):
                        continue
                    parts = []
                    if sc.get("scene_id"):
                        parts.append(str(sc.get("scene_id")))
                    if sc.get("location"):
                        parts.append(str(sc.get("location")))
                    if sc.get("beat") or sc.get("description"):
                        parts.append(str(sc.get("beat") or sc.get("description"))[:100])
                    if sc.get("mood") or sc.get("emotion"):
                        parts.append(f"mood={sc.get('mood') or sc.get('emotion')}")
                    sn_lines.append(f"  · {' | '.join(parts)}")
                if sn_lines:
                    sb_lines.append("- 场景:\n" + "\n".join(sn_lines))
            if sb_lines:
                script_block = "\n".join(sb_lines)

        # 0.6.1+: 按 mood / style / logline 命中关键词，给 LLM 一个"风格参考"（不强制）
        # LLM 应该基于剧本细节做独到判断，参考只在它跑偏到不相关 genre 时作为锚点
        starter_template = self._match_mood_template(
            mood, style,
            (script or {}).get("logline", ""),
            (script or {}).get("title", ""),
            shot_plan_summary,
        )
        starter_block = ""
        if starter_template:
            starter_block = (
                "\n### 📎 参考方向（仅供启发，不要照抄；最终 prompt 必须基于本片具体剧情独立产出）\n"
                f"对这类基调，常用的关键词组合大致是：`{starter_template}`\n"
                "你的任务是：**深读剧本后**，根据本片**独特的人物/动作/场景/情绪曲线**，给出更贴合的"
                "关键词列表（可换乐器、换 BPM、加专属 tag），不要被这个参考束缚。"
            )

        return f"""## 输入

总时长：**{total_duration} 秒**

### 剧本上下文（先深读这里再思考音乐风格）
{script_block}

### 项目偏好
{meta}
{starter_block}

### 分镜时间轴（按时长累加）
{timeline}

## 你的任务

**第 1 步**：默读剧本和分镜，**用一句话总结**这是什么类型的视频
**第 2 步**：**判定动作活跃度** — 看分镜 action 里都是什么动词（静态 sit/lie/hold vs 动态 jump/run/reach/play vs 极速 dash/chase）
**第 3 步**：**先选 BPM**（按 system prompt 里的 BPM 档位表，由活跃度决定，不要被 mood 带偏）
  - ⚠ 常见错误：看到"温馨/治愈"就选 60-70 BPM。其实"温馨+欢快"（孩子玩耍/宠物嬉闹）该用 95-110 BPM
**第 4 步**：从 Mubert 通用词汇里挑 4-5 个 tag（genre + mood + 通用乐器）
**第 5 步**：按模板 `[mood] [genre] with [instr1] and [instr2], [emotion words], [BPM] BPM, for [use case]` 组装 prompt
**第 6 步**：在 `reasoning` 里写明「为什么这套 mood + BPM 同时贴本片动作密度」

最后输出 segments：
- 时间窗覆盖 [0, {total_duration}]，最后一个 t1 == {total_duration}
- 段数 2-6 依总时长适配
- mood / intensity 与分镜 pacing / emotion 呼应；整体 style 一致

请直接输出 JSON。"""

    def _parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        import re
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                return None
        # 退一步：取第一个 { 到最后一个 } 之间
        try:
            start = text.index("{")
            end = text.rindex("}")
            return json.loads(text[start:end + 1])
        except Exception:
            return None

    def _normalize(self, plan: Dict[str, Any], total_duration: int) -> Dict[str, Any]:
        """clamp segments 到 [0, total_duration]，最后一段对齐到 total_duration。"""
        segments = plan.get("segments") or []
        out: List[Dict[str, Any]] = []
        prev_end = 0
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            try:
                t0 = max(0, min(total_duration, int(seg.get("t0", prev_end))))
                t1 = max(t0 + 1, min(total_duration, int(seg.get("t1", total_duration))))
            except (TypeError, ValueError):
                continue
            out.append({
                "t0": t0,
                "t1": t1,
                "mood": str(seg.get("mood") or "ambient").strip().lower(),
                "intensity": str(seg.get("intensity") or "medium").strip().lower(),
                "notes": str(seg.get("notes") or "").strip(),
            })
            prev_end = t1
        if not out:
            out = self._fallback_plan(total_duration, "")["segments"]
        else:
            # 强制最后一段对齐
            out[-1]["t1"] = total_duration
        plan["segments"] = out

        # 字段兜底
        if not plan.get("overall_mood"):
            plan["overall_mood"] = "cinematic ambient"
        tags = plan.get("style_tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        plan["style_tags"] = [str(t) for t in tags if t]

        # playlist_index 简单校验（可选回退）
        hint = str(plan.get("mubert_playlist_index_hint") or "").strip()
        if hint and not all(p.isdigit() for p in hint.split(".")):
            plan["mubert_playlist_index_hint"] = ""

        # 0.6.1+: Mubert text-to-music prompt（≤200 字符英文；推荐 ≤150 关键词列表风格）
        prompt = str(plan.get("mubert_prompt") or "").strip()
        if len(prompt) > 200:
            prompt = prompt[:200].rstrip().rstrip(",")
        plan["mubert_prompt"] = prompt

        # mubert_intensity（low/medium/high）
        intensity = str(plan.get("mubert_intensity") or "").strip().lower()
        if intensity not in {"low", "medium", "high"}:
            # 兜底：按 segments 多数派推断
            counts: Dict[str, int] = {"low": 0, "medium": 0, "high": 0}
            for seg in plan["segments"]:
                v = str(seg.get("intensity") or "medium").lower()
                if v in ("rising", "falling"):
                    v = "medium"
                if v in counts:
                    counts[v] += 1
            intensity = max(counts.items(), key=lambda x: x[1])[0] if any(counts.values()) else "medium"
        plan["mubert_intensity"] = intensity

        if "reasoning" not in plan:
            plan["reasoning"] = ""
        return plan

    # ═══════════════════════════════════════════════════════
    # 0.7.2 ②：专家分层 BGM —— series 定风格 → episode 按场景配（多镜共用、可复用）
    # ═══════════════════════════════════════════════════════

    @cloud_method
    async def plan_series_direction(
        self, script_summary: Dict[str, Any], script_excerpt: str = "",
    ) -> Dict[str, Any]:
        """Series 级：通读整剧定 BGM 整体风格基调（只定方向，不分段、不生成音乐）。
        输出落 series.script_summary["bgm_direction"]，约束后续每一集的场景配乐。"""
        ss = script_summary or {}
        scene_lines: List[str] = []
        for i, sc in enumerate((ss.get("scene_outline") or [])[:20]):
            if isinstance(sc, dict):
                scene_lines.append(f"  {i+1}. {(sc.get('chapter') or '')[:30]} | {(sc.get('summary') or '')[:80]}")
        excerpt_block = f"- 原文节选：{script_excerpt[:600]}" if script_excerpt else ""
        prompt = f"""你现在的任务是【全剧 BGM 风格方向】——通读整部剧的信息，为全剧定一份音乐基调"宪法"。
后续每一集会按场景在这份方向的约束下配乐，并尽量复用主题音乐保证全剧统一感。**只定方向，不分段、不写完整 prompt。**

## 剧集信息
- 剧名：{ss.get('title') or ''}
- Logline：{ss.get('logline') or ''}
- 类型/基调：{ss.get('genre') or ''} / {ss.get('tone') or ''}
- 预计总时长：{ss.get('est_total_min') or '?'} 分钟
- 章节脉络：
{chr(10).join(scene_lines) or '  （无）'}
{excerpt_block}

## 文化语境（硬规则，优先级最高）
- **音乐文化必须匹配题材**：中国古代/古装/历史/武侠题材 → 中国传统器乐（guzheng 古筝、erhu 二胡、dizi bamboo flute、xiao 箫、pipa 琵琶、guqin 古琴、Chinese war drums 战鼓、oriental cinematic orchestral），**不要默认西方流行/民谣配器**；现代都市/西方题材才用 piano/guitar/strings
- 主力 provider 是 ElevenLabs（深度文本理解）：**可以且应该写具体民族乐器英文名** —— system prompt 里"避免冷门乐器"仅针对 Mubert tag 池，对本任务不适用
- 全剧 BGM 默认**纯器乐 instrumental（无人声、无唱词）**

## 输出（严格 JSON，不要 markdown 包裹）
{{
  "overall_style": "中文一句话整剧音乐基调",
  "style_tags": ["oriental cinematic", "guzheng"],
  "mood_palette": [
    {{"mood": "边塞苍凉", "music": "sparse guzheng and xiao with low Chinese war drums, desolate, 60 BPM, instrumental"}},
    {{"mood": "冲突紧张", "music": "driving Chinese war drums with erhu tremolo, tense, 110 BPM, instrumental"}}
  ],
  "motif_notes": "主题动机与复用建议：哪类场景共用同一主题（如「家」主题/「真相」主题），跨集如何呼应",
  "avoid": ["western pop", "vocals"],
  "reasoning": "为什么这套基调贴合全剧"
}}
说明：style_tags 4-6 个英文词（genre/mood/乐器，全剧统一，文化匹配题材）；mood_palette 3-5 条「剧情情绪→音乐处理」映射。直接输出 JSON。"""
        response = await self.llm_generate(
            prompt=f"{self.system_prompt}\n\n{prompt}", system=None, json_mode=False, temperature=0.5,
        )
        plan = self._parse_json(response)
        if not plan or not isinstance(plan, dict):
            logger.warning("plan_series_direction 输出不可解析，兜底。前200字: %s", (response or "")[:200])
            plan = {
                "overall_style": ss.get("tone") or "cinematic ambient",
                "style_tags": ["cinematic", "ambient"],
                "mood_palette": [], "motif_notes": "", "avoid": [],
                "reasoning": "LLM 输出不可解析，使用兜底基调",
            }
        plan.setdefault("style_tags", [])
        plan.setdefault("mood_palette", [])
        plan.setdefault("motif_notes", "")
        plan.setdefault("avoid", [])
        return plan

    @staticmethod
    def _scene_windows(shots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 shot 顺序聚合：连续相同 scene_id 的镜头合成一个时间窗（t0/t1 按 duration 累加）。
        窗是配乐的最小单位 —— 多镜共用一段 BGM 的结构基础。"""
        windows: List[Dict[str, Any]] = []
        cur: Optional[Dict[str, Any]] = None
        t = 0
        for s in shots:
            if not isinstance(s, dict):
                continue
            dur = int(s.get("duration_seconds") or 0) or 5
            sid = str(s.get("scene_id") or s.get("scene") or "S?")
            if cur is None or cur["scene_id"] != sid:
                cur = {
                    "scene_id": sid, "t0": t, "t1": t + dur,
                    "scene_name": str(s.get("scene") or ""),  # 分镜自带中文场景名，比 script_scenes 映射稳
                    "shot_ids": [s.get("shot_id")],
                    "emotions": [s.get("emotion") or ""],
                    "actions": [(s.get("action") or "")[:30]],
                }
                windows.append(cur)
            else:
                cur["t1"] = t + dur
                cur["shot_ids"].append(s.get("shot_id"))
                cur["emotions"].append(s.get("emotion") or "")
                cur["actions"].append((s.get("action") or "")[:30])
                if not cur.get("scene_name") and s.get("scene"):
                    cur["scene_name"] = str(s.get("scene"))
            t += dur
        return windows

    @cloud_method
    async def plan_episode_scenes(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Episode 级：按场景时间窗分配 BGM（**不是每镜一个**）。
        时间窗由代码确定（连续同 scene_id 聚合），LLM 只做分配：
        相邻/同情绪窗共用 track_key；命中 series BGM 库则 source=library 复用不再生成。"""
        raw_sb = task.get("storyboard") or []
        if isinstance(raw_sb, dict):
            raw_sb = raw_sb.get("storyboard") or []
        shots = [s for s in raw_sb if isinstance(s, dict)]
        windows = self._scene_windows(shots)
        if not windows:
            return {"scene_bgm_plan": [], "reasoning": "无分镜数据", "total_duration": 0}
        direction = task.get("bgm_direction") or {}
        library = task.get("bgm_library") or {}
        scenes = task.get("script_scenes") or []
        scene_desc = {
            str(sc.get("scene_id")): (sc.get("location") or sc.get("scene_summary") or "")[:60]
            for sc in scenes if isinstance(sc, dict)
        }

        win_lines: List[str] = []
        for i, w in enumerate(windows):
            emo = "/".join(sorted({e for e in w["emotions"] if e}))[:40]
            acts = "、".join(a for a in w["actions"][:3] if a)
            win_lines.append(
                f"  {i}. [{w['t0']}s→{w['t1']}s] {w['scene_id']} {scene_desc.get(w['scene_id'], '')}"
                f" · {len(w['shot_ids'])}镜 · 情绪:{emo or '?'} · 动作:{acts}"
            )
        lib_lines = [
            f"  - {k}: {(v or {}).get('mood', '')} | {((v or {}).get('prompt') or '')[:60]}"
            for k, v in library.items()
        ]
        dir_block = json.dumps(
            {k: direction.get(k) for k in ("overall_style", "style_tags", "mood_palette", "motif_notes", "avoid") if direction.get(k)},
            ensure_ascii=False,
        )

        prompt = f"""你现在的任务是【本集场景配乐分配】。下面是按场景聚合好的时间窗（**每窗一段 BGM、窗内多镜共用**，不是每镜一段）。
为每个窗分配 track_key：**相邻/同情绪的窗尽量共用同一 track_key**（同一首音乐铺过去）；与已有 BGM 库主题吻合的窗**优先复用库**（source=library，prompt 留空，track_key 必须精确用库里的名字）。新 track 才写 prompt（短句+通用词+BPM+用途，≤200字符英文，遵守 system prompt 的 Mubert 规则）。

## 全剧 BGM 风格方向（必须遵守）
{dir_block or '（无，自行把控但保持全集统一）'}

## 已有 BGM 库（可复用）
{chr(10).join(lib_lines) or '（空）'}

## 本集场景时间窗
{chr(10).join(win_lines)}

## 输出（严格 JSON；assignments 按窗序号一一对应，不增不减）
{{
  "assignments": [
    {{"window": 0, "track_key": "home_warm_theme", "track_label": "温馨小家", "source": "new", "mood": "温馨", "prompt": "Gentle warm acoustic with soft piano, calm, 80 BPM, for family drama", "intensity": "low"}},
    {{"window": 1, "track_key": "home_warm_theme", "track_label": "温馨小家", "source": "new", "mood": "温馨", "prompt": "", "intensity": "low"}}
  ],
  "reasoning": "..."
}}
说明：同 track_key 的多个窗只需第一个写 prompt；track_key 用英文蛇形命名表达主题（如 truth_reveal_theme）；**track_label 是给创作者看的中文主题名（2-6 字，如「孤城围困」「家书传情」），必填**。
**prompt 硬规则**：① 文化语境匹配题材——中国古代/古装题材用民族乐器英文名（guzheng/erhu/dizi bamboo flute/xiao/pipa/Chinese war drums）+ oriental cinematic，不要默认西方配器（ElevenLabs 认识具体乐器名，system prompt 的"避免冷门乐器"仅针对 Mubert）；② **一律 instrumental 纯器乐，prompt 里写明 instrumental，禁止 vocals/lyrics/singing**。直接输出 JSON。"""
        response = await self.llm_generate(
            prompt=f"{self.system_prompt}\n\n{prompt}", system=None, json_mode=False, temperature=0.5,
        )
        data = self._parse_json(response) or {}
        assigns = data.get("assignments") or []
        by_idx: Dict[int, Dict[str, Any]] = {}
        for a in assigns:
            if isinstance(a, dict):
                try:
                    by_idx[int(a.get("window", -1))] = a
                except (TypeError, ValueError):
                    continue

        plan: List[Dict[str, Any]] = []
        key_meta: Dict[str, Dict[str, Any]] = {}
        for i, w in enumerate(windows):
            a = by_idx.get(i) or {}
            key = str(a.get("track_key") or f"scene_{w['scene_id'].lower()}_theme").strip()
            meta = key_meta.setdefault(key, {})
            if a.get("prompt"):
                meta["prompt"] = str(a["prompt"])[:200]
            if a.get("track_label"):
                meta.setdefault("label", str(a["track_label"])[:20])
            if a.get("mood"):
                meta.setdefault("mood", a["mood"])
            src = str(a.get("source") or "new").lower()
            if src == "library" and key not in library:
                src = "new"  # 库里没有 → 降级新生成
            plan.append({
                "scene_id": w["scene_id"], "t0": w["t0"], "t1": w["t1"], "shot_ids": w["shot_ids"],
                "scene_label": scene_desc.get(w["scene_id"]) or w.get("scene_name") or "",
                "track_key": key, "track_label": "", "source": src,
                "mood": a.get("mood") or "",
                "intensity": str(a.get("intensity") or "medium").strip().lower(),
                "prompt": "",
            })
        # 回填每 key 的 prompt/mood/中文主题名；新 key 没 prompt → mood 模板兜底
        _auto_label: Dict[str, str] = {}
        for seg in plan:
            meta = key_meta.get(seg["track_key"]) or {}
            seg["prompt"] = meta.get("prompt") or ""
            # 中文主题名：LLM 给的 track_label → 情绪词兜底 → 主题N（绝不露英文 key 给创作者）
            label = meta.get("label")
            if not label:
                if seg["track_key"] not in _auto_label:
                    _auto_label[seg["track_key"]] = (seg.get("mood") or f"主题{len(_auto_label) + 1}")
                label = _auto_label[seg["track_key"]]
            seg["track_label"] = label
            seg["mood"] = seg["mood"] or meta.get("mood") or ""
            if seg["source"] != "library" and not seg["prompt"]:
                tmpl = self._match_mood_template(seg["mood"], direction.get("overall_style") or "")
                seg["prompt"] = tmpl or "Soft cinematic ambient with gentle piano, warm, 75 BPM, for short film"
        # 合并相邻同 key 窗（同一首连续铺，避免重启）
        merged: List[Dict[str, Any]] = []
        for seg in plan:
            if merged and merged[-1]["track_key"] == seg["track_key"] and merged[-1]["t1"] == seg["t0"]:
                merged[-1]["t1"] = seg["t1"]
                merged[-1]["shot_ids"] = (merged[-1]["shot_ids"] or []) + (seg["shot_ids"] or [])
                merged[-1]["scene_id"] = f"{merged[-1]['scene_id']}+{seg['scene_id']}"
                _labels = [x for x in (merged[-1].get("scene_label"), seg.get("scene_label")) if x]
                merged[-1]["scene_label"] = "、".join(dict.fromkeys(_labels))
            else:
                merged.append(seg)
        return {
            "scene_bgm_plan": merged,
            "overall_style": direction.get("overall_style") or "",
            "track_count": len({s["track_key"] for s in merged}),
            "reuse_count": sum(1 for s in merged if s["source"] == "library"),
            "reasoning": data.get("reasoning") or "",
            "total_duration": windows[-1]["t1"] if windows else 0,
        }

    @staticmethod
    def _fallback_plan(total_duration: int, mood: str) -> Dict[str, Any]:
        """LLM 失败时的最小兜底：单段 ambient，覆盖全长。"""
        return {
            "overall_mood": mood or "ambient cinematic",
            "style_tags": ["ambient", "cinematic"],
            "segments": [{
                "t0": 0,
                "t1": int(max(5, total_duration)),
                "mood": "ambient",
                "intensity": "medium",
                "notes": "兜底单段 — LLM 未给出可用 plan",
            }],
            "mubert_prompt": "soft ambient cinematic music, gentle warm pads, slow tempo, minimal instrumentation",
            "mubert_intensity": "low",
            "mubert_playlist_index_hint": "",
            "reasoning": "LLM 返回不可解析，使用兜底 ambient 单段 plan",
        }
