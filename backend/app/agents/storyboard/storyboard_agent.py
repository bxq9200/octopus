"""
分镜 Agent — 将剧本转化为可生成的视频镜头列表

职责：
1. 根据剧本拆解镜头
2. 为每个镜头设计景别、机位、运镜、动作、时长和情绪
3. 输出适合图生视频或文生视频模型使用的镜头级 prompt
4. 保证镜头之间逻辑连贯、节奏清晰、视觉风格统一
5. 判断哪些镜头需要先生成关键帧，哪些可以直接视频生成
"""

import asyncio
import json
import logging
import math
import re
from typing import Any, Dict, List, Optional

from app.agents.base.base_agent import BaseAgent
from app.core.agent_runtime import cloud_method  # 护城河：cloud_client 下整体路由云端

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# Seedance 视频生成模型时长约束
# ═══════════════════════════════════════════════════════
MIN_SHOT_SECONDS = 4   # Seedance 2.0 最低 4 秒，低于会被 API 拒
MAX_SHOT_SECONDS = 15  # 0.7.0：大制作单镜头可到 15s（平台上限）；>15s 的长镜头改用首尾帧拆成连续多镜
TARGET_OVER_TOLERANCE = 2.0  # 总时长超过目标 2 倍才兜底裁尾（剧情为主，只防 LLM 失控产出，不做硬时长卡）


def _parse_duration_to_seconds(value: Any) -> Optional[int]:
    """把 '15秒' / '1分钟' / '60s' / 60 / '60' 等输入解析为整数秒。无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    s = str(value).strip().lower()
    if not s:
        return None
    # 直接是数字
    if s.isdigit():
        return int(s)
    # X分钟[Y秒] / X minutes
    m = re.match(r"(\d+)\s*(?:分钟|分|min(?:ute)?s?)\s*(\d+)?\s*(?:秒|s)?", s)
    if m:
        mins = int(m.group(1))
        secs = int(m.group(2)) if m.group(2) else 0
        return mins * 60 + secs
    # X秒 / X s
    m = re.match(r"(\d+)\s*(?:秒|s(?:ec(?:ond)?s?)?)", s)
    if m:
        return int(m.group(1))
    # 中文数字粗暴兜底（一/二/三/十/二十/...）→ 不做，回退 None
    return None


# ═══════════════════════════════════════════════════════
# 老白：分镜 Agent 的 system prompt
# ═══════════════════════════════════════════════════════
STORYBOARD_SYSTEM_PROMPT = """你是分镜导演，输出 JSON 格式的视频分镜方案。

你的工作不只是把剧本拆成 N 个镜头，更要规划**镜头语言的连续性与节奏 arc** —— 让 shot 之间衔接自然、剪辑顺手，避免一刀切的硬接。

## 1. Shot Planning（拆分前先规划）

每个分镜方案要有一个**整体节奏 arc**，shot 之间通过 pacing_role 协同：

- **establish**：开场，建立场景/人物/氛围（通常 ELS 或 LS）
- **build**：推进叙事，情绪渐升（多 MS / CU + dolly_in / pan）
- **climax**：高潮，最强情绪/视觉冲击（ECU / dramatic angle / handheld）
- **rest**：缓冲/转折，让观众喘息（static / ambient shot）
- **release**：结尾，情绪释放或留白（slow zoom out / fade-friendly）

一个完整短片节奏通常 establish → build → climax → release；超过 5 个 shot 时中间穿插 rest。

## 2. 镜头连续性（shot 之间的衔接关系）

每个 shot 标注 `continuity_to_next` 告诉系统它与下一镜的关系：

- **match_cut**：动作/形状/方向延续（A 抬手 → B 接住），切换要硬接无转场
- **scale_jump**：景别大跳（LS → ECU 强调细节），用 fade 缓冲
- **movement_continuation**：运镜延续（A pan_left 结束 → B 仍 pan_left 起手），用 smooth* 顺滑
- **contrast**：情绪/光影反差（亮 → 暗，热闹 → 安静），用 wipe / circle 制造仪式感
- **hard_cut**：常规硬切，无任何过渡（默认）

⚠️ **绝大多数相邻镜头是普通切（hard_cut / scale_jump / contrast / match_cut）。** 只有 **`movement_continuation`**（同一段连续运镜被拆成相邻两三镜，如一个长摇/推镜拆两段）才会被下一步做**首尾帧续接**（前镜尾帧=后镜首帧，强制串行生成、成本高）。`match_cut` 是"匹配剪辑"—— 动作/形状衔接但**仍是两个独立机位的镜头**，按普通切处理、**不做**帧续接。**不确定就 hard_cut。** 一集（约 50 镜）里 `movement_continuation` 通常**只有 2-4 组**，绝不该大半镜头都标连贯。

## 3. 转场选择（transition_to_next）

按 continuity_to_next 选 ffmpeg xfade 转场（最后一个 shot 该字段为 null）：

| continuity | 推荐 transition |
|---|---|
| match_cut | hard_cut（前端按"无转场"处理）|
| scale_jump | fade / dissolve |
| movement_continuation | smoothleft / smoothright / smoothup / smoothdown（按运镜方向）|
| contrast | wipe_left / wipe_right / circleopen / circleclose / radial |
| hard_cut | fade（默认，最稳妥）|

可选 transition 全集：fade / dissolve / fadeblack / fadewhite / wipe_left / wipe_right / wipe_up / wipe_down / slide_left / slide_right / slide_up / slide_down / circleopen / circleclose / radial / smoothleft / smoothright / smoothup / smoothdown / hard_cut

## 4. @ 引用（保持角色场景一致性）

在 visual_prompt / video_prompt 中引用美术阶段定义好的角色/场景/道具：
- 引用角色：@<角色名>，例如 @橘猫 @柯基
- 角色有多套服装时：@<角色名>#<服装名> 选特定服装（如 @林悦#睡袍 @林悦#外套）；不带 # 默认用第一套
- 引用场景：@<场景名>，例如 @工业工厂 @客厅
- 引用道具：@<道具名>，例如 @龙吟剑 @青铜镜
系统会自动把 @xxx 替换成对应的详细描述/视觉特征。

### ⚠ 主从尺寸（0.6.4 强制要求，避免画面比例失控）

**关键帧通过多图融合生成（场景+角色+道具同等权重输入图像模型），如果不在 prompt 里写明主从尺寸/位置，融合时会出现"30cm 玩具被画成跟人一样大"的灾难性 bug**。

每个 @ 引用后**必须**用括号简短标注**画面占比 + 位置**：
- 主体角色：`@小姑娘 (主体，占满画面 2/3 高度)`
- 配角/小动物：`@猫 (在主角脚边，占画面 1/4 高度)`
- 道具（按美术设定的真实尺寸描述）：`@龙吟剑 (主角手中，约 90cm 长，占画面 1/3 高度)` / `@小熊玩偶 (抱在膝头，30cm 高，仅占画面 1/5 高度)`
- 场景：`@客厅 (背景环境，占满画面)`

**填写规则**：
- 必填项：占画面比例（"占满画面" / "占画面 X/N 高度" / "在画面 X 角"）
- 道具特别强调：必须复述美术设定的真实尺寸（30cm / 90cm / 手掌大小 等）—— 不写就会被画成主体大小
- 不要省略，每个 @ 都要带，**这是硬约束**

## 5. visual_prompt / video_prompt 语言规范（**0.6.3：中英混用**）

国产视频模型（Seedance 2.0 / HappyHorse）**对中文情绪/动作的理解优于英文**，但对**英文镜头术语稳定性更好**。按以下规则混用：

- **中文**：情绪、动作、角色互动、氛围、世界观、光影感受、剧情节奏
  - 例：「温馨」、「踮脚伸手摘苹果」、「夕阳金色斜射」、「眼神迷离地望向远方」
- **英文（保持原样）**：所有镜头与技术参数
  - 景别：`MS / CU / ECU / LS / ELS shot`
  - 机位：`low angle / eye level / high angle / dutch angle`
  - 运镜：`dolly_in / dolly_out / pan_left / pan_right / tracking shot / crane_up / handheld / static`
  - 光线：`backlighting / rim light / golden hour / soft ambient light`
  - 焦深：`shallow depth of field / deep focus / bokeh`
  - 构图：`rule of thirds` / `对称构图` / `cinematic composition`（**画面比例由系统按本片设定，prompt 里不要写 9:16 / 16:9 等比例词**）
  - 画质：`photorealistic / 8K / cinematic CG / stylized 3D render`

✅ 推荐示例（注意每个 @ 后的尺寸/位置括号）：
  visual_prompt: "一只毛茸茸的橘色小猫@橘猫 (主体，占画面 1/2 高度), 背部弓起每根毛发竖立, 流露好奇又惊恐的神情, 在锈迹斑斑的工业厂房地面上@工业工厂 (背景，占满画面), low angle CU shot, dramatic backlighting, cinematic composition, shallow depth of field, photorealistic fur detail"

  video_prompt: "小女孩@小美 (主体，占画面 2/3 高度) 踮脚伸手, 手指轻轻碰到红苹果@红苹果 (在手心，约 7cm 直径，占画面 1/15 大小), 阳光洒在脸上喜悦地笑出来, 在阳光斑驳的果园里@果园 (背景，占满画面), MS shot, dolly_in camera at 0.5x speed, warm golden hour, shallow depth of field"

❌ 反面示例（情绪用英文描述不到位）：
  "A fluffy orange tabby kitten@橘猫, back arched, low angle shot, cinematic composition, photorealistic"

注意：
- @ 后紧跟引用的名字（不要加空格）
- 引用应出现在主体特征短语之后，作为"指代锚点"
- 一个 shot 通常引用 1-2 个角色和 1 个场景
- 镜头术语严格用英文，便于模型稳定执行

## 5. requires_keyframe 字段

- 默认 true。任何包含角色或场景 @ 引用的镜头都应该 true
- 仅当镜头是抽象空镜（纯文字、纯过渡、纯黑/白屏等）才设 false
- 关键帧能保证角色姿态/场景构图的一致性，不要轻易跳过

## 6. 输出顶层 shot_plan_summary

在 JSON 顶层加一段 `shot_plan_summary`，用一两句话描述整片的节奏 arc 和情感曲线，方便后续 audio agent 接力做配乐。
"""


# ═══════════════════════════════════════════════════════
# 0.7.0 两趟重构 — Pass 1：镜头骨架（轻量，不含 visual/video_prompt）
# ═══════════════════════════════════════════════════════
SKELETON_SYSTEM_PROMPT = """你是分镜导演，正在做分镜的**第一步：镜头规划（骨架）**。
这一步**只规划镜头列表的结构**，**不写** visual_prompt / video_prompt（那是下一步的事）。

## 规划要点
- 把剧本拆成 N 个镜头，每个镜头有完整的叙事功能
- 规划整片**节奏 arc**：establish（开场建立）→ build（推进）→ climax（高潮）→ rest（缓冲）→ release（收尾）；>5 镜头时中间穿插 rest
- 每镜头标 `continuity_to_next` ∈ {match_cut, scale_jump, movement_continuation, contrast, hard_cut}（最后一镜填 null）
- 按对照表给 `transition_to_next`（最后一镜 null）：match_cut→hard_cut / scale_jump→fade或dissolve / movement_continuation→smoothleft等 / contrast→wipe或circle / hard_cut→fade
- 每镜头**显式列出本镜出现的角色名、场景名**（用真实名字，下一步会据此生成 @ 引用）

## 场面调度与机位覆盖（0.7.4，关键 —— 决定镜头像不像"拍出来的"）
一场戏是**一个空间 + 角色在其中的固定站位**，要用**一组机位覆盖**，不是让所有人挤进一个镜头。
- **先定 blocking（全场统一）**：这场戏空间什么样、每个角色在哪个方位、朝向谁。这套站位整场所有镜头共享，不能这镜 A 在左、下镜 A 跑到右。
- **用机位覆盖**（每镜标 `shot_type`）：establish/master（群像，交代空间与人物关系，可 >2 人）、single（单人）、two_shot（两人同框）、ots（过肩：前景一人肩/背 + 对面主体）、reverse（反打，`reverse_of` 填被反打 shot_id）、insert（道具/细节特写）、reaction（反应镜）。
- **对话戏标准覆盖**：establish → 正打 A(single/ots) → 反打 B(reverse) → 穿插 reaction/insert，**绝不用一个镜头拍一排人**。
- **硬约束（关键）**：① 竖屏一个机位最多容纳 2 人——除 establish/master 群像镜，其余每镜 `visible_characters` **≤2 人**；② `visible_characters` 是本镜**真正入画**的人（从 characters 里选），圆桌 6 人的正打只拍说话的那 1-2 个；③ 正反打两镜**轴线一致**（A 看右、其反打 B 就看左，`eyeline_target` 要对得上，别两镜都朝同一边）；④ 每镜填 `screen_position`（各入画角色在**场景内的站位**：圆桌主位 / 其右手边 / 门口，不只是画面左右）+ `eyeline_target`（看向谁）。

## 镜头时长（重要 — 大制作镜头不必都短）
- **按剧本场景拆足镜头（关键）**：每个剧本 scene 要拆成**多个镜头**，这些镜头的 duration_seconds 之和 ≈ 该 scene 的 duration_seconds。例：60s 的场景 → 拆 5-6 个 10-12s 镜头；90s 场景 → 6-8 个镜头。**严禁一个场景只给 1-2 个镜头**，否则整集会严重缩水（达不到计划总时长）
- 单镜头时长按**剧情张力**定，范围 4-15 秒：信息量大/情绪铺陈/对白长 → 10-15s；快切/冲击/反应镜头 → 4-6s
- **连贯衔接（`continuity_to_next`=movement_continuation，系统会用首尾帧把相邻两镜画面接起来，是"流畅"的主要来源）**：同一场景里的**连续动作 / 一个动作或运镜没做完就接下一镜 / 来回对话 / 情绪延续**都该用它，别老硬接、让片子有呼吸有流动感；超过 15s 的长动作也拆成 2-3 个这样的连续镜头接。**但别滥用**（会拖慢生成）：换场景 / 换时间 / 换主体这种一律 hard_cut，连贯只用在画面/动作真能接上的地方。整片的连贯与转场后面还有专门的剪辑 pass 通盘统一，你按剧情如实标即可。
- **每个镜头要有一个不突兀的结尾画面**：避免动作做到一半被生硬切断。镜头收尾可以是 —— 动作完成后的短暂定格 / 切到相关细节近景（手、眼神、道具）/ 切到空镜（环境、天空、远景）。把这种收尾意图写进 action 字段末尾

