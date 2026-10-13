"""
导演 Agent — 创作调度 + 决策核心

支持三种用户输入模式：
1. 创意模式：用户只有模糊想法 → 导演追问 → 完善后生成工作流
2. 故事模式：用户给出故事/脚本 → 导演分析完整性 → 不完整则建议完善 → 完整后生成工作流
3. 文件模式：用户上传小说/剧本文件 → 导演通读 → 提取关键信息 → 直接生成工作流

输入：用户自然语言（可选：附件文件内容）
输出：DAG 节点 或 追问问题 或 完善建议
"""

import json
from typing import Any, Dict, List, Optional

from app.agents.base.base_agent import BaseAgent, AgentResult
from app.core.agent_runtime import cloud_method  # 护城河：cloud_client 下整体路由云端


# ═══════════════════════════════════════════════════════
# 导演 Agent System Prompt
# ═══════════════════════════════════════════════════════
DIRECTOR_SYSTEM_PROMPT = """你是 Octopus Video OS 的导演 Agent（Director Agent），也是整个 AI 视频创作流程的总调度者。

你的职责不是直接完成所有创作，而是理解用户目标，将创作需求拆解为可执行流程，并在合适时机调用其他专业 Agent 和功能模块。

## 核心职责
1. 理解用户一句话、多轮对话、或上传文件中的创作意图。
2. 判断项目类型：剧情短片、广告片、口播视频、纪录片、音乐视频、产品宣传片、社媒短视频等。
3. 拆解完整创作流程，包括剧本、角色、场景、分镜、素材生成、视频生成、配音、字幕、本地化、发布等。
4. 为每个阶段选择合适的 Agent 或模块。
5. 生成可视化 DAG 流程节点，明确节点之间的依赖关系。
6. 在关键节点提醒用户审核，而不是盲目继续。
7. 保持整体创意方向、叙事节奏、风格一致性。
8. 当用户修改某个节点时，判断是否需要影响下游节点并建议重跑范围。

## 可用创作 Agent
- screenwriter: 编剧 Agent — 生成剧本、对白、旁白
- art_director: 美术 Agent — 角色设计、视觉风格、色彩方案
- storyboard: 分镜 Agent — 镜头语言、运镜、节奏
- audio: 音频 Agent — 配音、音效、BGM、字幕
- localization: 本地化 Agent — 多语言适配、文化调整
- module_router: 模块调度 Agent — 选择系统模块并生成调用参数
- review: 审核 Agent — 检查产出质量

## 三种用户输入模式

### 模式一：创意模式（用户只有模糊想法）
- 用户说"帮我生成一个小猫的视频"
- 你的行为：
  1. 先给出合理的默认方案（主题、风格、时长等）
  2. 同时列出 1-3 个可调整的关键选项
  3. 询问用户是否满意默认方案，或需要调整
- 不要只问问题不给方案，用户需要看到你能做什么

### 模式二：故事模式（用户给出故事/脚本）
- 用户说"讲一个关于宇航员在火星发现生命的故事，要有悬疑感，结尾反转"
- 你的行为：
  1. 分析故事的完整性：是否有开头、发展、高潮、结尾？
  2. 判断是否有明确的角色、场景、情绪基调
  3. 如果完整 → 直接生成工作流
  4. 如果不完整 → 指出缺失部分，给出完善建议（不是简单追问，而是给出具体建议）
- 完善建议示例："故事框架不错！建议补充：1）主角的背景动机 2）火星环境的具体描写 3）反转的具体细节"

### 模式三：文件模式（用户上传小说/剧本文件）
- 用户上传一个小说文件
- 你的行为：
  1. 通读文件内容，提取：核心情节、主要角色、关键场景、情绪弧线、适合的视频时长
  2. 判断是否需要改编（小说→剧本的转换）
  3. 给出改编方案：哪些情节保留、哪些删减、如何分集/分段
  4. 直接生成工作流
- 不需要追问，文件内容已经足够

## 决策规则
- 用户只有一句话创意 → 模式一：给默认方案 + 可调整项
- 用户给出故事/情节 → 模式二：分析完整性 → 完整则 plan，不完整则建议完善
- 用户上传文件 → 模式三：通读 → 直接 plan
- 优先给出合理默认方案，同时指出可调整项
- 永远保持创作者主导权，你只是协助和调度

## 输出格式（JSON）
{
  "decision": "plan" | "ask" | "suggest",
  "mode": "creative" | "story" | "file",
  "thought": "你的思考过程",
  "project_intent": "项目意图摘要",
  "creative_direction": "创意方向",
  "default_plan": {
    "theme": "默认主题",
    "style": "默认风格",
    "duration": "默认时长",
    "audience": "默认受众",
    "mood": "默认情绪"
  },
  "adjustable_items": ["可调整项1", "可调整项2"],
  "workflow_nodes": [
    {
      "node_id": "node_001",
      "node_name": "剧本生成",
      "node_type": "script_generation",
      "agent": "screenwriter",
      "module": "text_to_text",
      "status": "pending",
      "input": {},
      "output": {},
      "depends_on": [],
      "next_nodes": [],
      "need_user_review": true,
      "created_by": "director"
    }
  ],
  "suggestions": ["完善建议1", "完善建议2"],
  "questions": ["问题1", "问题2"],
  "next_action": ""
}

## 注意
- 不要一次性生成无法执行的大而空方案。
- 每次输出都要面向"可执行节点"。
- 所有流程必须能够被系统模块调用。
- 用户不需要懂节点配置，你要用自然语言理解需求，并转化为结构化任务。
- 当信息不足时，优先给出合理默认方案，同时指出可调整项。
- 永远保持创作者主导权，你只是协助和调度。
"""


# ═══════════════════════════════════════════════════════
# 进阶模式（advanced）剧本协商 system prompt
# ═══════════════════════════════════════════════════════
OUTLINE_REFINE_SYSTEM_PROMPT = """你是一位有思想的电影导演，正在与创作者多轮对话深化一个视频项目的剧本大纲。

## 你的工作方式
- **不是 checklist 问答机** —— 不要一上来就问"你想要什么风格？时长多久？平台是？"
- 像一个真正的导演那样思考：故事的内核是什么？观众会被什么打动？冲突在哪？情感弧线如何？
- 每轮基于当前剧本大纲 + 创作者新意见，**增量更新** outline（保留已确认部分，只改创作者提到的地方）
- 给创作者**具体的建议和提议**，不是空泛的"你觉得怎么样"——例如"我建议第二场加一个回忆闪回，呈现他战友的牺牲"
- 简短回应（reply ≤80 字），节奏紧凑

## 输出 JSON schema（严格遵守）
{
  "reply": "导演用第一人称的简短回应，可包含具体建议或一个关键问题（≤80字）",
  "outline": {
    "title": "项目标题（可暂用工作标题）",
    "logline": "一句话故事内核（25-40字）",
    "tone": "情绪基调（如：温暖、悬疑、致敬、荒诞）",
    "characters": [
      {"name": "角色名", "role": "主角/配角/...", "description": "外貌+性格一句话"}
    ],
    "scenes": [
      {"scene_id": "S01", "location": "地点", "beat": "本场戏的叙事功能（开场/转折/高潮/收束）", "duration_seconds": 5, "key_action": "本场核心动作或情绪"}
    ]
  },
  "ready_to_confirm": false,
  "missing_aspects": ["还需要跟创作者确认的点（如：还没定结局）"]
}

## ready_to_confirm 判定（重要）
true 当且仅当：
- outline 的 title / logline / tone / characters / scenes 都已填充
- scenes 至少 3 个 且每个有 key_action
- 创作者最近 2 轮表达过满意（"OK / 就这样 / 可以 / 听起来不错"等）或没再提改动
- 你认为故事完整、可以进入下游创作

false 时 missing_aspects 列出关键缺口（最多 3 项），给创作者引导。

## 增量更新原则
- 创作者只改某个场景 → 只动 scenes 数组里对应那项，其它场景**原样保留**
- 创作者没提的字段 → **保持 current_outline 里的值**，不要改
- 第一次没有 current_outline → 基于创作者第一句话起一个完整初版
"""


# ═══════════════════════════════════════════════════════
# 0.6.4：总导演 review 美术输出 system prompt
# ═══════════════════════════════════════════════════════
DIRECTOR_REVIEW_ART_SYSTEM_PROMPT = """你是 Octopus 的总导演，正在 review 美术导演输出的视觉设计方案。

## 你的任务
对照剧本检查美术输出，**只针对确有问题的字段**给出 patch。允许什么都不改（返回空 patch + notes 标注"未改动"）。

## 可改字段（只能改这些）
- characters[*] 的 appearance / clothing / image_prompt / negative_prompt
- scenes[*] 的 environment / lighting / mood / image_prompt / negative_prompt
- props[*] 的 description / use_case / image_prompt / negative_prompt（0.6.4 关键道具）
- visual_style.keywords / color_palette / lighting / texture / camera_feel
- style_guide.consistency_rules / notes

## 严禁修改（改了下游会崩）
- 任何 ID（character_id / scene_id / prop_id）
- 任何 name / location（下游引用 key）
- characters / scenes / props 的条目增删（数量必须保持不变）
- subject_type（影响后续审核策略）

## Review 重点
1. **人物覆盖**：剧本里点名出现的角色是否都有对应 character？如果缺失，在 review_notes 里点名（**但不要在 patch 里加新条目**，由用户决定是否重生成）
2. **场景覆盖**：剧本关键场景是否都有？同上
3. **道具覆盖**：剧本里反复出现的关键道具是否都有对应 prop？同上
4. **风格一致**：characters[*].image_prompt / props[*].image_prompt 的风格词是否和 visual_style.keywords 对齐？不一致就改 image_prompt
5. **画面冲突**：角色的 clothing 和场景的 mood 是否冲突（如雪夜场景配吊带短裙）；道具的材质风格是否与剧本世界观一致（如奇幻剧情却给了塑料感道具）
6. **prompt 卫生**：角色 prompt 是否混入场景元素、场景 prompt 是否混入人物、道具 prompt 是否混入角色/场景（应当不混）

## 输出 JSON（严格）
{
  "characters_patch": {
    "<character_id>": {"image_prompt": "新的完整中文 prompt", "clothing": "..."}
  },
  "scenes_patch": {
    "<scene_id>": {"image_prompt": "...", "mood": "..."}
  },
  "props_patch": {
    "<prop_id>": {"image_prompt": "...", "description": "..."}
  },
  "visual_style_patch": {"keywords": [...], "lighting": "..."},
  "style_guide_patch": {},
  "review_notes": "1-3 句话讲改了什么、为什么。缺失的角色/场景/道具在这里点名"
}

只列要改的字段/条目，不改的不要出现。不要输出 JSON 之外的任何内容。"""


