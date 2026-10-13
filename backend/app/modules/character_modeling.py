"""
角色建模模块 — 工作流级模块

输入：剧本中的角色描述或角色需求
输出：角色设定表（外貌、性格、声线、服装等）

内部调用：text_to_text
"""

import json
from typing import Any, Dict, List
from app.modules.base import BaseModule
from app.modules.text_to_text import TextToTextModule


class CharacterModelingModule(BaseModule):
    """角色建模模块：根据剧本或需求生成角色设定
    
    输出包含：
    - 角色基础信息（姓名、年龄、性别）
    - 外貌特征（用于文生图的一致性提示词）
    - 性格标签
    - 声线特征（用于TTS选择）
    - 服装风格
    - 参考图像生成提示词（用于保持角色一致性）
    """
    
    name = "character_modeling"
    description = "根据剧本或角色需求生成详细的角色设定，包含外貌描述、性格、声线、服装等，并生成用于保持角色一致性的图像提示词。"
    category = "advanced"
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "剧本内容（用于提取角色）", "default": ""},
                "character_hint": {"type": "string", "description": "角色描述/需求", "default": ""},
                "style": {"type": "string", "description": "整体风格（写实/卡通/二次元等）", "default": "写实"},
                "count": {"type": "integer", "description": "需要生成的角色数量", "default": 1}
            },
            "required": []
        }
    
    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "characters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "age": {"type": "string"},
                            "gender": {"type": "string"},
                            "appearance": {"type": "string"},
                            "appearance_prompt": {"type": "string"},
                            "personality": {"type": "array"},
                            "voice_type": {"type": "string"},
                            "clothing": {"type": "string"},
                            "clothing_prompt": {"type": "string"},
                            "consistency_tags": {"type": "string"}
                        }
                    }
                }
            }
        }
    
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        script = input_data.get("script", "")
        character_hint = input_data.get("character_hint", "")
        style = input_data.get("style", "写实")
        count = input_data.get("count", 1)
        
        # 构建提示词
        if script:
            prompt = f"""请从以下剧本中提取并详细描述所有角色。

剧本内容：
{script[:2000]}

要求：
1. 为每个角色生成详细的设定
2. 外貌描述要具体到可以用于AI图像生成（{style}风格）
3. 生成"一致性标签"——一组关键词，用于在多个分镜中保持角色外貌一致
4. 声线描述用于后续TTS配音选择
5. 输出JSON格式

每个角色的字段：
- name: 角色名
- role: 在故事中的角色（主角/配角/反派等）
- age: 年龄段
- gender: 性别
- appearance: 外貌文字描述
- appearance_prompt: 用于Stable Diffusion/Midjourney的英文提示词
- personality: 性格标签数组
- voice_type: 声线类型（温柔/磁性/清脆/低沉等）
- clothing: 服装描述
- clothing_prompt: 服装的英文提示词
- consistency_tags: 保持角色一致性的核心标签（如发型、眼睛颜色、标志性配饰等）
"""
        else:
            prompt = f"""请根据以下描述生成{count}个角色设定。

角色需求：{character_hint}
风格：{style}

要求同上，输出JSON格式。"""
        
        text_module = TextToTextModule()
        result = await text_module.run({
            "prompt": prompt,
            "format": "json",
            "max_length": 4000
        })
        
        # 解析角色数据
        try:
            data = json.loads(result["text"])
            characters = data.get("characters", [])
            if not characters and isinstance(data, list):
                characters = data
            if not characters and isinstance(data, dict) and "name" in data:
                characters = [data]
        except json.JSONDecodeError:
            # 兜底：尝试提取
            text = result["text"]
            try:
                start = text.index("[")
                end = text.rindex("]") + 1
                characters = json.loads(text[start:end])
            except (ValueError, json.JSONDecodeError):
                # 生成一个默认角色
                characters = [{
                    "name": "主角",
                    "role": "主角",
                    "appearance": character_hint or "未指定",
                    "appearance_prompt": f"a character, {style} style, {character_hint}",
                    "consistency_tags": f"{style} style"
                }]
        
        # 确保每个角色都有consistency_tags
        for char in characters:
            if "consistency_tags" not in char:
                char["consistency_tags"] = f"{char.get('appearance', '')}, {char.get('clothing', '')}"
            if "appearance_prompt" not in char:
                char["appearance_prompt"] = f"a person, {char.get('appearance', '')}, {style} style"
        
        return {"characters": characters}
