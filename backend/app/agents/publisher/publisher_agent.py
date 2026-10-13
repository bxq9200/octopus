"""
发布 Agent — 为成片生成发布元数据（标题/简介/标签/封面建议）

职责：
1. 综合剧本主题、风格、受众，输出"爆款风格"的标题
2. 写一段 100-200 字的简介，强钩子
3. 输出 5-8 个标签（社媒平台搜索用）
4. 从已有的关键帧 / 视频首帧候选里推荐 3 个最适合做封面的，给出理由
5. 后续可选：基于成片重新生成封面（执行层用 image_to_image 走，agent 只负责给"封面 prompt"）

设计：
- 单次 LLM 调用，json_mode 输出
- 不调用任何模块（封面真正生成由 executor 调 image_to_image，由用户在 PublishEditor 触发）
- 失败时返回 error 字段而不是抛异常，让 PublishEditor 还能用占位文案
"""

import json
from typing import Any, Dict, List

from app.agents.base.base_agent import BaseAgent
from app.core.agent_runtime import cloud_method  # 护城河：cloud_client 下整体路由云端


PUBLISHER_SYSTEM_PROMPT = """你是社媒发布编辑，为已制作完成的短视频写发布物料。

任务：根据视频的主题、剧本、风格、受众，输出标题/简介/标签/封面建议，并撰写一句"用于 AI 重新生成封面"的英文提示词。

风格要求：
- 标题：≤20 字，有钩子（数字/反转/强情绪/对比），适合抖音/B站/小红书等社媒
- 简介：100-200 字，第一句话就要勾住读者，用第二人称或代入感强的表达
- 标签：5-8 个，单个标签不超过 6 字，覆盖主题、人物、情绪、风格、平台习惯（#xxx 不用加，纯文本即可）
- 封面推荐：从我给你的候选帧列表（每个有 idx、source、prompt 描述）里选 3 个，并按"最适合 → 第二适合 → 第三适合"排序，每个写一句 < 30 字的推荐理由。如果候选少于 3 个就给几个写几个
- 封面生成 prompt：万一用户对所有候选帧都不满意，要让 AI 重新画一张封面，这里写一段英文 prompt（80-150 词），描述构图、主角、情绪、光线、电影感、9:16 竖屏比例
"""


PUBLISHER_OUTPUT_SCHEMA_HINT = """请严格输出以下 JSON：
{
  "title": "≤20 字的标题",
  "description": "100-200 字简介",
  "tags": ["标签1", "标签2", "..."],
  "cover_recommendations": [
    {"idx": 候选帧索引数字, "reason": "< 30 字推荐理由", "rank": 1},
    {"idx": ..., "reason": "...", "rank": 2},
    {"idx": ..., "reason": "...", "rank": 3}
  ],
  "cover_prompt_en": "用于 AI 重生封面的英文 prompt，80-150 词"
}
不要输出任何其他内容。"""