# ═══════════════════════════════════════════════════════
# 0.6.4：总导演 review 分镜输出 system prompt
# ═══════════════════════════════════════════════════════
DIRECTOR_REVIEW_STORYBOARD_SYSTEM_PROMPT = """你是 Octopus 的总导演，正在 review 分镜导演输出的 shot list。

## 你的任务
对照剧本和美术方案检查分镜，**只对确有问题的镜头**给出字段级 patch。允许什么都不改。

## 可改字段（只能改这些）
- subject / action / emotion / environment（画面描述）
- dialogue / narration / sound_effects（音轨内容）
- visual_prompt / video_prompt（关键！下游生图/生视频依据）
- continuity_to_next / transition_to_next / pacing_role
- duration_seconds（可微调，但保持总时长 ±10% 内）

## 严禁修改
- shot_id / scene_id（引用 key）
- shot_size / camera_angle / camera_movement（技术参数交给分镜导演）
- requires_keyframe（影响下游 fanout 路径）
- shots 数量（不能增删！子节点已 pre-create，数量对不上会丢镜头）
- depends_on_assets（@角色 / @场景的引用关系）

## Review 重点
1. **相邻 shot 衔接**：subject / environment / continuity_to_next 是否承接？
   - 例：shot_3 用了"主角推门进入卧室"，shot_4 应该在卧室或卧室内的物件特写，不能突然切到剧本未出现的厨房
   - 例：shot_5 是 climax 然后 shot_6 不能立刻又是 establish（节奏断了）
2. **角色重复出场无意义**：同一角色连续多个 shot 做相似动作，把无意义的 emotion / action 改有意义（不要增删 shot）
3. **故事结束后冒出无关剧情**：最后几个 shot 的 subject / action 是否还在剧本主线上？跑题了改回来
4. **是否抓住重点**：剧本里的高潮事件是不是分到了 climax 类的 shot？如果错配，调 pacing_role + action
5. **音轨完整**：对白旁白音效是否和画面匹配？
6. **【0.6.4 关键】主从尺寸描述**：visual_prompt / video_prompt 中**每个 @ 引用后必须有"画面占比+位置"括号**，否则关键帧多图融合会把 30cm 玩偶画得跟主角一样大（"小熊巨大" bug）。
   - 检查规则：扫描 visual_prompt / video_prompt 中每个 `@<名字>`，看后面是否紧跟形如 `(主体，占画面 X/N 高度)` / `(在主角脚边，X cm 高，占画面 1/N)` / `(背景，占满画面)` 的括号
   - 缺失时：在 patch 里把整段 visual_prompt / video_prompt 重写补齐每个 @ 后的括号，**道具必须复述美术设定的真实尺寸**（30cm / 90cm / 手掌大小），不写就会被画成主体大小
   - 例（不合格）：`@小姑娘 模仿大人拍@小熊 圆鼓鼓肚子的侧脸, 在@客厅地毯 中, CU shot...`
   - 例（合格）：`@小姑娘 (主体，占画面 2/3 高度) 模仿大人拍@小熊 (30cm 玩偶，抱在膝头，占画面 1/5 高度) 圆鼓鼓肚子的侧脸, 在@客厅地毯 (背景，占满画面) 中, CU shot...`

7. **【0.6.5 关键】覆盖审查（兜底补 @ 引用）**：用户给的剧本会在 system context 上方列出**全部可引用的角色 / 场景 / 道具**。你必须扫描所有 shot 的 visual_prompt / video_prompt，**任何在美术阶段设计了但 0 个 shot 都没引用的资源都是漏的**。
   - 检查规则：对照上下文里 "可引用的角色 / 场景 / 道具" 列表，逐项查 `@<名字>` 是否至少出现 1 次（含别名/斜杠变体）
   - **缺失时强制补**：选择剧情最合适的 shot（通常是新场景的 establish shot 或道具的特写 shot），在 patch 里把 visual_prompt + video_prompt 重写，加进 @<缺失项> 引用 + 主从尺寸括号
   - 道具特别强调：道具是美术专门为剧情设计的视觉锚点，缺一个就少了一段"形象一致性"（这是 0.6.4 整套关键道具系统的初衷）；如果剧情确实塞不进，至少安排一个 1-2 秒的"主角注视着这个 @道具" 特写 shot
   - 例（覆盖缺失）：美术给了 @龙吟剑、@青铜镜、@护身符 三个道具，但 shots 里只有 @龙吟剑 出现 — review 必须找到合适 shot 补 @青铜镜 和 @护身符
   - 在 review_notes 里写"补了 N 个缺失引用：@青铜镜 加到 shot_4，@护身符 加到 shot_7"

## 输出 JSON（严格）
{
  "shots_patch": {
    "<shot_id>": {
      "subject": "...",
      "action": "...",
      "visual_prompt": "新的完整 visual_prompt（含 @ 引用）"
    }
  },
  "review_notes": "1-3 句话：改了几个 shot、改的是什么（如'shot_4 和 shot_5 衔接生硬，把 shot_5 的环境改成走廊承接'）。如果发现需要增删 shot 的根本性问题，在 notes 里说明"
}

只列要改的 shot 和字段，不改的不要出现。不要输出 JSON 之外的任何内容。"""


# ═══════════════════════════════════════════════════════
# 0.7.0 专家模式：通读剧本 + 分集协商 system prompts
# ═══════════════════════════════════════════════════════
SCRIPT_PARSE_SYSTEM_PROMPT = """你是一位专业的剧本统筹/编剧顾问，正在通读一份用户上传的剧本/小说原文，为后续 AI 视频制作做准备。

## 你的任务
快速通读全文，提取关键信息，输出结构化 JSON。**不要逐句翻译/复述，提取骨架即可。**

## 输出 JSON（严格遵守）
{
  "logline": "一句话概括（25-40 字）",
  "genre": "类型（如悬疑/古装/言情/奇幻/科幻）",
  "themes": ["主题词1", "主题词2"],
  "tone": "整体基调（如冷峻/温暖/荒诞）",
  "characters": [
    {"name": "角色名", "role": "主角/反派/配角", "personality": "性格一句话", "arc": "成长线/转变一句话"}
  ],
  "scene_outline": [
    {
      "chapter": "章节标题或编号",
      "summary": "本章故事 1-2 句",
      "key_events": ["关键事件1", "事件2"],
      "raw_passages": [
        "（极其重要）从原文摘抄 2-4 段最有代表性的内容：含核心对白（带引号）、关键动作、独特场景描写。**严格忠实原文用词**，不要 paraphrase。每段 100-400 字，整体 ≤ 1500 字。这是给后续编剧"遵循原著"的依据，缺则下游会自由二创丢原作味道。"
      ]
    }
  ],
  "est_total_min": 60,
  "suggested_episode_count": 6,
  "suggested_aspect_ratio": "9:16",
  "notes": "其他需要让创作团队知道的（如：本故事涉及大量打斗 / 旁白偏多 / 对白密集 / 需要历史考据 等）"
}

## 注意
- characters 抓主要的（≤ 8 个）
- scene_outline 按章节/段落自然分（≤ 20 项）
- suggested_episode_count 按"每集 5-15 分钟"反推
- suggested_aspect_ratio 默认 "9:16"（短视频/漫剧），明显电影叙事可建议 "16:9"
- 拿不准的字段宁缺勿编（用空字符串/空数组）
- 中文输出，专有人名照抄原文
"""


EPISODE_BREAKDOWN_SYSTEM_PROMPT = """你是漫剧 / 短剧的制片总监，正在与制作人协商如何把剧本切成 N 集。

## 你的工作方式
- 每集要有完整的起承转合（开 → 承 → 转 → 合）
- 每集结尾留 cliff-hanger 钩住观众看下一集；最后一集留情感落点 / 反转
- **每集时长跟着剧情走，不硬凑**：`script_summary.target_episode_min` 是创作者期望的每集时长（**参考，非硬指标**）——按它估算集数，但每集 `est_duration_min` 取**该集真实剧情所需**，集与集**不必等长**（剧情饱满的集长些、过场的集短些）。没有 target_episode_min 时按 est_total_min / 漫剧每集 5-15 分钟估
- 跨集贯穿一条主线 + 每集独立小线索
- 制作人可能说"把 3、4 集合并"/"第 5 集太长拆成两集"/"加一集前传"，尊重他的判断微调
- 简短回应（reply ≤ 80 字）

## 输出 JSON（严格遵守）
{
  "reply": "制片总监的简短第一人称回应，可含具体提议（≤ 80 字）",
  "episodes": [
    {
      "ep_id": "EP01",
      "title": "本集标题（5-12 字）",
      "story_arc": "本集起承转合（2-3 句）",
      "scene_range": "本集覆盖原剧本的哪些章节/场景（人话描述，如 第 1-3 章 / 场景 5-12）",
      "scene_indices": [0, 1, 2],
      "character_indices": [0, 2],
      "est_duration_min": 8,
      "cliff_hanger": "本集收尾的钩子（最后一集填情感落点）"
    }
  ],
  "ready_to_confirm": false,
  "missing_aspects": ["还需跟制作人确认的（如：第 5 集主旨不明 / 配角动机欠缺）"]
}

## 【关键】scene_indices（极度重要）
`scene_indices` 是 0-based 数组，**严格对应 script_summary.scene_outline 的索引位置**。
- 例：scene_outline 有 10 个场景，EP01 用前 3 个 → `scene_indices: [0, 1, 2]`
- 每个场景**只能属于一集**（除非剧情需要跨集穿插）。所有集 scene_indices 合起来应覆盖 scene_outline 的大部分场景，且基本不重叠。
- 严禁所有集都填 `[0,1,2,3,...,N-1]`（这是把整剧塞每一集，下游编剧会重复生成全本剧本）
- 严禁集间大量重叠（除非真有"闪回某一章"的剧情需要）
- 必须填 — 缺这个字段下游编剧没法只写本集

## 【关键】character_indices（极度重要）
`character_indices` 也是 0-based 数组，**对应 script_summary.characters 的索引**，表示**本集真正有戏份/出场的角色**。
- 例：master 4 个角色，EP01 只有主角 + 反派出场 → `character_indices: [0, 1]`
- 严禁每集都填 `[0,1,2,...,M-1]`（不可能每个配角每集都上）
- 配角戏份少的集就少填几个，不要凑数
- 必须填 — 下游编剧会按这个列表决定剧本里出场谁，不能列在外的角色

## ready_to_confirm 判定
true 当且仅当：
- 每集 title / story_arc / scene_range / est_duration_min 都填了
- 集数合理（不会一集塞 30 分钟也不会 0.5 分钟）
- 制作人最近 2 轮没再提改动
- 各集时长总和 ≈ 剧本体量（有 target_episode_min 时按它×集数；否则按 est_total_min）——**剧情为主，不为凑总时长注水，也不硬删剧情**

false 时 missing_aspects 给制作人引导（最多 3 项）。

## 增量更新原则
- 制作人只动某集 → 只改 episodes 数组里对应那项，其它原样保留
- 第一次 current_breakdown 为空 → 基于 script_summary 起一个完整初版
- ep_id 保持稳定（EP01 / EP02 ...），合并/拆分后重新编号
"""


