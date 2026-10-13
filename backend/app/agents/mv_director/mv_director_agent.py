"""MV 导演 Agent（1.1.0）—— 把一首歌变成一支能拍的 MV

职责：
1. analyze_song       听懂这首歌：段落结构、情绪弧线、视觉母题
2. analyze_style_refs 看懂参考图：把用户给的几张图固化成一句可复用的风格描述词
3. build_mv_script    写出 MV 概念：叙事型给故事+角色，意象型给意象序列
4. plan_mv_shots      给已定好的时间窗填画面内容

🔴 与普通分镜最根本的差别：**时间不归 Agent 管**。
   每个镜头占哪段时间、盖住哪几句词，由 app/modules/_mv_timeline.py 用代码算死
   （硬约束：每镜 4~12 秒、切点落在歌词句边界、首尾相接总和=歌长）。
   这里只负责「这段时间里画面演什么」。让模型同时管时间和内容，它会把时间算错，
   而时间错了成片就跟歌错位——是那种事后很难定位的坏。

护城河：所有公开方法都带 @cloud_method。打包剥壳器（desktop/gen_agent_stubs.py）
会把带装饰器的方法体换成 pass、把没装饰又不在 KEEP_LOCAL 的方法整个丢掉，
所以**新增公开入口必须记得加装饰器**，否则客户端调用时会 AttributeError。
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.agents.base.base_agent import BaseAgent
from app.core.agent_runtime import cloud_method
from app.modules._mv_timeline import plan_shot_windows, verify_windows

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 老白：MV 导演的 system prompt
# ═══════════════════════════════════════════════════════
MV_DIRECTOR_SYSTEM_PROMPT = """你是一位资深音乐录影带（MV）导演，拍过大量华语与欧美流行、民谣、摇滚的音乐影像。

你的专业判断建立在几条被反复验证的经验上：

【一、画面服从音乐，不是音乐配合画面】
歌是既成事实，长度、节奏、情绪转折都不可改。你的工作是让画面"长"在音乐上：
副歌到来时画面必须有可感知的能量跃升，间奏是留白与呼吸，尾奏要收得住。
一支MV最常见的失败不是画面丑，而是画面和音乐各走各的。

【二、歌词是意象来源，不是画面说明书】
"她独自走过空荡的长街"——不要真的拍一个女孩走在街上。
要拍的是这句词给人的**感受**：空、冷、被世界遗落。
可能是一盏忽明忽暗的路灯、一只被风吹动的塑料袋、雨水里晃动的霓虹倒影。
**直译歌词是业余导演最典型的破绽**。好的MV让观众"想起"歌词，而不是"看到"歌词。
但也不能完全脱钩——观众要能感觉到这个画面和这句词属于同一个世界。

【三、副歌要有记忆点，且必须重复】
同一首歌里副歌会出现2~4次。副歌画面应当建立一个**可辨认的视觉母题**
（一个特定的地点、一个反复的动作、一种特定的光），
每次副歌回来时它也回来，但要有递进——更近、更亮、更决绝、或更破碎。
让观众第二次看到时产生"又是这里"的认知快感。这是MV最重要的结构技巧。

【四、克制运镜】
MV不是炫技场。大部分镜头应当是稳的：固定机位、缓慢推、轻微手持。
把运镜的力气留给两三个真正需要的时刻（副歌炸开、情绪崩塌）。
每个镜头都在动 = 每个镜头都不动，观众会疲劳到麻木。

【五、一致性优先于丰富性】
同一支MV里，色调、光质、镜头语言必须像出自同一个人之手。
宁可十个镜头都在同一个雨夜，也不要十个镜头去十个不同的漂亮地方。
风格散掉的MV看起来像素材拼贴，而不是作品。

【输出纪律】
- 只输出JSON，不要任何解释文字、不要markdown代码围栏。
- 字符串里用中文书名号「」，不要用会破坏JSON的半角引号。
- 给定的时间参数（start/end/duration）**原样返回，一个数字都不许改**。
  时间是上游算好的硬约束，你改了会让整支片子跟歌错位。