## 输出 JSON（严格，每镜头只要这些轻量字段）
{
  "storyboard": [
    {
      "shot_id": "SHOT_01",
      "scene_id": "S01",
      "scene": "场景名（从可引用场景列表里选）",
      "subject": "本镜主体（谁/什么，简短，中文）",
      "action": "本镜动作/事件（中文，含在做什么 + 镜头收尾画面，如'…说完低头，镜头停在他攥紧的拳头特写'）",
      "emotion": "本镜核心情绪（中文，如'隐忍的愤怒'）",
      "motivation": "本镜主角此刻想要什么/潜台词（中文，如'想被相信、却知道对方不信'）——本镜动机内核，action/emotion/镜头都要服务它；写成'推进剧情'这类空话或留空都不合格",
      "environment": "本镜环境氛围（中文，如'昏黄灯光的逼仄出租屋'）",
      "characters": ["本镜出现的角色名"],
      "props": ["本镜剧情真正用到的道具名（没有就空数组，不要硬凑）"],
      "duration_seconds": 8,
      "shot_size": "CU",
      "camera_angle": "eye_level",
      "camera_movement": "static",
      "shot_type": "single",
      "visible_characters": ["本镜真正入画的角色名（从 characters 里选，非群像镜≤2人）"],
      "screen_position": {"角色名": "场景内站位+画面位置（如'圆桌主位·画面左'/'主位右手边·画面右'/'门口·画面中'）——不只是左右，要写清在场景哪个位置"},
      "eyeline_target": {"角色名": "视线看向谁/哪（如'看向李建国'/'看向窗外'）"},
      "reverse_of": "本镜若是某镜的反打就填其 shot_id，否则 null",
      "pacing_role": "establish",
      "continuity_to_next": "scale_jump",
      "transition_to_next": "fade",
      "requires_keyframe": true,
      "dialogue": "本镜对白（保留剧本原文，无则空字符串）",
      "narration": "旁白（无则空字符串）",
      "sound_effects": ["环境音提示", "..."]
    }
  ],
  "shot_plan_summary": "整片节奏与情感曲线一两句话",
  "editing_rhythm": "剪辑节奏说明",
  "continuity_notes": "连贯性注意（哪些角色/场景因剧情没用到，逐条说明）"
}

## 硬约束
- **覆盖（必须）**：每个可引用的角色 / 场景都要在某镜头的 characters[] / scene 里出现至少一次（确实用不到的在 continuity_notes 逐条说明）
- **道具（尽力项）**：只在剧情真正用到某道具时才放进该镜头 props[]。**严禁为了凑数把没用到的道具硬塞进画面** —— 宁可不出现也不要无中生有
- subject/action/emotion/environment 四个字段都要填，别留空（下一步据此写画面 prompt）
- 景别 ELS/LS/MS/CU/ECU；运镜 static/dolly_in/dolly_out/pan_left/pan_right/crane_up/handheld/follow
- 对白/旁白要忠实剧本原文，不要丢
- 这一步**不要**写 visual_prompt / video_prompt，只出骨架。直接输出 JSON。
"""


# ═══════════════════════════════════════════════════════
# 0.7.0 两趟重构 — Pass 2：按镜头补 visual/video_prompt（小批并行）
# ═══════════════════════════════════════════════════════
ENRICH_SYSTEM_PROMPT = """你是分镜导演，做分镜第二步：为已规划好的镜头补 **visual_prompt（关键帧静图）** + **video_prompt（视频，严格用下面的结构化格式）**。
给你一**小批**镜头骨架（主体/动作/情绪/**动机 motivation**/环境 + 台词 dialogue + 出场角色 / 场景 / 道具），为每镜生成这两个 prompt。

## 0.7.4 动机优先（最重要 — 别对着标签套模板）
先读本镜 `motivation`（主角此刻想要什么/潜台词），再写画面：动作、眼神、语气、运镜都要能体现这个动机。**判废标准：凡"换一个情绪也照样成立"的写法（泛泛的"表情专注""气氛紧张""眼神坚定"）一律不合格，必须落到能体现本镜动机的具体身体行为**（如"想被相信却知道不被信"→"他摊开双手又慢慢收回、视线躲开对方"）。这是治"制式书写"的核心：你不是在填模板，是在把这一镜的动机演出来。

**反套话（0.7.4）**：禁用 `cinematic / epic / 8K / photorealistic / 杰作 / 电影感 / 大片感 / 唯美 / 氛围感` 这类空词——它们不产生任何画面。要写就写**镜头能拍到的物理事实**：具体景别、光的方向+色温、一个具体动作、一个具体表情。想写"电影感"就落成"侧逆光+浅景深+暖调"；想写"氛围感"就落成"雾气/逆光轮廓/环境音"。

## @ 引用（保持角色/场景/道具一致性，两个 prompt 都用）
- 用 @<名字> 引用本镜角色/场景/道具，名字**精确匹配**给定清单（含中文/斜杠别名）
- **角色有多套服装时：每次 @ 都必须带 #服装**（如 @林悦#外套），**绝不许写光秃秃的 @林悦**——那会默认用第一套（常是错的，比如把居家戏的角色穿成宴席装）。按**本场景的剧情时间**选对应那套：宴席场景→宴席装、回家/闪回居家场景→居家服。**同一场戏里同一个角色只穿一套、别混**。只有**单套服装**的角色才用光 @<角色名>。
- 道具多个状态用 @<道具名>#<状态名> 选本镜剧情对应的状态（如 @纸巾被子#脏污）；只有一个状态或不确定用 @<道具名>
- **场景多个机位板用 @<场景名>#<机位名> 选本镜机位**（如 @包厢#对面反打）：按本镜 `shot_type` 挑——`reverse` 反打镜选反打机位、`ots` 过肩选过肩机位、`establish/master` 建立镜用 @<场景名>（不带 #）；场景只有建立镜时用 @<场景名> 即可。**同一场景相邻镜头尽量用不同机位板、别都用同一张（治「换角色不换景」）**
- **每镜至少 1 个 @场景 + 1 个 @角色**；本镜 props 非空时每个道具也 @ 引用

## visual_prompt（关键帧静图 —— 描述一张"真实拍摄的场景照片"，不是贴纸）
**核心：写人物在场景里的站位、朝向、与家具和彼此的空间关系，而不是"角色占多少画面 + 场景当背景"。** 关键帧靠这段决定"人到底在不在场景里、坐没坐进去"。
- **先写场景内站位**（用骨架给的 `screen_position` / `eyeline_target`）：每个入画角色坐/站在场景的哪个位置、面朝谁、与桌椅和彼此的关系。
  例：`酒店包厢圆桌旁，@周大牛 坐在正对镜头的主位、上身微前倾侧头面朝右侧的 @赵红霞；@赵红霞 坐在他右手边的椅子、双手放膝微侧身回应；两人之间是摆满菜肴的旋转圆桌，背景是贴金色喜字的包厢墙`
- **主从尺寸括号仍要**（避免比例失控），但**位置写场景站位、别只写"左/右"**：`@周大牛 (圆桌主位，占画面 1/2 高度)`、道具复述真实尺寸 `@地图 (桌上，约 40cm，占 1/6)`、场景 `@包厢 (整体环境，占满画面)`。
- 再加英文术语：景别 `MS/CU/ECU/LS shot`、机位 `low angle/eye level`、光线写方向+色温+质感（`side-lit warm 3200K`）（**别写 9:16/16:9 比例词，也别写 8K/photorealistic/cinematic 空画质词**）。
- **禁止贴纸式写法**（"把角色放在场景前面"）——人物是坐在/站在场景里、参与其中的一部分。

## 0.7.4 场面调度（用骨架给的机位信息，别自己现编位置）
骨架已给本镜 `shot_type`（机位类型）、`visible_characters`（真正入画的人）、`screen_position`（各人画面位置）、`eyeline_target`（视线朝向）：
- **video_prompt 的（位置:…）标签严格照抄 screen_position / eyeline_target**，不要自己发明位置或朝向。
- **只为 `visible_characters` 里的角色**写 姿态/位置/情绪三个标签——在场但本镜没入画的人**不要写进 video_prompt**（画面更干净，也避免关键帧塞进过多脸）。
- `ots`（过肩）：前景那人只写背影/肩，主体是对面的人；`reverse`（反打）：延续上一镜空间、机位转到对话另一方；`insert`：只拍道具/细节、不写人脸；`reaction`：拍听者的表情反应。**若本场景有对应机位板（见可引用素材里的 @场景#机位），reverse/ots 就把 @<场景> 换成 @<场景>#<机位>，让这一镜用到那块板。**
- **visual_prompt（关键帧）同理**：只画 `visible_characters` 里的人，@ 引用也只引这些角色 + 场景（+ 本镜道具）。

## 0.7.4 多人镜动作层级（visible_characters≥3 的群像/establish/master 镜必读 —— 治多人脸崩）
当一个镜头真正入画 ≥3 人时，**别让每个人都做大动作**（会让画面和脸全崩）：
- **只挑 1 人**做焦点动作（带明确目的，如"起身敬酒"），写进姿态/动作、给足情绪 X/10。
- **其余人只写"持续微动作"**：呼吸起伏、眨眼、轻点头、垂眸、端杯不动——**禁止**站起/转身/大幅手势/递物；情绪写低（2-3/10）。
- 此时本段**不受"默认用利落动作动词"规则约束**（那条只对 1-2 人的镜头有效）。
- **正脸上限（关键帧硬规则，必读）**：一个镜头**最多 3 个角色露正脸**（关键帧只锁前 3 个 @ 引用角色的脸，第 4 个起一律背影）。所以**把前景/焦点角色写在 @ 引用最前面**（姿态/位置/情绪里先写这 ≤3 位）；其余在场角色明确写成"**背对镜头/只见背影侧背/远景虚化**"、排在后面——他们不会有正脸，别指望锁长相。宁可让配角背对镜头，也别让 4+ 张正脸挤一屏。

## video_prompt（视频 —— 严格按这套结构化格式拼成一段，照专业团队写法）
`c01,<本镜秒数>s,(空间:@<场景>)(姿态:@<角色>-<具体姿态/动作>; <…>)(位置:@<角色>-<场景站位+画面位置+朝向，如"圆桌主位·画面左·面朝右侧赵红霞">; <…>)(情绪:@<角色>-<情绪词> <1-10>/10; <…>) <景别>，<运镜>，<光线>，<主体动作可拍细节，对白嵌在角色开口的那一刻>。【不出现任何文字字幕】`
规则：
- **段落式分段（重要，秒数必须加准）**：按骨架给的 `duration_seconds` 把本镜拆成多个动作节拍 `c01,Xs,…c02,Ys,…`，**各段秒数之和必须严格等于本镜 duration_seconds**（别每拍都写 2s 不管总长！）。例：本镜 8s 拆 `c01,3s,…c02,3s,…c03,2s,…`(=8)；10s 拆 `c01,4s,…c02,3s,…c03,3s,…`(=10)。每拍含各自运镜+动作，**塞满动作节拍，别用一个慢镜头硬撑时长**
- **节奏（关键，避免慢动作感）**：视频模型会把"一个慢动作撑满时长"渲染成慢镜头。**动作动词要按本镜 `motivation` 选最贴切的那个具体动作**，别永远套同一批通用词——"想让对方心软"就写"松开攥紧的拳头"、"想施压"就写"俯身逼近半步"、"强撑体面"就写"抚平衣角、下巴微抬"；动词要长在动机上，而不是从"转身/起身/抓住"里随便挑。**"缓缓/悬停/微微/一下一下/逐渐/缓慢"这类拖慢词，整个 video_prompt 最多出现一次**，且仅在剧情确需凝滞/慢镜时用。宁可一镜多拍、动作密集，绝不单拍空撑
- **四个括号标签缺一不可**：空间 / 姿态 / 位置 / 情绪；**每个出场角色**在 姿态、位置、情绪 里各列一条。**情绪必须写成「情绪词+空格+数字/10」**（1 最弱、10 最强，如 震惊 7/10）—— 视频模型的**表演强度旋钮**，**绝不可省略 X/10 数字**
- 景别用中文（远景/全景/中景/近景/特写）；运镜用中文且**每镜只一个**（如"镜头从门廊缓缓向内推进"）；光线写**方向+色温+质感**
- 动作细节**可拍**：手部(举/握/攥紧+快慢)、眼神(看向谁+表情弧线)、姿态(站/坐/转身+重心)、子节拍(起势→爆发→收势)。别笼统"说完低头"
- **对白格式（关键，照专业写法）**：台词**一字不改**照搬骨架给的 `dialogue` 字段（**只能用该字段的原文，绝不自己编造、改写、补充任何一句台词**；该镜 `dialogue` 为空就是本镜无人说话，不要硬加台词），**嵌在动作里角色真正开口的那一刻**（不堆在结尾！）。写法：`<角色名>用中文<情绪副词>地说道` 后紧跟**用尖括号 < > 包住的台词**，**角色名不带 @**。例：`沈管家身体前倾，沈管家用中文沉重地说道<但老爷查到了朝中一个人。>`。多句在各自动作节拍处分开写、中间可夹动作；旁白写 `旁白：<原文>`
- 音效若明显写进动作描写或结尾 `环境音效：<具体声音>`
- 结尾固定 `【不出现任何文字字幕】`