class PublisherAgent(BaseAgent):
    """发布元数据 Agent

    使用方式：
        agent = PublisherAgent()
        result = await agent.execute({
            "script": {...},                # 剧本（含 title / logline / scenes）
            "style": "写实",
            "platform": "抖音",
            "cover_candidates": [
                {"idx": 0, "source": "keyframe", "image_url": "/output/...", "prompt": "...", "shot_id": "SHOT_01"},
                ...
            ],
        })
    """

    name = "publisher"
    description = "发布元数据 Agent — 生成标题/简介/标签/封面建议"
    system_prompt = PUBLISHER_SYSTEM_PROMPT

    @cloud_method
    async def execute(self, task: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        script = task.get("script") or {}
        style = task.get("style") or script.get("style") or "通用"
        platform = task.get("platform") or "抖音"
        audience = task.get("audience") or "通用受众"
        mood = task.get("mood") or script.get("mood") or "温馨"
        cover_candidates: List[Dict] = task.get("cover_candidates") or []

        prompt = self._build_prompt(script, style, platform, audience, mood, cover_candidates)

        try:
            response = await self.llm_generate(
                prompt=prompt,
                system=self.system_prompt,
                json_mode=True,
                temperature=0.7,
            )
        except Exception as e:
            return {
                "error": f"LLM 调用失败：{str(e)[:200]}",
                "fallback": self._fallback(script, cover_candidates),
            }

        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            # 兜底：从 markdown code block 抽 json
            import re
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", response, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    return {
                        "error": "JSON 解析失败",
                        "raw_response": response[:500],
                        "fallback": self._fallback(script, cover_candidates),
                    }
            else:
                return {
                    "error": "未找到 JSON",
                    "raw_response": response[:500],
                    "fallback": self._fallback(script, cover_candidates),
                }

        return self._normalize(data, cover_candidates, script)

    def _build_prompt(
        self,
        script: Dict,
        style: str,
        platform: str,
        audience: str,
        mood: str,
        cover_candidates: List[Dict],
    ) -> str:
        # 提取剧本关键信息（避免把整个 JSON 塞进去 prompt 太长）
        sc_title = script.get("script_title") or script.get("title", "")
        sc_logline = script.get("logline", "")
        sc_scenes = script.get("scenes") or []
        scene_brief = "\n".join(
            f"  - {s.get('scene_id', f'S{i+1}')}: {s.get('description') or s.get('action') or ''}"[:120]
            for i, s in enumerate(sc_scenes[:8])
        )

        candidates_brief = "\n".join(
            f"  - idx={c.get('idx', i)}, source={c.get('source','keyframe')}, "
            f"shot={c.get('shot_id','')}, prompt={(c.get('prompt') or '')[:120]}"
            for i, c in enumerate(cover_candidates)
        ) or "  （无候选帧）"

        return f"""【视频信息】
标题：{sc_title}
一句话简介：{sc_logline}
风格：{style}
受众：{audience}
情绪基调：{mood}
发布平台：{platform}

【场景列表】
{scene_brief}

【封面候选帧】
{candidates_brief}

{PUBLISHER_OUTPUT_SCHEMA_HINT}
"""

    def _normalize(
        self,
        data: Dict,
        cover_candidates: List[Dict],
        script: Dict,
    ) -> Dict:
        """规范化字段 + 校验封面 idx 在候选范围内"""
        title = (data.get("title") or "").strip()[:30]
        description = (data.get("description") or "").strip()[:400]
        tags = data.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip().lstrip("#")[:10] for t in tags if t][:8]

        # 校验 cover_recommendations 的 idx
        valid_idx = {i for i, _ in enumerate(cover_candidates)}
        # candidate 可能自带 idx 字段，做兜底
        for i, c in enumerate(cover_candidates):
            if "idx" in c:
                valid_idx.add(int(c["idx"]))
        recs = data.get("cover_recommendations") or []
        if not isinstance(recs, list):
            recs = []
        norm_recs = []
        seen_idx = set()
        for r in recs:
            try:
                idx = int(r.get("idx"))
            except (TypeError, ValueError):
                continue
            if idx not in valid_idx or idx in seen_idx:
                continue
            seen_idx.add(idx)
            norm_recs.append({
                "idx": idx,
                "rank": int(r.get("rank") or (len(norm_recs) + 1)),
                "reason": (str(r.get("reason") or "").strip())[:60],
            })
        norm_recs.sort(key=lambda r: r["rank"])

        cover_prompt = (data.get("cover_prompt_en") or "").strip()

        return {
            "title": title or (script.get("script_title") or "未命名作品")[:30],
            "description": description or script.get("logline", "")[:400],
            "tags": tags,
            "cover_recommendations": norm_recs,
            "cover_prompt_en": cover_prompt,
        }

    def _fallback(self, script: Dict, cover_candidates: List[Dict]) -> Dict:
        """LLM 不可用时的兜底，让前端至少能用占位文案"""
        return {
            "title": (script.get("script_title") or "未命名作品")[:30],
            "description": (script.get("logline") or "")[:400],
            "tags": [],
            "cover_recommendations": [
                {"idx": i, "rank": i + 1, "reason": "候选帧"}
                for i in range(min(3, len(cover_candidates)))
            ],
            "cover_prompt_en": "",
        }

    # ── 进阶模式聊天系统 prompt（override BaseAgent 默认）──

    def _chat_system_prompt(self) -> str:
        return """你是 Octopus 的发布 Agent，创作者要你调整发布物料（标题 / 简介 / 标签 / 封面）。

## 你能改的节点
- type="publish" 的发布节点：params 含 title / description / tags / selected_cover / platform

## 关于参考图（Vision 模式）
当创作者上传参考图时，你能直接看到图。常见场景：
- 创作者贴 logo 图，要求"用这个 logo 当封面" → 描述图里 logo 的颜色/文字，建议封面策略
- 创作者贴一张图，要求"封面就用这种风格" → 描述实际看到的视觉风格元素

## 怎么改
- "标题改短一点 / 改得更吸引人" → patch params.title 给一个新版本，should_rerun=false（发布节点不需要重跑 agent，前端 PublishEditor 会读 params 重新渲染）
- "简介加一句 ..." → patch params.description
- "标签加上 #治愈" → patch params.tags（要保留原有标签，加进数组）
- 封面候选切换 → patch params.selected_cover（注意 cover_candidates 在 result，不在 params）
- "导出 / 发布" → 不属于你的职责，建议用户点 PublishEditor 的"导出"按钮

## 输出格式（严格 JSON）
{
  "reply": "发布编辑式简短回应（≤80 字）",
  "node_updates": [
    {"node_id": "publish节点id", "params_patch": {"title": "新标题"}, "should_rerun": false, "reason": "..."}
  ]
}
"""
