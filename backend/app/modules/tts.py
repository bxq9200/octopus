import os
import uuid
from typing import Any, Dict

from app.modules.base import BaseModule
from app.core.config import settings


class TTSModule(BaseModule):
    """文本转语音模块：将文本转换为语音音频

    用于：视频配音、旁白生成、角色对话等
    """

    name = "tts"
    description = "将文本转换为语音音频。输入为文本和声音参数，输出为音频文件。"
    category = "basic"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要转换为语音的文本"
                },
                "voice": {
                    "type": "string",
                    "default": "default",
                    "description": "声音类型/角色"
                },
                "speed": {
                    "type": "number",
                    "default": 1.0,
                    "minimum": 0.5,
                    "maximum": 2.0,
                    "description": "语速倍数"
                },
                "language": {
                    "type": "string",
                    "default": "zh",
                    "description": "语言代码（zh/en/jp 等）"
                },
                "emotion": {
                    "type": "string",
                    "enum": ["neutral", "happy", "sad", "angry", "excited", "calm"],
                    "default": "neutral",
                    "description": "情感风格"
                }
            },
            "required": ["text"]
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "audio_url": {"type": "string", "description": "生成的音频文件路径"},
                "duration": {"type": "number", "description": "音频时长（秒）"},
                "text": {"type": "string", "description": "原始文本"}
            },
            "required": ["audio_url"]
        }

    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        text = input_data.get("text", "")
        voice = input_data.get("voice", "default")
        speed = input_data.get("speed", 1.0)
        language = input_data.get("language", "zh")
        emotion = input_data.get("emotion", "neutral")

        if not text.strip():
            return {"audio_url": "", "duration": 0, "error": "文本不能为空"}

        # 优先尝试接入真实 TTS API，失败时降级到 mock
        try:
            return await self._generate_with_edge_tts(text, voice, speed, language, emotion)
        except Exception as e:
            print(f"[tts] 真实API调用失败，降级到mock: {e}")
            return await self._mock_generate(text, voice, speed, language, emotion)

    async def _generate_with_edge_tts(self, text: str, voice: str, speed: float, language: str, emotion: str) -> Dict[str, Any]:
        """使用 Edge TTS（Microsoft Edge 的免费 TTS 服务）"""
        import edge_tts
        import asyncio

        output_dir = os.path.join(settings.OUTPUT_DIR, "audio")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"tts_{uuid.uuid4().hex[:8]}.mp3")

        # 选择语音
        voice_map = {
            "zh": "zh-CN-XiaoxiaoNeural",
            "zh_female": "zh-CN-XiaoxiaoNeural",
            "zh_male": "zh-CN-YunjianNeural",
            "en": "en-US-JennyNeural",
            "en_male": "en-US-GuyNeural",
            "jp": "ja-JP-NanamiNeural",
        }

        selected_voice = voice_map.get(voice, voice_map.get(language, "zh-CN-XiaoxiaoNeural"))

        # 调整语速
        rate = f"{int((speed - 1) * 100):+d}%"

        # 生成音频
        communicate = edge_tts.Communicate(text, selected_voice, rate=rate)
        await communicate.save(output_path)

        # 估算时长
        if language == "zh":
            estimated_duration = len(text) / 4.0 / speed
        else:
            estimated_duration = len(text.split()) / 15.0 / speed

        return {
            "audio_url": f"/output/audio/{os.path.basename(output_path)}",
            "duration": estimated_duration,
            "text": text,
            "voice": selected_voice,
            "speed": speed,
            "language": language,
            "emotion": emotion,
            "mock": False
        }

    async def _mock_generate(self, text: str, voice: str, speed: float, language: str, emotion: str) -> Dict[str, Any]:
        """Mock TTS — 生成静音音频或简单的提示音"""
        output_dir = os.path.join(settings.OUTPUT_DIR, "audio")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"tts_{uuid.uuid4().hex[:8]}.mp3")

        # 估算时长（假设中文每秒 4 字，英文每秒 15 词）
        if language == "zh":
            estimated_duration = len(text) / 4.0 / speed
        else:
            estimated_duration = len(text.split()) / 15.0 / speed

        try:
            import subprocess
            
            # 生成一个短的提示音，标注文本内容
            # 实际使用时替换为真实 TTS 输出
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"anullsrc=r=24000:cl=mono",
                "-t", str(max(estimated_duration, 2.0)),
                "-acodec", "libmp3lame",
                "-q:a", "4",
                output_path
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
            
        except Exception:
            # 降级：创建空音频文件
            with open(output_path, "wb") as f:
                f.write(b"")

        return {
            "audio_url": f"/output/audio/{os.path.basename(output_path)}",
            "duration": estimated_duration,
            "text": text,
            "voice": voice,
            "speed": speed,
            "language": language,
            "emotion": emotion,
            "mock": True
        }