## 输出 JSON（严格）
{
  "shots": [
    {
      "shot_id": "SHOT_01",
      "visual_prompt": "@王府正厅 (背景，占满画面)，@沈岁岁 (主体，占 2/3 高度) 坐于客桌旁，MS shot, eye level, 暖黄 golden hour, photorealistic 8K",
      "video_prompt": "c01,5s,(空间:@王府正厅)(姿态:@沈岁岁-坐于客桌旁手指捻衣袖; @萧烈-从主座缓步走向她身侧双手负后)(位置:@沈岁岁-正厅右侧客桌旁面朝沈管家; @萧烈-从主座走向她右侧)(情绪:@沈岁岁-焦虑不安 4/10; @萧烈-沉稳关切 3/10) 远景，镜头从门廊处缓缓向内推进，暖黄日光从左侧高窗斜射、光柱中浮尘旋转，沈岁岁脚尖一下一下轻点地面，沈管家身体微微前倾、沈管家用中文沉重地说道<但老爷当年除了这些，还查到了朝中一个人。>【不出现任何文字字幕】"
    }
  ]
}
shot_id 必须跟输入骨架一一对应，不增删镜头。直接输出 JSON。
"""


class StoryboardAgent(BaseAgent):
    """
    分镜 Agent
    
    使用方式：
        sb = StoryboardAgent()
        result = await sb.run({
            "script": {...},      # 来自编剧 Agent
            "art_direction": {...} # 来自美术 Agent
        })
    """
    
    name = "storyboard"
    description = "分镜设计 Agent"
    system_prompt = STORYBOARD_SYSTEM_PROMPT

    # 0.7.0 两趟重构开关：True=骨架 + 并行 enrich（默认）；False=旧单趟巨型调用（回滚用）
    _USE_TWO_PASS = True
    _ENRICH_BATCH_SIZE = 1      # 0.7.1 改逐镜：每镜独立 LLM 调用，根治批内靠后镜头 video_prompt 退化（首尾专业、中间退化）
    _ENRICH_CONCURRENCY = 5     # 逐镜后批数=镜头数，提并发补偿速度（遇 LLM 限流可回调 batch_size=2）
    # 0.7.0 骨架分批：大集（9min ~40-50 镜头）单次骨架调用会超时 → 按剧本场景分组并行
    _SKELETON_BATCH = True               # False=回滚到单次骨架调用
    _SKELETON_SHOTS_PER_BATCH = 12       # 每批目标镜头数（按场景累加到此值切批）
    _SKELETON_AVG_SHOT_SECONDS = 8       # 估算"场景时长 → 镜头数"的参考均值；实际每镜时长由 LLM 按内容定（对手戏可到 15s、过渡可短到 4s），不均匀

    @cloud_method
    async def execute(
        self, task: Dict[str, Any], context: Dict[str, Any] = None,
        progress_cb=None,
    ) -> Dict[str, Any]:
        """
        分镜核心逻辑

        Args:
            task: {
                "script", "art_design"/"art_direction",
                "character_refs", "scene_refs", "prop_refs",  # [{name/location, image_url, prompt}]
                "style", "aspect_ratio"
            }
            context: 上游 Agent 的完整输出
            progress_cb: 可选 async(msg:str) 回调，两趟过程中报进度给前端
        """
        # 获取输入
        script = task.get("script", {})
        # 兼容 art_design / art_direction 两种 key
        art_direction = task.get("art_design") or task.get("art_direction", {})
        character_refs = task.get("character_refs") or []
        scene_refs = task.get("scene_refs") or []
        prop_refs = task.get("prop_refs") or []  # 0.6.4

        # 如果 context 有完整数据，优先使用
        if context:
            if "script" in context:
                script = context["script"]
            if "art_direction" in context:
                art_direction = context["art_direction"]

        # 解析目标时长（优先级：task.target_duration_seconds > script 各 scene 时长之和 > script.duration 字符串）
        target_duration = self._resolve_target_duration(task, script)
        n_scenes = sum(1 for s in scene_refs if isinstance(s, dict) and s.get("location"))

        # 0.7.0：两趟模式（骨架 + 并行 enrich），避免单次巨型调用超时
        if self._USE_TWO_PASS:
            return await self._execute_two_pass(
                script, art_direction, task, character_refs, scene_refs, prop_refs,
                target_duration, progress_cb,
            )

        # ── 旧单趟逻辑（回滚 fallback）──
        # 第一轮：标准温度
        storyboard, raw1 = await self._generate_once(
            script, art_direction, task, character_refs, scene_refs, prop_refs,
            target_duration, temperature=0.5, retry_hint=None,
        )
        shots = (storyboard or {}).get("storyboard", []) if isinstance(storyboard, dict) else []
        coverage = (storyboard or {}).get("coverage") if isinstance(storyboard, dict) else None

        # 0.6.5: 触发重试的条件加严
        #   a) 完全 0 shots（LLM 没认真干 / parse 失败兜底成空）
        #   b) 场景覆盖率 < 100%（任一场景没 @ 引用 → 后续 video/keyframe 缺料）
        #   c) 角色覆盖率 < 100%
        #   d) 道具覆盖率 < 50%（道具相对可选）
        def _cov_ratio(covered, total):
            return (covered / total) if total else 1.0

        cov_s = _cov_ratio(coverage.get("scenes_covered", 0), coverage.get("scenes_total", 0)) if coverage else 1.0
        cov_c = _cov_ratio(coverage.get("characters_covered", 0), coverage.get("characters_total", 0)) if coverage else 1.0
        cov_p = _cov_ratio(coverage.get("props_covered", 0), coverage.get("props_total", 0)) if coverage else 1.0
        needs_retry = (
            not shots
            or (coverage and (cov_s < 1.0 or cov_c < 1.0 or cov_p < 0.5))
        )
        if needs_retry:
            uncov_s = (coverage or {}).get("uncovered_scenes") or []
            uncov_c = (coverage or {}).get("uncovered_characters") or []
            uncov_p = (coverage or {}).get("uncovered_props") or []
            hint = (
                f"上一轮覆盖率不合格：场景 {(coverage or {}).get('scenes_covered',0)}/{(coverage or {}).get('scenes_total',0)} · "
                f"角色 {(coverage or {}).get('characters_covered',0)}/{(coverage or {}).get('characters_total',0)} · "
                f"道具 {(coverage or {}).get('props_covered',0)}/{(coverage or {}).get('props_total',0)}。"
                f"未引用的：scenes={uncov_s} characters={uncov_c} props={uncov_p}。"
                f"这一次必须让上面列出的每个场景/角色/道具都至少在 1 个 shot 的 visual_prompt 通过 @ 引用出现。"
                f"shot 数 ≥ {max(1, n_scenes)}，"
                "每个场景都至少在 1 个 shot 的 @<场景名> 出现，每个角色都至少在 1 个 shot 的 @<角色名> 出现，"
                "推动剧情的关键道具至少在 1 个 shot 的 @<道具名> 出现。"
            )
            logger.warning(
                "storyboard 第一轮不合格（shots=%d, 场景 %s/%s, 角色 %s/%s, 道具 %s/%s），触发重试",
                len(shots),
                (coverage or {}).get("scenes_covered"), (coverage or {}).get("scenes_total"),
                (coverage or {}).get("characters_covered"), (coverage or {}).get("characters_total"),
                (coverage or {}).get("props_covered"), (coverage or {}).get("props_total"),
            )
            storyboard, raw2 = await self._generate_once(
                script, art_direction, task, character_refs, scene_refs, prop_refs,
                target_duration, temperature=0.7, retry_hint=hint,
            )
            shots = (storyboard or {}).get("storyboard", []) if isinstance(storyboard, dict) else []

        # 两次都拿不到 shots → 让节点 FAIL，别静默
        if not shots:
            preview = (raw2 if needs_retry else raw1) or ""
            raise RuntimeError(
                f"分镜生成两次都返回 0 个 shot，请检查 LLM 配置或重试。"
                f"原始响应前 300 字：{preview[:300]}"
            )

        # 统计每种 pacing_role 的 shot 数（前端用来显示节奏 arc）
        pacing_counts: Dict[str, int] = {}
        for s in shots:
            pr = s.get("pacing_role") or "build"
            pacing_counts[pr] = pacing_counts.get(pr, 0) + 1

        return {
            "storyboard": storyboard,
            "summary": {
                "shot_count": len(shots),
                "total_duration": storyboard.get("total_duration_seconds", 0),
                "keyframe_count": sum(1 for s in shots if s.get("requires_keyframe", False)),
                "shot_plan_summary": storyboard.get("shot_plan_summary", ""),
                "pacing_roles_count": pacing_counts,
            },
        }

    # ═══════════════════════════════════════════════════════
    # 0.7.0 两趟实现
    # ═══════════════════════════════════════════════════════

    async def _execute_two_pass(
        self,
        script: Dict[str, Any],
        art_direction: Dict[str, Any],
        task: Dict[str, Any],
        character_refs: List[Dict],
        scene_refs: List[Dict],
        prop_refs: List[Dict],
        target_duration: Optional[int],
        progress_cb=None,
    ) -> Dict[str, Any]:
        """两趟：Pass1 骨架（覆盖校验/必要时重跑）→ Pass2 并行 enrich → 合并 → _normalize。"""
        async def _report(msg: str):
            if progress_cb:
                try:
                    await progress_cb(msg)
                except Exception:
                    pass

        # 归一 refs（与 _build_prompt 同逻辑：refs 空时从 art 提取）
        character_refs, scene_refs, prop_refs = self._resolve_refs(
            art_direction, character_refs, scene_refs, prop_refs,
        )

        # ── Pass 1：骨架 ──
        skeleton_shots: List[Dict[str, Any]] = []
        skel_extra: Dict[str, Any] = {}
        scenes = (script.get("scenes") or []) if isinstance(script, dict) else []

        # ── 0.7.6 节奏蓝图（前置）：通读整集出全局节奏弧线，喂给每批骨架（治批间零共享→同质化/节奏乱）──
        _bp = (await self._plan_rhythm_blueprint(script, scenes, task, target_duration, progress_cb=progress_cb)) \
            if getattr(self, "_RHYTHM_BLUEPRINT_PASS", True) else {}
        _blueprint = (_bp or {}).get("blueprint_text") or ""

        # 0.7.0：大集骨架（~40-50 镜头）单次调用会超时 → 按剧本场景分批并行
        if self._SKELETON_BATCH and len(scenes) >= 2:
            await _report("🎬 规划镜头骨架（按场景分批）...")
            skeleton_shots, skel_extra = await self._generate_skeleton_batched(
                script, art_direction, task, character_refs, scene_refs, prop_refs,
                target_duration, blueprint=_blueprint, progress_cb=progress_cb,
            )
            if skeleton_shots:
                gaps = self._skeleton_coverage_gaps(skeleton_shots, character_refs, scene_refs, prop_refs)
                if gaps["characters"] or gaps["scenes"]:
                    # 分批不整体重跑（代价高）；覆盖靠每批显式传场景角色保证，缺口只记日志，_normalize 会在 coverage 标注
                    logger.warning("storyboard 分批骨架覆盖不全（不重跑）：%s", gaps)

        # 未启用分批 / 场景太少 / 分批 0 镜头 → 单次骨架（带覆盖重试，最多 3 次）
        if not skeleton_shots:
            retry_hint = None
            MAX_SKEL_ATTEMPTS = 3
            for attempt in range(MAX_SKEL_ATTEMPTS):
                await _report(
                    "🎬 规划镜头骨架..." if attempt == 0
                    else f"🎬 骨架覆盖补全 重试 {attempt}/{MAX_SKEL_ATTEMPTS - 1}..."
                )
                skel, raw = await self._generate_skeleton(
                    script, art_direction, task, character_refs, scene_refs, prop_refs,
                    target_duration, retry_hint=retry_hint,
                )
                skeleton_shots = (skel or {}).get("storyboard", []) if isinstance(skel, dict) else []
                skel_extra = skel or {}
                if not skeleton_shots:
                    retry_hint = "上一轮没输出任何镜头，请按剧本至少拆出覆盖每个场景的镜头。"
                    logger.warning("storyboard 骨架第 %d 轮 0 镜头，重试", attempt + 1)
                    continue
                gaps = self._skeleton_coverage_gaps(skeleton_shots, character_refs, scene_refs, prop_refs)
                if not (gaps["characters"] or gaps["scenes"]):
                    if gaps["props"]:
                        logger.warning("storyboard 骨架道具未全覆盖（尽力项，不重跑）：%s", gaps["props"])
                    break
                retry_hint = (
                    f"上一轮覆盖不全：未出现的 角色={gaps['characters']} 场景={gaps['scenes']}。"
                    f"这一次必须让每个角色至少在某镜头 characters[]、每个场景至少在某镜头 scene 出现。"
                )
                logger.warning("storyboard 骨架第 %d 轮角色/场景覆盖不全 %s，重试", attempt + 1, gaps)

        if not skeleton_shots:
            raise RuntimeError("分镜骨架生成失败（多轮 0 镜头），请检查 LLM 配置或重试。")

        await _report(f"✂️ 骨架完成（{len(skeleton_shots)} 镜头），开始并行补画面/视频 prompt...")

        # ── 0.7.6 全局导演精修：全片通盘调 运镜/景别/机位类型/时长节奏（enrich 前，让 video_prompt 用上精修值）──
        if getattr(self, "_CINEMATOGRAPHY_PASS", True):
            refine_map = await self._refine_cinematography(skeleton_shots, task, blueprint=_blueprint, progress_cb=progress_cb)
            if refine_map:
                _RF = ("camera_movement", "shot_type", "shot_size", "duration_seconds", "pacing_role")
                for shot in skeleton_shots:
                    patch = refine_map.get(str(shot.get("shot_id")))
                    if patch:
                        for _k in _RF:
                            if patch.get(_k) not in (None, ""):
                                shot[_k] = patch[_k]

        # ── Pass 2：按批并行 enrich visual/video_prompt ──
        enriched_map = await self._enrich_shots_batched(
            skeleton_shots, character_refs, scene_refs, prop_refs, task, progress_cb=progress_cb,
        )

        # 合并 enrich 回骨架
        for shot in skeleton_shots:
            sid = shot.get("shot_id")
            # 骨架降级文案：动作 + 主体 + 环境拼一句，enrich 失败时当占位
            skel_text = "，".join(
                t for t in (shot.get("subject"), shot.get("action"), shot.get("environment")) if t
            ) or shot.get("beat") or ""
            patch = enriched_map.get(sid)
            if patch:
                shot["visual_prompt"] = patch.get("visual_prompt") or shot.get("visual_prompt") or skel_text
                shot["video_prompt"] = patch.get("video_prompt") or shot.get("video_prompt") or skel_text
            else:
                # 该镜头 enrich 失败 → 用骨架文案占位降级，不阻塞
                shot.setdefault("visual_prompt", skel_text)
                shot.setdefault("video_prompt", skel_text)

        # ── 0.7.6 剪辑连贯（后置）：通盘分配转场+连贯链（只改纯元数据，不碰已烤进 video_prompt 的东西）──
        if getattr(self, "_EDITING_CONTINUITY_PASS", True):
            edit_map = await self._refine_editing_continuity(skeleton_shots, task, blueprint=_blueprint, progress_cb=progress_cb)
            if edit_map:
                for shot in skeleton_shots:
                    patch = edit_map.get(str(shot.get("shot_id")))
                    if patch:
                        for _k in ("continuity_to_next", "transition_to_next"):
                            if patch.get(_k):
                                shot[_k] = patch[_k]

        # 组装成旧结构走 _normalize（复用 requires_keyframe 兜底 + 覆盖统计 + 时长裁剪）
        storyboard = {
            "storyboard": skeleton_shots,
            # 0.7.6：优先用节奏蓝图的权威 summary（批处理路径本来把每批 summary 丢弃了）
            "shot_plan_summary": (_bp or {}).get("shot_plan_summary") or skel_extra.get("shot_plan_summary", ""),
            "editing_rhythm": skel_extra.get("editing_rhythm", "标准节奏"),
            "continuity_notes": skel_extra.get("continuity_notes", ""),
        }
        storyboard = self._normalize(
            storyboard, target_duration,
            scene_refs=scene_refs, character_refs=character_refs, prop_refs=prop_refs,
        )

        shots = storyboard.get("storyboard", [])
        pacing_counts: Dict[str, int] = {}
        for s in shots:
            pr = s.get("pacing_role") or "build"
            pacing_counts[pr] = pacing_counts.get(pr, 0) + 1

        await _report(f"✅ 分镜完成（{len(shots)} 镜头）")
        return {
            "storyboard": storyboard,
            "summary": {
                "shot_count": len(shots),
                "total_duration": storyboard.get("total_duration_seconds", 0),
                "keyframe_count": sum(1 for s in shots if s.get("requires_keyframe", False)),
                "shot_plan_summary": storyboard.get("shot_plan_summary", ""),
                "pacing_roles_count": pacing_counts,
            },
        }

    @staticmethod
    def _resolve_refs(art, character_refs, scene_refs, prop_refs):
        """refs 空时从 art 提取（与 _build_prompt 同逻辑），返回 (char, scene, prop)。"""
        character_refs = list(character_refs or [])
        scene_refs = list(scene_refs or [])
        prop_refs = list(prop_refs or [])
        if not character_refs and isinstance(art, dict):
            character_refs = [
                {"name": c.get("name"), "prompt": c.get("image_prompt", ""),
                 "image_url": c.get("image_url")}
                for c in (art.get("characters") or []) if c.get("name")
            ]
        if not scene_refs and isinstance(art, dict):
            scene_refs = [
                {"location": s.get("location"), "prompt": s.get("image_prompt", ""),
                 "image_url": s.get("image_url")}
                for s in (art.get("scenes") or []) if s.get("location")
            ]
        if not prop_refs and isinstance(art, dict):
            prop_refs = [
                {"name": p.get("name"), "prompt": p.get("image_prompt", ""),
                 "use_case": p.get("use_case", "")}
                for p in (art.get("props") or []) if p.get("name")
            ]
        return character_refs, scene_refs, prop_refs

    @staticmethod
    def _skeleton_coverage_gaps(skeleton_shots, character_refs, scene_refs, prop_refs):
        """在骨架上做名字级覆盖校验（characters[]/scene/props[]），返回未覆盖项。"""
        # 0.7.0 #1: ·・• 分隔 + 剥离括号注释，让 "小峰（林悦老公） · 颓败归家装" 拆出 "小峰"，@小峰 也能命中
        split_pat = re.compile(r"[/／,，、·・•]")
        paren_pat = re.compile(r"[（(][^）)]*[）)]")

        def _aliases(name: str):
            if not name:
                return []
            out = []
            for cand in [name.strip()] + [p.strip() for p in split_pat.split(name) if p.strip()]:
                if cand and cand not in out:
                    out.append(cand)
                stripped = paren_pat.sub("", cand).strip()
                if stripped and stripped not in out:
                    out.append(stripped)
            return out

        mentioned_chars: set = set()
        mentioned_scenes: set = set()
        mentioned_props: set = set()
        for shot in skeleton_shots:
            for c in (shot.get("characters") or []):
                tok = str(c).strip()
                mentioned_chars.add(tok)
                mentioned_chars.add(tok.split("#", 1)[0].strip())  # 0.7.1 ①：剥 #服装 后缀，避免多服装角色误判未覆盖
            for p in (shot.get("props") or []):
                tok = str(p).strip()
                mentioned_props.add(tok)
                mentioned_props.add(tok.split("#", 1)[0].strip())  # 0.7.2：剥 #状态 后缀，避免多状态道具误判未覆盖
            sc = shot.get("scene") or shot.get("scene_id")
            if sc:
                mentioned_scenes.add(str(sc).strip())

        uncov_c = [c.get("name") for c in character_refs
                   if c.get("name") and not any(a in mentioned_chars for a in _aliases(c.get("name")))]
        uncov_s = [s.get("location") for s in scene_refs
                   if s.get("location") and not any(a in mentioned_scenes for a in _aliases(s.get("location")))]
        uncov_p = [p.get("name") for p in prop_refs
                   if p.get("name") and not any(a in mentioned_props for a in _aliases(p.get("name")))]
        return {
            "characters": uncov_c, "scenes": uncov_s, "props": uncov_p,
            "any": bool(uncov_c or uncov_s or uncov_p),
        }

    async def _generate_skeleton(
        self, script, art, task, character_refs, scene_refs, prop_refs,
        target_duration, retry_hint=None,
    ):
        """Pass1：1 次小调用生成镜头骨架（轻量字段，无 visual/video_prompt）。"""
        prompt = self._build_skeleton_prompt(
            script, art, task, character_refs, scene_refs, prop_refs, target_duration, retry_hint,
        )
        full_prompt = f"{SKELETON_SYSTEM_PROMPT}\n\n{prompt}"
        response = await self.llm_generate(
            prompt=full_prompt, system=None, json_mode=False,
            temperature=0.5, max_tokens=8192,  # 0.7.0：大制作 9min 集可能 40-50 镜，每镜含 主体/动作/情绪/环境，8k 兜底防截断
        )
        data = self._safe_parse_storyboard_json(response)
        return data, response

    async def _plan_rhythm_blueprint(self, script, scenes, task, target_duration=None, progress_cb=None) -> Dict[str, Any]:
        """0.7.6 节奏蓝图（前置）：先通读整集全部场景，出一份**全局节奏/情绪弧线蓝图**，喂给每一批骨架
        —— 治"骨架按场景分批、批间零共享、各拆各的"这个同质化+节奏乱的根因。一次 LLM 调用；
        失败返回 {}（退化成当前无蓝图行为）。返回 {blueprint_text, shot_plan_summary}。"""
        try:
            _scenes = [s for s in (scenes or []) if isinstance(s, dict)]
            if len(_scenes) < 2:
                return {}
            slim = [
                {
                    "scene_id": s.get("scene_id"),
                    "location": s.get("location"),
                    "summary": (s.get("scene_summary") or s.get("action") or "")[:80],
                    "emotion": s.get("emotion"),
                    "duration_seconds": s.get("duration_seconds"),
                }
                for s in _scenes
            ]
            structure = (script.get("structure") if isinstance(script, dict) else None) or {}
            prompt = f"""你是短剧总导演。下面是本集全部 {len(slim)} 个场景（按顺序）。请通读全篇，先定一份**全局节奏蓝图**——**别拆镜头**，只从整片高度规划节奏与情绪，好让后面分镜时每一场都知道自己在整片里的位置、不各拆各的、不雷同。