"""


def _parse_json(text: str) -> Optional[Any]:
    """LLM 返回的 JSON 有围栏/前后缀是常态，逐级降级解析。"""
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
            pass
    for lo, hi in (("{", "}"), ("[", "]")):
        try:
            return json.loads(text[text.index(lo):text.rindex(hi) + 1])
        except (ValueError, json.JSONDecodeError):
            continue
    return None


def _fmt_lyrics(lines: List[Dict], limit: int = 200) -> str:
    """歌词轴 → 「[mm:ss] 词」逐行文本。带时间戳喂模型，它才能判断段落边界。"""
    out = []
    for ln in (lines or [])[:limit]:
        s = float(ln.get("start", 0))
        out.append(f"[{int(s // 60):02d}:{int(s % 60):02d}] {ln.get('text', '')}")
    if len(lines or []) > limit:
        out.append(f"…（共 {len(lines)} 句，此处省略）")
    return "\n".join(out)


class MVDirectorAgent(BaseAgent):
    """MV 导演 Agent

    使用方式：
        mv = MVDirectorAgent()
        song = await mv.analyze_song(lyrics=[...], duration=245.0)
        style = await mv.analyze_style_refs(image_urls=[...], preset="国风水墨")
        script = await mv.build_mv_script(song=song, style=style, mv_type="narrative")
        shots = await mv.plan_mv_shots(song=song, style=style, script=script, lyrics=[...])
    """

    name = "mv_director"
    description = "MV 导演 Agent（歌曲理解 / 风格锁定 / 卡点分镜）"
    system_prompt = MV_DIRECTOR_SYSTEM_PROMPT

    _SHOTS_PER_BATCH = 6      # 一次填几个镜头。太大→后面的镜头描述明显退化（0.7.1 分镜踩过）
    _BATCH_CONCURRENCY = 4    # 并发批数

    # ─────────────────────────────────────────────
    # 统一入口（引擎按 action 分发）
    # ─────────────────────────────────────────────
    @cloud_method
    async def execute(self, task: Dict[str, Any], context: Dict[str, Any] = None,
                      progress_cb=None) -> Dict[str, Any]:
        action = (task.get("action") or "analyze_song").strip()
        if action == "analyze_song":
            return await self.analyze_song(
                lyrics=task.get("lyrics") or [],
                duration=float(task.get("duration") or 0),
                hint=task.get("hint") or "",
            )
        if action == "analyze_style_refs":
            return await self.analyze_style_refs(
                image_urls=task.get("image_urls") or [],
                preset=task.get("preset") or "",
                mv_type=task.get("mv_type") or "narrative",
            )
        if action == "build_mv_script":
            return await self.build_mv_script(
                song=task.get("song") or {},
                style=task.get("style") or {},
                mv_type=task.get("mv_type") or "narrative",
                user_brief=task.get("user_brief") or "",
            )
        if action == "plan_mv_shots":
            return await self.plan_mv_shots(
                song=task.get("song") or {},
                style=task.get("style") or {},
                script=task.get("script") or {},
                lyrics=task.get("lyrics") or [],
                duration=float(task.get("duration") or 0),
                mv_type=task.get("mv_type") or "narrative",
                progress_cb=progress_cb,
            )
        raise ValueError(f"mv_director 不认识的 action: {action}")

    # ─────────────────────────────────────────────
    # 1. 听懂这首歌
    # ─────────────────────────────────────────────
    @cloud_method
    async def analyze_song(self, lyrics: List[Dict], duration: float,
                           hint: str = "") -> Dict[str, Any]:
        """歌词轴 + 时长 → 段落结构 / 情绪弧线 / 视觉母题。

        段落边界（sections）是下游卡点分镜的输入：副歌切得密、主歌用长镜，
        全靠这里判断得准不准。
        """
        mm, ss = int(duration // 60), int(duration % 60)
        prompt = f"""下面是一首歌的完整歌词，每句都带真实时间戳（由语音识别得到，可能有个别错字）。
歌曲总长 {mm}分{ss}秒（{duration:.1f} 秒）。

{_fmt_lyrics(lyrics)}
{f"创作者补充：{hint}" if hint else ""}

请以MV导演的身份分析这首歌，输出JSON：

{{
  "language": "zh / en / ja / …",
  "genre": "曲风，如 民谣 / 抒情流行 / 独立摇滚",
  "tempo_feel": "slow / mid / fast",
  "theme": "一句话说清这首歌在唱什么",
  "mood_arc": "情绪从哪里走到哪里，用一句话描述整首的走向",
  "sections": [
    {{"type": "intro", "start": 0, "end": 20.5, "summary": "这一段在做什么"}},
    {{"type": "verse", "start": 20.5, "end": 60.0, "summary": "…"}}
  ],
  "visual_motifs": ["贯穿全片的视觉母题，2~4个，要具体到能拍"],
  "chorus_motif": "副歌每次回来都要出现的那个标志性画面，一句话",
  "color_palette": "整体色调，如 冷蓝灰调，只在副歌出现暖光"
}}

