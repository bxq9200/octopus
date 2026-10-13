"""RouterAgent — 进阶模式下的聊天意图路由器。

职责单一：看创作者一句话 + 项目结构最小集，判断
  - target_agent: 应该让哪个 agent 处理（5 个之一 + 兜底 director）
  - target_node_ids: 要让 agent 改哪些具体节点（0-N 个）
  - summary: 用一句话描述用户意图，交给目标 agent 用

不做：具体怎么改（交给 agent.chat()）；多轮上下文记忆（每次 stateless）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.agents.base.base_agent import BaseAgent
from app.core.agent_runtime import cloud_method  # 护城河：cloud_client 下整体路由云端

logger = logging.getLogger(__name__)


ROUTER_SYSTEM_PROMPT = """你是一个意图路由器。创作者在视频制作工作流中跟系统对话，你要把消息派给最合适的 agent。

## 可路由的 agent
- director: 跨阶段调度。处理"改剧本/换平台/换风格"等大改动，或闲聊、状态查询、不属于其他 agent 的兜底
- screenwriter: 改 script 节点的剧本字段（场景描述、对白等）
- art_director: 改 character/scene 子节点的视觉描述、重生成角色或场景图
- storyboard: 改 storyboard 节点的镜头（visual_prompt / video_prompt / 时长 / 运镜），或重生成单个视频片段
- publisher: 改 publish 节点的标题 / 简介 / 标签 / 封面选择

## 路由规则
1. 看创作者消息提到了什么对象：
   - "角色 / 立绘 / 形象 / 颜色 / 外貌 / 场景图 / 背景图" → art_director
   - "分镜 / 镜头 / SHOT / 关键帧 / keyframe / 首帧 / 运镜 / 推镜 / 拉镜" → storyboard（关键帧+视频节点都归分镜 agent）
   - "标题 / 简介 / 标签 / 封面" → publisher
   - "剧本 / 对白 / 旁白 / 情节" → screenwriter（但若已经进入下游 phase，需谨慎，因为可能影响下游）
   - 否则 director
2. 看项目结构里有哪些具体节点能匹配（节点列表里 keyframe/video 节点带 shot_id 字段）：
   - 用户说"麻糬眼睛改大" → 项目里有 label="🎭 麻糬" 的 character 节点 → target_node_ids 填那个节点 id
   - 用户说"SHOT_03 加推镜头 / 重做 SHOT_03 视频" → 找 type=video_generation 且 shot_id=="SHOT_03" 的节点
   - 用户说"重做 shot08 关键帧 / shot8 关键帧重新生成" → 找 type=keyframe_generation 且 shot_id 匹配 8（SHOT_08）的节点
   - shot 号可能写成 shot08 / SHOT_8 / 镜头8 / 第8个镜头，一律按数字去匹配 shot_id
3. 找不到具体节点 → target_node_ids 为空数组，agent 拿全局上下文
4. summary 用一句话复述用户想做什么（给目标 agent 看）

## 输出格式（严格 JSON）
{
  "target_agent": "director|screenwriter|art_director|storyboard|publisher",
  "target_node_ids": ["..."],
  "summary": "用户想..."
}

只输出 JSON，不要其他文字。"""


class RouterAgent(BaseAgent):
    name = "router"
    description = "聊天意图路由器"
    system_prompt = ROUTER_SYSTEM_PROMPT

    VALID_AGENTS = {"director", "screenwriter", "art_director", "storyboard", "publisher"}

    async def execute(self, task: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        """BaseAgent 抽象方法兜底实现 — RouterAgent 实际用 route()"""
        return await self.route(
            message=task.get("message", ""),
            project_summary=task.get("project_summary") or {},
            mode=task.get("mode", "advanced"),
            current_phase=task.get("current_phase", ""),
        )

    @cloud_method
    async def route(
        self,
        message: str,
        project_summary: Dict[str, Any],
        mode: str = "advanced",
        current_phase: str = "",
    ) -> Dict[str, Any]:
        """单次 LLM 调用 → 路由决策。失败时降级到 director 兜底。"""
        if not message or not message.strip():
            return {"target_agent": "director", "target_node_ids": [], "summary": ""}

        # 0.7.1: 去掉 indent（省字符）+ 上限 3000→16000。旧的 indent=2 + 3000 会把 48 镜 episode
        # 上百个排在末尾的 per-shot keyframe/video 节点全截掉 → router 看不到 → "重做 shotNN" 解析不到节点。
        summary_block = json.dumps(project_summary, ensure_ascii=False)[:16000]

        user_prompt = f"""## 当前 phase
{current_phase or '未知'}

## 创作模式
{mode}

## 项目结构（节点列表 + collected_info）
{summary_block}

## 创作者最新消息
{message}

请按 system prompt 的 JSON schema 输出路由决策。"""

        try:
            response = await self.llm_generate(
                prompt=user_prompt,
                system=ROUTER_SYSTEM_PROMPT,
                temperature=0.0,
                json_mode=True,
            )
        except Exception as e:
            logger.warning("RouterAgent LLM 调用失败，降级 director: %s", e)
            return {"target_agent": "director", "target_node_ids": [], "summary": message[:100]}

        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            # 兜底：抽 markdown code block
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", response, re.DOTALL)
            data = {}
            if m:
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    data = {}
            if not data:
                logger.warning("RouterAgent JSON 解析失败，降级 director: %s", response[:200])
                return {"target_agent": "director", "target_node_ids": [], "summary": message[:100]}

        target_agent = (data.get("target_agent") or "director").strip()
        if target_agent not in self.VALID_AGENTS:
            target_agent = "director"

        target_node_ids = data.get("target_node_ids") or []
        if not isinstance(target_node_ids, list):
            target_node_ids = []
        target_node_ids = [str(x) for x in target_node_ids if x]

        return {
            "target_agent": target_agent,
            "target_node_ids": target_node_ids,
            "summary": (data.get("summary") or message[:100]).strip()[:300],
        }
