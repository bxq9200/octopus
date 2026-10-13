"""
编剧 Agent — 将用户想法转化为可拍摄、可生成、可拆分的剧本内容

职责：
1. 根据导演 Agent 提供的创作方向生成故事梗概
2. 设计人物关系、冲突、情绪变化和叙事节奏
3. 输出适合 AI 视频生成的短视频剧本、广告脚本、旁白脚本或剧情分场
4. 将抽象创意拆解为清晰的场景、动作、对白、旁白和情绪指令
5. 为后续分镜 Agent、美术 Agent、音频 Agent 提供明确输入
"""

import json
import logging
import re
from typing import Any, Dict, List

from app.agents.base.base_agent import BaseAgent
from app.core.agent_runtime import cloud_method  # 护城河：cloud_client 下整体路由云端

logger = logging.getLogger(__name__)


def _parse_target_seconds(value) -> int:
    """把计划时长字符串解析成秒：'9分钟'→540 / '540秒'→540 / '540'→540。解析不出返回 0。"""
    s = str(value or "")
    m = re.search(r"(\d+)\s*分", s)
    if m:
        return int(m.group(1)) * 60
    m2 = re.search(r"(\d+)", s)
    return int(m2.group(1)) if m2 else 0


# ═══════════════════════════════════════════════════════
# 老白：编剧 Agent 的 system prompt
# ═══════════════════════════════════════════════════════
SCREENWRITER_SYSTEM_PROMPT = """你是 Octopus Video OS 的编剧 Agent（Screenwriter Agent），负责将用户想法转化为可拍摄、可生成、可拆分的剧本内容。

## 核心职责
1. 根据导演 Agent 提供的创作方向生成故事梗概。
2. 设计人物关系、冲突、情绪变化和叙事节奏。
3. 输出适合 AI 视频生成的短视频剧本、广告脚本、旁白脚本或剧情分场。
4. 将抽象创意拆解为清晰的场景、动作、对白、旁白和情绪指令。
5. 为后续分镜 Agent、美术 Agent、音频 Agent 提供明确输入。

## 剧本类型适配
- 短视频：节奏快，开头3秒要有吸引力（钩子）
- 商业视频：突出产品卖点和用户痛点
- 剧情视频：明确起承转合
- 口播视频：口语化、有感染力
- 纪录片：真实感、信息密度

## 你必须遵守
- 剧本必须适合视频生成，不要写无法视觉化的抽象段落。
- 每一场都要包含：场景、角色、动作、情绪、画面重点。
- **剧本细化（重要）**：原始素材可能很"薄"（一句话剧情 / 一句对白 / 极简描述）。必须补成**可拍的完整画面**——具体环境与光影、角色身体语言与表情、关键视觉锚点；不是把对白原样转录。action / visual_notes 要细到分镜/美术能直接据此生成画面。
- 如果是短视频，节奏必须快，开头3秒要有吸引力。
- 如果是商业视频，要突出产品卖点和用户痛点。
- 如果是剧情视频，要有明确起承转合。
- **叙事连续性（0.7.4，重要）**：故事要**线性推进**，每场在前一场基础上往前走。① **不重复交代**：`prior_context` 里已知的信息（观众上一场已看到的），本场不要再用对白/旁白重新解释一遍；② **不提前泄露**：后面场次/后面集才发生的关键转折、真相、结局，**严禁**在当前场提前演出或用台词剧透（伏笔可埋、不可揭）；③ 每场靠 `reveals`（本场新推进的东西）挣存在意义——什么都没推进、和上一场重复的戏，删掉或并入相邻场。
- 不要主动添加敏感、暴力、血腥、违法或版权角色内容。
- 不要使用真实明星、现有IP角色，除非用户明确拥有授权。

## 对白 / 旁白 / 音效规范（重要 — 初版视频要带声音内容）
视频初版需要带**对白、旁白、与剧情相关的音效**（**不含 BGM**，背景音乐后期单独加）。后期配音模块是基于初版视频来做的，所以这些内容必须先在剧本里写清楚、给足。
- **对白（dialogue）**：剧情片 / 漫剧 / 短剧是**对话驱动**的——**凡有角色互动、交流、冲突、情绪交锋的场景都必须写对白，不要留空**。
  - "不是对白原样转录"指的是：**薄素材要额外补全画面**，**绝不是删掉对白**——对白和画面细节**两者都要有**。
  - 对白要贴合角色身份/性格/当下情绪，口语自然、避免书面腔；一来一回的交锋比单句独白更有戏。
- **旁白（voiceover）**：需要交代背景 / 时间跳跃 / 内心活动时写画外音或内心独白；纯动作场景可留空。
- **音效（sound_effects）**：列出**与剧情/动作直接相关**的具体音效（给视频模型生成环境音用）：
  - 动作音：脚步声、跑动、开关门、碗筷碰撞、拳脚打斗、摔倒、物体落地
  - 环境音：雨声、风声、街市嘈杂、钟摆滴答、电话铃、人群喧哗
  - 情绪音：心跳、喘息、叹息、啜泣
  - **不要写 BGM / 背景音乐**；确无明显音效的静态场景可留空数组。

## 语言规范（0.6.3）
- **scene_summary / action / emotion / dialogue / voiceover / visual_notes 全部用中文**（让后续美术 / 分镜 / 配音 Agent 理解情绪、世界观、氛围最准确）
- visual_notes 里可少量穿插英文镜头术语（如 `close-up`、`wide shot`、`low angle`）作为分镜的提示，但情绪/动作/氛围必须中文
- characters[*].description / personality 中文

## 输出格式（JSON）
{
  "script_title": "剧本标题",
  "logline": "一句话梗概",
  "style": "风格",
  "duration": "预计时长",
  "structure": {
    "hook": "开头钩子（3秒）",
    "development": "发展",
    "turning_point": "转折",
    "ending": "结尾"
  },
  "scenes": [
    {
      "scene_id": "S01",
      "scene_summary": "场景摘要",
      "prior_context": "本场开始前观众已经知道的信息（一句话，用于下游避免重复交代已知内容；第一场填'无'）",
      "reveals": "本场推进或新揭示的关键信息（一句话，本场存在的意义；纯情绪铺垫填'情绪推进：xxx'）",
      "location": "地点",
      "characters": ["角色1", "角色2"],
      "action": "动作描述",
      "emotion": "情绪",
      "dialogue": "对白（角色台词；凡有角色互动/对话/冲突的场景必填，别留空）",
      "voiceover": "旁白（画外音/内心独白，用于交代信息或情绪；纯动作场景可留空）",
      "sound_effects": ["与剧情相关的具体音效，如：脚步声、关门声、打斗声、雨声、心跳声"],
      "visual_notes": "画面要点",
      "duration_seconds": 5
    }
  ],
  "characters": [
    {
      "character_id": "C01",
      "name": "角色名",
      "description": "角色描述",
      "personality": "性格"
    }
  ],
  "props": [
    {
      "prop_id": "P01",
      "name": "道具名（如：龙吟剑 / 青铜古镜 / 神秘信封）",
      "description": "道具的视觉特征（材质、颜色、尺寸、关键细节）",
      "use_case": "在剧情中的作用，在第几场出现"
    }
  ]
}

## 关键道具（props）规范（0.6.4 新增）
**你必须主动从剧本中识别关键道具，不要等用户提供**。判定规则（满足任一即列为关键道具）：

1. **跨场景重复**：同一道具在 **≥2 个 scene** 的 action / visual_notes / dialogue 里出现 → 必列
2. **单场景高频**：在 **同一个 scene** 的 action + visual_notes + dialogue 中出现 **≥3 次** → 必列
3. **剧情关键**：道具直接推动剧情转折（被发现、被夺走、被毁坏、揭示秘密、引发冲突）→ 必列
4. **独特视觉**：道具有独特外观，让观众一眼能认出来（古剑、护身符、特殊纹样的信物）→ 必列

**操作步骤**：
- 写完所有 scenes 后，**回头扫描每个 scene 的 action / visual_notes / dialogue**，把名词性物件挑出来计数
- 跨场景出现 OR 单场景 ≥3 次的，按上面 4 条规则过一遍，命中就放进 props 数组
- 数量上限 **≤5 个**；候选过多时按"剧情关键 > 跨场景重复 > 高频出现 > 独特视觉"优先级筛选

**不算关键道具**：通用日用品（杯子、椅子、手机、纸笔等）— 除非它是剧情核心（如《泰坦尼克号》"海洋之心"项链、《指环王》"至尊魔戒"）

**绝不能输出空 props**，除非剧本内容确实没有任何符合上述规则的物件（极少见 — 像纯对白片、纯风景纪录片）。即使用户没明确说有什么道具，你也要从剧本里**主动挖掘**。

每个 props 必须能让美术为它单独画一张特写图（独立、可识别、有记忆点）。
"""


