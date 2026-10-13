"""
分镜生成模块 — 工作流级模块

输入：剧本 + 角色设定
输出：分镜列表（每个分镜含：画面描述、运镜、时长、角色一致性提示）

内部调用：text_to_text
关键特性：分镜一致性控制 — 在每个分镜中注入角色一致性标签
"""

import json
from typing import Any, Dict, List
from app.modules.base import BaseModule
from app.modules.text_to_text import TextToTextModule


class StoryboardGenerationModule(BaseModule):
    """分镜生成模块：将剧本拆解为详细分镜
    
    关键设计 — 分镜一致性控制：
    1. 接收角色设定的 consistency_tags
    2. 在每个分镜的 image_prompt 中注入一致性标签
    3. 确保同一角色在不同分镜中保持外貌一致
    
    输出每个分镜包含：
    - shot_id: 分镜编号
    - scene_ref: 关联的场景ID
    - description: 画面描述
    - image_prompt: 用于文生图的提示词（含一致性标签）
    - camera_movement: 运镜方式
    - duration: 时长（秒）
    - characters_in_shot: 出镜角色及一致性标签
    - audio_hint: 音频/配音提示
    """
    
    name = "storyboard_generation"
    description = "将剧本拆解为详细分镜脚本，每个分镜包含画面描述、AI图像生成提示词（含角色一致性控制）、运镜方式和时长。输入为剧本和角色设定，输出为分镜列表。"
    category = "advanced"
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "script": {"type": "object", "description": "剧本数据（script_generation的输出）"},
                "characters": {"type": "array", "description": "角色设定列表（character_modeling的输出）", "default": []},
                "style": {"type": "string", "description": "视觉风格", "default": "写实电影感"},
                "aspect_ratio": {"type": "string", "description": "画面比例", "default": "16:9"}
            },
            "required": ["script"]
        }
    
    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "storyboard": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "shot_id": {"type": "integer"},
                            "scene_id": {"type": "integer"},
                            "description": {"type": "string"},
                            "image_prompt": {"type": "string"},
                            "camera_movement": {"type": "string"},
                            "duration": {"type": "integer"},
                            "characters_in_shot": {"type": "array"},
                            "audio_hint": {"type": "string"}
                        }
                    }
                },
                "total_shots": {"type": "integer"},
                "total_duration": {"type": "integer"}
            }
        }
    
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        script = input_data["script"]
        characters = input_data.get("characters", [])
        style = input_data.get("style", "写实电影感")
        aspect_ratio = input_data.get("aspect_ratio", "16:9")
        
        # 构建角色一致性参考表
        char_consistency = {}
        for char in characters:
            char_consistency[char["name"]] = {
                "consistency_tags": char.get("consistency_tags", ""),
                "appearance_prompt": char.get("appearance_prompt", ""),
                "clothing_prompt": char.get("clothing_prompt", "")
            }
        
        # 构建剧本文本
        script_text = json.dumps(script, ensure_ascii=False, indent=2)
        
        # 角色一致性指令
        consistency_instruction = ""
        if characters:
            consistency_instruction = """
【角色一致性控制 — 关键要求】
对于每个分镜，如果包含角色，必须在 image_prompt 中插入该角色的"一致性标签"，确保AI生成图像时角色外貌保持一致。

角色一致性标签：
"""
            for name, info in char_consistency.items():
                tags = info["consistency_tags"]
                consistency_instruction += f"\n- {name}: {tags}"
            
            consistency_instruction += """

规则：
1. 一致性标签必须原样出现在每个包含该角色的分镜的 image_prompt 中
2. 可以在一致性标签前后添加场景相关描述，但标签本身不可修改
3. 如果分镜中没有角色，则不需要插入标签
"""
        
        prompt = f"""请将以下剧本拆解为详细的分镜脚本。

剧本：
{script_text}

视觉风格：{style}
画面比例：{aspect_ratio}

{consistency_instruction}

输出要求：
1. 每个场景拆解为1-3个分镜
2. 每个分镜必须包含：
   - shot_id: 分镜编号（从1开始）
   - scene_id: 所属场景ID
   - description: 中文画面描述（50字内）
   - image_prompt: 英文AI图像生成提示词（必须包含角色一致性标签！）
   - camera_movement: 运镜方式（如：推轨、摇镜、固定、手持等）
   - duration: 时长（秒）
   - characters_in_shot: 出镜角色列表（含一致性标签）
   - audio_hint: 音频提示（环境音/配乐/配音等）

3. 所有分镜的 image_prompt 必须保持统一的视觉风格
4. 输出JSON格式

输出格式：
{{
  "storyboard": [
    {{
      "shot_id": 1,
      "scene_id": 1,
      "description": "主角站在窗前，阳光洒在脸上",
      "image_prompt": "cinematic shot, a young woman standing by window, morning sunlight on face, {consistency_tags}, realistic film style, 16:9, high quality",
      "camera_movement": "缓慢推轨",
      "duration": 5,
      "characters_in_shot": [{{"name": "主角", "consistency_tags": "..."}}],
      "audio_hint": "轻柔钢琴配乐"
    }}
  ],
  "total_shots": 10,
  "total_duration": 60
}}"""
        
        text_module = TextToTextModule()
        result = await text_module.run({
            "prompt": prompt,
            "format": "json",
            "max_length": 6000
        })
        
        # 解析分镜数据
        try:
            data = json.loads(result["text"])
            storyboard = data.get("storyboard", [])
            if not storyboard and isinstance(data, list):
                storyboard = data
        except json.JSONDecodeError:
            text = result["text"]
            try:
                start = text.index("[")
                end = text.rindex("]") + 1
                storyboard = json.loads(text[start:end])
            except (ValueError, json.JSONDecodeError):
                # 兜底：从剧本场景生成分镜
                scenes = script.get("scenes", [])
                storyboard = []
                for i, scene in enumerate(scenes):
                    storyboard.append({
                        "shot_id": i + 1,
                        "scene_id": scene.get("scene_id", i + 1),
                        "description": scene.get("setting", "场景"),
                        "image_prompt": f"cinematic shot, {scene.get('setting', '')}, {style}, high quality",
                        "camera_movement": scene.get("camera", "固定镜头"),
                        "duration": scene.get("duration", 5),
                        "characters_in_shot": [{"name": c, "consistency_tags": char_consistency.get(c, {}).get("consistency_tags", "")} for c in scene.get("characters", [])],
                        "audio_hint": ""
                    })
        
        # 后处理：确保一致性标签被正确注入
        for shot in storyboard:
            for char_info in shot.get("characters_in_shot", []):
                char_name = char_info.get("name", "")
                if char_name in char_consistency:
                    tags = char_consistency[char_name]["consistency_tags"]
                    # 如果image_prompt中没有一致性标签，追加到末尾
                    prompt_text = shot.get("image_prompt", "")
                    if tags and tags not in prompt_text:
                        shot["image_prompt"] = prompt_text + f", {tags}"
                    # 确保character_info中有consistency_tags
                    char_info["consistency_tags"] = tags
        
        total_shots = len(storyboard)
        total_duration = sum(s.get("duration", 5) for s in storyboard)
        
        return {
            "storyboard": storyboard,
            "total_shots": total_shots,
            "total_duration": total_duration
        }
