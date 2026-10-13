"""
美术 Agent — 建立视频项目的视觉世界观、角色形象、场景风格、道具设定和视觉一致性

职责：
1. 根据剧本和导演意图设计整体视觉风格
2. 生成角色设定，包括外貌、服装、气质、年龄、身份、材质细节
3. 生成场景设定，包括空间、时代、色彩、光影、氛围、建筑和道具
4. 为文生图、图生图、角色物料生成、场景生成模块提供高质量 prompt
5. 维护角色和场景的一致性，生成可复用的视觉资产描述
"""

import json
from typing import Any, Dict, List

from app.agents.base.base_agent import BaseAgent
from app.core.agent_runtime import cloud_method  # 护城河：cloud_client 下整体路由云端


# ═══════════════════════════════════════════════════════
# 老白：美术 Agent 的 system prompt
# ═══════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════
# 0.7.0 轮 1：Episode 模式 system prompt — 在 master 基础上"细化"
# ═══════════════════════════════════════════════════════
EPISODE_ART_DIRECTOR_SYSTEM_PROMPT = """你是漫剧的本集美术指导。总美术（master）已经设计好了：
- 全部角色（长相、体型、基础形象）
- 全部场景（基础布局、材质）
- 全部关键道具（基础形态）

你的任务**不是重做**，而是在 master 基础上，**为本集剧本做细化**：

## 1. 角色 outfits（服装系统）
- 列出本集**真正出场**的角色（不出场的不要列）
- 每个角色给出本集穿的 1-N 套衣服（同一集可能换装：日常 → 出门正装 → 居家睡袍）
- 每件衣服必须给：outfit_id / name / when（剧本第几段穿） / outfit_prompt（**中文**，只描述衣服+发型微调）
- outfit_prompt **不要**重描述脸/体型/身材（已经在 master 角色立绘里），只写衣服本身：款式、颜色、材质、领口袖口细节、配饰
- 例：「米白色及踝丝绸睡袍，深 V 领，宽松腰带，肩带滑落一边，居家氛围」

## 2. 场景 variations（场景变化）
- master 已有的场景，在本集如果出现，给出**本集独有的变化**：
  - time_of_day（白天/黄昏/深夜/凌晨）
  - lighting（"窗外暴雨打雷，室内一盏暖黄台灯"）
  - camera_angle（"低角度仰拍" / "俯视全景"）
  - 任何剧情驱动的环境变化（如"客厅被打砸一片狼藉"）
- 新增 master 没设计但本集需要的场景（如"医院走廊"），按 master 同样规范输出 image_prompt
- **同一地点只出一个场景条目（重要）**：一个 location（如"酒店包厢"）本集不管出现在几场戏，**只输出一条 scenes_in_episode**，把这个地点需要的机位全放进它的 `camera_setups`；**绝不要为同一个房间拆成多个场景变体**（否则画布会出现好几个一模一样的场景、机位还被打散够不着）。只有白天↔深夜、完好↔被砸这类**剧情性巨变**才拆成两条。
- **机位板 camera_setups（重要）**：场景的 image_prompt 是「建立镜」（全景/主视角，board 0）。对**戏份重、要反复出现（多个分镜、有正反打/对话）**的场景，额外提 2–4 个**机位板**，让不同分镜有不同机位、不再复用同一张图（治「换角色不换景」）。**机位之间必须真不同**（机位高度/朝向/景别各异，别提两个近义的反打）：
  - 每个机位给：camera_id / camera_name（**简短**，供分镜用 `@场景#机位` 引用，如"对面反打"/"门口视角"/"桌面近景"）/ camera_prompt（**只写镜头角度/取景/朝向的增量**，如"镜头移到圆桌对面反打、看向主位；同一房间、同样家具陈设与光线") / when_to_use（何时用）
  - camera_prompt **不要**重描述整个房间（建立镜已定），只写"换到哪个机位、看向哪、取景多大"；保持与建立镜同一空间/陈设/光线
  - 简单过渡镜、一次性出现的场景 → camera_setups 留空数组 []

## 3. 道具 states（道具状态）
- master 已有的道具，本集如果有**剧情变化**（如戒指丢失/磨损/染血/被砸碎），列出 state
- state_prompt 只描写**变化的差异**，不重复整体形状（已在 master 道具图里）
- 例：「金属表面有 3 道明显划痕，宝石松动可见缝隙，整体磨损暗淡」

## 输出 JSON schema（严格）
```json
{
  "episode_summary": "1-2 句概括本集视觉重点",
  "characters_in_episode": [
    {
      "master_character_id": "C01",       // master 已有的角色引用其 id；**master 没有但本集剧本出现的新角色填 null**
      "name": "林悦",
      "appearance_notes": "本集情绪/状态变化（如：疲惫的眼神，黑眼圈）",
      "image_prompt": "仅新角色(master_character_id=null)必填：完整外形——脸型/发型/体型/年龄/气质，纯白背景无场景",
      "outfits": [
        {
          "outfit_id": "C01_O01",
          "name": "深夜睡袍",
          "when": "开场到第2场",
          "outfit_prompt": "中文 prompt — 只描写衣服本身",
          "negative_prompt": "其他风格的衣服，配饰，发型大变"
        }
      ]
    }
  ],
  "scenes_in_episode": [
    {
      "master_scene_id": "S01",            // 引用 master；新场景填 null
      "location": "客厅",                    // master 没有的就自己起名
      "variation_id": "S01_V01",
      "variation_name": "深夜独处",
      "time_of_day": "凌晨2点",
      "lighting": "单盏暖黄台灯 + 月光透过纱帘",
      "camera_angle": "中景平视",
      "image_prompt": "中文 — 完整描述本集这一刻的环境（含光线/时间），空场景无人物",
      "camera_setups": [
        {
          "camera_id": "S01_CAM01",
          "camera_name": "对面反打",
          "camera_prompt": "镜头移到圆桌对面反打，看向主位方向；同一房间、同样家具陈设与光线，空场景",
          "when_to_use": "反打/对话另一方视角的分镜"
        }
      ],
      "negative_prompt": "人，人物，角色，动物"
    }
  ],
  "props_in_episode": [
    {
      "master_prop_id": "P01",             // 引用 master；新道具填 null
      "name": "婚戒",
      "state_id": "P01_S01",
      "state_name": "磨损版",
      "state_description": "本集情节让道具产生的变化（一句话）",
      "image_prompt": "中文 — 单道具特写，纯白背景，体现变化",
      "negative_prompt": "人，场景，其他道具"
    }
  ],
  "notes": "其他需要让下游分镜/视频导演知道的（如：本集光线整体偏冷蓝）"
}
```

## 重要约束
- outfit_id / variation_id / state_id 命名约定：`<masterId>_<O|V|S><序号>`
- **只列本集真正用到的**，不要把 master 全部资产再列一遍
- 不要修改 master 资产本身（你看不到也改不了 master 图）
- 引用 master 时填 master_character_id / master_scene_id / master_prop_id；**master 没有但本集剧本出现的新角色/场景/道具填 null 并给完整 image_prompt**（角色的 image_prompt 要含脸型/发型/体型/年龄）
- 本集出场的每个角色都要覆盖：master 里有的引用其 id 做 outfit 细化，master 里没有的（新配角）填 null 新建，**不要漏掉本集剧本里出现的角色**
- 不要输出 JSON 之外的内容"""


