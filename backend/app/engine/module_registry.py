from typing import Dict, List, Type
from app.modules.base import BaseModule


class ModuleRegistry:
    """模块注册表：管理所有可用的创作模块"""
    
    _modules: Dict[str, BaseModule] = {}
    
    @classmethod
    def register(cls, module_instance: BaseModule):
        cls._modules[module_instance.name] = module_instance
    
    @classmethod
    def get(cls, name: str) -> BaseModule | None:
        return cls._modules.get(name)
    
    @classmethod
    def list_all(cls) -> List[Dict]:
        """返回所有模块的元数据（给 Agent 看的）"""
        return [{
            "name": m.name,
            "description": m.description,
            "category": m.category,
            "input_schema": m.input_schema,
            "output_schema": m.output_schema,
        } for m in cls._modules.values()]
    
    @classmethod
    def get_tool_definitions(cls) -> List[Dict]:
        """返回给 LLM Tool Calling 的工具定义"""
        return [m.to_tool_definition() for m in cls._modules.values()]


# 初始化注册所有模块
from app.modules.text_to_text import TextToTextModule
from app.modules.text_to_image import TextToImageModule
from app.modules.text_to_video import TextToVideoModule
from app.modules.image_to_image import ImageToImageModule
from app.modules.image_to_video import ImageToVideoModule
from app.modules.tts import TTSModule
from app.modules.subtitle import SubtitleModule
from app.modules.video_merge import VideoMergeModule
from app.modules.publish import PublishModule
from app.modules.script_generation import ScriptGenerationModule
from app.modules.character_modeling import CharacterModelingModule
from app.modules.storyboard_generation import StoryboardGenerationModule
from app.modules.ai_director_calibration import AIDirectorCalibrationModule
from app.modules.video_generation import VideoGenerationModule

ModuleRegistry.register(TextToTextModule())
ModuleRegistry.register(TextToImageModule())
ModuleRegistry.register(TextToVideoModule())
ModuleRegistry.register(ImageToImageModule())
ModuleRegistry.register(ImageToVideoModule())
ModuleRegistry.register(VideoGenerationModule())
ModuleRegistry.register(TTSModule())
ModuleRegistry.register(SubtitleModule())
ModuleRegistry.register(VideoMergeModule())
ModuleRegistry.register(PublishModule())
ModuleRegistry.register(ScriptGenerationModule())
ModuleRegistry.register(CharacterModelingModule())
ModuleRegistry.register(StoryboardGenerationModule())
ModuleRegistry.register(AIDirectorCalibrationModule())