段落划分要求（这是本次分析最重要的产出）：
- type 只能用：intro / verse / pre_chorus / chorus / bridge / interlude / outro
- **必须严丝合缝覆盖 0 到 {duration:.1f} 秒**：第一段从0开始，最后一段结束于{duration:.1f}，段与段之间不留缝、不重叠
- 歌词密集且反复出现相同或相近句子的地方通常是副歌
- 有较长时间没有歌词的地方是 intro / interlude / outro
- 副歌至少出现2次的话，每次都要单独列为一个 section

visual_motifs 与 chorus_motif 要能直接拍，不要写"孤独感""青春气息"这种抽象词。"""

        resp = await self.llm_chat(
            messages=[{"role": "system", "content": self.system_prompt},
                      {"role": "user", "content": prompt}],
            temperature=0.7, response_format="json", max_tokens=4096,
        )
        data = _parse_json(resp.get("content", "")) or {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("sections", [])
        data["duration"] = duration
        logger.info("[mv] analyze_song: %d 个段落, 主题=%s",
                    len(data.get("sections") or []), str(data.get("theme"))[:40])
        return data

    # ─────────────────────────────────────────────
    # 2. 看懂参考图 → 固化风格描述词
    # ─────────────────────────────────────────────
    @cloud_method
    async def analyze_style_refs(self, image_urls: List[str], preset: str = "",
                                 mv_type: str = "narrative") -> Dict[str, Any]:
        """把用户给的风格参考图，固化成一句能拼进所有出图 prompt 的风格描述词。

        没给参考图时只按预设文字生成，不调 vision。
        """
        ask = """请把这些参考图共有的**视觉风格**提炼出来，输出JSON：

{
  "style_prompt": "一句可直接拼进出图提示词的风格描述，60~120字，要具体（画种/笔触/光质/色调/质感），不要写主题内容",
  "palette": "主色与点缀色",
  "lighting": "光线特征，如 侧逆光、柔和漫射、强对比硬光",
  "texture": "质感，如 胶片颗粒、水彩晕染、赛璐璐平涂",
  "camera": "镜头感，如 浅景深长焦 / 广角轻微畸变",
  "avoid": "为了保持这个风格，出图时要避免出现什么"
}

只描述风格，不要描述图里画了什么内容——因为这句话会被套用到全片所有画面上。
如果多张图风格不完全一致，取它们的**共同点**，并在 style_prompt 里偏向最统一的那个方向。"""

        if preset:
            ask += f"\n\n创作者指定的风格基调：{preset}（请与参考图融合，冲突时以参考图为准）。"

        try:
            if image_urls:
                # 注意 llm_chat_with_vision 返回的是**字符串**，不是 llm_chat 那样的 dict
                content = await self.llm_chat_with_vision(
                    text_prompt=ask, image_urls=image_urls[:6],
                    system=self.system_prompt, temperature=0.4, json_mode=True,
                )
            else:
                resp = await self.llm_chat(
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content":
                            f"创作者想要的MV视觉风格是：「{preset or '电影感写实'}」。\n"
                            f"请把它展开成完整的风格设定。\n\n{ask}"},
                    ],
                    temperature=0.6, response_format="json", max_tokens=1500,
                )
                content = resp.get("content", "")
        except Exception as e:
            logger.warning("[mv] 风格分析失败，退回预设文字: %s", e)
            content = ""

        data = _parse_json(content) or {}
        if not isinstance(data, dict):
            data = {}
        if not data.get("style_prompt"):
            # 兜底：风格描述词绝不能空——它是全片一致性的锚，空了每个镜头都会自由发挥
            data["style_prompt"] = preset or "电影感写实，柔和自然光，浅景深，统一冷调"
        data["reference_images"] = list(image_urls or [])
        data["preset"] = preset
        logger.info("[mv] style_prompt = %s", str(data.get("style_prompt"))[:60])
        return data

    # ─────────────────────────────────────────────
    # 3. MV 剧本概念
    # ─────────────────────────────────────────────
    @cloud_method
    async def build_mv_script(self, song: Dict[str, Any], style: Dict[str, Any],
                              mv_type: str = "narrative",
                              user_brief: str = "") -> Dict[str, Any]:
        """产出 MV 概念。

        mv_type='narrative' 叙事型：有角色有故事线，下游要出角色三视图、走锁脸链路。
        mv_type='visual'    意象型：无固定角色，靠场景与意象说话。
        """
        sections_brief = "\n".join(
            f"  {s.get('type')}  {float(s.get('start', 0)):.0f}s–{float(s.get('end', 0)):.0f}s  {s.get('summary', '')}"
            for s in (song.get("sections") or [])
        )
        common = f"""这首歌的分析结果：