class DirectorAgent(BaseAgent):
    """
    导演 Agent — 创作大脑
    
    三种模式：
    1. 创意模式：模糊想法 → 默认方案 + 可调整项
    2. 故事模式：故事/脚本 → 分析完整性 → plan 或 suggest
    3. 文件模式：上传文件 → 通读 → 直接 plan
    
    使用方式：
        director = DirectorAgent()
        
        # 创意模式
        result = await director.run({
            "request": "帮我创作一个关于猫咪的温馨短视频"
        })
        
        # 故事模式
        result = await director.run({
            "request": "讲一个宇航员在火星发现生命的故事，要有悬疑感"
        })
        
        # 文件模式
        result = await director.run({
            "request": "把这个小说改编成视频",
            "file_content": "小说全文...",
            "file_name": "mars_novel.txt"
        })
    """
    
    name = "director"
    description = "创作调度与决策核心"
    system_prompt = DIRECTOR_SYSTEM_PROMPT
    
    # 创意模式：信息完整度阈值（低于此值才追问）
    CREATIVE_CONFIDENCE_THRESHOLD = 0.3
    
    # 故事模式：完整性阈值
    STORY_COMPLETENESS_THRESHOLD = 0.6
    
    @cloud_method
    async def execute(self, task: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        导演核心逻辑 — 自动判断模式并处理
        
        Args:
            task: {
                "request": "用户原始输入",
                "file_content": "文件内容（可选）",
                "file_name": "文件名（可选）",
                "history": ["历史对话"],
                ...
            }
            context: 历史上下文
        """
        user_request = task.get("request", "")
        file_content = task.get("file_content", "")
        file_name = task.get("file_name", "")
        history = task.get("history", [])
        context = task.get("context", {})

        # 判断模式
        if file_content:
            # 模式三：文件模式
            return await self._handle_file_mode(user_request, file_content, file_name)

        # 其余一律走 checklist 收集需求（创意/故事统一入口）
        # 旧版按 LLM 分类走 story/creative 双分支，但 story 分支不收集风格/时长/平台/情绪等关键参数，
        # 直接生成空方案，违背"checklist 不完成不出方案"的产品约定。
        return await self._handle_creative_mode(user_request, history, context)
    
    async def _classify_request(self, request: str) -> str:
        """
        判断用户输入是"创意"还是"故事"
        
        Returns: "creative" | "story"
        """
        prompt = f"""判断以下用户输入的类型：

用户输入："{request}"

- "creative"：用户只有模糊想法，比如"帮我做个视频""生成一个小猫视频""我想创作点什么"
- "story"：用户给出了具体的故事情节、角色、场景，比如"讲一个关于...的故事""主角是一个..."

只返回 JSON：{{"type": "creative" | "story", "reason": "判断理由"}}"""
        
        response = await self.llm_generate(prompt, system=self.system_prompt, json_mode=True)
        try:
            result = json.loads(response)
            return result.get("type", "creative")
        except json.JSONDecodeError:
            # 启发式回退
            story_keywords = ["故事", "主角", "情节", "剧情", "讲述", "关于", "发现", "冒险", "爱情", "悬疑", "科幻"]
            if any(kw in request for kw in story_keywords) and len(request) > 20:
                return "story"
            return "creative"
    
    # ═══════════════════════════════════════════════════════
    # 模式一：创意模式
    # ═══════════════════════════════════════════════════════
    
    # ═══════════════════════════════════════════════════════
    # 模式一：创意模式（强制一问一答收集需求）
    # ═══════════════════════════════════════════════════════
    
    # 创意模式必须收集的 checklist
    CREATIVE_CHECKLIST = [
        "content_source",      # 内容来源：一句话创意/故事大纲/现成剧本/小说
        "style",               # 风格：写实/动漫/电影感/赛博朋克等
        "duration",            # 时长：15秒/60秒/3分钟/等
        "episodes",            # 集数：1集/多集
        "aspect_ratio",        # 比例：16:9/9:16/1:1
        "platform",            # 平台：抖音/小红书/B站/等
        "mood",                # 情绪：温馨/悬疑/搞笑/等
    ]
    
    # checklist 满足阈值：必须全部满足才生成方案
    CHECKLIST_COMPLETION_THRESHOLD = 1.0
    
    async def _handle_creative_mode(self, request: str, history: List[str] = None, context: Dict = None) -> Dict[str, Any]:
        """
        创意模式处理：强制一问一答收集需求
        
        策略：
        1. 分析当前已收集的信息
        2. 检查 checklist 完成度
        3. 如果 checklist 未完成 → 追问缺失项
        4. 如果 checklist 完成 → 生成执行方案，询问确认
        """
        history = history or []
        context = context or {}
        
        # 提取已收集的信息（从对话历史和上下文中）
        collected_info = self._extract_collected_info(history, context)
        
        # 检查 checklist 完成度
        checklist_status = self._check_checklist(collected_info)
        completion_rate = checklist_status["completion_rate"]
        missing_items = checklist_status["missing_items"]
        
        # 如果 checklist 未完成，追问缺失项
        if completion_rate < self.CHECKLIST_COMPLETION_THRESHOLD:
            questions = self._generate_questions(missing_items, collected_info)
            
            return {
                "decision": "ask",
                "mode": "creative",
                "thought": f"Checklist 完成度 {completion_rate:.0%}，需要收集: {', '.join(missing_items)}",
                "project_intent": collected_info.get("theme", request),
                "creative_direction": "待确定",
                "collected_info": collected_info,
                "checklist_status": checklist_status,
                "questions": questions,
                "next_action": "请回答以上问题，帮助我更好地理解您的创作需求",
                "progress": f"需求收集进度: {completion_rate:.0%}"
            }
        
        # checklist 已完成，生成方案摘要（不返回全部节点，节点逐步生成）
        default_plan = self._build_default_plan(collected_info)
        
        # 只在脑子里生成完整方案，不暴露给前端
        workflow_nodes = self._build_full_workflow_nodes(collected_info, default_plan)
        plan_summary = self._get_plan_summary(workflow_nodes)
        
        return {
            "decision": "plan",
            "mode": "creative",
            "thought": "Checklist 已完成，生成创作方案。确认后将从剧本生成开始逐步执行。",
            "project_intent": collected_info.get("theme", ""),
            "creative_direction": collected_info.get("style", "通用"),
            "default_plan": default_plan,
            # 不返回 workflow_nodes，只返回方案摘要
            "plan_summary": plan_summary,
            "collected_info": collected_info,
            "checklist_status": checklist_status,
            "adjustable_items": self._generate_adjustable_items(collected_info),
            "next_action": "方案已生成！包含剧本→美术→分镜→素材→视频→音频→字幕→合并的完整流程。确认后我将开始逐步创作，每步完成后与您确认。",
            "progress": "需求收集完成 ✓ — 等待确认开始创作"
        }
    
    def _extract_collected_info(self, history: List[str], context: Dict) -> Dict[str, Any]:
        """从对话历史和上下文中提取已收集的信息"""
        info = {
            "theme": "",
            "style": "",
            "duration": "",
            "episodes": "",
            "aspect_ratio": "",
            "platform": "",
            "mood": "",
            "content_source": "",
            "characters": [],
            "scenes": [],
            "raw_input": ""
        }
        
        # 合并所有对话内容 — 统一转小写便于匹配
        all_text = " ".join(history) if history else ""
        all_text += " " + str(context)
        all_text_lower = all_text.lower()
        
        # 简单启发式提取（实际应该用 LLM 提取）
        # 内容来源
        if any(kw in all_text for kw in ["小说", "剧本", "文件", "文档"]):
            info["content_source"] = "现成作品"
        elif any(kw in all_text for kw in ["故事", "情节", "剧情", "主角"]):
            info["content_source"] = "故事大纲"
        else:
            info["content_source"] = "一句话创意"
        
        # 风格
        style_keywords = {
            "写实": ["写实", "真实", "纪录片", "真人"],
            "动漫": ["动漫", "二次元", "卡通", "动画"],
            "电影感": ["电影感", " cinematic", "大片", "电影"],
            "赛博朋克": ["赛博朋克", "cyberpunk", "科幻"],
            "温馨": ["温馨", "治愈", "温暖"],
            "悬疑": ["悬疑", "惊悚", "紧张"],
            "搞笑": ["搞笑", "喜剧", "幽默", "沙雕"]
        }
        for style, keywords in style_keywords.items():
            if any(kw in all_text for kw in keywords):
                info["style"] = style
                break
        
        # 时长
        duration_patterns = [
            ("15秒", ["15秒", "15s", "十五秒"]),
            ("30秒", ["30秒", "30s", "三十秒"]),
            ("60秒", ["60秒", "60s", "一分钟", "1分钟"]),
            ("3分钟", ["3分钟", "三分钟", "180秒", "180s"]),
            ("5分钟", ["5分钟", "五分钟", "300秒"]),
            ("10分钟", ["10分钟", "十分钟"])
        ]
        for duration, patterns in duration_patterns:
            if any(p in all_text for p in patterns):
                info["duration"] = duration
                break
        
        # 比例
        if "9:16" in all_text or "竖屏" in all_text or "抖音" in all_text or "快手" in all_text or "小红书" in all_text:
            info["aspect_ratio"] = "9:16"
        elif "1:1" in all_text or "方形" in all_text:
            info["aspect_ratio"] = "1:1"
        elif "16:9" in all_text or "横屏" in all_text or "B站" in all_text or "YouTube" in all_text:
            info["aspect_ratio"] = "16:9"
        
        # 平台 — 使用小写匹配，避免大小写问题
        platform_keywords = {
            "抖音": ["抖音", "douyin", "tiktok"],
            "小红书": ["小红书", "redbook", "xhs"],
            "B站": ["B站", "bilibili", "哔哩哔哩"],
            "快手": ["快手", "kuaishou"],
            "YouTube": ["youtube", "油管"],
            "微信视频号": ["视频号", "微信"]
        }
        for platform, keywords in platform_keywords.items():
            # 同时检查原始文本和小写文本
            if any(kw in all_text or kw in all_text_lower for kw in keywords):
                info["platform"] = platform
                break
        
        # 情绪
        mood_keywords = {
            "温馨": ["温馨", "治愈", "温暖", "感人"],
            "悬疑": ["悬疑", "惊悚", "紧张", "刺激"],
            "搞笑": ["搞笑", "喜剧", "幽默", "欢乐", "沙雕"],
            "浪漫": ["浪漫", "爱情", "甜蜜"],
            "励志": ["励志", "正能量", "奋斗"],
            "科幻": ["科幻", "未来", "科技"]
        }
        for mood, keywords in mood_keywords.items():
            if any(kw in all_text for kw in keywords):
                info["mood"] = mood
                break
        
        # 集数
        if any(kw in all_text for kw in ["多集", "系列", "季", "集数"]):
            info["episodes"] = "多集"
        elif any(kw in all_text for kw in ["1集", "一集", "单集", "短片"]):
            info["episodes"] = "1集"
        
        # 主题（取第一句话或前50字）
        if history:
            info["theme"] = history[0][:50] if len(history[0]) > 50 else history[0]
        
        info["raw_input"] = all_text[:200]
        return info
    
    def _check_checklist(self, collected_info: Dict) -> Dict[str, Any]:
        """检查 checklist 完成度"""
        checklist_items = {
            "content_source": collected_info.get("content_source", ""),
            "style": collected_info.get("style", ""),
            "duration": collected_info.get("duration", ""),
            "aspect_ratio": collected_info.get("aspect_ratio", ""),
            "platform": collected_info.get("platform", ""),
            "mood": collected_info.get("mood", ""),
        }
        
        completed = sum(1 for v in checklist_items.values() if v)
        total = len(checklist_items)
        completion_rate = completed / total
        
        missing_items = [k for k, v in checklist_items.items() if not v]
        
        return {
            "completion_rate": completion_rate,
            "completed_count": completed,
            "total_count": total,
            "missing_items": missing_items,
            "checklist_items": checklist_items
        }
    
    def _generate_questions(self, missing_items: List[str], collected_info: Dict) -> List[str]:
        """根据缺失项生成追问问题"""
        question_map = {
            "content_source": "您是想从一句话创意开始，还是有现成的小说/剧本/故事大纲？",
            "style": "您希望是什么视觉风格？比如：写实、动漫、电影感、赛博朋克、温馨治愈等",
            "duration": "视频时长大概是多少？15秒/30秒/60秒/3分钟/更长？",
            "aspect_ratio": "视频比例是多少？16:9（横屏）还是9:16（竖屏）？",
            "platform": "想在哪个平台发布？抖音/小红书/B站/快手/YouTube？",
            "mood": "视频的情绪基调是什么？温馨/悬疑/搞笑/浪漫/励志？",
        }
        
        # 优先追问最重要的缺失项（最多3个）
        priority_order = ["content_source", "style", "duration", "aspect_ratio", "platform", "mood"]
        sorted_missing = sorted(missing_items, key=lambda x: priority_order.index(x) if x in priority_order else 999)
        
        questions = []
        for item in sorted_missing[:3]:
            if item in question_map:
                questions.append(question_map[item])
        
        return questions
    
    def _build_default_plan(self, collected_info: Dict) -> Dict[str, Any]:
        """根据收集的信息构建默认方案"""
        return {
            "theme": collected_info.get("theme", "未命名项目"),
            "style": collected_info.get("style", "写实"),
            "duration": collected_info.get("duration", "60秒"),
            "audience": self._infer_audience(collected_info.get("platform", "")),
            "mood": collected_info.get("mood", "温馨"),
            "aspect_ratio": collected_info.get("aspect_ratio", "9:16"),
            "platform": collected_info.get("platform", "抖音"),
            "episodes": collected_info.get("episodes", "1集"),
            "content_source": collected_info.get("content_source", "一句话创意")
        }
    
    def _infer_audience(self, platform: str) -> str:
        """根据平台推断受众"""
        platform_audience = {
            "抖音": "18-35岁年轻人",
            "小红书": "18-30岁女性用户",
            "B站": "18-25岁Z世代",
            "快手": "25-45岁下沉市场",
            "YouTube": "全球通用受众"
        }
        return platform_audience.get(platform, "通用受众")
    
    def _generate_adjustable_items(self, collected_info: Dict) -> List[str]:
        """生成可调整项"""
        items = []
        if collected_info.get("style"):
            items.append(f"风格：当前为「{collected_info['style']}」，可调整")
        if collected_info.get("duration"):
            items.append(f"时长：当前为「{collected_info['duration']}」，可调整")
        if collected_info.get("mood"):
            items.append(f"情绪：当前为「{collected_info['mood']}」，可调整")
        return items[:3]
    
    async def _analyze_creative_request(self, request: str) -> Dict[str, Any]:
        """[已废弃] 旧版分析逻辑，保留兼容"""
        return {
            "thought": "使用新版 checklist 逻辑",
            "confidence": 0.5,
            "project_intent": request,
            "creative_direction": "通用短视频",
            "project_type": "社媒短视频",
            "default_plan": {
                "theme": request or "温馨日常",
                "style": "写实",
                "duration": "60秒",
                "audience": "18-35岁年轻人",
                "mood": "温馨"
            },
            "adjustable_items": ["主题可以更具体", "风格可调整", "时长可调整"],
            "questions": ["您希望视频的主题更具体一些吗？"]
        }
    
    # ═══════════════════════════════════════════════════════
    # 模式二：故事模式
    # ═══════════════════════════════════════════════════════
    
    async def _handle_story_mode(self, request: str) -> Dict[str, Any]:
        """
        故事模式处理：用户给出故事/脚本
        
        策略：
        1. 分析故事完整性
        2. 如果完整 → 直接生成工作流
        3. 如果不完整 → 给出具体完善建议（不是简单追问）
        """
        analysis = await self._analyze_story(request)
        completeness = analysis.get("completeness", 0)
        
        # 如果 LLM 没返回 completeness，根据 story_analysis 自行计算
        if completeness is None or completeness == 0:
            sa = analysis.get("story_analysis", {})
            if sa:
                score = sum([
                    1 if sa.get("has_beginning", False) else 0,
                    1 if sa.get("has_development", False) else 0,
                    1 if sa.get("has_climax", False) else 0,
                    1 if sa.get("has_ending", False) else 0,
                    1 if sa.get("has_characters", False) else 0,
                    1 if sa.get("has_scenes", False) else 0,
                    1 if sa.get("has_emotion_arc", False) else 0,
                ]) / 7.0
                completeness = score
        
        response = {
            "mode": "story",
            "thought": analysis.get("thought", ""),
            "project_intent": analysis.get("project_intent", request),
            "creative_direction": analysis.get("creative_direction", ""),
            "story_analysis": analysis.get("story_analysis", {}),
        }
        
        if completeness >= self.STORY_COMPLETENESS_THRESHOLD:
            # 故事完整，直接生成工作流
            response["decision"] = "plan"
            extracted = analysis.get("extracted", {})
            workflow = await self._create_workflow(
                request,
                analysis.get("project_type", "剧情短片"),
                extracted
            )
            response["workflow_nodes"] = workflow
            response["next_action"] = "故事框架完整！我已生成创作工作流，请审核"
        else:
            # 故事不完整，给出完善建议
            response["decision"] = "suggest"
            response["suggestions"] = analysis.get("suggestions", [])
            response["next_action"] = "故事框架不错！建议完善以上几点后，我可以直接生成工作流"
        
        return response
    
    async def _analyze_story(self, request: str) -> Dict[str, Any]:
        """
        分析故事的完整性
        
        返回：
        - completeness: 0-1
        - story_analysis: 结构分析
        - suggestions: 完善建议
        - extracted: 提取的信息（用于生成工作流）
        """
        prompt = f"""分析以下故事/脚本的完整性：

用户输入："{request}"

请分析并返回 JSON：
{{
  "thought": "思考过程",
  "completeness": 0.0-1.0,
  "project_intent": "项目意图",
  "creative_direction": "创意方向",
  "project_type": "剧情短片|广告片|口播视频|纪录片|音乐视频|产品宣传片|社媒短视频",
  "story_analysis": {{
    "has_beginning": true/false,
    "has_development": true/false,
    "has_climax": true/false,
    "has_ending": true/false,
    "has_characters": true/false,
    "has_scenes": true/false,
    "has_emotion_arc": true/false,
    "missing_elements": ["缺失元素1"]
  }},
  "extracted": {{
    "theme": "主题",
    "style": "风格",
    "duration": "时长",
    "audience": "受众",
    "mood": "情绪基调",
    "characters": ["角色1", "角色2"],
    "scenes": ["场景1", "场景2"],
    "platform": "发布平台"
  }},
  "suggestions": ["具体完善建议1", "具体完善建议2"]
}}

规则：
- completeness >= 0.6 表示故事足够完整，可以生成工作流
- suggestions 要具体，不是简单追问，而是给出"建议补充...""建议明确..."
- 即使不完整，也要提取已有信息到 extracted 中"""
        
        response = await self.llm_generate(prompt, system=self.system_prompt, json_mode=True)
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return self._fallback_story_analysis(request)
    
    def _fallback_story_analysis(self, request: str) -> Dict[str, Any]:
        """故事分析失败时的默认回复"""
        return {
            "thought": "使用默认分析",
            "completeness": 0.7,
            "project_intent": request,
            "creative_direction": "剧情短片",
            "project_type": "剧情短片",
            "story_analysis": {
                "has_beginning": True,
                "has_development": True,
                "has_climax": False,
                "has_ending": False,
                "has_characters": True,
                "has_scenes": False,
                "has_emotion_arc": True,
                "missing_elements": ["高潮细节", "结局"]
            },
            "extracted": {
                "theme": request,
                "style": "电影感",
                "duration": "3分钟",
                "audience": "通用受众",
                "mood": "悬疑",
                "characters": [],
                "scenes": [],
                "platform": "B站"
            },
            "suggestions": ["建议补充高潮部分的具体情节", "建议明确结局走向"]
        }
    
    # ═══════════════════════════════════════════════════════
    # 模式三：文件模式
    # ═══════════════════════════════════════════════════════
    
    async def _handle_file_mode(self, request: str, file_content: str, file_name: str) -> Dict[str, Any]:
        """
        文件模式处理：用户上传小说/剧本文件
        
        策略：
        1. 通读文件内容
        2. 提取关键信息
        3. 给出改编方案
        4. 直接生成工作流
        """
        analysis = await self._analyze_file(file_content, file_name)
        
        # 文件模式直接生成工作流
        extracted = analysis.get("extracted", {})
        workflow = await self._create_workflow(
            request,
            analysis.get("project_type", "剧情短片"),
            extracted
        )
        
        return {
            "decision": "plan",
            "mode": "file",
            "thought": analysis.get("thought", ""),
            "project_intent": analysis.get("project_intent", f"改编自 {file_name}"),
            "creative_direction": analysis.get("creative_direction", ""),
            "file_analysis": analysis.get("file_analysis", {}),
            "adaptation_plan": analysis.get("adaptation_plan", {}),
            "workflow_nodes": workflow,
            "next_action": f"已通读《{file_name}》，生成改编工作流，请审核"
        }
    
    async def _analyze_file(self, file_content: str, file_name: str) -> Dict[str, Any]:
        """
        分析文件内容
        
        返回：
        - file_analysis: 文件分析
        - adaptation_plan: 改编方案
        - extracted: 提取的信息
        """
        # 截断过长的文件内容
        max_length = 8000
        truncated_content = file_content[:max_length]
        if len(file_content) > max_length:
            truncated_content += "\n...（内容已截断，后续部分将在剧本生成阶段处理）"
        
        prompt = f"""分析以下文件内容，给出改编方案：

文件名：{file_name}
文件内容：
{truncated_content}

请分析并返回 JSON：
{{
  "thought": "思考过程",
  "project_intent": "项目意图",
  "creative_direction": "创意方向",
  "project_type": "剧情短片|广告片|口播视频|纪录片|音乐视频|产品宣传片|社媒短视频",
  "file_analysis": {{
    "title": "作品标题",
    "genre": "类型",
    "word_count": "字数",
    "main_plot": "主要情节摘要",
    "key_characters": ["角色1", "角色2"],
    "key_scenes": ["场景1", "场景2"],
    "emotion_arc": "情绪弧线",
    "suitable_duration": "适合的视频时长"
  }},
  "adaptation_plan": {{
    "episodes": 1,
    "keep_scenes": ["保留场景1"],
    "remove_scenes": ["删减场景1"],
    "add_elements": ["需要补充的元素"],
    "visual_style": "视觉风格建议"
  }},
  "extracted": {{
    "theme": "主题",
    "style": "风格",
    "duration": "时长",
    "audience": "受众",
    "mood": "情绪基调",
    "characters": ["角色1", "角色2"],
    "scenes": ["场景1", "场景2"],
    "platform": "发布平台"
  }}
}}

规则：
- 给出具体的改编建议：保留哪些情节、删减哪些、如何调整节奏
- 判断适合的视频时长（短视频1-3分钟、中视频5-15分钟、长视频30分钟+）
- 提取所有关键信息用于生成工作流"""
        
        response = await self.llm_generate(prompt, system=self.system_prompt, json_mode=True)
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return self._fallback_file_analysis(file_name)
    
    def _fallback_file_analysis(self, file_name: str) -> Dict[str, Any]:
        """文件分析失败时的默认回复"""
        return {
            "thought": "文件分析失败，使用默认方案",
            "project_intent": f"改编自 {file_name}",
            "creative_direction": "剧情短片改编",
            "project_type": "剧情短片",
            "file_analysis": {
                "title": file_name,
                "genre": "未知",
                "word_count": "未知",
                "main_plot": "需要进一步分析",
                "key_characters": [],
                "key_scenes": [],
                "emotion_arc": "未知",
                "suitable_duration": "3分钟"
            },
            "adaptation_plan": {
                "episodes": 1,
                "keep_scenes": ["主要情节"],
                "remove_scenes": ["次要支线"],
                "add_elements": ["视觉描写", "镜头提示"],
                "visual_style": "电影感"
            },
            "extracted": {
                "theme": f"改编自 {file_name}",
                "style": "电影感",
                "duration": "3分钟",
                "audience": "通用受众",
                "mood": "温馨",
                "characters": [],
                "scenes": [],
                "platform": "B站"
            }
        }
    
    # ═══════════════════════════════════════════════════════
    # 通用：逐步生成节点（交互式工作流）
    # ═══════════════════════════════════════════════════════
    
    # 节点定义模板（按执行顺序）
    NODE_TEMPLATES = [
        {
            "node_id": "node_001",
            "node_name": "剧本生成",
            "node_type": "script_generation",
            "agent": "screenwriter",
            "module": "text_to_text",
            "need_user_review": True,
            "description": "根据主题创作剧本、对白、旁白"
        },
        {
            "node_id": "node_002",
            "node_name": "美术设定",
            "node_type": "art_direction",
            "agent": "art_director",
            "module": "text_to_text",
            "need_user_review": True,
            "description": "角色设计、视觉风格、色彩方案"
        },
        {
            "node_id": "node_003",
            "node_name": "分镜设计",
            "node_type": "storyboard_generation",
            "agent": "storyboard",
            "module": "text_to_text",
            "need_user_review": True,
            "description": "镜头语言、运镜、节奏设计"
        },
        {
            "node_id": "node_004",
            "node_name": "素材生成",
            "node_type": "asset_generation",
            "agent": "module_router",
            "module": "text_to_image",
            "need_user_review": False,
            "description": "根据分镜生成图片素材"
        },
        {
            "node_id": "node_005",
            "node_name": "视频生成",
            "node_type": "video_generation",
            "agent": "module_router",
            "module": "image_to_video",
            "need_user_review": False,
            "description": "将素材转为动态视频片段"
        },
        {
            "node_id": "node_006",
            "node_name": "音频设计",
            "node_type": "audio_design",
            "agent": "audio",
            "module": "tts",
            "need_user_review": False,
            "description": "配音、音效、BGM"
        },
        {
            "node_id": "node_007",
            "node_name": "字幕生成",
            "node_type": "subtitle_generation",
            "agent": "audio",
            "module": "subtitle",
            "need_user_review": False,
            "description": "根据音频生成字幕"
        },
        {
            "node_id": "node_008",
            "node_name": "视频合并",
            "node_type": "video_merge",
            "agent": "module_router",
            "module": "video_merge",
            "need_user_review": True,
            "description": "最终合成输出"
        },
    ]
    
    # 节点依赖关系图
    NODE_DEPENDENCIES = {
        "node_001": [],
        "node_002": [],
        "node_003": ["node_001", "node_002"],
        "node_004": ["node_003"],
        "node_005": ["node_004"],
        "node_006": ["node_001"],
        "node_007": ["node_005", "node_006"],
        "node_008": ["node_005", "node_006", "node_007"],
    }
    
    def _build_full_workflow_nodes(self, collected_info: Dict, default_plan: Dict) -> List[Dict[str, Any]]:
        """构建完整的8节点DAG"""
        platform = collected_info.get("platform", "抖音")
        # 优先用用户在对话里说的 aspect_ratio（横屏/竖屏），没有再按平台猜
        user_ratio = (collected_info.get("aspect_ratio") or "").strip()
        if user_ratio in ("16:9", "9:16", "1:1", "4:3", "3:4"):
            aspect_ratio = user_ratio
        else:
            aspect_ratio = "9:16" if platform in ["抖音", "快手", "小红书"] else "16:9"
        
        nodes = []
        for template in self.NODE_TEMPLATES:
            node_id = template["node_id"]
            
            # 根据节点类型构建输入
            node_input = {}
            if node_id == "node_001":
                node_input = {
                    "topic": collected_info.get("theme", ""),
                    "style": collected_info.get("style", "写实"),
                    "duration": collected_info.get("duration", "60秒"),
                    "audience": self._infer_audience(platform),
                    "mood": collected_info.get("mood", "温馨"),
                    "platform": platform,
                    "aspect_ratio": aspect_ratio,
                }
            elif node_id == "node_002":
                node_input = {
                    "style": collected_info.get("style", "写实"),
                    "mood": collected_info.get("mood", "温馨"),
                    "platform": platform,
                }
            elif node_id == "node_003":
                node_input = {
                    "style": collected_info.get("style", "写实"),
                    "aspect_ratio": aspect_ratio,
                    "platform": platform,
                }
            elif node_id == "node_004":
                node_input = {
                    "batch_size": 5,
                    "aspect_ratio": aspect_ratio,
                }
            elif node_id == "node_005":
                node_input = {
                    "fps": 24,
                    "duration_per_clip": 5,
                }
            elif node_id == "node_006":
                node_input = {
                    "language": "zh",
                    "mood": collected_info.get("mood", "温馨"),
                    "has_voiceover": True,
                    "has_bgm": True,
                }
            elif node_id == "node_007":
                node_input = {
                    "language": "zh",
                    "format": "srt",
                }
            elif node_id == "node_008":
                node_input = {
                    "transition": "fade",
                    "transition_duration": 1.0,
                    "output_format": "mp4",
                    "resolution": "1080x1920" if aspect_ratio == "9:16" else "1920x1080",
                }
            
            deps = self.NODE_DEPENDENCIES.get(node_id, [])
            # 计算 next_nodes（哪些节点依赖当前节点）
            next_nodes = []
            for other_id, other_deps in self.NODE_DEPENDENCIES.items():
                if node_id in other_deps:
                    next_nodes.append(other_id)
            
            nodes.append({
                "node_id": node_id,
                "node_name": template["node_name"],
                "node_type": template["node_type"],
                "agent": template["agent"],
                "module": template["module"],
                "status": "pending",
                "input": node_input,
                "output": {},
                "depends_on": deps,
                "next_nodes": next_nodes,
                "need_user_review": template["need_user_review"],
                "created_by": "director",
                "description": template["description"]
            })
        
        return nodes
    
    def _get_next_node(self, current_node_id: str, project_state: Dict) -> Optional[Dict[str, Any]]:
        """
        根据当前确认的节点，决定下一个要创建的节点
        
        Args:
            current_node_id: 刚被确认的节点ID
            project_state: 项目当前状态（已完成的节点、收集的信息等）
        
        Returns:
            下一个节点的定义，如果没有则返回 None
        """
        # 找到当前节点在模板中的索引
        current_idx = None
        for i, template in enumerate(self.NODE_TEMPLATES):
            if template["node_id"] == current_node_id:
                current_idx = i
                break
        
        if current_idx is None or current_idx >= len(self.NODE_TEMPLATES) - 1:
            return None  # 没有更多节点了
        
        # 获取下一个节点模板
        next_template = self.NODE_TEMPLATES[current_idx + 1]
        collected_info = project_state.get("collected_info", {})
        platform = collected_info.get("platform", "抖音")
        user_ratio = (collected_info.get("aspect_ratio") or "").strip()
        if user_ratio in ("16:9", "9:16", "1:1", "4:3", "3:4"):
            aspect_ratio = user_ratio
        else:
            aspect_ratio = "9:16" if platform in ["抖音", "快手", "小红书"] else "16:9"
        
        # 构建节点输入
        node_input = {}
        if next_template["node_id"] == "node_002":
            # 美术设定
            node_input = {
                "style": collected_info.get("style", "写实"),
                "mood": collected_info.get("mood", "温馨"),
                "platform": platform,
            }
        elif next_template["node_id"] == "node_003":
            # 分镜设计
            node_input = {
                "style": collected_info.get("style", "写实"),
                "aspect_ratio": aspect_ratio,
                "platform": platform,
            }
        elif next_template["node_id"] == "node_004":
            # 素材生成
            node_input = {
                "batch_size": 5,
                "aspect_ratio": aspect_ratio,
            }
        elif next_template["node_id"] == "node_005":
            # 视频生成
            node_input = {
                "fps": 24,
                "duration_per_clip": 5,
            }
        elif next_template["node_id"] == "node_006":
            # 音频设计
            node_input = {
                "language": "zh",
                "mood": collected_info.get("mood", "温馨"),
                "has_voiceover": True,
                "has_bgm": True,
            }
        elif next_template["node_id"] == "node_007":
            # 字幕生成
            node_input = {
                "language": "zh",
                "format": "srt",
            }
        elif next_template["node_id"] == "node_008":
            # 视频合并
            node_input = {
                "transition": "fade",
                "transition_duration": 1.0,
                "output_format": "mp4",
                "resolution": "1080x1920" if aspect_ratio == "9:16" else "1920x1080",
            }
        
        # 构建完整节点
        deps = self.NODE_DEPENDENCIES.get(next_template["node_id"], [])
        return {
            "node_id": next_template["node_id"],
            "node_name": next_template["node_name"],
            "node_type": next_template["node_type"],
            "agent": next_template["agent"],
            "module": next_template["module"],
            "status": "pending",
            "input": node_input,
            "output": {},
            "depends_on": deps,
            "next_nodes": [],
            "need_user_review": next_template["need_user_review"],
            "created_by": "director",
            "description": next_template["description"]
        }
    
    def _get_plan_summary(self, workflow_nodes: List[Dict] = None) -> str:
        """返回完整计划摘要"""
        nodes = workflow_nodes or self.NODE_TEMPLATES
        steps = []
        for i, node in enumerate(nodes, 1):
            name = node.get("node_name", node.get("node_id", f"节点{i}"))
            review = node.get("need_user_review", False)
            review_mark = " 👀需确认" if review else ""
            desc = node.get("description", "")
            steps.append(f"{i}. {name}{review_mark} — {desc}")
        return "\n".join(steps)
    
    # [保留旧方法用于兼容]
    async def _create_workflow(self, request: str, project_type: str, params: Dict) -> List[Dict[str, Any]]:
        """[已废弃] 旧版一次性生成所有节点，保留兼容"""
        # 统一参数提取
        theme = params.get("theme", request)
        style = params.get("style", "写实")
        duration = params.get("duration", "60秒")
        audience = params.get("audience", "通用受众")
        mood = params.get("mood", "温馨")
        characters = params.get("characters", [])
        scenes = params.get("scenes", [])
        platform = params.get("platform", "抖音")
        
        nodes = []
        aspect_ratio = "9:16" if platform in ["抖音", "快手", "小红书"] else "16:9"
        
        for template in self.NODE_TEMPLATES:
            node = {
                "node_id": template["node_id"],
                "node_name": template["node_name"],
                "node_type": template["node_type"],
                "agent": template["agent"],
                "module": template["module"],
                "status": "pending",
                "input": {},
                "output": {},
                "depends_on": self.NODE_DEPENDENCIES.get(template["node_id"], []),
                "next_nodes": [],
                "need_user_review": template["need_user_review"],
                "created_by": "director"
            }
            nodes.append(node)
        
        return nodes
    
    @cloud_method
    async def on_node_confirmed(self, node_id: str, project_state: Dict) -> Dict[str, Any]:
        """
        当用户确认一个节点后，导演决定下一步
        
        Args:
            node_id: 被确认的节点ID
            project_state: 项目状态（包含已收集的信息、已完成的节点等）
        
        Returns:
            {
                "action": "create_next_node" | "execute" | "complete",
                "next_node": {...} | None,
                "message": "提示消息"
            }
        """
        # 找到被确认的节点
        confirmed_nodes = project_state.get("confirmed_nodes", [])
        collected_info = project_state.get("collected_info", {})
        
        # 获取下一个节点
        next_node = self._get_next_node(node_id, project_state)
        
        if next_node:
            return {
                "action": "create_next_node",
                "next_node": next_node,
                "message": f"✅ {self._get_node_name(node_id)} 已确认！接下来进行：{next_node['node_name']}"
            }
        else:
            return {
                "action": "complete",
                "next_node": None,
                "message": "🎉 所有节点已完成！项目创作流程全部结束。"
            }
    
    def _get_node_name(self, node_id: str) -> str:
        """获取节点名称"""
        for template in self.NODE_TEMPLATES:
            if template["node_id"] == node_id:
                return template["node_name"]
        return node_id
    
    # ═══════════════════════════════════════════════════════
    # 节点修改影响分析
    # ═══════════════════════════════════════════════════════
    
    async def review_workflow(self, node_id: str, new_params: Dict, full_dag: List[Dict]) -> Dict[str, Any]:
        """
        用户修改节点后，导演判断影响范围
        
        Args:
            node_id: 被修改的节点ID
            new_params: 新参数
            full_dag: 完整DAG
        
        Returns:
            {affected_nodes: [], need_rerun: [], reason: "", suggestion: ""}
        """
        # 找到被修改节点
        target_node = None
        for node in full_dag:
            if node["node_id"] == node_id:
                target_node = node
                break
        
        if not target_node:
            return {"error": f"节点 {node_id} 未找到"}
        
        # BFS 遍历下游节点
        affected = []
        need_rerun = []
        visited = set()
        queue = [node_id]
        
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            
            for node in full_dag:
                if current in node.get("depends_on", []):
                    if node["node_id"] not in affected:
                        affected.append(node["node_id"])
                        need_rerun.append(node["node_id"])
                        queue.append(node["node_id"])
        
        # 生成建议
        if affected:
            suggestion = f"修改 {target_node['node_name']} 会影响下游 {len(affected)} 个节点：{', '.join(affected)}。建议重跑受影响节点以保持一致性。"
        else:
            suggestion = f"修改 {target_node['node_name']} 不影响其他节点，可以直接更新。"
        
        return {
            "affected_nodes": affected,
            "need_rerun": need_rerun,
            "reason": f"修改 {target_node['node_name']} 会影响下游 {len(affected)} 个节点",
            "suggestion": suggestion
        }

    # ═══════════════════════════════════════════════════════
    # 进阶模式（advanced）：与创作者多轮协商剧本大纲
    # ═══════════════════════════════════════════════════════

    @cloud_method
    async def refine_outline(
        self,
        message: str,
        history: List[str],
        current_outline: Optional[Dict[str, Any]],
        collected_info: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """进阶模式剧本协商核心。

        每次创作者发一条消息，调用一次 LLM 增量更新 outline。
        返回 {reply, outline, ready_to_confirm, missing_aspects}。
        """
        # 简短拼最近 6 轮对话上下文（再多没必要，token 浪费）
        recent_history = history[-6:] if history else []
        history_block = "\n".join(f"- {h[:200]}" for h in recent_history) or "（暂无）"

        current_outline_json = json.dumps(current_outline or {}, ensure_ascii=False, indent=2)

        # collected_info 是从对话里抓到的零散偏好（平台 / 时长 / 风格 / aspect_ratio 等），
        # 进阶模式不再做 checklist 强制，但有这些倾向就喂给 LLM 当背景
        hints = collected_info or {}
        hints_lines = []
        for k in ("platform", "duration", "style", "mood", "aspect_ratio"):
            v = hints.get(k)
            if v:
                hints_lines.append(f"- {k}: {v}")
        hints_block = "\n".join(hints_lines) or "（暂无偏好）"

        # 附件段（创作者贴的参考图）—— 编号便于后续阶段引用
        attach_block = ""
        if attachments:
            atts = attachments[:5]
            lines = [
                f"  图 {i + 1}: {a.get('url', '')} （{a.get('name', '?')}）"
                for i, a in enumerate(atts)
            ]
            attach_block = (
                "\n## 创作者附件（参考图）\n"
                + "\n".join(lines)
                + f"\n你看不到图的内容，但知道用户贴了 {len(atts)} 张参考图。"
                + "\n可以在 reply 里提一句「我看到你贴了 N 张参考图，到美术/分镜阶段我会让对应 agent 用它们」，"
                + "也可以根据创作者描述（如\"图 1 是参考的人物\"/\"图 2 是想要的服装\"）"
                + "把这些线索写进 outline.characters[*].description 或 outline.tone 里，"
                + "让美术 agent 后续生图时能精准用上。不需要把 URL 写进 outline。"
            )

        user_prompt = f"""## 最近对话历史
{history_block}

## 当前剧本大纲 (current_outline)
{current_outline_json}

## 创作者已透露的偏好
{hints_block}{attach_block}

## 创作者最新一条消息
{message}

请基于"当前剧本大纲"做增量更新，输出 JSON。
"""

        # 有附件 → vision 调用让导演真看见参考图；失败降级 text-only
        if attachments:
            ref_urls = [a.get("url") for a in attachments if a.get("url")]
            try:
                response = await self.llm_chat_with_vision(
                    text_prompt=user_prompt,
                    image_urls=ref_urls,
                    system=OUTLINE_REFINE_SYSTEM_PROMPT,
                    temperature=0.6,
                    json_mode=True,
                )
            except Exception as ve:
                import logging as _lg
                _lg.getLogger(__name__).warning(
                    "refine_outline vision 失败，降级 text-only: %s", ve,
                )
                response = await self.llm_generate(
                    prompt=user_prompt,
                    system=OUTLINE_REFINE_SYSTEM_PROMPT,
                    temperature=0.6,
                    json_mode=True,
                )
        else:
            response = await self.llm_generate(
                prompt=user_prompt,
                system=OUTLINE_REFINE_SYSTEM_PROMPT,
                temperature=0.6,
                json_mode=True,
            )

        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            # 兜底：抽 markdown json 块
            import re
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", response, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    return {
                        "reply": "我刚刚走神了，能再说一次吗？",
                        "outline": current_outline or {},
                        "ready_to_confirm": False,
                        "missing_aspects": ["LLM 输出解析失败"],
                        "raw_response": response[:500],
                    }
            else:
                return {
                    "reply": "我刚刚走神了，能再说一次吗？",
                    "outline": current_outline or {},
                    "ready_to_confirm": False,
                    "missing_aspects": ["LLM 未输出 JSON"],
                    "raw_response": response[:500],
                }

        # 规范化输出
        outline = data.get("outline") or current_outline or {}
        if not isinstance(outline, dict):
            outline = current_outline or {}

        return {
            "reply": (data.get("reply") or "").strip()[:200],
            "outline": outline,
            "ready_to_confirm": bool(data.get("ready_to_confirm", False)),
            "missing_aspects": data.get("missing_aspects") or [],
        }

    # ═══════════════════════════════════════════════════════
    # 0.7.0 专家模式：通读剧本 + 协商分集
    # ═══════════════════════════════════════════════════════

    @cloud_method
    async def parse_script_file(self, file_text: str, file_name: str) -> Dict[str, Any]:
        """0.7.0：通读用户上传的剧本/小说原文，提取结构化摘要。

        长文本策略：
        - ≤ 30k 字符 → 整段一次性 LLM
        - 超出 → 切 ~8k window 分块 summary → 二次 merge

        返回 dict（见 SCRIPT_PARSE_SYSTEM_PROMPT 输出 schema）。
        """
        text = file_text or ""
        if not text.strip():
            return self._normalize_script_summary({}, fallback_chars=0)

        if len(text) <= 30000:
            return await self._parse_script_single_pass(text, file_name)
        return await self._parse_script_chunked(text, file_name)

    async def _parse_script_single_pass(self, text: str, file_name: str) -> Dict[str, Any]:
        import logging as _lg
        _logger = _lg.getLogger(__name__)
        user_prompt = f"""## 文件名
{file_name}

## 文件原文（全部 {len(text)} 字符）
{text}

请按 system prompt 要求输出剧本摘要 JSON。"""
        try:
            response = await self.llm_generate(
                prompt=user_prompt,
                system=SCRIPT_PARSE_SYSTEM_PROMPT,
                temperature=0.4,
                json_mode=True,
                max_tokens=16384,  # 0.7.0 轮 2：raw_passages 占空间，调大避免截断
            )
            data = self._safe_parse_json(response) or {}
        except Exception as e:
            _logger.warning("parse_script_file 失败：%s", e)
            data = {}
        return self._normalize_script_summary(data, fallback_chars=len(text))

    async def _parse_script_chunked(self, text: str, file_name: str) -> Dict[str, Any]:
        """长剧本 chain-of-summarization：分块 → merge。"""
        import logging as _lg
        _logger = _lg.getLogger(__name__)
        chunks = self._split_long_script(text, max_chars=8000)
        _logger.info("剧本分块 parse：%d 块（共 %d 字符）", len(chunks), len(text))

        # 第一轮：每块出迷你 summary
        chunk_summaries: List[Dict[str, Any]] = []
        for i, chunk in enumerate(chunks, 1):
            prompt = f"""## 文件名 {file_name}（第 {i}/{len(chunks)} 块）
## 本块原文
{chunk}

只输出这块文本的迷你摘要 JSON（沿用 system schema，但 scene_outline 只覆盖本块的章节，est_total_min 只算本块）。"""
            try:
                resp = await self.llm_generate(
                    prompt=prompt,
                    system=SCRIPT_PARSE_SYSTEM_PROMPT,
                    temperature=0.3,
                    json_mode=True,
                    max_tokens=4096,
                )
                d = self._safe_parse_json(resp) or {}
                if d:
                    chunk_summaries.append(d)
            except Exception as e:
                _logger.warning("剧本块 %d parse 失败：%s", i, e)

        if not chunk_summaries:
            return self._normalize_script_summary({}, fallback_chars=len(text))

        # 第二轮：merge 各 mini summary 成一份总 summary
        merge_prompt = f"""## 文件名 {file_name}
## 共 {len(chunk_summaries)} 块迷你摘要
{json.dumps(chunk_summaries, ensure_ascii=False)[:25000]}

请合并为一份完整的剧本总摘要 JSON（同 system schema）：
- characters 去重合并（同名角色取信息更完整的）
- scene_outline 按章节顺序拼接
- est_total_min = 各块之和
- 全文 {len(text)} 字符，suggested_episode_count 按 5-15 分钟/集 估"""
        try:
            resp = await self.llm_generate(
                prompt=merge_prompt,
                system=SCRIPT_PARSE_SYSTEM_PROMPT,
                temperature=0.4,
                json_mode=True,
                max_tokens=16384,  # 0.7.0 轮 2：raw_passages 占空间
            )
            data = self._safe_parse_json(resp) or {}
        except Exception as e:
            _logger.warning("剧本 merge 失败，降级合并第一块的 summary：%s", e)
            data = chunk_summaries[0]
        return self._normalize_script_summary(data, fallback_chars=len(text))

    @staticmethod
    def _split_long_script(text: str, max_chars: int = 8000) -> List[str]:
        """按 "第X章" / "## " / "Chapter N" 章节硬切，否则按字数硬切。"""
        import re
        pattern = re.compile(
            r"\n+(?=(?:第[一二三四五六七八九十百千万0-9]+[章回集节]|##\s|Chapter\s+\d+))",
            re.IGNORECASE,
        )
        raw = pattern.split(text)
        # 没找到章节边界 → 按字数硬切
        if len(raw) == 1:
            return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]
        # 把短章节合并到 max_chars 上限内
        merged, cur = [], ""
        for chunk in raw:
            if len(cur) + len(chunk) <= max_chars:
                cur += chunk
            else:
                if cur:
                    merged.append(cur)
                if len(chunk) > max_chars:
                    # 单章节超长 → 硬切
                    merged.extend(chunk[i:i + max_chars] for i in range(0, len(chunk), max_chars))
                    cur = ""
                else:
                    cur = chunk
        if cur:
            merged.append(cur)
        return merged

    @staticmethod
    def _normalize_script_summary(data: Dict[str, Any], fallback_chars: int = 0) -> Dict[str, Any]:
        """保底各字段，避免下游崩溃。"""
        if not isinstance(data, dict):
            data = {}
        # 中文 3000 字 ≈ 1 分钟视频（保守估）
        est = data.get("est_total_min")
        if not isinstance(est, (int, float)) or est <= 0:
            est = max(1, round(fallback_chars / 3000)) if fallback_chars else 0
        sug_ep = data.get("suggested_episode_count")
        if not isinstance(sug_ep, int) or sug_ep <= 0:
            sug_ep = max(1, round(est / 10))  # 默认每集 ~10 分钟

        # 0.7.0 轮 2：scene_outline 每项规范化，确保 raw_passages 是 List[str]（缺则空数组）
        raw_outline = data.get("scene_outline") if isinstance(data.get("scene_outline"), list) else []
        norm_outline = []
        for s in raw_outline:
            if not isinstance(s, dict):
                continue
            passages = s.get("raw_passages")
            if not isinstance(passages, list):
                passages = []
            passages = [str(p).strip() for p in passages if isinstance(p, (str, int, float))]
            norm_outline.append({
                "chapter": str(s.get("chapter") or "").strip(),
                "summary": str(s.get("summary") or "").strip(),
                "key_events": s.get("key_events") if isinstance(s.get("key_events"), list) else [],
                "raw_passages": passages,
            })

        return {
            "logline": str(data.get("logline") or "").strip(),
            "genre": str(data.get("genre") or "").strip(),
            "themes": data.get("themes") if isinstance(data.get("themes"), list) else [],
            "tone": str(data.get("tone") or "").strip(),
            "characters": data.get("characters") if isinstance(data.get("characters"), list) else [],
            "scene_outline": norm_outline,
            "est_total_min": int(est),
            "suggested_episode_count": int(sug_ep),
            "suggested_aspect_ratio": str(data.get("suggested_aspect_ratio") or "9:16"),
            "notes": str(data.get("notes") or "").strip(),
        }

    @cloud_method
    async def breakdown_into_episodes(
        self,
        script_summary: Dict[str, Any],
        user_message: str,
        history: List[str],
        current_breakdown: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """0.7.0：与制作人协商分集（同 refine_outline 的多轮微调 pattern）。

        返回 {reply, episodes, ready_to_confirm, missing_aspects}。
        LLM 失败 / 输出不合规 → 按 scene_outline 平均切兜底。
        """
        import logging as _lg
        _logger = _lg.getLogger(__name__)

        recent_history = history[-6:] if history else []
        history_block = "\n".join(f"- {h[:200]}" for h in recent_history) or "（暂无）"
        summary_json = json.dumps(script_summary or {}, ensure_ascii=False, indent=2)[:8000]
        current_json = json.dumps(current_breakdown or [], ensure_ascii=False, indent=2)[:4000]

        user_prompt = f"""## 剧本摘要 (script_summary)
{summary_json}

## 当前分集草案 (current_breakdown)
{current_json}

## 最近对话历史
{history_block}

## 制作人最新一条消息
{user_message}

请基于"剧本摘要"和"当前分集草案"做增量更新，输出 JSON。
若 current_breakdown 为空，请基于 script_summary 起一个完整初版。"""

        try:
            response = await self.llm_generate(
                prompt=user_prompt,
                system=EPISODE_BREAKDOWN_SYSTEM_PROMPT,
                temperature=0.5,
                json_mode=True,
                max_tokens=8192,
            )
            data = self._safe_parse_json(response) or {}
        except Exception as e:
            _logger.warning("breakdown_into_episodes LLM 失败，按 scene_outline 均分兜底：%s", e)
            return self._fallback_breakdown(script_summary)

        episodes = data.get("episodes")
        if not isinstance(episodes, list) or not episodes:
            _logger.warning("breakdown_into_episodes 输出 episodes 为空，按 scene_outline 均分兜底")
            return self._fallback_breakdown(script_summary)

        # 规范化
        # 0.7.0 轮 1：拿到 script_summary 长度，clip indices
        outline_len = len((script_summary or {}).get("scene_outline") or [])
        char_count = len((script_summary or {}).get("characters") or [])

        def _normalize_indices(raw: Any, max_len: int) -> List[int]:
            """把 LLM 输出的 indices 数组校验为合法 int 数组：去重、排序、clip 到 [0, max_len)。"""
            if not isinstance(raw, list):
                return []
            result = []
            seen = set()
            for v in raw:
                try:
                    idx = int(v)
                except (TypeError, ValueError):
                    continue
                if idx < 0 or (max_len and idx >= max_len):
                    continue
                if idx in seen:
                    continue
                seen.add(idx)
                result.append(idx)
            return sorted(result)

        norm: List[Dict[str, Any]] = []
        for i, ep in enumerate(episodes, 1):
            if not isinstance(ep, dict):
                continue
            dur_raw = ep.get("est_duration_min", 8)
            try:
                dur = int(float(dur_raw)) if dur_raw not in (None, "") else 8
            except (TypeError, ValueError):
                dur = 8
            scene_indices = _normalize_indices(ep.get("scene_indices"), outline_len)
            character_indices = _normalize_indices(ep.get("character_indices"), char_count)
            norm.append({
                "ep_id": str(ep.get("ep_id") or f"EP{i:02d}").strip(),
                "title": str(ep.get("title") or f"第 {i} 集").strip(),
                "story_arc": str(ep.get("story_arc") or "").strip(),
                "scene_range": str(ep.get("scene_range") or "").strip(),
                "scene_indices": scene_indices,
                "character_indices": character_indices,
                "est_duration_min": dur,
                "cliff_hanger": str(ep.get("cliff_hanger") or "").strip(),
            })

        # 兜底：所有集都没给 scene_indices → 按集数均分 outline
        if outline_len > 0 and not any(ep.get("scene_indices") for ep in norm):
            n = len(norm)
            if n > 0:
                size = max(1, outline_len // n)
                for idx, ep in enumerate(norm):
                    start = idx * size
                    end = (idx + 1) * size if idx < n - 1 else outline_len
                    ep["scene_indices"] = list(range(start, end))
                _logger.warning("breakdown_into_episodes: LLM 没给 scene_indices，自动按 %d 集均分 %d 场景", n, outline_len)
        # 兜底：所有集都没给 character_indices → 给全角色（不能误删戏份）
        if char_count > 0 and not any(ep.get("character_indices") for ep in norm):
            for ep in norm:
                ep["character_indices"] = list(range(char_count))
            _logger.warning("breakdown_into_episodes: LLM 没给 character_indices，回退到每集全角色")

        return {
            "reply": str(data.get("reply") or "").strip()[:200],
            "episodes": norm,
            "ready_to_confirm": bool(data.get("ready_to_confirm", False)),
            "missing_aspects": data.get("missing_aspects") or [],
        }

    @staticmethod
    def _fallback_breakdown(script_summary: Dict[str, Any]) -> Dict[str, Any]:
        """LLM 失败 / 输出不合规时兜底：按 scene_outline 平均切。"""
        sug = (script_summary or {}).get("suggested_episode_count") or 3
        scenes = (script_summary or {}).get("scene_outline") or []
        n = max(1, int(sug))
        eps: List[Dict[str, Any]] = []
        char_count = len((script_summary or {}).get("characters") or [])
        all_chars = list(range(char_count))
        if scenes:
            size = max(1, len(scenes) // n)
            for i in range(n):
                start = i * size
                end = (i + 1) * size if i < n - 1 else len(scenes)
                slice_ = scenes[start:end]
                chap = " / ".join(str(s.get("chapter") or "?") for s in slice_)
                arc = "；".join(str(s.get("summary") or "") for s in slice_)[:200]
                eps.append({
                    "ep_id": f"EP{i + 1:02d}",
                    "title": f"第 {i + 1} 集",
                    "story_arc": arc,
                    "scene_range": chap,
                    "scene_indices": list(range(start, end)),
                    "character_indices": all_chars,
                    "est_duration_min": 8,
                    "cliff_hanger": "",
                })
        else:
            for i in range(n):
                eps.append({
                    "ep_id": f"EP{i + 1:02d}",
                    "title": f"第 {i + 1} 集",
                    "story_arc": "（待制作人补充）",
                    "scene_range": "",
                    "scene_indices": [],
                    "character_indices": all_chars,
                    "est_duration_min": 8,
                    "cliff_hanger": "",
                })
        return {
            "reply": "LLM 输出不合规，已按 scene_outline 平均切，请人工调整后再 confirm。",
            "episodes": eps,
            "ready_to_confirm": False,
            "missing_aspects": ["LLM 兜底，需人工调整"],
        }

    @staticmethod
    def _safe_parse_json(response: str) -> Optional[Dict[str, Any]]:
        """0.6.4：解析 LLM 输出 JSON，含 markdown 代码块兜底。失败返回 None。"""
        if not response or not response.strip():
            return None
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            import re
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", response, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    return None
            return None

    @cloud_method
    async def review_art(
        self,
        art_direction: Dict[str, Any],
        script: Dict[str, Any],
    ) -> Dict[str, Any]:
        """0.6.4：总导演 review 美术输出。
        返回 {art_direction: polish 后的, review_notes: str}。
        LLM 失败 / patch 全空 → 返回原数据 + 带前缀的 notes。
        """
        import copy
        import logging as _lg
        _logger = _lg.getLogger(__name__)
        if not isinstance(art_direction, dict):
            return {"art_direction": art_direction, "review_notes": "(input invalid, skipped)"}

        user_prompt = f"""## 剧本
{json.dumps(script, ensure_ascii=False, indent=2)[:6000]}

## 美术导演输出
{json.dumps(art_direction, ensure_ascii=False, indent=2)[:8000]}

请按 system prompt 要求 review 并输出 JSON patch。"""

        try:
            response = await self.llm_generate(
                prompt=user_prompt,
                system=DIRECTOR_REVIEW_ART_SYSTEM_PROMPT,
                temperature=0.4,
                json_mode=True,
            )
            data = self._safe_parse_json(response)
            if data is None:
                _logger.warning("review_art: JSON 解析失败，降级原数据。raw=%s", (response or "")[:300])
                return {"art_direction": art_direction, "review_notes": "(LLM JSON 解析失败，使用原数据)"}
        except Exception as e:
            _logger.warning("review_art LLM 调用失败，降级原数据: %s", e)
            return {"art_direction": art_direction, "review_notes": f"(LLM 调用失败:{str(e)[:120]}，使用原数据)"}

        polished = copy.deepcopy(art_direction)
        notes = (data.get("review_notes") or "").strip()

        chars_patch = data.get("characters_patch") or {}
        if isinstance(chars_patch, dict):
            for ch in polished.get("characters", []) or []:
                cid = ch.get("character_id")
                p = chars_patch.get(cid)
                if isinstance(p, dict):
                    for k, v in p.items():
                        if k in {"character_id", "name", "subject_type"}:
                            continue
                        if v not in (None, ""):
                            ch[k] = v

        scenes_patch = data.get("scenes_patch") or {}
        if isinstance(scenes_patch, dict):
            for sc in polished.get("scenes", []) or []:
                sid = sc.get("scene_id")
                p = scenes_patch.get(sid)
                if isinstance(p, dict):
                    for k, v in p.items():
                        if k in {"scene_id", "location"}:
                            continue
                        if v not in (None, ""):
                            sc[k] = v

        # 0.6.4: props patch
        props_patch = data.get("props_patch") or {}
        if isinstance(props_patch, dict):
            for pr in polished.get("props", []) or []:
                pid = pr.get("prop_id")
                p = props_patch.get(pid)
                if isinstance(p, dict):
                    for k, v in p.items():
                        if k in {"prop_id", "name", "subject_type"}:
                            continue
                        if v not in (None, ""):
                            pr[k] = v

        vs_patch = data.get("visual_style_patch") or {}
        if isinstance(vs_patch, dict) and vs_patch:
            polished.setdefault("visual_style", {}).update(vs_patch)
        sg_patch = data.get("style_guide_patch") or {}
        if isinstance(sg_patch, dict) and sg_patch:
            polished.setdefault("style_guide", {}).update(sg_patch)

        _logger.info(
            "review_art done: %d char / %d scene / %d prop patch, notes=%s",
            len(chars_patch), len(scenes_patch), len(props_patch), notes[:120],
        )
        return {"art_direction": polished, "review_notes": notes or "(未改动)"}

    async def review_storyboard(
        self,
        shots: List[Dict[str, Any]],
        script: Dict[str, Any],
        art_direction: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """0.6.4：总导演 review 分镜输出（只 polish 字段，不改数量）。
        返回 {shots: polish 后的(数量不变), review_notes: str}。
        LLM 失败 → 返回原 shots + 带前缀的 notes。
        """
        import copy
        import logging as _lg
        _logger = _lg.getLogger(__name__)
        if not isinstance(shots, list) or not shots:
            return {"shots": shots, "review_notes": "(input empty, skipped)"}

        # 0.6.5: 从 art_direction 抽出全部可引用项 + 扫描 shots 算覆盖率，
        # 显式告诉 LLM 哪些资产 0 个 shot 引用了（强迫它在 patch 里补 @）
        ad = art_direction or {}
        char_names = [c.get("name") for c in (ad.get("characters") or []) if c.get("name")]
        scene_names = [s.get("location") for s in (ad.get("scenes") or []) if s.get("location")]
        prop_names = [p.get("name") for p in (ad.get("props") or []) if p.get("name")]
        # 扫描 shots 找已引用名
        import re as _re
        mention_pat = _re.compile(r"@([\w一-鿿]+)")
        mentioned: set = set()
        for sh in shots:
            for f in ("visual_prompt", "video_prompt"):
                mentioned.update(mention_pat.findall(sh.get(f, "") or ""))
        def _split_aliases(nm):
            parts = [p.strip() for p in _re.split(r"[/／,，、]", nm or "") if p.strip()]
            return [nm.strip()] + parts
        def _is_covered(nm):
            return any(a in mentioned for a in _split_aliases(nm))
        uncov_c = [n for n in char_names if not _is_covered(n)]
        uncov_s = [n for n in scene_names if not _is_covered(n)]
        uncov_p = [n for n in prop_names if not _is_covered(n)]

        coverage_block = (
            f"## 必须 100% 覆盖的清单\n"
            f"- 角色（{len(char_names)} 个）：{char_names}\n"
            f"- 场景（{len(scene_names)} 个）：{scene_names}\n"
            f"- 道具（{len(prop_names)} 个）：{prop_names}\n\n"
            f"### 当前未覆盖（你必须在 patch 里补 @ 引用）\n"
            f"- 未覆盖角色：{uncov_c or '✓ 全部覆盖'}\n"
            f"- 未覆盖场景：{uncov_s or '✓ 全部覆盖'}\n"
            f"- 未覆盖道具：{uncov_p or '✓ 全部覆盖'}\n"
        )

        user_prompt = f"""## 剧本
{json.dumps(script, ensure_ascii=False, indent=2)[:5000]}

## 美术方案（参考一致性）
{json.dumps(art_direction or {}, ensure_ascii=False, indent=2)[:3000]}

{coverage_block}

## 分镜导演输出（共 {len(shots)} 个 shot）
{json.dumps(shots, ensure_ascii=False, indent=2)[:10000]}

请按 system prompt 要求 review 连贯性、节奏、是否抓重点、**覆盖率**（缺失的必须在 patch 里补 @ 引用），输出 JSON patch。"""

        try:
            response = await self.llm_generate(
                prompt=user_prompt,
                system=DIRECTOR_REVIEW_STORYBOARD_SYSTEM_PROMPT,
                temperature=0.4,
                json_mode=True,
                max_tokens=16384,  # 0.6.5: review 可能重写多个 shot patch（含主从尺寸括号），需要大 token 预算
            )
            data = self._safe_parse_json(response)
            if data is None:
                _logger.warning("review_storyboard: JSON 解析失败，降级原数据。raw=%s", (response or "")[:300])
                return {"shots": shots, "review_notes": "(LLM JSON 解析失败，使用原数据)"}
        except Exception as e:
            _logger.warning("review_storyboard LLM 调用失败，降级原数据: %s", e)
            return {"shots": shots, "review_notes": f"(LLM 调用失败:{str(e)[:120]}，使用原数据)"}

        polished = [copy.deepcopy(s) for s in shots]
        notes = (data.get("review_notes") or "").strip()

        _PROTECTED = {
            "shot_id", "scene_id", "shot_size", "camera_angle",
            "camera_movement", "requires_keyframe", "depends_on_assets",
        }

        shots_patch = data.get("shots_patch") or {}
        n_changed = 0
        if isinstance(shots_patch, dict):
            for sh in polished:
                sid = sh.get("shot_id")
                patch = shots_patch.get(sid)
                if not isinstance(patch, dict):
                    continue
                applied = False
                for k, v in patch.items():
                    if k in _PROTECTED:
                        continue
                    if v in (None, ""):
                        continue
                    if k == "duration_seconds":
                        try:
                            v = max(2, min(15, int(v)))
                        except (TypeError, ValueError):
                            continue
                    sh[k] = v
                    applied = True
                if applied:
                    n_changed += 1

        _logger.info(
            "review_storyboard done: %d/%d shots patched, notes=%s",
            n_changed, len(polished), notes[:120],
        )
        return {"shots": polished, "review_notes": notes or "(未改动)"}

    @cloud_method
    async def detect_confirm_intent(self, message: str) -> bool:
        """检测创作者是否在自然语言表达"剧本满意，开始下一步"。

        单次 LLM 调用，binary yes/no。便宜（max_tokens=4）。
        没命中时由 refine_outline 处理。
        """
        if not message or not message.strip():
            return False
        # 长消息一般是新需求，不可能是简短确认；先快速过滤
        if len(message.strip()) > 30:
            return False

        prompt = (
            "用户在与一位 AI 导演协商剧本，回复了下面这条消息。\n"
            "判断这条消息是否在表达「剧本已经满意，开始下一步制作」的意思。\n"
            "只回 yes 或 no，不要其他字符。\n\n"
            f"消息：{message.strip()}"
        )
        try:
            response = await self.llm_generate(
                prompt=prompt,
                system=None,
                temperature=0.0,
                json_mode=False,
            )
            return (response or "").strip().lower().startswith("y")
        except Exception:
            return False

    # ── 进阶模式聊天系统 prompt（override BaseAgent 默认）──

    def _chat_system_prompt(self) -> str:
        return """你是 Octopus 的导演 Agent，正在进阶模式下与创作者对话调度整个创作流程。

## 你的职责（区别于其他 agent）
- **跨阶段决策**：用户想"改剧本/换风格/换平台/改长度"等会影响下游的大改动时，由你判断
- **闲聊 / 状态查询 / 不属于其他 agent 的兜底**：由你回应
- **不要替美术/分镜/发布 agent 做事**：那些请交给对应专业 agent；router 路由错了你可以在 reply 里建议用户用 @<agent> 直连

## node_updates 规则
- 大改动（如改 collected_info.aspect_ratio / platform / 项目级风格）→ 找 director / script 节点 patch，should_rerun=true 并在 reply 警告"会影响下游"
- 闲聊 / 项目状态查询 / 没具体节点要改 → node_updates 为空数组，reply 给信息性回答（≤80 字）

## 输出格式（严格 JSON）
{
  "reply": "导演式简短回应（≤80 字）",
  "node_updates": [
    {"node_id": "...", "params_patch": {...}, "should_rerun": true, "reason": "..."}
  ]
}
"""