起承转合结构：{json.dumps(structure, ensure_ascii=False)}
目标总时长：{target_duration or '按剧情'} 秒
场景序列：
{json.dumps(slim, ensure_ascii=False, indent=2)}

输出 JSON：
{{
  "arc": [{{"scene_id":"S01","pacing_role":"establish|build|climax|rest|release 之一","tension":"1-10 张力值","note":"这一场在整片里的作用+节奏基调（如'慢铺垫别急'/'情绪拉满快切'）"}}, ...每场一条],
  "climax_scene_id": "全片情绪最高点在哪一场",
  "continuity_anchors": "哪些相邻场景/节拍该流畅连接（连续动作/情绪延续），哪些该硬切换气口——一两句",
  "anti_homogeneity": "为避免雷同给分镜的硬指令（如'相邻场景景别/机位/节奏拉开，别每场都中景对切；安静场用长镜，冲突场用短快切'）——一两句",
  "shot_plan_summary": "一句话概括整片节奏与情感曲线（给音频接力用）"
}}
只输出 JSON。"""
            full = f"你是把控整片叙事节奏的短剧总导演。\n\n{prompt}"
            resp = await self.llm_generate(prompt=full, system=None, json_mode=False, temperature=0.6, max_tokens=3072)
            data = self._safe_parse_storyboard_json(resp) or {}
            if not isinstance(data, dict) or not data.get("arc"):
                return {}
            arc = [a for a in (data.get("arc") or []) if isinstance(a, dict)]
            arc_lines = "；".join(
                f"{a.get('scene_id')}={a.get('pacing_role')}(张力{a.get('tension')}：{a.get('note') or ''})"
                for a in arc
            )
            blueprint_text = (
                f"【全局节奏蓝图】高潮在 {data.get('climax_scene_id') or '—'}。"
                f"各场定位：{arc_lines}。"
                f"连贯：{data.get('continuity_anchors') or '—'}。"
                f"反雷同：{data.get('anti_homogeneity') or '—'}。"
            )
            if progress_cb:
                try:
                    await progress_cb("🎬 规划整片节奏蓝图...")
                except Exception:
                    pass
            return {"blueprint_text": blueprint_text, "shot_plan_summary": data.get("shot_plan_summary") or ""}
        except Exception as e:
            logger.warning("节奏蓝图 pass 失败（跳过，退化无蓝图）：%s", e)
            return {}

    async def _refine_editing_continuity(self, shots, task, blueprint=None, progress_cb=None) -> Dict[str, Dict[str, str]]:
        """0.7.6 剪辑连贯（后置，enrich 之后）：通盘给每个切点分配 转场(transition_to_next)+连贯链(continuity_to_next)。
        **只碰这两个纯元数据字段**（不在 video_prompt 里，enrich 后改安全）。失败返回 {}。
        返回 {shot_id: {continuity_to_next, transition_to_next}}。"""
        try:
            if len(shots) < 3:
                return {}
            seq = [
                {
                    "shot_id": s.get("shot_id"),
                    "scene": s.get("scene"),
                    "shot_type": s.get("shot_type"),
                    "pacing_role": s.get("pacing_role"),
                    "action": (s.get("action") or "")[:40],
                }
                for s in shots
            ]
            prompt = f"""你是剪辑师。下面是全片 {len(seq)} 个镜头（按顺序）。请通盘给**每个切点**（第 i 镜→第 i+1 镜）定两件事，让整片剪得流畅、有呼吸：

序列：
{json.dumps(seq, ensure_ascii=False, indent=2)}

1. **continuity_to_next**（画面连贯）∈ match_cut / scale_jump / movement_continuation / contrast / hard_cut：
   - **movement_continuation**：相邻两镜是**连续的动作/运镜/情绪延续**（一个转身没做完就接下一镜、镜头持续推进跨镜）——系统会用首尾帧把画面接起来，最流畅。**同一场景的连续节拍/来回对话多用它**，别整片全 hard_cut。
   - match_cut：靠相似构图/形状/动作匹配切；scale_jump：景别跳切（远→近）提神；contrast：强烈反差切；hard_cut：普通硬切（换场景/换时间/换主体）。
2. **transition_to_next**（转场特效）∈ hard_cut / fade / dissolve：同场景连贯多用 hard_cut（干脆），转场/情绪落点用 fade/dissolve。

**别滥用连贯**：只有真连续才标 movement_continuation（会拖慢生成）；但也别整片死板全 hard_cut——同场景连续动作/对话来回该连就连。最后一镜两个字段都填 null。