- 主题：{song.get('theme', '')}
- 情绪走向：{song.get('mood_arc', '')}
- 曲风 / 速度：{song.get('genre', '')} / {song.get('tempo_feel', '')}
- 视觉母题：{'、'.join(song.get('visual_motifs') or [])}
- 副歌标志画面：{song.get('chorus_motif', '')}
- 色调：{song.get('color_palette', '')}
- 总长：{float(song.get('duration') or 0):.1f} 秒

段落结构：
{sections_brief}

全片视觉风格（所有画面都必须遵守）：
{style.get('style_prompt', '')}
{f"创作者要求：{user_brief}" if user_brief else ""}
"""

        if mv_type == "visual":
            prompt = common + """
请设计一支**意象型MV**（没有固定主角，靠场景、物件、光影与自然意象承载情绪）。

输出JSON：
{
  "type": "visual",
  "concept": "整支MV的核心视觉概念，2~3句",
  "through_line": "把所有画面串成一体的那条线索（一个物件/一种光/一个地点的变化）",
  "scenes": [
    {"scene_id": "SC01", "name": "场景名", "description": "环境的具体样子，要能直接拿去出图",
     "time_of_day": "黄昏/深夜/清晨…", "mood": "情绪"}
  ],
  "imagery_sequence": [
    {"section": "对应段落type", "imagery": "这一段的核心意象", "escalation": "相对上一次副歌的递进（非副歌填空字符串）"}
  ]
}

scenes 控制在 4~7 个：**场景太多会让全片散掉**，宁可同一个场景在不同时间/光线下反复出现。
"""
        else:
            prompt = common + """
请设计一支**叙事型MV**（有具体人物，有一条能看懂的情感线索）。

输出JSON：
{
  "type": "narrative",
  "logline": "一句话说清这支MV在讲什么",
  "characters": [
    {"name": "角色名", "role": "主角/配角", "age": "年龄段", "gender": "性别",
     "appearance": "外貌与穿着，要具体到能出三视图：发型发色、脸型、体型、服装款式与颜色",
     "personality": "性格", "arc": "这个人在片中经历了什么变化"}
  ],
  "scenes": [
    {"scene_id": "SC01", "name": "场景名", "description": "环境具体样子，能直接出图",
     "time_of_day": "…", "mood": "…"}
  ],
  "beats": [
    {"section": "对应段落type", "beat": "这一段在故事里发生了什么", "emotion": "情绪"}
  ]
}

