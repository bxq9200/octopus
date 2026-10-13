"""
交互式工作流执行引擎

设计：
- 每个阶段（script / art / storyboard / video / merge）由一个 PhaseSpec 描述：
  * runner       —— 调用 agent 或模块跑出原始结果
  * outputs_key  —— 节点 outputs 字段里存哪个 key
  * post_execute —— 执行完后的钩子（art 生成图片子节点；storyboard 更新子分镜）
  * formatter    —— 给前端的 result_preview 文案
- 入口只有三个：handle_initial_chat / confirm_node / retry_node
- 节点状态机由 _run_with_status 统一管理（IDLE → RUNNING → COMPLETED/FAILED）
- 阶段推进由 director.on_node_confirmed 决定下一步，再由 PHASES 配置创建下一节点
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.agents.art_director.art_director_agent import ArtDirectorAgent
from app.agents.director.director_agent import DirectorAgent
from app.agents.module_router.module_router_agent import ModuleRouterAgent
from app.agents.publisher.publisher_agent import PublisherAgent
from app.agents.screenwriter.screenwriter_agent import ScreenwriterAgent
from app.agents.storyboard.storyboard_agent import StoryboardAgent
from app.engine.module_registry import ModuleRegistry
from app.engine.workflow_state import (
    NodeStatus,
    ProjectPhase,
    workflow_state,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 阶段配置
# ═══════════════════════════════════════════════════════

@dataclass
class PhaseSpec:
    """单个阶段的执行规范"""
    phase: str                         # "script" / "art" / ...
    node_type: str                     # 节点 type 字段
    label: str                         # 节点显示名
    agent_name: str                    # 哪个 agent / module 负责
    project_phase: ProjectPhase        # 推进到的项目级阶段
    outputs_key: str                   # outputs 字典里的主 key
    next_phase: Optional[str] = None   # 默认下一阶段（可被 director 覆盖）

    # phase 内部使用的回调（运行时由 InteractiveExecutor 注入）
    runner: Optional[Callable] = None
    post_execute: Optional[Callable] = None
    formatter: Optional[Callable] = None


# 节点 type → phase 反向映射
NODE_TYPE_TO_PHASE = {
    "script_generation": "script",
    "art_design": "art",
    "art_direction": "art",
    "storyboard_design": "storyboard",
    "storyboard_generation": "storyboard",
    "keyframe_generation": "keyframe",
    "video_generation": "video",
    "asset_generation": "video",
    # 0.6.1: 音频导演 + Mubert BGM（独立 phase 字符串，避免 _execute_node 路由冲突）
    "audio_plan": "audio",
    "bgm_fetch": "bgm",
    # 0.7.2 ②：专家分层 BGM —— series 风格方向（master）+ episode 场景 BGM 计划（分集）
    "bgm_direction": "bgm_direction",
    "episode_audio_plan": "episode_audio",
    "video_merge": "merge",
    "publish": "publish",
    # 1.1.0 MV 模式：歌曲解析 → MV 剧本 →（复用 art）→ MV 卡点分镜 →（复用 keyframe/video/merge）
    "mv_audio_analysis": "mv_audio",
    "mv_script_design": "mv_script",
    "mv_storyboard_design": "mv_storyboard",
    # 0.6.5: 成本总结独立节点（publish 确认后自动追加）
    "cost_summary": "cost",
}


def resolve_phase(node: Dict[str, Any]) -> str:
    """从节点里推断 phase"""
    if node.get("phase"):
        return node["phase"]
    return NODE_TYPE_TO_PHASE.get(node.get("type", ""), "")


def _bgm_default_on() -> bool:
    """0.6.2: BGM 默认开关由 audio provider 决定。
    ElevenLabs 质量稳定，默认开；Mubert 质量不稳，默认关让用户主动选。
    """
    try:
        from app.core.runtime_config import active_audio_provider_name
        return active_audio_provider_name() == "elevenlabs"
    except Exception:
        return False


# ═══════════════════════════════════════════════════════
# 主类
# ═══════════════════════════════════════════════════════

class InteractiveExecutor:
    """交互式执行引擎"""

    def __init__(self, project_id: str, ws_callback: Optional[Callable] = None):
        self.project_id = project_id
        self.ws_callback = ws_callback
        self.state = workflow_state

        # 把 ws_callback 注册到 workflow_state，这样 create_node / update_node /
        # set_node_status 触发的 _notify 会自动同步到前端，避免每个调用点都手动 notify。
        if ws_callback is not None:
            self.state.register_ws_callback(project_id, ws_callback)

        self.director = DirectorAgent()
        self.module_router = ModuleRouterAgent()
        self.screenwriter = ScreenwriterAgent()
        self.art_director = ArtDirectorAgent()
        self.storyboard = StoryboardAgent()
        self.publisher = PublisherAgent()
        # 0.6.1: 音频导演 agent
        from app.agents.audio_director.audio_director_agent import AudioDirectorAgent
        self.audio_director = AudioDirectorAgent()
        # 1.1.0 MV 导演
        from app.agents.mv_director.mv_director_agent import MVDirectorAgent
        self.mv_director = MVDirectorAgent()
        # 进阶模式聊天意图路由（0.6.0 阶段 ②b）
        from app.agents.router.router_agent import RouterAgent
        self.router = RouterAgent()

        # 装配阶段配置（绑定 self 上的回调）
        self.phases: Dict[str, PhaseSpec] = {
            # ── 1.1.0 MV 模式：只有 mode="mv" 的项目会走这三个阶段 ──
            # 之后的 art / keyframe / video / merge 全部复用现有阶段，零改动。
            "mv_audio": PhaseSpec(
                phase="mv_audio",
                node_type="mv_audio_analysis",
                label="歌曲解析",
                agent_name="mv_director",
                project_phase=ProjectPhase.MV_AUDIO,
                outputs_key="mv_audio",
                next_phase="mv_script",
                runner=self._run_mv_audio,
                formatter=self._format_mv_audio_preview,
            ),
            "mv_script": PhaseSpec(
                phase="mv_script",
                node_type="mv_script_design",
                label="MV 剧本",
                agent_name="mv_director",
                project_phase=ProjectPhase.SCRIPT,
                outputs_key="script",
                next_phase="art",          # 美术阶段原样复用
                runner=self._run_mv_script,
                formatter=self._format_mv_script_preview,
            ),
            "mv_storyboard": PhaseSpec(
                phase="mv_storyboard",
                node_type="mv_storyboard_design",
                label="MV 分镜",
                agent_name="mv_director",
                project_phase=ProjectPhase.STORYBOARD,
                outputs_key="storyboard",
                next_phase="keyframe",
                runner=self._run_mv_storyboard,
                post_execute=self._post_storyboard,
                formatter=self._format_storyboard_preview,
            ),
            "script": PhaseSpec(
                phase="script",
                node_type="script_generation",
                label="剧本生成",
                agent_name="screenwriter",
                project_phase=ProjectPhase.SCRIPT,
                outputs_key="script",
                next_phase="art",
                runner=self._run_script,
                formatter=self._format_script_preview,
            ),
            "art": PhaseSpec(
                phase="art",
                node_type="art_design",
                label="美术设定",
                agent_name="art_director",
                project_phase=ProjectPhase.ART,
                outputs_key="art_direction",
                next_phase="storyboard",
                runner=self._run_art,
                post_execute=self._post_art,
                formatter=self._format_art_preview,
            ),
            # 0.7.0 轮 1：Episode 专用 art 阶段 — 在 master shared_assets 基础上细化
            # （区别于上面的 art：master 项目跑 art，Episode 项目跑 episode_art）
            "episode_art": PhaseSpec(
                phase="episode_art",
                node_type="episode_art_design",
                label="本集美术",
                agent_name="art_director",
                project_phase=ProjectPhase.ART,
                outputs_key="art_direction",
                next_phase="storyboard",
                runner=self._run_episode_art,
                post_execute=self._post_episode_art,
                formatter=self._format_episode_art_preview,
            ),
            "storyboard": PhaseSpec(
                phase="storyboard",
                node_type="storyboard_design",
                label="分镜设计",
                agent_name="storyboard",
                project_phase=ProjectPhase.STORYBOARD,
                outputs_key="storyboard",
                next_phase="keyframe",
                runner=self._run_storyboard,
                post_execute=self._post_storyboard,
                formatter=self._format_storyboard_preview,
            ),
            "keyframe": PhaseSpec(
                phase="keyframe",
                node_type="keyframe_generation",
                label="关键帧生成",
                agent_name="module_router",
                project_phase=ProjectPhase.KEYFRAME,
                outputs_key="keyframe",
                next_phase="video",
                runner=self._run_keyframe,
                formatter=self._format_keyframe_preview,
            ),
            "video": PhaseSpec(
                phase="video",
                node_type="video_generation",
                label="视频生成",
                agent_name="module_router",
                project_phase=ProjectPhase.VIDEO,
                outputs_key="video",
                next_phase="merge",
                runner=self._run_video,
                formatter=self._format_video_preview,
            ),
            # 0.6.1: 音频规划阶段（不串到下游，与 keyframe/video 并行）
            "audio": PhaseSpec(
                phase="audio",
                node_type="audio_plan",
                label="音频导演",
                agent_name="audio_director",
                project_phase=ProjectPhase.AUDIO,
                outputs_key="audio_plan",
                next_phase=None,
                runner=self._run_audio_plan,
                post_execute=self._post_audio_plan,
            ),
            # 0.6.1: Mubert BGM 实际生成节点；phase 字符串单独命名 "bgm" 避免
            # 跟 audio_plan 共享 phase 时被 _execute_node 误派到 audio_plan runner。
            # project_phase 仍归类到 AUDIO（项目阶段层面是同一段）。
            "bgm": PhaseSpec(
                phase="bgm",
                node_type="bgm_fetch",
                label="BGM 生成",
                agent_name="module_router",
                project_phase=ProjectPhase.AUDIO,
                outputs_key="bgm",
                next_phase=None,
                runner=self._run_bgm_fetch,
            ),
            # 0.7.2 ②：series 级 BGM 风格方向（master canvas，自动跑展示；落 series.script_summary）
            "bgm_direction": PhaseSpec(
                phase="bgm_direction",
                node_type="bgm_direction",
                label="BGM 风格",
                agent_name="audio_director",
                project_phase=ProjectPhase.AUDIO,
                outputs_key="bgm_direction",
                next_phase=None,
                runner=self._run_bgm_direction,
            ),
            # 0.7.2 ②：episode 场景 BGM 计划（分集；自动跑规划，**确认后**才 spawn bgm_fetch 花钱生成）
            "episode_audio": PhaseSpec(
                phase="episode_audio",
                node_type="episode_audio_plan",
                label="本集音频",
                agent_name="audio_director",
                project_phase=ProjectPhase.AUDIO,
                outputs_key="episode_audio_plan",
                next_phase=None,
                runner=self._run_episode_audio_plan,
            ),
            "merge": PhaseSpec(
                phase="merge",
                node_type="video_merge",
                label="视频合并",
                agent_name="module_router",
                project_phase=ProjectPhase.MERGE,
                outputs_key="merged_video",
                next_phase="publish",
                runner=self._run_merge,
                formatter=self._format_merge_preview,
            ),
            # 0.7.7 本地化：合成后、发布前的可选视频翻译节点（手动触发，不自动跑、不 chain）
            "localization": PhaseSpec(
                phase="localization",
                node_type="localization",
                label="本地化翻译",
                agent_name="module_router",
                project_phase=ProjectPhase.PUBLISH,
                outputs_key="localization",
                next_phase=None,
                runner=self._run_localization,
            ),
            "publish": PhaseSpec(
                phase="publish",
                node_type="publish",
                label="发布",
                agent_name="publisher",
                project_phase=ProjectPhase.PUBLISH,
                outputs_key="publish",
                next_phase=None,
                runner=self._run_publish,
                formatter=self._format_publish_preview,
            ),
            # 0.6.5: 成本总结（publish 确认后自动追加）
            "cost": PhaseSpec(
                phase="cost",
                node_type="cost_summary",
                label="成本总结",
                agent_name="cost_aggregator",
                project_phase=ProjectPhase.COMPLETED,
                outputs_key="cost_summary",
                next_phase=None,
                runner=self._run_cost_summary,
                formatter=self._format_cost_preview,
            ),
        }

    # ─── 通知 ───

    async def _notify(self, msg_type: str, data: Dict[str, Any]) -> None:
        if not self.ws_callback:
            return
        msg = {"type": msg_type, "project_id": self.project_id, **data}
        result = self.ws_callback(msg)
        if asyncio.iscoroutine(result):
            await result

    async def _progress(self, node_id: str, message: str, percent: Optional[float] = None) -> None:
        """0.6.5 进度反馈 helper：往前端推 node_progress 事件（不入 DB，纯瞬态）。
        节点跑完 status 自动 completed，前端 progress 也跟着清空。"""
        payload: Dict[str, Any] = {"node_id": node_id, "message": message}
        if percent is not None:
            payload["progress"] = float(percent)
        await self._notify("node_progress", payload)

    # ═══════════════════════════════════════════════════════
    # 入口 1：初始对话 → 收集需求
    # ═══════════════════════════════════════════════════════

    async def handle_initial_chat(
        self,
        message: str,
        context: Optional[Dict] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        project = self.state.get_project(self.project_id) or self.state.create_project(self.project_id)

        history = project.get("chat_history", [])
        history_entry: Dict[str, Any] = {
            "role": "user", "content": message, "time": datetime.utcnow().isoformat(),
        }
        if attachments:
            history_entry["attachments"] = attachments
        history.append(history_entry)
        self.state.update_project(self.project_id, {"chat_history": history})

        # 0.6.0 / 0.7.0 模式分流：
        # - expert  → handle_expert_chat（专家模式：通读剧本 → 分集 → fan-out episodes，0.7.0 引入）
        # - advanced + 未进入下游 phase → handle_advanced_chat（剧本协商）
        # - advanced + 已进入下游 phase → handle_creator_chat（与各 agent 对话修改）
        # - 其他模式 → 现有 director.execute checklist 链路
        mode = (project.get("config") or {}).get("mode", "simple")
        if mode == "mv":
            # 还没开工：输入是音频+参考图，聊天框只做引导，不触发生成。
            if not (project.get("config") or {}).get("mv"):
                return {
                    "type": "message",
                    "message": "🎵 这是一个 MV 项目。请在左侧 MV 面板上传歌曲与风格参考图后开始生成；"
                               "歌曲解析完成后可以在画布上校对歌词时间轴。",
                    "project_id": self.project_id,
                }
            # 已经在跑：跟进阶模式一样交给创作者对话，能 @美术 / @分镜 改具体节点。
            # 🔴 原来这里无条件返回引导语，等于 MV 项目永远没法跟 agent 对话——
            #    而 MV 恰恰最需要改（"这镜换个机位""场景改成黄昏"）。
            return await self.handle_creator_chat(message, history, context, attachments=attachments)
        if mode == "expert":
            return await self.handle_expert_chat(message, history, context, attachments=attachments)
        if mode == "advanced":
            phase = project.get("phase") or ProjectPhase.INITIAL
            pre_phases = {ProjectPhase.INITIAL, ProjectPhase.PLAN_CONFIRMED}
            # PLAN_CONFIRMED 是"刚刚确认完，script 节点已创建"的瞬时态。
            # 一旦 phase 推进到 SCRIPT/ART/... 之后，就让 creator_chat 接管。
            if phase in pre_phases and not project.get("advanced_confirmed_at"):
                return await self.handle_advanced_chat(message, history, context, attachments=attachments)
            return await self.handle_creator_chat(message, history, context, attachments=attachments)

        director_result = await self.director.execute({
            "request": message,
            "history": [h["content"] for h in history],
            "context": context or {},
        })

        decision = director_result.get("decision", "ask")

        if decision == "ask":
            self.state.update_project(self.project_id, {
                "collected_info": director_result.get("collected_info", {}),
            })
            return {
                "type": "ask",
                "message": director_result.get("next_action", "请告诉我更多关于您的创作需求"),
                "questions": director_result.get("questions", []),
                "default_plan": director_result.get("default_plan", {}),
                "checklist_status": director_result.get("checklist_status", {}),
                "project_id": self.project_id,
            }

        if decision in ("plan", "suggest"):
            collected_info = director_result.get("collected_info", {})
            self.state.update_project(self.project_id, {
                "execution_plan": {
                    "mode": director_result.get("mode"),
                    "default_plan": director_result.get("default_plan", {}),
                    "workflow_summary": director_result.get("project_intent", ""),
                    "plan_summary": director_result.get("plan_summary", ""),
                },
                "collected_info": collected_info,
                "confirmed_nodes": [],
            })

            return {
                "type": "plan",
                "message": self._format_plan_message(director_result.get("default_plan", {}), director_result),
                "plan": director_result.get("default_plan", {}),
                "adjustable_items": director_result.get("adjustable_items", []),
                "plan_summary": director_result.get("plan_summary", ""),
                "project_id": self.project_id,
                "need_confirm": True,
            }

        return {
            "type": "text",
            "message": director_result.get("next_action", "我是 Octopus 导演 Agent，请告诉我您想创作什么视频？"),
            "project_id": self.project_id,
        }

    # ═══════════════════════════════════════════════════════
    # 入口 2：确认方案 → 创建第一个节点
    # ═══════════════════════════════════════════════════════

    async def confirm_plan(self) -> Dict[str, Any]:
        project = self.state.get_project(self.project_id)
        if not project:
            return {"error": "项目不存在"}
        plan = project.get("execution_plan") or {}
        if not plan:
            return {"error": "没有可确认的执行方案"}

        self.state.set_project_phase(self.project_id, ProjectPhase.PLAN_CONFIRMED)

        collected_info = project.get("collected_info", {})
        default_plan = plan.get("default_plan", {})

        script_node = self._create_phase_node(
            phase="script",
            node_template_id="node_001",
            depends_on=[],
            position={"x": 100, "y": 200},
            params={
                "topic": default_plan.get("theme", ""),
                "style": default_plan.get("style", ""),
                "duration": default_plan.get("duration", "60秒"),
                "audience": default_plan.get("audience", ""),
                "mood": default_plan.get("mood", ""),
                "project_type": plan.get("mode", "社媒短视频"),
                "platform": collected_info.get("platform", ""),
                "aspect_ratio": collected_info.get("aspect_ratio", ""),
            },
        )

        await self._notify("new_node", {
            "message": "✅ 方案已确认！开始生成剧本...",
            "node": script_node,
        })

        asyncio.create_task(self._execute_node(script_node["id"]))

        return {
            "type": "execution_started",
            "message": "方案已确认，剧本生成节点已创建并开始执行",
            "node_id": script_node["id"],
            "phase": "script",
        }

    # ═══════════════════════════════════════════════════════
    # 进阶模式 (advanced)：与导演多轮协商剧本大纲
    # ═══════════════════════════════════════════════════════

    async def handle_advanced_chat(
        self,
        message: str,
        history: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """进阶模式聊天入口。每条消息：先看是否"确认意图"，否则跑 refine_outline。"""
        project = self.state.get_project(self.project_id) or {}
        current_outline = project.get("advanced_outline")
        collected_info = dict(project.get("collected_info") or {})
        history_strs = [h.get("content", "") for h in history if isinstance(h, dict)]

        # 0.6.3: 先抓 collected_info 并立即持久化 —— 无论后续走哪条路径（confirm/refine/异常）都生效
        # 否则用户说了"画面要横屏"但下一条说"可以"触发 confirm_intent 短路，aspect_ratio 永远丢失
        try:
            all_history = history_strs + [message]
            detected = self.director._extract_collected_info(all_history, context or {})
            changed = {}
            for k in ("aspect_ratio", "platform", "duration", "style", "mood"):
                v = detected.get(k)
                if v and collected_info.get(k) != v:
                    collected_info[k] = v
                    changed[k] = v
            if changed:
                self.state.update_project(self.project_id, {"collected_info": collected_info})
                logger.info("advanced_chat 更新 collected_info: %s", changed)
        except Exception as e:
            logger.warning("进阶模式 extract collected_info 失败: %s", e)

        # 已有草稿时，先做一次轻量"确认意图"判断
        # （第一次进来没草稿，跳过；草稿才进入"可确认"语义范围）
        if current_outline:
            try:
                if await self.director.detect_confirm_intent(message):
                    return await self.confirm_advanced_outline()
            except Exception as e:
                logger.warning("detect_confirm_intent 失败，继续走 refine: %s", e)

        # 协商：让 director 增量更新 outline
        try:
            refined = await self.director.refine_outline(
                message=message,
                history=history_strs,
                current_outline=current_outline,
                collected_info=collected_info,
                attachments=attachments,
            )
        except Exception as e:
            logger.exception("refine_outline 失败")
            return {
                "type": "text",
                "message": f"导演这边出了点小问题，再说一遍：{str(e)[:120]}",
                "project_id": self.project_id,
            }

        new_outline = refined.get("outline") or current_outline or {}
        reply = refined.get("reply") or ""
        ready_to_confirm = bool(refined.get("ready_to_confirm", False))

        # 持久化：outline + chat_history（collected_info 已在上方独立持久化）
        history.append({
            "role": "agent",
            "content": reply,
            "time": datetime.utcnow().isoformat(),
        })
        self.state.update_project(self.project_id, {
            "advanced_outline": new_outline,
            "chat_history": history,
            "collected_info": collected_info,
        })

        return {
            "type": "outline_draft",
            "message": reply,
            "outline": new_outline,
            "ready_to_confirm": ready_to_confirm,
            "missing_aspects": refined.get("missing_aspects") or [],
            "project_id": self.project_id,
        }

    async def confirm_advanced_outline(self) -> Dict[str, Any]:
        """用户确认剧本大纲 → 创建 script 节点（params 带 outline）→ 走下游与 simple 模式一致。"""
        project = self.state.get_project(self.project_id)
        if not project:
            return {"error": "项目不存在"}
        outline = project.get("advanced_outline")
        if not outline:
            return {"error": "尚无可确认的剧本大纲，请先与导演协商"}

        self.state.set_project_phase(self.project_id, ProjectPhase.PLAN_CONFIRMED)
        self.state.update_project(self.project_id, {
            "advanced_confirmed_at": datetime.utcnow().isoformat(),
        })

        collected_info = project.get("collected_info") or {}

        # 从 outline 推 script 节点的基本参数
        topic = outline.get("title") or outline.get("logline") or ""
        tone = outline.get("tone") or ""
        scenes = outline.get("scenes") or []
        total_dur = sum(int(s.get("duration_seconds") or 5) for s in scenes if isinstance(s, dict))
        duration_str = f"{total_dur}秒" if total_dur else collected_info.get("duration", "60秒")

        script_node = self._create_phase_node(
            phase="script",
            node_template_id="node_001",
            depends_on=[],
            position={"x": 100, "y": 200},
            params={
                "topic": topic,
                "style": collected_info.get("style", ""),
                "duration": duration_str,
                "audience": collected_info.get("audience", ""),
                "mood": tone or collected_info.get("mood", ""),
                "project_type": "advanced_outline",
                "platform": collected_info.get("platform", ""),
                "aspect_ratio": collected_info.get("aspect_ratio", ""),
                # 关键：把已协商的 outline 完整传给 screenwriter
                "outline": outline,
            },
        )

        await self._notify("new_node", {
            "message": "✅ 剧本已确认！开始按大纲细化剧本...",
            "node": script_node,
        })
        asyncio.create_task(self._execute_node(script_node["id"]))

        return {
            "type": "advanced_confirmed",
            "message": "剧本已确认，开始细化并进入下游创作...",
            "node_id": script_node["id"],
            "phase": "script",
        }

    # ═══════════════════════════════════════════════════════
    # 专家模式 (expert) — 0.7.0：通读剧本 → 协商分集 → fan-out episodes
    # ═══════════════════════════════════════════════════════

    async def handle_expert_chat(
        self,
        message: str,
        history: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """0.7.0 专家模式 master 项目聊天入口。

        分支按 series.status 而非 shared_assets.frozen_at（避免 confirm 后 art 未冻结时
        仍然走"refine breakdown"导致改剧本去了）：
        - draft     → branch 1（首次上传剧本）/ branch 4（友好引导）
        - breakdown → branch 2（多轮微调或 confirm 意图）
        - visual_dev / producing / done → branch 3（handle_creator_chat 与 art / storyboard 等对话）
        """
        series_id = await self._get_series_id_for_project()
        if not series_id:
            return {
                "type": "text",
                "message": "当前项目未关联剧集（专家模式请通过「剧集」tab 新建剧集）",
                "project_id": self.project_id,
            }
        series = await self._load_series(series_id)
        if not series:
            return {
                "type": "text",
                "message": "剧集数据缺失,请重新创建剧集",
                "project_id": self.project_id,
            }

        history_strs = [h.get("content", "") for h in history if isinstance(h, dict)]
        status = series.get("status") or "draft"

        # 0.7.1 修复：捕获用户显式画面比例（横屏/竖屏），覆盖脚本解析默认值 —— script_summary.suggested_aspect_ratio
        # 是专家全链路真值源（confirm_expert_breakdown / 分集美术 / fan-out 都读它）。
        # 0.7.2 修：兜底**只认 user 消息、从最新往旧逐条取第一个命中** —— 旧版 join 全部历史（含导演话术
        # "建议横屏16:9（漫剧一般竖屏…）"同时含两词，且 9:16 词组先匹配）→ 用户确认横版后的下一条
        # 无比例词消息会把比例覆盖回竖屏（"从军行"横版变竖版的根因）。
        forced_ar = self._detect_aspect_ratio(message)
        if not forced_ar:
            for _m in reversed([h.get("content", "") for h in history
                                if isinstance(h, dict) and h.get("role") == "user"]):
                forced_ar = self._detect_aspect_ratio(_m)
                if forced_ar:
                    break
        if forced_ar and series.get("script_summary"):
            ss = dict(series.get("script_summary") or {})
            if ss.get("suggested_aspect_ratio") != forced_ar:
                ss["suggested_aspect_ratio"] = forced_ar
                await self._save_series_fields(series_id, {"script_summary": ss})
                series["script_summary"] = ss
                logger.info("专家模式：用户指定画面比例=%s，已更新 script_summary.suggested_aspect_ratio", forced_ar)

        # 找出本轮上传的剧本附件(kind == "script")
        script_attachment = None
        for a in (attachments or []):
            if isinstance(a, dict) and a.get("kind") == "script":
                script_attachment = a
                break

        # ─── 0. 已 confirm 分集 (visual_dev / producing / done) ───
        if status in ("visual_dev", "producing", "done"):
            # 0a. 用户明确想"重新分集" → 退回 breakdown 协商（不要走 creator_chat 误判成重跑节点）
            RE_BREAKDOWN_KW = ("重新分集", "重新分", "重分集", "改分集", "重新切", "集数", "分集结构", "重新规划")
            if series.get("script_summary") and any(kw in message for kw in RE_BREAKDOWN_KW):
                await self._save_series_fields(series_id, {"status": "breakdown"})
                status = "breakdown"  # 落到下面 branch 2 重新协商
                await self._notify("agent_thinking", {"message": "📝 回到分集协商，重新规划每集角色和场景..."})
            else:
                # 0b. 其它消息（改场景/改美术等）→ creator_chat
                return await self.handle_creator_chat(message, history, context, attachments)

        # ─── 1. 首次有剧本附件 + series 还没 script_raw → 通读 + 初稿分集 ───
        if script_attachment and not series.get("script_raw"):
            try:
                full_text = await self._read_upload_text(
                    script_attachment.get("url"),
                    script_attachment.get("name", ""),
                )
            except ValueError as e:
                return {
                    "type": "text",
                    "message": f"剧本解析失败：{e}",
                    "project_id": self.project_id,
                }
            if not full_text.strip():
                return {
                    "type": "text",
                    "message": "上传的剧本是空的，请检查文件内容后再传",
                    "project_id": self.project_id,
                }

            await self._notify("agent_thinking", {
                "message": f"📖 我在通读剧本（{len(full_text)} 字符），稍等..."
            })
            summary = await self.director.parse_script_file(
                full_text, script_attachment.get("name", "")
            )
            # 0.7.1：用户说的横屏/竖屏覆盖脚本解析的默认建议（首次上传时 script_summary 刚生成）
            if forced_ar:
                summary["suggested_aspect_ratio"] = forced_ar

            # 0.7.1：通读完**不立刻分集**，先让导演问创作者「每集时长 + 横竖屏」，收集后(collect_prefs 分支)再 breakdown
            await self._save_series_fields(series_id, {
                "script_raw": full_text,
                "script_summary": summary,
                "status": "collect_prefs",
            })
            ask = self._build_prefs_ask(summary)
            history.append({"role": "agent", "content": ask, "time": datetime.utcnow().isoformat()})
            self.state.update_project(self.project_id, {"chat_history": history})
            return {
                "type": "text",
                "message": ask,
                "script_summary": summary,
                "series_id": series_id,
                "project_id": self.project_id,
            }

        # ─── 1.5 status=collect_prefs：通读完、已问每集时长+横竖屏 → 收集后再起分集草案 ───
        if status == "collect_prefs" and series.get("script_summary"):
            ss = dict(series.get("script_summary") or {})
            ep_min = self._extract_episode_minutes(message)
            if ep_min:
                ss["target_episode_min"] = ep_min          # 创作者期望每集时长（分集/编剧的参考，非硬指标）
            if forced_ar:
                ss["suggested_aspect_ratio"] = forced_ar
            await self._save_series_fields(series_id, {"script_summary": ss, "status": "breakdown"})
            series["script_summary"] = ss

            await self._notify("agent_thinking", {"message": "✂️ 按你的要求起分集草案..."})
            breakdown_resp = await self.director.breakdown_into_episodes(
                script_summary=ss,
                user_message=message or "请按剧情和我刚说的偏好初步分集",
                history=history_strs,
                current_breakdown=None,
            )
            episodes = breakdown_resp.get("episodes") or []
            await self._save_series_fields(series_id, {"episodes_breakdown": episodes})

            ar_label = {"9:16": "竖屏", "16:9": "横屏", "1:1": "方形"}.get(ss.get("suggested_aspect_ratio"), "")
            pref_bits = []
            if ss.get("target_episode_min"):
                pref_bits.append(f"每集≈{ss['target_episode_min']}分钟")
            if ar_label:
                pref_bits.append(ar_label)
            pref_line = f"（按你说的{('、'.join(pref_bits)) or '剧情节奏'}来）" if pref_bits else ""
            reply = (breakdown_resp.get("reply") or f"初步切了 {len(episodes)} 集，看草案。") + pref_line
            history.append({"role": "agent", "content": reply, "time": datetime.utcnow().isoformat()})
            self.state.update_project(self.project_id, {"chat_history": history})
            return {
                "type": "episode_breakdown_draft",
                "message": reply,
                "script_summary": ss,
                "episodes": episodes,
                "ready_to_confirm": bool(breakdown_resp.get("ready_to_confirm")),
                "missing_aspects": breakdown_resp.get("missing_aspects") or [],
                "series_id": series_id,
                "project_id": self.project_id,
            }

        # ─── 2. status=breakdown：已 parse 但还没 confirm 分集 → 多轮微调 ───
        # 上面 branch 0 已经把 visual_dev / producing / done 的情况切出去了
        if status == "breakdown" and series.get("script_summary"):
            # 已有草案才考虑"确认意图"
            if series.get("episodes_breakdown"):
                try:
                    if await self.director.detect_confirm_intent(message):
                        return await self.confirm_expert_breakdown(series_id)
                except Exception as e:
                    logger.warning("expert detect_confirm_intent 失败：%s", e)

            try:
                resp = await self.director.breakdown_into_episodes(
                    script_summary=series.get("script_summary") or {},
                    user_message=message,
                    history=history_strs,
                    current_breakdown=series.get("episodes_breakdown") or None,
                )
            except Exception as e:
                logger.exception("expert breakdown 失败")
                return {
                    "type": "text",
                    "message": f"协商出了点小问题，再说一遍：{str(e)[:120]}",
                    "project_id": self.project_id,
                }

            episodes = resp.get("episodes") or []
            await self._save_series_fields(series_id, {"episodes_breakdown": episodes})
            reply = resp.get("reply") or ""
            history.append({"role": "agent", "content": reply, "time": datetime.utcnow().isoformat()})
            self.state.update_project(self.project_id, {"chat_history": history})

            return {
                "type": "episode_breakdown_draft",
                "message": reply,
                "script_summary": series.get("script_summary") or {},
                "episodes": episodes,
                "ready_to_confirm": bool(resp.get("ready_to_confirm")),
                "missing_aspects": resp.get("missing_aspects") or [],
                "series_id": series_id,
                "project_id": self.project_id,
            }

        # ─── 4. 还没上传剧本（status=draft 且没 script_raw）→ 友好引导 ───
        if not series.get("script_raw") and not series.get("script_summary"):
            return {
                "type": "text",
                "message": "请先上传剧本 / 小说文件（点 + 选 .txt / .md / .docx），我通读后帮你分集。",
                "project_id": self.project_id,
            }

        # ─── 兜底：奇怪状态（理论不该到这）→ 走 creator_chat 让用户至少能聊 ───
        logger.warning("expert_chat 兜底分支触发：status=%s, has_summary=%s, has_episodes=%s",
                       status, bool(series.get("script_summary")), bool(series.get("episodes_breakdown")))
        return await self.handle_creator_chat(message, history, context, attachments)

    async def confirm_expert_breakdown(self, series_id: str) -> Dict[str, Any]:
        """专家模式：分集 confirm → master canvas 创建 episode_breakdown 节点（COMPLETED）
        + art_design 节点（IDLE → 立即异步执行）→ series.status = visual_dev。
        """
        series = await self._load_series(series_id)
        if not series:
            return {"error": "剧集数据缺失"}
        if not series.get("episodes_breakdown"):
            return {"error": "尚无可确认的分集草案"}

        # 推进 series 状态
        await self._save_series_fields(series_id, {"status": "visual_dev"})

        # 推进项目级 phase（让 advanced_confirmed_at 标记，后续聊天就走 creator_chat 而非 expert_chat）
        self.state.set_project_phase(self.project_id, ProjectPhase.PLAN_CONFIRMED)
        self.state.update_project(self.project_id, {
            "advanced_confirmed_at": datetime.utcnow().isoformat(),
            # 把 script_summary 灌进 collected_info，让下游 agent 也能拿到 tone / aspect_ratio
            "collected_info": {
                **(self.state.get_project(self.project_id) or {}).get("collected_info", {}),
                "aspect_ratio": (series.get("script_summary") or {}).get("suggested_aspect_ratio") or "9:16",
                "mood": (series.get("script_summary") or {}).get("tone") or "",
            },
        })

        episodes = series.get("episodes_breakdown") or []
        n = len(episodes)
        script_summary = series.get("script_summary") or {}

        # ─── 1. episode_breakdown 展示节点：已存在就更新（幂等，避免重新分集时建重复节点）───
        ep_result = {
            "episodes": episodes,
            "logline": script_summary.get("logline") or "",
            "genre": script_summary.get("genre") or "",
            "tone": script_summary.get("tone") or "",
            "total_min": sum(int(e.get("est_duration_min") or 0) for e in episodes),
        }
        existing_ep_node = next(
            (nd for nd in self.state.get_all_nodes(self.project_id)
             if nd.get("type") == "episode_breakdown"),
            None,
        )
        if existing_ep_node:
            ep_node = existing_ep_node
            self.state.update_node(self.project_id, ep_node["id"], {
                "label": f"分集（{n} 集）",
                "params": {"script_summary": script_summary, "series_id": series_id},
                "result": ep_result,
                "outputs": {"episodes": episodes},
                "confirmed": True,
            })
            self.state.set_node_status(self.project_id, ep_node["id"], NodeStatus.CONFIRMED)
            await self._notify("node_updated", {
                "node": self.state.get_node(self.project_id, ep_node["id"]),
            })
        else:
            ep_node = self.state.create_node(self.project_id, {
                "node_id": "node_episode_breakdown",
                "type": "episode_breakdown",
                "label": f"分集（{n} 集）",
                "position": {"x": 100, "y": 200},
                "params": {"script_summary": script_summary, "series_id": series_id},
                "depends_on": [],
                "agent": "director",
                "need_confirm": False,
                "phase": "expert_breakdown",
            })
            self.state.update_node(self.project_id, ep_node["id"], {
                "result": ep_result,
                "outputs": {"episodes": episodes},
                "confirmed": True,
            })
            self.state.set_node_status(self.project_id, ep_node["id"], NodeStatus.CONFIRMED)
            await self._notify("new_node", {
                "message": f"✅ 分集已确认（共 {n} 集），开始视觉开发...",
                "node": self.state.get_node(self.project_id, ep_node["id"]),
            })

        # 已有 art_design 节点 → 说明是"重新分集"，美术不重做，提示用户后续用「重做 N 集」刷新子集
        existing_art = next(
            (nd for nd in self.state.get_all_nodes(self.project_id)
             if nd.get("type") == "art_design"),
            None,
        )
        if existing_art:
            msg = (f"✅ 分集已更新（共 {n} 集）。总美术不变；如要让各集吃新分集，"
                   f"回总项目点「🔄 重做 N 集」重新 fan-out。")
            await self._notify("episode_breakdown_confirmed", {
                "series_id": series_id, "episode_count": n, "message": msg,
            })
            return {
                "type": "episode_breakdown_confirmed",
                "message": msg,
                "series_id": series_id,
                "episode_count": n,
                "episodes": episodes,
                "project_id": self.project_id,
            }

        # ─── 2. 构造合成 "script" 对象供 art_director 使用 ───
        synth_script = {
            "title": series.get("name") or "",
            "logline": script_summary.get("logline") or "",
            "tone": script_summary.get("tone") or "",
            "genre": script_summary.get("genre") or "",
            "themes": script_summary.get("themes") or [],
            "characters": [
                {
                    "name": c.get("name") or "",
                    "role": c.get("role") or "",
                    # 把 personality / arc 拼成 description，让 art_director 有素材出图
                    "description": " · ".join(filter(None, [
                        c.get("personality") or "",
                        c.get("arc") or "",
                    ])) or (c.get("role") or ""),
                }
                for c in (script_summary.get("characters") or [])
            ],
            "scenes": [
                {
                    "scene_id": f"S{i + 1:02d}",
                    "location": (s.get("chapter") or s.get("summary") or "")[:80],
                    "key_action": (s.get("summary") or "")[:200],
                    "key_events": s.get("key_events") or [],
                }
                for i, s in enumerate(script_summary.get("scene_outline") or [])
            ],
            "_from_expert": True,  # 标记：让下游 agent / review 知道这是 expert 模式合成的 script
        }

        # ─── 3. 创建 art_design 节点（depends_on episode_breakdown）+ 异步执行 ───
        art_node = self._create_phase_node(
            phase="art",
            node_template_id="node_art",
            depends_on=[ep_node["id"]],
            position={"x": 400, "y": 200},
            params={
                "script": synth_script,
                "style": script_summary.get("tone") or "",  # tone 同时当 style 兜底
                "mood": script_summary.get("tone") or "",
                "characters": synth_script["characters"],
                "scenes": synth_script["scenes"],
                "props": [],  # art_director 可基于剧本自己识别关键道具
                "aspect_ratio": script_summary.get("suggested_aspect_ratio") or "9:16",
            },
        )

        await self._notify("new_node", {
            "message": "🎨 美术导演开始设计角色/场景/道具...",
            "node": art_node,
        })

        # 异步执行 art_design — 不阻塞 chat 响应
        asyncio.create_task(self._execute_node(art_node["id"]))

        # 0.7.2 ②：BGM 风格方向节点（与 art 并列自动跑）—— 配乐总监通读整剧定基调，
        # 落 series.script_summary["bgm_direction"]，约束后续每集的场景配乐
        bgm_dir_node = self._create_phase_node(
            phase="bgm_direction",
            node_template_id="node_bgm_direction",
            depends_on=[ep_node["id"]],
            position={"x": 400, "y": 620},
            params={
                "series_id": series_id,
                "script_summary": script_summary,
            },
            label="BGM 风格",
        )
        await self._notify("new_node", {
            "message": "🎵 配乐总监开始通读整剧、规划 BGM 风格基调...",
            "node": bgm_dir_node,
        })
        asyncio.create_task(self._execute_node(bgm_dir_node["id"]))

        msg = f"✅ 分集已确认（共 {n} 集），美术开始设计角色/场景/道具..."

        await self._notify("episode_breakdown_confirmed", {
            "series_id": series_id,
            "episode_count": n,
            "message": msg,
        })

        return {
            "type": "episode_breakdown_confirmed",
            "message": msg,
            "series_id": series_id,
            "episode_count": n,
            "episodes": series.get("episodes_breakdown") or [],
            "project_id": self.project_id,
        }

    # ─── Series DB helpers ───────────────────────────────

    async def _get_series_id_for_project(self) -> Optional[str]:
        """根据 self.project_id 反查 series_id（projects.series_id 列）。"""
        from sqlalchemy import select
        from app.core.database import async_session
        from app.models.project import Project as ProjectModel
        try:
            async with async_session() as db:
                row = await db.execute(
                    select(ProjectModel.series_id).where(ProjectModel.id == self.project_id)
                )
                return row.scalar_one_or_none()
        except Exception as e:
            logger.warning("_get_series_id_for_project 失败：%s", e)
            return None

    async def _load_series(self, series_id: str) -> Optional[Dict[str, Any]]:
        """DB 读出 Series 行 → 普通 dict（避免到处带 ORM 对象）。"""
        from sqlalchemy import select
        from app.core.database import async_session
        from app.models.series import Series
        try:
            async with async_session() as db:
                row = await db.execute(select(Series).where(Series.id == series_id))
                s = row.scalar_one_or_none()
                if not s:
                    return None
                return {
                    "id": s.id,
                    "name": s.name,
                    "description": s.description,
                    "status": s.status,
                    "master_project_id": s.master_project_id,
                    "script_raw": s.script_raw or "",
                    "script_summary": s.script_summary or {},
                    "episodes_breakdown": s.episodes_breakdown or [],
                    "shared_assets": s.shared_assets or {},
                }
        except Exception as e:
            logger.warning("_load_series(%s) 失败：%s", series_id, e)
            return None

    async def _save_series_fields(self, series_id: str, fields: Dict[str, Any]) -> bool:
        """部分 UPDATE Series 行。"""
        from sqlalchemy import select
        from app.core.database import async_session
        from app.models.series import Series
        try:
            async with async_session() as db:
                row = await db.execute(select(Series).where(Series.id == series_id))
                s = row.scalar_one_or_none()
                if not s:
                    return False
                for k, v in fields.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
                await db.commit()
                return True
        except Exception as e:
            logger.warning("_save_series_fields(%s, keys=%s) 失败：%s",
                           series_id, list(fields.keys()), e)
            return False

    async def _read_upload_text(self, url: str, filename: str = "") -> str:
        """把 /output/uploads/... URL 映射回本地文件，重新 parse 出全文。

        uploads API 只在响应里塞了 parsed_text 前 2000 字，全文要走这里。
        """
        from app.core.paths import resolve_output_path
        from app.core.script_parser import parse_script_bytes
        if not url:
            return ""
        local = resolve_output_path(url)
        try:
            with open(local, "rb") as f:
                data = f.read()
        except Exception as e:
            logger.warning("_read_upload_text 读取 %s 失败：%s", local, e)
            return ""
        return parse_script_bytes(data, filename or os.path.basename(local))

    # ═══════════════════════════════════════════════════════
    # 进阶模式 (advanced)：plan_confirmed 之后的对话式协作
    # ═══════════════════════════════════════════════════════

    _AGENT_ALIASES = {
        "导演": "director", "director": "director",
        "美术": "art_director", "美术指导": "art_director", "art": "art_director",
        "art_director": "art_director", "artdirector": "art_director",
        "编剧": "screenwriter", "writer": "screenwriter", "screenwriter": "screenwriter",
        "分镜": "storyboard", "分镜师": "storyboard", "storyboard": "storyboard",
        "发布": "publisher", "publisher": "publisher",
    }

    def _parse_at_prefix(self, message: str):
        """检测 @<agent> 前缀。命中返回 (cleaned_message, target_agent)，否则 None。"""
        import re as _re
        m = _re.match(r"^@(\S+)\s+(.+)$", message.strip())
        if not m:
            return None
        raw = m.group(1).lower()
        target = self._AGENT_ALIASES.get(raw)
        if not target:
            return None
        return (m.group(2).strip(), target)

    def _get_agent_by_name(self, name: str):
        return {
            "director": self.director,
            "screenwriter": self.screenwriter,
            "art_director": self.art_director,
            "storyboard": self.storyboard,
            "publisher": self.publisher,
        }.get(name)

    def _build_project_summary(self) -> Dict[str, Any]:
        """给 RouterAgent 看的项目结构最小集。"""
        project = self.state.get_project(self.project_id) or {}
        all_nodes = self.state.get_all_nodes(self.project_id) or []
        outline = project.get("advanced_outline") or {}
        nodes_brief: List[Dict[str, Any]] = []
        for n in all_nodes:
            ntype = n.get("type")
            brief = {
                "id": n.get("id"),
                "type": ntype,
                "label": n.get("label"),
                "status": n.get("status"),
            }
            # per-shot 节点补 shot_id，让 router 能按 "shotNN" 精确匹配（label 里也有，但 shot_id 更稳）
            if ntype in ("keyframe_generation", "video_generation"):
                shot = (n.get("params") or {}).get("shot")
                if isinstance(shot, dict) and shot.get("shot_id"):
                    brief["shot_id"] = shot.get("shot_id")
            nodes_brief.append(brief)
        return {
            "phase": project.get("phase"),
            "collected_info": project.get("collected_info") or {},
            "advanced_outline_title": outline.get("title") or "",
            "nodes": nodes_brief,
        }

    def _resolve_shot_refs(self, message: str):
        """兜底解析消息里的 shot 引用 → 命中的 keyframe/video 节点 id（router 看不到被截断的
        per-shot 节点时的保险）。返回 (node_ids, 建议 target_agent)。

        - 抽 shot 号：shot08 / SHOT_8 / 镜头8 / 分镜8 / 第8个镜头
        - 按"关键帧/视频"关键词过滤节点类型；没点明就两类都给，交给 agent 自己挑
        - per-shot 节点统一归 storyboard（它的 chat 同时管 keyframe + video 节点）
        """
        if not message:
            return [], None
        import re
        nums = set()
        for m in re.finditer(r"shot\s*[_#\-]?\s*0*(\d{1,3})", message, re.IGNORECASE):
            nums.add(int(m.group(1)))
        for m in re.finditer(r"(?:镜头|分镜)\s*0*(\d{1,3})", message):
            nums.add(int(m.group(1)))
        for m in re.finditer(r"第\s*0*(\d{1,3})\s*(?:个|号)?\s*(?:镜头|分镜|镜)", message):
            nums.add(int(m.group(1)))
        if not nums:
            return [], None

        want_kf = any(k in message for k in ("关键帧", "keyframe", "首帧", "静帧"))
        want_vid = any(k in message for k in ("视频", "片段", "运镜", "video", "动起来"))
        types = set()
        if want_kf:
            types.add("keyframe_generation")
        if want_vid:
            types.add("video_generation")
        if not types:
            types = {"keyframe_generation", "video_generation"}

        out: List[str] = []
        for n in self.state.get_all_nodes(self.project_id) or []:
            ntype = n.get("type")
            if ntype not in types:
                continue
            shot = (n.get("params") or {}).get("shot")
            sid = shot.get("shot_id") if isinstance(shot, dict) else None
            text = str(sid or n.get("label") or "")
            mm = re.search(r"(\d{1,3})", text)
            if mm and int(mm.group(1)) in nums:
                out.append(n.get("id"))
        return out, ("storyboard" if out else None)

    def _trim_node_for_chat(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """裁剪节点字段，只保留 agent.chat() 需要看到的部分，控制 token。"""
        params = node.get("params") or {}
        result = node.get("result") or {}
        ntype = node.get("type") or ""
        trimmed_params: Dict[str, Any] = {}
        trimmed_result: Dict[str, Any] = {}

        if ntype == "result":
            # 角色 / 场景子节点
            for k in ("prompt", "name", "character_id", "location", "scene_id",
                      "subject_type", "negative_prompt", "appearance", "clothing", "environment"):
                if k in params:
                    trimmed_params[k] = params[k]
            for k in ("prompt", "image_url", "result_type", "name", "location",
                      "fortified_prompt", "three_view_image_url"):
                if k in result:
                    trimmed_result[k] = result[k]
        elif ntype == "video_generation":
            for k in ("prompt", "duration", "first_frame_url", "aspect_ratio"):
                if k in params:
                    trimmed_params[k] = params[k]
            shot = params.get("shot")
            if isinstance(shot, dict):
                trimmed_params["shot"] = {
                    sk: shot.get(sk) for sk in
                    ("shot_id", "scene_id", "visual_prompt", "video_prompt",
                     "duration_seconds", "camera_movement", "shot_size", "action")
                    if sk in shot
                }
            for k in ("video_url", "mock"):
                if k in result:
                    trimmed_result[k] = result[k]
        elif ntype == "keyframe_generation":
            for k in ("prompt", "width", "height", "aspect_ratio"):
                if k in params:
                    trimmed_params[k] = params[k]
            shot = params.get("shot")
            if isinstance(shot, dict):
                trimmed_params["shot_id"] = shot.get("shot_id")
            if "image_url" in result:
                trimmed_result["image_url"] = result["image_url"]
        elif ntype == "publish":
            for k in ("title", "description", "tags", "selected_cover", "platform"):
                if k in params:
                    trimmed_params[k] = params[k]
            for k in ("title_suggested", "description_suggested", "tags_suggested",
                      "cover_recommendations", "cover_prompt_en"):
                if k in result:
                    trimmed_result[k] = result[k]
        else:
            # 通用：取 params 全部 + result 摘要（前 500 字符）
            trimmed_params = {k: v for k, v in params.items() if not k.startswith("_")}
            import json as _json
            r_str = _json.dumps(result, ensure_ascii=False)[:500]
            trimmed_result = {"_preview": r_str}

        return {
            "id": node.get("id"),
            "type": ntype,
            "label": node.get("label"),
            "status": node.get("status"),
            "params": trimmed_params,
            "result": trimmed_result,
        }

    def _collect_target_nodes(self, target_node_ids: List[str]) -> List[Dict[str, Any]]:
        out = []
        for nid in target_node_ids:
            node = self.state.get_node(self.project_id, nid)
            if node:
                out.append(self._trim_node_for_chat(node))
        return out

    async def _apply_node_updates(self, updates: List[Dict[str, Any]]) -> List[str]:
        """应用 agent.chat() 返回的 node_updates。返回触发重跑的节点 id 列表。

        result 类型节点（character/scene 子节点）的 reference_image_urls 需要
        留在 result 里给 _retry_result_child 读，所以这类节点的 patch 同时 merge 到 result。
        """
        rerun_ids: List[str] = []
        for u in updates or []:
            nid = u.get("node_id")
            if not nid:
                continue
            node = self.state.get_node(self.project_id, nid)
            if not node:
                logger.warning("apply_node_updates: 节点 %s 不存在", nid)
                continue
            patch = u.get("params_patch") or {}
            ntype = node.get("type") or ""
            if patch:
                new_params = {**(node.get("params") or {}), **patch}
                self.state.update_node(self.project_id, nid, {"params": new_params})
                # result 子节点：把同样的 patch merge 进 result（_retry_result_child 是从 result 拿 prompt）
                if ntype == "result":
                    new_result = {**(node.get("result") or {}), **patch}
                    self.state.update_node(self.project_id, nid, {"result": new_result})
            if u.get("should_rerun"):
                # 0.7.0：episode_breakdown / cost_summary 等无 runner 的展示节点不能重跑
                # （否则 _execute_node 找不到 runner，节点会卡在 IDLE/等待中）
                if ntype in ("episode_breakdown",):
                    logger.info("apply_node_updates: 跳过 %s 节点 %s 重跑（展示节点无 runner）", ntype, nid)
                    continue
                if ntype == "result":
                    # 不能调 _execute_node（会被拒）。0.7.1：按签名分流 —— 分集子节点(ep_*，含被旧重跑改坏
                    # result_type 的)走 _retry_episode_child（用 master 参考 img2img + 重建 composite），否则 _retry_result_child。
                    # 注意：两者各自管理状态机。
                    self.state.set_node_status(self.project_id, nid, NodeStatus.IDLE)
                    if self._resolve_ep_kind(node.get("result") or {}):
                        asyncio.create_task(self._retry_episode_child(nid))
                    else:
                        asyncio.create_task(self._retry_result_child(nid))
                else:
                    self.state.update_node(self.project_id, nid, {
                        "result": None, "outputs": {}, "error": None, "confirmed": False,
                    })
                    self.state.set_node_status(self.project_id, nid, NodeStatus.IDLE)
                    asyncio.create_task(self._execute_node(nid))
                rerun_ids.append(nid)
        return rerun_ids

    async def handle_creator_chat(
        self,
        message: str,
        history: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """进阶模式 plan_confirmed 后的对话入口：router → agent.chat → apply node_updates。"""
        # 0.6.3+: 跟 handle_advanced_chat 同步 — 每条消息抓 aspect_ratio/platform/mood 进 collected_info
        # 用户可能在剧本/分镜阶段才补充"画面要横屏"这类指令，必须实时更新让后续 fanout 用到
        try:
            project = self.state.get_project(self.project_id) or {}
            collected_info = dict(project.get("collected_info") or {})
            history_strs = [h.get("content", "") for h in history if isinstance(h, dict)]
            detected = self.director._extract_collected_info(history_strs + [message], context or {})
            changed = False
            for k in ("aspect_ratio", "platform", "duration", "style", "mood"):
                v = detected.get(k)
                if v and collected_info.get(k) != v:
                    collected_info[k] = v
                    changed = True
            if changed:
                self.state.update_project(self.project_id, {"collected_info": collected_info})
                logger.info(
                    "creator_chat 更新 collected_info: %s",
                    {k: collected_info.get(k) for k in ("aspect_ratio", "platform", "mood")},
                )
        except Exception as e:
            logger.warning("creator_chat extract collected_info 失败: %s", e)

        # 1. @<agent> 前缀直连
        parsed = self._parse_at_prefix(message)
        if parsed:
            clean_message, target_agent = parsed
            target_node_ids: List[str] = []
            summary = clean_message[:150]
        else:
            project_summary = self._build_project_summary()
            try:
                routed = await self.router.route(
                    message=message,
                    project_summary=project_summary,
                    mode="advanced",
                    current_phase=str(project_summary.get("phase") or ""),
                )
            except Exception as e:
                logger.warning("router.route 失败，降级 director: %s", e)
                routed = {"target_agent": "director", "target_node_ids": [], "summary": message[:150]}
            clean_message = message
            target_agent = routed.get("target_agent") or "director"
            target_node_ids = routed.get("target_node_ids") or []
            summary = routed.get("summary") or message[:150]

        # 0.7.1 修复：router 的 project_summary 会被截断，靠后的 per-shot 节点（48 镜 episode 有上百个
        # keyframe/video 节点）丢失 → "重做 shotNN 关键帧/视频" 解析不到 node → 报"目标节点列表为空"。
        # 按 shot 号在代码里兜底解析并入 target_node_ids，保证命中（storyboard.chat 同管 keyframe+video）。
        det_ids, det_agent = self._resolve_shot_refs(message)
        if det_ids:
            target_node_ids = list(dict.fromkeys([*det_ids, *target_node_ids]))
            # shot 关键帧/视频节点只有 storyboard 的 chat 能改 → 纠正路由（除非用户用 @ 显式指定了别的 agent）
            if det_agent and not parsed:
                target_agent = det_agent
            logger.info(
                "shot 引用兜底解析命中 %d 个节点 → agent=%s ids=%s", len(det_ids), target_agent, det_ids
            )

        # 2. 收集裁剪后的目标节点 + 项目元数据
        target_nodes = self._collect_target_nodes(target_node_ids)
        project = self.state.get_project(self.project_id) or {}
        outline = project.get("advanced_outline") or {}
        project_meta = {
            "phase": project.get("phase"),
            "collected_info": project.get("collected_info") or {},
            "outline_title": outline.get("title") or "",
            "outline_logline": outline.get("logline") or "",
            "outline_tone": outline.get("tone") or "",
        }

        agent = self._get_agent_by_name(target_agent)
        if agent is None:
            return {
                "type": "text",
                "message": f"路由到未知 agent: {target_agent}",
                "project_id": self.project_id,
            }

        # 3. 调对应 agent.chat()
        try:
            chat_result = await agent.chat(
                message=clean_message,
                summary=summary,
                project_meta=project_meta,
                target_nodes=target_nodes,
                attachments=attachments,
            )
        except Exception as e:
            logger.exception("agent.%s.chat 异常", target_agent)
            return {
                "type": "text",
                "message": f"{target_agent} 处理出错：{str(e)[:120]}",
                "project_id": self.project_id,
            }

        reply = chat_result.get("reply") or ""
        node_updates = chat_result.get("node_updates") or []

        # 4. 应用 patch + 触发重跑
        rerun_ids = await self._apply_node_updates(node_updates)

        # 5. 写入 chat_history
        history.append({
            "role": "agent",
            "content": reply,
            "agent": target_agent,
            "time": datetime.utcnow().isoformat(),
        })
        self.state.update_project(self.project_id, {"chat_history": history})

        # 把 patch 摘要也带回去给前端展示（创作者能看到 LLM 实际写了什么 prompt，方便 debug）
        def _patch_summary(u):
            patch = u.get("params_patch") or {}
            return {
                "node_id": u.get("node_id"),
                "reason": u.get("reason", ""),
                "should_rerun": u.get("should_rerun", False),
                # prompt 是最容易出问题的字段，单独提取；其余保留
                "prompt": patch.get("prompt"),
                "reference_image_urls": patch.get("reference_image_urls"),
                "other_fields": {
                    k: (str(v)[:200] if not isinstance(v, (str, int, float, bool, list, dict)) else v)
                    for k, v in patch.items()
                    if k not in ("prompt", "reference_image_urls")
                },
            }

        applied_updates = [_patch_summary(u) for u in node_updates]

        await self._notify("creator_chat", {
            "message": reply,
            "target_agent": target_agent,
            "updated_nodes": [u.get("node_id") for u in node_updates if u.get("node_id")],
            "rerun_nodes": rerun_ids,
            "applied_updates": applied_updates,
        })

        return {
            "type": "creator_chat",
            "message": reply,
            "target_agent": target_agent,
            "updated_nodes": [u.get("node_id") for u in node_updates if u.get("node_id")],
            "rerun_nodes": rerun_ids,
            "applied_updates": applied_updates,
            "project_id": self.project_id,
        }

    # ═══════════════════════════════════════════════════════
    # 入口 3：确认节点 → 推进下一阶段
    # ═══════════════════════════════════════════════════════

    async def confirm_node(self, node_id: str) -> Dict[str, Any]:
        node = self.state.get_node(self.project_id, node_id)
        if not node:
            return {"error": "节点不存在"}

        # 标记为已确认
        if node.get("status") != NodeStatus.CONFIRMED:
            try:
                self.state.confirm_node(self.project_id, node_id)
            except ValueError as e:
                # 节点尚未完成，无法确认
                return {"error": str(e)}

        current_phase = resolve_phase(node)
        spec = self.phases.get(current_phase)
        logger.info(
            "[confirm_node] node_id=%s, type=%s, phase=%s -> current_phase=%s",
            node_id, node.get("type"), node.get("phase"), current_phase,
        )
        if spec:
            self.state.set_project_phase(self.project_id, spec.project_phase)

        # art 阶段：
        # - 主节点 art_design 确认 → 触发子节点生成（首次）或检查全确认（已生成时）
        # - 子节点（result 类型）确认 → 检查 siblings 是否全部 CONFIRMED → 弹"开始分镜"对话
        if current_phase == "art":
            if node.get("type") == "art_design":
                return await self._after_art_main_confirm(node_id)
            if node.get("type") == "result":
                return await self._after_art_child_confirm(node_id)

        # 0.7.0 轮 1：Episode art 阶段
        # - episode_art_design 主节点 confirm → 首次 spawn 子节点；之后再 confirm 检查全确认
        # - ep_character / ep_scene / ep_prop 子节点 confirm → 检查 siblings 全 CONFIRMED → 启动 storyboard
        if current_phase == "episode_art":
            if node.get("type") == "episode_art_design":
                return await self._after_episode_art_main_confirm(node_id)
            if node.get("type") == "result":
                return await self._after_episode_art_child_confirm(node_id)

        # 0.6.1: audio_plan / bgm_fetch 节点 — 与主线解耦，确认后只通知不推进
        # （bgm 拉好后，merge 阶段会自动从 latest bgm_fetch 节点拿 bgm_local_path）
        if current_phase in ("audio", "bgm"):
            return {
                "type": "node_confirmed",
                "node_id": node_id,
                "message": "已确认音频节点",
                "phase": current_phase,
            }

        # keyframe 阶段：单个关键帧确认后，创建对应的 video 节点
        if current_phase == "keyframe":
            return await self._after_keyframe_confirm(node_id)

        # video 阶段：fan-out 节点的批量确认 — 全部确认后才询问是否合并
        if current_phase == "video":
            return await self._after_video_confirm(node_id)

        # merge 阶段：跳到 publish（先生成发布元数据，等用户确认/编辑后才算项目完成）
        if current_phase == "merge":
            return await self._after_merge_confirm(node_id)

        # publish 阶段：项目正式完成 → 0.6.5 自动追加 cost_summary 节点
        if current_phase == "publish":
            await self._spawn_cost_summary_after(node_id)
            self.state.set_project_phase(self.project_id, ProjectPhase.COMPLETED)
            await self._notify("project_completed", {
                "message": "🎉 项目全部完成！发布物料已准备就绪。",
                "node_id": node_id,
            })
            return {"type": "project_completed", "message": "项目全部完成！", "node_id": node_id}

        # cost_summary 阶段：终点节点，确认后无后续
        if current_phase == "cost":
            return {"type": "node_confirmed", "node_id": node_id, "message": "成本总结已确认"}

        # 0.7.0 轮 1：Episode 项目的 script 确认 → 不走标准 advance，而是创 episode_art_design
        if current_phase == "script" and node.get("type") == "script_generation":
            ep_art_resp = await self._maybe_after_script_confirm_for_episode(node_id, node)
            if ep_art_resp is not None:
                return ep_art_resp

        # 0.7.2 ②：episode 场景 BGM 计划确认 → 按唯一 track_key spawn bgm_fetch（确认才花钱生成）
        if node.get("type") == "episode_audio_plan":
            return await self._post_episode_audio_plan(node_id, node.get("result") or {})

        # 0.7.2 ②：series BGM 风格方向确认 → 纯展示，无下游
        if node.get("type") == "bgm_direction":
            return {"type": "node_confirmed", "node_id": node_id, "message": "🎵 BGM 风格方向已确认"}

        # 其它阶段（script / art / storyboard）：调 director 决定下一步
        return await self._advance_to_next(node, node_id, current_phase)

    async def _advance_to_next(
        self,
        confirmed_node: Dict[str, Any],
        node_id: str,
        current_phase: str,
    ) -> Dict[str, Any]:
        """script/art/storyboard 确认后，推进到下一阶段"""
        project = self.state.get_project(self.project_id) or {}
        director_state = {
            "confirmed_nodes": project.get("confirmed_nodes", []),
            "collected_info": project.get("collected_info", {}),
            "current_node_id": node_id,
            "current_phase": current_phase,
        }

        node_template_id = confirmed_node.get("node_id", node_id)
        next_step = await self.director.on_node_confirmed(node_template_id, director_state)

        logger.info(
            "[advance] current_phase=%s, node_template_id=%s, director.action=%s, next_node.node_type=%s",
            current_phase, node_template_id, next_step.get("action"),
            (next_step.get("next_node") or {}).get("node_type"),
        )

        if next_step.get("action") != "create_next_node":
            # 兜底：director 找不到下一步（通常是 node_template_id 不在 NODE_TEMPLATES 里），
            # 但当前 phase 的 PhaseSpec 知道默认 next_phase → 沿用之，避免流程在中途"卡死"。
            spec_now = self.phases.get(current_phase)
            fallback_next_phase = spec_now.next_phase if spec_now else None
            fallback_spec = self.phases.get(fallback_next_phase) if fallback_next_phase else None
            if fallback_spec:
                logger.warning(
                    "[advance] director 没认出 node_template_id=%s，回退用 PhaseSpec.next_phase=%s",
                    node_template_id, fallback_next_phase,
                )
                next_template = {
                    "node_id": f"node_{fallback_next_phase}",
                    "node_name": fallback_spec.label,
                    "node_type": fallback_spec.node_type,
                    "phase": fallback_next_phase,
                }
                next_step = {
                    "action": "create_next_node",
                    "next_node": next_template,
                    "message": f"✅ 进入 {fallback_spec.label}...",
                }
            else:
                return {
                    "type": "completed" if next_step.get("action") == "complete" else "node_confirmed",
                    "message": next_step.get("message", ""),
                }

        next_template = next_step["next_node"]
        # 判断下一阶段
        next_phase = next_template.get("phase") or NODE_TYPE_TO_PHASE.get(next_template.get("node_type", ""), "")

        # 🔴 1.1.0 MV：美术之后必须走 MV 卡点分镜，不能走通用分镜。
        #    MV 复用了现成的 art 阶段（省一套美术流程），但 art.next_phase 写死是 "storyboard"，
        #    而且 director 的 NODE_TEMPLATES 认识 node_art、会主动把下一步指到 node_storyboard。
        #    两条路都会把 MV 引到通用分镜——那个 agent 自己决定时长，实测把一首 3 分 28 秒的歌
        #    规划成 60 秒 6 个镜头，画面和歌完全对不上。
        #    这里是阶段推进的唯一出口，在这儿改道最省事，也不会漏掉某条分支。
        if next_phase == "storyboard" and self._is_mv_project():
            logger.info("[advance] MV 项目：把 storyboard 改道到 mv_storyboard")
            next_phase = "mv_storyboard"
            _spec = self.phases[next_phase]
            next_template = {
                "node_id": "node_mv_storyboard",
                "node_name": _spec.label,
                "node_type": _spec.node_type,
                "phase": next_phase,
            }

        next_spec = self.phases.get(next_phase)
        logger.info("[advance] next_phase=%s, has_spec=%s", next_phase, next_spec is not None)
        if not next_spec:
            return {"type": "error", "message": f"未知的下一阶段: {next_phase}"}

        # storyboard → keyframe 是 fan-out（每个 requires_keyframe=True 的分镜一个关键帧节点；
        # 不需要关键帧的分镜直接 fan-out 为 video 节点）
        if current_phase in ("storyboard", "mv_storyboard") and next_phase in ("keyframe", "video"):
            logger.info("[advance] → 进入 _fanout_storyboard")
            return await self._fanout_storyboard(confirmed_node, next_step)

        # script → art：节点输入需要剧本结果
        # art → storyboard：节点 depends_on 包含所有图片子节点
        new_node = self._build_next_node(confirmed_node, next_template, next_phase, next_spec)

        await self._notify("new_node", {"message": next_step.get("message", ""), "node": new_node})
        asyncio.create_task(self._execute_node(new_node["id"]))

        return {
            "type": "new_node",
            "message": next_step.get("message", ""),
            "node": new_node,
            "phase": next_phase,
        }

    def _collect_art_refs(self, art_node: Dict[str, Any]):
        """从 art 节点的子节点收集 character_refs/scene_refs/prop_refs（含 0.7.1 服装聚合）。
        返回 (character_refs, scene_refs, prop_refs, all_deps)；all_deps[0]=art 节点本身。
        创建分镜节点(_build_next_node) 和 分镜重跑(_run_storyboard) 共用 → 重跑能拿到最新美术 + 聚合服装。"""
        art_node_id = art_node.get("id")
        art_children = art_node.get("children", []) or []
        character_refs, scene_refs, prop_refs, all_deps = [], [], [], [art_node_id]
        # 0.7.1 ①：同一 master 角色的多套服装(ep_character)聚合成一条 ref（含 outfits[]），按首见顺序
        ep_char_groups: Dict[str, Dict[str, Any]] = {}
        # 0.7.2：同一 master 道具的多个状态(ep_prop)聚合成一条 ref（含 states[]），对称 outfits
        ep_prop_groups: Dict[str, Dict[str, Any]] = {}
        for child_id in art_children:
            child = self.state.get_node(self.project_id, child_id)
            if not child:
                continue
            child_result = child.get("result") or {}
            rtype = child_result.get("result_type")
            if rtype == "character":
                character_refs.append({
                    "node_id": child_id,
                    "character_id": child_result.get("character_id", ""),
                    "name": child_result.get("name", ""),
                    "image_url": child_result.get("image_url"),
                    # 0.7.0 轮 2：composite/三视图 透传给下游 keyframe + video
                    "composite_reference_url": child_result.get("composite_reference_url"),
                    "three_view_image_url": child_result.get("three_view_image_url"),
                    "face_close_up_url": child_result.get("face_close_up_url"),
                    "prompt": child_result.get("prompt", ""),
                    "subject_type": child_result.get("subject_type", "human"),
                    "character_asset_id": child_result.get("character_asset_id"),  # 0.7.3 asset 锁身份
                })
                all_deps.append(child_id)
            elif rtype == "scene":
                scene_refs.append({
                    "node_id": child_id,
                    "scene_id": child_result.get("scene_id", ""),
                    "location": child_result.get("location", ""),
                    "image_url": child_result.get("image_url"),
                    "prompt": child_result.get("prompt", ""),
                    # 0.7.5 机位板：建立镜 + 额外机位（@场景#机位 选板用）；无则空
                    "cameras": child_result.get("cameras") or [],
                })
                all_deps.append(child_id)
            elif rtype == "prop":
                # 0.6.4: 关键道具
                prop_refs.append({
                    "node_id": child_id,
                    "prop_id": child_result.get("prop_id", ""),
                    "name": child_result.get("name", ""),
                    "image_url": child_result.get("image_url"),
                    "prompt": child_result.get("prompt", ""),
                    "use_case": child_result.get("use_case", ""),
                })
                all_deps.append(child_id)
            # 0.7.1 ①：Episode 角色 → 按 master 角色聚合，每套服装进 outfits[]（每套仍各自一个节点）
            elif rtype == "ep_character":
                master_id = (
                    child_result.get("master_character_id")
                    or child_result.get("outfit_id")
                    or child_id
                )
                base_name = (
                    child_result.get("character_name")
                    or (child_result.get("name") or "").split(" · ")[0].strip()
                    or (child_result.get("parent_master") or {}).get("name")
                    or "角色"
                )
                outfit = {
                    "outfit_id": child_result.get("outfit_id"),
                    "outfit_name": child_result.get("outfit_name"),
                    "outfit_when": child_result.get("outfit_when"),
                    "node_id": child_id,
                    "image_url": child_result.get("image_url"),
                    # composite = [master_face | outfit_3view]，下游优先用
                    "composite_reference_url": child_result.get("composite_reference_url"),
                    "three_view_image_url": child_result.get("three_view_image_url"),
                    "face_close_up_url": (child_result.get("parent_master") or {}).get("face_close_up_url"),
                    "prompt": child_result.get("outfit_prompt") or child_result.get("fortified_prompt", ""),
                    "character_asset_id": child_result.get("character_asset_id"),  # 0.7.3 本套服装的 asset://火山id（@角色#服装 时用这套）
                }
                grp = ep_char_groups.get(master_id)
                if grp is None:
                    # 顶层字段用第一套(默认)，保证旧逻辑 / @角色(不带#) 拿到默认套
                    grp = {
                        "node_id": child_id,
                        "character_id": child_result.get("master_character_id") or child_result.get("outfit_id", ""),
                        "name": base_name,
                        "image_url": outfit["image_url"],
                        "composite_reference_url": outfit["composite_reference_url"],
                        "three_view_image_url": outfit["three_view_image_url"],
                        "face_close_up_url": outfit["face_close_up_url"],
                        "prompt": outfit["prompt"],
                        "subject_type": child_result.get("subject_type", "human"),
                        "character_asset_id": child_result.get("character_asset_id"),  # 0.7.3 asset 锁身份（master 角色级）
                        "outfits": [],
                    }
                    ep_char_groups[master_id] = grp
                grp["outfits"].append(outfit)
                all_deps.append(child_id)
            elif rtype == "ep_scene":
                scene_refs.append({
                    "node_id": child_id,
                    "scene_id": child_result.get("variation_id") or child_result.get("master_scene_id", ""),
                    "location": child_result.get("location") or child_result.get("variation_name", ""),
                    "image_url": child_result.get("image_url"),
                    "prompt": child_result.get("image_prompt") or child_result.get("fortified_prompt", ""),
                    # 0.7.5 机位板：建立镜 + 额外机位（@场景#机位 选板用）；无则空
                    "cameras": child_result.get("cameras") or [],
                    "_variation_name": child_result.get("variation_name"),
                    "_time_of_day": child_result.get("time_of_day"),
                    "_lighting": child_result.get("lighting"),
                })
                all_deps.append(child_id)
            elif rtype == "ep_prop":
                # 0.7.2：按 master 道具聚合，每个状态进 states[]（每态仍各自一个节点）
                master_pid = (
                    child_result.get("master_prop_id")
                    or child_result.get("state_id")
                    or child_id
                )
                state = {
                    "state_id": child_result.get("state_id"),
                    "state_name": child_result.get("state_name"),
                    "state_description": child_result.get("state_description") or "",
                    "node_id": child_id,
                    "image_url": child_result.get("image_url"),
                    "prompt": child_result.get("image_prompt") or child_result.get("fortified_prompt", ""),
                }
                pgrp = ep_prop_groups.get(master_pid)
                if pgrp is None:
                    # 顶层字段用第一态(默认)：@道具(不带#) 拿默认态，旧下游零影响
                    pgrp = {
                        "node_id": child_id,
                        "prop_id": child_result.get("state_id") or child_result.get("master_prop_id", ""),
                        "name": child_result.get("name", ""),
                        "image_url": state["image_url"],
                        "prompt": state["prompt"],
                        "use_case": child_result.get("state_description") or "",
                        "_state_name": child_result.get("state_name"),
                        "states": [],
                    }
                    ep_prop_groups[master_pid] = pgrp
                pgrp["states"].append(state)
                all_deps.append(child_id)

        # 0.7.1 ①：聚合后的 episode 角色 refs 落地（每角色一条，含 outfits[]）
        if ep_char_groups:
            character_refs.extend(ep_char_groups.values())
        # 0.7.2：聚合后的 episode 道具 refs 落地（每道具一条，含 states[]）
        if ep_prop_groups:
            prop_refs.extend(ep_prop_groups.values())
        return character_refs, scene_refs, prop_refs, all_deps

    def _build_next_node(
        self,
        prev_node: Dict[str, Any],
        template: Dict[str, Any],
        next_phase: str,
        next_spec: PhaseSpec,
    ) -> Dict[str, Any]:
        """根据上一节点结果，为下一阶段构造节点"""
        prev_pos = prev_node.get("position", {"x": 100, "y": 200})
        position = {"x": prev_pos["x"] + 300, "y": prev_pos["y"]}
        prev_id = prev_node["id"]
        prev_result = prev_node.get("result") or {}

        # 各阶段的输入构造
        if next_phase == "art":
            params = {
                "script": prev_result,
                "style": prev_result.get("style", "写实"),
                "characters": prev_result.get("characters", []),
                "scenes": prev_result.get("scenes", []),
                "props": prev_result.get("props", []) or [],  # 0.6.4 关键道具
            }
            return self._create_phase_node(
                phase="art",
                node_template_id=template.get("node_id", "node_art"),
                depends_on=[prev_id],
                position=position,
                params=params,
                label=template.get("node_name") or next_spec.label,
            )

        if next_phase == "storyboard":
            # 0.7.1 ①：refs 收集（含服装聚合）抽到 _collect_art_refs，与分镜重跑 _run_storyboard 共用
            character_refs, scene_refs, prop_refs, all_deps = self._collect_art_refs(prev_node)

            param_connections = (
                [{"id": f"param_{i}", "label": f"🎭 {c['name']}", "type": "character",
                  "image_url": c["image_url"]} for i, c in enumerate(character_refs)] +
                [{"id": f"param_{len(character_refs)+i}", "label": f"🏞️ {s['location']}",
                  "type": "scene", "image_url": s["image_url"]} for i, s in enumerate(scene_refs)] +
                [{"id": f"param_{len(character_refs)+len(scene_refs)+i}", "label": f"🎁 {p['name']}",
                  "type": "prop", "image_url": p["image_url"]} for i, p in enumerate(prop_refs)]
            )

            # 剧本结果（art 节点的 depends_on 第一个就是 script 节点）
            script_node_id = (prev_node.get("depends_on") or [None])[0]
            script_node = self.state.get_node(self.project_id, script_node_id) if script_node_id else None
            script_result = (script_node or {}).get("result") or {}

            sb_params = {
                "script": script_result,
                "art_design": prev_result,
                "scenes": script_result.get("scenes", []),
                "character_refs": character_refs,
                "scene_refs": scene_refs,
                "prop_refs": prop_refs,  # 0.6.4
            }
            # 0.7.0 #2: episode 用「分集计划时长」(est_duration_min×60) 作为分镜目标总时长，
            # 否则 _resolve_target_duration 会退化成累加编剧的短场景时长 → 9分钟的集只出 ~149s
            proj = self.state.get_project(self.project_id) or {}
            if (proj.get("advanced_outline") or {}).get("_from_expert_series"):
                planned_dur = (proj.get("collected_info") or {}).get("duration")
                if planned_dur:
                    sb_params["target_duration_seconds"] = planned_dur

            return self.state.create_node(self.project_id, {
                "node_id": template.get("node_id", "node_storyboard"),
                "type": next_spec.node_type,
                "label": template.get("node_name") or next_spec.label,
                "position": position,
                "params": sb_params,
                "depends_on": all_deps,
                "agent": next_spec.agent_name,
                "need_confirm": True,
                "phase": "storyboard",
                "has_children": True,
                "connection_points": {"params": param_connections},
            })

        # ── 1.1.0 MV：歌曲解析 → MV 剧本 ──
        if next_phase == "mv_script":
            return self._create_phase_node(
                phase="mv_script",
                node_template_id=template.get("node_id", "node_mv_script"),
                depends_on=[prev_id],
                position=position,
                params={
                    "song": prev_result.get("song") or {},
                    "style": prev_result.get("style") or {},
                    "mv_type": prev_result.get("mv_type") or "narrative",
                    "user_brief": prev_result.get("user_brief") or "",
                },
                label=template.get("node_name") or next_spec.label,
            )

        # ── 1.1.0 MV：美术 → MV 卡点分镜 ──
        if next_phase == "mv_storyboard":
            character_refs, scene_refs, prop_refs, all_deps = self._collect_art_refs(prev_node)
            param_connections = (
                [{"id": f"param_{i}", "label": f"🎭 {c['name']}", "type": "character",
                  "image_url": c["image_url"]} for i, c in enumerate(character_refs)] +
                [{"id": f"param_{len(character_refs)+i}", "label": f"🏞️ {sc['location']}",
                  "type": "scene", "image_url": sc["image_url"]} for i, sc in enumerate(scene_refs)]
            )
            # 回溯两跳取歌曲解析结果：art ← mv_script ← mv_audio。
            # 分镜的时间轴全靠 lyrics/duration，断了这条回溯链就只能凭空编时长。
            mv_script_node = self.state.get_node(
                self.project_id, (prev_node.get("depends_on") or [None])[0])
            script_result = (mv_script_node or {}).get("result") or {}
            mv_audio_node = self.state.get_node(
                self.project_id, ((mv_script_node or {}).get("depends_on") or [None])[0])
            mv = (mv_audio_node or {}).get("result") or {}

            return self.state.create_node(self.project_id, {
                "node_id": template.get("node_id", "node_mv_storyboard"),
                "type": next_spec.node_type,
                "label": template.get("node_name") or next_spec.label,
                "position": position,
                "params": {
                    "script": script_result,
                    "art_design": prev_result,
                    "character_refs": character_refs,
                    "scene_refs": scene_refs,
                    "prop_refs": prop_refs,
                    "song": mv.get("song") or {},
                    "style": mv.get("style") or {},
                    "lyrics": mv.get("lyrics") or [],
                    "duration": mv.get("duration") or 0,
                    "mv_type": mv.get("mv_type") or "narrative",
                },
                "depends_on": all_deps,
                "agent": next_spec.agent_name,
                "need_confirm": True,
                "phase": "mv_storyboard",
                "has_children": True,
                "connection_points": {"params": param_connections},
            })

        # 其它阶段（理论上 storyboard→video 走 fan-out，merge 走 start_merge）
        return self._create_phase_node(
            phase=next_phase,
            node_template_id=template.get("node_id", f"node_{next_phase}"),
            depends_on=[prev_id],
            position=position,
            params={"prev_result": prev_result},
            label=template.get("node_name") or next_spec.label,
        )

    # ═══════════════════════════════════════════════════════
    # 节点创建 + 执行
    # ═══════════════════════════════════════════════════════

    def _create_phase_node(
        self,
        phase: str,
        node_template_id: str,
        depends_on: List[str],
        position: Dict[str, int],
        params: Dict[str, Any],
        label: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        spec = self.phases[phase]
        payload = {
            "node_id": node_template_id,
            "type": spec.node_type,
            "label": label or spec.label,
            "position": position,
            "params": params,
            "depends_on": depends_on,
            "agent": spec.agent_name,
            "need_confirm": True,
            "phase": phase,
        }
        if extra:
            payload.update(extra)
        return self.state.create_node(self.project_id, payload)

    @asynccontextmanager
    async def _run_with_status(self, node_id: str):
        """节点状态机：RUNNING → COMPLETED；异常时 FAILED + 通知"""
        self.state.set_node_status(self.project_id, node_id, NodeStatus.RUNNING)
        try:
            yield
            self.state.set_node_status(self.project_id, node_id, NodeStatus.COMPLETED)
        except Exception as e:
            logger.exception("执行节点 %s 失败: %s", node_id, e)
            self.state.set_node_status(self.project_id, node_id, NodeStatus.FAILED, error=str(e))
            await self._notify("node_failed", {"node_id": node_id, "error": str(e)})
            raise

    async def _execute_node(self, node_id: str) -> None:
        """单节点执行入口：查 PHASES → 调 runner → 后处理 → 通知"""
        node = self.state.get_node(self.project_id, node_id)
        if not node:
            logger.warning("节点 %s 不存在", node_id)
            return

        # 兜底：result 类型节点（角色/场景子图）永远不该走 phase runner，
        # 即便它们有 phase="art" 的标签。重跑这些节点必须走 _retry_result_child。
        # 这个保护避免重跑子节点时意外触发 _run_art 产生孙节点。
        if node.get("type") == "result":
            logger.warning(
                "_execute_node 收到 result 类型节点 %s，已拒绝执行（应通过 _retry_result_child）",
                node_id,
            )
            return

        phase = resolve_phase(node)
        spec = self.phases.get(phase)
        if not spec or not spec.runner:
            logger.warning("未知阶段或缺 runner: phase=%s", phase)
            return

        try:
            self.state.set_node_status(self.project_id, node_id, NodeStatus.RUNNING)
            params = node.get("params", {})
            result_data, extra_outputs = await spec.runner(node, params)

            # 0.6.5: 捕获 usage 追加到 project.cost_log（成本统计用）
            self._capture_usage_to_cost_log(node_id, node.get("type"), phase, result_data, extra_outputs)

            if spec.post_execute:
                # 有 post_execute 时：先写 result，再跑 hook（hook 可能写子节点 / 调 review），
                # 全跑完才把状态标 COMPLETED。失败由下方 except 兜底。
                self.state.update_node(self.project_id, node_id, {
                    "result": result_data,
                    "outputs": {spec.outputs_key: result_data, **(extra_outputs or {})},
                })
                await spec.post_execute(node_id, result_data)
                self.state.set_node_status(self.project_id, node_id, NodeStatus.COMPLETED)
            else:
                # 0.6.4：无 post_execute 时把 result + COMPLETED 合并成一次 update_node 原子写入，
                # 避免"runner 完成 → 写 result → 进程崩 → 来不及写 COMPLETED → 状态卡 running"的 race。
                self.state.update_node(self.project_id, node_id, {
                    "result": result_data,
                    "outputs": {spec.outputs_key: result_data, **(extra_outputs or {})},
                    "status": NodeStatus.COMPLETED,
                })
        except Exception as e:
            logger.exception("执行节点 %s 失败: %s", node_id, e)
            self.state.set_node_status(self.project_id, node_id, NodeStatus.FAILED, error=str(e))
            await self._notify("node_failed", {"node_id": node_id, "error": str(e)})
            return

        # 通知前端
        node_after = self.state.get_node(self.project_id, node_id) or {}
        preview = spec.formatter(result_data) if spec.formatter else ""
        await self._notify("node_completed", {
            "node_id": node_id,
            "node_type": spec.node_type,
            "result": node_after.get("result"),
            "result_preview": preview,
            "need_confirm": True,
            "child_nodes": node_after.get("children", []),
        })

        # W5 即时传帧：链镜视频一出片，就把尾帧推给同链下一镜的 first_frame（下一镜生成前即可预览真尾帧）
        await self._propagate_tail_to_next_link(node_after)

    # ─── 0.6.5: 成本统计辅助 ───
    _PHASE_TO_AGENT_ATTR = {
        "script": "screenwriter",
        "art": "art_director",
        "storyboard": "storyboard",
        "audio": "audio_director",
    }

    def _agent_for_phase(self, phase: str):
        """从 phase 字符串拿对应 agent 实例，用于读 _last_usage（捕获后清空避免串项）。"""
        attr = self._PHASE_TO_AGENT_ATTR.get(phase)
        return getattr(self, attr, None) if attr else None

    def _capture_usage_to_cost_log(
        self,
        node_id: str,
        node_type: Optional[str],
        phase: str,
        result_data: Any,
        extra_outputs: Optional[Dict[str, Any]],
    ) -> None:
        """runner 跑完后捕获 usage 追加到 project.cost_log。
        三个来源（按优先级）：result_data.usage > extra_outputs.usage > agent._last_usage。
        """
        try:
            usage: Optional[Dict[str, Any]] = None
            if isinstance(result_data, dict):
                # mock 的图/视频不计费
                if result_data.get("mock"):
                    pass
                else:
                    usage = result_data.get("usage")
            if not usage and extra_outputs:
                usage = extra_outputs.get("usage")
            if not usage:
                agent = self._agent_for_phase(phase)
                last = getattr(agent, "_last_usage", None) if agent else None
                if last:
                    usage = last
                    try:
                        agent._last_usage = None  # 消费即清空，避免下次串项
                    except Exception:
                        pass

            if not usage:
                return

            project = self.state.get_project(self.project_id) or {}
            cost_log = list(project.get("cost_log") or [])
            cost_log.append({
                "timestamp": datetime.utcnow().isoformat(),
                "node_id": node_id,
                "node_type": node_type,
                "phase": phase,
                "usage": usage,
            })
            self.state.update_project(self.project_id, {"cost_log": cost_log})

            # 0.8 缝6：旁挂计量上报。local 是 no-op；cloud_client 只上报视频（credits）。
            # meter.report 自己保证不抛不阻塞，所以放在 cost_log 之后、不影响既有逻辑。
            from app.core import meter
            meter.report(
                usage, node_id=node_id, node_type=node_type or "",
                phase=phase, project_id=self.project_id,
            )
        except Exception as e:
            logger.warning("捕获 usage 失败（不影响节点执行）: %s", e)

    async def retry_node(self, node_id: str) -> Dict[str, Any]:
        node = self.state.get_node(self.project_id, node_id)
        if not node:
            return {"error": "节点不存在"}

        # result 类型子节点（角色图/场景图）没有 phase runner，单独处理：
        # 拿到节点已存的 prompt + result_type，重新跑一次 _generate_image / 三视图
        if node.get("type") == "result":
            # 0.7.1 修复：分集子节点重跑走专用路径（img2img from master + 重建 composite）。
            # 按"签名"识别（_resolve_ep_kind），兼容被旧重跑改坏 result_type(ep_character→character) 的节点。
            if self._resolve_ep_kind(node.get("result") or {}):
                asyncio.create_task(self._retry_episode_child(node_id))
            else:
                asyncio.create_task(self._retry_result_child(node_id))
            return {
                "type": "node_rerunning",
                "node_id": node_id,
                "message": f"正在重新生成 {node.get('label', '图片')}...",
            }

        # 0.7.1：重跑分镜节点 → 先**级联删掉旧的下游(关键帧/视频)**，否则：新旧 shot 不一致；
        # 确认后 _fanout_storyboard 复用旧 children（按新分镜重建失败）；残留孤儿节点的悬空依赖还可能让画布崩。
        # 清空 children，让确认后按新分镜从头 fan-out。
        if node.get("type") == "storyboard_design":
            # 下游关键帧/视频是用 depends_on 挂的、**不一定登记在 storyboard.children 里**，
            # 所以按类型找全部 keyframe/video/merge 节点删（一集只有这一个分镜的 fan-out 产物，删全部是对的）。
            all_nodes = self.state.get_all_nodes(self.project_id) or []
            downstream = [n for n in all_nodes
                          if n.get("type") in ("keyframe_generation", "video_generation", "video_merge")]
            deleted_ids: List[str] = []
            for n in downstream:
                if self.state.get_node(self.project_id, n["id"]):  # 幂等：可能已被某次级联删过
                    deleted_ids.extend(self.state.delete_node(self.project_id, n["id"], cascade=True) or [])
            deleted_ids = list(dict.fromkeys(deleted_ids))
            if deleted_ids:
                self.state.update_node(self.project_id, node_id, {"children": []})
                await self._notify("nodes_deleted", {"node_ids": deleted_ids, "count": len(deleted_ids)})
                logger.info("重跑分镜 %s：删除旧下游 %d 个（关键帧/视频/合并）", node_id, len(deleted_ids))

        self.state.update_node(self.project_id, node_id, {
            "result": None, "outputs": {}, "error": None, "confirmed": False,
        })
        self.state.set_node_status(self.project_id, node_id, NodeStatus.IDLE)
        asyncio.create_task(self._execute_node(node_id))
        return {
            "type": "node_rerunning",
            "node_id": node_id,
            "message": f"正在重新执行 {node.get('label', '节点')}...",
        }

    async def replace_node_image(
        self,
        node_id: str,
        image_url: str,
        field: str = "image_url",
    ) -> Dict[str, Any]:
        """用用户上传的图直接替换节点 result 里的某个图片字段。

        允许的 field：
          - image_url           主图（character 立绘 / scene 场景图 / keyframe 关键帧）
          - three_view_image_url 角色三视图（仅 character 节点）

        不重跑、不调模型，直接 patch result + WS 通知。
        """
        ALLOWED_FIELDS = {"image_url", "three_view_image_url"}
        if field not in ALLOWED_FIELDS:
            return {"error": f"不支持的 field: {field}"}
        if not image_url:
            return {"error": "image_url 不能为空"}

        node = self.state.get_node(self.project_id, node_id)
        if not node:
            return {"error": "节点不存在"}

        old_result = node.get("result") or {}
        new_result = {**old_result, field: image_url}
        # 用户上传的图永远不是 mock；同步清掉对应字段的 mock 标记
        if field == "image_url":
            new_result["mock"] = False
            new_result["user_uploaded"] = True
        elif field == "three_view_image_url":
            new_result["three_view_mock"] = False
            new_result["three_view_user_uploaded"] = True

        # 写回 result + outputs
        ntype = node.get("type") or ""
        kind = old_result.get("result_type") or ("keyframe" if ntype == "keyframe_generation" else None)
        updates = {"result": new_result}
        if kind:
            updates["outputs"] = {kind: new_result}
        self.state.update_node(self.project_id, node_id, updates)

        await self._notify("node_updated", {
            "node_id": node_id,
            "result": new_result,
            "message": f"已用用户上传的图片替换 {field}",
        })

        return {
            "type": "node_image_replaced",
            "node_id": node_id,
            "field": field,
            "image_url": image_url,
        }

    @staticmethod
    def _resolve_ep_kind(data: Dict[str, Any]):
        """判定 result 子节点是否分集子节点，返回 ep_character/ep_scene/ep_prop 或 None。
        兼容被旧重跑改坏 result_type（ep_character→character）的节点：靠 ep 专属字段 + parent_master 识别。"""
        rt = (data or {}).get("result_type") or ""
        if rt.startswith("ep_"):
            return rt
        if not data:
            return None
        if data.get("parent_master") or data.get("outfit_id") or data.get("outfit_name"):
            return "ep_character"
        if data.get("variation_id") or data.get("variation_name"):
            return "ep_scene"
        if data.get("state_id") or data.get("state_name"):
            return "ep_prop"
        return None

    async def _retry_episode_child(self, node_id: str) -> None:
        """重跑分集子节点：复用 _fill_episode_subject_image，保证用 master 参考图 img2img + 重建 composite。
        兼容被旧重跑改坏 result_type 的节点：用 _resolve_ep_kind 识别并修回正确 result_type。
        prompt 取舍见 _fill_episode_subject_image（result.prompt 优先）。"""
        node = self.state.get_node(self.project_id, node_id) or {}
        data = dict(node.get("result") or node.get("params") or {})
        kind = self._resolve_ep_kind(data)
        if not kind:
            await self._retry_result_child(node_id)
            return
        data["result_type"] = kind  # 修回被旧重跑改坏的 result_type，让后续聚合/路由都对
        # 0.7.3：重跑时从 live master 补 master 外形（旧节点 result 没有 master_appearance）→ 降级 t2i 也保身份不漂
        if kind == "ep_character" and not data.get("master_appearance"):
            try:
                _sid = await self._get_series_id_for_project()
                _shared = (await self._load_series(_sid) or {}).get("shared_assets") if _sid else None
                _key = data.get("master_character_id")
                for _mc in ((_shared or {}).get("characters") or []):
                    if ((_mc.get("character_id") or _mc.get("name")) == _key
                            or _mc.get("name") == data.get("character_name")):
                        data["master_appearance"] = (_mc.get("appearance") or _mc.get("prompt")
                                                     or _mc.get("description") or "")
                        break
            except Exception as _e:
                logger.warning("重跑补 master_appearance 失败: %s", _e)
        self.state.set_node_status(self.project_id, node_id, NodeStatus.RUNNING)
        try:
            await self._fill_episode_subject_image({"child": node, "data": data, "kind": kind})
        except Exception as e:
            logger.warning("重跑分集子节点 %s 失败: %s", node_id, e)
            self.state.set_node_status(self.project_id, node_id, NodeStatus.FAILED, error=str(e))

    async def _retry_result_child(self, node_id: str) -> None:
        """重跑 result 类型子节点：用节点 result 里存的 prompt 重新出图。
        - 角色：重新生成正面图 + 三视图（图生图，参考新正面图）
        - 场景：重新生成单张场景图
        - 道具（0.6.4+）：重新生成单张道具特写图

        容错：若节点 result 被污染（kind 缺失），从父节点 art_data 回溯 prompt。
        """
        async with self._run_with_status(node_id):
            node = self.state.get_node(self.project_id, node_id) or {}
            old = node.get("result") or {}
            kind = old.get("result_type")  # "character" | "scene" | "prop"

            # 容错路径：result 被破坏（比如之前的 bug 把 art_data 覆盖进来）
            # → 通过 label / parent art_data 重建 prompt
            # 0.7.0 轮 1：ep_character / ep_scene / ep_prop 视同 character/scene/prop 处理（同 retry 逻辑）
            EP_TO_MASTER_KIND = {"ep_character": "character", "ep_scene": "scene", "ep_prop": "prop"}
            if kind in EP_TO_MASTER_KIND:
                # 把 ep_character 的 outfit_prompt 提到 prompt，让下面统一分支用
                if kind == "ep_character" and not old.get("prompt"):
                    old["prompt"] = (old.get("outfit_prompt") or "").strip()
                kind = EP_TO_MASTER_KIND[kind]
            if kind not in ("character", "scene", "prop"):
                kind, recovered = self._recover_result_child(node)
                if kind is None or recovered is None:
                    logger.warning(
                        "retry_result_child: 节点 %s 的 result_type 缺失且无法从父节点恢复",
                        node_id,
                    )
                    return
                old = recovered
                logger.info("retry_result_child: 节点 %s 从父节点 art_data 恢复了 prompt", node_id)

            # 优先使用节点已编辑的 prompt（用户在右侧面板可能改过）
            base_prompt = (old.get("prompt") or old.get("fortified_prompt") or "").strip()
            if not base_prompt:
                logger.warning("retry_result_child: 节点 %s 没有 prompt", node_id)
                return

            new_data: Dict[str, Any] = {**old}

            # 0.6.0 阶段 ②d/②f：创作者上传参考图 → 多图融合（ARK Seedream）
            ref_urls = [u for u in (old.get("reference_image_urls") or []) if u]
            params_ref = [u for u in ((node.get("params") or {}).get("reference_image_urls") or []) if u]
            for u in params_ref:
                if u not in ref_urls:
                    ref_urls.append(u)

            # ⚠ Seedream 换装关键：把"原角色立绘"作为 Photo 1，把"用户参考图"作为 Photo 2+
            # 否则 Seedream 没有"原角色"视觉锚点，只会按 prompt 重画
            existing_image_url = old.get("image_url") or ""

            if kind == "character":
                subject_type = (old.get("subject_type") or "").strip().lower()
                if subject_type == "human":
                    base_prompt = self._dephoto_human_prompt(base_prompt)
                fortified = self._fortify_prompt(base_prompt, self.CHARACTER_BG_SUFFIX)

                if ref_urls:
                    # 组装：先放原立绘（Photo 1 = 角色），再放用户参考图（Photo 2+ = 服装/物品）
                    combined_urls: List[str] = []
                    if existing_image_url:
                        combined_urls.append(existing_image_url)
                    for u in ref_urls:
                        if u and u != existing_image_url:
                            combined_urls.append(u)
                    try:
                        img = await self._generate_image_from_urls(
                            combined_urls, fortified, width=1024, height=1024,
                        )
                    except Exception as e:
                        logger.warning("多图融合生成角色失败，降级文生图: %s", e)
                        img = await self._generate_image(fortified, width=1024, height=1024)
                else:
                    img = await self._generate_image(fortified, width=1024, height=1024)

                new_data["image_url"] = img.get("image_url")
                new_data["mock"] = img.get("mock", False)
                new_data["fortified_prompt"] = fortified

                # 重生三视图（仍参考新生成的正面图）
                tv_base = self._dephoto_human_prompt(base_prompt) if subject_type == "human" else base_prompt
                three_view_prompt = self._build_three_view_prompt(tv_base)
                tv_img: Dict[str, Any] = {}
                if new_data.get("image_url"):
                    try:
                        tv_img = await self._generate_image_from_image(
                            new_data["image_url"], self.THREE_VIEW_IMG2IMG_PROMPT,
                            width=1344, height=768,
                        )
                    except Exception as e:
                        logger.warning("重跑三视图（图生图）失败: %s", e)
                if not tv_img.get("image_url") or tv_img.get("mock"):
                    try:
                        tv_img = await self._generate_image(three_view_prompt, width=1344, height=768)
                    except Exception as e:
                        logger.warning("重跑三视图（文生图）失败: %s", e)
                        new_data["three_view_error"] = str(e)
                if tv_img.get("image_url"):
                    new_data["three_view_image_url"] = tv_img["image_url"]
                    new_data["three_view_mock"] = tv_img.get("mock", False)
                    new_data["three_view_prompt"] = three_view_prompt

                # 0.7.0 轮 2：重生 face_close_up（仅 human）+ composite_reference_url
                # 否则下游 keyframe / 视频还会用老脸 → 完全不一致
                new_frontal = new_data.get("image_url")
                if subject_type == "human" and new_frontal:
                    face_prompt = (
                        "extreme close-up portrait of the SAME character shown in the reference, "
                        "head and shoulders, facing camera, eyes open, neutral expression, "
                        "pure white background, studio lighting, no environment, "
                        "头部特写，肩部以上，正面平视，面部清晰，纯白背景，工作室灯光，无场景"
                    )
                    face_img: Dict[str, Any] = {}
                    try:
                        face_img = await self._generate_image_from_image(
                            new_frontal, face_prompt, width=1024, height=1024,
                        )
                    except Exception as e:
                        logger.warning("retry: face close-up img2img 失败，降级 t2i: %s", e)
                    if not face_img.get("image_url") or face_img.get("mock"):
                        try:
                            face_t2i_prompt = self._fortify_prompt(
                                self._dephoto_human_prompt(base_prompt) + ", head and shoulders portrait, facing camera",
                                ", 头部特写，肩部以上，纯白色背景，工作室灯光",
                            )
                            face_img = await self._generate_image(face_t2i_prompt, width=1024, height=1024)
                        except Exception as e:
                            logger.warning("retry: face close-up t2i 也失败: %s", e)
                    if face_img.get("image_url"):
                        new_data["face_close_up_url"] = face_img.get("image_url")
                        new_data["face_close_up_mock"] = face_img.get("mock", False)
                    else:
                        # 清掉旧的，避免下游用错脸
                        new_data.pop("face_close_up_url", None)
                        new_data.pop("face_close_up_mock", None)

                # 重新拼接 composite（左=face_close_up 或 frontal，右=新三视图）
                from app.core.paths import compose_character_reference
                left_img = new_data.get("face_close_up_url") or new_frontal
                if left_img and new_data.get("three_view_image_url"):
                    composite_url = compose_character_reference(left_img, new_data["three_view_image_url"])
                    if composite_url:
                        new_data["composite_reference_url"] = composite_url
                        logger.info("retry: character %s 重新拼接 composite: %s", old.get("name"), composite_url)
                    else:
                        # 拼接失败 → 清旧的避免用错
                        new_data.pop("composite_reference_url", None)
                else:
                    new_data.pop("composite_reference_url", None)
            elif kind == "scene":
                fortified = self._fortify_prompt(base_prompt, self.SCENE_BG_SUFFIX)
                if ref_urls:
                    # 场景：原场景图 (Photo 1) + 用户参考图 (Photo 2+)
                    combined_urls: List[str] = []
                    if existing_image_url:
                        combined_urls.append(existing_image_url)
                    for u in ref_urls:
                        if u and u != existing_image_url:
                            combined_urls.append(u)
                    try:
                        img = await self._generate_image_from_urls(
                            combined_urls, fortified, width=1344, height=768,
                        )
                    except Exception as e:
                        logger.warning("多图融合生成场景失败，降级文生图: %s", e)
                        img = await self._generate_image(fortified, width=1344, height=768)
                else:
                    img = await self._generate_image(fortified, width=1344, height=768)
                new_data["image_url"] = img.get("image_url")
                new_data["mock"] = img.get("mock", False)
                new_data["fortified_prompt"] = fortified
                # 0.7.5.4：场景重跑也要**重生机位板**（否则 establish 换了新图、机位板还是旧的，对不上）。
                # camera_setups 优先取节点存的；老场景没存 → 从现有 cameras[1:] 反推（camera_name + 存的 prompt）。
                _setups = old.get("camera_setups") or [
                    {"camera_id": c.get("camera_id"), "camera_name": c.get("camera_name"),
                     "camera_prompt": c.get("prompt") or c.get("camera_name"),
                     "when_to_use": c.get("when_to_use")}
                    for c in (old.get("cameras") or [])[1:] if c.get("camera_name")
                ]
                if _setups and new_data.get("image_url") and not img.get("mock"):
                    new_data["cameras"] = await self._generate_camera_boards(
                        node_id, new_data.get("image_url"), img.get("origin_url"),
                        base_prompt, _setups, 1344, 768,
                    )
            else:  # kind == "prop" (0.6.4+ 关键道具，方图、产品摄影感)
                fortified = self._fortify_prompt(base_prompt, self.PROP_BG_SUFFIX)
                if ref_urls:
                    combined_urls: List[str] = []
                    if existing_image_url:
                        combined_urls.append(existing_image_url)
                    for u in ref_urls:
                        if u and u != existing_image_url:
                            combined_urls.append(u)
                    try:
                        img = await self._generate_image_from_urls(
                            combined_urls, fortified, width=1024, height=1024,
                        )
                    except Exception as e:
                        logger.warning("多图融合生成道具失败，降级文生图: %s", e)
                        img = await self._generate_image(fortified, width=1024, height=1024)
                else:
                    img = await self._generate_image(fortified, width=1024, height=1024)
                new_data["image_url"] = img.get("image_url")
                new_data["mock"] = img.get("mock", False)
                new_data["fortified_prompt"] = fortified

            new_data["result_type"] = kind  # 强制写回，防止之后再被识别失败
            new_data["confirmed"] = False
            new_data["error"] = None
            # 清理可能从父 art_data 污染过来的字段
            for poison in ("visual_style", "characters", "scenes", "style_guide"):
                new_data.pop(poison, None)
            self.state.update_node(self.project_id, node_id, {
                "result": new_data,
                "outputs": {kind: new_data},
                "error": None,
                "confirmed": False,
            })

    def _recover_result_child(self, node: Dict) -> tuple[Optional[str], Optional[Dict]]:
        """子节点的 result 被破坏时，从父 art_design 节点的 result 回溯重建。

        靠 node.label 的 emoji（🎭=角色 / 🏞️=场景 / 🎁=道具）和 result_index 定位。
        Returns: (kind, rebuilt_result) 或 (None, None) 表示无法恢复
        """
        label = node.get("label") or ""
        if "🎭" in label:
            kind, list_key = "character", "characters"
        elif "🏞️" in label:
            kind, list_key = "scene", "scenes"
        elif "🎁" in label:
            kind, list_key = "prop", "props"
        else:
            return None, None

        # 找父节点（依赖列表里的第一个 art_design 节点）
        for dep_id in (node.get("depends_on") or []):
            dep = self.state.get_node(self.project_id, dep_id)
            if dep and dep.get("type") == "art_design":
                art_data = dep.get("result") or {}
                items = art_data.get(list_key) or []
                idx = node.get("result_index")
                if idx is None:
                    # 兜底：根据 label 文本匹配 name/location
                    label_clean = label.replace("🎭 ", "").replace("🏞️ ", "").replace("🎁 ", "").strip()
                    name_field = "location" if kind == "scene" else "name"
                    for it in items:
                        if it.get(name_field) == label_clean:
                            return kind, self._rebuild_result_from_art_item(kind, it)
                    return None, None
                # result_index 是绝对索引（fan-out 时按 character → scene → prop 顺序累加 offset）
                # character: idx = 0..n_char-1
                # scene:     idx = n_char..n_char+n_scene-1
                # prop:      idx = n_char+n_scene..end
                n_char = len(art_data.get("characters") or [])
                n_scene = len(art_data.get("scenes") or [])
                if kind == "scene":
                    real_idx = idx - n_char
                elif kind == "prop":
                    real_idx = idx - n_char - n_scene
                else:
                    real_idx = idx
                if 0 <= real_idx < len(items):
                    return kind, self._rebuild_result_from_art_item(kind, items[real_idx])
        return None, None

    @staticmethod
    def _rebuild_result_from_art_item(kind: str, item: Dict) -> Dict:
        """从 art_data 的 characters/scenes/props 列表项重建 result 字典"""
        if kind == "character":
            return {
                "result_type": "character",
                "prompt": item.get("image_prompt", ""),
                "character_id": item.get("character_id", ""),
                "name": item.get("name", ""),
                "subject_type": (item.get("subject_type") or "human").strip().lower(),
                "negative_prompt": item.get("negative_prompt", ""),
                "appearance": item.get("appearance", ""),
                "clothing": item.get("clothing", ""),
            }
        if kind == "prop":
            return {
                "result_type": "prop",
                "prompt": item.get("image_prompt", ""),
                "prop_id": item.get("prop_id", ""),
                "name": item.get("name", ""),
                "negative_prompt": item.get("negative_prompt", ""),
                "description": item.get("description", ""),
                "use_case": item.get("use_case", ""),
            }
        return {
            "result_type": "scene",
            "prompt": item.get("image_prompt", ""),
            "scene_id": item.get("scene_id", ""),
            "location": item.get("location", ""),
            "negative_prompt": item.get("negative_prompt", ""),
            "environment": item.get("environment", ""),
        }

    async def skip_node(self, node_id: str) -> Dict[str, Any]:
        node = self.state.get_node(self.project_id, node_id)
        if not node:
            return {"error": "节点不存在"}
        self.state.set_node_status(self.project_id, node_id, NodeStatus.SKIPPED)
        return await self.confirm_node(node_id)

    # ═══════════════════════════════════════════════════════
    # 各阶段 runner（实际跑 agent / 模块）
    # ═══════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════
    # 1.1.0 MV 模式
    # ═══════════════════════════════════════════════════════

    def _mv_progress_cb(self, node_id: str, emoji: str = "🎵"):
        """把模块/agent 的同步 progress_cb 桥到异步的 _progress。

        transcribe_song / plan_mv_shots 的回调是同步的（它们不该被迫 async），
        这里用 create_task 把消息推到前端。回调里绝不能抛异常，否则会中断整条链路。
        """
        def _cb(msg: str):
            try:
                asyncio.create_task(self._progress(node_id, f"{emoji} {msg}"))
            except Exception:
                pass
        return _cb

    async def _run_mv_audio(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        """歌曲解析：识别歌词时间轴 → 分析曲式情绪 → 锁定视觉风格。"""
        audio = params.get("audio_path") or ""
        if not audio:
            raise ValueError("MV 项目缺少音频文件，请先上传歌曲")

        # ① 歌词与时间轴（切片 + 偏移拼接，细节见 _zhipu_asr 顶部注释）
        await self._progress(node["id"], "🎵 正在识别歌词与时间轴...")
        from app.modules._zhipu_asr import transcribe_song
        asr = await transcribe_song(audio, progress_cb=self._mv_progress_cb(node["id"]))
        lyrics, duration = asr["lines"], asr["duration"]

        # ② 曲式与情绪 —— 段落边界决定下游怎么卡点，是这一步最关键的产出
        await self._progress(node["id"], f"🎼 已识别 {len(lyrics)} 句歌词，正在分析曲式与情绪...")
        song = await self.mv_director.analyze_song(
            lyrics=lyrics, duration=duration, hint=params.get("hint") or "")

        # ③ 视觉风格锁定（有参考图走 vision，没有就按预设文字展开）
        refs = params.get("style_refs") or []
        await self._progress(
            node["id"],
            f"🎨 正在从 {len(refs)} 张参考图提取视觉风格..." if refs else "🎨 正在确立视觉风格...")
        style = await self.mv_director.analyze_style_refs(
            image_urls=refs, preset=params.get("style_preset") or "",
            mv_type=params.get("mv_type") or "narrative")

        return {
            "lyrics": lyrics,
            "duration": duration,
            "song": song,
            "style": style,
            "audio_path": audio,
            "mv_type": params.get("mv_type") or "narrative",
            "user_brief": params.get("user_brief") or "",
            "asr_slices": asr.get("slices"),
        }, {}

    async def _run_mv_script(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        """MV 剧本：叙事型出故事+角色，意象型出视觉意象序列。"""
        mv_type = params.get("mv_type") or "narrative"
        await self._progress(
            node["id"],
            "✍️ MV 导演正在构思故事线与人物..." if mv_type == "narrative"
            else "✍️ MV 导演正在构思视觉意象序列...")
        script = await self.mv_director.build_mv_script(
            song=params.get("song") or {}, style=params.get("style") or {},
            mv_type=mv_type, user_brief=params.get("user_brief") or "")
        # 下游 art 阶段按 script.style 取风格字符串；这里补上，免得美术自由发挥跑偏
        if not script.get("style"):
            script["style"] = (params.get("style") or {}).get("style_prompt", "")
        # 美术和发布都按 script_title 取片名，而 MV 剧本本身没这个字段（会显示成「未命名」）。
        # 歌名就是最自然的片名，直接用上传的文件名。
        if not script.get("script_title"):
            _mv = ((self.state.get_project(self.project_id) or {}).get("config") or {}).get("mv") or {}
            _stem = os.path.splitext(os.path.basename(_mv.get("audio_path") or ""))[0]
            script["script_title"] = (
                _stem or str((params.get("song") or {}).get("theme") or "")[:24] or "未命名 MV"
            )
        return script, {}

    async def _run_mv_storyboard(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        """MV 卡点分镜：时间窗由代码算死，agent 只填画面内容。"""
        await self._progress(node["id"], "🎬 正在按歌曲节奏切分镜头...")
        # 重跑时优先读**最新**的歌曲解析结果（用户可能刚在校对界面改过歌词轴），
        # 读不到才退回建节点那刻写进 params 的旧快照。
        song, style = params.get("song") or {}, params.get("style") or {}
        lyrics = params.get("lyrics") or []
        duration = float(params.get("duration") or 0)
        mv_node = self._find_node_by_phase("mv_audio")
        if mv_node and (mv_node.get("result") or {}).get("lyrics"):
            live = mv_node["result"]
            song = live.get("song") or song
            style = live.get("style") or style
            lyrics = live.get("lyrics") or lyrics
            duration = float(live.get("duration") or duration)

        out = await self.mv_director.plan_mv_shots(
            song=song, style=style, script=params.get("script") or {},
            lyrics=lyrics, duration=duration,
            mv_type=params.get("mv_type") or "narrative",
            progress_cb=self._mv_progress_cb(node["id"], "🎬"))

        shots = out.get("shots") or []
        return {"storyboard": shots, "total_shots": len(shots), "duration": duration}, {}

    @staticmethod
    def _video_model_speaks_prompt() -> bool:
        """当前视频模型是不是「音画联合生成」的（会把提示词读出声那种）。

        这类模型（MiniMax H3）同时生成画面与语音，**提示词里的祈使句/说明文字会被当台词念**，
        嘴型跟着那段话动。所以给纯文本条件模型（Seedance）准备的文字约束前缀，
        到这里必须跳过——对它既无用（它靠参考图锁脸）又有害（念出来毁嘴型）。
        """
        try:
            from app.core.runtime_config import active_video_provider_name, comfyui_config
            if active_video_provider_name() != "comfyui":
                return False
            from app.modules._comfyui_h3 import is_h3_model
            return is_h3_model(comfyui_config().model)
        except Exception:
            return False

    def _shot_speech(self, shot: Dict[str, Any]) -> Dict[str, Any]:
        """镜头里「该念出来的话」——只给音画联合生成的模型（H3）用，别的 provider 拿了没用。

        🔴 这是「人物说胡话、嘴型对不上」的正解：**不是让它闭嘴，是让它念对的内容**。
           MV 用本镜覆盖的歌词（唱），其余用剧本对白（说）；取不到就返回空，
           由 format_prompt 补一句闭嘴描述兜底。

        speaker 固定用中性英文短语（在 build_dialogue_line 里），**不塞角色中文名**——
        描述整段是英文，夹一个中文 token 正好踩中「中文会被当台词念」那个坑。
        台词里的「金果：」这种名字前缀由 build_dialogue_line 统一剥（契约规则，不在这层做）。
        """
        if not self._video_model_speaks_prompt():
            return {}
        if self._is_mv_project():
            line, singing = (shot.get("lyric_text") or "").strip(), True
        else:
            line, singing = (shot.get("dialogue") or "").strip(), False
        if not line:
            return {}
        logger.info("[h3] 镜头台词（%s）：%s", "唱" if singing else "说", line[:40])
        return {"dialogue": line, "singing": singing}

    def _is_mv_project(self) -> bool:
        """当前项目是不是 MV。多处分叉都要问它，别各写各的判断。"""
        cfg = (self.state.get_project(self.project_id) or {}).get("config") or {}
        return (cfg.get("mode") or "") == "mv"

    def _find_node_by_phase(self, phase: str) -> Optional[Dict[str, Any]]:
        """按 phase 找项目里的节点（MV 链路回溯用）。同 phase 有多个时取最后一个。"""
        nodes = self.state.get_nodes_by_phase(self.project_id, phase) or []
        return nodes[-1] if nodes else None

    def _format_mv_audio_preview(self, data: Dict) -> str:
        song = data.get("song") or {}
        lyrics = data.get("lyrics") or []
        secs = song.get("sections") or []
        d = float(data.get("duration") or 0)
        out = [
            f"🎵 **歌曲解析完成**（{int(d // 60)}分{int(d % 60)}秒）", "",
            f"· 识别歌词 **{len(lyrics)}** 句",
            f"· 曲风：{song.get('genre', '—')}　情绪：{song.get('mood_arc', '—')}",
            f"· 主题：{song.get('theme', '—')}", "",
            f"**段落结构**（{len(secs)} 段）：",
        ]
        for sec in secs[:12]:
            st, en = float(sec.get("start", 0)), float(sec.get("end", 0))
            out.append(f"　{sec.get('type', '')}　{st:.0f}s–{en:.0f}s　{sec.get('summary', '')}")
        out += ["", f"**视觉风格**：{(data.get('style') or {}).get('style_prompt', '')}"]
        return "\n".join(out)

    def _format_mv_script_preview(self, script: Dict) -> str:
        n_scene = len(script.get("scenes") or [])
        if script.get("type") == "visual":
            return (f"🎞️ **MV 概念**\n\n{script.get('concept', '')}\n\n"
                    f"贯穿线索：{script.get('through_line', '')}\n场景 {n_scene} 个")
        chars = "、".join(c.get("name", "") for c in (script.get("characters") or []))
        return (f"🎞️ **MV 剧本**\n\n{script.get('logline', '')}\n\n"
                f"角色：{chars or '—'}　|　场景 {n_scene} 个")

    async def start_mv(self, mv_config: Dict[str, Any]) -> Dict[str, Any]:
        """MV 模式入口：写配置 + 建歌曲解析节点 + 开跑。

        MV 不走 director 的 checklist 对话——它的输入就是一首歌加几张图，
        没有什么要「问」的，直接开工。
        """
        project = self.state.get_project(self.project_id) or {}
        cfg = dict(project.get("config") or {})
        cfg["mode"] = "mv"
        cfg["mv"] = mv_config
        # 🔴 画幅要显式写进 collected_info：MV 不走 director 对话，collected_info 是空的，
        #    而 _project_aspect_ratio() 取不到值就 fallback 到 9:16 竖屏——MV 多数是横屏，
        #    不写死的话出来的成片方向就错了，而且要等出图才发现。
        ci = dict(project.get("collected_info") or {})
        ci["aspect_ratio"] = mv_config.get("aspect_ratio") or "16:9"
        self.state.update_project(self.project_id, {
            "config": cfg,
            "collected_info": ci,
            "phase": ProjectPhase.MV_AUDIO,
        })

        node = self._create_phase_node(
            phase="mv_audio",
            node_template_id="node_mv_audio",
            depends_on=[],
            position={"x": 100, "y": 200},
            params={
                "audio_path": mv_config.get("audio_path") or "",
                "style_refs": mv_config.get("style_refs") or [],
                "style_preset": mv_config.get("style_preset") or "",
                "mv_type": mv_config.get("type") or "narrative",
                "user_brief": mv_config.get("user_brief") or "",
            },
        )
        await self._notify("new_node", {"message": "🎵 开始解析歌曲...", "node": node})
        asyncio.create_task(self._execute_node(node["id"]))
        return {"type": "new_node", "node": node, "phase": "mv_audio",
                "message": "🎵 开始解析歌曲..."}

    async def _run_script(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        await self._progress(node["id"], "📝 编剧 Agent 正在创作剧本...")
        result = await self.screenwriter.execute(params)
        script = result.get("script", {})
        return script, {"summary": result.get("summary", {})}

    async def _run_art(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        await self._notify("node_status_changed", {
            "node_id": node["id"], "status": "running", "message": "🎨 正在设计视觉风格...",
        })
        await self._progress(node["id"], "🎨 美术导演正在设计视觉风格、角色、场景、道具...")
        result = await self.art_director.execute(params)
        art_data = result.get("art_direction", {})
        return art_data, {}

    # 0.7.0 轮 1：Episode 专用 art runner
    async def _run_episode_art(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        await self._notify("node_status_changed", {
            "node_id": node["id"], "status": "running",
            "message": "🎨 本集美术细化中（服装、场景光线、道具状态）...",
        })
        await self._progress(node["id"], "🎨 美术导演正在拿 master 资产做本集细化...")
        # 0.7.3：从 live series 重读最新 shared_assets，避免用建节点那刻的旧快照。
        # （master 后来补了角色/重新冻结时，分集重跑必须看到最新的全部角色；否则永远停在旧数量。）
        # 回写 params，让下游 _spawn_episode_art_children 也拿到刷新后的 master 清单。
        try:
            _sid = params.get("series_id") or await self._get_series_id_for_project()
            if _sid:
                _shared = (await self._load_series(_sid) or {}).get("shared_assets") or {}
                if _shared.get("characters"):
                    params = {**params, "master_shared_assets": _shared}
                    self.state.update_node(self.project_id, node["id"], {"params": params})
                    logger.info("_run_episode_art 刷新 master 快照 → %d 角色", len(_shared.get("characters") or []))
        except Exception as _e:
            logger.warning("_run_episode_art 刷新 master 快照失败，用旧 params: %s", _e)
        # 注入 mode='episode'，让 art_director 走 _execute_episode 分支
        ep_params = {**params, "mode": "episode"}
        result = await self.art_director.execute(ep_params)
        ep_art_data = result.get("art_direction", {})
        return ep_art_data, {}

    async def _run_storyboard(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        await self._progress(node["id"], "🎬 分镜导演正在拆分镜头...")
        # 0.7.0 #2: episode 用「分集计划时长」做分镜目标总时长（在 runner 里注入 → 重跑也生效，
        # 不依赖节点创建时写入的旧 params）。否则会退化成累加编剧场景时长。
        if not params.get("target_duration_seconds"):
            proj = self.state.get_project(self.project_id) or {}
            if (proj.get("advanced_outline") or {}).get("_from_expert_series"):
                planned_dur = (proj.get("collected_info") or {}).get("duration")
                if planned_dur:
                    params = {**params, "target_duration_seconds": planned_dur}
        # 0.7.1：注入项目画面比例（task 缺 aspect_ratio 时分镜 prompt 会默认竖屏 9:16）
        params = {**params, "aspect_ratio": self._project_aspect_ratio()}
        # 0.7.1 ①：重跑时从 art 节点重新收集 refs（含服装聚合 + 最新美术图），不依赖创建时旧快照；
        # 回写 params 持久化 → 下游 keyframe/video 与前端下拉/高亮都拿到聚合后 refs
        try:
            art_node = None
            for dep_id in (node.get("depends_on") or []):
                d = self.state.get_node(self.project_id, dep_id)
                if d and d.get("children"):
                    art_node = d
                    break
            if art_node:
                cr, sr, pr, _ = self._collect_art_refs(art_node)
                if cr or sr or pr:
                    params = {**params, "character_refs": cr, "scene_refs": sr, "prop_refs": pr}
                    self.state.update_node(self.project_id, node["id"], {"params": params})
        except Exception as e:
            logger.warning("storyboard 重跑刷新 refs 失败，沿用旧 params：%s", e)
        # 0.7.0 两趟：传 progress 回调，前端进度条跟着"骨架 → 补细节 N 批"动
        async def _cb(msg: str):
            await self._progress(node["id"], msg)
        result = await self.storyboard.execute(params, progress_cb=_cb)
        sb_data = result.get("storyboard", {})
        shots = sb_data.get("storyboard", []) if isinstance(sb_data, dict) else []
        return sb_data, {"shots": shots}

    # 0.6.1: audio_director — 基于分镜规划整片音频叙事
    async def _run_audio_plan(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        # 0.8 缝4：能力门控（local 恒放行）。抛出即节点转 FAILED，话术直接可读。
        from app.core.entitlements import require, Capability
        require(Capability.AUDIO_GENERATE)
        await self._notify("node_status_changed", {
            "node_id": node["id"], "status": "running",
            "message": "🎼 音频导演正在规划 BGM 叙事...",
        })
        await self._progress(node["id"], "🎼 音频导演正在规划整片 BGM 叙事节奏...")
        plan = await self.audio_director.execute(params)
        return plan, {}

    # 0.6.1: audio_plan 跑完后自动建 bgm_fetch 节点并执行
    async def _post_audio_plan(self, node_id: str, plan_data: Dict) -> None:
        """audio_plan 节点完成 → 创建 bgm_fetch 子节点 → 异步执行。"""
        # 0.8 缝4：真正产生音频消耗的是这一步（建 bgm_fetch 子节点），单独把闸
        from app.core.entitlements import require, Capability
        require(Capability.AUDIO_GENERATE)
        node = self.state.get_node(self.project_id, node_id) or {}
        parent_pos = node.get("position", {"x": 1000, "y": 600})

        total_duration = int(plan_data.get("total_duration") or 0)
        if total_duration <= 0:
            # 兜底：audio_plan params 里也有
            total_duration = int((node.get("params") or {}).get("total_duration") or 30)

        overall_mood = plan_data.get("overall_mood") or "ambient"
        hint = plan_data.get("mubert_playlist_index_hint") or ""
        mubert_prompt = plan_data.get("mubert_prompt") or ""
        mubert_intensity = plan_data.get("mubert_intensity") or "medium"

        bgm_spec = self.phases.get("bgm")
        if bgm_spec is None:
            logger.error("PhaseSpec[bgm] 未注册，跳过 bgm_fetch 创建")
            return

        # 直接走 state.create_node：bgm 节点 type=bgm_fetch、phase=audio（与 audio_plan 同 phase）
        # 0.6.2: label 跟着 active provider 走，避免一直显示 (Mubert)
        try:
            from app.core.runtime_config import active_audio_provider_name
            _prov = active_audio_provider_name()
        except Exception:
            _prov = "mubert"
        _prov_label = {"elevenlabs": "ElevenLabs", "mubert": "Mubert"}.get(_prov, _prov)
        bgm_node = self.state.create_node(self.project_id, {
            "node_id": "node_bgm_fetch",
            "type": bgm_spec.node_type,
            "label": f"BGM 生成 ({_prov_label})",
            "position": {"x": parent_pos["x"] + 300, "y": parent_pos["y"]},
            "params": {
                "duration": total_duration,
                "mood": overall_mood,
                # 0.6.1+: text-to-music prompt（优先）+ intensity；playlist_index 仅兜底
                "mubert_prompt": mubert_prompt,
                "mubert_intensity": mubert_intensity,
                "playlist_index_hint": hint,
                "segments": plan_data.get("segments") or [],
                "style_tags": plan_data.get("style_tags") or [],
            },
            "depends_on": [node_id],
            "agent": bgm_spec.agent_name,
            "need_confirm": True,
            "phase": bgm_spec.phase,
        })

        await self._notify("new_node", {
            "message": f"🎵 音频 plan 完成，开始拉取 BGM（{total_duration}s · {overall_mood}）",
            "node": bgm_node,
        })
        asyncio.create_task(self._execute_node(bgm_node["id"]))

    # 0.6.1: 调 Mubert 实际拉 BGM。失败时 result 含 error 但节点状态不抛异常
    # （merge 阶段会兜底跳过 BGM 混入）
    async def _run_bgm_fetch(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        """0.6.1/0.6.2: 拉 BGM。按 active_audio_provider_name 路由到 elevenlabs / mubert。
        失败时 result 含 error 但节点状态不抛异常（merge 阶段会兜底跳过 BGM 混入）。
        """
        from app.core.runtime_config import (
            mubert_config, elevenlabs_music_config, active_audio_provider_name,
        )
        from app.modules import mubert_music, elevenlabs_music

        provider = active_audio_provider_name()
        duration = int(params.get("duration") or 30)
        mood = params.get("mood") or params.get("overall_mood") or "ambient"
        mubert_prompt = params.get("mubert_prompt") or ""
        # 0.7.2: BGM 硬约束 —— 一律纯器乐。对白/旁白在视频音轨里，BGM 带人声唱词会打架；
        # ElevenLabs 不加约束时可能自由发挥英文唱词（古装题材里极其违和）。总漏斗追加，全模式受益。
        if mubert_prompt and "instrumental" not in mubert_prompt.lower():
            mubert_prompt = f"{mubert_prompt}, instrumental only, no vocals, no lyrics, no singing"
        mubert_intensity = params.get("mubert_intensity") or "medium"
        hint = params.get("playlist_index_hint") or ""

        await self._notify("node_status_changed", {
            "node_id": node["id"], "status": "running",
            "message": f"🎵 正在生成 BGM（{provider}）...",
        })
        await self._progress(node["id"], f"🎵 {provider} 正在生成 {duration}s BGM...")

        if provider == "elevenlabs":
            cfg = elevenlabs_music_config()
            bgm = await elevenlabs_music.generate_track(
                duration=duration,
                mood=mood,
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                model_id=cfg.model_id,
                output_format=cfg.output_format,
                prompt=mubert_prompt,        # audio_director 输出的 prompt 字段，两 provider 通用
                intensity=mubert_intensity,
            )
        else:
            cfg = mubert_config()
            bgm = await mubert_music.generate_track(
                duration=duration,
                mood=mood,
                company_id=cfg.company_id,
                license_token=cfg.license_token,
                base_url=cfg.base_url,
                playlist_index_hint=hint,
                prompt=mubert_prompt,
                intensity=mubert_intensity,
            )

        # 0.7.2 ②：专家分集的主题 track 完成 → 回写 series BGM 库（跨集复用通道）。
        # 多集并发生成时 read-modify-write 有小概率互覆，v1 接受（丢的只是复用缓存，可重生成）。
        track_key = (params.get("track_key") or "").strip()
        if track_key and not bgm.get("error") and bgm.get("local_path"):
            try:
                series_id = await self._get_series_id_for_project()
                if series_id:
                    series = await self._load_series(series_id) or {}
                    ss = dict(series.get("script_summary") or {})
                    lib = dict(ss.get("bgm_library") or {})
                    lib[track_key] = {
                        "local_path": bgm.get("local_path"),
                        "public_url": bgm.get("public_url"),
                        "duration": bgm.get("duration"),
                        "mood": params.get("mood") or "",
                        "prompt": params.get("mubert_prompt") or "",
                        "from_project": self.project_id,
                    }
                    ss["bgm_library"] = lib
                    await self._save_series_fields(series_id, {"script_summary": ss})
                    logger.info("BGM 库回写：%s（series=%s）", track_key, series_id)
            except Exception as e:
                logger.warning("BGM 库回写失败（不影响本集）：%s", e)
        return bgm, {}

    # ═══════════════════════════════════════════════════════
    # 0.7.2 ②：专家分层 BGM —— series 风格 / episode 场景计划 / 确认 spawn
    # ═══════════════════════════════════════════════════════

    async def _run_bgm_direction(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        """series 级 BGM 风格方向：通读整剧定基调，落 series.script_summary["bgm_direction"]。"""
        await self._progress(node["id"], "🎵 配乐总监通读整剧，规划 BGM 风格基调...")
        series_id = params.get("series_id") or await self._get_series_id_for_project()
        script_summary = params.get("script_summary") or {}
        excerpt = ""
        if series_id:
            series = await self._load_series(series_id) or {}
            script_summary = script_summary or (series.get("script_summary") or {})
            excerpt = (series.get("script_raw") or "")[:600]
        direction = await self.audio_director.plan_series_direction(script_summary, excerpt)
        if series_id:
            try:
                series = await self._load_series(series_id) or {}
                ss = dict(series.get("script_summary") or {})
                ss["bgm_direction"] = direction
                await self._save_series_fields(series_id, {"script_summary": ss})
            except Exception as e:
                logger.warning("bgm_direction 落库失败（节点结果仍可用）：%s", e)
        return direction, {}

    async def _run_episode_audio_plan(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        """episode 场景 BGM 计划：自动跑规划（LLM 便宜）；**确认后** _post_episode_audio_plan 才生成音乐。
        bgm_direction 与 BGM 库在此实时查 series（库动态增长，不烤进 params）。"""
        # 0.8 缝4：能力门控（local 恒放行）
        from app.core.entitlements import require, Capability
        require(Capability.AUDIO_GENERATE)
        await self._progress(node["id"], "🎼 音频导演按场景规划本集 BGM（多镜共用 / 优先复用主题）...")
        task = dict(params)
        try:
            series_id = await self._get_series_id_for_project()
            if series_id:
                series = await self._load_series(series_id) or {}
                ss = series.get("script_summary") or {}
                task["bgm_direction"] = ss.get("bgm_direction") or {}
                task["bgm_library"] = ss.get("bgm_library") or {}
        except Exception as e:
            logger.warning("episode_audio_plan 读取 series 失败（不阻塞，按无方向规划）：%s", e)
        plan = await self.audio_director.plan_episode_scenes(task)
        return plan, {}

    async def _post_episode_audio_plan(self, node_id: str, plan_data: Dict) -> Dict[str, Any]:
        """episode 计划**确认后**：按唯一 track_key spawn bgm_fetch（确认才花钱）；命中 series 库的主题直接复用不生成。"""
        node = self.state.get_node(self.project_id, node_id) or {}
        parent_pos = node.get("position", {"x": 1000, "y": 600})
        plan = (plan_data or {}).get("scene_bgm_plan") or []
        if not plan:
            return {"type": "node_confirmed", "node_id": node_id, "message": "本集无场景 BGM 计划"}

        library: Dict[str, Any] = {}
        try:
            series_id = await self._get_series_id_for_project()
            if series_id:
                series = await self._load_series(series_id) or {}
                library = (series.get("script_summary") or {}).get("bgm_library") or {}
        except Exception:
            pass

        # 唯一 track_key → 该 key 各窗的最长时长（复用到更短的窗时由合成阶段 atrim 截取）
        need: Dict[str, Dict[str, Any]] = {}
        for seg in plan:
            key = seg.get("track_key") or "theme"
            dur = max(5, int((seg.get("t1") or 0) - (seg.get("t0") or 0)))
            ent = need.setdefault(key, {"duration": 0, "seg": seg})
            ent["duration"] = max(ent["duration"], dur)

        try:
            from app.core.runtime_config import active_audio_provider_name
            _prov = {"elevenlabs": "ElevenLabs", "mubert": "Mubert"}.get(active_audio_provider_name(), "BGM")
        except Exception:
            _prov = "BGM"

        # ElevenLabs 订阅档并发上限=2（超出 429 concurrent_limit_exceeded）→ 信号量限流，其余排队
        bgm_sem = asyncio.Semaphore(2)

        async def _bounded_bgm(nid: str) -> None:
            async with bgm_sem:
                await self._execute_node(nid)

        spawned: List[str] = []
        reused: List[str] = []
        i = 0
        for key, ent in need.items():
            if key in library and (library.get(key) or {}).get("local_path"):
                reused.append(key)
                continue
            seg = ent["seg"]
            bgm_node = self.state.create_node(self.project_id, {
                "node_id": f"node_bgm_{uuid.uuid4().hex[:6]}",
                "type": "bgm_fetch",
                "label": f"BGM · {seg.get('track_label') or key} ({_prov})",
                "position": {"x": parent_pos["x"] + 300, "y": parent_pos["y"] + i * 150},
                "params": {
                    "duration": ent["duration"],
                    "mood": seg.get("mood") or "",
                    "mubert_prompt": seg.get("prompt") or "",
                    "mubert_intensity": seg.get("intensity") or "medium",
                    "track_key": key,
                },
                "depends_on": [node_id],
                "agent": "module_router",
                "need_confirm": True,
                "phase": "bgm",
            })
            spawned.append(bgm_node["id"])
            asyncio.create_task(_bounded_bgm(bgm_node["id"]))
            i += 1

        msg = (
            f"🎵 场景 BGM 计划确认：{len(plan)} 个窗 / {len(need)} 个主题 —— 生成 {len(spawned)} 个新主题"
            + (f"，复用库 {len(reused)} 个（{'、'.join(reused)}）" if reused else "")
        )
        await self._notify("episode_audio_confirmed", {"message": msg, "new_nodes": spawned})
        return {
            "type": "episode_audio_confirmed", "node_id": node_id,
            "message": msg, "spawned": spawned, "reused": reused,
        }

    # ─── 项目级画面比例工具 ───
    # 用户在 director 阶段说的 aspect_ratio 是整个项目的"真值源"，
    # 所有 fan-out 节点（keyframe / video / cover）都应该从这里取，
    # 避免在多处硬编码 9:16 / 720x1280 这种竖屏默认值。

    _RATIO_TO_RES = {
        "16:9": (1280, 720),
        "9:16": (720, 1280),
        "1:1":  (1024, 1024),
        "4:3":  (1024, 768),
        "3:4":  (768, 1024),
    }

    def _project_aspect_ratio(self) -> str:
        """从项目 collected_info 拿用户指定的画面比例；非法值 fallback 9:16。"""
        try:
            project = self.state.get_project(self.project_id) or {}
        except Exception:
            project = {}
        ci = project.get("collected_info") or {}
        ratio = (ci.get("aspect_ratio") or "").strip()
        return ratio if ratio in self._RATIO_TO_RES else "9:16"

    def _resolution_for_ratio(self, ratio: str) -> tuple[int, int]:
        return self._RATIO_TO_RES.get(ratio, (720, 1280))

    @staticmethod
    def _detect_aspect_ratio(text: str) -> str:
        """从用户消息里识别显式画面比例（横屏/竖屏/16:9 等），识别不到返回 ''。"""
        t = text or ""
        if any(k in t for k in ("9:16", "竖屏", "竖版", "抖音", "快手", "小红书")):
            return "9:16"
        if any(k in t for k in ("16:9", "横屏", "横版", "宽屏", "B站", "哔哩", "YouTube", "油管")):
            return "16:9"
        if "1:1" in t or "方形" in t:
            return "1:1"
        return ""

    @staticmethod
    def _extract_episode_minutes(text: str) -> Optional[int]:
        """从创作者消息里解析"每集大概多少分钟"。识别不到返回 None。"""
        if not text:
            return None
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:分钟|分鐘|min|mins|分)", text)
        if m:
            try:
                v = float(m.group(1))
                if 0.5 <= v <= 60:
                    return int(round(v))
            except ValueError:
                pass
        return None

    @staticmethod
    def _build_prefs_ask(summary: Dict[str, Any]) -> str:
        """通读完成后，导演提醒创作者确认「每集时长」+「横屏/竖屏」（带通读建议作默认）。"""
        genre = summary.get("genre") or "—"
        tone = summary.get("tone") or "—"
        chars = len(summary.get("characters") or [])
        scenes = len(summary.get("scene_outline") or [])
        est_total = summary.get("est_total_min") or 0
        ep_cnt = summary.get("suggested_episode_count") or 0
        ar = summary.get("suggested_aspect_ratio") or "9:16"
        ar_label = {"9:16": "竖屏 9:16", "16:9": "横屏 16:9", "1:1": "方形 1:1"}.get(ar, ar)
        per = round(est_total / ep_cnt) if (est_total and ep_cnt) else 8
        return (
            f"📖 剧本通读完了！\n"
            f"- 类型：{genre}　基调：{tone}\n"
            f"- 主要角色 {chars} 个、关键场景 {scenes} 段；我估摸整部约 {est_total or '—'} 分钟，建议切 {ep_cnt or '若干'} 集\n\n"
            f"开始分集前先跟你确认两件事（我按剧情来安排，**不注水也不硬缩**）：\n"
            f"① **每集大概多长**？建议每集 ≈ {per} 分钟，你想更长/更短都行——太长我在保剧情前提下精简，太短想加我就丰富剧情节拍\n"
            f"② **视频横屏还是竖屏**？建议 {ar_label}（漫剧一般竖屏，横屏更像电影）\n\n"
            f"回我一句就行，比如「每集 8 分钟，竖屏」。"
        )

    async def _resolve_chain_first_frame(self, node: Dict[str, Any]) -> Optional[str]:
        """W5：若 node 是连贯链的 link（chain_pos>0），找同链上一镜（chain_pos-1）的成片，
        提其尾帧作为本镜首帧 —— 让"单镜手动生成"也能续接（批量路径用 _run_chain 内存传帧）。
        前一镜还没出片 → 返回 None（优雅回退该镜自己 built 的首帧）。"""
        params = node.get("params") or {}
        head_idx = params.get("chain_head_index")
        pos = params.get("chain_pos")
        if head_idx is None or not pos:  # None=非链；0=链首(无前驱) → 不处理
            return None
        for n in self.state.get_all_nodes(self.project_id) or []:
            if n.get("type") != "video_generation":
                continue
            p = n.get("params") or {}
            if p.get("chain_head_index") == head_idx and p.get("chain_pos") == pos - 1:
                vurl = (n.get("result") or {}).get("video_url")
                if vurl:
                    return await self._extract_last_frame(vurl, tag=node.get("id", ""))
                return None
        return None

    def _is_auto_fanout_mode(self) -> bool:
        """简易模式 = keyframe/video 确认后自动执行（非专家分集、非独立进阶）。
        与 _fanout_storyboard 里 `manual_fanout = is_expert or is_advanced` 严格取反 ——
        连贯链的自动串行推进只在简易模式做；专家/进阶靠 batch 的 _run_chain 驱动。"""
        proj = self.state.get_project(self.project_id) or {}
        cfg = proj.get("config") or {}
        is_expert = bool(cfg.get("expert_seeded") or cfg.get("series_id") or proj.get("series_id"))
        is_advanced = (cfg.get("mode") == "advanced")
        return not (is_expert or is_advanced)

    async def _propagate_tail_to_next_link(self, node: Dict[str, Any]) -> None:
        """W5 即时传帧：某镜视频出片后，立刻抽它的尾帧推给"同链下一镜"的 first_frame_url，
        让下一链镜**生成前就能预览真尾帧**（不必再等它自己生成时才 _resolve）。
        只注入尚未出片的下一镜，绝不覆盖已生成镜的真实首帧。"""
        if node.get("type") != "video_generation":
            return
        params = node.get("params") or {}
        head_idx = params.get("chain_head_index")
        pos = params.get("chain_pos")
        if head_idx is None or pos is None:
            return  # 非链镜
        vurl = (node.get("result") or {}).get("video_url")
        if not vurl:
            return
        # 找同链 chain_pos == pos+1 的下一镜
        nxt = None
        for n in self.state.get_all_nodes(self.project_id) or []:
            if n.get("type") != "video_generation":
                continue
            p = n.get("params") or {}
            if p.get("chain_head_index") == head_idx and p.get("chain_pos") == pos + 1:
                nxt = n
                break
        if not nxt:
            return
        # 上游重跑判定：下一镜**已出片** = 这是「上游重跑」而非首次生成 —— 前镜尾帧变了，
        # 下一镜的续接帧和视频都过期。要更新续接帧 + 清掉过期视频（否则续接帧预览显示新尾帧、
        # 视频却还是从旧尾帧生成的，二者对不上）。这修的正是"重跑上一镜下一镜续接帧不更新"的 bug。
        nxt_had_video = bool((nxt.get("result") or {}).get("video_url"))
        # 尽力抽新尾帧（供续接帧预览）；失败不阻断 —— 下一镜真正生成时 _resolve_chain_first_frame 会重解析。
        try:
            tail = await self._extract_last_frame(vurl, tag=nxt.get("id", ""))
        except Exception as e:
            logger.warning("W5 即时传帧抽尾帧失败 %s: %s（下一镜生成时会重解析）", node.get("id"), e)
            tail = None
        if tail:
            self.state.update_node(self.project_id, nxt["id"], {
                "params": {**(nxt.get("params") or {}), "first_frame_url": tail, "first_frame_from_chain": True},
            })
            logger.info("W5 即时传帧：%s 尾帧 → %s first_frame", node.get("id"), nxt["id"])
        if nxt_had_video:
            # 上游重跑 → 下一镜过期：清视频、退回 idle（前端显示"待重生成"）。用户重跑/自动链会用
            # 新续接帧重生成它；它出片后再把新尾帧传给更下一镜 → 级联失效逐镜自动传递到链尾。
            _nr = {k: v for k, v in (nxt.get("result") or {}).items()
                   if k not in ("video_url", "origin_url", "mock", "task_id")}
            self.state.update_node(self.project_id, nxt["id"], {"result": _nr, "status": "idle"})
            logger.info("W5 上游重跑 → 下一镜 %s 视频过期已清、续接帧已更新，待重生成", nxt["id"])
        nxt_after = self.state.get_node(self.project_id, nxt["id"]) or {}
        await self._notify("node_updated", {"node": nxt_after})

        # 简易(自动)模式：事件驱动串行链 —— 前镜出片后触发下一 link 生成（含刚清掉的过期镜 → 级联重生成）。
        # 这是修"link 抢在链首前生成"的关键：link 不在 fanout 时并发发出，而是等到这里、
        # 确保前镜已出片、拿得到真尾帧才触发。
        # **专家/进阶不在此触发** —— 它们走 batch 的 _run_chain 自行串行驱动，这里触发会双发。
        # **只在首次生成时自动推进**（下一镜从没出过片）；上游**重跑**(nxt_had_video)时**不自动烧 credits**
        # —— 只更新续接帧+标记待重生成，让用户手动逐镜重跑（避免"重跑链首=自动重生成整条下游"意外扣费）。
        if self._is_auto_fanout_mode() and not nxt_had_video:
            nxt_now = self.state.get_node(self.project_id, nxt["id"]) or {}
            st = str(nxt_now.get("status") or "").lower()
            if "idle" in st or "failed" in st:      # 只触发未跑的，避免重复触发
                logger.info("W5 链自动推进：%s 出片 → 触发下一镜 %s", node.get("id"), nxt["id"])
                asyncio.create_task(self._execute_node(nxt["id"]))

    async def _run_video(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        """视频生成。
        - 优先使用 fanout 时已经解析好的 params['prompt']（@ 引用替换后的完整 prompt）
        - 有合法 first_frame_url 时走 image_to_video（图生视频，保持场景一致）
        - first_frame 不存在 / 是空文件 / 被 Seedance 内容审核拒绝 → 自动降级到 text_to_video
        """
        shot = params.get("shot") or {}
        prompt = (
            params.get("prompt")
            or shot.get("video_prompt")
            or shot.get("visual_prompt")
            or params.get("scene_description", "")
            or ""
        )
        # 分段时间标记 cNN,Ns, 仅供 Seedance 在一镜内定时；本地 ComfyUI(LTX/Wan)不识别 → 发送前剥掉避免干扰。
        # 时间信息仍保留在 params.prompt / raw_video_prompt（接 Seedance 时再取用）。
        if prompt:
            prompt = re.sub(r"c\d+\s*,\s*\d+\s*s\s*,\s*", "", prompt).lstrip()
        # 0.7.1：全局一致性前缀（脸部/角色锁定 + 无文字 + 无 BGM，参照专业模板）—— 对每条视频 prompt 生效；带去重守卫
        # 🔴 1.1.0：音画联合生成的模型（MiniMax H3）必须跳过这段。
        #    实测 2026-09-09：这段祈使句被 H3 当台词**念了出来**——把成片音轨转写回来是
        #    「全程同一早宴无换脸无串脸无人脸漂移」，跟原文一字对应；人物嘴型跟着这段话动，
        #    画面上就像在说胡话。而 H3 本来就靠参考图/首帧锁脸，这段文字约束对它既无用又有害。
        if prompt and "五官骨相固定不变" not in prompt and not self._video_model_speaks_prompt():
            prompt = (
                "全程严格参照参考图人物脸部特征生成；五官骨相固定不变，脸型/眼睛/鼻子/嘴唇比例永久一致；"
                "全程同一角色，无换脸、无串脸、无人脸漂移；发型、妆容、年龄感固定；镜头切换后人物保持完全一致。"
                "画面不出现任何文字/字幕/水印，无 BGM。\n" + prompt
            )
        duration = int(shot.get("duration_seconds") or params.get("duration", 5))

        # W5：链式 link 镜"单镜手动生成"（非批量）时，也补"同链上一镜尾帧"作首帧。
        # 批量路径 _run_chain 已在内存里注入并打 first_frame_from_chain；此处给逐个手动生成兜底。
        if not params.get("first_frame_from_chain"):
            chain_ff = await self._resolve_chain_first_frame(node)
            if chain_ff:
                params = {**params, "first_frame_url": chain_ff, "first_frame_from_chain": True}
                self.state.update_node(self.project_id, node["id"], {
                    "params": {**(node.get("params") or {}), "first_frame_url": chain_ff,
                               "first_frame_from_chain": True},
                })
                logger.info("W5 单镜链续接：%s 用同链上一镜尾帧 %s", node.get("id"), chain_ff)

        first_frame = params.get("first_frame_url") or shot.get("first_frame_url")

        # 校验 first_frame 是否真的有效（必须是 /output/... 路径，且本地文件 > 1KB）
        if first_frame and not self._is_valid_first_frame(first_frame):
            logger.warning("first_frame %s 无效（不存在或太小），降级为 text_to_video", first_frame)
            first_frame = None

        # 0.7.3 统一 asset:// 管线（antchain）：关键帧入 asset 库 → asset://火山id 当首帧。
        # 实测(07-02 双角色)：asset:// 虚拟素材引用【免真人审核】——真人图直传 origin_url/asset_url 才被
        # PrivacyInformation 拦；入库成 asset:// 后免审，且关键帧当首帧=精确构图。缓存 first_frame_volc_id 避免重复入库。
        first_frame_asset = None  # asset://火山id（antchain 专用免审首帧）
        if first_frame:
            try:
                from app.core.runtime_config import active_video_provider_name as _avp2
                if _avp2() == "antchain":
                    from app.modules._antchain_video import register_asset as _reg_kf
                    _kf_vid = (node.get("result") or {}).get("first_frame_volc_id")
                    if not _kf_vid:
                        _kf_vid = await _reg_kf(first_frame, f"keyframe_{str(node.get('id',''))[:8]}")
                        if _kf_vid:
                            _r = node.get("result") or {}
                            self.state.update_node(self.project_id, node["id"],
                                                   {"result": {**_r, "first_frame_volc_id": _kf_vid}})
                    if _kf_vid:
                        first_frame_asset = f"asset://{_kf_vid}"
                        logger.info("[keyframe asset] 关键帧入库 → %s 当免审首帧", first_frame_asset)
            except Exception as _e:
                logger.warning("关键帧入 asset 库失败，用原图首帧(可能触发真人审核): %s", _e)

        # 比例：优先 params 透传（fan-out 时写入），否则回退到项目级 aspect_ratio
        ci = (params.get("aspect_ratio") or "").strip()
        ratio = ci if ci in self._RATIO_TO_RES else self._project_aspect_ratio()
        v_w, v_h = self._resolution_for_ratio(ratio)

        # 0.7.1 #5：角色参考图喂 Seedance（多模态全能参考）锁主体身份。
        # **优先单张人脸特写(face_close_up)** —— Seedance 对"单张清晰人脸"引用最稳；composite(脸+三视图)
        # 是含多 figure 的角色表，Seedance 容易当多人脸 → 不一致(memory 实测)。从角色节点实时取最新脸图，
        # 不依赖 fan-out 冻结的旧 ref。仅 Seedance(ark) 用；封顶 3 张控 payload。配合 prompt 的"参照参考图脸部特征"前缀。
        seedance_refs: List[str] = []
        for _c in (params.get("referenced_characters") or []):
            _u = None
            _nid = _c.get("node_id")
            if _nid:
                _art = self.state.get_node(self.project_id, _nid)
                _ar = (_art or {}).get("result") or {}
                _u = _ar.get("face_close_up_url") or _ar.get("composite_reference_url")
            _u = _u or _c.get("face_close_up_url") or _c.get("composite_reference_url") or _c.get("three_view_image_url") or _c.get("image_url")
            if _u and _u not in seedance_refs:
                seedance_refs.append(_u)
        seedance_refs = seedance_refs[:3]

        async def _emit_progress(progress: float, message: str):
            """把 module 内部的轮询进度推到前端"""
            await self._notify("node_progress", {
                "node_id": node["id"],
                "progress": progress,
                "message": message,
            })

        async def _try_image_to_video() -> Dict:
            await self._notify("node_status_changed", {
                "node_id": node["id"], "status": "running",
                "message": "🎥 调用 Seedance 生成视频（图生视频，保持场景一致）...",
            })
            module = ModuleRegistry.get("image_to_video")
            # 0.7.3 人物一致性：asset_id 实时从角色节点读（入库异步 ~16s，快照常早于入库完成 → null）
            # 同时记角色名 —— 火山虚拟人像正解：prompt 必须用"图片N"指代 content 里的图，模型才知道哪张是要锁的主体
            _asset_ids: List[str] = []
            _asset_names: List[str] = []
            for _c in (params.get("referenced_characters") or []):
                _aid = _c.get("character_asset_id")
                if not _aid and _c.get("node_id"):
                    _art = self.state.get_node(self.project_id, _c["node_id"])
                    _aid = ((_art or {}).get("result") or {}).get("character_asset_id")
                if _aid and _aid not in _asset_ids:
                    _asset_ids.append(_aid)
                    _asset_names.append(_c.get("name") or "角色")
            # image_url 顺序：图片1=首帧、图片2+=各角色 asset（与 _antchain_video 拼接顺序一致）→ prompt 指代对应
            _vid_prompt = prompt
            if _asset_ids:
                # 火山 Seedance 新范式（用户官方示意确认）：image_url 全是 reference_image、**无 first_frame role**，
                # 首帧/角色全靠 prompt 用"图片N"指代调度。image_url 顺序：图片1=关键帧(镜头画面)、图片2+=各角色 asset。
                # 关键：图片1(关键帧)含已漂移的脸，与图片2(asset标准脸)冲突 → 明确分工「构图跟图片1，面部只认图片2」
                _refs = ["画面构图、镜头角度、人物姿态与朝向严格遵循图片1"]
                for _i, _nm in enumerate(_asset_names):
                    _refs.append(f"但角色「{_nm}」的面部长相以图片{_i + 2}为唯一标准，忽略图片1中该角色的面部，全程严格保持其五官、脸型、发型与图片{_i + 2}完全一致，不得改变长相")
                _vid_prompt = "；".join(_refs) + "。\n" + prompt
            return await module.run({
                "image_url": first_frame_asset or first_frame,  # antchain: asset://火山id(免审首帧); 其它 provider: 原图
                "prompt": _vid_prompt,
                "duration": duration,
                "fps": 24,
                "resolution": f"{v_w}x{v_h}",
                "reference_images": seedance_refs,  # 0.7.1 #5：角色 composite 喂 Seedance(ark) 锁身份
                "character_asset_ids": _asset_ids,  # 0.7.3：antchain 用 asset://火山id 锁身份（实时读，不用快照）
                **self._shot_speech(shot),          # 1.1.0：H3 走 <d> 标签把歌词/对白念对
                "_progress_callback": _emit_progress,
            })

        async def _try_text_to_video() -> Dict:
            await self._notify("node_status_changed", {
                "node_id": node["id"], "status": "running",
                "message": "🎥 调用 Seedance 生成视频（文生视频）...",
            })
            module = ModuleRegistry.get("text_to_video")
            return await module.run({
                "prompt": prompt,
                "duration": duration,
                "ratio": ratio,
                "resolution": "720p",
                "generate_audio": True,
                **self._shot_speech(shot),          # 同上
                "_progress_callback": _emit_progress,
            })

        # 记录降级链路，便于在前端 result 里看到为什么是 mock
        attempts: List[str] = []

        # 1. 有合法首帧：先试图生视频；返回 mock 时记录原因再试文生视频
        # 保留各 provider 返回的**真实** mock_reason（如"Octopus 云端中转调用失败: content[1].image_url..."），
        # 别被最后那句笼统的 "Seedance 调用失败" 盖掉——否则排查时看不到真因。
        real_reason = ""
        if first_frame:
            try:
                result = await _try_image_to_video()
                attempts.append(f"image_to_video: mock={result.get('mock')} reason={str(result.get('mock_reason'))[:160]}")
                if not result.get("mock"):
                    result["attempts"] = attempts
                    return result, {}
                real_reason = result.get("mock_reason") or real_reason
                logger.warning("image_to_video 返回 mock（%s），再试 text_to_video", result.get("mock_reason"))
            except Exception as e:
                attempts.append(f"image_to_video: exception={type(e).__name__}: {str(e)[:200]}")
                real_reason = real_reason or f"{type(e).__name__}: {str(e)[:200]}"
                logger.warning("image_to_video 失败：%s，降级到 text_to_video", e)
        else:
            attempts.append("image_to_video: skipped (no valid first_frame)")

        # 2. 文生视频
        try:
            result = await _try_text_to_video()
            attempts.append(f"text_to_video: mock={result.get('mock')} reason={str(result.get('mock_reason'))[:160]}")
            result["attempts"] = attempts
            if result.get("mock"):
                # 优先展示真实 provider 报错；两路都 mock 时把首帧路的真因带出来
                real_reason = result.get("mock_reason") or real_reason
                result["mock_reason"] = real_reason or "视频生成失败（首帧/纯文本路径都被拒或超时）— 看 attempts 字段"
            return result, {}
        except Exception as e:
            attempts.append(f"text_to_video: exception={type(e).__name__}: {str(e)[:200]}")
            logger.error("text_to_video 也失败：%s", e)
            return {
                "video_url": "", "status": "error", "error": str(e),
                "attempts": attempts,
            }, {}

    @staticmethod
    def _is_valid_first_frame(first_frame: str) -> bool:
        """校验 first_frame_url 指向的本地文件是否合法（存在且 > 1KB）"""
        from app.core.config import settings as _settings
        if not first_frame:
            return False
        if first_frame.startswith(("http://", "https://", "data:")):
            return True
        if first_frame.startswith("/output/"):
            local = os.path.join(_settings.OUTPUT_DIR, first_frame[len("/output/"):])
        else:
            local = first_frame
        try:
            return os.path.exists(local) and os.path.getsize(local) > 1024
        except OSError:
            return False

    async def _extract_last_frame(self, video_url: str, *, tag: str = "") -> Optional[str]:
        """W5：提取视频尾帧 → PNG 存 images_dir → 返回 /output/images/... URL。
        失败返回 None（调用方回退到该镜自己的 built 首帧）。ffmpeg 提帧，provider 无关。"""
        import subprocess
        import uuid as _uuid
        from app.core.paths import images_dir, resolve_output_path, to_public_url

        if not video_url:
            return None
        local_in = resolve_output_path(video_url)
        if not local_in or not os.path.exists(local_in):
            logger.warning("W5 尾帧提取：源视频不存在 %s -> %s (%s)", video_url, local_in, tag)
            return None
        out = os.path.join(images_dir(), f"lastframe_{_uuid.uuid4().hex[:12]}.png")

        def _ok() -> bool:
            try:
                return os.path.exists(out) and os.path.getsize(out) > 1024
            except OSError:
                return False

        def _run(cmd: List[str]) -> None:
            try:
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            except Exception as e:  # noqa: BLE001 — ffmpeg 任何失败都回退
                logger.warning("W5 尾帧提取 ffmpeg 出错 %s (%s)", e, tag)

        # 快路径：从尾部 0.5s 取一帧（-update 1 覆盖，留窗口内最后一帧）
        await asyncio.to_thread(_run, [
            "ffmpeg", "-y", "-sseof", "-0.5", "-i", local_in,
            "-update", "1", "-frames:v", "1", out,
        ])
        if not _ok():
            # 回退：reverse 取真·最后一帧（解全片，慢但精确，≤15s 镜可接受）
            await asyncio.to_thread(_run, [
                "ffmpeg", "-y", "-i", local_in, "-vf", "reverse", "-frames:v", "1", out,
            ])
        if not _ok():
            logger.warning("W5 尾帧提取失败 %s (%s)", video_url, tag)
            return None
        logger.info("W5 尾帧提取成功 %s -> %s (%s)", video_url, out, tag)
        return to_public_url(out)

    async def _run_merge(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        module = ModuleRegistry.get("video_merge")
        if not module:
            return ({"video_url": "", "status": "mock", "message": "video_merge 模块未注册"}, {})

        # 0.6.2: 每次执行前**重新查最新 bgm_fetch**，覆盖 params 里 stale 的 bgm_path
        # 否则用户切了 provider 重跑 bgm_fetch 后，merge 节点还会用老 mp3
        # 0.7.2 ②：专家分集优先用"场景分层 BGM 合成轨"（各窗按 track 拼成一条整片轨，
        # 下游仍走单 bgm_path 链路）；没有计划/缺 track/拼接失败 → 回退 0.6.2 单段逻辑。
        latest_bgm = (await self._compose_episode_bgm()) or self._find_latest_bgm()
        if latest_bgm:
            params = dict(params)
            params["bgm_path"] = latest_bgm.get("local_path") or params.get("bgm_path")
            # audio_segments 也跟着 plan 走
            seg = latest_bgm.get("audio_segments")
            if seg is not None:
                params["audio_segments"] = seg

        # 修 bug：合并前用各视频节点的**最新** video_url 刷新 videos —— params["videos"] 是合并节点
        # 创建时的快照，分镜重跑后不会更新它 → 重跑合并会把旧视频又合一遍。按 video_node_id 对应
        # 回各节点实时 result，**保留用户拖拽排序**，只换掉过期 URL（跟上面 BGM 重取同理）。
        params = dict(params)
        _vids, _changed = [], 0
        for v in (params.get("videos") or []):
            vnid = v.get("video_node_id")
            _cr = (self.state.get_node(self.project_id, vnid) or {}).get("result") if vnid else None
            if isinstance(_cr, dict) and _cr.get("video_url"):
                if _cr.get("video_url") != v.get("video_url"):
                    _changed += 1
                v = {**v, "video_url": _cr.get("video_url"),
                     "origin_url": _cr.get("origin_url"), "mock": _cr.get("mock")}
            _vids.append(v)
        params["videos"] = _vids

        # ── 1.1.0 MV：成片必须严格跟着歌走 ──
        _proj = self.state.get_project(self.project_id) or {}
        _pcfg = _proj.get("config") or {}
        if (_pcfg.get("mode") or "") == "mv":
            _song = (_pcfg.get("mv") or {}).get("audio_path") or ""
            if _song:
                # MV 的声音只有那首歌：丢掉 Seedance 每段配的环境音/人声，音量不压
                params["bgm_path"] = _song
                params["audio_mode"] = "replace"
                params["bgm_volume_db"] = 0.0
            # 每段对齐到分镜算出来的时间窗（Seedance 只能出整数秒，不校正会累积漂移）
            _durs = []
            for _v in params.get("videos") or []:
                _vn = self.state.get_node(self.project_id, _v.get("video_node_id")) or {}
                _shot = (_vn.get("params") or {}).get("shot") or {}
                _durs.append(float(_shot.get("duration_seconds") or 0))
            if _durs and all(d > 0 for d in _durs):
                # 🔴 切点整体提前一点点，否则画面看起来"慢半拍"。
                #    实测（2026-09-08，31 镜的真实 MV）：29 个有词镜头里 16 个的切点
                #    距离首句起唱不足 0.2 秒，其中 13 个正好是 0.00 秒——那是锚点对齐的
                #    必然结果（歌词行被钉在锚点时刻，镜头边界也切在同一点），
                #    于是画面切换与唱词同时发生。剪辑上切点要落在拍点**前**几帧才跟得上；
                #    再叠加生成视频开头动作渐起，观感就是迟到。
                #    做法：把首段缩短 lead 秒，后续所有切点自然整体前移，
                #    镜头内容和时长关系不变，只是提前登场。
                try:
                    from app.core.user_settings_service import user_settings as _us
                    _lead = float(_us.get("comfyui", "mv_cut_lead") or 0.12)
                except (TypeError, ValueError, ImportError):
                    _lead = 0.12
                if _lead > 0 and len(_durs) > 1 and _durs[0] - _lead >= 2.0:
                    _durs[0] = round(_durs[0] - _lead, 3)
                    logger.info("[mv] 切点整体提前 %.2fs（首段 %.2f→%.2f）",
                                _lead, _durs[0] + _lead, _durs[0])
                params["target_durations"] = _durs
            else:
                logger.warning("[mv] 取不到完整的镜头时长（%s），跳过时间窗对齐", _durs)
            # 🔴 转场会吃时间：xfade 每个接缝吞掉 trans_dur 秒，三十个镜头累计好几秒，
            #    画面会整体越走越早、跟歌脱节。MV 一律纯拼接（"none" → concat 滤镜，零损耗）。
            params["transition"] = "none"
            logger.info("[mv] 合成参数：音轨替换=%s，%d 段做时间窗对齐，纯拼接无转场",
                        bool(_song), len(_durs))
        if _changed:
            logger.info("合并前刷新 %d 段视频为重跑后的最新版（避免合并旧片）", _changed)

        n_seg = len((params.get("videos") or []))
        bgm_hint = "（含 BGM 混音）" if params.get("bgm_path") and params.get("bgm_only", True) else ""
        await self._progress(node["id"], f"🎬 ffmpeg 正在合成 {n_seg} 段视频{bgm_hint}...")
        result = await module.run(params)
        return result, {}

    def _find_latest_bgm(self) -> Optional[Dict[str, Any]]:
        """0.6.2: 返回最新的 bgm_fetch 节点 + 对应 audio_plan 的 segments。
        多个 bgm_fetch 时取 created_at 最大的（最近一次成功生成）。
        Returns {local_path, audio_segments} 或 None
        """
        bgm_nodes = []
        for n in self.state.get_all_nodes(self.project_id):
            if n.get("type") != "bgm_fetch":
                continue
            res = n.get("result") or {}
            if not isinstance(res, dict) or res.get("error"):
                continue
            local = res.get("local_path") or ""
            if not local:
                continue
            bgm_nodes.append(n)

        if not bgm_nodes:
            return None

        # 按 created_at 字符串排序（ISO8601 字典序 == 时间序）；缺字段排末位
        bgm_nodes.sort(key=lambda n: n.get("created_at") or "", reverse=True)
        latest = bgm_nodes[0]

        # 从该 bgm 依赖的 audio_plan 节点拿 segments
        segments = []
        for dep_id in (latest.get("depends_on") or []):
            dep = self.state.get_node(self.project_id, dep_id) or {}
            if dep.get("type") == "audio_plan":
                segments = (dep.get("result") or {}).get("segments") or []
                break

        return {
            "local_path": (latest.get("result") or {}).get("local_path"),
            "audio_segments": segments,
        }

    async def _compose_episode_bgm(self) -> Optional[Dict[str, Any]]:
        """0.7.2 ②：把 episode_audio_plan 的场景时间窗按 track 拼成**一条整片 BGM 轨**（mp3），
        下游 video_merge 继续走单 bgm_path 链路（ducking 等零改动）。
        track 来源：本项目 bgm_fetch（params.track_key，取最新）→ series BGM 库兜底。
        没有计划 / 任一窗缺 track / ffmpeg 失败 → 返回 None 回退单段逻辑。"""
        plans = [
            n for n in self.state.get_all_nodes(self.project_id)
            if n.get("type") == "episode_audio_plan" and (n.get("result") or {}).get("scene_bgm_plan")
        ]
        if not plans:
            return None
        plans.sort(key=lambda n: n.get("created_at") or "", reverse=True)
        plan = (plans[0].get("result") or {}).get("scene_bgm_plan") or []
        if not plan:
            return None

        # track_key → 本地文件：本项目 bgm_fetch 优先（后写覆盖=最新），series 库兜底
        key_to_path: Dict[str, str] = {}
        bgms = [n for n in self.state.get_all_nodes(self.project_id) if n.get("type") == "bgm_fetch"]
        bgms.sort(key=lambda n: n.get("created_at") or "")
        for n in bgms:
            k = ((n.get("params") or {}).get("track_key") or "").strip()
            p = ((n.get("result") or {}).get("local_path") or "")
            if k and p and os.path.exists(p):
                key_to_path[k] = p
        missing = {seg.get("track_key") for seg in plan} - set(key_to_path)
        if missing:
            try:
                series_id = await self._get_series_id_for_project()
                if series_id:
                    series = await self._load_series(series_id) or {}
                    lib = (series.get("script_summary") or {}).get("bgm_library") or {}
                    for k in list(missing):
                        p = (lib.get(k) or {}).get("local_path") or ""
                        if p and os.path.exists(p):
                            key_to_path[k] = p
            except Exception:
                pass
        missing = {seg.get("track_key") for seg in plan} - set(key_to_path)
        if missing:
            logger.warning("episode BGM 合成跳过：缺 track %s（回退单段 BGM 逻辑）", missing)
            return None

        # ffmpeg 拼接：每窗 aloop 兜短 + atrim 到窗长 + 首尾 0.4s 淡入淡出，concat 保持精确时长
        # （不用 acrossfade —— 它会让总长漂移 (n-1)*d，场景窗就对不上画面了）
        out_dir = os.path.dirname(next(iter(key_to_path.values())))
        out_path = os.path.join(out_dir, f"bgm_ep_{uuid.uuid4().hex[:8]}.mp3")
        files = list(dict.fromkeys(key_to_path[seg["track_key"]] for seg in plan))
        f_idx = {f: i for i, f in enumerate(files)}
        parts: List[str] = []
        labels: List[str] = []
        for i, seg in enumerate(plan):
            seg_len = max(1, int((seg.get("t1") or 0) - (seg.get("t0") or 0)))
            src = f_idx[key_to_path[seg["track_key"]]]
            fade_out_st = max(0.0, seg_len - 0.4)
            parts.append(
                f"[{src}:a]aloop=loop=-1:size=2147483647,atrim=0:{seg_len},asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d=0.4,afade=t=out:st={fade_out_st}:d=0.4[s{i}]"
            )
            labels.append(f"[s{i}]")
        filter_complex = ";".join(parts) + f";{''.join(labels)}concat=n={len(plan)}:v=0:a=1[out]"
        cmd = ["ffmpeg", "-y"]
        for f in files:
            cmd += ["-i", f]
        cmd += ["-filter_complex", filter_complex, "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", out_path]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0 or not os.path.exists(out_path):
                logger.warning("episode BGM 合成 ffmpeg 失败（回退单段）：%s", (err or b"")[-300:])
                return None
        except Exception as e:
            logger.warning("episode BGM 合成异常（回退单段）：%s", e)
            return None
        logger.info("episode BGM 合成轨：%d 窗 / %d track → %s", len(plan), len(files), out_path)
        return {
            "local_path": out_path,
            "audio_segments": [
                {"t0": s.get("t0"), "t1": s.get("t1"), "mood": s.get("mood"), "track_key": s.get("track_key")}
                for s in plan
            ],
        }

    # ─── 发布相关 ───

    # director 记的平台字符串 → PublishEditor 下拉里能识别的 value
    _PUBLISH_PLATFORM_ALIAS = {
        "youtube": "youtube",
        "YouTube": "youtube",
        "油管": "youtube",
        "tiktok": "tiktok",
        "TikTok": "tiktok",
        "微信视频号": "视频号",
    }

    def _resolve_publish_platform(self) -> str:
        """从项目 collected_info.platform 推 PublishEditor 默认值；映射 YouTube→youtube 等。"""
        try:
            project = self.state.get_project(self.project_id) or {}
        except Exception:
            project = {}
        raw = ((project.get("collected_info") or {}).get("platform") or "").strip()
        if not raw:
            return "抖音"
        return self._PUBLISH_PLATFORM_ALIAS.get(raw, raw)

    async def _run_localization(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        """0.7.7 本地化：把合成成片经蚂蚁 videotranslate 翻译成多语言。失败不阻断（result 标 error）。"""
        # 0.8 缝4：能力门控。本方法的既有约定是"失败不阻断，result 标 error"，
        # 所以无权时也返回 error result，而不是抛异常打断整条链。
        from app.core.entitlements import check, deny_reason, Capability
        if not check(Capability.LOCALIZATION):
            return ({"status": "error", "message": deny_reason(Capability.LOCALIZATION),
                     "translations": []}, {})
        from app.core.runtime_config import video_translate
        vt = video_translate()
        video_url = params.get("merged_video_url") or params.get("video_url")
        if not video_url:
            return ({"status": "error", "message": "没有可翻译的合成片", "translations": []}, {})
        langs = params.get("target_languages") or vt.target_languages
        ttype = (params.get("translate_type") or vt.translate_type or "VOICE").upper()
        await self._progress(node["id"], f"🌐 视频翻译启动（{langs} · {ttype}）...")
        try:
            from app.modules._antchain_video_translate import translate_and_wait
            results = await translate_and_wait(
                video_url, target_languages=langs, translate_type=ttype,
                source_language=vt.source_language,
                erase_subtitle=vt.erase_subtitle, embed_subtitle=vt.embed_subtitle,
                video_name=f"octopus_{str(self.project_id)[:8]}",
                progress_cb=lambda m: self._progress(node["id"], m),
            )
        except Exception as e:
            logger.warning("视频翻译失败: %s", e)
            return ({"status": "error", "message": f"视频翻译失败：{e}", "translations": []}, {})
        ok = [r for r in results if r.get("video_url")]
        # 结算：翻译按 (合成片时长 × 成功语言数) 秒数计量 → 塞进 extra_outputs["usage"]，
        # _capture_usage_to_cost_log 会自动落一笔账（phase=localization）；pricing 用 videotranslate 占位价（标"估算"）。
        merge_dur = 0.0
        for _n in self.state.get_all_nodes(self.project_id) or []:
            if _n.get("type") == "video_merge":
                merge_dur = float((_n.get("result") or {}).get("duration") or 0) or merge_dur
        extra: Dict[str, Any] = {"localization": results}
        if ok and merge_dur > 0:
            extra["usage"] = {
                "provider": "antchain",
                "model": f"videotranslate-{ttype.lower()}",
                "kind": "video",
                "seconds": round(merge_dur * len(ok), 1),
            }
        return (
            {"status": "completed", "translate_type": ttype, "translations": results,
             "message": f"本地化完成：{len(ok)}/{len(results)} 语言成功"},
            extra,
        )

    async def run_localization(self, node_id: str, target_languages, translate_type: str = "VOICE") -> Dict[str, Any]:
        """手动触发本地化：写入用户选的语言/类型 → 执行该节点。"""
        node = self.state.get_node(self.project_id, node_id) or {}
        params = dict(node.get("params") or {})
        params["target_languages"] = target_languages or params.get("target_languages") or ["en"]
        params["translate_type"] = (translate_type or "VOICE").upper()
        self.state.update_node(self.project_id, node_id, {"params": params})
        asyncio.create_task(self._execute_node(node_id))
        return {"type": "localization_started", "node_id": node_id,
                "target_languages": params["target_languages"], "translate_type": params["translate_type"]}

    async def _after_merge_confirm(self, merge_node_id: str) -> Dict[str, Any]:
        """合并节点确认后，创建 publish 节点并跑 publisher agent。"""
        merge_node = self.state.get_node(self.project_id, merge_node_id) or {}
        merge_result = merge_node.get("result") or {}
        merged_video_url = merge_result.get("video_url")

        self.state.set_project_phase(self.project_id, ProjectPhase.PUBLISH)

        # 收集封面候选：所有关键帧的图 + 各 video 节点的 first_frame_url
        cover_candidates = self._collect_cover_candidates()

        merge_pos = merge_node.get("position", {"x": 2000, "y": 300})

        # 0.7.7 本地化（可选，手动触发）：合成后、发布结算前建一个视频翻译节点。默认 IDLE 不自动跑，用户选语言后触发。
        try:
            from app.core.runtime_config import video_translate as _vt_cfg
            _vt = _vt_cfg()
            _loc_langs = [x.strip() for x in (_vt.target_languages or "en").split(",") if x.strip()] or ["en"]
            loc_node = self._create_phase_node(
                phase="localization",
                node_template_id="node_localization",
                depends_on=[merge_node_id],
                position={"x": merge_pos["x"] + 350, "y": merge_pos["y"] + 240},
                params={
                    "merged_video_url": merged_video_url,
                    "target_languages": _loc_langs,
                    "translate_type": _vt.translate_type,
                },
                extra={"status": NodeStatus.IDLE},
            )
            await self._notify("new_node", {
                "message": "🌐 可选：把成片翻译成其它语言（在本地化节点选语言后点「本地化」）",
                "node": loc_node,
            })
        except Exception as _le:
            logger.warning("创建本地化节点失败（跳过，不影响发布）：%s", _le)

        publish_node = self._create_phase_node(
            phase="publish",
            node_template_id="node_publish",
            depends_on=[merge_node_id],
            position={"x": merge_pos["x"] + 350, "y": merge_pos["y"]},
            params={
                "merged_video_url": merged_video_url,
                "cover_candidates": cover_candidates,
                # 用户在 PublishEditor 编辑后会把这些值落到 params
                "title": "",
                "description": "",
                "tags": [],
                "selected_cover": None,
                "platform": self._resolve_publish_platform(),
            },
        )

        await self._notify("new_node", {
            "message": "🎬 视频合并完成！正在生成发布物料...",
            "node": publish_node,
        })

        asyncio.create_task(self._execute_node(publish_node["id"]))

        return {
            "type": "new_node",
            "node": publish_node,
            "phase": "publish",
        }

    def _collect_cover_candidates(self) -> List[Dict[str, Any]]:
        """从已生成的节点里收集封面候选帧。

        来源：
        - 关键帧节点（keyframe_generation）的 result.image_url
        - 视频节点 params 里的 first_frame_url（不重复添加同一张图）
        """
        candidates: List[Dict[str, Any]] = []
        seen_urls = set()

        for n in self.state.get_all_nodes(self.project_id):
            if n.get("type") == "keyframe_generation":
                r = n.get("result") or {}
                url = r.get("image_url")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    shot = (n.get("params") or {}).get("shot") or {}
                    candidates.append({
                        "idx": len(candidates),
                        "source": "keyframe",
                        "image_url": url,
                        "shot_id": shot.get("shot_id", ""),
                        "prompt": (n.get("params") or {}).get("prompt", "")[:200],
                        "node_id": n["id"],
                    })

        for n in self.state.get_all_nodes(self.project_id):
            if n.get("type") == "video_generation":
                params = n.get("params") or {}
                url = params.get("first_frame_url")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    shot = params.get("shot") or {}
                    candidates.append({
                        "idx": len(candidates),
                        "source": "video_first_frame",
                        "image_url": url,
                        "shot_id": shot.get("shot_id", ""),
                        "prompt": params.get("prompt", "")[:200],
                        "node_id": n["id"],
                    })

        return candidates

    async def _run_publish(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        """调 publisher_agent 生成发布元数据建议。

        实际"导出"在用户编辑确认后再触发（通过 PublishEditor 调 publish module）。
        这里只生成 agent 的建议，写到节点 result。
        """
        # 找剧本节点拿到原始 script
        script = {}
        style = ""
        for n in self.state.get_all_nodes(self.project_id):
            if n.get("type") == "script_generation":
                script = n.get("result") or {}
                style = script.get("style", "")
                break

        project = self.state.get_project(self.project_id) or {}
        collected = project.get("collected_info", {})

        await self._notify("node_status_changed", {
            "node_id": node["id"], "status": "running",
            "message": "✍️ 发布编辑 Agent 正在撰写标题与简介...",
        })
        await self._progress(node["id"], "✍️ 发布编辑 Agent 正在撰写标题、简介、标签...")

        agent_result = await self.publisher.execute({
            "script": script,
            "style": style or collected.get("style", ""),
            "platform": collected.get("platform", "抖音"),
            "audience": collected.get("audience", ""),
            "mood": collected.get("mood", ""),
            "cover_candidates": params.get("cover_candidates") or [],
        })

        # 标准化输出：把 error fallback 也展平
        if agent_result.get("error") and agent_result.get("fallback"):
            payload = {**agent_result["fallback"], "agent_error": agent_result.get("error")}
        else:
            payload = agent_result

        return ({
            "title_suggested": payload.get("title", ""),
            "description_suggested": payload.get("description", ""),
            "tags_suggested": payload.get("tags") or [],
            "cover_recommendations": payload.get("cover_recommendations") or [],
            "cover_prompt_en": payload.get("cover_prompt_en", ""),
            "cover_candidates": params.get("cover_candidates") or [],
            "merged_video_url": params.get("merged_video_url"),
            "agent_error": payload.get("agent_error"),
            # 这些字段等用户在 PublishEditor 编辑/导出时再填
            "exported": False,
            "export_path": None,
        }, {})

    def _format_publish_preview(self, result: Dict) -> str:
        title = (result or {}).get("title_suggested") or "（未生成）"
        return f"📢 发布物料草稿就绪：「{title[:20]}」"

    # ─── 0.6.5: 成本总结节点 ───

    async def _run_cost_summary(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        """读 project.cost_log → pricing.calc/summarize → 返回汇总结果"""
        from app.core.pricing import calc_entry, summarize
        project = self.state.get_project(self.project_id) or {}
        log = list(project.get("cost_log") or [])
        await self._progress(node["id"], f"💰 正在汇总 {len(log)} 条调用记录的成本...")
        enriched = [calc_entry(e) for e in log]
        result = summarize(enriched)
        logger.info(
            "cost_summary: %d entries, total ¥%.2f / $%.2f",
            len(enriched), result.get("total_cny", 0), result.get("total_usd", 0),
        )
        return result, {}

    def _format_cost_preview(self, result: Dict) -> str:
        total = (result or {}).get("total_cny", 0) or 0
        n = len((result or {}).get("entries") or [])
        return f"💰 累计 ¥{total:.2f}（{n} 次调用）"

    async def _spawn_cost_summary_after(self, publish_node_id: str) -> None:
        """publish 节点确认后自动追加 cost_summary 节点（已存在则跳过）。"""
        # 已有 cost_summary → 不重复创建
        for n in self.state.get_all_nodes(self.project_id):
            if n.get("type") == "cost_summary":
                logger.info("cost_summary 节点已存在 (%s)，跳过创建", n.get("id"))
                return

        publish_node = self.state.get_node(self.project_id, publish_node_id) or {}
        base_pos = publish_node.get("position", {"x": 1800, "y": 300})
        cost_node = self._create_phase_node(
            phase="cost",
            node_template_id="node_cost_summary",
            depends_on=[publish_node_id],
            position={"x": base_pos.get("x", 1800) + 300, "y": base_pos.get("y", 300)},
            params={},
        )
        await self._notify("new_node", {
            "message": "💰 正在统计本次项目成本...",
            "node": cost_node,
        })
        asyncio.create_task(self._execute_node(cost_node["id"]))

    async def publish_export(
        self,
        node_id: str,
        title: str,
        description: str,
        tags: List[str],
        cover_image_url: str,
        platform: str,
    ) -> Dict[str, Any]:
        """触发实际导出：调 publish module 写文件 + 把最终值落到节点 result"""
        node = self.state.get_node(self.project_id, node_id)
        if not node:
            return {"error": "publish 节点不存在"}

        result = node.get("result") or {}
        merged_video_url = result.get("merged_video_url")
        if not merged_video_url:
            return {"error": "找不到合并后的视频"}

        module = ModuleRegistry.get("publish")
        if not module:
            return {"error": "publish 模块未注册"}

        # 0.7.7：把本地化(翻译)产出的多语言版也一并纳入导出包（自动从 localization 节点取，无需前端传）
        translations = []
        for _n in self.state.get_all_nodes(self.project_id) or []:
            if _n.get("type") == "localization":
                for _t in ((_n.get("result") or {}).get("translations") or []):
                    if _t.get("video_url"):
                        translations.append({
                            "target_language": _t.get("target_language"),
                            "video_url": _t.get("video_url"),
                        })

        try:
            export_out = await module.run({
                "project_id": self.project_id,
                "merged_video_url": merged_video_url,
                "cover_image_url": cover_image_url,
                "title": title,
                "description": description,
                "tags": tags,
                "platform": platform,
                "translations": translations,
            })
        except Exception as e:
            logger.exception("publish export failed")
            return {"error": str(e)}

        new_result = {
            **result,
            "title": title,
            "description": description,
            "tags": tags,
            "selected_cover": cover_image_url,
            "platform": platform,
            "exported": True,
            "export_path": export_out.get("export_path"),
            "export_url": export_out.get("export_url"),
            "exported_files": export_out.get("files", {}),
            "exported_at": export_out.get("metadata", {}).get("exported_at"),
        }

        # 如果选了真实发布平台，本地导出完成后尝试 upload
        upload_result = None
        from app.publishers import get_publisher
        pub = get_publisher(platform)
        if pub is not None:
            export_path = export_out.get("export_path") or ""
            video_file = (export_out.get("files") or {}).get("video") or "video.mp4"
            video_abs = os.path.join(export_path, video_file) if export_path else ""
            if video_abs and os.path.exists(video_abs):
                up = await pub.upload(video_abs, {
                    "title": title,
                    "description": description,
                    "tags": tags,
                    "visibility": "private",  # 未通过 Google 审核的 OAuth app 上传默认 private
                })
                upload_result = {
                    "ok": up.ok,
                    "platform": up.platform,
                    "video_id": up.video_id,
                    "video_url": up.video_url,
                    "visibility": up.visibility,
                    "error": up.error,
                }
            else:
                upload_result = {"ok": False, "platform": platform, "error": "找不到导出的视频文件"}
            new_result["upload"] = upload_result

        self.state.update_node(self.project_id, node_id, {
            "result": new_result,
            "outputs": {"publish": new_result},
            "params": {
                **(node.get("params") or {}),
                "title": title,
                "description": description,
                "tags": tags,
                "selected_cover": cover_image_url,
                "platform": platform,
            },
        })

        await self._notify("publish_exported", {
            "node_id": node_id,
            "export_url": export_out.get("export_url"),
            "upload": upload_result,
            "message": f"✅ 已导出到 {export_out.get('export_url')}" + (
                f"\n📤 已上传到 {platform}：{upload_result.get('video_url') or upload_result.get('video_id') or '草稿箱'}"
                if upload_result and upload_result.get("ok") else
                (f"\n⚠ {platform} 上传失败：{upload_result.get('error')}" if upload_result else "")
            ),
        })
        return {
            "type": "publish_exported",
            "node_id": node_id,
            "export_url": export_out.get("export_url"),
            "files": export_out.get("files"),
            "upload": upload_result,
        }

    async def publish_generate_cover(
        self,
        node_id: str,
        prompt: str,
        reference_image_urls: List[str],
    ) -> Dict[str, Any]:
        """让 AI 重新生成一张封面（用 image_to_image 多图融合）

        参考图：用户挑选的关键帧 / 首帧，没传就空，由 prompt 主导。
        生成的图直接 append 到 publish 节点的 cover_candidates，并返回该 candidate。
        """
        node = self.state.get_node(self.project_id, node_id)
        if not node:
            return {"error": "publish 节点不存在"}

        await self._notify("node_status_changed", {
            "node_id": node_id, "status": "running",
            "message": "🎨 正在让 AI 生成新封面...",
        })

        cover_ratio = self._project_aspect_ratio()
        cv_w, cv_h = self._resolution_for_ratio(cover_ratio)
        # 用项目 ratio 描述给 LLM 看（横屏 → "16:9 landscape"，竖屏 → "9:16 portrait"，方屏 → "1:1 square"）
        ratio_hint = {
            "16:9": "16:9 landscape",
            "9:16": "9:16 portrait",
            "1:1":  "1:1 square",
            "4:3":  "4:3 landscape",
            "3:4":  "3:4 portrait",
        }.get(cover_ratio, "9:16 portrait")
        fallback_prompt = f"cinematic poster cover, eye-catching, {ratio_hint}, dramatic lighting"

        if reference_image_urls:
            module = ModuleRegistry.get("image_to_image")
            payload = {
                "image_urls": reference_image_urls,
                "prompt": prompt or fallback_prompt,
                "width": cv_w,
                "height": cv_h,
            }
        else:
            module = ModuleRegistry.get("text_to_image")
            payload = {
                "prompt": prompt or fallback_prompt,
                "width": cv_w,
                "height": cv_h,
                "style": "cinematic",
            }

        if not module:
            return {"error": "image 模块未注册"}

        try:
            img = await module.run(payload)
        except Exception as e:
            logger.exception("AI 生成封面失败")
            self.state.set_node_status(self.project_id, node_id, NodeStatus.COMPLETED)
            return {"error": str(e)}

        # 把新封面追加到 cover_candidates
        result = dict(node.get("result") or {})
        candidates = list(result.get("cover_candidates") or [])
        new_cand = {
            "idx": len(candidates),
            "source": "ai_generated",
            "image_url": img.get("image_url"),
            "shot_id": "",
            "prompt": prompt[:200],
            "mock": img.get("mock", False),
        }
        candidates.append(new_cand)
        result["cover_candidates"] = candidates
        # 也同步到 params 让前端看到
        new_params = dict(node.get("params") or {})
        new_params["cover_candidates"] = candidates
        self.state.update_node(self.project_id, node_id, {
            "result": result,
            "params": new_params,
        })

        # publish 节点是个长存节点；这里不要因为短任务把 status 改 running 后留着
        self.state.set_node_status(self.project_id, node_id, NodeStatus.COMPLETED)

        await self._notify("cover_generated", {
            "node_id": node_id,
            "cover": new_cand,
            "message": "🎨 新封面已生成",
        })
        return {"type": "cover_generated", "cover": new_cand}

    # ═══════════════════════════════════════════════════════
    # 后处理钩子
    # ═══════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════
    # 0.7.0 轮 1：Episode art 后处理 + 子节点 spawn
    # ═══════════════════════════════════════════════════════

    async def _post_episode_art(self, node_id: str, art_data: Dict) -> None:
        """episode_art_design 跑完后只保存数据，等用户 confirm 主节点再 spawn 子节点。
        与 master art 流程对齐 —— 用户先 review LLM 输出的"骨架"（出场角色/装束/场景变化/道具状态），
        满意了再 confirm 触发昂贵的 img2img 出图。
        """
        outputs = {"art_direction": art_data}
        self.state.update_node(self.project_id, node_id, {"outputs": outputs})
        # 不再自动 spawn — 等 _after_episode_art_main_confirm 触发

    async def _spawn_episode_art_children(self, node_id: str) -> None:
        """根据 episode_art_design 节点 result（含 characters_in_episode/scenes/props）
        spawn 子节点：每件 outfit / 每个 scene_variation / 每个 prop_state 一个。
        子节点出图时优先用 master 元素图作 reference（img2img）。
        """
        node = self.state.get_node(self.project_id, node_id) or {}
        # 0.7.1 专家模式：本集美术子图也不自动生成 —— 建成 IDLE，等用户「开始生成本集美术」批量触发。
        _proj = self.state.get_project(self.project_id) or {}
        _cfg = _proj.get("config") or {}
        is_expert = bool(_cfg.get("expert_seeded") or _cfg.get("series_id") or _proj.get("series_id"))
        art_data = node.get("result") or {}
        parent_pos = node.get("position", {"x": 400, "y": 200})
        params = node.get("params") or {}
        master = params.get("master_shared_assets") or {}

        # 建立 master id → 图 url 反查表
        master_chars = {(c.get("character_id") or c.get("name")): c for c in (master.get("characters") or [])}
        master_scenes = {(s.get("scene_id") or s.get("location")): s for s in (master.get("scenes") or [])}
        master_props = {(p.get("prop_id") or p.get("name")): p for p in (master.get("props") or [])}

        # 1) 收集 items —— 一个 outfit 一项
        items: List[Dict[str, Any]] = []
        for c in (art_data.get("characters_in_episode") or []):
            master_c = master_chars.get(c.get("master_character_id"), {})
            master_img = master_c.get("image_url")
            char_name = c.get("name") or master_c.get("name") or "角色"
            char_subject_type = (master_c.get("subject_type") or "human").strip().lower()
            # 0.7.3：master 角色外形(人种/年龄/长相)——分集 img2img reference + 降级 t2i 都拼上，保身份不漂欧美女
            _master_look = (master_c.get("appearance") or master_c.get("prompt")
                            or master_c.get("description") or c.get("appearance_notes") or "")
            _outfits = c.get("outfits") or []
            _img_prompt = c.get("image_prompt") or ""
            # 0.7.3：新角色(master 没有)常常没 outfits → 用 image_prompt 建 1 个默认装束，别漏角色
            if not _outfits:
                _outfits = [{"outfit_id": f"{(c.get('master_character_id') or char_name)}_O01",
                             "name": "默认装束",
                             "outfit_prompt": _img_prompt or c.get("appearance_notes") or ""}]
            for o in _outfits:
                items.append({
                    "kind": "ep_character",
                    "label": f"🎭 {char_name} · {o.get('name') or '装束'}",
                    "data": {
                        "result_type": "ep_character",
                        "master_character_id": c.get("master_character_id"),
                        "character_name": char_name,
                        "name": f"{char_name} · {o.get('name') or ''}",
                        "outfit_id": o.get("outfit_id"),
                        "outfit_name": o.get("name"),
                        "outfit_when": o.get("when"),
                        "outfit_prompt": o.get("outfit_prompt"),
                        "appearance_notes": c.get("appearance_notes"),
                        "image_prompt": _img_prompt,  # 0.7.3 新角色完整外形(master 没有→出图走 t2i)
                        "master_appearance": _master_look,  # 0.7.3 master 外形→img2img+降级t2i 都拼上，审核降级也保身份
                        "subject_type": char_subject_type,
                        "negative_prompt": o.get("negative_prompt") or "其他场景，多个角色，配饰大变",
                        "reference_image_url": master_img,
                        "parent_master": {
                            "id": c.get("master_character_id"),
                            "name": master_c.get("name"),
                            "image_url": master_img,
                            # 0.7.0 轮 2：透传 master 的 face/3-view，让 outfit composite 拼接用
                            "face_close_up_url": master_c.get("face_close_up_url"),
                            "three_view_image_url": master_c.get("three_view_image_url"),
                            "composite_reference_url": master_c.get("composite_reference_url"),
                        },
                    },
                })

        for s in (art_data.get("scenes_in_episode") or []):
            master_s = master_scenes.get(s.get("master_scene_id"), {}) if s.get("master_scene_id") else {}
            master_img = master_s.get("image_url")
            items.append({
                "kind": "ep_scene",
                "label": f"🏞️ {s.get('location') or '场景'} · {s.get('variation_name') or '变化'}",
                "data": {
                    "result_type": "ep_scene",
                    "master_scene_id": s.get("master_scene_id"),
                    "location": s.get("location"),
                    "variation_id": s.get("variation_id"),
                    "variation_name": s.get("variation_name"),
                    "time_of_day": s.get("time_of_day"),
                    "lighting": s.get("lighting"),
                    "camera_angle": s.get("camera_angle"),
                    "image_prompt": s.get("image_prompt"),
                    "camera_setups": s.get("camera_setups") or [],  # 0.7.5 机位板 spec
                    "negative_prompt": s.get("negative_prompt") or "人，人物，角色，动物",
                    "reference_image_url": master_img,
                    "parent_master": {
                        "id": s.get("master_scene_id"),
                        "location": master_s.get("location"),
                        "image_url": master_img,
                    },
                },
            })

        for p in (art_data.get("props_in_episode") or []):
            master_p = master_props.get(p.get("master_prop_id"), {}) if p.get("master_prop_id") else {}
            master_img = master_p.get("image_url")
            items.append({
                "kind": "ep_prop",
                "label": f"🎁 {p.get('name') or '道具'} · {p.get('state_name') or '状态'}",
                "data": {
                    "result_type": "ep_prop",
                    "master_prop_id": p.get("master_prop_id"),
                    "name": p.get("name"),
                    "state_id": p.get("state_id"),
                    "state_name": p.get("state_name"),
                    "state_description": p.get("state_description"),
                    "image_prompt": p.get("image_prompt"),
                    "negative_prompt": p.get("negative_prompt") or "人，场景，其他道具",
                    "reference_image_url": master_img,
                    "parent_master": {
                        "id": p.get("master_prop_id"),
                        "name": master_p.get("name"),
                        "image_url": master_img,
                    },
                },
            })

        # 2) 同步建占位子节点 → 推前端
        children: List[Dict[str, Any]] = []
        for i, it in enumerate(items):
            child = self.state.create_node(self.project_id, {
                "node_id": f"{node_id}_ep_{i}",
                "type": "result",
                "label": it["label"],
                "position": {
                    "x": parent_pos["x"] + 300,
                    "y": parent_pos["y"] + i * 110 - len(items) * 55,
                },
                "params": it["data"],
                "depends_on": [node_id],
                "parent_result_id": node_id,
                "result_index": i,
                "agent": "art_director",
                "need_confirm": True,
                "phase": "episode_art",
                "status": NodeStatus.IDLE if is_expert else NodeStatus.RUNNING,
                "result": it["data"],
            })
            await self._notify("new_node", {
                "message": (f"🖼️ {it['label']} 待生成（点「开始生成本集美术」触发）"
                            if is_expert else f"🖼️ {it['label']} 生成中（在总美术基础上细化）..."),
                "node": child,
            })
            children.append({"child": child, "data": it["data"], "kind": it["kind"]})

        # 3) 异步并发出图（共享 sem=2 同 master art）
        shared_sem = asyncio.Semaphore(2)
        async def _fill(entry):
            async with shared_sem:
                await self._fill_episode_subject_image(entry)
        if not is_expert:
            # 专家模式不自动跑 —— 子图建成 IDLE，留给「开始生成本集美术」批量触发
            for entry in children:
                asyncio.create_task(_fill(entry))

        # 4) 更新主节点 children + outputs
        result_children = [c["child"]["id"] for c in children]
        self.state.update_node(self.project_id, node_id, {
            "outputs": {
                **(node.get("outputs") or {}),
                "result_children": result_children,
                "ep_characters_count": sum(1 for c in children if c["kind"] == "ep_character"),
                "ep_scenes_count": sum(1 for c in children if c["kind"] == "ep_scene"),
                "ep_props_count": sum(1 for c in children if c["kind"] == "ep_prop"),
            },
            "children": result_children,
        })
        logger.info(
            "episode_art_design %s spawn %d 子节点（%d outfits + %d scene_variants + %d prop_states）",
            node_id, len(children),
            sum(1 for c in children if c["kind"] == "ep_character"),
            sum(1 for c in children if c["kind"] == "ep_scene"),
            sum(1 for c in children if c["kind"] == "ep_prop"),
        )

    # ═══════════════════════════════════════════════════════
    # 0.7.2：资产管理面板 —— 新增服装 / 道具状态 / 道具
    # 统一建 IDLE 子节点挂对应 art 主节点；出图由面板/画布「重跑」触发
    # （ep_* 走 _retry_episode_child img2img；master prop 走 _retry_result_child）。
    # 删除/单套重生不在此：复用 delete-node(cascade) / node-action rerun。
    # ═══════════════════════════════════════════════════════

    def _latest_node_of_type(self, node_type: str) -> Optional[Dict[str, Any]]:
        nodes = [n for n in self.state.get_all_nodes(self.project_id) if n.get("type") == node_type]
        return nodes[-1] if nodes else None

    async def create_asset(self, action: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """create_outfit（分集新服装）/ create_prop_state（分集道具新状态）/ create_prop（总项目/独立项目新道具）。
        子节点字段逐项对齐 _spawn_episode_art_children / _generate_subject_images 的结构，
        node_id 用 uuid 后缀（spawn 用批内索引命名，二次创建会撞 id）。"""
        fields = fields or {}
        if action in ("create_outfit", "create_prop_state"):
            art_node = self._latest_node_of_type("episode_art_design")
            if not art_node:
                return {"error": "当前项目没有本集美术节点（episode_art_design），无法新增分集资产"}
            master = (art_node.get("params") or {}).get("master_shared_assets") or {}
            if action == "create_outfit":
                mc_id = (fields.get("master_character_id") or "").strip()
                master_c = next((c for c in (master.get("characters") or [])
                                 if (c.get("character_id") or c.get("name")) == mc_id), {})
                # 兄弟 outfit 兜底：master_shared_assets 缺失时从同角色已有 outfit 节点抄 parent_master
                sibling: Dict[str, Any] = {}
                for cid in (art_node.get("children") or []):
                    ch = self.state.get_node(self.project_id, cid) or {}
                    r = ch.get("result") or {}
                    if r.get("result_type") == "ep_character" and r.get("master_character_id") == mc_id:
                        sibling = r
                        break
                if not master_c and sibling:
                    master_c = dict(sibling.get("parent_master") or {})
                char_name = (fields.get("character_name") or master_c.get("name")
                             or sibling.get("character_name") or "角色")
                outfit_name = (fields.get("outfit_name") or "新服装").strip()
                data = {
                    "result_type": "ep_character",
                    "master_character_id": mc_id,
                    "character_name": char_name,
                    "name": f"{char_name} · {outfit_name}",
                    "outfit_id": f"O_x{uuid.uuid4().hex[:4]}",
                    "outfit_name": outfit_name,
                    "outfit_when": fields.get("outfit_when") or "",
                    "outfit_prompt": fields.get("outfit_prompt") or "",
                    "appearance_notes": sibling.get("appearance_notes") or "",
                    "subject_type": (sibling.get("subject_type")
                                     or (master_c.get("subject_type") or "human")).strip().lower(),
                    "negative_prompt": "其他场景，多个角色，配饰大变",
                    "reference_image_url": master_c.get("image_url"),
                    "parent_master": {
                        "id": mc_id,
                        "name": master_c.get("name") or char_name,
                        "image_url": master_c.get("image_url"),
                        "face_close_up_url": master_c.get("face_close_up_url"),
                        "three_view_image_url": master_c.get("three_view_image_url"),
                        "composite_reference_url": master_c.get("composite_reference_url"),
                    },
                }
                label = f"🎭 {char_name} · {outfit_name}"
            else:  # create_prop_state
                mp_id = (fields.get("master_prop_id") or "").strip() or None
                master_p = next((p for p in (master.get("props") or [])
                                 if (p.get("prop_id") or p.get("name")) == mp_id), {})
                name = fields.get("name") or master_p.get("name") or "道具"
                state_name = (fields.get("state_name") or "新状态").strip()
                data = {
                    "result_type": "ep_prop",
                    "master_prop_id": mp_id,
                    "name": name,
                    "state_id": f"S_x{uuid.uuid4().hex[:4]}",
                    "state_name": state_name,
                    "state_description": fields.get("state_description") or "",
                    "image_prompt": fields.get("image_prompt") or fields.get("prompt") or "",
                    "negative_prompt": "人，场景，其他道具",
                    "reference_image_url": master_p.get("image_url"),
                    "parent_master": {"id": mp_id, "name": master_p.get("name"),
                                      "image_url": master_p.get("image_url")},
                }
                label = f"🎁 {name} · {state_name}"
            phase = "episode_art"
        elif action == "create_prop":
            art_node = self._latest_node_of_type("art_design")
            if not art_node:
                return {"error": "当前项目没有美术节点（art_design），无法新增道具"}
            name = (fields.get("name") or "新道具").strip()
            data = {
                "result_type": "prop",
                "prop_id": f"P_x{uuid.uuid4().hex[:4]}",
                "name": name,
                "prompt": fields.get("prompt") or "",
                "negative_prompt": "人，场景，其他道具",
                "description": fields.get("description") or "",
                "use_case": fields.get("use_case") or "",
            }
            label = f"🎁 {name}"
            phase = "art"
        else:
            return {"error": f"未知资产操作: {action}"}

        parent_id = art_node["id"]
        parent_pos = art_node.get("position", {"x": 400, "y": 200})
        siblings = list(art_node.get("children") or [])
        child = self.state.create_node(self.project_id, {
            "node_id": f"{parent_id}_x{uuid.uuid4().hex[:6]}",
            "type": "result",
            "label": label,
            "position": {"x": parent_pos["x"] + 300, "y": parent_pos["y"] + len(siblings) * 110 + 80},
            "params": data,
            "depends_on": [parent_id],
            "parent_result_id": parent_id,
            "result_index": len(siblings),
            "agent": "art_director",
            "need_confirm": True,
            "phase": phase,
            "status": NodeStatus.IDLE,
            "result": data,
        })
        prev_outputs = art_node.get("outputs") or {}
        self.state.update_node(self.project_id, parent_id, {
            "children": siblings + [child["id"]],
            "outputs": {
                **prev_outputs,
                "result_children": list(prev_outputs.get("result_children") or siblings) + [child["id"]],
            },
        })
        await self._notify("new_node", {
            "message": f"🆕 {label} 已创建（待生成，在资产面板或画布点「生成」出图）",
            "node": child,
        })
        return {"type": "asset_created", "node_id": child["id"], "label": label}

    async def _fill_episode_subject_image(self, entry: Dict[str, Any]) -> None:
        """给一个 episode 子节点出图。优先 img2img（用 master 图当 reference）；master 图缺失走 t2i。"""
        child = entry["child"]
        data = dict(entry["data"])
        kind = entry["kind"]
        child_id = child["id"]
        ref_url = data.get("reference_image_url")

        # 构造 prompt（区分 outfit / scene_variant / prop_state）
        # 0.7.1：重跑时优先用编辑器改过的 prompt（result.prompt）；首次生成 prompt 还没回填 → 用 outfit/image_prompt 默认
        edited_prompt = (data.get("prompt") or "").strip()
        if kind == "ep_character":
            _mlook = data.get("master_appearance") or ""
            if not ref_url and data.get("image_prompt"):
                # 新角色(master 没有)：用 image_prompt(含脸/体型/发型) + outfit，保证 t2i 有完整外形
                default_prompt = ", ".join(filter(None, [data.get("image_prompt") or "", data.get("outfit_prompt") or ""]))
            else:
                # master 角色换装：拼 master 外形(_mlook 人种/年龄/长相) + 衣服 + 情绪。
                # 关键(根因①)：img2img reference 保脸；一旦被真人审核降级 t2i，_mlook 也保住身份不漂成欧美女。
                default_prompt = ", ".join(filter(None, [_mlook, data.get("outfit_prompt") or "", data.get("appearance_notes") or ""]))
            full_prompt = edited_prompt or default_prompt
            fortified = self._fortify_prompt(full_prompt, self.CHARACTER_BG_SUFFIX)
            img_w, img_h = 1024, 1024
            kind_label = "🎭 本集角色"
        elif kind == "ep_scene":
            full_prompt = edited_prompt or (data.get("image_prompt") or "")
            fortified = self._fortify_prompt(full_prompt, self.SCENE_BG_SUFFIX)
            img_w, img_h = 1344, 768
            kind_label = "🏞️ 本集场景"
        else:  # ep_prop
            full_prompt = edited_prompt or (data.get("image_prompt") or "")
            fortified = self._fortify_prompt(full_prompt, self.PROP_BG_SUFFIX)
            img_w, img_h = 1024, 1024
            kind_label = "🎁 本集道具"

        await self._progress(child_id, f"🖼️ 生成 {kind_label}「{data.get('name') or data.get('location') or ''}」...")

        try:
            img: Dict[str, Any] = {}
            # 优先 img2img（保身材/造型/材质一致），失败降级 t2i
            if ref_url:
                try:
                    img = await self._generate_image_from_image(
                        ref_url, fortified, width=img_w, height=img_h,
                    )
                    data["generated_from_reference"] = True
                except Exception as e:
                    logger.warning("episode 子节点 img2img 失败，降级 t2i: %s", e)
                    img = {}
            if not img.get("image_url") or img.get("mock"):
                if ref_url:
                    logger.warning("episode 子节点 %s img2img 无效(mock=%s)，降级纯文生图 → 会丢 master 一致性",
                                   child_id, img.get("mock"))
                else:
                    logger.warning("episode 子节点 %s 无 reference_image_url，走纯文生图（无 master 参考）", child_id)
                img = await self._generate_image(fortified, width=img_w, height=img_h)
                data["generated_from_reference"] = False

            data["image_url"] = img.get("image_url")
            data["mock"] = img.get("mock", False)
            data["fortified_prompt"] = fortified
            # 0.7.0：编辑器"图像生成提示词"读 result.prompt；ep_* 之前只存 outfit_prompt/image_prompt → 显示空。
            # 统一回填 prompt（出图实际用的 pre-fortify 文案），编辑器可见、改后重跑也用它。
            if not data.get("prompt"):
                data["prompt"] = full_prompt
            self._capture_usage_to_cost_log(child_id, "result", "art", img, None)

            # 0.7.5 场景机位板：本集场景建立镜出完后按 camera_setups 生成额外机位板
            if kind == "ep_scene":
                data["cameras"] = await self._generate_camera_boards(
                    child_id, data.get("image_url"), img.get("origin_url"),
                    full_prompt, data.get("camera_setups") or [], img_w, img_h,
                )

            # 0.7.0：ep_character outfit 也生成三视图（与 master character 一致）
            # ep_scene / ep_prop 不需要三视图
            if kind == "ep_character" and data.get("image_url"):
                await self._progress(child_id, f"🖼️ 生成 {kind_label}「{data.get('name')}」三视图...")
                tv_base = ", ".join(filter(None, [
                    data.get("outfit_prompt") or "",
                    data.get("appearance_notes") or "",
                ]))
                three_view_prompt = self._build_three_view_prompt(tv_base)
                tv_img: Dict[str, Any] = {}
                # 优先用刚生成的 outfit 正面图当 reference 做 img2img 三视图（保 outfit 一致）
                try:
                    tv_img = await self._generate_image_from_image(
                        data["image_url"], self.THREE_VIEW_IMG2IMG_PROMPT,
                        width=1344, height=768,
                    )
                    data["three_view_from_reference"] = True
                except Exception as e:
                    logger.warning("episode outfit 三视图 img2img 失败，降级 t2i: %s", e)
                    tv_img = {}
                if not tv_img.get("image_url") or tv_img.get("mock"):
                    try:
                        tv_img = await self._generate_image(three_view_prompt, width=1344, height=768)
                        data["three_view_from_reference"] = False
                    except Exception as e:
                        logger.warning("episode outfit 三视图 t2i 也失败: %s", e)
                        data["three_view_error"] = str(e)
                        tv_img = {}
                if tv_img.get("image_url"):
                    data["three_view_image_url"] = tv_img.get("image_url")
                    data["three_view_prompt"] = three_view_prompt
                    data["three_view_mock"] = tv_img.get("mock", False)
                    # 0.7.3：保留三视图的火山 Seedream 原始 URL（asset 入库图源，火山可访问）
                    if tv_img.get("origin_url"):
                        data["three_view_origin_url"] = tv_img.get("origin_url")
                self._capture_usage_to_cost_log(child_id, "result", "art", tv_img, None)

                # 0.7.0 轮 2：拼接 composite — 左侧用 master 角色的 face_close_up（保脸一致），
                # 右侧用本 outfit 的三视图（保身材+衣服+360°）
                from app.core.paths import compose_character_reference
                parent_master = data.get("parent_master") or {}
                # 0.7.3：脸源**实时**从 live series.shared_assets 取 master 正脸（parent_master 是建节点时冻结的旧快照→老图根因）；
                # **绝不 fallback data["image_url"]**（本套 outfit 换装图的脸，可能已被审核降级漂成欧美女）。
                _live_face = None
                try:
                    _sid = await self._get_series_id_for_project()
                    if _sid:
                        _shared = (await self._load_series(_sid) or {}).get("shared_assets") or {}
                        _key = data.get("master_character_id")
                        for _mc in (_shared.get("characters") or []):
                            if ((_mc.get("character_id") or _mc.get("name")) == _key
                                    or _mc.get("name") == data.get("character_name")):
                                _live_face = _mc.get("face_close_up_url") or _mc.get("image_url")
                                break
                except Exception as _fe:
                    logger.warning("composite 取 live master 脸失败: %s", _fe)
                left_img = _live_face or parent_master.get("face_close_up_url") or parent_master.get("image_url")
                if left_img and data.get("three_view_image_url"):
                    composite_url = compose_character_reference(left_img, data["three_view_image_url"])
                    if composite_url:
                        data["composite_reference_url"] = composite_url
                        logger.info(
                            "ep_character %s outfit %s composite: %s",
                            data.get("character_name"), data.get("outfit_name"), composite_url,
                        )
        except Exception as e:
            logger.warning("episode 子节点出图失败: %s", e)
            data["image_url"] = None
            data["error"] = str(e)

        self.state.update_node(self.project_id, child_id, {
            "result": data,
            "params": data,
        })
        self.state.set_node_status(self.project_id, child_id, NodeStatus.COMPLETED)
        entry["data"] = data

    def _format_episode_art_preview(self, art_data: Dict) -> str:
        """episode_art_design 节点 result_preview 文本。"""
        if not isinstance(art_data, dict):
            return "本集美术细化完成"
        chars = art_data.get("characters_in_episode") or []
        scenes = art_data.get("scenes_in_episode") or []
        props = art_data.get("props_in_episode") or []
        outfits_total = sum(len(c.get("outfits") or []) for c in chars)
        parts = []
        if chars:
            parts.append(f"{len(chars)} 角色 / {outfits_total} 套装束")
        if scenes:
            parts.append(f"{len(scenes)} 场景变化")
        if props:
            parts.append(f"{len(props)} 道具状态")
        return "本集美术：" + " · ".join(parts) if parts else "本集美术细化完成"

    async def _post_art(self, node_id: str, art_data: Dict) -> None:
        """美术节点跑完后只保存 art_direction 数据，等用户确认主节点后再生成子节点（图像物料）。

        旧实现会立刻 fan-out 所有 character/scene 图，但用户不一定满意 art_direction
        本身（角色拆得对不对、场景列得全不全），先让人确认主节点才合理。
        子节点生成在 _spawn_art_children（由 _after_art_main_confirm 触发）。

        0.6.4：保存前先让总导演 review polish（不展示节点，notes 落 outputs）。
        """
        node = self.state.get_node(self.project_id, node_id) or {}
        script_ctx = (node.get("params") or {}).get("script") or {}
        review_notes = ""
        raw_before = art_data
        try:
            await self._progress(node_id, "💎 总导演正在 review 美术输出...")
            rev = await self.director.review_art(art_data, script_ctx)
            polished = rev.get("art_direction") or art_data
            review_notes = rev.get("review_notes") or ""
            if polished is not art_data:
                # 覆盖 node.result，下游 _spawn_art_children 读 result.characters/scenes
                self.state.update_node(self.project_id, node_id, {"result": polished})
                art_data = polished
        except Exception as e:
            logger.warning("post_art review_art 异常，降级原数据: %s", e)
            review_notes = f"(review 异常:{str(e)[:120]})"

        outputs = {
            "art_direction": art_data,
            "director_review_notes": review_notes,
        }
        # 调试期：notes 非"(未改动"/"(input invalid" 前缀时存 raw_before，方便对比 polish 前后
        if review_notes and not review_notes.startswith("(未改动") and not review_notes.startswith("(input"):
            outputs["raw_before_review"] = raw_before
        self.state.update_node(self.project_id, node_id, {"outputs": outputs})

    # 三视图后缀：front + side + back，纯色背景
    THREE_VIEW_SUFFIX = (
        " three view sheet, character turnaround: front view, side view, back view, "
        "evenly spaced, identical character, identical proportions, "
        "isolated on pure white background, studio lighting, no environment, "
        "no shadows on background, character design reference sheet"
    )

    # 图生图三视图 prompt：外观由参考图提供，这里只描述布局/姿态约束
    THREE_VIEW_IMG2IMG_PROMPT = (
        "Generate a character turnaround sheet of the SAME character shown in the reference: "
        "front view, side view, back view, evenly spaced in one wide horizontal image, "
        "identical character, identical proportions, identical clothing and colors, "
        "neutral T-pose or A-pose, isolated on pure white background, studio lighting, "
        "no environment, no shadows on background, character design reference sheet"
    )

    # 角色单图：放在 prompt 末尾，强约束白底无场景（中英双语，Zhipu 对中文响应更稳）
    CHARACTER_BG_SUFFIX = (
        ", 纯白色背景，无场景，无环境，无道具，工作室灯光，"
        "isolated on pure white background, studio lighting, no environment, "
        "centered composition, full body shot"
    )

    # 真人/类人角色去写实后缀：保留电影感，避免被 Seedance 识别为真人照片触发 PrivacyInformation
    HUMAN_DEPHOTO_SUFFIX = (
        ", stylized 3D character render, cinematic CG, soft painterly texture, "
        "non-photorealistic character"
    )

    # 0.6.4: 真人写实限制放开 —— 不再强剔关键词，先让 Zhipu/Seedance 实际审核率说话
    # 旧关键词列表保留为注释，回滚时填回 + 恢复下方循环即可：
    #   英: photorealistic / photo realistic / professional photography / 8k photo / real person / DSLR / shot on / candid photograph
    #   中: 写实照片级 / 真人照片 / 高清照片 / 8K 照片 / 专业摄影 / 单反相机 / 真实人物
    _PHOTO_REAL_KEYWORDS = ()

    @classmethod
    def _dephoto_human_prompt(cls, prompt: str) -> str:
        """0.6.4: 真人写实限制放开 —— 直接返回原 prompt。
        保留方法签名 + HUMAN_DEPHOTO_SUFFIX 常量不动，未来按需启用：
        把 _PHOTO_REAL_KEYWORDS 填回 + 恢复原循环 + 追加 suffix 即可。
        """
        return prompt

    # 场景图：约束无任何角色（中英双语）
    SCENE_BG_SUFFIX = (
        ", 空场景，无人物，无角色，无动物，仅环境与道具，"
        "no characters, no people, no animals, empty scene, "
        "environment only, wide angle establishing shot"
    )

    # 0.6.4: 道具图：单道具特写、纯白背景、无场景无人物（中英双语）
    PROP_BG_SUFFIX = (
        ", 纯白色背景，无场景，无人物，无角色，独立道具，工作室灯光，"
        "isolated on pure white background, studio lighting, no environment, "
        "no characters, no people, centered composition, product reference shot"
    )

    async def _generate_subject_images(
        self,
        parent_id: str,
        kind: str,                 # "character" | "scene" | "prop"
        items: List[Dict],
        parent_pos: Dict[str, int],
        offset: int,
        sem: Optional[asyncio.Semaphore] = None,  # 0.6.3+: 共享 sem 协调 character + scene + prop 总并发
        generate: bool = True,  # 0.7.1: False=只建 IDLE 占位子节点不出图（系列/专家项目手动批量触发）
    ) -> List[Dict[str, Any]]:
        """为角色/场景批量生图并落子节点。

        0.6.3 重构：
        - **同步**创建所有占位子节点（status=RUNNING，无 image_url），立即推前端
        - **异步并发**出图填充每个子节点（接受 sem 参数，调用方传共享 Semaphore 限制全局并发）
        - **不再** notify parent_id 的 status_changed，保持主节点 CONFIRMED 不闪烁
        - 返回的 list 含占位 data，调用方拿到立刻继续 update parent.children
        """
        # Step 1: 同步建占位子节点 + 推前端
        children: List[Dict[str, Any]] = []  # 占位 child node + item + index
        total = len(items)
        for i, item in enumerate(items):
            image_prompt = item.get("image_prompt", "")
            if not image_prompt:
                continue

            # 元信息（出图前就能填的字段）
            data: Dict[str, Any] = {
                "result_type": kind,
                "prompt": image_prompt,
            }
            if kind == "character":
                data.update({
                    "character_id": item.get("character_id", f"C{i+1:02d}"),
                    "name": item.get("name", f"角色{i+1}"),
                    "subject_type": (item.get("subject_type") or "human").strip().lower(),
                    "negative_prompt": item.get("negative_prompt", ""),
                    "appearance": item.get("appearance", ""),
                    "clothing": item.get("clothing", ""),
                })
                label = f"🎭 {data['name']}"
            elif kind == "scene":
                data.update({
                    "scene_id": item.get("scene_id", f"S{i+1:02d}"),
                    "location": item.get("location", f"场景{i+1}"),
                    "negative_prompt": item.get("negative_prompt", ""),
                    "environment": item.get("environment", ""),
                    "camera_setups": item.get("camera_setups") or [],  # 0.7.5.4 存机位方案，供场景重跑重生机位板
                })
                label = f"🏞️ {data['location']}"
            else:  # "prop"（0.6.4）
                data.update({
                    "prop_id": item.get("prop_id", f"P{i+1:02d}"),
                    "name": item.get("name", f"道具{i+1}"),
                    "negative_prompt": item.get("negative_prompt", ""),
                    "description": item.get("description", ""),
                    "use_case": item.get("use_case", ""),
                })
                label = f"🎁 {data['name']}"

            child_position = {
                "x": parent_pos["x"] + 300,
                "y": parent_pos["y"] + (offset + i) * 120 - (offset + total) * 60,
            }
            child = self.state.create_node(self.project_id, {
                "node_id": f"{parent_id}_{kind}_{i}",
                "type": "result",
                "label": label,
                "position": child_position,
                "params": data,
                "depends_on": [parent_id],
                "parent_result_id": parent_id,
                "result_index": offset + i,
                "agent": "art_director",
                "need_confirm": True,
                "phase": "art",
                "status": NodeStatus.RUNNING if generate else NodeStatus.IDLE,
                "result": data,
            })
            await self._notify("new_node", {
                "message": (f"🖼️ {label} 生成中..." if generate else f"🖼️ {label}（待生成）"),
                "node": child,
            })
            children.append({"child": child, "item": item, "node_id": child["id"], "data": data})

        # Step 2: 异步并发出图填充（不阻塞调用方，保持主节点 CONFIRMED 不动）
        # 调用方未传 sem 时本地起一个 Semaphore(2)；通常上层 _spawn_art_children 会传共享 sem
        local_sem = sem if sem is not None else asyncio.Semaphore(2)

        async def _fill(entry: Dict[str, Any]) -> None:
            async with local_sem:
                await self._fill_subject_image(entry, kind)

        if generate:
            for entry in children:
                asyncio.create_task(_fill(entry))

        return children

    async def _fill_subject_image(self, entry: Dict[str, Any], kind: str) -> None:
        """异步给一个子节点出图填充。完成后更新 result + status=COMPLETED。"""
        child = entry["child"]
        item = entry["item"]
        child_id = child["id"]
        data = dict(entry["data"])
        image_prompt = item.get("image_prompt", "")
        kind_label = {"character": "🎭 角色", "scene": "🏞️ 场景", "prop": "🎁 道具"}.get(kind, kind)
        await self._progress(child_id, f"🖼️ 智谱 GLM-Image 正在生成 {kind_label}「{data.get('name') or data.get('location') or ''}」...")

        try:
            if kind == "character":
                base_prompt = image_prompt
                subject_type = (item.get("subject_type") or "").strip().lower()
                if subject_type == "human":
                    base_prompt = self._dephoto_human_prompt(base_prompt)
                fortified_prompt = self._fortify_prompt(base_prompt, self.CHARACTER_BG_SUFFIX)
                img_w, img_h = 1024, 1024
            elif kind == "scene":
                fortified_prompt = self._fortify_prompt(image_prompt, self.SCENE_BG_SUFFIX)
                img_w, img_h = 1344, 768
            else:  # "prop"（0.6.4）：方图、产品摄影感
                fortified_prompt = self._fortify_prompt(image_prompt, self.PROP_BG_SUFFIX)
                img_w, img_h = 1024, 1024

            img = await self._generate_image(fortified_prompt, width=img_w, height=img_h)
            data["image_url"] = img.get("image_url")
            data["mock"] = img.get("mock", False)
            data["fortified_prompt"] = fortified_prompt
            # 0.6.5: 子节点不走 _execute_node，得手动 capture usage 到 cost_log
            self._capture_usage_to_cost_log(child_id, "result", "art", img, None)

            # 0.7.5 场景机位板：建立镜出完后按 camera_setups 生成额外机位板（master/简易/进阶）
            if kind == "scene":
                data["cameras"] = await self._generate_camera_boards(
                    child_id, data.get("image_url"), img.get("origin_url"),
                    image_prompt, item.get("camera_setups") or [], img_w, img_h,
                )

            if kind == "character":
                tv_base = image_prompt
                if (item.get("subject_type") or "").strip().lower() == "human":
                    tv_base = self._dephoto_human_prompt(tv_base)
                three_view_prompt = self._build_three_view_prompt(tv_base)
                front_image = data.get("image_url")
                tv_img: Dict[str, Any] = {}
                if front_image:
                    try:
                        tv_img = await self._generate_image_from_image(
                            front_image, self.THREE_VIEW_IMG2IMG_PROMPT, width=1344, height=768,
                        )
                        data["three_view_from_reference"] = True
                    except Exception as e:
                        logger.warning("图生图三视图失败，降级到文生图: %s", e)
                        tv_img = {}
                if not tv_img.get("image_url") or tv_img.get("mock"):
                    try:
                        tv_img = await self._generate_image(three_view_prompt, width=1344, height=768)
                        data["three_view_from_reference"] = False
                    except Exception as e:
                        logger.warning("三视图生成失败: %s", e)
                        data["three_view_error"] = str(e)
                        tv_img = {}
                if tv_img.get("image_url"):
                    data["three_view_image_url"] = tv_img.get("image_url")
                    data["three_view_prompt"] = three_view_prompt
                    data["three_view_mock"] = tv_img.get("mock", False)
                    # 0.7.3：保留三视图的火山 Seedream 原始 URL（asset 入库图源，火山可访问）
                    if tv_img.get("origin_url"):
                        data["three_view_origin_url"] = tv_img.get("origin_url")
                # 0.6.5: 三视图也算钱
                self._capture_usage_to_cost_log(child_id, "result", "art", tv_img, None)

                # 0.7.0 轮 2：再额外生成"头部特写" + 拼接 composite_reference_url
                # human → 单独 gen 头肩特写；non-human → 跳过特写，用 frontal 当左侧
                subject_type = (item.get("subject_type") or "").strip().lower()
                if subject_type == "human" and front_image:
                    await self._progress(child_id, f"🖼️ 生成 {kind_label}「{data.get('name')}」头部特写...")
                    # 用 frontal 当 reference img2img 出脸（确保是同一个人）
                    face_prompt = (
                        "extreme close-up portrait of the SAME character shown in the reference, "
                        "head and shoulders, facing camera, eyes open, neutral expression, "
                        "pure white background, studio lighting, no environment, "
                        "头部特写，肩部以上，正面平视，面部清晰，纯白背景，工作室灯光，无场景"
                    )
                    face_img: Dict[str, Any] = {}
                    try:
                        face_img = await self._generate_image_from_image(
                            front_image, face_prompt, width=1024, height=1024,
                        )
                    except Exception as e:
                        logger.warning("face close-up img2img 失败，降级 t2i: %s", e)
                    if not face_img.get("image_url") or face_img.get("mock"):
                        try:
                            face_t2i_prompt = self._fortify_prompt(
                                self._dephoto_human_prompt(image_prompt) + ", head and shoulders portrait, facing camera",
                                ", 头部特写，肩部以上，纯白色背景，工作室灯光",
                            )
                            face_img = await self._generate_image(face_t2i_prompt, width=1024, height=1024)
                        except Exception as e:
                            logger.warning("face close-up t2i 也失败: %s", e)
                    if face_img.get("image_url"):
                        data["face_close_up_url"] = face_img.get("image_url")
                        data["face_close_up_mock"] = face_img.get("mock", False)
                        # 0.7.3：保留头部特写的火山 Seedream 原始 URL（asset 入库优选图源，单张清晰人脸）
                        if face_img.get("origin_url"):
                            data["face_close_up_origin_url"] = face_img.get("origin_url")
                        self._capture_usage_to_cost_log(child_id, "result", "art", face_img, None)

                # 拼接 composite_reference_url（face/frontal + 3-view 横向）
                from app.core.paths import compose_character_reference
                left_img = data.get("face_close_up_url") or data.get("image_url")
                if left_img and data.get("three_view_image_url"):
                    composite_url = compose_character_reference(left_img, data["three_view_image_url"])
                    if composite_url:
                        data["composite_reference_url"] = composite_url
                        logger.info("character %s composite_reference_url: %s", data.get("name"), composite_url)
        except Exception as e:
            logger.warning("%s 图片生成失败: %s", kind, e)
            data["image_url"] = None
            data["error"] = str(e)

        # 把图填回子节点 + 标 COMPLETED
        self.state.update_node(self.project_id, child_id, {
            "result": data,
            "params": data,
        })
        self.state.set_node_status(self.project_id, child_id, NodeStatus.COMPLETED)
        # 把最新 data 也写回 entry 方便上层 connection_points 用
        entry["data"] = data

    @staticmethod
    def _qwen_angle_prompt(cam_name: str, cam_prompt: str, idx: int) -> str:
        """0.7.5.2 Tier 1：把美术的机位（中文名/描述）映射成 Qwen 多角度 LoRA 英文触发词。
        **强制中角度**（quarter/side，按 idx 轮换不同侧）——绝不 back view(180°)，那档必崩几何。
        高度/景别按关键词。格式 `<sks> <方位> <仰角> <景别>`。"""
        text = f"{cam_name or ''} {cam_prompt or ''}"
        _AZ = ["front-right quarter view", "left side view", "front-left quarter view", "right side view"]
        az = _AZ[idx % len(_AZ)]
        if any(k in text for k in ("低角度", "仰拍", "仰视", "低机位")):
            el = "low-angle shot"
        elif any(k in text for k in ("俯", "高角度", "高机位", "鸟瞰", "顶")):
            el = "high-angle shot"
        else:
            el = "eye-level shot"
        if any(k in text for k in ("特写", "近景", "近")):
            dist = "close-up"
        elif any(k in text for k in ("全景", "远景", "大全", "宽")):
            dist = "wide shot"
        else:
            dist = "medium shot"
        return f"<sks> {az} {el} {dist}"

    async def _generate_camera_boards(
        self,
        child_id: str,
        establish_img_url: Optional[str],
        establish_origin_url: Optional[str],
        base_scene_prompt: str,
        camera_setups: List[Dict[str, Any]],
        width: int,
        height: int,
    ) -> List[Dict[str, Any]]:
        """0.7.5 场景机位板：board0=建立镜；对每个 camera_setup 用**建立镜当单张参考**走图生图各出一块板。
        板只提供"同一场景另一机位的空底图"，人物由下游关键帧再融入（治「换角色不换景」）。
        失败/mock 的板跳过（不污染 cameras）。返回 cameras[]（至少含建立镜一项，向后兼容）。"""
        cameras: List[Dict[str, Any]] = [{
            "camera_id": "establish",
            "camera_name": "建立镜",
            "image_url": establish_img_url,
            "origin_url": establish_origin_url,
            "prompt": base_scene_prompt or "",
        }]
        setups = [
            c for c in (camera_setups or [])
            if isinstance(c, dict) and c.get("camera_name") and c.get("camera_prompt")
        ]
        if not establish_img_url or not setups:
            return cameras
        _K = 4  # 每场景额外机位板上限（0.7.5.1 合并同地点后板变多，放到 4）
        # 0.7.5.2 Tier 1：provider=qwen 走 ComfyUI Qwen 转机位（真换机位/中角度），否则 Seedream 图生图（同墙换取景）
        _use_qwen = False
        try:
            from app.core.runtime_config import scene_camera_provider
            _use_qwen = (scene_camera_provider() == "qwen")
        except Exception:
            _use_qwen = False
        for k, cam in enumerate(setups[:_K]):
            cam_name = str(cam.get("camera_name")).strip()
            # Seedream 兜底用的板 prompt（Qwen 失败时回退）
            board_prompt = self._fortify_prompt(
                str(cam.get("camera_prompt") or "").strip()
                + "，保持与参考图同一空间、家具陈设、材质与光线，空场景无人物",
                self.SCENE_BG_SUFFIX,
            )
            bimg = None
            _used_prompt = board_prompt
            if _use_qwen:
                try:
                    from app.modules._comfyui_qwen import generate_angle_view
                    _angle = self._qwen_angle_prompt(cam_name, cam.get("camera_prompt"), k)
                    await self._progress(child_id, f"🎥 Qwen 转机位「{cam_name}」({_angle})...")
                    bimg = await generate_angle_view(
                        establish_img_url, _angle,
                        progress_cb=lambda m: self._progress(child_id, m),
                    )
                    _used_prompt = _angle
                except Exception as e:
                    logger.warning("机位板「%s」Qwen 转机位失败，回退 Seedream: %s", cam_name, e)
                    bimg = None
            if not bimg or not bimg.get("image_url"):
                await self._progress(child_id, f"🎥 生成机位板「{cam_name}」...")
                _used_prompt = board_prompt
                try:
                    bimg = await self._generate_image_from_image(
                        establish_img_url, board_prompt, width=width, height=height,
                    )
                except Exception as e:
                    logger.warning("机位板「%s」img2img 失败，跳过: %s", cam_name, e)
                    continue
            if not bimg.get("image_url") or bimg.get("mock"):
                logger.warning("机位板「%s」无效(mock=%s)，跳过", cam_name, bimg.get("mock"))
                continue
            self._capture_usage_to_cost_log(child_id, "result", "art", bimg, None)
            cameras.append({
                "camera_id": str(cam.get("camera_id") or f"CAM{k + 1:02d}").strip(),
                "camera_name": cam_name,
                "image_url": bimg.get("image_url"),
                "origin_url": bimg.get("origin_url"),
                "prompt": _used_prompt,
                "when_to_use": str(cam.get("when_to_use") or "").strip(),
            })
        if len(cameras) > 1:
            logger.info("场景机位板：建立镜 + %d 块额外板", len(cameras) - 1)
        return cameras

    @staticmethod
    def _build_three_view_prompt(base_prompt: str) -> str:
        """从角色单图 prompt 构造三视图 prompt：
        - 去掉单视角描述（"sitting", "front view", "portrait" 等可能影响多视角生成的词）
        - 加上 turnaround sheet 描述
        """
        stripped = base_prompt
        for bad in [
            ", sitting calmly", ", standing", ", portrait", ", full body shot",
            ", front view only", "isolated on pure white background",
            "studio lighting, no environment",
        ]:
            stripped = stripped.replace(bad, "")
        return stripped.strip().rstrip(",") + InteractiveExecutor.THREE_VIEW_SUFFIX

    @staticmethod
    def _fortify_prompt(base_prompt: str, suffix: str) -> str:
        """把 base_prompt 末尾已经存在的同义短语去掉，再追加更强的中英双语后缀。
        这样避免重复，也保证关键约束放在 prompt 末尾（Zhipu / SD 类模型对末尾权重高）。
        """
        cleaned = base_prompt or ""
        # 去掉已存在的英文白底/无场景约束（避免和我们的强约束打架或重复占长度）
        for redundant in (
            "isolated on pure white background",
            "studio lighting, no environment",
            "no shadows on background",
            "no characters, no people, no animals, empty scene",
            "no characters",
            "no people",
            "no animals",
            "empty scene",
            "product reference shot",
            "centered composition",
        ):
            cleaned = cleaned.replace(redundant, "")
        cleaned = cleaned.strip().rstrip(",").strip()
        return f"{cleaned}{suffix}"

    async def _post_storyboard(self, node_id: str, sb_data: Dict) -> None:
        """分镜节点跑完后，把每个 shot 写到（已存在的）子节点里。

        0.7.0：移除自动 review_storyboard（那是第 3 个 16384 巨型调用，episode 多镜头时超时）。
        它的职责已被两趟拆分承接：① @ 覆盖 → Pass1 骨架确定性校验 ② 主从尺寸 → Pass2 逐镜头强制。
        review_storyboard 方法保留，用户可在 chat 里 @导演 手动触发。
        """
        review_notes = ""
        # 分发到子节点（两趟产出的 shots 已规范化，直接用）
        node = self.state.get_node(self.project_id, node_id) or {}  # refetch
        children = node.get("children", []) or []
        shots = sb_data.get("storyboard", []) if isinstance(sb_data, dict) else []
        for i, child_id in enumerate(children):
            if i < len(shots):
                self.state.update_node(self.project_id, child_id, {
                    "result": shots[i],
                    "outputs": {"shot": shots[i]},
                    "status": NodeStatus.COMPLETED,
                })

        # 主节点 result 同步覆盖（下游 _fanout_storyboard 可能从 result 拿 shots）
        outputs = {
            **(node.get("outputs") or {}),
            "storyboard": sb_data,
            "director_review_notes": review_notes,
        }
        self.state.update_node(self.project_id, node_id, {
            "result": sb_data,
            "outputs": outputs,
        })

    # ═══════════════════════════════════════════════════════
    # storyboard → keyframe / video 的 fan-out
    # ═══════════════════════════════════════════════════════

    async def _fanout_storyboard(
        self,
        storyboard_node: Dict[str, Any],
        next_step: Dict[str, Any],
    ) -> Dict[str, Any]:
        """分镜确认 → 为每个分镜创建节点：
        - shot.requires_keyframe == True → 关键帧节点（image_to_image 多图融合）
        - 其它 → 直接视频节点（沿用 first_frame fallback 链）
        """
        # 0.8 缝4：可选闸 —— 扇出本身不花钱，但它是"即将大量出片"的前置动作，
        # 在这里拦比等用户点了批量生成再拒更早、更省事。
        from app.core.entitlements import require, Capability
        require(Capability.STORYBOARD_FANOUT)
        children = storyboard_node.get("children", []) or []
        if not children:
            sb_result = storyboard_node.get("result") or {}
            shot_items: List[tuple] = [(None, s) for s in (
                sb_result.get("storyboard", []) if isinstance(sb_result, dict) else []
            )]
        else:
            shot_items = []
            for cid in children:
                child = self.state.get_node(self.project_id, cid) or {}
                shot_items.append((cid, child.get("result") or {}))

        sb_params = storyboard_node.get("params") or {}
        char_refs = sb_params.get("character_refs") or []
        scene_refs = sb_params.get("scene_refs") or []
        prop_refs = sb_params.get("prop_refs") or []  # 0.6.4

        keyframe_nodes: List[str] = []
        video_nodes: List[str] = []
        base_pos = storyboard_node.get("position", {"x": 1000, "y": 200})

        # 项目级 aspect_ratio：决定 keyframe / video 节点的分辨率
        project_ratio = self._project_aspect_ratio()
        kf_w, kf_h = self._resolution_for_ratio(project_ratio)

        # W5：连贯链识别 —— continuity_to_next ∈ {match_cut, movement_continuation} 的相邻镜连成链
        shots_only = [s for (_pid, s) in shot_items]
        chains = self._detect_continuity_chains(shots_only)
        link_indices: set = set()
        chain_meta_by_index: Dict[int, Dict[str, Any]] = {}
        for ch in chains:
            head_idx = ch[0]
            for pos, idx in enumerate(ch):
                chain_meta_by_index[idx] = {
                    "chain_role": "head" if pos == 0 else "link",
                    "chain_head_index": head_idx,
                    "chain_pos": pos,
                }
                if pos > 0:
                    link_indices.add(idx)
        prev_index_node: Dict[int, str] = {}  # shot index → 该镜画布主节点 id（链内连线锚点）

        for i, (parent_shot_id, shot) in enumerate(shot_items):
            built = self._build_video_payload(shot, char_refs, scene_refs, prop_refs)
            deps = [parent_shot_id] if parent_shot_id else [storyboard_node["id"]]
            cmeta = chain_meta_by_index.get(i)  # None = 非链镜

            logger.info(
                "fanout shot[%d] shot_id=%s requires_keyframe=%r chain=%s",
                i, shot.get("shot_id"), shot.get("requires_keyframe"),
                cmeta and cmeta.get("chain_role"),
            )

            if i in link_indices:
                # W5：链内后续镜 —— 跳过关键帧，首帧来自上一镜尾帧（运行时 chain-runner 注入）。
                # depends_on 锚到上一链成员（i-1，链是连续索引）= 画布连线 + 提取失败时的回退依赖。
                # W5：link 的输入连"上一镜的视频节点"（取其尾帧当首帧）。上一镜=i-1，其 video 模板 id=node_video_{i:02d}。
                # 链首(head)的 video 是确认关键帧后才创建，所以这条线会等 head video 出现后才画出来。
                link_deps = [f"node_video_{i:02d}"]
                video_node = self._build_video_node_for_shot(
                    shot=shot, built=built, deps=link_deps,
                    index=i, base_pos=base_pos, first_frame_override=None,
                    chain_meta=cmeta,
                )
                video_nodes.append(video_node["id"])
                prev_index_node[i] = video_node["id"]
            elif shot.get("requires_keyframe"):
                # 关键帧节点：依赖分镜 + 引用的角色/场景/道具
                # image_urls 顺序：场景背景 + 道具 + 角色主体（最多 4 张：Zhipu image_to_image 上限）
                scene_imgs = [s["image_url"] for s in built["referenced_scenes"] if s.get("image_url")]
                prop_imgs = [p["image_url"] for p in built["referenced_props"] if p.get("image_url")]  # 0.6.4
                # 0.7.0 轮 2：角色优先用 composite_reference_url（face + 三视图拼接），
                # 没有就降级 three_view，再降级 frontal — 一致性 >> 体积
                # 0.7.3：Seedream 多主体融合**优先喂单张清晰正脸特写**(face_close_up)——composite/三视图是
                # "多figure图"(正脸+多个全身角度)，脸不突出、多角色融合时锁不住(实测王桂芬老太太→年轻女)。
                # 优先级 face_close_up > composite > 三视图 > 全身正面。
                # 0.7.5.3：一个关键帧最多喂 **3 张正脸**（Seedream 多主体融合上限，4+ 必串脸/脸崩）。
                # referenced_characters 前 3 个（@引用顺序≈剧情重要度）当"正脸角色"喂脸；其余角色只在 prompt 里
                # 标成背影/侧背/后脑、不喂脸不锁长相 → 治 establish/群像镜 6 张脸全崩。
                _KF_FACE_MAX = 3
                _all_ref_chars = list(built["referenced_characters"])
                _face_chars = _all_ref_chars[:_KF_FACE_MAX]
                _bg_chars = _all_ref_chars[_KF_FACE_MAX:]
                char_imgs = [
                    c.get("face_close_up_url") or c.get("composite_reference_url")
                    or c.get("three_view_image_url") or c.get("image_url")
                    for c in _face_chars
                ]
                char_imgs = [u for u in char_imgs if u]
                # 角色优先(锁脸最重要) + 场景1张打底 + 道具；上限9(留1 buffer)，超了先砍场景/道具、保角色。
                image_urls = char_imgs + scene_imgs[:1] + prop_imgs + scene_imgs[1:]
                _KF_MAX = 9
                if len(image_urls) > _KF_MAX:
                    logger.warning(
                        "shot %s 关键帧 image_urls 超过 %d 张 (scene=%d prop=%d char=%d)，截断到前 %d 张(角色优先)",
                        shot.get("shot_id"), _KF_MAX, len(scene_imgs), len(prop_imgs), len(char_imgs), _KF_MAX,
                    )
                    image_urls = image_urls[:_KF_MAX]

                # 0.7.4.1：fanout 时就拼好**完整关键帧 prompt**；只列正脸角色（≤3），背影角色单独标注。
                _spos = shot.get("screen_position") if isinstance(shot.get("screen_position"), dict) else {}
                _n_char_kf = min(len(char_imgs), _KF_MAX)
                _idx_lines = []
                for _ci in range(_n_char_kf):
                    _nm = ((_face_chars[_ci].get("name")
                            if _ci < len(_face_chars) else None) or f"角色{_ci+1}")
                    _pos = (_spos or {}).get(_nm) or ""
                    _idx_lines.append(f"图{_ci+1}是{_nm}" + (f"（{_pos}）" if _pos else ""))
                if scene_imgs and _n_char_kf < _KF_MAX:
                    _cam_nm = (built["referenced_scenes"][0].get("camera_name")
                               if built["referenced_scenes"] else None)  # 0.7.5 机位板名
                    _idx_lines.append(f"图{_n_char_kf+1}是场景背景" + (f"（{_cam_nm}机位）" if _cam_nm else ""))
                # 0.7.5.3：背影角色提示——在场但只露背影/侧背，不给正脸、不锁长相，避免多脸混乱
                _bg_note = ""
                if _bg_chars:
                    _bg_names = "、".join((c.get("name") or "配角") for c in _bg_chars)
                    _bg_note = (f"另外 {_bg_names} 也在场，但**只以背影/侧背/后脑、或前景虚化的肩背出现**"
                                f"（坐在圆桌远侧背对镜头，或前景只见后脑与肩），**绝不给他们正脸、不必匹配任何长相**；"
                                f"画面里清晰正脸的只有上面点名的这 {_n_char_kf} 位。")
                _kf_w2 = shot.get("keyframe_width") or kf_w
                _kf_h2 = shot.get("keyframe_height") or kf_h
                _orient = "横幅 16:9 宽画面" if _kf_w2 > _kf_h2 else ("竖幅 9:16 画面" if _kf_h2 > _kf_w2 else "方形画面")
                _fuse = (
                    f"把这些参考图重建成一张真实拍摄的{_orient}场景照片：按下面的画面描述，让每个人物"
                    "**坐/站在场景内正确的位置上**（如围坐在圆桌旁的椅子上、各就各位），有正确的身体朝向和彼此互动，"
                    "人物与家具/环境之间有真实的空间关系和前后遮挡——**他们是参与这个场景的人，不是贴在背景前的立牌/剪影**。"
                    "全画面同一光源、同一色调、统一透视与比例；参考图只提供人物长相/服装与场景风格，不要照搬任何一张的构图。"
                    "严禁拼贴、分屏、并排排版、画中画、边框分割。"
                    "Rebuild into ONE real photograph: seat/place each person at their correct spot INSIDE the scene "
                    "with true spatial relationship and occlusion, unified lighting/perspective; they are NOT cutouts "
                    "pasted in front of the background; NO collage, NO split-screen, NO side-by-side panels."
                )
                _ref_hint = (("；".join(_idx_lines) + "。这几位严格保持其对应参考图的五官/脸型/发型/长相不变，"
                             "不得混淆或互换任何人的面孔，并放在其括号标注的场景站位上。") if _idx_lines else "") + _bg_note
                _kf_prompt = "\n".join(filter(None, [_ref_hint, _fuse, built["keyframe_prompt"]]))

                kf_node = self._create_phase_node(
                    phase="keyframe",
                    node_template_id=f"node_keyframe_{i+1:02d}",
                    depends_on=deps + built["extra_deps"],
                    position={"x": base_pos["x"] + 300, "y": base_pos["y"] + i * 160},
                    params={
                        "shot": shot,
                        "shot_index": i,
                        "prompt": _kf_prompt,
                        "raw_visual_prompt": shot.get("visual_prompt", ""),
                        "image_urls": image_urls,
                        "referenced_characters": built["referenced_characters"],
                        "referenced_scenes": built["referenced_scenes"],
                        "referenced_props": built["referenced_props"],  # 0.6.4
                        "width": shot.get("keyframe_width") or kf_w,
                        "height": shot.get("keyframe_height") or kf_h,
                        "aspect_ratio": project_ratio,
                    },
                    label=f"关键帧 {shot.get('shot_id', f'Shot{i+1}')}",
                    extra={"parent_shot": deps[0]},
                )
                keyframe_nodes.append(kf_node["id"])
                prev_index_node[i] = kf_node["id"]
            else:
                # 不需要关键帧 → 直接走视频（含非关键帧的链 head：打 head 标签）
                video_node = self._build_video_node_for_shot(
                    shot=shot, built=built, deps=deps,
                    index=i, base_pos=base_pos, first_frame_override=None,
                    chain_meta=cmeta,
                )
                video_nodes.append(video_node["id"])
                prev_index_node[i] = video_node["id"]

        # W5：把链结构存到 storyboard 节点 params，供 batch chain-runner / keyframe-confirm 读取
        if chains:
            self.state.update_node(self.project_id, storyboard_node["id"], {
                "params": {**sb_params, "continuity_chains": chains},
            })
            logger.info("W5 检测到 %d 条连贯链：%s", len(chains), chains)

        # 0.7.1：专家(分集)模式检测（expert_seeded / series_id；分集项目 config.mode 是 "advanced"，别用 mode 判断）
        _proj = self.state.get_project(self.project_id) or {}
        _cfg = _proj.get("config") or {}
        is_expert = bool(_cfg.get("expert_seeded") or _cfg.get("series_id") or _proj.get("series_id"))
        # 0.7.1：独立进阶(advanced)项目也走"停 IDLE + 手动批量"，与专家一致 —— 修自动并发不尊重 W5 链 /
        # 关键帧→视频依赖的问题。专家分集 config.mode 也是 "advanced"，故 is_advanced 对其也 True，但已被
        # is_expert 覆盖、行为不变；简易 mode=simple → manual_fanout=False 仍自动。
        is_advanced = (_cfg.get("mode") == "advanced")
        manual_fanout = is_expert or is_advanced
        # 1.1.0 MV：歌就是音轨，不需要再生成 BGM。
        # 合成那步已经强制用原曲（_run_merge 里 audio_mode=replace），就算这里建了节点也不会
        # 进成片——但白花一次音频生成的钱，画布上还多个永远用不上的节点。
        is_mv = (_cfg.get("mode") == "mv")

        # 0.6.1: audio_plan 节点（与 keyframe/video 并行）。**专家模式先不生成音频**（0.7.2 音频重构后再接入），连节点都不建。
        # audio_plan 需要 storyboard + total_duration + mood，并喂完整剧本以便 LLM 做深度分析。
        sb_result = storyboard_node.get("result") or {}
        total_duration = int(sb_result.get("total_duration_seconds") or 0)
        if total_duration <= 0:
            total_duration = sum(int(s.get("duration_seconds") or 0)
                                 for _, s in shot_items if isinstance(s, dict))
        project = self.state.get_project(self.project_id) or {}
        collected = project.get("collected_info") or {}

        # 找最新 script_generation 节点，把 logline / scenes 喂给 audio_director
        script_context: Dict[str, Any] = {}
        for n in self.state.get_all_nodes(self.project_id):
            if n.get("type") == "script_generation":
                sr = n.get("result") or {}
                if isinstance(sr, dict):
                    script_context = {
                        "title": sr.get("script_title") or "",
                        "logline": sr.get("logline") or "",
                        "style": sr.get("style") or "",
                        "duration": sr.get("duration") or "",
                        "structure": sr.get("structure") or {},
                        "scenes": sr.get("scenes") or [],
                    }

        # 音频门控单独用 is_expert：简易/进阶都建整段 BGM（audio_plan），只有专家不建（分段音频留 0.7.2）。
        # 不能用 manual_fanout —— 那会把进阶也停掉；keyframe/video 的手动与 audio 的建立是两件事。
        audio_node = None if (is_expert or is_mv) else self._create_phase_node(
            phase="audio",
            node_template_id="node_audio_plan",
            depends_on=[storyboard_node["id"]],
            position={"x": base_pos["x"], "y": base_pos["y"] + 700},
            params={
                "storyboard": sb_result.get("storyboard") or [],
                "total_duration": total_duration,
                "mood": collected.get("mood") or "",
                "style": collected.get("style") or "",
                "shot_plan_summary": sb_result.get("shot_plan_summary") or "",
                "script": script_context,
            },
            label="音频导演",
        )

        # 0.7.2 ②：专家分集 → 场景 BGM 计划节点（episode_audio_plan）。
        # 自动跑 LLM 规划（便宜）；**用户确认计划后**才 spawn bgm_fetch 花钱生成（_post_episode_audio_plan）。
        ep_audio_node = None
        if is_expert:
            ep_audio_node = self._create_phase_node(
                phase="episode_audio",
                node_template_id="node_episode_audio_plan",
                depends_on=[storyboard_node["id"]],
                position={"x": base_pos["x"], "y": base_pos["y"] + 700},
                params={
                    "storyboard": sb_result.get("storyboard") or [],
                    "total_duration": total_duration,
                    "script_scenes": script_context.get("scenes") or [],
                },
                label="本集音频 · 场景BGM",
            )

        # 0.7.1：专家/进阶模式不自动执行 —— keyframe/video 停 IDLE 等手动触发；音频不建（见上）。
        if manual_fanout:
            confirm_msg = (
                f"✅ 分镜确认！已创建 {len(keyframe_nodes)} 关键帧 + {len(video_nodes)} 视频 节点（待生成）。"
                f"可逐镜调整后，点「开始生成」批量触发，或单镜「重跑」。"
                + ("同时音频导演正在按场景规划本集 BGM（确认计划后才生成音乐）。" if ep_audio_node else "")
            )
        else:
            confirm_msg = (
                f"✅ 分镜确认！{len(keyframe_nodes)} 个镜头需先生成关键帧"
                f"，{len(video_nodes)} 个镜头直接生成视频；同时启动音频导演规划 BGM"
            )
        await self._notify("storyboard_confirmed", {
            "message": confirm_msg,
            "new_nodes": keyframe_nodes + video_nodes
                + ([audio_node["id"]] if audio_node else [])
                + ([ep_audio_node["id"]] if ep_audio_node else []),
            "manual_trigger": manual_fanout,
        })

        # 并发上限：image_to_image 也走 Seedream，跟 video 一样控制
        sem = asyncio.Semaphore(2)

        async def _bounded_execute(nid: str) -> None:
            async with sem:
                await self._execute_node(nid)

        if not manual_fanout:
            # 简易：自动执行 keyframe + 非链镜 + 链首。
            # **连贯链 link 镜(chain_pos>0)不在此发** —— 否则会抢在链首出片前并行生成、
            # 拿不到前镜尾帧而回退自己的场景图（实测 bug：SHOT_02 用了美术场景图当首帧）。
            # link 改由前镜出片后的 _propagate_tail_to_next_link 事件驱动串行触发（见该方法）。
            for nid in keyframe_nodes:
                asyncio.create_task(_bounded_execute(nid))
            for nid in video_nodes:
                _vn = self.state.get_node(self.project_id, nid) or {}
                if (_vn.get("params") or {}).get("chain_pos"):   # truthy = pos>0 = link，暂不发
                    logger.info("[fanout] 简易连贯链 link %s 暂缓，等前镜出片驱动", nid)
                    continue
                asyncio.create_task(_bounded_execute(nid))
        else:
            logger.info(
                "[fanout_storyboard] 专家/进阶模式：%d keyframe + %d video 节点创建为 IDLE，等手动触发",
                len(keyframe_nodes), len(video_nodes),
            )
        # audio_plan 不占视频信号量（LLM 调用，独立池）。与 keyframe/video 的手动门控解耦：
        # 简易/进阶都自动跑整段 BGM 规划；专家走 episode_audio_plan（场景分层，确认后才生成）。
        if audio_node:
            asyncio.create_task(self._execute_node(audio_node["id"]))
        if ep_audio_node:
            asyncio.create_task(self._execute_node(ep_audio_node["id"]))

        return {
            "type": "phase_advanced",
            "message": next_step.get("message", "分镜已确认"),
            "keyframe_nodes": keyframe_nodes,
            "video_nodes": video_nodes,
            "audio_node": audio_node["id"] if audio_node else None,
            "episode_audio_node": ep_audio_node["id"] if ep_audio_node else None,
            "phase": "keyframe" if keyframe_nodes else "video",
            "manual_trigger": manual_fanout,
        }

    # 兼容旧名（外部不应再调用，但保留以防未列出的引用）
    _fanout_storyboard_to_videos = _fanout_storyboard

    async def batch_execute_nodes(self, phase: str = "all", node_ids=None) -> Dict[str, Any]:
        """0.7.1 专家模式手动触发：批量执行某 phase 的所有 IDLE/失败 节点（或指定 node_ids）。
        依赖图保证顺序：_execute_node 会按 depends_on 处理（W5 连贯链也据此串行）。"""
        # 0.8 缝4：**付费视频总闸**（本方法是批量出片的唯一入口，门控首要落点）。
        # 这里按本方法既有约定返回结构化 dict，而不是抛异常——上层 API 直接把 message 显示给用户。
        from app.core.entitlements import check, deny_reason, Capability
        if not check(Capability.VIDEO_GENERATE):
            return {"type": "entitlement_denied", "count": 0,
                    "message": deny_reason(Capability.VIDEO_GENERATE)}
        all_nodes = self.state.get_all_nodes(self.project_id) or []

        def _st(n) -> str:
            return str(n.get("status") or "").lower()

        # 0.7.1 专家：美术子图批量触发（非标准 runner）。总项目美术(phase=art)走 _retry_result_child；
        # 本集美术(phase=episode_art)走 _retry_episode_child（img2img from master）。串行限速由各自 provider 适配器兜底。
        if phase in ("art", "episode_art"):
            arts = [n for n in all_nodes
                    if str(n.get("phase") or "") in ("art", "episode_art")
                    and n.get("type") == "result"
                    and ("idle" in _st(n) or "failed" in _st(n) or (n.get("result") or {}).get("mock"))]
            if not arts:
                return {"type": "batch_no_idle", "message": "没有待生成的美术子图", "count": 0}
            sem_art = asyncio.Semaphore(2)
            async def _runart(n: Dict[str, Any]) -> None:
                nid = n["id"]
                async with sem_art:
                    try:
                        if str(n.get("phase") or "") == "episode_art":
                            await self._retry_episode_child(nid)
                        else:
                            await self._retry_result_child(nid)
                    except Exception as e:
                        logger.error("batch 美术子图 %s 失败：%s", nid, e)
            for n in arts:
                asyncio.create_task(_runart(n))
            await self._notify("batch_execute_started", {
                "message": f"🎨 开始生成 {len(arts)} 个美术子图...",
                "phase": "art", "count": len(arts),
            })
            return {"type": "batch_execute_started", "count": len(arts),
                    "message": f"已启动 {len(arts)} 个美术子图", "phase": "art"}

        if node_ids:
            wanted = set(node_ids)
            runnable = [n for n in all_nodes if n.get("id") in wanted]
        else:
            phases = ["keyframe", "video"] if phase in ("all", "shots") else [phase]
            runnable = [
                n for n in all_nodes
                if resolve_phase(n) in phases and ("idle" in _st(n) or "failed" in _st(n))
            ]
        if not runnable:
            return {"type": "batch_no_idle", "message": "没有待生成的镜头节点", "count": 0}

        sem = asyncio.Semaphore(2)

        async def _run_single(nid: str) -> None:
            async with sem:
                try:
                    await self._execute_node(nid)
                except Exception as e:
                    logger.error("batch 执行节点 %s 失败：%s", nid, e)

        async def _run_chain(ordered: List[Dict[str, Any]]) -> None:
            """W5：一条连贯链占 1 个并发槽、链内严格串行；每镜完成提尾帧注入下一镜首帧。"""
            async with sem:
                prev_video_url = None
                for node in ordered:
                    nid = node["id"]
                    if prev_video_url:
                        last_png = await self._extract_last_frame(prev_video_url, tag=nid)
                        if last_png:  # 提取成功才覆盖；失败则保留该 link 自己的 built 首帧（优雅回退）
                            cur = self.state.get_node(self.project_id, nid) or node
                            cur_params = cur.get("params") or {}
                            self.state.update_node(self.project_id, nid, {
                                "params": {**cur_params, "first_frame_url": last_png,
                                           "first_frame_from_chain": True},
                            })
                            await self._notify("node_progress", {
                                "node_id": nid, "progress": 0,
                                "message": "🔗 续接上一镜尾帧作为首帧...",
                            })
                    try:
                        await self._execute_node(nid)
                    except Exception as e:
                        logger.error("batch 链节点 %s 失败：%s", nid, e)
                        prev_video_url = None
                        continue
                    done = self.state.get_node(self.project_id, nid) or node
                    prev_video_url = ((done.get("result") or {}).get("video_url")) or None
                    if not prev_video_url:
                        logger.warning("W5 链节点 %s 无视频产出，后续镜回退自己的首帧", nid)

        # W5：runnable 分成 chain 组（按 chain_head_index 聚合、head 在前）+ singles（其余，行为不变）
        chain_groups: Dict[Any, List[Dict[str, Any]]] = {}
        singles: List[str] = []
        for n in runnable:
            p = n.get("params") or {}
            head = p.get("chain_head_index")
            if head is not None and n.get("type") == "video_generation":
                chain_groups.setdefault(head, []).append(n)
            else:
                singles.append(n["id"])

        started = 0
        for head_idx, grp in chain_groups.items():
            grp.sort(key=lambda x: (x.get("params") or {}).get("chain_pos", 0))
            if (grp[0].get("params") or {}).get("chain_pos", 99) != 0:
                # 链首(head video)还没就绪（keyframe 未确认）→ 本批跳过这些 link，等 head 出现再跑
                logger.info("W5 链 head_index=%s 未就绪，%d 个 link 本批跳过", head_idx, len(grp))
                continue
            asyncio.create_task(_run_chain(grp))
            started += len(grp)
        for nid in singles:
            asyncio.create_task(_run_single(nid))
            started += 1

        await self._notify("batch_execute_started", {
            "message": f"🎬 开始生成 {started} 个镜头...",
            "phase": phase, "count": started,
        })
        return {
            "type": "batch_execute_started", "count": started,
            "message": f"已启动 {started} 个节点生成", "phase": phase,
        }

    def _build_video_node_for_shot(
        self,
        shot: Dict[str, Any],
        built: Dict[str, Any],
        deps: List[str],
        index: int,
        base_pos: Dict[str, int],
        first_frame_override: Optional[str] = None,
        chain_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """根据分镜生成单个视频节点（被 fan-out 和 keyframe→video 都调用）"""
        first_frame = first_frame_override or built.get("first_frame_url")
        params = {
            "shot": shot,
            "shot_index": index,
            "scene_description": shot.get("scene_description", ""),
            "characters": shot.get("characters", []),
            "duration": shot.get("duration_seconds", 5),
            "prompt": built["prompt"],
            "raw_visual_prompt": shot.get("visual_prompt", ""),
            "raw_video_prompt": shot.get("video_prompt", ""),
            "first_frame_url": first_frame,
            "first_frame_from_keyframe": bool(first_frame_override),
            "referenced_characters": built["referenced_characters"],
            "referenced_scenes": built["referenced_scenes"],
            "referenced_props": built.get("referenced_props", []),  # 0.6.4
            # 项目级画面比例 → _run_video 用来决定 ratio + resolution
            "aspect_ratio": self._project_aspect_ratio(),
        }
        if chain_meta:  # W5：chain_role / chain_head_index / chain_pos
            params.update(chain_meta)
        return self._create_phase_node(
            phase="video",
            node_template_id=f"node_video_{index+1:02d}",
            depends_on=deps + built["extra_deps"],
            position={"x": base_pos["x"] + 600, "y": base_pos["y"] + index * 160},
            params=params,
            label=f"视频生成 {shot.get('shot_id', f'Shot{index+1}')}",
            extra={"parent_shot": deps[0]},
        )

    @staticmethod
    def _detect_continuity_chains(shots: List[Dict[str, Any]]) -> List[List[int]]:
        """W5：扫描有序 shots，把 continuity_to_next ∈ {match_cut, movement_continuation}
        连接的相邻镜分成极大链，只返回长度 ≥2 的链（链[0]=head）。非连贯/末镜终止链。"""
        LINK = {"movement_continuation"}  # 只有运镜延续适合"前镜尾帧=后镜首帧"；match_cut 是匹配切(仍是独立镜头)，不做帧续接
        chains: List[List[int]] = []
        cur: List[int] = []
        n = len(shots)
        for i in range(n):
            if not cur:
                cur = [i]
            cont = str((shots[i] or {}).get("continuity_to_next") or "").strip().lower()
            if cont in LINK and i + 1 < n:
                cur.append(i + 1)  # i 与 i+1 同链
            else:
                if len(cur) >= 2:
                    chains.append(cur)
                cur = []
        if len(cur) >= 2:
            chains.append(cur)
        return chains

    # ─── keyframe runner & 后续 ───

    async def _run_keyframe(self, node: Dict, params: Dict) -> tuple[Dict, Dict]:
        """关键帧 runner：调用 image_to_image 多图融合"""
        module = ModuleRegistry.get("image_to_image")
        if not module:
            return ({"image_url": "", "status": "mock", "message": "image_to_image 模块未注册"}, {})

        image_urls = params.get("image_urls") or []
        # 0.7.3：**实时**从角色节点重构 image_urls，优先单张正脸 face_close_up（旧节点 params 存的是 composite 多figure）。
        # 这样只重跑关键帧就生效，不用重新分镜删下游。face > composite > 三视图 > 全身正面；场景 1 张打底。
        _refs = params.get("referenced_characters") or []
        if _refs:
            _refs = _refs[:3]  # 0.7.5.3：关键帧最多喂 3 张正脸（Seedream 多主体上限，4+ 必串脸）；其余角色 prompt 已标背影
            _rebuilt = []
            for _c in _refs:
                _face = _c.get("face_close_up_url")
                if not _face and _c.get("node_id"):
                    _rr = (self.state.get_node(self.project_id, _c["node_id"]) or {}).get("result") or {}
                    _face = _rr.get("face_close_up_url")
                _u = (_face or _c.get("composite_reference_url")
                      or _c.get("three_view_image_url") or _c.get("image_url"))
                if _u:
                    _rebuilt.append(_u)
            for _s in (params.get("referenced_scenes") or [])[:1]:
                if _s.get("image_url"):
                    _rebuilt.append(_s["image_url"])
            if _rebuilt:
                image_urls = _rebuilt[:9]
                logger.info("[keyframe] 实时重构 image_urls：%d 张(优先单张正脸)", len(image_urls))
        prompt = params.get("prompt") or ""
        width = params.get("width") or 720
        height = params.get("height") or 1280

        if not image_urls:
            # 没有参考图，降级为只用 prompt（image_to_image 会报错，让上层标 FAILED）
            logger.warning("keyframe 节点 %s 没有可用参考图，跳过 image_to_image", node.get("id"))
            return ({"image_url": "", "skipped": True, "reason": "no reference images"}, {})

        await self._progress(node["id"], f"🖼️ Seedream 多图融合中（{len(image_urls)} 张参考图）...")

        # 0.7.4.1：关键帧 prompt（图N是谁+站位 → 重建规则 → 场景画面）已在 fanout 时拼好存进 params.prompt，
        # 这里直接用、不重复拼（image_urls 上面已按 face_close_up 优先实时重构；"图N"对应=referenced_characters 顺序，稳定一致）。

        # 调试日志：方便用户排查 storyboard.chat 给 keyframe 加参考图后实际跑了什么
        logger.info(
            "[keyframe→Seedream] node=%s, %d ref(s), %dx%d, prompt: %s",
            node.get("id"), len(image_urls), width, height, prompt[:500],
        )

        result = await module.run({
            "image_urls": image_urls,
            "prompt": prompt,
            "width": width,
            "height": height,
        })
        return result, {}

    def _format_keyframe_preview(self, result: Dict) -> str:
        if not isinstance(result, dict) or not result.get("image_url"):
            return "🖼️ 关键帧（未生成）"
        n = result.get("reference_count", 1)
        return f"🖼️ 关键帧已生成（融合 {n} 张参考图）"

    async def _after_keyframe_confirm(self, keyframe_node_id: str) -> Dict[str, Any]:
        """关键帧确认 → 创建对应 shot 的视频节点（first_frame = 关键帧图）"""
        kf_node = self.state.get_node(self.project_id, keyframe_node_id) or {}
        kf_result = kf_node.get("result") or {}
        # 0.7.1 #5：优先用 Seedream 原始火山产物 URL(受信任、免 Seedance 真人审核;http 透传不被 base64)；
        # 0.7.3：**本地 image_url 优先**（走 OSS 入库/base64 直传都不过期）；origin_url 兜底(老数据)。
        # origin_url 是火山 24h 签名链接、隔天必失效 → 若当首帧入 asset 会下载 403，故用本地图。
        kf_image = (kf_result.get("image_url") or kf_result.get("origin_url")) if kf_result else None
        kf_params = kf_node.get("params") or {}
        shot = kf_params.get("shot") or {}
        shot_index = kf_params.get("shot_index", 0)

        # 重新跑一次 _build_video_payload 拿 prompt + extra_deps（不依赖 keyframe 节点的 params 缓存）
        # 找到 storyboard 节点位置作为定位基准
        storyboard_node = self._find_storyboard_node()
        base_pos = (storyboard_node or kf_node).get("position", {"x": 1000, "y": 200})
        sb_params = (storyboard_node or {}).get("params") or {}
        char_refs = sb_params.get("character_refs") or []
        scene_refs = sb_params.get("scene_refs") or []
        prop_refs = sb_params.get("prop_refs") or []  # 0.6.4

        built = self._build_video_payload(shot, char_refs, scene_refs, prop_refs)

        # W5：若该镜是连贯链链首，给其 video 节点打 head 标签（供 batch chain-runner 顺序驱动）
        chain_meta = None
        for ch in (sb_params.get("continuity_chains") or []):
            if ch and ch[0] == shot_index:
                chain_meta = {"chain_role": "head", "chain_head_index": shot_index, "chain_pos": 0}
                break

        video_node = self._build_video_node_for_shot(
            shot=shot,
            built=built,
            deps=[keyframe_node_id],  # 视频依赖关键帧
            index=shot_index,
            base_pos=base_pos,
            first_frame_override=kf_image,
            chain_meta=chain_meta,
        )

        await self._notify("new_node", {
            "message": f"✅ 关键帧已确认，开始生成视频 {shot.get('shot_id', '')}...",
            "node": video_node,
        })

        # W5：专家模式不在"确认 keyframe"时自动跑 video —— 留给 batch chain-runner 顺序驱动；
        # 非专家（简易/进阶）维持原"确认即自动跑"。
        _proj = self.state.get_project(self.project_id) or {}
        _cfg = _proj.get("config") or {}
        is_expert = bool(_cfg.get("expert_seeded") or _cfg.get("series_id") or _proj.get("series_id"))
        if not is_expert:
            asyncio.create_task(self._execute_node(video_node["id"]))

        return {
            "type": "new_node",
            "node": video_node,
            "phase": "video",
        }

    def _find_storyboard_node(self) -> Optional[Dict[str, Any]]:
        for n in self.state.get_all_nodes(self.project_id):
            if n.get("type") == "storyboard_design":
                return n
        return None

    # ─── @ 引用解析与 video payload 组装 ───

    @staticmethod
    def _build_video_payload(
        shot: Dict,
        character_refs: List[Dict],
        scene_refs: List[Dict],
        prop_refs: Optional[List[Dict]] = None,  # 0.6.4
    ) -> Dict[str, Any]:
        """根据分镜和角色/场景/道具库，组装视频生成所需的最终参数。

        Returns:
            {
                "prompt": "替换 @ 引用后的英文 prompt",
                "first_frame_url": "/output/images/.png" 或 None,
                "extra_deps": [被引用的子节点 id 列表],
                "referenced_characters": [{name, outfit_name, prompt, image_url, composite_reference_url, three_view_image_url, node_id}],
                "referenced_scenes":     [{location, prompt, image_url, node_id}],
                "referenced_props":      [{name, prompt, image_url, node_id}],  # 0.6.4
            }
        """
        import re
        import difflib
        prop_refs = prop_refs or []

        def _aliases(name: str) -> List[str]:
            """把 "麻糬 / Mochi" 拆成 ["麻糬 / Mochi", "麻糬", "Mochi"]，方便 LLM 用任意一个变体引用。
            0.7.0 #1: ·・• 分隔 + 剥离括号注释，episode 角色名 "小峰（林悦老公） · 颓败归家装" 拆出 "小峰"，@小峰 也能挂上图。"""
            if not name:
                return []
            base = name.strip()
            out: List[str] = []
            for cand in [base] + [p.strip() for p in re.split(r"[/／,，、·・•]", base) if p.strip()]:
                if cand and cand not in out:
                    out.append(cand)
                stripped = re.sub(r"[（(][^）)]*[）)]", "", cand).strip()
                if stripped and stripped not in out:
                    out.append(stripped)
            return out

        # 把每个角色/场景/道具注册到所有别名下
        char_by_alias: Dict[str, Dict] = {}
        for c in character_refs:
            for a in _aliases(c.get("name") or ""):
                char_by_alias.setdefault(a, c)
        scene_by_alias: Dict[str, Dict] = {}
        for s in scene_refs:
            for a in _aliases(s.get("location") or ""):
                scene_by_alias.setdefault(a, s)
        # 0.7.5.1：同一物理地点常被美术拆成多个场景节点（各带各的机位板），而 @场景 别名 setdefault
        # 只认第一个节点 → 分镜要的板可能长在够不着的兄弟节点上（@酒店包厢#周大牛身后视角 回退建立镜的根因）。
        # 解法：把**同 location 的所有机位板汇成一个池**，@场景#机位 跨节点在池里找。
        cameras_by_location: Dict[str, List[Dict]] = {}
        for s in scene_refs:
            loc = s.get("location") or ""
            if not loc:
                continue
            for cam in (s.get("cameras") or []):
                if isinstance(cam, dict) and cam.get("camera_name"):
                    cameras_by_location.setdefault(loc, []).append({**cam, "_node_id": s.get("node_id")})
        prop_by_alias: Dict[str, Dict] = {}
        for p in prop_refs:
            for a in _aliases(p.get("name") or ""):
                prop_by_alias.setdefault(a, p)

        # 视频生成优先用 video_prompt（结构化：景别/运镜/光线/动作 + 对白，专为视频设计，@引用更全），
        # visual_prompt 兜底。本函数只给 video 节点用（keyframe 另走 visual_prompt 路径），故翻转安全。
        raw = (shot.get("video_prompt") or shot.get("visual_prompt") or "").strip()

        referenced_characters: List[Dict] = []
        referenced_scenes: List[Dict] = []
        referenced_props: List[Dict] = []  # 0.6.4
        extra_deps: List[str] = []
        seen_char_nodes: set = set()
        seen_scene_nodes: set = set()
        seen_prop_nodes: set = set()

        # 匹配 @<名字>。名字可能含空格 / 斜杠（如 "The Jumper" / "Locker room / preparation zone"），
        # 单纯 \w+ 会切到第一个空格就停，导致后端只识别 "@The"。
        # 解决：把所有已注册名（含别名）按长度倒序拼成 alternation 优先匹配，
        # fallback 退化为单词正则。
        all_known_names = sorted(
            set(char_by_alias.keys()) | set(scene_by_alias.keys()) | set(prop_by_alias.keys()),
            key=len, reverse=True,
        )
        # 0.7.1 ①：可选 # 后缀 → group(2)。@林悦#睡袍 选服装；0.7.2：@纸巾被子#脏污 选道具状态；不带 # 用默认(第一个)
        outfit_suffix = r"(?:#([^\s,，。、()（）@#]+))?"
        if all_known_names:
            known_alt = "|".join(re.escape(n) for n in all_known_names)
            pattern = re.compile(rf"@({known_alt}|[\w一-鿿]+){outfit_suffix}")
        else:
            pattern = re.compile(rf"@([\w一-鿿]+){outfit_suffix}")

        def _norm_outfit(s: str) -> str:
            return re.sub(r"[（(][^）)]*[）)]", "", (s or "")).replace(" ", "").strip()

        def _match_variant(variants: List[Dict], key: str, q: str) -> Optional[Dict]:
            """0.7.2：通用变体匹配链（服装/道具状态共用）：精确/规范化相等 → 子串 → difflib≥0.6 → None。"""
            nq = _norm_outfit(q)
            for o in variants:  # 1) 精确 / 规范化相等
                on = o.get(key) or ""
                if on == q or (nq and _norm_outfit(on) == nq):
                    return o
            for o in variants:  # 2) 子串
                on = _norm_outfit(o.get(key) or "")
                if nq and (nq in on or (on and on in nq)):
                    return o
            best, best_r = None, 0.0  # 3) 最相似 ≥ 0.6
            for o in variants:
                r = difflib.SequenceMatcher(None, nq, _norm_outfit(o.get(key) or "")).ratio()
                if r > best_r:
                    best, best_r = o, r
            if best is not None and best_r >= 0.6:
                return best
            return None

        def _pick_outfit(ref: Dict, outfit_q: Optional[str]) -> Dict[str, Any]:
            """从角色 ref 选一套服装的字段集（node_id/image_url/composite/三视图/prompt/outfit_name）。
            无 outfits（master/旧 refs/非专家）或选不中 → 回退顶层/第一套。"""
            def _fields(src: Dict, outfit_name=None) -> Dict[str, Any]:
                return {
                    "node_id": src.get("node_id") or ref.get("node_id"),
                    "image_url": src.get("image_url"),
                    "composite_reference_url": src.get("composite_reference_url"),
                    "three_view_image_url": src.get("three_view_image_url"),
                    "prompt": src.get("prompt", ""),
                    "outfit_name": outfit_name,
                    "face_close_up_url": src.get("face_close_up_url") or ref.get("face_close_up_url"),  # 0.7.3 关键帧优先喂单张正脸特写
                    # 0.7.3：选中套的 asset://火山id（专家分集每套独立入库；进阶/master src=ref 取角色级）
                    "character_asset_id": src.get("character_asset_id") or ref.get("character_asset_id"),
                }
            outfits = ref.get("outfits") or []
            if not outfits:
                return _fields(ref)
            if not outfit_q:
                if len(outfits) > 1:
                    logger.info("storyboard @%s 未指定服装，默认第一套「%s」（共 %d 套）",
                                ref.get("name"), outfits[0].get("outfit_name"), len(outfits))
                return _fields(outfits[0], outfits[0].get("outfit_name"))
            hit = _match_variant(outfits, "outfit_name", outfit_q)
            if hit is not None:
                return _fields(hit, hit.get("outfit_name"))
            logger.warning("storyboard @%s#%s 未匹配到服装，回退第一套「%s」",
                           ref.get("name"), outfit_q, outfits[0].get("outfit_name"))
            return _fields(outfits[0], outfits[0].get("outfit_name"))

        def _pick_prop_state(ref: Dict, state_q: Optional[str]) -> Dict[str, Any]:
            """0.7.2：从道具 ref 选一个状态（node_id/image_url/prompt/state_name）。
            无 states（master 单态/旧 refs）或选不中 → 回退顶层/第一态。"""
            def _fields(src: Dict, state_name=None) -> Dict[str, Any]:
                return {
                    "node_id": src.get("node_id") or ref.get("node_id"),
                    "image_url": src.get("image_url"),
                    "prompt": src.get("prompt", ""),
                    "state_name": state_name,
                }
            states = ref.get("states") or []
            if not states:
                return _fields(ref, ref.get("_state_name"))
            if not state_q:
                if len(states) > 1:
                    logger.info("storyboard @%s 未指定道具状态，默认第一态「%s」（共 %d 态）",
                                ref.get("name"), states[0].get("state_name"), len(states))
                return _fields(states[0], states[0].get("state_name"))
            hit = _match_variant(states, "state_name", state_q)
            if hit is not None:
                return _fields(hit, hit.get("state_name"))
            logger.warning("storyboard @%s#%s 未匹配到道具状态，回退第一态「%s」",
                           ref.get("name"), state_q, states[0].get("state_name"))
            return _fields(states[0], states[0].get("state_name"))

        def _pick_camera(ref: Dict, cam_q: Optional[str]) -> Dict[str, Any]:
            """0.7.5：从场景选一个机位板（image_url/camera_name/prompt/node_id）。镜像 _pick_outfit。
            0.7.5.1：**跨同名地点汇池**——@场景#机位 在该 location 所有节点的板里找（板常分散在兄弟节点）。
            不带#机位/池里选不中 → 回退**本节点**建立镜(board0)或顶层 image_url。"""
            cameras = ref.get("cameras") or []
            def _establish() -> Dict[str, Any]:
                if cameras:
                    b0 = cameras[0]
                    return {"image_url": b0.get("image_url") or ref.get("image_url"),
                            "camera_name": b0.get("camera_name"),
                            "prompt": b0.get("prompt") or ref.get("prompt", ""),
                            "node_id": ref.get("node_id")}
                return {"image_url": ref.get("image_url"), "camera_name": None,
                        "prompt": ref.get("prompt", ""), "node_id": ref.get("node_id")}
            if not cam_q:
                return _establish()
            pool = cameras_by_location.get(ref.get("location") or "") or [
                {**c, "_node_id": ref.get("node_id")} for c in cameras if c.get("camera_name")
            ]
            # 剔除各节点的"建立镜"(board0，都同名)，避免 @X#某反打 被 difflib 误配到建立镜
            angle_pool = [c for c in pool if c.get("camera_name") and c.get("camera_name") != "建立镜"] or pool
            hit = _match_variant(angle_pool, "camera_name", cam_q)
            if hit is not None:
                return {"image_url": hit.get("image_url") or ref.get("image_url"),
                        "camera_name": hit.get("camera_name"),
                        "prompt": hit.get("prompt") or ref.get("prompt", ""),
                        "node_id": hit.get("_node_id") or ref.get("node_id")}
            logger.warning("storyboard @%s#%s 未匹配到机位（跨 %d 块同地点板），回退建立镜",
                           ref.get("location"), cam_q, len(pool))
            return _establish()

        def replace(match: "re.Match[str]") -> str:
            name = match.group(1)
            outfit_q = match.group(2)  # 0.7.1 ①：#服装（可能 None）
            if name in char_by_alias:
                ref = char_by_alias[name]
                chosen = _pick_outfit(ref, outfit_q)
                node_id = chosen.get("node_id")
                if node_id and node_id not in seen_char_nodes:
                    seen_char_nodes.add(node_id)
                    referenced_characters.append({
                        "name": ref.get("name") or name,
                        "outfit_name": chosen.get("outfit_name"),
                        "node_id": node_id,
                        "image_url": chosen.get("image_url"),
                        # keyframe 取图优先级 composite>三视图>正面，必须带上选中套的 composite/三视图
                        "composite_reference_url": chosen.get("composite_reference_url"),
                        "three_view_image_url": chosen.get("three_view_image_url"),
                        "face_close_up_url": chosen.get("face_close_up_url"),  # 0.7.3 关键帧优先喂单张正脸特写
                        "prompt": chosen.get("prompt", ""),
                        "character_asset_id": chosen.get("character_asset_id"),  # 0.7.3 用**选中那套 outfit** 的 asset://火山id
                    })
                    extra_deps.append(node_id)
                desc = (chosen.get("prompt") or "").strip()
                desc = re.sub(
                    r",?\s*(isolated on pure white background|studio lighting,?\s*no environment|no shadows on background|character design reference sheet)[^,]*",
                    "",
                    desc,
                    flags=re.IGNORECASE,
                ).strip().rstrip(",")
                return f"({desc})" if desc else ""
            if name in scene_by_alias:
                ref = scene_by_alias[name]
                cam = _pick_camera(ref, outfit_q)  # 0.7.5 outfit_q=group2=#机位；0.7.5.1 跨同名地点池选板
                node_id = cam.get("node_id") or ref.get("node_id")  # 0.7.5.1 板所属节点（可能是兄弟节点）
                if node_id and node_id not in seen_scene_nodes:
                    seen_scene_nodes.add(node_id)
                    referenced_scenes.append({
                        "location": ref.get("location") or name,
                        "node_id": node_id,
                        "image_url": cam.get("image_url"),          # 0.7.5 选中机位板图（回退建立镜）
                        "camera_name": cam.get("camera_name"),      # 0.7.5 机位名（None=建立镜）
                        "prompt": cam.get("prompt") or ref.get("prompt", ""),
                    })
                    extra_deps.append(node_id)
                desc = (ref.get("prompt") or "").strip()
                desc = re.sub(
                    r",?\s*(no characters|no people|no animals|empty scene)[^,]*",
                    "",
                    desc,
                    flags=re.IGNORECASE,
                ).strip().rstrip(",")
                return f"({desc})" if desc else ""
            if name in prop_by_alias:
                # 0.6.4 道具引用 + 0.7.2 @道具#状态：多状态按 # 后缀选态，不带 # 用默认(第一态)
                ref = prop_by_alias[name]
                chosen_ps = _pick_prop_state(ref, outfit_q)
                node_id = chosen_ps.get("node_id")
                if node_id and node_id not in seen_prop_nodes:
                    seen_prop_nodes.add(node_id)
                    referenced_props.append({
                        "name": ref.get("name") or name,
                        "state_name": chosen_ps.get("state_name"),
                        "node_id": node_id,
                        "image_url": chosen_ps.get("image_url"),
                        "prompt": chosen_ps.get("prompt", ""),
                    })
                    extra_deps.append(node_id)
                desc = (chosen_ps.get("prompt") or "").strip()
                desc = re.sub(
                    r",?\s*(isolated on pure white background|studio lighting,?\s*no environment|no characters|no people|product reference shot|centered composition)[^,]*",
                    "",
                    desc,
                    flags=re.IGNORECASE,
                ).strip().rstrip(",")
                return f"({desc})" if desc else ""
            return f"@{name}"  # 未匹配，原样保留

        prompt = pattern.sub(replace, raw)
        prompt = re.sub(r"\s+", " ", prompt)
        prompt = re.sub(r",\s*,", ",", prompt).strip().strip(",")

        # 0.7.1：**关键帧节点单独的 prompt** —— 用 visual_prompt（静态构图，无 c01 分段/运镜/对白），
        # 视频节点才用上面的 video prompt。二者共用 referenced_*（融合图一致）。visual_prompt 空时退回 video prompt。
        _kf_raw = (shot.get("visual_prompt") or "").strip()
        if _kf_raw:
            keyframe_prompt = pattern.sub(replace, _kf_raw)
            keyframe_prompt = re.sub(r"\s+", " ", keyframe_prompt)
            keyframe_prompt = re.sub(r",\s*,", ",", keyframe_prompt).strip().strip(",")
        else:
            keyframe_prompt = prompt

        # 0.6.2: 把对白/旁白/音效附加到 prompt 末尾，让 Seedance / HappyHorse 自动做对口型 + 环境音
        # 中文场景下用中文短句拼接（Seedance 支持中英混合）；
        # 视频模型按此提示生成角色说话动作（lip-sync）和氛围音效
        dialogue = (shot.get("dialogue") or "").strip()
        narration = (shot.get("narration") or "").strip()
        sfx = shot.get("sound_effects") or []
        audio_hints: List[str] = []
        # 三类音轨内容优先由 ENRICH 内联（对白嵌动作 "…说道<台词>"、旁白、环境音效）；
        # ENRICH 没写时才在此兜底补，靠关键词去重避免重复
        if dialogue and "说道<" not in prompt and "说道「" not in prompt:
            audio_hints.append(f"角色对白：{dialogue.replace(chr(10), ' ').strip()}（请生成对应口型与说话动作）")
        if narration and "旁白" not in prompt:
            audio_hints.append(f"画外旁白：{narration}")
        if isinstance(sfx, list) and sfx and "环境音效" not in prompt:
            sfx_str = "、".join(str(s) for s in sfx if s)
            if sfx_str:
                audio_hints.append(f"环境音效：{sfx_str}")
        if audio_hints:
            hint_str = " ".join(audio_hints)
            # 0.7.1：对白/旁白/音效放到"该放的位置"——动作描述之后、结尾"无字幕"元指令之前，
            # 而非整段最末（参照专业模板：对白在结构内，不 bolt 在【字幕】标记后面）
            m = re.search(r"\s*(【[^】]*字幕[^】]*】)\s*$", prompt)
            if m:
                prompt = prompt[:m.start()].rstrip("。.，, ").rstrip() + "。" + hint_str + " " + m.group(1)
            else:
                prompt = prompt.rstrip(", ").rstrip() + ". " + hint_str

        # first_frame 选取顺序：
        # 1) shot 里实际 @ 引用到的场景（最精确）
        # 2) 用 shot.scene_id 匹配 scene_refs（精准 ID 兜底）
        # 3) 用 shot 里 @ 引用的角色图（拿不到场景就用主体）
        # 4) 都没有 → None（走 text_to_video）
        first_frame_url = None
        first_frame_source = None

        for s in referenced_scenes:
            if s.get("image_url"):
                first_frame_url = s["image_url"]
                first_frame_source = "referenced_scene"
                break

        if not first_frame_url:
            shot_scene_id = (shot.get("scene_id") or "").strip()
            if shot_scene_id:
                for s in scene_refs:
                    if s.get("scene_id") == shot_scene_id and s.get("image_url"):
                        first_frame_url = s["image_url"]
                        first_frame_source = f"matched_scene_id={shot_scene_id}"
                        if s.get("node_id") and s["node_id"] not in seen_scene_nodes:
                            extra_deps.append(s["node_id"])
                            seen_scene_nodes.add(s["node_id"])
                            referenced_scenes.append({
                                "location": s.get("location", ""),
                                "node_id": s.get("node_id"),
                                "image_url": s.get("image_url"),
                                "prompt": s.get("prompt", ""),
                                "auto_matched_by": "scene_id",
                            })
                        break

        if not first_frame_url:
            for c in referenced_characters:
                if c.get("image_url"):
                    first_frame_url = c["image_url"]
                    first_frame_source = "referenced_character"
                    break

        # ENRICH 失败/漏用 @ 引用时补登 referenced_*。**关键：按本镜 shot.characters / shot.scene 选**，
        # 绝不能把整集 cast / 全部场景都塞进来 —— 否则别镜的角色（如小峰）会乱入本镜关键帧融合，人物全错。
        if not referenced_characters and character_refs:
            seen_inj: set = set()
            for nm in (shot.get("characters") or []):
                base = str(nm).split("#", 1)[0].strip()
                ref = char_by_alias.get(base) or char_by_alias.get(str(nm).strip())
                nid = ref.get("node_id") if ref else None
                if ref and nid not in seen_inj:
                    seen_inj.add(nid)
                    referenced_characters.append({
                        "name": ref.get("name", "") or base,
                        "node_id": nid,
                        "image_url": ref.get("image_url"),
                        "composite_reference_url": ref.get("composite_reference_url"),
                        "three_view_image_url": ref.get("three_view_image_url"),
                        "face_close_up_url": ref.get("face_close_up_url"),  # 0.7.3 关键帧优先喂单张正脸特写
                        "prompt": ref.get("prompt", ""),
                        "character_asset_id": ref.get("character_asset_id"),  # 0.7.3 兜底：默认套 asset://火山id
                        "auto_injected": True,
                    })
        if not referenced_scenes and scene_refs:
            shot_scene = str(shot.get("scene") or "").strip()
            chosen = (scene_by_alias.get(shot_scene) if shot_scene else None) or scene_refs[0]
            referenced_scenes.append({
                "location": chosen.get("location", ""),
                "node_id": chosen.get("node_id"),
                "image_url": chosen.get("image_url"),
                "prompt": chosen.get("prompt", ""),
                "auto_injected": True,
            })

        return {
            "prompt": prompt,
            "keyframe_prompt": keyframe_prompt,   # 关键帧专用（visual_prompt 静态构图）
            "first_frame_url": first_frame_url,
            "first_frame_source": first_frame_source,
            "extra_deps": list(dict.fromkeys(extra_deps)),
            "referenced_characters": referenced_characters,
            "referenced_scenes": referenced_scenes,
            "referenced_props": referenced_props,  # 0.6.4
        }

    # ═══════════════════════════════════════════════════════
    # art 主节点确认 → fan-out 子节点；所有子节点确认 → 询问进入分镜
    # ═══════════════════════════════════════════════════════

    async def _after_art_main_confirm(self, node_id: str) -> Dict[str, Any]:
        """主 art_design 节点确认：
        - 首次确认 → 生成所有角色/场景图子节点（_spawn_art_children）
        - 已经有子节点（用户二次点确认）→ 检查是否全部确认 → 视情况弹对话
        """
        node = self.state.get_node(self.project_id, node_id) or {}
        if node.get("children"):
            return await self._check_art_assets_all_confirmed(node_id)
        return await self._spawn_art_children(node_id)

    async def _spawn_art_children(self, node_id: str) -> Dict[str, Any]:
        """根据 art_design 节点 result 中的 characters/scenes 列表创建并执行图片子节点。

        与旧 _post_art 的内容相同，但触发点改为用户对主节点的确认，给用户先审阅
        art_direction（角色/场景分得对不对）的机会。
        """
        node = self.state.get_node(self.project_id, node_id) or {}
        art_data = node.get("result") or {}
        parent_pos = node.get("position", {"x": 400, "y": 200})

        characters = art_data.get("characters", []) or []
        scenes = art_data.get("scenes", []) or []
        props = art_data.get("props", []) or []  # 0.6.4: 关键道具
        # 0.7.1：系列/专家项目（总项目美术）改成**手动批量生成** —— 只建 IDLE 占位子节点、不自动出图，
        # 由前端「🎨 生成总项目美术」批量按钮触发（和分集美术一致；配合智谱串行限速避免一次性 ~12 张图打爆 429 降 mock）。
        series_id = await self._get_series_id_for_project()
        gen = not bool(series_id)
        # 0.6.3+: 全局共享 Semaphore，character + scene + prop 协调一个上限 2（仅 gen=True 自动出图时用）
        shared_sem = asyncio.Semaphore(2)
        char_imgs = await self._generate_subject_images(
            node_id, "character", characters, parent_pos, 0, sem=shared_sem, generate=gen,
        )
        scene_imgs = await self._generate_subject_images(
            node_id, "scene", scenes, parent_pos, len(characters), sem=shared_sem, generate=gen,
        )
        prop_imgs = await self._generate_subject_images(
            node_id, "prop", props, parent_pos, len(characters) + len(scenes), sem=shared_sem, generate=gen,
        )

        result_children = (
            [c["node_id"] for c in char_imgs]
            + [s["node_id"] for s in scene_imgs]
            + [p["node_id"] for p in prop_imgs]
        )
        self.state.update_node(self.project_id, node_id, {
            "outputs": {
                **(node.get("outputs") or {}),
                "character_designs": [c["data"] for c in char_imgs],
                "scene_styles": [s["data"] for s in scene_imgs],
                "prop_designs": [p["data"] for p in prop_imgs],  # 0.6.4
                "result_children": result_children,
            },
            "children": result_children,
            "connection_points": {
                "results": [
                    {"id": f"result_{i}", "label": f"角色: {c['data']['name']}"}
                    for i, c in enumerate(char_imgs)
                ] + [
                    {"id": f"result_{len(char_imgs)+i}", "label": f"场景: {s['data']['location']}"}
                    for i, s in enumerate(scene_imgs)
                ] + [
                    {"id": f"result_{len(char_imgs)+len(scene_imgs)+i}", "label": f"道具: {p['data']['name']}"}
                    for i, p in enumerate(prop_imgs)
                ],
            },
        })

        prop_msg = f" / {len(prop_imgs)} 个道具" if prop_imgs else ""
        if gen:
            spawn_msg = (
                f"✅ 美术设定已确认！已生成 {len(char_imgs)} 个角色 / {len(scene_imgs)} 个场景{prop_msg} 的图片，"
                "请逐个确认后再进入分镜阶段。"
            )
        else:
            spawn_msg = (
                f"✅ 美术设定已确认！列出 {len(char_imgs)} 个角色 / {len(scene_imgs)} 个场景{prop_msg}，"
                "点画布右上角「🎨 生成总项目美术」批量生成（生成完逐个确认再进分镜）。"
            )
        await self._notify("art_children_spawned", {"message": spawn_msg})

        # 兜底：art_data 完全没角色没场景 → 直接弹"开始分镜"
        if not result_children:
            await self._emit_art_all_confirmed()

        return {
            "type": "art_children_spawned",
            "node_id": node_id,
            "children_count": len(result_children),
        }

    async def _after_art_child_confirm(self, node_id: str) -> Dict[str, Any]:
        """单个 art 子节点（角色/场景图）确认后，检查所有 siblings 是否全部已确认。"""
        return await self._check_art_assets_all_confirmed(node_id)

    async def _after_episode_art_main_confirm(self, node_id: str) -> Dict[str, Any]:
        """episode_art_design 主节点 confirm 后：
        - 首次确认（没 children）→ spawn 子节点（异步出图）
        - 已有 children → 转交 _check 检查全确认
        """
        node = self.state.get_node(self.project_id, node_id) or {}
        children = node.get("children") or []
        if not children:
            # 首次 confirm → spawn
            asyncio.create_task(self._spawn_episode_art_children(node_id))
            return {
                "type": "node_confirmed",
                "node_id": node_id,
                "message": "✅ 本集美术骨架已确认，开始按 outfit / 变化 / 状态出图...",
            }
        # 已经 spawn 过 → 检查子节点是否全 confirmed
        all_confirmed = all(
            (self.state.get_node(self.project_id, cid) or {}).get("status") == NodeStatus.CONFIRMED
            for cid in children
        )
        if all_confirmed:
            await self._notify("art_assets_all_confirmed", {
                "message": "✅ 本集美术全部确认！是否开始分镜设计？",
                "need_dialog_confirm": True,
                "confirm_action": "start_storyboard",
                "cancel_action": "stay_in_art_phase",
            })
        return {
            "type": "episode_art_main_confirmed",
            "node_id": node_id,
            "all_confirmed": all_confirmed,
        }

    async def _after_episode_art_child_confirm(self, node_id: str) -> Dict[str, Any]:
        """0.7.0 轮 1：episode 子节点 confirm → 检查 siblings 全 CONFIRMED → 启动 storyboard。
        逻辑跟 _check_art_assets_all_confirmed 类似，但找的是 episode_art_design 主节点。
        """
        node = self.state.get_node(self.project_id, node_id) or {}
        art_main = None
        for dep_id in node.get("depends_on", []) or []:
            dep = self.state.get_node(self.project_id, dep_id) or {}
            if dep.get("type") == "episode_art_design":
                art_main = dep
                break
        if not art_main:
            return {"type": "episode_art_child_confirmed", "node_id": node_id, "all_confirmed": False}

        children_ids = art_main.get("children") or []
        all_confirmed = bool(children_ids) and all(
            (self.state.get_node(self.project_id, cid) or {}).get("status") == NodeStatus.CONFIRMED
            for cid in children_ids
        )

        if all_confirmed:
            # 0.7.3 人物一致性：分集美术全确认 → 每套 outfit 入 asset 库（借主美术脸 + 锁本套衣服 → asset://）。
            # 专家分集之前漏了这一步(只 master 的 _check_art_assets_all_confirmed 会入库)，导致分集角色无 asset://。
            asyncio.create_task(self._register_all_character_assets(children_ids))
            await self._notify("art_assets_all_confirmed", {
                "message": "✅ 本集美术全部确认！是否开始分镜设计？",
                "need_dialog_confirm": True,
                "confirm_action": "start_storyboard",
                "cancel_action": "stay_in_art_phase",
            })

        return {
            "type": "episode_art_child_confirmed",
            "node_id": node_id,
            "all_confirmed": all_confirmed,
        }

    async def _register_all_character_assets(self, child_ids: List[str]) -> None:
        """0.7.3 人物一致性：角色/分集服装定稿后批量入 asset 库（火山虚拟人像，asset:// 锁脸+锁服装）。
        仅 antchain + human；入库慢→异步、**并行**、一次性；asset_id 回写子节点 result。
        - character(进阶/master 基础设定)：图源=脸+三视图 竖排 composite
        - ep_character(专家分集每套 outfit)：图源=**主美术脸 + 本套新衣服三视图** 竖排 composite
          （用户方案：主美术不单独入库，每套分集服装各入一个 asset://=借主美术的脸+锁本套衣服）。"""
        try:
            from app.core.runtime_config import active_video_provider_name
            if active_video_provider_name() != "antchain":
                return  # 只有蚂蚁通道走 asset 锁身份
        except Exception:
            return
        try:
            from app.modules._antchain_video import register_character_asset
            from app.core.paths import compose_character_reference
        except Exception:
            return
        import asyncio as _aio
        _sem = _aio.Semaphore(4)  # 限并发，避免一集多套服装并行压垮蚂蚁网关

        async def _reg_one(cid: str) -> None:
            node = self.state.get_node(self.project_id, cid) or {}
            res = node.get("result") or {}
            rtype = res.get("result_type")
            if rtype not in ("character", "ep_character") or res.get("character_asset_id"):
                return
            if (res.get("subject_type") or "human").lower() != "human":
                return  # 非人物不做人脸锚定
            # 图源：实时重拼竖排 composite（宽高比0.64过火山校验、走 OSS 不怕过期）。
            if rtype == "ep_character":
                # 分集每套 outfit：借主美术的脸 + 本套新衣服三视图 → 锁脸+锁本套服装
                _pm = res.get("parent_master") or {}
                _face = _pm.get("face_close_up_url") or res.get("face_close_up_url") or _pm.get("image_url")
                _tv = res.get("three_view_image_url")  # 本套 outfit 三视图（新衣服）
                _label = res.get("name") or "服装"
            else:
                _face = res.get("face_close_up_url") or res.get("image_url")
                _tv = res.get("three_view_image_url")
                _label = res.get("name") or "角色"
            img = None
            if _face and _tv:
                try:
                    img = compose_character_reference(_face, _tv)  # 竖排
                except Exception as _ce:
                    logger.warning("重拼竖排 composite 失败 %s: %s", _label, _ce)
            img = (img or res.get("composite_reference_url")
                   or res.get("face_close_up_origin_url")
                   or res.get("three_view_origin_url") or res.get("origin_url"))
            if not img:
                logger.info("「%s」无可用图，跳过 asset 入库", _label)
                return
            logger.info("「%s」(%s) asset 入库图源: %s", _label, rtype, str(img)[:80])
            async with _sem:
                aid = await register_character_asset(img, _label)
            if aid:
                _cur = (self.state.get_node(self.project_id, cid) or {}).get("result") or res
                self.state.update_node(self.project_id, cid, {"result": {**_cur, "character_asset_id": aid}})
                try:
                    await self._notify("node_updated", {"node_id": cid, "node": self.state.get_node(self.project_id, cid)})
                except Exception:
                    pass
                logger.info("「%s」已入 asset 库 asset_id=%s", _label, aid)

        # 并行入库（多角色/多 outfit 同时跑，Semaphore 限 4，别串行干等 16s×N）
        await _aio.gather(*[_reg_one(cid) for cid in (child_ids or [])], return_exceptions=True)

    async def _check_art_assets_all_confirmed(self, ref_node_id: str) -> Dict[str, Any]:
        """从主节点或子节点都能进来，定位到 art_design 主节点，看 children 是否全部 CONFIRMED。"""
        node = self.state.get_node(self.project_id, ref_node_id) or {}
        if node.get("type") == "art_design":
            art_main = node
        else:
            art_main = None
            for dep_id in node.get("depends_on", []) or []:
                dep = self.state.get_node(self.project_id, dep_id) or {}
                if dep.get("type") == "art_design":
                    art_main = dep
                    break

        if not art_main:
            return {"type": "art_child_confirmed", "node_id": ref_node_id, "all_confirmed": False}

        children_ids = art_main.get("children", []) or []
        all_confirmed = bool(children_ids) and all(
            (self.state.get_node(self.project_id, cid) or {}).get("status") == NodeStatus.CONFIRMED
            for cid in children_ids
        )

        if all_confirmed:
            # 0.7.3 人物一致性：角色定稿 → 异步入 asset 库（火山私域库人脸校验，锁跨镜头/集身份）
            asyncio.create_task(self._register_all_character_assets(children_ids))
            # 0.7.0：在 master project 上 art 全 confirm → 推 series_assets_frozen（让前端亮"开始制作分集"按钮）
            # 普通项目 → 走标准 art_assets_all_confirmed dialog（"开始分镜"）
            series_id = await self._get_series_id_for_project()
            if series_id and not await self._is_episode_project():
                await self._freeze_series_assets(series_id)
            else:
                await self._emit_art_all_confirmed()

        return {
            "type": "art_child_confirmed",
            "node_id": ref_node_id,
            "all_confirmed": all_confirmed,
        }

    async def _emit_art_all_confirmed(self) -> None:
        await self._notify("art_assets_all_confirmed", {
            "message": "✅ 所有角色和场景物料已确认！是否开始分镜设计？",
            "need_dialog_confirm": True,
            "confirm_action": "start_storyboard",
            "cancel_action": "stay_in_art_phase",
        })

    async def _is_episode_project(self) -> bool:
        """DB 查 projects.is_episode（用于区分 master vs episode 子项目）"""
        from sqlalchemy import select
        from app.core.database import async_session
        from app.models.project import Project as ProjectModel
        try:
            async with async_session() as db:
                row = await db.execute(
                    select(ProjectModel.is_episode).where(ProjectModel.id == self.project_id)
                )
                return bool(row.scalar_one_or_none())
        except Exception as e:
            logger.warning("_is_episode_project 失败：%s", e)
            return False

    async def _freeze_series_assets(self, series_id: str) -> None:
        """0.7.0：总项目 art 全 confirm → 把 art 主节点 + 角色/场景/道具 子节点结果打包冻结到 series.shared_assets。
        首次：推 series_assets_frozen → 前端"开始制作分集"按钮亮起。
        再次（用户改了 art 重 confirm）：推 series_assets_changed → 前端同步对话框。
        """
        # 先看是不是 re-freeze（已 frozen 过）
        prev_series = await self._load_series(series_id) or {}
        was_frozen = bool((prev_series.get("shared_assets") or {}).get("frozen_at"))
        # 找出 art_design 主节点
        art_main = None
        for n in self.state.get_all_nodes(self.project_id):
            if n.get("type") == "art_design" and n.get("status") in (NodeStatus.CONFIRMED, NodeStatus.COMPLETED):
                art_main = n  # 取最后一个 confirmed
        if not art_main:
            logger.warning("_freeze_series_assets: master project 没找到 confirmed 的 art_design")
            return

        # 收集子节点（characters / scenes / props）
        characters, scenes, props = [], [], []
        for cid in (art_main.get("children") or []):
            child = self.state.get_node(self.project_id, cid)
            if not child:
                continue
            res = child.get("result") or {}
            rtype = res.get("result_type")
            entry = {**res, "_source_node_id": cid}
            if rtype == "character":
                characters.append(entry)
            elif rtype == "scene":
                scenes.append(entry)
            elif rtype == "prop":
                props.append(entry)

        shared_assets = {
            "frozen_at": datetime.utcnow().isoformat(),
            "art_main_node_id": art_main["id"],
            "art_main_result": art_main.get("result") or {},
            "characters": characters,
            "scenes": scenes,
            "props": props,
        }
        await self._save_series_fields(series_id, {
            "shared_assets": shared_assets,
            # status 保持 visual_dev（资产已冻结但还没 fan-out），fanout_episodes 后才推到 producing
        })

        series = await self._load_series(series_id) or {}
        ep_count = len(series.get("episodes_breakdown") or [])

        if was_frozen:
            # 用户修改了 art 后重新 confirm → 推同步对话框事件
            existing_eps = await self._list_existing_episodes(series_id)
            await self._notify("series_assets_changed", {
                "series_id": series_id,
                "characters_count": len(characters),
                "scenes_count": len(scenes),
                "props_count": len(props),
                "existing_episodes_count": len(existing_eps),
                "message": f"🎨 总项目美术已更新，是否同步到 {len(existing_eps)} 个子集？",
            })
            logger.info("series %s assets re-frozen (changed): existing_eps=%d",
                        series_id, len(existing_eps))
        else:
            await self._notify("series_assets_frozen", {
                "series_id": series_id,
                "episode_count": ep_count,
                "characters_count": len(characters),
                "scenes_count": len(scenes),
                "props_count": len(props),
                "message": f"🎨 视觉开发完成！{len(characters)} 个角色 / {len(scenes)} 个场景 / {len(props)} 件道具已就绪，可以「开始制作 {ep_count} 集」",
            })
            logger.info("series %s assets frozen first time: chars=%d scenes=%d props=%d",
                        series_id, len(characters), len(scenes), len(props))

    # ─── 0.7.0 Step 10：Episode fan-out ───────────────────────────────

    async def _maybe_after_script_confirm_for_episode(
        self, script_node_id: str, script_node: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """如果当前项目是 Episode + series 有 shared_assets，
        script 确认后创建 episode_art_design 节点 + 立即执行。
        否则返回 None 让标准 _advance_to_next 接管。
        """
        if not await self._is_episode_project():
            return None
        series_id = await self._get_series_id_for_project()
        if not series_id:
            return None
        series = await self._load_series(series_id)
        if not series:
            return None
        shared = series.get("shared_assets") or {}
        if not shared.get("frozen_at"):
            logger.warning(
                "Episode %s script confirmed 但 master shared_assets 未冻结，按 advanced 标准流程走",
                self.project_id,
            )
            return None

        # 节点已存在 → 不重复创建（幂等）
        for n in self.state.get_all_nodes(self.project_id) or []:
            if n.get("type") == "episode_art_design":
                logger.info("episode_art_design 已存在，跳过创建")
                return {
                    "type": "node_confirmed",
                    "node_id": script_node_id,
                    "message": "剧本已确认（episode_art_design 已存在）",
                }

        # 创建 episode_art_design 节点（IDLE → 立即执行）
        prev_pos = script_node.get("position", {"x": 100, "y": 200})
        ep_art = self._create_phase_node(
            phase="episode_art",
            node_template_id="node_episode_art",
            depends_on=[script_node_id],
            position={"x": prev_pos.get("x", 100) + 300, "y": prev_pos.get("y", 200)},
            params={
                "script": script_node.get("result") or {},
                "series_id": series_id,
                "episode_index": (self.state.get_project(self.project_id) or {})
                                  .get("advanced_outline", {}).get("_episode_index"),
                # master shared_assets 整包传给 art_director（可在 prompt 里"细化"）
                "master_shared_assets": shared,
                "episode_outline": (self.state.get_project(self.project_id) or {})
                                    .get("advanced_outline") or {},
            },
        )

        await self._notify("new_node", {
            "message": "🎨 剧本确认！开始本集美术细化（在总美术基础上做服装、光线、道具状态变化）...",
            "node": ep_art,
        })
        asyncio.create_task(self._execute_node(ep_art["id"]))

        return {
            "type": "node_confirmed",
            "node_id": script_node_id,
            "next_node_id": ep_art["id"],
            "next_phase": "episode_art",
            "message": "剧本已确认，本集美术开始细化",
        }

    async def start_episode_script(self) -> Dict[str, Any]:
        """0.7.0：找到本项目 IDLE 状态的 script_generation 节点 → 启动执行。
        用于 sidebar 「▶ 开始制作」按钮：用户不用进 canvas 也能直接启动这一集。

        幂等：
        - 找不到 script 节点 → 404 error
        - 节点已 RUNNING/COMPLETED/CONFIRMED → 返回 already_started ack，不重复触发
        """
        script_node = None
        for n in self.state.get_all_nodes(self.project_id) or []:
            if n.get("type") == "script_generation":
                script_node = n
                break
        if not script_node:
            return {"error": "本项目没有 script_generation 节点"}

        status = script_node.get("status")
        if status in (NodeStatus.RUNNING, NodeStatus.COMPLETED, NodeStatus.CONFIRMED):
            return {
                "type": "already_started",
                "node_id": script_node["id"],
                "status": status,
                "message": f"剧本节点已是 {status} 状态，无需重复启动",
            }

        await self._notify("new_node", {
            "message": "📝 开始生成剧本...",
            "node": script_node,
        })
        asyncio.create_task(self._execute_node(script_node["id"]))
        return {
            "type": "episode_script_started",
            "node_id": script_node["id"],
            "message": "剧本生成已启动",
        }

    async def fanout_episodes(self) -> Dict[str, Any]:
        """从 master project 触发 fan-out — 为 series.episodes_breakdown 每集创建一个 Episode project。

        前提：series.shared_assets 已冻结。每集 pre-seeded：
        - script_node IDLE（用户进 episode canvas 后点确认开始细化剧本）
        - art_design + 子节点 CONFIRMED（共享 master 图片）
        """
        series_id = await self._get_series_id_for_project()
        if not series_id:
            return {"error": "当前项目未关联剧集"}
        if await self._is_episode_project():
            return {"error": "Episode 项目不能再 fan-out"}
        series = await self._load_series(series_id)
        if not series:
            return {"error": "剧集数据缺失"}
        shared = series.get("shared_assets") or {}
        if not shared.get("frozen_at"):
            return {"error": "美术资产尚未冻结，请先在总项目完成视觉开发并 confirm 全部子节点"}
        eps = series.get("episodes_breakdown") or []
        if not eps:
            return {"error": "尚无分集草案"}

        # 防重复：如果该 series 已经有 Episode 了，先报警告（前端可决定是否继续）
        existing = await self._list_existing_episodes(series_id)
        if existing:
            return {
                "error": f"该剧集已有 {len(existing)} 集子项目；如需重做请先删除再 fan-out。",
                "existing_episode_ids": [e["id"] for e in existing],
            }

        created: List[Dict[str, Any]] = []
        for i, ep_meta in enumerate(eps, 1):
            try:
                new_proj = await self._init_episode_from_series(series, ep_meta, i)
                created.append(new_proj)
            except Exception as e:
                logger.exception("Episode %d 创建失败：%s", i, e)

        await self._save_series_fields(series_id, {"status": "producing"})

        await self._notify("series_episodes_created", {
            "series_id": series_id,
            "count": len(created),
            "episode_ids": [p["id"] for p in created],
            "message": f"📺 已创建 {len(created)} 集子项目，可在左侧「剧集」tab 展开",
        })

        return {
            "type": "series_episodes_created",
            "series_id": series_id,
            "count": len(created),
            "episodes": created,
        }

    async def _list_existing_episodes(self, series_id: str) -> List[Dict[str, Any]]:
        from sqlalchemy import select
        from app.core.database import async_session
        from app.models.project import Project as ProjectModel
        async with async_session() as db:
            rows = (await db.execute(
                select(ProjectModel.id, ProjectModel.episode_index).where(
                    ProjectModel.series_id == series_id,
                    ProjectModel.is_episode == True,  # noqa: E712
                )
            )).all()
            return [{"id": r[0], "episode_index": r[1]} for r in rows]

    async def _init_episode_from_series(
        self,
        series: Dict[str, Any],
        ep_meta: Dict[str, Any],
        episode_index: int,
    ) -> Dict[str, Any]:
        """为单集创建 Project DB 行 + workflow_state in-mem + pre-create script(IDLE) only。

        0.7.0 轮 1 架构变更：不再 pre-confirm master art 子节点。
        - master shared_assets 是"参考库"，episode 通过 episode_art_design 节点拿来用 + 细化
        - script 节点 IDLE 等待用户在 sidebar 点 ▶ 或 canvas 编辑器里启动
        - script confirm 后会自动触发 episode_art_design 节点（见 _after_script_confirm_for_episode）
        """
        import uuid as _uuid
        from app.core.database import async_session
        from app.models.project import Project as ProjectModel

        series_id = series["id"]
        summary = series.get("script_summary") or {}

        ep_id = str(_uuid.uuid4())
        title = ep_meta.get("title") or f"第{episode_index}集"
        ep_name = f"第{episode_index}集 · {title}"
        ep_outline_logline = ep_meta.get("story_arc") or ""

        # 1. DB 建 Project 行
        async with async_session() as db:
            new_project = ProjectModel(
                id=ep_id,
                name=ep_name,
                description=ep_outline_logline,
                status="draft",
                config={"mode": "advanced", "expert_seeded": True, "series_id": series_id},
                series_id=series_id,
                is_episode=True,
                episode_index=episode_index,
            )
            db.add(new_project)
            await db.commit()

        # 2. workflow_state in-memory create
        self.state.create_project(ep_id, name=ep_name)

        # 0.7.0 轮 2：按 character_indices 只把本集出场角色给编剧（避免角色冗余）
        all_chars = summary.get("characters") or []
        char_indices = ep_meta.get("character_indices")
        if isinstance(char_indices, list) and char_indices:
            ep_chars_master = [all_chars[i] for i in char_indices if 0 <= i < len(all_chars)]
        else:
            ep_chars_master = all_chars  # 兜底：没指定就给全的

        # 0.7.0 轮 2：拼接本集对应 scenes 的原著 raw_passages，喂编剧"遵循原著"
        scene_indices = ep_meta.get("scene_indices") or []
        scene_outline = summary.get("scene_outline") or []
        raw_passages_block = []
        for i in scene_indices:
            if 0 <= i < len(scene_outline):
                s = scene_outline[i]
                passages = s.get("raw_passages") or []
                if passages:
                    raw_passages_block.append({
                        "scene_id": f"S{i + 1:02d}",
                        "chapter": s.get("chapter") or "",
                        "raw_passages": passages,
                    })
        # 限制 raw_passages 总长不超过 ~8000 字，避免编剧 prompt 过载
        total_raw_chars = sum(
            len(p) for blk in raw_passages_block for p in blk["raw_passages"]
        )
        if total_raw_chars > 8000:
            # 等比截断
            ratio = 8000 / total_raw_chars
            for blk in raw_passages_block:
                blk["raw_passages"] = [
                    p[: max(200, int(len(p) * ratio))] for p in blk["raw_passages"]
                ]

        ep_outline = {
            "title": title,
            "logline": ep_outline_logline,
            "tone": summary.get("tone") or "",
            "genre": summary.get("genre") or "",
            "characters": [
                {
                    "name": c.get("name"),
                    "role": c.get("role"),
                    "description": " · ".join(filter(None, [
                        c.get("personality") or "",
                        c.get("arc") or "",
                    ])) or (c.get("role") or ""),
                }
                for c in ep_chars_master
            ],
            "scenes": self._derive_episode_scenes(ep_meta, summary),
            "_from_expert_series": series_id,
            "_episode_index": episode_index,
            "_episode_total": len(series.get("episodes_breakdown") or []),
            "_scene_range": ep_meta.get("scene_range") or "",
            "_cliff_hanger": ep_meta.get("cliff_hanger") or "",
            # 0.7.0 轮 2：原著节选 — 编剧必须遵循（对白、关键场景描写）
            "_raw_passages": raw_passages_block,
        }

        est_min = int(ep_meta.get("est_duration_min") or 8)
        collected_info = {
            "aspect_ratio": summary.get("suggested_aspect_ratio") or "9:16",
            "duration": f"{est_min * 60}秒",
            "mood": summary.get("tone") or "",
            "style": "",
        }

        self.state.update_project(ep_id, {
            "advanced_outline": ep_outline,
            "collected_info": collected_info,
            "advanced_confirmed_at": datetime.utcnow().isoformat(),
        })
        self.state.set_project_phase(ep_id, ProjectPhase.PLAN_CONFIRMED)

        # 3. Pre-create script 节点（IDLE — sidebar ▶ 或编辑器里启动）
        script_spec = self.phases["script"]
        self.state.create_node(ep_id, {
            "node_id": "node_001",
            "type": script_spec.node_type,
            "label": script_spec.label,
            "position": {"x": 100, "y": 200},
            "params": {
                "topic": title,
                "outline": ep_outline,
                "duration": collected_info["duration"],
                "aspect_ratio": collected_info["aspect_ratio"],
                "mood": collected_info["mood"],
                "project_type": "advanced_outline",
            },
            "depends_on": [],
            "agent": script_spec.agent_name,
            "need_confirm": True,
            "phase": "script",
        })

        return {
            "id": ep_id,
            "name": ep_name,
            "episode_index": episode_index,
        }

    async def sync_episodes_assets(
        self,
        target: str = "all_unstarted",
        episode_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """0.7.0 Step 11：把 series.shared_assets 重新同步到指定 Episode 的 art 节点。

        target:
        - all_unstarted: 仅未开工的 Episode（除 script_node 外没 RUNNING/COMPLETED 节点）
        - all: 全部 Episode（含已开工，慎用）
        - selected: 指定 episode_ids
        """
        series_id = await self._get_series_id_for_project()
        if not series_id:
            return {"error": "当前项目未关联剧集"}
        series = await self._load_series(series_id)
        if not series:
            return {"error": "剧集数据缺失"}
        shared = series.get("shared_assets") or {}
        if not shared.get("frozen_at"):
            return {"error": "美术资产尚未冻结"}

        existing = await self._list_existing_episodes(series_id)
        if not existing:
            return {"error": "该剧集暂无子项目，无需同步"}

        target_ids: List[str]
        if target == "selected":
            target_ids = [e["id"] for e in existing if e["id"] in (episode_ids or [])]
        elif target == "all":
            target_ids = [e["id"] for e in existing]
        else:  # all_unstarted
            target_ids = [eid for eid in (e["id"] for e in existing)
                          if not self._episode_started(eid)]

        updated, skipped = [], []
        for ep_id in target_ids:
            try:
                if not self.state.get_project(ep_id):
                    # 内存里没有 → 跳过；下次切到该 episode 时会从 DB hydrate（暂不强行加载）
                    skipped.append({"id": ep_id, "reason": "not in memory"})
                    continue
                await self._overwrite_episode_art(ep_id, shared, series_id)
                updated.append(ep_id)
            except Exception as e:
                logger.warning("同步 episode %s 失败: %s", ep_id, e)
                skipped.append({"id": ep_id, "reason": str(e)[:80]})

        return {
            "type": "series_assets_synced",
            "series_id": series_id,
            "target": target,
            "updated": updated,
            "skipped": skipped,
            "message": f"已同步 {len(updated)} 集" + (f"，跳过 {len(skipped)} 集" if skipped else ""),
        }

    def _episode_started(self, ep_id: str) -> bool:
        """除 script_node 之外有 RUNNING/COMPLETED 节点 → 视为已开工。"""
        from app.engine.workflow_state import NodeStatus as _NS
        nodes = self.state.get_all_nodes(ep_id) or []
        for n in nodes:
            if n.get("type") == "script_generation":
                continue
            if n.get("type") in ("art_design", "result", "episode_art_design"):
                # art_design/result 是 pre-create 共享节点；episode_art_design 是本集美术阶段——
                # 都不算"已开工"，让同步(all_unstarted)能覆盖到还在美术期的集。
                continue
            if n.get("status") in (_NS.RUNNING, _NS.COMPLETED, _NS.CONFIRMED):
                return True
        return False

    async def _overwrite_episode_art(self, ep_id: str, shared: Dict[str, Any], series_id: str) -> None:
        """用新 shared_assets 同步 Episode 美术：
        - 专家分集(episode_art_design)：刷新 master 快照 + 异步重跑本集美术细化(按新 master 重造 outfit/补新角色)。
        - 普通(art_design)：直接拷 master 图为 CONFIRMED 子节点(旧行为)。"""
        ep_art = None
        art_main = None
        for n in self.state.get_all_nodes(ep_id):
            _t = n.get("type")
            if _t == "episode_art_design":
                ep_art = n
            elif _t == "art_design":
                art_main = n
        # 专家分集：刷新 master 快照 → 异步重跑（_run_episode_art 会再从 live 读最新 master，双保险 + 按新 master 重造 outfit/新角色）
        if ep_art is not None:
            _p = {**(ep_art.get("params") or {}), "master_shared_assets": shared}
            self.state.update_node(ep_id, ep_art["id"], {"params": _p})
            import asyncio as _aio
            _ex = InteractiveExecutor(project_id=ep_id, ws_callback=self.ws_callback)
            _aio.create_task(_ex.retry_node(ep_art["id"]))
            logger.info("同步：episode_art_design %s 刷新 master(%d 角色) + 异步重跑", ep_id, len(shared.get("characters") or []))
            return
        # 普通项目 art_design：原拷贝逻辑
        if not art_main:
            return
        # 删旧子节点
        old_children = art_main.get("children") or []
        for cid in old_children:
            try:
                # workflow_state.delete_node 如果没有，就降级用 update 标记
                if hasattr(self.state, "delete_node"):
                    self.state.delete_node(ep_id, cid)
            except Exception:
                pass
        # 更新 art_main result
        self.state.update_node(ep_id, art_main["id"], {
            "result": shared.get("art_main_result") or {},
            "outputs": {"art_direction": shared.get("art_main_result") or {}},
            "children": [],
            "confirmed": True,
        })
        # 重建子节点
        children_ids: List[str] = []
        col_x = 400
        for cat, items in (
            ("character", shared.get("characters") or []),
            ("scene", shared.get("scenes") or []),
            ("prop", shared.get("props") or []),
        ):
            for i, item in enumerate(items):
                child = self.state.create_node(ep_id, {
                    "node_id": f"node_{cat}_{i}_synced",
                    "type": "result",
                    "label": item.get("name") or item.get("location") or f"{cat}{i + 1}",
                    "position": {"x": col_x, "y": 100 + i * 90},
                    "params": dict(item),
                    "depends_on": [art_main["id"]],
                    "agent": "art_director",
                    "need_confirm": False,
                    "phase": "art",
                })
                self.state.update_node(ep_id, child["id"], {
                    "result": {**item, "result_type": cat, "from_series": series_id, "_synced": True},
                    "outputs": {"image_url": item.get("image_url")},
                    "confirmed": True,
                })
                self.state.set_node_status(ep_id, child["id"], NodeStatus.CONFIRMED)
                children_ids.append(child["id"])
            col_x += 280
        if children_ids:
            self.state.update_node(ep_id, art_main["id"], {"children": children_ids})

    @staticmethod
    def _derive_episode_scenes(ep_meta: Dict[str, Any], summary: Dict[str, Any]) -> List[Dict[str, Any]]:
        """根据 ep_meta.scene_indices 从 script_summary.scene_outline 筛出本集场景。

        优先级：
        1. ep_meta.scene_indices（导演分集时给的 0-based 索引数组）— 严格按此 filter
        2. 兜底：正则解析 scene_range 字符串里的"场景 X-Y"模式
        3. 最后兜底：给空 list（让 screenwriter 只看 story_arc）
        """
        scenes_outline = summary.get("scene_outline") or []
        if not scenes_outline:
            return []

        scene_indices = ep_meta.get("scene_indices")
        if not isinstance(scene_indices, list) or not scene_indices:
            # 兜底：从 scene_range 抓 "场景 X-Y" 或 "scenes X-Y"
            import re
            scene_range = ep_meta.get("scene_range") or ""
            m = re.search(r"(?:场景|scenes?)\s*(\d+)\s*[-~–至到]\s*(\d+)", scene_range, re.IGNORECASE)
            if m:
                # scene_range 里的数字通常是 1-based，转 0-based
                start = max(0, int(m.group(1)) - 1)
                end = min(len(scenes_outline), int(m.group(2)))
                scene_indices = list(range(start, end))
            else:
                # 抓不到 → 返回空 list（避免把整剧 scene 塞给本集编剧）
                logger.warning(
                    "_derive_episode_scenes: ep %s 没 scene_indices 也没解析出 scene_range，传空场景列表",
                    ep_meta.get("ep_id"),
                )
                scene_indices = []

        # 按索引 filter
        result = []
        for i in scene_indices:
            if 0 <= i < len(scenes_outline):
                s = scenes_outline[i]
                result.append({
                    # scene_id 保留原索引位置，方便下游识别
                    "scene_id": f"S{i + 1:02d}",
                    "location": (s.get("chapter") or s.get("summary") or "")[:50],
                    "key_action": (s.get("summary") or "")[:200],
                    "beat": "",
                    "duration_seconds": 5,
                    "key_events": s.get("key_events") or [],
                })
        return result

    async def start_storyboard(self) -> Dict[str, Any]:
        """前端对话框点"开始分镜设计"后调用：找最新一个已确认的 art_design / episode_art_design → 走标准 advance。
        0.7.0 轮 1：episode 项目找 episode_art_design；master / 老独立项目找 art_design。
        """
        art_main = None
        # Episode 项目优先找 episode_art_design；找不到再 fallback art_design
        for n in self.state.get_all_nodes(self.project_id):
            if n.get("type") == "episode_art_design":
                # episode 主节点不一定 CONFIRMED（自动 spawn 模式），但只要 children 都 confirmed 就算
                children_ids = n.get("children") or []
                if children_ids and all(
                    (self.state.get_node(self.project_id, cid) or {}).get("status") == NodeStatus.CONFIRMED
                    for cid in children_ids
                ):
                    art_main = n
        if not art_main:
            for n in self.state.get_all_nodes(self.project_id):
                if n.get("type") == "art_design" and n.get("status") == NodeStatus.CONFIRMED:
                    art_main = n  # 多个 art_design 时取最后一个

        if not art_main:
            return {"error": "没有已确认的美术设定节点"}

        # 复用 _advance_to_next：让 director 走 NODE_TEMPLATES 拿到 "node_003" 模板，
        # 否则手写 node_id="node_storyboard" 会让用户后续 confirm storyboard 时 director 找不到下一步。
        return await self._advance_to_next(art_main, art_main["id"], "art")

    # ═══════════════════════════════════════════════════════
    # video → merge 的批量确认
    # ═══════════════════════════════════════════════════════

    async def _after_video_confirm(self, node_id: str) -> Dict[str, Any]:
        """单个视频确认后，检查是否全部确认 → 触发对话询问是否合并"""
        all_videos = [
            n for n in self.state.get_all_nodes(self.project_id)
            if n.get("type") == "video_generation"
        ]
        all_confirmed = bool(all_videos) and all(
            n.get("status") == NodeStatus.CONFIRMED for n in all_videos
        )

        if all_confirmed:
            await self._notify("all_videos_confirmed", {
                "message": "🎉 所有分镜视频已确认！是否进行视频合并？",
                "need_dialog_confirm": True,
                "confirm_action": "merge_videos",
                "cancel_action": "stay_in_video_phase",
            })

        return {
            "type": "video_confirmed",
            "node_id": node_id,
            "all_confirmed": all_confirmed,
        }

    async def start_merge(self) -> Dict[str, Any]:
        """前端确认合并对话框 → 创建 merge 节点 → 执行。

        排序策略：按 shot_id（从 video 节点的 params.shot.shot_id 提取数字部分）升序，
        避免"重跑节点后新视频排到末尾"导致合并顺序错乱。
        """
        import re
        self.state.set_project_phase(self.project_id, ProjectPhase.MERGE)

        def _shot_sort_key(node: Dict[str, Any]) -> tuple:
            params = node.get("params") or {}
            shot = params.get("shot") or {}
            sid = shot.get("shot_id") or ""
            # 提 shot_id 里的数字部分；提不到退化为 lex 末尾
            m = re.search(r"(\d+)", sid)
            return (0, int(m.group(1)), sid) if m else (1, 0, sid)

        videos = [
            n for n in self.state.get_all_nodes(self.project_id)
            if n.get("type") == "video_generation" and n.get("status") == NodeStatus.CONFIRMED
        ]
        videos.sort(key=_shot_sort_key)
        video_ids = [n["id"] for n in videos]

        # 把 shot_id / shot_index / transition_to_next 注入到 video result 里，方便前端展示 + 拖拽排序保留
        def _enriched(n: Dict[str, Any]) -> Dict[str, Any]:
            params = n.get("params") or {}
            shot = params.get("shot") or {}
            return {
                **(n.get("result") or {}),
                "shot_id": shot.get("shot_id") or "",
                "shot_index": params.get("shot_index"),
                "video_node_id": n.get("id"),
                # 0.6.1: 分镜导演给的下一段转场（最后一镜为 None）
                "transition_to_next": shot.get("transition_to_next"),
                "pacing_role": shot.get("pacing_role"),
                "continuity_to_next": shot.get("continuity_to_next"),
            }

        # 输出分辨率：跟项目 aspect_ratio 走，避免默认 720x1280 把横屏项目拉成竖屏
        merge_ratio = self._project_aspect_ratio()
        m_w, m_h = self._resolution_for_ratio(merge_ratio)

        enriched_videos = [_enriched(n) for n in videos]
        # 0.6.1: 收集每段之间的转场（长度 = N-1，对应 videos[i] → videos[i+1]）
        # 最后一镜不参与转场，所以取 [:-1]；空值回落 fade
        transitions = [
            (v.get("transition_to_next") or "fade")
            for v in enriched_videos[:-1]
        ]

        # 0.6.1: 查找最新的 bgm_fetch 节点 → 把 BGM 本地路径塞进 merge params
        # 没找到 / 拉取失败时 → 不传 bgm，video_merge 走原逻辑不混 BGM
        bgm_path: Optional[str] = None
        bgm_volume_db: float = -12.0
        audio_segments: List[Dict[str, Any]] = []
        bgm_depends: List[str] = []
        for n in self.state.get_all_nodes(self.project_id):
            if n.get("type") != "bgm_fetch":
                continue
            res = n.get("result") or {}
            if not isinstance(res, dict):
                continue
            if res.get("error"):
                continue
            local = res.get("local_path") or ""
            if local:
                bgm_path = local
                bgm_depends.append(n["id"])
                # 从相邻的 audio_plan 节点取 segments
                for dep_id in (n.get("depends_on") or []):
                    dep = self.state.get_node(self.project_id, dep_id) or {}
                    if dep.get("type") == "audio_plan":
                        audio_segments = (dep.get("result") or {}).get("segments") or []
                        break

        merge_depends = list(video_ids) + bgm_depends

        merge_node = self._create_phase_node(
            phase="merge",
            node_template_id="node_merge",
            depends_on=merge_depends,
            position={"x": 1500, "y": 300},
            params={
                "videos": enriched_videos,
                # 0.6.1: 每段独立转场。video_merge.py 优先用 transitions 数组；
                # 兜底（数组为空 / 老项目无字段）用单值 transition
                "transitions": transitions,
                "transition": "fade",
                "resolution": f"{m_w}x{m_h}",
                "aspect_ratio": merge_ratio,
                # 0.6.1: BGM 字段（缺失时 video_merge 走原逻辑）
                "bgm_path": bgm_path,
                "bgm_volume_db": bgm_volume_db,
                "audio_segments": audio_segments,
                # 0.6.1/0.6.2: BGM 默认开关按 provider 决定
                # - elevenlabs（deep model，质量稳）→ 默认 True，丢弃 Seedance 音轨用 ElevenLabs
                # - mubert（tag 池匹配，质量不稳）→ 默认 False，用户主动勾才用
                "bgm_only": _bgm_default_on() if bgm_path else False,
            },
        )

        await self._notify("merge_started", {
            "message": "🎬 开始视频合并...",
            "new_node": merge_node,
        })
        asyncio.create_task(self._execute_node(merge_node["id"]))

        return {
            "type": "merge_started",
            "node_id": merge_node["id"],
            "phase": "merge",
        }

    # ═══════════════════════════════════════════════════════
    # 图片生成（统一入口）
    # ═══════════════════════════════════════════════════════

    def _mv_style_refs(self) -> List[str]:
        """MV 项目的风格参考图（非 MV 项目返回空表，行为一行不变）。

        只取前 3 张：Seedream 多图融合给的槽位有限，塞太多反而互相打架、风格更不稳。
        """
        cfg = (self.state.get_project(self.project_id) or {}).get("config") or {}
        if (cfg.get("mode") or "") != "mv":
            return []
        return [r for r in ((cfg.get("mv") or {}).get("style_refs") or []) if r][:3]

    async def _generate_image(
        self, prompt: str, width: int = 1024, height: int = 1024
    ) -> Dict[str, Any]:
        # 1.1.0 MV：有风格参考图就改走多图融合，让风格在**图像层**也被锚住。
        # 只靠 prompt 里的风格描述词（文字层）不够——同一句话分批出图仍会飘，
        # 而 MV 全片风格一散就变成素材拼贴。
        refs = self._mv_style_refs()
        if refs:
            # 🔴 必须显式声明「只借风格、不借内容」。不说这句，Seedream 会把参考图里的
            #    人和物一起融进来——想要水墨质感，结果连参考图里那个人都长在画面上了。
            styled = (f"{prompt}\n\n"
                      f"【参考图用途】随附的 {len(refs)} 张图**仅供参考画风**："
                      f"笔触、色调、光感、质感、构图习惯。"
                      f"不要复制参考图里的人物、物体或场景内容，画面内容完全以上文描述为准。")
            return await self._generate_image_from_urls(refs, styled, width, height)

        module = ModuleRegistry.get("text_to_image")
        if module is None:
            raise RuntimeError("text_to_image 模块未注册")
        return await module.run({
            "prompt": prompt, "width": width, "height": height, "style": "realistic",
        })

    async def _generate_image_from_image(
        self, reference_url: str, prompt: str, width: int = 1024, height: int = 768,
    ) -> Dict[str, Any]:
        """图生图（用参考图保持视觉一致性）— 用于角色三视图等需要保持人物特征的场景"""
        module = ModuleRegistry.get("image_to_image")
        if module is None:
            raise RuntimeError("image_to_image 模块未注册")
        return await module.run({
            "image_url": reference_url,
            "prompt": prompt,
            "width": width, "height": height,
            "strength": 0.7,
        })

    async def _generate_image_from_urls(
        self, image_urls: List[str], prompt: str,
        width: int = 1024, height: int = 1024,
    ) -> Dict[str, Any]:
        """多图融合生图（ARK Seedream）— 用于创作者上传参考图后重生成角色/场景。"""
        module = ModuleRegistry.get("image_to_image")
        if module is None:
            raise RuntimeError("image_to_image 模块未注册")
        # 调试日志：让用户能在 backend.log 看到 art_director LLM 实际给 Seedream 的 prompt
        logger.info(
            "[refimg→Seedream] %d ref(s), %dx%d, prompt: %s",
            len(image_urls), width, height, prompt[:500],
        )
        return await module.run({
            "image_urls": image_urls[:5],
            "prompt": prompt,
            "width": width, "height": height,
        })

    # ═══════════════════════════════════════════════════════
    # 文案格式化
    # ═══════════════════════════════════════════════════════

    def _format_plan_message(self, plan: Dict, director_result: Dict) -> str:
        lines = [
            "🎬 **创作方案已生成**", "",
            f"**主题**：{plan.get('theme', '未指定')}",
            f"**风格**：{plan.get('style', '未指定')}",
            f"**时长**：{plan.get('duration', '未指定')}",
            f"**受众**：{plan.get('audience', '未指定')}",
            f"**情绪**：{plan.get('mood', '未指定')}", "",
            "**创作流程**：",
        ]
        for i, node in enumerate(director_result.get("workflow_nodes", [])[:5], 1):
            lines.append(f"{i}. {node.get('node_name', '未命名')}")
        lines += ["", "💡 **可调整项**："]
        for item in director_result.get("adjustable_items", [])[:3]:
            lines.append(f"- {item}")
        lines += ["", "👉 请点击 **确认此方案** 开始创作，或继续对话调整方案。"]
        return "\n".join(lines)

    def _format_script_preview(self, script: Dict) -> str:
        lines = [
            f"**{script.get('script_title', '未命名剧本')}**", "",
            f"📖 {script.get('logline', '')}", "",
            f"🎬 场景数：{len(script.get('scenes', []))} | 👥 角色数：{len(script.get('characters', []))}",
            "", "**场景概览**：",
        ]
        for scene in script.get("scenes", [])[:3]:
            lines.append(f"- {scene.get('scene_id', '')}: {scene.get('scene_summary', '')}")
        return "\n".join(lines)

    def _format_art_preview(self, art_data: Dict) -> str:
        vs = art_data.get("visual_style", {})
        keyword = (vs.get("keywords") or ["未指定"])[0] if isinstance(vs, dict) else "未指定"
        characters = art_data.get("characters", []) or []
        scenes = art_data.get("scenes", []) or []
        lines = [
            "🎨 **美术设定完成**", "",
            f"**视觉风格**：{keyword}", "",
            f"**角色** ({len(characters)} 个) | **场景** ({len(scenes)} 个)",
            "", "👉 请点击 **确认** 继续生成分镜设计",
        ]
        return "\n".join(lines)

    def _format_storyboard_preview(self, sb_data: Dict) -> str:
        shots = sb_data.get("storyboard", []) if isinstance(sb_data, dict) else []
        return f"🎬 分镜设计完成！共 {len(shots)} 个分镜"

    def _format_video_preview(self, result: Dict) -> str:
        if result.get("video_url"):
            return "🎬 视频生成完成！"
        return f"🎬 视频生成完成（{result.get('status', '演示模式')}）"

    def _format_merge_preview(self, result: Dict) -> str:
        return "🎬 视频合并完成！" if result.get("video_url") else "🎬 视频合并已结束"

    # ═══════════════════════════════════════════════════════
    # 查询
    # ═══════════════════════════════════════════════════════

    def get_project_status(self) -> Dict[str, Any]:
        project = self.state.get_project(self.project_id)
        if not project:
            return {"error": "项目不存在"}
        nodes = self.state.get_all_nodes(self.project_id)
        return {
            "project": project,
            "nodes": nodes,
            "dag": self.state.get_dag_structure(self.project_id),
            "current_phase": project.get("phase"),
            "completed_nodes": [n["id"] for n in nodes if n.get("status") == NodeStatus.CONFIRMED],
            "pending_nodes": [
                n["id"] for n in nodes
                if n.get("status") in (NodeStatus.IDLE, NodeStatus.COMPLETED)
            ],
        }