ART_DIRECTOR_SYSTEM_PROMPT = """你是美术导演，输出JSON格式的视觉设计方案。

**重要约定（0.6.3）**：所有 image_prompt 用**中文**撰写（智谱 glm-image 和火山 Seedream 都是国产模型，对中文理解更精准）。
- 主体描述（外观、表情、姿态、材质细节、氛围、情绪、世界观）→ **中文**
- 技术参数（白底/无场景/工作室灯光/构图等约束、风格关键词）→ 中英文混用，后端会自动追加双语后缀

关键约束（必须遵守）：

1) 角色 subject_type（重要，决定后续视频生成是否需要规避审核）：
   - 必须从以下枚举中选一个：human（真人/写实人形角色）/ animal（动物）/ creature（奇幻/异形生物）/ mech（机械/机器人）/ object（拟人化物体）
   - 卡通人、二次元角色、Q 版人形、像素人——只要保留人形面孔，仍然标 human

2) 角色 image_prompt（**中文为主**）：
   - 只描绘**单个角色本身**（外观、表情、姿态、材质细节）
   - 干净的白底或纯色背景：用中文写"纯白色背景，工作室灯光，无任何场景"
   - 不要包含任何场景元素、道具、其他角色、地点描述
   - 镜头：写"全身镜头"或"半身肖像"
   - 按剧本要求自由选择风格（写实电影感 / 风格化 3D / 二次元 / 油画感 等），默认偏向"写实电影感"
   - 例子（animal）：一只毛茸茸的橘色虎斑小猫咪，金色琥珀大眼睛，柔软毛发质感，安静坐姿，全身镜头，纯白色背景，工作室灯光，无任何场景，写实风格，8K 高细节
   - 例子（human）：一个齐肩黑发的年轻女孩，温暖笑容，米白色休闲衬衫，全身镜头，纯白色背景，工作室灯光，无任何场景，写实电影感，柔和自然光，8K 高细节

3) 场景 image_prompt（**中文为主**）：
   - 只描绘**环境、空间、光影**，**禁止出现任何角色/人/动物**；可有环境性道具（家具、植物）但不要孤立特写
   - 必须明确"空场景，无人物，无角色，无动物"
   - **空间尺度服务剧情**：按剧本里这个场景实际发生的事，只生成**必要的空间范围**——剧情只在床边/桌前/门口，就画那个局部，别无脑铺成宏大全景/整栋建筑/空旷大场地（失真又费算力）；确需大空间的剧情（广场、远景）才画大
   - 例子：一间现代中式公寓客厅，米色亚麻沙发，散落的猫玩具，温暖的金色黄昏光从窗帘缝隙洒入，空场景，无人物，无角色，无动物，写实风格，电影感
   - **机位板 camera_setups（0.7.5）**：image_prompt 是这个场景的「建立镜」（主视角，board 0）。对**戏份重、会有多个分镜反复出现（对话/正反打）**的场景，额外给 2–3 个机位板（camera_id / camera_name 简短如"对面反打" / camera_prompt 只写镜头角度取景朝向的增量、同房间同陈设同光线 / when_to_use），供分镜用 `@场景#机位` 引用，让不同镜头有不同机位；简单/一次性场景 camera_setups 留空数组

4) 关键道具 props image_prompt（**中文为主**，0.6.4 新增）：
   - **单道具特写**，**纯白色背景**，**无场景、无人物、无角色**，产品摄影感
   - 必须明确"独立道具，纯白色背景，无场景，无人物，无角色，无其他道具"
   - 镜头：写"产品特写"或"道具特写镜头"
   - 例子：一把黑铁长剑，剑身镶嵌一颗蓝宝石，剑柄缠暗红丝带，长度约90cm，独立道具，纯白色背景，无场景，无人物，无角色，无其他道具，工作室灯光，产品特写，写实金属质感，8K 高细节

**props 兜底识别**：如果 script.props 为空 / 缺失，但你扫描剧本场景描述发现有跨场景重复 / 单场景高频出现的物件（如反复提到的玩偶、武器、信物等），**主动补 1-3 个关键道具**到 props 数组（按编剧规范输出 prop_id=P01..P0N + name + description + use_case + image_prompt + negative_prompt）。**宁可多识别一个，不要给空数组**。

5) negative_prompt（**中文**）：
   - 角色：列出要避免的场景元素（"背景场景，环境，道具，多个角色"）
   - 场景：列出要避免的角色元素（"人，人物，角色，动物，猫，狗"）
   - 道具：列出要避免的角色/场景/其他道具元素（"人，人物，角色，场景，背景物体，多个道具"）"""