硬性要求：
- 角色**最多2个**。MV只有几分钟，人一多观众记不住，锁脸成本也翻倍。
- scenes 控制在 4~7 个，理由同上：场景一散，全片就像素材拼贴。
- 故事不必有完整起承转合，MV允许留白与跳跃，但情绪必须连贯。
- appearance 必须写到能凭它画出稳定的同一个人——这段文字会直接用来生成角色三视图。
"""

        resp = await self.llm_chat(
            messages=[{"role": "system", "content": self.system_prompt},
                      {"role": "user", "content": prompt}],
            temperature=0.8, response_format="json", max_tokens=4096,
        )
        data = _parse_json(resp.get("content", "")) or {}
        if not isinstance(data, dict):
            data = {}
        data["type"] = mv_type
        data.setdefault("scenes", [])
        data.setdefault("characters", [])
        logger.info("[mv] build_mv_script(%s): %d 场景 %d 角色",
                    mv_type, len(data["scenes"]), len(data["characters"]))
        return data

    # ─────────────────────────────────────────────
    # 4. 卡点分镜
    # ─────────────────────────────────────────────
    @cloud_method
    async def plan_mv_shots(self, song: Dict[str, Any], style: Dict[str, Any],
                            script: Dict[str, Any], lyrics: List[Dict],
                            duration: float, mv_type: str = "narrative",
                            progress_cb=None) -> Dict[str, Any]:
        """先用代码算出镜头时间窗，再让模型逐批填画面内容。

        时间窗算完就是既定事实：模型填完内容后，时间字段一律用算出来的值覆盖回去，
        不采信模型返回的任何时间。
        """
        duration = float(duration or song.get("duration") or 0)
        windows = plan_shot_windows(lyrics, song.get("sections"), duration)
        if not windows:
            raise ValueError("时间窗规划失败：歌曲时长为 0 或歌词轴为空")

        problems = verify_windows(windows, duration)
        if problems:
            # 契约破了说明 _mv_timeline 有 bug，早发现早修，别把错时间轴带到下游
            logger.error("[mv] 时间窗违规: %s", problems)
            raise ValueError("镜头时间轴不合规：" + "；".join(problems[:3]))

        def emit(msg: str):
            if progress_cb:
                try:
                    progress_cb(msg)
                except Exception:
                    pass

        emit(f"已按歌切出 {len(windows)} 个镜头，正在设计画面…")

        batches = [windows[i:i + self._SHOTS_PER_BATCH]
                   for i in range(0, len(windows), self._SHOTS_PER_BATCH)]
        sem = asyncio.Semaphore(self._BATCH_CONCURRENCY)
        done = [0]

        async def run_batch(bi: int, batch: List[Dict]) -> List[Dict]:
            async with sem:
                try:
                    filled = await self._fill_batch(bi, batch, song, style, script, mv_type, len(windows))
                except Exception as e:
                    logger.warning("[mv] 第 %d 批填充失败，退回占位内容: %s", bi + 1, e)
                    filled = {}
                done[0] += 1
                emit(f"画面设计 {done[0]}/{len(batches)} 批")
                return [self._merge_shot(w, filled.get(str(w["index"])) or filled.get(w["index"]) or {},
                                         style, script, mv_type) for w in batch]

        results = await asyncio.gather(*[run_batch(i, b) for i, b in enumerate(batches)])
        shots = [s for group in results for s in group]

        # 最终防线：时间字段以代码算的为准，模型改了也无效
        for w, sh in zip(windows, shots):
            sh["start"], sh["end"] = w["start"], w["end"]
            sh["duration_seconds"] = w["duration"]

        return {
            "shots": shots,
            "total_shots": len(shots),
            "duration": duration,
            "style_prompt": style.get("style_prompt", ""),
        }

    # ─── 以下为内部方法：没有 @cloud_method，只在云端执行 ───

    async def _fill_batch(self, bi: int, batch: List[Dict], song: Dict, style: Dict,
                          script: Dict, mv_type: str, total: int) -> Dict[str, Any]:
        """给一批镜头填画面内容。返回 {镜头序号: {...}}。"""
        scenes = script.get("scenes") or []
        scene_list = "\n".join(
            f"  {s.get('scene_id', '')} {s.get('name', '')}：{s.get('description', '')}"
            for s in scenes) or "  （未定义场景，请自行设定并保持全片一致）"

        chars = script.get("characters") or []
        char_list = "\n".join(
            f"  {c.get('name', '')}（{c.get('role', '')}）：{c.get('appearance', '')}"
            for c in chars) or "  （本片无固定角色）"

        shot_lines = []
        for w in batch:
            words = " / ".join(l.get("text", "") for l in (w.get("lyrics") or [])) or "（纯器乐，无歌词）"
            shot_lines.append(
                f'  镜头{w["index"]}：{w["start"]:.1f}s–{w["end"]:.1f}s（{w["duration"]:.1f}秒）'
                f'，段落={w["section"]}，此刻在唱：{words}'
            )

        prompt = f"""正在为一支MV设计分镜。全片共 {total} 个镜头，这是第 {bi + 1} 批。

【全片设定】
主题：{song.get('theme', '')}
情绪走向：{song.get('mood_arc', '')}
副歌标志画面：{song.get('chorus_motif', '')}
视觉母题：{'、'.join(song.get('visual_motifs') or [])}
{'故事：' + script.get('logline', '') if mv_type == 'narrative' else '视觉概念：' + script.get('concept', '')}
贯穿线索：{script.get('through_line', '') or script.get('logline', '')}

【全片统一风格（每个画面都必须符合）】
{style.get('style_prompt', '')}
光线：{style.get('lighting', '')}　色调：{style.get('palette', '')}　质感：{style.get('texture', '')}
要避免：{style.get('avoid', '')}

【可用场景】
{scene_list}

【出场人物】
{char_list}

【本批镜头（时间已定死，不许改）】
{chr(10).join(shot_lines)}

请为每个镜头设计画面，输出JSON（key 是镜头序号的字符串）：