只输出 JSON：{{"shots":[{{"shot_id":"...","continuity_to_next":"...","transition_to_next":"..."}}]}}"""
            full = f"你是把控剪辑节奏与画面连贯的剪辑师。\n\n{prompt}"
            resp = await self.llm_generate(prompt=full, system=None, json_mode=False, temperature=0.4, max_tokens=4096)
            data = self._safe_parse_storyboard_json(resp) or {}
            out: Dict[str, Dict[str, str]] = {}
            for it in (data.get("shots") or []):
                sid = it.get("shot_id")
                if not sid:
                    continue
                patch = {}
                for k in ("continuity_to_next", "transition_to_next"):
                    v = it.get(k)
                    if v not in (None, ""):
                        patch[k] = str(v)
                if patch:
                    out[str(sid)] = patch
            if progress_cb and out:
                try:
                    await progress_cb(f"✂️ 剪辑连贯优化（{len(out)} 切点）")
                except Exception:
                    pass
            return out
        except Exception as e:
            logger.warning("剪辑连贯 pass 失败（跳过）：%s", e)
            return {}

    async def _refine_cinematography(self, skeleton_shots, task, blueprint=None, progress_cb=None) -> Dict[str, Dict[str, Any]]:
        """0.7.6 全局导演精修（原 0.7.1 摄影指导扩展）：通盘看全片镜头序列，除运镜外再统一精修
        shot_type/shot_size 反雷同 + duration 节奏弧线 + pacing 对齐蓝图。**必须在 enrich 之前**（enrich 会把
        duration/景别烤进 video_prompt）。全列表单次调用；任何失败返回 {}（安全降级）。
        返回 {shot_id: {字段:值}}（每镜只含实际调整的字段）。"""
        try:
            if len(skeleton_shots) < 3:
                return {}
            seq = [
                {
                    "shot_id": s.get("shot_id"),
                    "scene": s.get("scene"),
                    "shot_type": s.get("shot_type"),
                    "shot_size": s.get("shot_size"),
                    "camera_movement": s.get("camera_movement"),
                    "pacing_role": s.get("pacing_role"),
                    "duration_seconds": s.get("duration_seconds"),
                    "action": (s.get("action") or s.get("beat") or "")[:50],
                }
                for s in skeleton_shots
            ]
            _bp = f"\n【全局节奏蓝图，据此对齐 pacing 弧线与节奏起伏】\n{blueprint}\n" if blueprint else ""
            prompt = f"""你是电影剪辑总导演。下面是全片 {len(seq)} 个镜头的序列，请**通盘**精修，让整片节奏有起伏、镜头语言不雷同（看整体，别逐镜孤立）。{_bp}
当前序列：
{json.dumps(seq, ensure_ascii=False, indent=2)}

精修四件事：
1. **camera_movement**：避免连续 3+ static；相邻同景别/同 pacing 的运镜要变化；establish/climax 可 dolly_in/push 造张力，rest/release 用缓慢 zoom_out/pan 收；每镜只一个运镜。取值 static/dolly_in/dolly_out/pan_left/pan_right/tilt_up/tilt_down/tracking/handheld/zoom_in/zoom_out/orbit。
2. **shot_type / shot_size 反雷同**：相邻镜别连着同一景别+同一机位类型（如连着 3 个 MS single）；对话戏用 establish→正打(single)→反打(reverse/ots)→反应(reaction) 覆盖；景别在 ELS/LS/MS/CU/ECU 间有节奏地跳。
3. **duration_seconds 节奏弧线**：别均匀注水！对手戏/关键动作/高潮拉长（可 12-15s），过渡/空镜/单个反应收短（4-6s），让时长本身形成呼吸、服从 pacing。整数 4-15。
4. **pacing_role**：与全局蓝图弧线对齐（establish→build→climax→rest→release），别每段各自 establish→climax。

