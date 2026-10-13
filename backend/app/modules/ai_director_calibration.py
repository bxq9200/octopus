"""
AI导演校准模块 — 工作流级模块

输入：分镜脚本 + 用户反馈
输出：校准后的分镜（调整画面、时长、运镜等）

内部调用：text_to_text
用于：用户干预后的自动调优
"""

import json
from typing import Any, Dict
from app.modules.base import BaseModule
from app.modules.text_to_text import TextToTextModule


class AIDirectorCalibrationModule(BaseModule):
    """AI导演校准模块：根据用户反馈自动调优分镜
    
    支持调整：
    - 画面风格/色调
    - 分镜时长分配
    - 运镜方式
    - 角色出场顺序
    - 节奏（加快/放慢）
    """
    
    name = "ai_director_calibration"
    description = "AI导演校准：根据用户反馈自动调整分镜脚本。支持调整画面风格、时长分配、运镜方式、节奏等。输入为当前分镜和用户调整指令，输出为校准后的分镜。"
    category = "advanced"
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "storyboard": {"type": "array", "description": "当前分镜列表"},
                "script": {"type": "object", "description": "原始剧本", "default": {}},
                "adjustment": {"type": "string", "description": "用户调整指令（如：加快节奏、增加特写、色调变暖等）"},
                "characters": {"type": "array", "description": "角色设定", "default": []}
            },
            "required": ["storyboard", "adjustment"]
        }
    
    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "storyboard": {"type": "array"},
                "changes_made": {"type": "array", "description": "具体做了哪些调整"},
                "total_duration": {"type": "integer"}
            }
        }
    
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        storyboard = input_data["storyboard"]
        script = input_data.get("script", {})
        adjustment = input_data["adjustment"]
        characters = input_data.get("characters", [])
        
        # 构建当前分镜的文本表示
        storyboard_text = json.dumps(storyboard, ensure_ascii=False, indent=2)
        
        # 构建角色一致性参考
        char_info = ""
        if characters:
            char_info = "\n角色设定（保持一致性）：\n"
            for char in characters:
                char_info += f"- {char.get('name', '')}: {char.get('consistency_tags', '')}\n"
        
        prompt = f"""你是一位资深AI导演。请根据用户的调整指令，修改以下分镜脚本。

当前分镜：
{storyboard_text}

{char_info}

用户调整指令：{adjustment}

要求：
1. 保持分镜的整体叙事逻辑不变
2. 根据用户指令进行精确调整
3. 保持角色一致性标签不被修改
4. 如果调整涉及时长，确保总时长合理
5. 输出完整的修改后分镜列表
6. 同时列出具体做了哪些修改（changes_made）

输出JSON格式：
{{
  "storyboard": [...],
  "changes_made": ["调整了第3镜的运镜方式为特写", "将整体色调提示词加入暖色调关键词"],
  "total_duration": 58
}}"""
        
        text_module = TextToTextModule()
        result = await text_module.run({
            "prompt": prompt,
            "format": "json",
            "max_length": 6000
        })
        
        try:
            data = json.loads(result["text"])
            new_storyboard = data.get("storyboard", storyboard)
            changes = data.get("changes_made", [])
            duration = data.get("total_duration", sum(s.get("duration", 5) for s in new_storyboard))
        except json.JSONDecodeError:
            # 如果解析失败，返回原始分镜
            new_storyboard = storyboard
            changes = ["校准模块未能正确解析，返回原始分镜"]
            duration = sum(s.get("duration", 5) for s in storyboard)
        
        return {
            "storyboard": new_storyboard,
            "changes_made": changes,
            "total_duration": duration
        }