class ScreenwriterAgent(BaseAgent):
    """
    编剧 Agent
    
    使用方式：
        writer = ScreenwriterAgent()
        result = await writer.run({
            "topic": "一只橘猫和主人的温馨日常",
            "style": "温情治愈",
            "duration": "60秒",
            "project_type": "社媒短视频"
        })
    """
    
    name = "screenwriter"
    description = "剧本创作 Agent"
    system_prompt = SCREENWRITER_SYSTEM_PROMPT
    
    @cloud_method
    async def execute(self, task: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        编剧核心逻辑
        
        Args:
            task: {
                "topic": "主题",
                "style": "风格",
                "duration": "时长",
                "audience": "受众",
                "mood": "情绪",
                "project_type": "项目类型",
                "characters": ["角色1"],
                "scenes": ["场景1"]
            }
        """
        topic = task.get("topic", "")
        style = task.get("style", "通用")
        duration = task.get("duration", "60秒")
        project_type = task.get("project_type", "社媒短视频")
        
        # 构建创作提示
        prompt = self._build_prompt(task)
        
        # 调用 LLM 生成剧本
        response = await self.llm_generate(
            prompt=prompt,
            system=self.system_prompt,
            json_mode=True,
            temperature=0.7,
            max_tokens=16384,  # 0.7.0：分集要拆出 ~10+ 个含 action/dialogue 的场景，8k 会逼 LLM 压缩成少数大场景
        )
        
        # 解析结果
        try:
            script = json.loads(response)
        except json.JSONDecodeError:
            return {
                "error": "剧本生成失败 - JSON 解析错误",
                "raw_response": response[:500]
            }
        
        # 后处理：确保字段完整
        script = self._normalize_script(script, task)
        
        return {
            "script": script,
            "summary": {
                "title": script.get("script_title", "未命名"),
                "scene_count": len(script.get("scenes", [])),
                "character_count": len(script.get("characters", [])),
                "estimated_duration": duration
            }
        }
    
    def _build_prompt(self, task: Dict[str, Any]) -> str:
        """构建创作提示"""
        # 进阶模式下，导演已经与创作者协商出 outline，这里以 outline 为骨架做细化
        outline = task.get("outline") or {}
        outline_block = ""
        is_episode = isinstance(outline, dict) and outline.get("_episode_index")
        if isinstance(outline, dict) and outline:
            # 0.7.0 轮 2：从 outline 抽出原著节选，单独成块（不放 outline_json，避免 LLM 把它当大纲看）
            raw_passages = outline.get("_raw_passages") or []
            # 抽完用浅拷贝 outline 去掉 raw_passages，避免重复给 LLM
            outline_for_dump = {k: v for k, v in outline.items() if k != "_raw_passages"}
            outline_json = json.dumps(outline_for_dump, ensure_ascii=False, indent=2)

            episode_warning = ""
            # 默认（非分集进阶大纲）：保留导演协商好的场次划分
            refine_rules = (
                "- 故事结构、角色设置、场次划分、scene_id 必须**完全保留** outline 的设定，不要增加/删减/重排场次\n"
                "- 每场 scene 的 duration_seconds 沿用 outline 给的值（若 outline 没给则用合理默认）\n"
                "- 角色描述基于 outline.characters 扩展（外形 / 服装 / 情绪），但 name 保持一致"
            )
            if is_episode:
                ep_idx = outline.get("_episode_index")
                ep_total = outline.get("_episode_total") or "?"
                scene_range = outline.get("_scene_range") or ""
                cliff = outline.get("_cliff_hanger") or ""
                char_count = len(outline.get("characters") or [])
                scene_count = len(outline.get("scenes") or [])
                # 0.7.0 #2：目标总时长取 task.duration（分集计划时长，如"540秒"），
                # **不能**用 outline.scenes 的 duration —— 那是占位 5s，会把 target 算成 0/2。
                ep_total_sec = _parse_target_seconds(task.get("duration"))
                target_count = max(scene_count, round(ep_total_sec / 50)) if ep_total_sec else max(scene_count, 8)
                episode_warning = f"""

## ⚠️ 【极重要：这是漫剧第 {ep_idx}/{ep_total} 集，不是整剧】
- outline.scenes 是导演为本集筛出的 **{scene_count} 段粗线条剧情**（这是「剧情范围」，**不是最终场景数**）——你在这几段剧情范围内写剧本
- outline.characters 是本集出场角色（共 {char_count} 个），**严禁加 outline 之外的人物**
- **范围红线（关于"剧情"，不是"场景数"）**：只讲这 {scene_count} 段剧情对应的故事，**不要扩展到整部剧 / 其它集的支线**；其它集会单独制作
- **场景数按剧情走、不是按数字凑**：粗 outline 的 {scene_count} 段只是「剧情范围」，请把每段拆成它**剧情真正需要**的细场景节拍（别只写 {scene_count} 个大场景）；参考量级 ≈ **{target_count}** 个场景，但**以剧情为准，不硬凑也不硬缩**
- 原剧本对应范围：{scene_range}
- 本集 cliff-hanger（必须在最后一场体现）：{cliff or '（无 — 最后一集，给个情感落点）'}
- title / logline 用 outline.title / outline.logline（已经是"本集"的标题和梗概，不是整剧的）
"""
                avg_sec = round(ep_total_sec / target_count) if (ep_total_sec and target_count) else 50
                refine_rules = (
                    f"- 【场景粒度 — 重要】本集目标时长 ≈ {ep_total_sec or '按 outline'} 秒（创作者期望，**参考不是硬指标**）。outline 的场景偏粗（仅 {scene_count} 个），"
                    f"请**把长场景按剧情细分成多个子场景**，参考量级 ≈ **{target_count}** 个（**以剧情为准，不硬凑也不硬缩**）。\n"
                    "  - 子场景必须**落在原 outline 场景的剧情范围内**（把一段戏拆成起承转合的节拍），"
                    "**严禁引入 outline 之外的剧情、人物或其它集的内容**（这是分集，不是整剧）\n"
                    "  - 子场景 scene_id 用原 id 加后缀（如 S01 → S01-1 / S01-2 / S01-3）\n"
                    "  - ⚠️【时长定义 — 极重要，上一版在这里出错了】duration_seconds 是「**这段戏在成片里占多少秒**」，"
                    "**不是单个镜头的时长**！一个场景通常 **30-90 秒**（后面 storyboard 会把每个场景再拆成 4-8 个 4-15s 的镜头）。"
                    "**绝对不要写 8 / 12 / 15 这种镜头级的小数字** —— 那会让整集严重缩水。\n"
                    f"  - **总时长跟着剧情走，不硬凑**：目标 ≈ {ep_total_sec or '计划时长'} 秒只是**参考**（来自创作者期望，约 {target_count} 场、每场 ≈ {avg_sec}s）。剧情自然写足到这个量级最好；**天然偏短**就把剧情节拍写细（真实戏剧内容，不是注水空场）；**天然偏长**在保剧情前提下精简。绝不为凑数字硬撑空场景、也不为压时长硬删剧情。\n"
                    "  - 每个子场景都要有自己饱满的 action / emotion / dialogue / sound_effects / visual_notes，不要只复制原场景描述；"
                    "原剧情薄时**补全成可拍画面**（环境光影/身体语言/视觉锚点）。注意'不是对白转录'指**额外补画面**、"
                    "**不是删对白**——有角色互动/对话的场景**必须写足对白**，并列出与剧情相关的音效（脚步/开门/打斗/环境音等）\n"
                    "  - 【场景空间尺度】每个场景只取**剧情实际需要的交互空间**：只用床边一角就别写整间卧室、只在门口就别写整栋楼——避免无脑大场景（失真又费算力）\n"
                    "- 角色设置保留 outline.characters（name 一致），严禁新增 outline 之外的人物"
                )

            # 原著节选块（如果有的话）— 编剧的"事实依据"，dialogue / 关键场景描写必须沿用
            raw_block = ""
            if raw_passages:
                raw_dump = json.dumps(raw_passages, ensure_ascii=False, indent=2)
                raw_block = f"""

## 📖 【原著节选（极重要 — 严格遵循）】
{raw_dump}

**对白用原著台词**（带引号的话原样用），**关键场景描写沿用原著用词**，不要 paraphrase 改写。
可以补充镜头建议 / 节奏点 / 视觉细节，但**事件、对白、角色行为必须忠于原著节选**。
原著没写的部分（如转场细节）可以二创，但不要改原著已写明的剧情。
"""

            outline_block = f"""{episode_warning}{raw_block}

## 【导演已协商的剧本大纲（必须遵循）】
{outline_json}

你的任务是**基于以上大纲细化剧本**：补 dialogue（对白，对话场景必须给足）/ voiceover（旁白）/ sound_effects（剧情相关音效：脚步/打斗/环境音等）/ 详细场景描述 / 镜头建议 / 情绪节拍。**有原著节选时对白严格沿用原著台词**（带引号的原话原样用），不要 paraphrase。
{refine_rules}
"""

        return f"""请为以下创作需求生成完整剧本：

主题：{task.get("topic", "")}
风格：{task.get("style", "通用")}
时长：{task.get("duration", "60秒")}
项目类型：{task.get("project_type", "社媒短视频")}
受众：{task.get("audience", "通用受众")}
情绪基调：{task.get("mood", "温馨")}

角色要求：{json.dumps(task.get("characters", []), ensure_ascii=False)}
场景要求：{json.dumps(task.get("scenes", []), ensure_ascii=False)}
{outline_block}
请生成完整的剧本 JSON，确保：
1. 开头3秒有吸引力（钩子）
2. 每一场都有明确的场景、动作、情绪；**原始素材若是一句话/一句对白这种极简输入，必须扩展成可拍的完整场景**（环境与光影细节、角色身体语言与表情、视觉重点），而非对白转录
3. 角色描述具体可视觉化
4. 总时长控制在要求范围内
5. **每场 scene 的 duration_seconds 必须 >= 5**（视频生成模型最低 4 秒，留缓冲）；如内容紧凑可适度合并相邻场景，避免 3 秒以下的小碎片
6. **关键道具 props**：**写完 scenes 后必做** — 扫描每个 scene 的 action/visual_notes/dialogue，按 system prompt 的判定规则（跨场景重复 / 单场景≥3次 / 剧情关键 / 独特视觉）主动挖掘关键道具，输出 name + description（材质颜色尺寸细节）+ use_case（在哪几场出现做什么）。**不要等用户告诉你**；几乎所有剧情类剧本都有 1-3 个关键道具
7. **对白 / 旁白 / 音效**：凡有角色互动/对话的场景**必须给足 dialogue**（有原著节选则沿用原著台词、不要改写）；需要交代背景或内心时加 voiceover；每场按剧情列出 sound_effects（脚步 / 开关门 / 打斗 / 雨声 / 环境音等），**不写 BGM**
"""
    
    def _normalize_script(self, script: Dict, task: Dict) -> Dict:
        """规范化剧本结构"""
        # 确保必要字段存在
        if "script_title" not in script:
            script["script_title"] = task.get("topic", "未命名")[:20]
        if "logline" not in script:
            script["logline"] = f"关于{task.get('topic', '')}的{task.get('style', '')}视频"
        if "style" not in script:
            script["style"] = task.get("style", "通用")
        if "duration" not in script:
            script["duration"] = task.get("duration", "60秒")
        if "structure" not in script:
            script["structure"] = {
                "hook": "",
                "development": "",
                "turning_point": "",
                "ending": ""
            }
        if "scenes" not in script:
            script["scenes"] = []
        if "characters" not in script:
            script["characters"] = []
        if "props" not in script:
            script["props"] = []

        # 为每个场景添加 scene_id 并强制把过短的时长 clamp 到 4（Seedance 最低 4 秒）
        for i, scene in enumerate(script["scenes"]):
            if "scene_id" not in scene:
                scene["scene_id"] = f"S{i+1:02d}"
            ds = scene.get("duration_seconds", 5)
            try:
                ds = int(round(float(ds)))
            except (TypeError, ValueError):
                ds = 5
            scene["duration_seconds"] = max(4, ds)

        # 0.7.0 #2 兜底：episode 剧本如果各场景时长之和远低于计划总时长（LLM 常把"场景时长"
        # 误当成"镜头时长"写小），按比例放大，使总和 ≈ 计划时长 → storyboard 才能据此拆出足量镜头填满整集。
        # 场景内容是真实节拍，放大不是注水：每个场景后面会被拆成多个镜头。
        outline = task.get("outline") or {}
        if isinstance(outline, dict) and outline.get("_episode_index") and script["scenes"]:
            target_sec = _parse_target_seconds(task.get("duration"))
            cur_sum = sum(int(s.get("duration_seconds") or 0) for s in script["scenes"])
            if target_sec and cur_sum and cur_sum < target_sec * 0.7:
                factor = target_sec / cur_sum
                for s in script["scenes"]:
                    s["duration_seconds"] = max(8, min(120, round(int(s.get("duration_seconds") or 8) * factor)))
                logger.info(
                    "episode 剧本场景时长之和 %ds 远低于计划 %ds，按 ×%.2f 放大到 ≈%ds（共 %d 场景）",
                    cur_sum, target_sec, factor,
                    sum(s["duration_seconds"] for s in script["scenes"]), len(script["scenes"]),
                )

        # 为每个角色添加 character_id
        for i, char in enumerate(script["characters"]):
            if "character_id" not in char:
                char["character_id"] = f"C{i+1:02d}"

        # 0.6.4: 关键道具 props_id 补全 + 上限 5 个
        if isinstance(script["props"], list):
            if len(script["props"]) > 5:
                script["props"] = script["props"][:5]
            for i, prop in enumerate(script["props"]):
                if isinstance(prop, dict) and "prop_id" not in prop:
                    prop["prop_id"] = f"P{i+1:02d}"
        else:
            script["props"] = []

        return script

    # ── 进阶模式聊天系统 prompt（override BaseAgent 默认）──

    def _chat_system_prompt(self) -> str:
        return """你是 Octopus 的编剧 Agent，创作者要你修改已生成的剧本。

## 你能改的内容
- script 节点的 params 字段：topic / style / duration / mood / outline 等输入参数
- 改了之后通常 should_rerun=true（重新生成整本剧本）—— 但要警告创作者：剧本变了，下游美术/分镜会受影响

## 注意
- 如果项目已经进入美术或更下游阶段，**慎重**改剧本：reply 里提醒"这会废掉已生成的美术/分镜节点"
- 创作者只是想小修个别场景描述、对白 → 可以建议他直接点 script 节点的编辑面板手动改，而不是整本重生成

## 输出格式（严格 JSON）
{
  "reply": "编剧式简短回应（≤80 字）",
  "node_updates": [
    {"node_id": "script节点id", "params_patch": {"topic": "新主题"}, "should_rerun": true, "reason": "..."}
  ]
}
"""