{{
  "{batch[0]['index']}": {{
    "scene_id": "从上面场景里选一个的ID",
    "scene": "场景名",
    "scene_description": "这个镜头里环境的具体样子",
    "shot_type": "远景/全景/中景/近景/特写/大特写",
    "camera_move": "固定/缓推/缓拉/横移/轻微手持/跟随",
    "visual_prompt": "生成这一帧静态画面的完整提示词。要包含：主体、动作姿态、环境、光线、构图、景别。80~150字。不要写风格词（风格会由系统统一附加）",
    "video_prompt": "这个镜头里**动起来**的是什么。描述主体动作与镜头运动，30~60字。注意只有{batch[0]['duration']:.0f}秒左右，别设计复杂的多阶段动作",
    "characters": ["出现在这个镜头里的角色名，没有就空数组"],
    "mood": "这个镜头的情绪",
    "transition_to_next": "cut / dissolve / fade",
    "pacing_role": "这个镜头在整体节奏里的作用，如 铺陈/推进/爆发/留白/收束"
  }}
}}

设计要求（重要）：
1. **画面呼应歌词，但不要直译歌词**。唱"走过长街"不必真拍人走路，
   要拍出那句词的感受。但观众要能感到画面和歌词属于同一个世界。
2. **副歌镜头必须复用副歌标志画面**，每次回来都更进一步（更近/更亮/更破碎）。
3. 场景要**反复使用**上面列出的那几个，不要每个镜头换新地方——全片会散。
4. 大部分镜头 camera_move 用「固定」或「缓推」。只在情绪最强的一两个镜头用大运动。
5. 纯器乐段（无歌词）是呼吸口，用留白的空镜、环境细节，不要硬塞信息。
6. 只输出JSON，不要解释。"""

        resp = await self.llm_chat(
            messages=[{"role": "system", "content": self.system_prompt},
                      {"role": "user", "content": prompt}],
            temperature=0.85, response_format="json", max_tokens=6000,
        )
        data = _parse_json(resp.get("content", ""))
        return data if isinstance(data, dict) else {}

    def _merge_shot(self, window: Dict, filled: Dict, style: Dict,
                    script: Dict, mv_type: str) -> Dict[str, Any]:
        """时间窗 + 模型填的内容 → 下游能直接消费的镜头对象。

        字段名与 storyboard 产出对齐，好让 keyframe / video / merge 全链路零改动复用。
        """
        idx = window["index"]
        lyrics = window.get("lyrics") or []
        lyric_text = " ".join(l.get("text", "") for l in lyrics)
        scenes = script.get("scenes") or []
        fallback_scene = scenes[idx % len(scenes)] if scenes else {}

        visual = (filled.get("visual_prompt") or "").strip()
        if not visual:
            # 兜底也要给出能出图的东西，否则这一镜会整个空掉
            visual = (fallback_scene.get("description")
                      or script.get("concept")
                      or script.get("logline")
                      or "空镜，环境细节特写")

        # 风格词常自带句尾标点，直接拼会出现「…意境空灵。。远景，…」
        style_prompt = (style.get("style_prompt") or "").strip().rstrip("。.，,；;")
        return {
            "shot_id": f"SHOT{idx:02d}",
            "index": idx,
            "start": window["start"],
            "end": window["end"],
            "duration_seconds": window["duration"],
            "section": window.get("section", "verse"),
            "lyrics": lyrics,
            "lyric_text": lyric_text,

            "scene_id": filled.get("scene_id") or fallback_scene.get("scene_id") or f"SC{(idx % 5) + 1:02d}",
            "scene": filled.get("scene") or fallback_scene.get("name") or "主场景",
            "scene_description": filled.get("scene_description") or fallback_scene.get("description") or "",
            "shot_type": filled.get("shot_type") or "中景",
            "camera_move": filled.get("camera_move") or "固定",

            # 🔴 风格描述词拼在提示词最前面：全片一致性的锚点。
            #    放在后面会被前面的具体描述稀释，实测前置效果明显更稳。
            "visual_prompt": f"{style_prompt}。{visual}" if style_prompt else visual,
            "video_prompt": filled.get("video_prompt") or "画面轻微呼吸感，镜头缓慢推进",

            "characters": filled.get("characters") or ([] if mv_type == "visual" else []),
            "mood": filled.get("mood") or "",
            "pacing_role": filled.get("pacing_role") or "",
            "transition_to_next": filled.get("transition_to_next") or "cut",
            "requires_keyframe": True,

            # 🔴 MV 的声音只有那首歌。这两个字段留空，
            #    否则下游音频环节会把歌词当旁白念出来，直接毁掉成片。
            "dialogue": "",
            "narration": "",
        }