只输出 JSON：{{"shots":[{{"shot_id":"...","camera_movement":"...","shot_type":"...","shot_size":"...","duration_seconds":8,"pacing_role":"..."}}]}}，**每镜只列你实际调整的字段**（没改的省略），仅含需调整的镜头。"""
            full = f"你是专业电影剪辑总导演，通盘把控镜头语言与整片节奏。\n\n{prompt}"
            resp = await self.llm_generate(prompt=full, system=None, json_mode=False, temperature=0.5, max_tokens=6144)
            data = self._safe_parse_storyboard_json(resp) or {}
            out: Dict[str, Dict[str, Any]] = {}
            _ALLOWED = ("camera_movement", "shot_type", "shot_size", "duration_seconds", "pacing_role")
            for it in (data.get("shots") or []):
                sid = it.get("shot_id")
                if not sid:
                    continue
                patch = {k: it[k] for k in _ALLOWED if it.get(k) not in (None, "")}
                if patch:
                    out[str(sid)] = patch
            if progress_cb and out:
                try:
                    await progress_cb(f"🎬 导演通盘精修（{len(out)} 镜：运镜/景别/时长/节奏）")
                except Exception:
                    pass
            return out
        except Exception as e:
            logger.warning("全局导演精修 pass 失败（跳过精修）：%s", e)
            return {}

    def _scene_shot_count(self, sc: Dict) -> int:
        """按场景时长估算该场景应拆多少镜头。"""
        ds = _parse_duration_to_seconds(sc.get("duration_seconds")) or MIN_SHOT_SECONDS
        return max(1, round(ds / self._SKELETON_AVG_SHOT_SECONDS))

    async def _generate_skeleton_batched(
        self, script, art, task, character_refs, scene_refs, prop_refs,
        target_duration, blueprint="", progress_cb=None,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Pass1 分批版：按剧本场景分组并行生成镜头骨架，避免单次调用过大超时。
        每批只规划几个场景的镜头（~_SKELETON_SHOTS_PER_BATCH 个）。返回 (shots, extra)。"""
        scenes = [s for s in ((script.get("scenes") or []) if isinstance(script, dict) else []) if isinstance(s, dict)]
        if len(scenes) < 2:
            skel, _ = await self._generate_skeleton(
                script, art, task, character_refs, scene_refs, prop_refs, target_duration,
            )
            shots = (skel or {}).get("storyboard", []) if isinstance(skel, dict) else []
            return shots, (skel or {})

        char_names = [c.get("name") for c in character_refs if c.get("name")]
        scene_names = [s.get("location") for s in scene_refs if s.get("location")]
        prop_names = [p.get("name") for p in prop_refs if p.get("name")]

        # 按目标镜头数把场景切成批
        batches: List[List[Dict]] = []
        cur: List[Dict] = []
        cur_count = 0
        for sc in scenes:
            n = self._scene_shot_count(sc)
            if cur and cur_count + n > self._SKELETON_SHOTS_PER_BATCH:
                batches.append(cur)
                cur, cur_count = [], 0
            cur.append(sc)
            cur_count += n
        if cur:
            batches.append(cur)

        total_batches = len(batches)
        sem = asyncio.Semaphore(self._ENRICH_CONCURRENCY)
        results: List[List[Dict]] = [[] for _ in batches]
        done = 0

        async def _run_batch(idx: int, batch: List[Dict]):
            nonlocal done
            async with sem:
                try:
                    prompt = self._build_scene_batch_skeleton_prompt(
                        batch, char_names, scene_names, prop_names, task, blueprint,
                    )
                    full = f"{SKELETON_SYSTEM_PROMPT}\n\n{prompt}"
                    resp = await self.llm_generate(
                        prompt=full, system=None, json_mode=False,
                        temperature=0.5, max_tokens=6144,  # 每批 ~12 镜头，6k 足够
                    )
                    data = self._safe_parse_storyboard_json(resp) or {}
                    shots = data.get("storyboard") or data.get("shots") or []
                    results[idx] = [s for s in shots if isinstance(s, dict)]
                    if not results[idx]:
                        results[idx] = self._fallback_scene_shots(batch)
                except Exception as e:
                    logger.warning("storyboard 骨架批 %d 失败（用场景占位镜头降级）：%s", idx, e)
                    results[idx] = self._fallback_scene_shots(batch)
                finally:
                    done += 1
                    if progress_cb:
                        try:
                            await progress_cb(f"🎬 规划镜头骨架 {done}/{total_batches} 批...")
                        except Exception:
                            pass

        await asyncio.gather(*[_run_batch(i, b) for i, b in enumerate(batches)])

        merged: List[Dict] = []
        for lst in results:
            merged.extend(lst)
        for i, s in enumerate(merged):
            s["shot_id"] = f"SHOT_{i+1:02d}"   # 重新编号，跨批连续

        extra = {"shot_plan_summary": "", "editing_rhythm": "标准节奏", "continuity_notes": ""}
        return merged, extra

    def _build_scene_batch_skeleton_prompt(
        self, batch_scenes, char_names, scene_names, prop_names, task, blueprint="",
    ) -> str:
        """给一小批剧本场景，要求只为这些场景拆镜头骨架。"""
        slim = []
        for sc in batch_scenes:
            ds = _parse_duration_to_seconds(sc.get("duration_seconds")) or MIN_SHOT_SECONDS
            slim.append({
                "scene_id": sc.get("scene_id"),
                "location": sc.get("location"),
                "duration_seconds": ds,
                "target_shots": self._scene_shot_count(sc),
                "characters": sc.get("characters") or [],
                "prior_context": sc.get("prior_context") or "",  # 0.7.4：本场前观众已知，骨架据此不重复交代已知信息
                "action": sc.get("action") or sc.get("scene_summary") or "",
                "emotion": sc.get("emotion") or "",
                "dialogue": sc.get("dialogue") or "",
                "voiceover": sc.get("voiceover") or sc.get("narration") or "",
            })
        _bp = f"\n## 全局节奏蓝图（本批要服从整片的节奏/情绪弧线，与相邻场景在景别/机位/节奏上拉开、别雷同）\n{blueprint}\n" if blueprint else ""
        return f"""只为下面这几个剧本场景生成镜头骨架（**不要涉及其它场景**）。
画面比例：{task.get("aspect_ratio", "9:16")}　风格：{task.get("style", "电影感")}
{_bp}
## 可引用角色名（每镜 characters[] 用这些真实名）
{('、'.join(char_names)) or '（无）'}
## 可引用场景名（每镜 scene 字段从这里选最贴近的）
{('、'.join(scene_names)) or '（无）'}
## 可选道具（仅剧情用到才放 props[]）
{('、'.join(prop_names)) or '（无）'}

## 本批场景（把每个场景**按剧情**拆成镜头，参考 ≈ target_shots 个；该场景各镜头 duration_seconds 之和 ≈ 场景 duration_seconds）
{json.dumps(slim, ensure_ascii=False, indent=2)}

要求：
- 按剧情把场景拆成镜头（target_shots 是**参考量级，别死守**），覆盖该场景 action 的起承转合，别只给 1-2 个
- **叙事切片（0.7.4）**：只呈现这些场景本身的剧情——`prior_context` 里已知的信息不要用镜头重复交代，也**不要提前演出后续场次才发生的转折/揭示**
- 每镜 scene 填该场景对应的「可引用场景名」（最贴近的那个），characters[] 用本场景出现的角色
- **场面调度（0.7.4，关键）**：先定这场戏的空间站位（每人在哪个方位、朝向谁，全场统一），再用机位覆盖拆镜。给每镜标 shot_type + visible_characters（真正入画的人，除群像 establish/master 外**≤2 人**）+ screen_position + eyeline_target；对话戏用"establish→正打→反打(reverse_of)→反应"覆盖，**别把多人塞一排**；正反打两镜轴线一致（一个朝右、其反打朝左）
- 每镜给 subject/action/emotion/**motivation**/environment（中文）+ shot_size/camera_angle/camera_movement + duration_seconds(4-15) + requires_keyframe + dialogue/narration/sound_effects
- **motivation（动机，必填）**：这一镜主角此刻想要什么、潜台词是什么（如'想让对方心软'）——后续画面/表演/运镜都据此设计；别写'推进剧情'这类空话
- **每镜 duration_seconds 按内容定、别全部同长**：大段对手戏 / 连贯关键动作 → 长（可到 15s；一镜装不下且要保连贯，就在合适处切成两镜）；过渡镜 / 空镜 / 单个反应 → 短（4-6s）。宁可时长不齐，也别均匀注水撑时长
- **对白（dialogue）来自场景 dialogue，切分到真正说这句话的那一镜（不要丢、也不要每镜重复整段）**；该镜无人说话就留空
- **音效（sound_effects）按本镜实际动作/环境填具体音**（脚步、开关门、碗筷碰撞、拳脚打斗、雨声、人群嘈杂等），与画面动作对应；纯静态镜可留空数组。**不要写 BGM**
按 system prompt 的 schema 输出 {{"storyboard":[...]}}（只含本批镜头）。直接输出 JSON。"""

    def _fallback_scene_shots(self, batch_scenes) -> List[Dict[str, Any]]:
        """某批 LLM 失败时，用场景信息生成占位镜头（保证场景/角色覆盖 + 时长不丢）。"""
        out: List[Dict[str, Any]] = []
        for sc in batch_scenes:
            ds = _parse_duration_to_seconds(sc.get("duration_seconds")) or MIN_SHOT_SECONDS
            n = self._scene_shot_count(sc)
            per = max(MIN_SHOT_SECONDS, min(MAX_SHOT_SECONDS, round(ds / n)))
            action = sc.get("action") or sc.get("scene_summary") or ""
            for k in range(n):
                out.append({
                    "scene_id": sc.get("scene_id"),
                    "scene": sc.get("location"),
                    "subject": "，".join(sc.get("characters") or []) or "主体",
                    "action": action,
                    "emotion": sc.get("emotion") or "",
                    "environment": sc.get("location") or "",
                    "characters": list(sc.get("characters") or []),
                    "props": [],
                    "duration_seconds": per,
                    "shot_size": "MS",
                    "camera_angle": "eye_level",
                    "camera_movement": "static",
                    "requires_keyframe": True,
                    "dialogue": sc.get("dialogue") if k == 0 else "",
                    "narration": "",
                    "sound_effects": [],
                })
        return out

    async def _enrich_shots_batched(
        self, skeleton_shots, character_refs, scene_refs, prop_refs, task, progress_cb=None,
    ) -> Dict[str, Dict[str, str]]:
        """Pass2：按批并行补 visual/video_prompt。返回 {shot_id: {visual_prompt, video_prompt}}。"""
        batch_size = self._ENRICH_BATCH_SIZE
        batches = [skeleton_shots[i:i + batch_size] for i in range(0, len(skeleton_shots), batch_size)]
        total_batches = len(batches)
        sem = asyncio.Semaphore(self._ENRICH_CONCURRENCY)
        result: Dict[str, Dict[str, str]] = {}
        done = 0

        async def _run_batch(batch_idx: int, batch: List[Dict]):
            nonlocal done
            async with sem:
                batch_ids = [s.get("shot_id") for s in batch if s.get("shot_id")]
                # 偶发 LLM 失败 / JSON 坏 → 重试，减少镜头降级成纯文本（没 @ 引用 → 关键帧会塞错角色）
                for attempt in range(3):
                    try:
                        prompt = self._build_enrich_prompt(batch, character_refs, scene_refs, prop_refs, task)
                        full_prompt = f"{ENRICH_SYSTEM_PROMPT}\n\n{prompt}"
                        resp = await self.llm_generate(
                            prompt=full_prompt, system=None, json_mode=False,
                            temperature=0.6, max_tokens=6144,  # 逐镜：单镜 visual+video，6k 绰绰有余
                        )
                        data = self._safe_parse_storyboard_json(resp) or {}
                        for s in (data.get("shots") or []):
                            sid = s.get("shot_id")
                            if sid:
                                result[sid] = {
                                    "visual_prompt": str(s.get("visual_prompt") or "").strip(),
                                    "video_prompt": str(s.get("video_prompt") or "").strip(),
                                }
                        if all(sid in result for sid in batch_ids):
                            break  # 本批全补到，停止重试
                    except Exception as e:
                        logger.warning("storyboard enrich 批 %d 第 %d 次失败：%s", batch_idx, attempt + 1, e)
                    if attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                missing = [sid for sid in batch_ids if sid not in result]
                if missing:
                    logger.warning("storyboard enrich 批 %d 重试后仍缺 %d 镜（降级纯文本）：%s", batch_idx, len(missing), missing)
                done += 1
                if progress_cb:
                    try:
                        await progress_cb(f"🖌️ 补镜头细节 {done}/{total_batches} 批...")
                    except Exception:
                        pass

        await asyncio.gather(*[_run_batch(i, b) for i, b in enumerate(batches)])
        return result

    @staticmethod
    def _safe_parse_storyboard_json(response: str) -> Optional[Dict[str, Any]]:
        """解析 LLM JSON 输出（含 markdown ```json 块兜底）。失败返 None。"""
        if not response or not response.strip():
            return None
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", response, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    return None
            return None

    def _build_skeleton_prompt(
        self, script, art, task, character_refs, scene_refs, prop_refs, target_duration, retry_hint,
    ) -> str:
        """Pass1 骨架 prompt：列可引用名字 + 时长约束，要求只出骨架。"""
        script_json = json.dumps(script, ensure_ascii=False, indent=2)[:6000] if isinstance(script, dict) else str(script)[:6000]
        char_names = [c.get("name") for c in character_refs if c.get("name")]
        scene_names = [s.get("location") for s in scene_refs if s.get("location")]
        prop_names = [p.get("name") for p in prop_refs if p.get("name")]
        n_scenes = len(scene_names)

        if target_duration and target_duration >= MIN_SHOT_SECONDS:
            min_shots = max(max(1, math.ceil(target_duration / MAX_SHOT_SECONDS)), n_scenes or 1)
            max_shots = max(min_shots, target_duration // MIN_SHOT_SECONDS)
            dur_block = (
                f"目标总时长 ≈ {target_duration} 秒；单镜头 {MIN_SHOT_SECONDS}-{MAX_SHOT_SECONDS} 秒；"
                f"镜头数 {min_shots}–{max_shots} 个（至少 {min_shots}）"
            )
        else:
            min_shots = max(1, n_scenes or 1)
            dur_block = f"单镜头 {MIN_SHOT_SECONDS}-{MAX_SHOT_SECONDS} 秒；镜头数至少 {min_shots} 个（按场景兜底）"

        retry_block = f"\n## ⚠ 重试提示\n{retry_hint}\n" if retry_hint else ""
        return f"""请将以下剧本拆解为**镜头骨架**（只出结构，不写 visual/video_prompt）。{retry_block}
## 剧本
{script_json}

画面比例：{task.get("aspect_ratio", "9:16")}　风格：{task.get("style", "电影感")}

## 时长/镜头数约束
{dur_block}

## 可引用的角色（每个都要在某镜头 characters[] 出现）
{('、'.join(char_names)) or '（无）'}

## 可引用的场景（每个都要在某镜头 scene 出现）
{('、'.join(scene_names)) or '（无）'}

## 可选道具（**仅在剧情真正用到时**才放进对应镜头 props[]，不要为凑数硬塞）
{('、'.join(prop_names)) or '（无）'}

按 system prompt 的 JSON schema 输出骨架。每镜头列出 characters[]/scene 真实名字 + subject/action/emotion/**motivation**/environment + **shot_type/visible_characters（除群像 establish/master 外≤2 人）/screen_position（场景站位）/eyeline_target**。**遵守 system prompt 的场面调度（机位覆盖·正反打·别把多人塞一排）与叙事连续性（不重复交代已知、不提前泄露后续）。**道具只在剧情需要时引用。直接输出 JSON。"""

    def _build_enrich_prompt(self, batch, character_refs, scene_refs, prop_refs, task) -> str:
        """Pass2 enrich prompt：给一小批骨架镜头 + 它们涉及的 @ 引用清单（含视觉描述）。"""
        # 本批涉及的角色/场景/道具，带美术 prompt 摘要，帮 LLM 写准
        # 0.7.1 ①：角色按基名归集（带 outfits[]），多套服装时列出 @角色#服装 选项
        char_by_name = {c.get("name"): c for c in character_refs if c.get("name")}
        scene_map = {s.get("location"): (s.get("prompt") or "")[:80] for s in scene_refs if s.get("location")}
        # 0.7.5：场景按名字归集（带 cameras[]），多机位时列出 @场景#机位 选项
        scene_by_name = {s.get("location"): s for s in scene_refs if s.get("location")}
        # 0.7.2：道具按名字归集（带 states[]），多状态时列出 @道具#状态 选项
        prop_by_name = {p.get("name"): p for p in prop_refs if p.get("name")}

        ref_lines = []
        seen = set()
        for shot in batch:
            for nm in (shot.get("characters") or []):
                base = str(nm).split("#", 1)[0].strip()
                if base in char_by_name and base not in seen:
                    seen.add(base)
                    c = char_by_name[base]
                    outfits = c.get("outfits") or []
                    if len(outfits) > 1:
                        for o in outfits:
                            ref_lines.append(
                                f"  - @{base}#{o.get('outfit_name')}（角色·服装，{o.get('outfit_when') or '—'}）: {(o.get('prompt') or '')[:70]}"
                            )
                        ref_lines.append(f"  - （@{base} 不带 # 默认用第一套「{outfits[0].get('outfit_name')}」）")
                    else:
                        ref_lines.append(f"  - @{base}（角色）: {(c.get('prompt') or '')[:80]}")
            sc = shot.get("scene")
            if sc in scene_by_name and sc not in seen:
                seen.add(sc)
                _sref = scene_by_name[sc]
                _cams = _sref.get("cameras") or []
                if len(_cams) > 1:
                    for _cam in _cams:
                        _cn = _cam.get("camera_name")
                        if not _cn:
                            continue
                        ref_lines.append(
                            f"  - @{sc}#{_cn}（场景·机位，{_cam.get('when_to_use') or '—'}）: {(_cam.get('prompt') or '')[:60]}"
                        )
                    ref_lines.append(f"  - （@{sc} 不带 # 默认用建立镜「{_cams[0].get('camera_name') or '建立镜'}」）")
                else:
                    ref_lines.append(f"  - @{sc}（场景）: {scene_map.get(sc, '')}")
            for nm in (shot.get("props") or []):
                base = str(nm).split("#", 1)[0].strip()
                if base in prop_by_name and base not in seen:
                    seen.add(base)
                    p = prop_by_name[base]
                    states = p.get("states") or []
                    if len(states) > 1:
                        for st in states:
                            ref_lines.append(
                                f"  - @{base}#{st.get('state_name')}（道具·状态，{(st.get('state_description') or '—')[:30]}）: {(st.get('prompt') or '')[:50]}"
                            )
                        ref_lines.append(f"  - （@{base} 不带 # 默认用第一态「{states[0].get('state_name')}」）")
                    else:
                        ref_lines.append(f"  - @{base}（道具）: {(p.get('prompt') or '')[:60]}")
        ref_block = "\n".join(ref_lines) or "  （本批无可引用素材）"

        # 精简骨架（只给 enrich 需要的字段）
        slim = [
            {
                "shot_id": s.get("shot_id"),
                "scene": s.get("scene"),
                "subject": s.get("subject") or "",
                "action": s.get("action") or s.get("beat") or "",
                "emotion": s.get("emotion") or "",
                "motivation": s.get("motivation") or "",   # 0.7.4：本镜动机/潜台词，enrich 据此写有动机的表演，而非对着标签套模板
                "environment": s.get("environment") or "",
                "characters": s.get("characters") or [],
                "props": s.get("props") or [],
                "duration_seconds": s.get("duration_seconds"),  # 关键：ENRICH 据此让 c0X 各段秒数之和=本镜时长
                "shot_size": s.get("shot_size"),
                "camera_angle": s.get("camera_angle"),
                "camera_movement": s.get("camera_movement"),
                # 0.7.4：场面调度信息带给 enrich —— 位置/朝向照抄不现编，且只为入画角色写标签
                "shot_type": s.get("shot_type"),
                "visible_characters": s.get("visible_characters") or s.get("characters") or [],
                "screen_position": s.get("screen_position") or {},
                "eyeline_target": s.get("eyeline_target") or {},
                # 0.7.4：台词/旁白/音效原文带给 enrich —— 修复此前 slim 丢弃 dialogue 导致 video_prompt 台词被 LLM 凭空编造、与音频真台词对不上的 bug
                "dialogue": s.get("dialogue") or "",
                "narration": s.get("narration") or "",
                "sound_effects": s.get("sound_effects") or [],
            }
            for s in batch
        ]
        return f"""为下面这批镜头生成 visual_prompt + video_prompt。

画面比例：{task.get("aspect_ratio", "9:16")}　风格：{task.get("style", "电影感")}

## 本批可引用素材（@<名字> + 美术视觉摘要）
{ref_block}

## 镜头骨架（为每个 shot_id 生成 visual_prompt + video_prompt）
{json.dumps(slim, ensure_ascii=False, indent=2)}

按 system prompt 要求：每镜头 @ 引用本镜的角色/场景/道具 + 主从尺寸括号 + 中英混用。**角色若有多套服装，必须用 @角色#服装 选对应本镜剧情/时间的那套（按服装"何时穿"匹配本镜场景）；只有一套用 @角色 即可。道具若有多个状态，用 @道具#状态 选对应本镜剧情的状态；只有一个状态用 @道具 即可。场景若上面列出多个机位板，按本镜 shot_type 用 @场景#机位 选（reverse→反打机位、ots→过肩机位、establish/master→@场景不带#），同场景相邻镜尽量换不同机位板、别都用同一张。** 直接输出 JSON {{"shots":[...]}}。"""

    async def _generate_once(
        self,
        script: Dict[str, Any],
        art_direction: Dict[str, Any],
        task: Dict[str, Any],
        character_refs: List[Dict],
        scene_refs: List[Dict],
        prop_refs: List[Dict],
        target_duration: Optional[int],
        temperature: float,
        retry_hint: Optional[str],
    ) -> tuple[Optional[Dict[str, Any]], str]:
        """跑一轮：构建 prompt → 调 LLM → 解析 JSON → normalize。
        Returns (storyboard_dict 或 None, raw_response)。
        """
        prompt = self._build_prompt(
            script, art_direction, task, character_refs, scene_refs, prop_refs, target_duration,
            retry_hint=retry_hint,
        )
        full_prompt = f"{self.system_prompt}\n\n{prompt}" if self.system_prompt else prompt
        response = await self.llm_generate(
            prompt=full_prompt,
            system=None,
            json_mode=False,
            temperature=temperature,
            max_tokens=16384,  # 0.6.5: 10+ shots × 长 visual/video_prompt + dialogue + 主从尺寸括号 容易超 8k
        )

        # 解析 JSON（从 markdown 代码块中提取）
        try:
            storyboard = json.loads(response)
        except json.JSONDecodeError:
            import re
            json_match = re.search(r"```(?:json)?\s*(.*?)\s*```", response, re.DOTALL)
            if not json_match:
                logger.warning("storyboard LLM 输出未找到 JSON：%s", response[:300])
                return None, response
            try:
                storyboard = json.loads(json_match.group(1))
            except json.JSONDecodeError as e:
                logger.warning("storyboard LLM 输出 JSON 解析失败：%s; 原文前 300 字：%s", e, response[:300])
                return None, response

        storyboard = self._normalize(
            storyboard, target_duration,
            scene_refs=scene_refs, character_refs=character_refs, prop_refs=prop_refs,
        )
        return storyboard, response
    
    def _resolve_target_duration(self, task: Dict, script: Dict) -> Optional[int]:
        """目标时长（秒）。来源优先级：task → script 场景累加 → script.duration 字符串。"""
        # 1) task 显式传入
        explicit = _parse_duration_to_seconds(task.get("target_duration_seconds"))
        if explicit:
            return explicit
        # 2) script.scenes[*].duration_seconds 累加
        if isinstance(script, dict):
            scenes = script.get("scenes") or []
            if isinstance(scenes, list) and scenes:
                total = 0
                for s in scenes:
                    if not isinstance(s, dict):
                        continue
                    ds = _parse_duration_to_seconds(s.get("duration_seconds"))
                    if ds:
                        total += ds
                if total > 0:
                    return total
            # 3) script.duration 字符串（e.g. "15秒"）
            from_str = _parse_duration_to_seconds(script.get("duration"))
            if from_str:
                return from_str
        return None

    def _build_prompt(
        self,
        script: Dict,
        art: Dict,
        task: Dict,
        character_refs: List[Dict] = None,
        scene_refs: List[Dict] = None,
        prop_refs: List[Dict] = None,
        target_duration: Optional[int] = None,
        retry_hint: Optional[str] = None,
    ) -> str:
        """构建分镜提示，把可引用的角色/场景/道具名字显式列出"""
        script_json = json.dumps(script, ensure_ascii=False, indent=2)[:1500] if isinstance(script, dict) else str(script)[:1500]
        art_json = json.dumps(art, ensure_ascii=False, indent=2)[:1000] if isinstance(art, dict) else str(art)[:1000]

        # 从 art 或 character_refs 中提取可引用的名字
        character_refs = character_refs or []
        scene_refs = scene_refs or []
        prop_refs = prop_refs or []  # 0.6.4
        if not character_refs and isinstance(art, dict):
            character_refs = [
                {"name": c.get("name"), "prompt": c.get("image_prompt", "")}
                for c in (art.get("characters") or []) if c.get("name")
            ]
        if not scene_refs and isinstance(art, dict):
            scene_refs = [
                {"location": s.get("location"), "prompt": s.get("image_prompt", "")}
                for s in (art.get("scenes") or []) if s.get("location")
            ]
        if not prop_refs and isinstance(art, dict):
            prop_refs = [
                {"name": p.get("name"), "prompt": p.get("image_prompt", ""), "use_case": p.get("use_case", "")}
                for p in (art.get("props") or []) if p.get("name")
            ]

        char_names = [c.get("name") for c in character_refs if c.get("name")]
        scene_names = [s.get("location") for s in scene_refs if s.get("location")]
        prop_names = [p.get("name") for p in prop_refs if p.get("name")]
        n_chars = len(char_names)
        n_scenes = len(scene_names)
        n_props = len(prop_names)

        def _fmt_char_line(c):
            # 0.7.1 ①：多套服装 → 提示 @角色#服装 选项
            outfits = c.get("outfits") or []
            if len(outfits) > 1:
                opts = " / ".join(f"#{o.get('outfit_name')}" for o in outfits if o.get("outfit_name"))
                return f"  - @{c.get('name')}（多套服装，引用写 @{c.get('name')}#服装名；可选：{opts}）: {(c.get('prompt') or '')[:60]}"
            return f"  - @{c.get('name')}: {(c.get('prompt') or '')[:80]}"
        char_lines = "\n".join(
            _fmt_char_line(c) for c in character_refs if c.get("name")
        ) or "  （暂无）"
        scene_lines = "\n".join(
            f"  - @{s.get('location')}: {(s.get('prompt') or '')[:80]}"
            for s in scene_refs if s.get("location")
        ) or "  （暂无）"
        def _fmt_prop_line(p):
            # 0.7.2：多状态道具 → 提示 @道具#状态 选项
            states = p.get("states") or []
            if len(states) > 1:
                opts = " / ".join(f"#{s.get('state_name')}" for s in states if s.get("state_name"))
                return f"  - @{p.get('name')}（多个状态，引用写 @{p.get('name')}#状态名；可选：{opts}）: {(p.get('prompt') or '')[:50]}"
            return f"  - @{p.get('name')}: {(p.get('prompt') or '')[:60]}（用途：{(p.get('use_case') or '')[:30]}）"
        prop_lines = "\n".join(
            _fmt_prop_line(p) for p in prop_refs if p.get("name")
        ) or "  （暂无）"

        # 动态构造一个 @ 引用的示例（0.6.3 中英混用：情绪/动作中文，镜头术语英文）
        # 0.6.4：每个 @ 后必须带"画面占比+位置"括号，避免多图融合时主从比例失控
        sample_char = char_names[0] if char_names else "角色1"
        sample_scene = scene_names[0] if scene_names else "场景1"
        sample_prop = prop_names[0] if prop_names else None
        prop_in_sample = f", 手中@{sample_prop} (主角手中，按美术设定的真实尺寸描述，仅占画面 1/8 大小)" if sample_prop else ""
        sample_visual = (
            f"主体@{sample_char} (主体，占画面 2/3 高度) 流露专注又柔和的神情, "
            f"在@{sample_scene} (背景，占满画面) 中{prop_in_sample}, "
            f"low angle CU shot, dramatic backlighting, cinematic composition, "
            f"shallow depth of field, photorealistic detail"
        )

        # 时长约束 — 根据目标总时长动态计算建议 shot 数和单 shot 时长
        # 兜底：shot 数下限 ≥ 场景数，否则有些场景必然没法覆盖
        if target_duration and target_duration >= MIN_SHOT_SECONDS:
            min_shots_by_dur = max(1, math.ceil(target_duration / MAX_SHOT_SECONDS))
            min_shots = max(min_shots_by_dur, n_scenes or 1)
            max_shots = max(min_shots, target_duration // MIN_SHOT_SECONDS)
            avg_dur = max(MIN_SHOT_SECONDS, min(MAX_SHOT_SECONDS, round(target_duration / min_shots)))
            coverage_note = (
                f"（场景数={n_scenes}，所以 shot 数已抬到至少 {min_shots} 个以保证覆盖；"
                f"总时长会因此略超 {target_duration}s，没关系，先保覆盖）"
                if n_scenes > min_shots_by_dur else ""
            )
            duration_block = (
                f"## ⏱ 时长约束（必须严格遵守）\n"
                f"- 目标总时长：**{target_duration} 秒**（来自用户需求和剧本，超出 ±30% 会被自动裁剪尾部）\n"
                f"- 单 shot 时长：{MIN_SHOT_SECONDS}-{MAX_SHOT_SECONDS} 秒（视频模型最低 {MIN_SHOT_SECONDS} 秒）\n"
                f"- **shot 数：{min_shots}–{max_shots} 个，至少 {min_shots} 个**{coverage_note}\n"
                f"- total_duration_seconds 尽量接近 {target_duration}"
            )
            duration_rule = (
                f"至少 {min_shots} 个镜头，每个 {MIN_SHOT_SECONDS}–{MAX_SHOT_SECONDS} 秒，"
                f"总时长接近目标 {target_duration} 秒"
            )
        else:
            min_shots = max(1, n_scenes or 1)
            duration_block = (
                f"## ⏱ 时长约束\n"
                f"- 单 shot 时长：{MIN_SHOT_SECONDS}-{MAX_SHOT_SECONDS} 秒（视频模型最低 {MIN_SHOT_SECONDS} 秒）\n"
                f"- **shot 数至少 {min_shots} 个**（按场景数兜底）"
            )
            duration_rule = f"至少 {min_shots} 个镜头，每个 {MIN_SHOT_SECONDS}-{MAX_SHOT_SECONDS} 秒"

        retry_block = f"\n\n## ⚠ 重试提示\n{retry_hint}\n" if retry_hint else ""

        return f"""你是分镜导演，请将以下剧本拆解为可生成的视频分镜。{retry_block}
## 输入数据

剧本：
{script_json}

美术设定：
{art_json}

画面比例：{task.get("aspect_ratio", "9:16")}
风格：{task.get("style", "电影感")}

{duration_block}

## 可引用的角色（用 @<名字> 在 visual_prompt / video_prompt 中引用）：
{char_lines}

## 可引用的场景（用 @<名字> 在 visual_prompt / video_prompt 中引用）：
{scene_lines}

## 可引用的关键道具（用 @<名字> 在 visual_prompt / video_prompt 中引用，让该 shot 画面带上这件道具）：
{prop_lines}

## 你的任务
1. 根据剧本场景拆解镜头，**{duration_rule}**
2. 为每个镜头设计：景别、机位、运镜、主体、动作、情绪
3. **规划整片节奏 arc** — 给每个 shot 标 `pacing_role` ∈ {{establish, build, climax, rest, release}}；标 `continuity_to_next` ∈ {{match_cut, scale_jump, movement_continuation, contrast, hard_cut}}（最后一镜可省或填 hard_cut）；按对照表给 `transition_to_next` ∈ {{fade, dissolve, fadeblack, fadewhite, wipe_left, wipe_right, wipe_up, wipe_down, slide_left, slide_right, slide_up, slide_down, circleopen, circleclose, radial, smoothleft, smoothright, smoothup, smoothdown, hard_cut}}（最后一镜该字段填 null）
4. **每个 shot 必须设置 scene_id 字段**（取上方"可引用的场景"中对应场景的 ID，如 S01/S02），让系统能精准定位首帧
5. 生成英文 visual_prompt — **每个 shot 必须至少包含 1 个 @<场景名> 和 1 个 @<角色名> 引用**（从上方列表中选），名字必须精确匹配（含中文/分隔符），不要重复描述角色外观/服装；**当本镜的剧情用到上方"可引用的关键道具"时，必须在 visual_prompt 里 @<道具名>**，让关键帧合成时带上该道具图。**【0.6.4 硬约束】每个 @ 引用后必须用括号写出"画面占比 + 位置"**（如 `@小姑娘 (主体，占画面 2/3 高度)`、`@小熊玩偶 (抱在膝头，30cm 高，仅占画面 1/5)`、`@客厅 (背景，占满画面)`）。**道具必须复述美术设定里的真实尺寸**（不写就会被画成主体大小）
6. 生成英文 video_prompt — 同样必须有 @ 引用（角色 + 场景必有，道具在用到时必加） + **必须带主从尺寸括号**，描述动作/情绪/运镜
7. **从剧本场景里抽取对白/旁白/音效，分配到对应 shot**（这是音频环节的输入，不要丢）：
   - `dialogue`: 角色台词（保留原文中文，含说话人前缀更好，例 `"小美：今天去摘苹果！"`）；该 shot 无对白填 `""`
   - `narration`: 旁白/解说词（画面外的叙述）；无填 `""`
   - `sound_effects`: 数组形式列出本镜应有的环境音/特效音提示，例 `["树叶沙沙声", "远处鸟鸣"]`；无填 `[]`
   - 注意：一段对白通常跨 1-2 个 shot 完成，按时长合理切分；不要丢失任何剧本里出现的对白/旁白
8. 在顶层填 `shot_plan_summary` 一两句话描述整片节奏与情感曲线（供后续 audio agent 接力配乐参考）
9. **覆盖检查（硬约束 — 0.6.5 强制 100% 覆盖，否则系统会自动重跑你这一次输出）**：
   - **所有角色**（共 {n_chars} 个）必须至少在 1 个 shot 里通过 @<角色名> 引用
   - **所有场景**（共 {n_scenes} 个）必须至少在 1 个 shot 里通过 @<场景名> 引用 — 不要把多个场景的叙事压到同一个场景完成
   - **所有关键道具**（共 {n_props} 个）必须至少在 1 个 shot 里通过 @<道具名> 引用 — 道具是美术专门为剧情设计的，缺一个就少了一段视觉锚点；如果某个道具确实没法自然出现，至少安排一个特写 shot（"主角注视着这个 @道具"）
   - 如确实剧本叙事不需要某个场景/角色/道具，必须在 continuity_notes 里**逐条**写出"<名字> 未用因为..."，否则视为不合格
9. 严禁输出无 @ 引用、无 scene_id、duration_seconds < {MIN_SHOT_SECONDS} 的 shot

## @ 引用示例（重要）
visual_prompt 示例：
  "{sample_visual}"

注意：@ 后紧跟名字（不要加空格），名字必须从上面列表中选；其他动作、光影、镜头细节正常用英文描述。

## 景别
ELS极远景/LS远景/MS中景/CU近景/ECU特写

## 运镜
static固定/dolly_in推近/pan_left左摇/crane_up升/handheld手持/follow跟拍

## 输出格式（严格JSON）
{json.dumps({
    "storyboard": [
        {
            "shot_id": "SHOT_01",
            "scene_id": "S01",
            "duration_seconds": 5,
            "shot_size": "CU",
            "camera_angle": "eye_level",
            "camera_movement": "static",
            "subject": "主体",
            "action": "动作",
            "emotion": "情绪",
            "environment": "环境",
            "dialogue": "对白：角色说的话（无对白填空字符串）",
            "narration": "旁白：画面外解说（无旁白填空字符串）",
            "sound_effects": ["环境音/特效音提示数组，例：'树叶沙沙声' '远处鸟鸣'（无填空数组）"],
            "visual_prompt": f"... @{sample_char} ... @{sample_scene} ...",
            "video_prompt": f"... @{sample_char} ... @{sample_scene} ...",
            "pacing_role": "establish",
            "continuity_to_next": "scale_jump",
            "transition_to_next": "fade",
            "requires_keyframe": True,
            "depends_on_assets": ["C01", "S01"]
        }
    ],
    "shot_plan_summary": "整片节奏：从场景建立 → 情绪推进 → 高潮 → 释放结尾",
    "editing_rhythm": "剪辑节奏说明",
    "continuity_notes": "连贯性注意事项",
    "total_duration_seconds": 60
}, ensure_ascii=False, indent=2)}

请直接输出 JSON，不要其他内容。"""
    
    def _normalize(
        self,
        storyboard: Dict,
        target_duration: Optional[int] = None,
        scene_refs: Optional[List[Dict]] = None,
        character_refs: Optional[List[Dict]] = None,
        prop_refs: Optional[List[Dict]] = None,
    ) -> Dict:
        """规范化分镜 + 按 target_duration 兜底裁剪过长尾部 shot + 场景/角色/道具覆盖检查"""
        if "storyboard" not in storyboard:
            storyboard["storyboard"] = []
        if "editing_rhythm" not in storyboard:
            storyboard["editing_rhythm"] = "标准节奏"
        if "continuity_notes" not in storyboard:
            storyboard["continuity_notes"] = ""
        if "shot_plan_summary" not in storyboard:
            storyboard["shot_plan_summary"] = ""

        # 0.6.1 新增字段的允许集合（normalize 时把非法值矫正）
        _valid_pacing = {"establish", "build", "climax", "rest", "release"}
        _valid_continuity = {"match_cut", "scale_jump", "movement_continuation", "contrast", "hard_cut"}
        _valid_transitions = {
            "fade", "dissolve", "fadeblack", "fadewhite",
            "wipe_left", "wipe_right", "wipe_up", "wipe_down",
            "slide_left", "slide_right", "slide_up", "slide_down",
            "circleopen", "circleclose", "radial",
            "smoothleft", "smoothright", "smoothup", "smoothdown",
            "hard_cut",
        }

        # 为每个镜头补字段、强制 duration ∈ [MIN, MAX]
        _mention_pat = re.compile(r"@([\w一-鿿]+)")
        forced_keyframe_shots: List[str] = []
        n_total = len(storyboard["storyboard"])
        for i, shot in enumerate(storyboard["storyboard"]):
            if "shot_id" not in shot:
                shot["shot_id"] = f"SHOT_{i+1:02d}"
            ds = shot.get("duration_seconds", MIN_SHOT_SECONDS)
            try:
                ds = int(round(float(ds)))
            except (TypeError, ValueError):
                ds = MIN_SHOT_SECONDS
            shot["duration_seconds"] = max(MIN_SHOT_SECONDS, min(MAX_SHOT_SECONDS, ds))

            # 0.6.2: 对白/旁白/音效 默认兜底（LLM 没填就给空，确保下游字段稳定存在）
            if "dialogue" not in shot or shot.get("dialogue") is None:
                shot["dialogue"] = ""
            else:
                shot["dialogue"] = str(shot["dialogue"]).strip()
            if "narration" not in shot or shot.get("narration") is None:
                shot["narration"] = ""
            else:
                shot["narration"] = str(shot["narration"]).strip()
            sfx = shot.get("sound_effects") or []
            if isinstance(sfx, str):
                sfx = [sfx] if sfx.strip() else []
            elif not isinstance(sfx, list):
                sfx = []
            shot["sound_effects"] = [str(s).strip() for s in sfx if str(s).strip()]

            # 0.7.0 #4: 主体/动作/情绪/环境 默认兜底（两趟骨架产出，确保前端字段不空）
            # 0.7.4: 加 motivation（动机）兜底
            for _f in ("subject", "action", "emotion", "motivation", "environment"):
                v = shot.get(_f)
                shot[_f] = str(v).strip() if v else ""

            # 0.7.4: 场面调度字段兜底（shot_type/visible_characters/screen_position/eyeline_target）
            _valid_shot_types = {"establish", "master", "two_shot", "single", "ots", "reverse", "insert", "reaction"}
            _st = (shot.get("shot_type") or "").strip().lower()
            shot["shot_type"] = _st if _st in _valid_shot_types else ""
            _vc = shot.get("visible_characters")
            shot["visible_characters"] = [str(x).strip() for x in _vc if str(x).strip()] if isinstance(_vc, list) else []
            if not isinstance(shot.get("screen_position"), dict):
                shot["screen_position"] = {}
            if not isinstance(shot.get("eyeline_target"), dict):
                shot["eyeline_target"] = {}

            # 0.6.1: pacing_role 兜底 — 按位置推断
            pr = (shot.get("pacing_role") or "").strip().lower()
            if pr not in _valid_pacing:
                if i == 0:
                    pr = "establish"
                elif i == n_total - 1:
                    pr = "release"
                else:
                    pr = "build"
            shot["pacing_role"] = pr

            # 0.6.1: continuity_to_next 兜底 — 最后一镜可空，其余默认 hard_cut
            is_last = (i == n_total - 1)
            cn = (shot.get("continuity_to_next") or "").strip().lower()
            if is_last:
                shot["continuity_to_next"] = None
            else:
                shot["continuity_to_next"] = cn if cn in _valid_continuity else "hard_cut"

            # 0.6.1: transition_to_next 兜底 — 最后一镜为 null；其余非法值回落 fade
            tn = (shot.get("transition_to_next") or "").strip().lower() if shot.get("transition_to_next") else ""
            if is_last:
                shot["transition_to_next"] = None
            else:
                shot["transition_to_next"] = tn if tn in _valid_transitions else "fade"

            # 兜底：含 @ 引用的 shot 必须走 keyframe。否则 video 节点只拿场景图当 first_frame
            # （场景图按 SCENE_BG_SUFFIX 强制无人物）+ 文字描述，Seedance 只能脑补角色 → 形象漂移。
            visual = shot.get("visual_prompt", "") or ""
            video_p = shot.get("video_prompt", "") or ""
            # ②B：含 @ 引用，或本镜有角色出场（characters 非空）→ 一律建关键帧（双保险，保角色一致性；
            # 避免 LLM 把"氛围+角色"镜误判成空镜、又因 enrich 退化丢了 @ 引用而漏成非关键帧）
            has_chars = bool(shot.get("characters"))
            if _mention_pat.search(visual) or _mention_pat.search(video_p) or has_chars:
                if not shot.get("requires_keyframe"):
                    shot["requires_keyframe"] = True
                    forced_keyframe_shots.append(shot["shot_id"])
        if forced_keyframe_shots:
            logger.warning(
                "storyboard normalize 强制开启 requires_keyframe（shots=%s）：含 @ 引用或有角色出场但 LLM 未标 keyframe，"
                "为保角色/场景一致性已自动兜底（有角色的镜头一律建关键帧）",
                forced_keyframe_shots,
            )

        # 兜底：总时长远超目标时，从尾部丢 shot 直到落入容忍区间
        shots = storyboard["storyboard"]
        if target_duration and shots:
            limit = int(target_duration * TARGET_OVER_TOLERANCE)
            total = sum(s["duration_seconds"] for s in shots)
            dropped = 0
            while total > limit and len(shots) > 1:
                dropped_shot = shots.pop()
                total -= dropped_shot["duration_seconds"]
                dropped += 1
            if dropped:
                storyboard["dropped_tail_shots"] = dropped
                storyboard["editing_rhythm"] = (
                    f"{storyboard.get('editing_rhythm', '')}（自动裁剪 {dropped} 个尾部 shot 以贴近目标时长 {target_duration}s）"
                )

        storyboard["total_duration_seconds"] = sum(s["duration_seconds"] for s in storyboard["storyboard"])
        if target_duration:
            storyboard["target_duration_seconds"] = target_duration

        # ─── 场景 / 角色 / 道具 覆盖检查 ───
        # 从所有 shot 的 visual_prompt / video_prompt 抽 @ 引用，跟 *_refs 交叉。
        # 名字支持 "麻糬 / Mochi" 这种斜杠分隔的别名，任意一个被引用就算覆盖。
        if scene_refs or character_refs or prop_refs:
            import re as _re
            # 0.7.0 #1: ·・• 分隔 + 剥离括号注释，"小峰（林悦老公） · 颓败归家装" 拆出 "小峰"；mention 抽到的 @小峰 即可命中
            split_pat = _re.compile(r"[/／,，、·・•]")
            paren_pat = _re.compile(r"[（(][^）)]*[）)]")
            mention_pat = _re.compile(r"@([\w一-鿿]+)")

            mentioned: set = set()
            for shot in storyboard["storyboard"]:
                for text in (shot.get("visual_prompt", ""), shot.get("video_prompt", "")):
                    mentioned.update(mention_pat.findall(text or ""))

            def _aliases(name: str) -> List[str]:
                if not name:
                    return []
                out: List[str] = []
                for cand in [name.strip()] + [p.strip() for p in split_pat.split(name) if p.strip()]:
                    if cand and cand not in out:
                        out.append(cand)
                    stripped = paren_pat.sub("", cand).strip()
                    if stripped and stripped not in out:
                        out.append(stripped)
                return out

            uncovered_scenes = []
            for s in (scene_refs or []):
                loc = s.get("location") or ""
                if loc and not any(a in mentioned for a in _aliases(loc)):
                    uncovered_scenes.append(loc)

            uncovered_characters = []
            for c in (character_refs or []):
                nm = c.get("name") or ""
                if nm and not any(a in mentioned for a in _aliases(nm)):
                    uncovered_characters.append(nm)

            # 0.6.5: props 覆盖检查
            uncovered_props = []
            for p in (prop_refs or []):
                nm = p.get("name") or ""
                if nm and not any(a in mentioned for a in _aliases(nm)):
                    uncovered_props.append(nm)

            total_scenes = len([s for s in (scene_refs or []) if s.get("location")])
            total_chars = len([c for c in (character_refs or []) if c.get("name")])
            total_props = len([p for p in (prop_refs or []) if p.get("name")])

            coverage = {
                "scenes_total": total_scenes,
                "scenes_covered": total_scenes - len(uncovered_scenes),
                "characters_total": total_chars,
                "characters_covered": total_chars - len(uncovered_characters),
                "props_total": total_props,
                "props_covered": total_props - len(uncovered_props),
                "uncovered_scenes": uncovered_scenes,
                "uncovered_characters": uncovered_characters,
                "uncovered_props": uncovered_props,
            }
            storyboard["coverage"] = coverage
            if uncovered_scenes or uncovered_characters or uncovered_props:
                logger.warning(
                    "storyboard 覆盖不全：未用场景=%s 未用角色=%s 未用道具=%s（总 scenes=%d chars=%d props=%d）",
                    uncovered_scenes, uncovered_characters, uncovered_props,
                    total_scenes, total_chars, total_props,
                )

        return storyboard

    # ── 进阶模式聊天系统 prompt（override BaseAgent 默认）──

    def _chat_system_prompt(self) -> str:
        return """你是 Octopus 的分镜 Agent，创作者要你修改已生成的分镜或单个视频片段。

## 你能改的节点
- type="video_generation" 的节点（fan-out 出来的单个 video）
  - params 里有 prompt / first_frame_url / shot（含 visual_prompt / video_prompt / duration_seconds / camera_movement）
  - 修改时建议同步 params.prompt（最终给视频模型用的 prompt）和 params.shot.visual_prompt
- type="keyframe_generation" 的关键帧节点（params.prompt）
- type="storyboard" 的分镜总表（params.shots[i] / result.storyboard）——通常不推荐整体重跑，太重

## 怎么改
- "SHOT_03 改成推镜头" → 找 params.shot.shot_id=="SHOT_03" 的 video_generation 节点 → patch params.prompt（在尾部加 ", dolly_in camera, slow push'）+ should_rerun=true
- "SHOT_05 换成黄昏" → patch params.prompt 加 ", golden hour lighting'，should_rerun=true
- "重生成 SHOT_02 的视频" → 只 should_rerun=true，params_patch 留空
- 多个分镜同时改 → node_updates 数组多项

## 注意
- params_patch.prompt 应该是**最终完整英文 prompt**（包含场景/角色的 @ 引用替换后的版本）
- 时长改动：params.shot.duration_seconds 改后 should_rerun=true，但 Seedance 限制 4-12s
- 重生成而不改 prompt 是常用操作（视频模型每次输出有随机性）

## 关于创作者上传的参考图（Vision 模式 — 重要）

**你能直接看到创作者上传的参考图**。系统通过多模态 API 把图片传给你，
你能识别颜色 / 形状 / 文字 / logo / 构图等所有视觉细节。

写 Seedream prompt 时**基于你实际看到的细节**（具体颜色、文字内容、logo 形状），
而**不是凭空脑补**。

当 user prompt 里有"创作者附件（参考图）"段落时，创作者多半想在某一帧里**加新元素**（logo / 道具 / 角色）。

### 路径选择
1. **改 keyframe_generation 节点**（推荐）：keyframe 是用 image_to_image 多图融合生成的
   - keyframe 节点 params 里有 `image_urls`（原有场景图 + 角色图列表）
   - patch 要把**原有 image_urls + 上传的参考图 URL**合在一起一并塞回去
     （目标节点的 params.image_urls 在 target_nodes 里能看到，必须保留原数组再追加，不要丢）
   - prompt 里明确每张图的职能，例如：
     ```
     [原 keyframe prompt 的描述], with the logo from reference image N
     overlaid at the top-right corner, keep the original scene composition unchanged
     ```
2. **改 video_generation 节点的 first_frame_url**：仅在创作者明确说"用图 1 当首帧"时
   - params_patch = {"first_frame_url": "图 1 URL"}, should_rerun=true
   - 注意 image_to_video 只接受单张首帧，不能多图

### 典型场景模板
- "把这个 logo 放到 SHOT_03 右上角" → 改 SHOT_03 对应的 keyframe 节点：
  - **vision 模式下你应该看清楚 logo 的颜色 / 形状 / 文字**，写进 prompt：
  ```
  params_patch = {
    "image_urls": [...原有列表..., "用户上传的 logo URL"],
    "prompt": "[原 prompt], with [具体描述：a white 'ACME' wordmark logo with angular sans-serif font] from reference image N overlaid at top-right corner, preserve scene composition and character",
  }, should_rerun: true
  ```
- "加一个虚拟道具在角色手上" → 同上路径，prompt 强调 "holding the [道具] from reference image N"
- "这个镜头换个运镜" → 不需要参考图，直接 patch prompt 加 ", dolly_in push camera"

## 输出格式（严格 JSON）
{
  "reply": "分镜师式简短回应（≤80 字）",
  "node_updates": [
    {"node_id": "节点id",
     "params_patch": {"prompt": "新完整 prompt", "image_urls": [...完整数组...]},
     "should_rerun": true,
     "reason": "..."}
  ]
}
"""
