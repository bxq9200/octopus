"""
剧本生成模块 — 工作流级模块

输入：创作主题/需求
输出：完整剧本（含场景、角色、对话、镜头指示）

内部调用：text_to_text（多次，结构化生成）
"""

import json
from typing import Any, Dict
from app.modules.base import BaseModule
from app.modules.text_to_text import TextToTextModule


class ScriptGenerationModule(BaseModule):
    """剧本生成模块：将创作需求转化为完整剧本
    
    工作流程：
    1. 分析需求，确定剧本类型、风格、时长
    2. 生成故事大纲
    3. 生成详细剧本（场景+对话+镜头指示）
    4. 输出结构化剧本数据
    """
    
    name = "script_generation"
    description = "根据创作主题生成完整短视频剧本，包含故事大纲、场景描述、角色对话和镜头指示。输入为主题描述，输出为结构化剧本。"
    category = "advanced"
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "创作主题/需求描述"},
                "style": {"type": "string", "description": "风格偏好（搞笑/温情/悬疑/科普等）", "default": "通用"},
                "duration": {"type": "integer", "description": "目标时长（秒）", "default": 60},
                "characters": {"type": "array", "description": "指定角色（可选）", "default": []},
                "context": {"type": "string", "description": "额外上下文", "default": ""}
            },
            "required": ["topic"]
        }
    
    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "script": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "scenes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "scene_id": {"type": "integer"},
                                    "setting": {"type": "string"},
                                    "characters": {"type": "array"},
                                    "action": {"type": "string"},
                                    "dialogue": {"type": "string"},
                                    "camera": {"type": "string"},
                                    "duration": {"type": "integer"}
                                }
                            }
                        },
                        "total_duration": {"type": "integer"}
                    }
                },
                "raw_text": {"type": "string"}
            },
            "required": ["script"]
        }
    
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        topic = input_data["topic"]
        style = input_data.get("style", "通用")
        duration = input_data.get("duration", 60)
        characters = input_data.get("characters", [])
        context = input_data.get("context", "")
        
        # 步骤1：生成故事大纲
        outline_prompt = f"""请为以下主题生成一个{style}风格的短视频故事大纲。

主题：{topic}
目标时长：{duration}秒
{'指定角色：' + ', '.join(characters) if characters else ''}
{context if context else ''}

要求：
1. 故事要有起承转合，适合短视频节奏
2. 估算每个场景的大致时长
3. 标注每个场景需要的镜头类型
4. 输出为JSON格式

输出格式：
{{
  "title": "剧本标题",
  "summary": "一句话故事梗概",
  "scenes": [
    {{
      "scene_id": 1,
      "setting": "场景描述",
      "characters": ["角色名"],
      "action": "角色动作",
      "dialogue": "对话内容（如有）",
      "camera": "镜头指示（景别+运镜）",
      "duration": 10
    }}
  ],
  "total_duration": {duration}
}}"""
        
        text_module = TextToTextModule()
        
        # 调用LLM生成大纲
        outline_result = await text_module.run({
            "prompt": outline_prompt,
            "format": "json",
            "max_length": 3000
        })
        
        # 解析JSON
        try:
            script_data = json.loads(outline_result["text"])
        except json.JSONDecodeError:
            # 如果LLM没有返回纯JSON，尝试提取JSON部分
            text = outline_result["text"]
            try:
                start = text.index("{")
                end = text.rindex("}") + 1
                script_data = json.loads(text[start:end])
            except (ValueError, json.JSONDecodeError):
                script_data = {
                    "title": topic,
                    "summary": text[:200],
                    "scenes": [{"scene_id": 1, "setting": topic, "action": text, "dialogue": "", "camera": "固定镜头", "duration": duration}],
                    "total_duration": duration
                }
        
        # 确保必要字段存在
        if "scenes" not in script_data:
            script_data["scenes"] = []
        if "title" not in script_data:
            script_data["title"] = topic
        
        # 计算总时长
        total = sum(s.get("duration", 10) for s in script_data.get("scenes", []))
        script_data["total_duration"] = total
        
        return {
            "script": script_data,
            "raw_text": outline_result["text"]
        }