def _normalize_camera_setups(raw: Any, scene_id_prefix: str) -> List[Dict[str, Any]]:
    """0.7.5 场景机位板：规范化 camera_setups[]。
    建立镜(board 0)= 场景 image_prompt 本身、不在此列；这里只是**额外机位**。
    每个机位必须有 camera_name + camera_prompt，否则丢弃（防生成空板）。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for k, cam in enumerate(raw):
        if not isinstance(cam, dict):
            continue
        name = str(cam.get("camera_name") or "").strip()
        cprompt = str(cam.get("camera_prompt") or "").strip()
        if not name or not cprompt:
            continue
        out.append({
            "camera_id": str(cam.get("camera_id") or f"{scene_id_prefix}_CAM{k + 1:02d}").strip(),
            "camera_name": name,
            "camera_prompt": cprompt,
            "when_to_use": str(cam.get("when_to_use") or "").strip(),
        })
    return out


class ArtDirectorAgent(BaseAgent):
    """
    美术 Agent
    
    使用方式：
        artist = ArtDirectorAgent()
        result = await artist.run({
            "script": {...},  # 来自编剧 Agent 的剧本
            "style": "写实可爱",
            "mood": "温馨"
        })
    """
    
    name = "art_director"
    description = "美术设定 Agent"
    system_prompt = ART_DIRECTOR_SYSTEM_PROMPT
    
    @cloud_method
    async def execute(self, task: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        美术核心逻辑

        Args:
            task: {
                "mode": "master"（默认）或 "episode"
                "script": 剧本内容或摘要
                # mode='episode' 额外需要：
                "master_shared_assets": { characters, scenes, props, ... } —— 总美术
                "episode_outline": 本集大纲

                # 通用：
                "style", "mood", "characters", "scenes", "props", "reference_images"
            }
            context: 上游 Agent 的输出
        """
        # 0.7.0 轮 1：episode 模式分支（不走 master 的设计流，而是在 master 基础上细化）
        mode = (task.get("mode") or "master").lower()
        if mode == "episode":
            return await self._execute_episode(task)

        # 获取剧本（优先从 context 获取完整剧本）
        script = task.get("script", {})
        if context and "script" in context:
            script = context["script"]

        style = task.get("style", "写实")
        mood = task.get("mood", "温馨")

        # 构建提示
        prompt = self._build_prompt(task, script)
        
        # 调用 LLM（不用 system prompt，避免 Kimi Code API 超时）
        # 把所有要求合并到 user prompt 中
        full_prompt = f"{self.system_prompt}\n\n{prompt}" if self.system_prompt else prompt
        response = await self.llm_generate(
            prompt=full_prompt,
            system=None,  # 不用 system prompt
            json_mode=False,
            temperature=0.6
        )
        
        # 解析 JSON（从 markdown 代码块中提取）
        try:
            # 尝试直接解析
            art_direction = json.loads(response)
        except json.JSONDecodeError:
            # 尝试从 markdown 代码块中提取
            import re
            json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', response, re.DOTALL)
            if json_match:
                try:
                    art_direction = json.loads(json_match.group(1))
                except json.JSONDecodeError:
                    return {
                        "error": "美术设定生成失败 - JSON 解析错误",
                        "raw_response": response[:500]
                    }
            else:
                return {
                    "error": "美术设定生成失败 - 未找到 JSON",
                    "raw_response": response[:500]
                }
        
        # 规范化
        art_direction = self._normalize(art_direction, task)
        
        return {
            "art_direction": art_direction,
            "summary": {
                "style_keywords": art_direction.get("visual_style", {}).get("keywords", []),
                "character_count": len(art_direction.get("characters", [])),
                "scene_count": len(art_direction.get("scenes", [])),
                "prop_count": len(art_direction.get("props", []) or []),
            }
        }
    
    # ═══════════════════════════════════════════════════════
    # 0.7.0 轮 1：Episode 模式执行 — 在 master 基础上细化
    # ═══════════════════════════════════════════════════════
    async def _execute_episode(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Episode 模式：拿 master shared_assets + 本集剧本，输出
        characters_in_episode (含 outfits) / scenes_in_episode (含 light/angle/time)
        / props_in_episode (含 state) 三块结构。
        """
        import json as _json
        import logging as _lg
        _logger = _lg.getLogger(__name__)

        script = task.get("script") or {}
        master = task.get("master_shared_assets") or {}
        episode_outline = task.get("episode_outline") or {}

        # 给 LLM 看的 master 简化清单（id + 名字 + 一句话）—— 节省 token
        master_chars_brief = [
            {
                "master_character_id": c.get("character_id") or c.get("name"),
                "name": c.get("name"),
                "appearance": (c.get("appearance") or c.get("prompt") or "")[:120],
                "subject_type": c.get("subject_type") or "human",
            }
            for c in (master.get("characters") or [])
        ]
        master_scenes_brief = [
            {
                "master_scene_id": s.get("scene_id") or s.get("location"),
                "location": s.get("location"),
                "environment": (s.get("environment") or s.get("prompt") or "")[:120],
            }
            for s in (master.get("scenes") or [])
        ]
        master_props_brief = [
            {
                "master_prop_id": p.get("prop_id") or p.get("name"),
                "name": p.get("name"),
                "use_case": p.get("use_case") or "",
                "description": (p.get("description") or p.get("prompt") or "")[:80],
            }
            for p in (master.get("props") or [])
        ]

        # 本集剧本——优先用 script.result.scenes/script_text，fallback 用 outline
        script_scenes = script.get("scenes") if isinstance(script, dict) else None
        script_text = script.get("script_text") if isinstance(script, dict) else None
        ep_title = episode_outline.get("title") or script.get("script_title") or ""
        ep_logline = episode_outline.get("logline") or ""

        user_prompt_parts = [
            f"## 本集：《{ep_title}》",
            f"logline: {ep_logline}",
            "",
            "## Master 角色清单（master 有的引用其 id 做 outfit 细化；master 没有但本集剧本点名的新角色，master_character_id 填 null + 给完整 image_prompt）",
            _json.dumps(master_chars_brief, ensure_ascii=False, indent=2)[:3000],
            "",
            "## Master 场景清单（本集复用 + 可新增）",
            _json.dumps(master_scenes_brief, ensure_ascii=False, indent=2)[:2000],
            "",
            "## Master 道具清单（本集 state 变化 + 可新增）",
            _json.dumps(master_props_brief, ensure_ascii=False, indent=2)[:1500],
            "",
        ]
        # 本集出场角色名单（来自分集拆解，可能含 master 没有的新配角）→ 让 LLM 逐个覆盖、别漏角色
        _ep_chars = episode_outline.get("characters") or []
        if _ep_chars:
            _ec = [{"name": c.get("name"), "role": c.get("role"),
                    "desc": (c.get("description") or c.get("appearance") or "")[:60]} for c in _ep_chars]
            user_prompt_parts += [
                "## 本集出场角色名单（**每个都要覆盖**：master 有的引用其 id，master 没有的填 null + image_prompt 新建）",
                _json.dumps(_ec, ensure_ascii=False, indent=2)[:2000],
                "",
            ]
        user_prompt_parts.append("## 本集剧本")
        if script_scenes:
            user_prompt_parts.append(_json.dumps(script_scenes, ensure_ascii=False, indent=2)[:6000])
        elif script_text:
            user_prompt_parts.append(str(script_text)[:6000])
        else:
            user_prompt_parts.append("(暂无详细剧本，请按 logline 推断)")
        user_prompt_parts.append("")
        user_prompt_parts.append("请按 system prompt 要求输出 JSON。")
        user_prompt = "\n".join(user_prompt_parts)

        try:
            response = await self.llm_generate(
                prompt=user_prompt,
                system=EPISODE_ART_DIRECTOR_SYSTEM_PROMPT,
                temperature=0.5,
                json_mode=True,
                max_tokens=8192,
            )
            data = self._safe_parse_json(response) or {}
        except Exception as e:
            _logger.exception("episode art LLM 失败: %s", e)
            data = {}

        # 规范化输出
        normed = self._normalize_episode(data, master)
        return {
            "art_direction": normed,
            "mode": "episode",
            "summary": {
                "characters_in_episode": len(normed.get("characters_in_episode") or []),
                "outfits_total": sum(
                    len(c.get("outfits") or [])
                    for c in (normed.get("characters_in_episode") or [])
                ),
                "scenes_in_episode": len(normed.get("scenes_in_episode") or []),
                "props_in_episode": len(normed.get("props_in_episode") or []),
            },
        }

    @staticmethod
    def _safe_parse_json(response: str):
        import json as _json
        import re
        if not response or not response.strip():
            return None
        try:
            return _json.loads(response)
        except _json.JSONDecodeError:
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", response, re.DOTALL)
            if m:
                try:
                    return _json.loads(m.group(1))
                except _json.JSONDecodeError:
                    return None
            return None

    @staticmethod
    def _normalize_episode(data: Dict[str, Any], master: Dict[str, Any]) -> Dict[str, Any]:
        """保底 episode art 输出字段，避免下游崩。"""
        if not isinstance(data, dict):
            data = {}
        chars = data.get("characters_in_episode")
        if not isinstance(chars, list):
            chars = []
        norm_chars = []
        for i, c in enumerate(chars):
            if not isinstance(c, dict):
                continue
            outfits_raw = c.get("outfits") or []
            outfits = []
            for j, o in enumerate(outfits_raw):
                if not isinstance(o, dict):
                    continue
                outfits.append({
                    "outfit_id": str(o.get("outfit_id") or f"O{j + 1:02d}").strip(),
                    "name": str(o.get("name") or f"装束 {j + 1}").strip(),
                    "when": str(o.get("when") or "").strip(),
                    "outfit_prompt": str(o.get("outfit_prompt") or "").strip(),
                    "negative_prompt": str(o.get("negative_prompt") or "").strip(),
                })
            norm_chars.append({
                "master_character_id": str(c.get("master_character_id") or "").strip(),
                "name": str(c.get("name") or f"角色 {i + 1}").strip(),
                "appearance_notes": str(c.get("appearance_notes") or "").strip(),
                # 0.7.3：新角色(master_character_id 为空)的完整外形 prompt，下游走 t2i 时用（master 角色可空）
                "image_prompt": str(c.get("image_prompt") or "").strip(),
                "outfits": outfits,
            })

        scenes = data.get("scenes_in_episode")
        if not isinstance(scenes, list):
            scenes = []
        norm_scenes = []
        for i, s in enumerate(scenes):
            if not isinstance(s, dict):
                continue
            _vid = str(s.get("variation_id") or f"V{i + 1:02d}").strip()
            norm_scenes.append({
                "master_scene_id": (str(s.get("master_scene_id")).strip() if s.get("master_scene_id") else ""),
                "location": str(s.get("location") or f"场景 {i + 1}").strip(),
                "variation_id": _vid,
                "variation_name": str(s.get("variation_name") or "").strip(),
                "time_of_day": str(s.get("time_of_day") or "").strip(),
                "lighting": str(s.get("lighting") or "").strip(),
                "camera_angle": str(s.get("camera_angle") or "").strip(),
                "image_prompt": str(s.get("image_prompt") or "").strip(),
                # 0.7.5 场景机位板：建立镜=image_prompt；这里是额外机位（供 @场景#机位 引用）
                "camera_setups": _normalize_camera_setups(s.get("camera_setups"), _vid),
                "negative_prompt": str(s.get("negative_prompt") or "人，人物，角色，动物").strip(),
            })

        # 0.7.5.1 清理：同一 location 合并成一个场景资产（治"一个包厢被拆成 3 个同质节点、机位板还被打散"）。
        # 把同地点各条目的 camera_setups 汇到第一条、按 camera_name 去重（顺带去掉近义/重复机位）；
        # 其余字段留第一条（建立镜）。日夜/完好↔被砸这类剧情性巨变本应分开，但当前按 location 合并根治同质化。
        merged_scenes: List[Dict[str, Any]] = []
        by_loc: Dict[str, Dict[str, Any]] = {}
        for s in norm_scenes:
            loc = s.get("location") or ""
            base = by_loc.get(loc)
            if base is None:
                by_loc[loc] = s
                merged_scenes.append(s)
            else:
                seen = {c.get("camera_name") for c in (base.get("camera_setups") or [])}
                for c in (s.get("camera_setups") or []):
                    if c.get("camera_name") and c.get("camera_name") not in seen:
                        base.setdefault("camera_setups", []).append(c)
                        seen.add(c.get("camera_name"))
        if len(merged_scenes) != len(norm_scenes):
            norm_scenes = merged_scenes

        props = data.get("props_in_episode")
        if not isinstance(props, list):
            props = []
        norm_props = []
        for i, p in enumerate(props):
            if not isinstance(p, dict):
                continue
            norm_props.append({
                "master_prop_id": (str(p.get("master_prop_id")).strip() if p.get("master_prop_id") else ""),
                "name": str(p.get("name") or f"道具 {i + 1}").strip(),
                "state_id": str(p.get("state_id") or f"ST{i + 1:02d}").strip(),
                "state_name": str(p.get("state_name") or "").strip(),
                "state_description": str(p.get("state_description") or "").strip(),
                "image_prompt": str(p.get("image_prompt") or "").strip(),
                "negative_prompt": str(p.get("negative_prompt") or "人，场景，其他道具").strip(),
            })

        return {
            "episode_summary": str(data.get("episode_summary") or "").strip(),
            "characters_in_episode": norm_chars,
            "scenes_in_episode": norm_scenes,
            "props_in_episode": norm_props,
            "notes": str(data.get("notes") or "").strip(),
        }

    def _build_prompt(self, task: Dict, script: Any) -> str:
        """构建创作提示（极简版，避免 Kimi API 超时）"""
        # 🔴 字段名要兼容两套上游（2026-08-26 撞出来的）：
        #    编剧产出的场景是 {location, scene_summary}，MV 导演产出的是 {name, description}。
        #    只读一套的话，另一套会全渲染成「场景 未知：」——提示词里等于什么信息都没给，
        #    模型只能自己编。实测 MV 剧本写的是「山野间采摘果实」，美术出的却是
        #    卧室/学校天台/老街区小巷，整套素材跟歌完全无关，而且没有任何报错。
        _s = script if isinstance(script, dict) else {}
        script_title = _s.get("script_title") or _s.get("title") or "未命名"
        # 故事梗概：原来只给标题，信息太少。MV 剧本的核心信息全在 logline/concept 里。
        logline = _s.get("logline") or _s.get("concept") or ""
        characters = task.get("characters", [])
        scenes = task.get("scenes", [])
        props = task.get("props", []) or []
        if isinstance(script, dict) and not props:
            # 兜底：script 里有 props 但 task 没传
            props = script.get("props", []) or []

        lines = [
            f"为剧本《{script_title}》设计视觉风格，输出JSON。",
            f"风格：{task.get('style', '写实')}，情绪：{task.get('mood', '温馨')}",
        ]
        if logline:
            lines.append(f"故事：{logline[:200]}")
        lines.append("")
        # 0.7.3 修复"角色 6→4 丢配角"：原 [:4] 是防 Kimi 超时的硬切片，机械砍掉第 5/6 个角色(非按重要度)。
        # 放开到 [:8]（对齐 director ≤8 角色上限）——描述都截断到 50 字，8 个也不长、不会退回超时。
        # master 造齐全部角色，分集才能按 character_indices 引用到本集出场的配角。
        for c in characters[:8]:
            # MV 导演给的是 appearance/personality，编剧给的是 description
            _cd = c.get("description") or c.get("appearance") or ""
            lines.append(f"角色 {c.get('name', '未知')}：{_cd[:50]}")
        for s in scenes[:8]:
            _sn = s.get("location") or s.get("name") or "未知"
            _sd = s.get("scene_summary") or s.get("description") or ""
            lines.append(f"场景 {_sn}：{_sd[:50]}（若戏份重/多镜反复出现，给 2-3 个机位板 camera_setups）")
        for p in props[:8]:
            lines.append(f"道具 {p.get('name', '未知')}：{p.get('description', '')[:50]}（用途：{(p.get('use_case') or '')[:30]}）")

        lines.extend([
            "",
            "输出 JSON 字段：",
            "- visual_style: { keywords[], color_palette[], lighting, texture, camera_feel }",
            "- characters[]: { character_id, name, identity, appearance, clothing, recognizable_features,",
            "                  subject_type(必填，枚举: human|animal|creature|mech|object),",
            "                  image_prompt(**中文**，单角色，纯白背景，无场景；按剧本风格自由表达，可写实可风格化),",
            "                  negative_prompt(**中文**，列出要避免的场景/其他角色等) }",
            "- scenes[]: { scene_id, location, environment, props, lighting, mood,",
            "             image_prompt(**中文**，纯环境，无任何角色/人/动物),",
            "             camera_setups[](0.7.5，戏份重/多镜反复出现的场景给2-3个额外机位板；简单场景留空[]):",
            "               { camera_id, camera_name(简短，如\"对面反打\"), camera_prompt(**中文**，只写镜头角度/取景/朝向增量，同房间同陈设同光线，空场景), when_to_use },",
            "             negative_prompt(**中文**，列出要避免的人物/角色) }",
            "- props[]: { prop_id, name, description, use_case,",
            "            image_prompt(**中文**，单道具特写，纯白背景，无场景无人物，产品摄影感),",
            "            negative_prompt(**中文**，列出要避免的角色/场景/其他道具) }",
            "- style_guide: { consistency_rules, notes }",
            "",
            "硬性要求（极重要，必须遵守）：",
            '1) 每个角色的 image_prompt 必须包含中文"纯白色背景，工作室灯光，无任何场景"（后端会自动追加双语后缀，但 LLM 也要明确写出）',
            '2) 每个场景的 image_prompt 必须包含中文"空场景，无人物，无角色，无动物"',
            '3) 每个道具的 image_prompt 必须包含中文"独立道具，纯白色背景，无场景，无人物，无角色"',
            "4) 角色提示词中不要出现场景/地点；场景提示词中不要出现任何角色/动物/人；道具提示词中不要出现角色/场景/其他道具",
            "5) 不要侵权内容、真实人物、现存IP",
            "6) 角色的 subject_type 必须填写（影响下游链路）",
            "7) **整段 image_prompt 必须是中文为主**，国产模型（智谱 glm-image / 火山 Seedream）对中文理解更精准；可少量保留 cinematic / 8K 等通用英文风格词",
            "8) props 兜底：若 script.props 已有，逐个为它们给设计；若 script.props 为空但你能从剧本场景描述里识别出反复出现的物件（玩偶/武器/信物等），**主动补 1-3 个关键道具**到 props 数组（带 prop_id 从 P01 起编号）。**绝不输出空 props**除非剧情确实无任何可识别物件",
            "9) **场景空间尺度**：scene 的 image_prompt 只生成剧情实际需要的空间范围（局部角落/单间/必要外景），不要无脑放大成宏大全景或整栋建筑；scene 的 negative_prompt 加入「过度宽广的场景, 多余的宏大全景, 无关建筑, 空旷大场地」(excessive wide landscape, bloated background, unrelated architecture)",
            "10) **上面列出的角色和场景必须逐个保留、名称原样沿用**，不要新增、替换或改名。"
            "你的工作是给它们做视觉设计，不是重新编一套。下游分镜按名字引用这些场景，改了名整条链就对不上。"
            "（props 例外，见第 8 条，允许主动补）",
        ])

        return "\n".join(lines)
    
    def _normalize(self, art: Dict, task: Dict) -> Dict:
        """规范化输出"""
        if "visual_style" not in art:
            art["visual_style"] = {
                "keywords": [task.get("style", "写实")],
                "color_palette": [],
                "lighting": "自然光",
                "texture": "平滑",
                "camera_feel": "电影感"
            }
        if "characters" not in art:
            art["characters"] = []
        if "scenes" not in art:
            art["scenes"] = []
        # 0.7.5 场景机位板：规范化每个场景的 camera_setups（建立镜=image_prompt，这里是额外机位）
        for i, sc in enumerate(art.get("scenes") or []):
            if isinstance(sc, dict):
                _sid = str(sc.get("scene_id") or f"S{i + 1:02d}").strip()
                sc["camera_setups"] = _normalize_camera_setups(sc.get("camera_setups"), _sid)
        if "props" not in art or not isinstance(art["props"], list):
            art["props"] = []
        # 0.6.4: prop_id 补全
        for i, prop in enumerate(art.get("props") or []):
            if isinstance(prop, dict) and "prop_id" not in prop:
                prop["prop_id"] = f"P{i+1:02d}"
        if "style_guide" not in art:
            art["style_guide"] = {
                "consistency_rules": [],
                "reference_images": [],
                "notes": ""
            }

        return art

    # ── 进阶模式聊天系统 prompt（override BaseAgent 默认）──

    def _chat_system_prompt(self) -> str:
        return """你是 Octopus 的美术指导 Agent，创作者要你修改已生成的角色立绘 / 场景图。

## 你能改的节点
- type="result" 且 result_type="character" 的子节点 — 角色立绘
- type="result" 且 result_type="scene" 的子节点 — 场景背景图

## 怎么改
- 改某个角色的视觉描述 → params_patch={"prompt": "新的完整英文 prompt"}，should_rerun=true
  - 新 prompt 应当**保留原有角色基本特征**（如猫的品种、性别），只调用户提到的部分（眼睛大小 / 颜色 / 服装 / 姿势 / 风格等）
  - 建议在 prompt 里加上你的美术判断，让画面更出彩
- 改场景图同理：保留场景基本元素，只调用户要求的（如"加点夕阳光"/"换成下雨天"）
- 如果创作者只是问"这张图怎么样"等闲聊 → node_updates 空，reply 给点评

## 注意
- params_patch 里的 prompt 必须是**完整英文 prompt**（不是 diff），后端会替换原有的 prompt 字段
- 不要凭空给中文 prompt — Zhipu glm-image 偏好英文 + 风格修饰词组合

## 关于创作者上传的参考图（重要）
当 user prompt 里有"创作者附件（参考图）"段落时：
- 创作者多半是想"按这个样子重画" → 在 params_patch 中加入 `reference_image_urls`: 数组，
  把附件段落里列出的 URL 全部塞进去（最多 5 张）
- should_rerun=true
- 后端检测到 reference_image_urls 非空时，会走 image_to_image 多图融合（ARK Seedream），
  输出风格会贴近参考图。智谱 glm-image 不支持多图参考，所以必须走 ARK 这条路

## 关于创作者上传的参考图（Vision 模式 — 重要）

**你能直接看到创作者上传的参考图**。系统通过多模态 API 把图片以视觉 content 形式传给你，
你能识别图里的颜色、材质、形状、纹理、logo、文字、人物、构图等所有视觉元素。

### Seedream 4.0 多图融合 — Photo 编号约定（关键）

后端会自动把"原角色立绘"作为 **Photo 1**（角色锚点），创作者上传的参考图作为 **Photo 2 / Photo 3 / ...**（衣服/物品/配饰素材）。

所以你的英文 prompt **必须使用 Seedream 官方推荐的格式**：

```
Dress the person in Photo 1 in the [描述：black ripstop nylon wingsuit with white "ACME" chest logo,
full-body fabric panels extending from arms to legs, full-length front zipper] from Photo 2.
Preserve the face, hair, and body identity from Photo 1.
Edit clothing only — do not change the character's pose, face, hair, or body proportions.
Isolated on pure white background, character reference sheet, full body shot.
```

**关键短语（每条都不能少）**：
- `Dress the person in Photo 1 in [...] from Photo 2` ← imperative，告诉 Seedream 这是 outfit transfer
- `Preserve the face, hair, and body identity from Photo 1` ← 防 Seedream 把模特脸搬过来
- `Edit clothing only` ← 防 Seedream 重新生成整张图（这是关键 — 没这句它会按你的 prompt 重画）
- 描述衣服时**写出你实际看到的视觉细节**（颜色 / 材质 / logo 文字 / 形状），不要凭空脑补

### 多图组合（Photo 1 = 角色，Photo 2 = 帽子，Photo 3 = 衣服）
```
Edit the person in Photo 1: dress them in the [hat description] from Photo 2 and the [jacket description] from Photo 3.
Preserve face, hair, and body identity from Photo 1.
Edit only the hat and jacket — do not change anything else.
Isolated on pure white background, character reference sheet.
```

### 场景重组（Photo 1 = 原场景，Photo 2 = 要嵌入的物体）
```
Edit the scene in Photo 1: add the [building description] from Photo 2 placed at [位置：背景远景 / 中景右侧].
Preserve original scene composition, lighting, atmosphere from Photo 1.
Only add the new building — do not modify the rest of the scene.
No characters in the scene.
```

### 场景换氛围（不需要参考图）
```
Edit the scene in Photo 1: same composition and layout, change atmosphere to [night with neon lights / golden hour / rainy day].
Do not add or remove any objects.
```

### 关键原则总结

- ✅ **始终使用 `Photo 1 / Photo 2 / Photo 3` 这套术语**（Seedream 官方训练用语）
- ✅ **使用 imperative 动词**：`Dress`、`Edit`、`Add`、`Replace`，不要 `wearing` 这种状态描述
- ✅ **必须有 `Edit ... only`** 限定 scope，明确告诉 Seedream 哪些不变
- ✅ **必须有 `Preserve [face/hair/identity/composition] from Photo 1`** 防原图丢失
- ✅ 描述参考物时**基于你实际看到的视觉细节**（颜色/材质/文字/形状）
- ❌ 不要写 `wearing the X from reference image 1` 这种状态描述
- ❌ 不要写 `verbatim / copy ... details` 这种笼统短语
- ❌ 不要凭空脑补未在图中出现的元素

### 输出前自查

- 用了 `Dress / Edit / Add` 这种 imperative 动词吗？
- 用了 `Photo 1 / Photo 2` 这套术语吗？
- 有 `Edit ... only` 和 `Preserve ... from Photo 1` 两条护栏吗？
- 描述衣服/物体时是不是基于真实看到的视觉细节？

## 注意
- params_patch 里的 prompt 必须是**完整英文 prompt**（不是 diff），后端会替换原有的 prompt 字段
- 不要凭空给中文 prompt — Zhipu glm-image 偏好英文 + 风格修饰词组合
- 用法 B 是最难的，宁愿写 100 词长 prompt 也不要含糊。明确说"只换 X，其他不变"

## 输出格式（严格 JSON）
{
  "reply": "美术指导式简短回应（≤80 字）",
  "node_updates": [
    {"node_id": "角色/场景节点id",
     "params_patch": {
       "prompt": "完整新英文 prompt",
       "reference_image_urls": ["/output/uploads/xxx.png", ...]  // 有参考图时必填
     },
     "should_rerun": true,
     "reason": "..."
    }
  ]
}
"""
