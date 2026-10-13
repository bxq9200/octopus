import os
import uuid
import re
from typing import Any, Dict, List

from app.modules.base import BaseModule
from app.core.config import settings


class SubtitleModule(BaseModule):
    """字幕生成模块：为视频生成字幕文件

    支持：SRT、VTT、ASS 格式，支持多语言翻译
    """

    name = "subtitle"
    description = "为视频生成字幕文件。输入为视频或音频文件，输出为字幕文件（SRT/VTT）。"
    category = "basic"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "video_url": {
                    "type": "string",
                    "description": "视频或音频文件路径"
                },
                "text": {
                    "type": "string",
                    "description": "如果已有文本，直接生成时间轴对齐的字幕"
                },
                "language": {
                    "type": "string",
                    "default": "zh",
                    "description": "字幕语言"
                },
                "format": {
                    "type": "string",
                    "enum": ["srt", "vtt", "ass"],
                    "default": "srt",
                    "description": "字幕格式"
                },
                "max_chars_per_line": {
                    "type": "integer",
                    "default": 20,
                    "description": "每行最大字符数"
                },
                "translate_to": {
                    "type": "string",
                    "default": "",
                    "description": "翻译目标语言（如 en, jp），空则不翻译"
                }
            },
            "required": []
        }

    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "subtitle_url": {"type": "string", "description": "字幕文件路径"},
                "format": {"type": "string"},
                "language": {"type": "string"},
                "line_count": {"type": "integer"}
            },
            "required": ["subtitle_url"]
        }

    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        video_url = input_data.get("video_url", "")
        text = input_data.get("text", "")
        language = input_data.get("language", "zh")
        fmt = input_data.get("format", "srt")
        max_chars = input_data.get("max_chars_per_line", 20)
        translate_to = input_data.get("translate_to", "")

        # 如果提供了文本，直接生成字幕
        if text:
            return await self._generate_from_text(text, language, fmt, max_chars, translate_to)
        
        # 如果提供了视频，需要先做语音转文字（mock）
        if video_url:
            return await self._generate_from_video(video_url, language, fmt, max_chars)
        
        return {"subtitle_url": "", "error": "需要提供视频文件或文本"}

    async def _generate_from_text(self, text: str, language: str, fmt: str, max_chars: int, translate_to: str) -> Dict[str, Any]:
        """从文本生成字幕（模拟时间轴）"""
        output_dir = os.path.join(settings.OUTPUT_DIR, "subtitles")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"subtitle_{uuid.uuid4().hex[:8]}.{fmt}")

        # 翻译（mock）
        display_text = text
        if translate_to:
            display_text = f"[{translate_to}] {text}"  # mock 翻译

        # 分句
        sentences = self._split_text(display_text, max_chars)
        
        # 生成时间轴（模拟：每句 3-5 秒）
        subtitles = []
        current_time = 0.0
        for sentence in sentences:
            duration = min(max(len(sentence) / 5.0, 2.0), 6.0)  # 根据长度估算时长
            subtitles.append({
                "start": current_time,
                "end": current_time + duration,
                "text": sentence
            })
            current_time += duration + 0.5  # 0.5秒间隔

        # 写入文件
        content = self._format_subtitles(subtitles, fmt)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            "subtitle_url": f"/output/subtitles/{os.path.basename(output_path)}",
            "format": fmt,
            "language": translate_to or language,
            "line_count": len(subtitles),
            "mock": True
        }

    async def _generate_from_video(self, video_url: str, language: str, fmt: str, max_chars: int) -> Dict[str, Any]:
        """从视频生成字幕（需要语音转文字，mock）"""
        # TODO: 接入 Whisper 等语音转文字 API
        # 目前返回 mock 字幕
        mock_text = "这是根据视频内容自动生成的字幕文本。实际使用时会调用语音识别 API。"
        return await self._generate_from_text(mock_text, language, fmt, max_chars, "")

    def _split_text(self, text: str, max_chars: int) -> List[str]:
        """将文本按标点分句，并限制每行长度"""
        # 按标点分句
        sentences = re.split(r'([。！？.!?\n])', text)
        result = []
        current = ""
        
        for part in sentences:
            if not part:
                continue
            if len(current) + len(part) <= max_chars:
                current += part
            else:
                if current:
                    result.append(current.strip())
                current = part
        
        if current:
            result.append(current.strip())
        
        return result if result else [text]

    def _format_subtitles(self, subtitles: List[Dict], fmt: str) -> str:
        """格式化字幕为指定格式"""
        if fmt == "srt":
            return self._format_srt(subtitles)
        elif fmt == "vtt":
            return self._format_vtt(subtitles)
        elif fmt == "ass":
            return self._format_ass(subtitles)
        return self._format_srt(subtitles)

    def _format_srt(self, subtitles: List[Dict]) -> str:
        """SRT 格式"""
        lines = []
        for i, sub in enumerate(subtitles, 1):
            start = self._format_time(sub["start"], ",")
            end = self._format_time(sub["end"], ",")
            lines.append(f"{i}")
            lines.append(f"{start} --> {end}")
            lines.append(sub["text"])
            lines.append("")
        return "\n".join(lines)

    def _format_vtt(self, subtitles: List[Dict]) -> str:
        """VTT 格式"""
        lines = ["WEBVTT", ""]
        for sub in subtitles:
            start = self._format_time(sub["start"], ".")
            end = self._format_time(sub["end"], ".")
            lines.append(f"{start} --> {end}")
            lines.append(sub["text"])
            lines.append("")
        return "\n".join(lines)

    def _format_ass(self, subtitles: List[Dict]) -> str:
        """ASS 格式（简化版）"""
        lines = [
            "[Script Info]",
            "Title: Generated Subtitle",
            "ScriptType: v4.00+",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
        ]
        for sub in subtitles:
            start = self._format_ass_time(sub["start"])
            end = self._format_ass_time(sub["end"])
            lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{sub['text']}")
        return "\n".join(lines)

    def _format_time(self, seconds: float, separator: str) -> str:
        """格式化时间为 SRT/VTT 格式"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"

    def _format_ass_time(self, seconds: float) -> str:
        """ASS 时间格式"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours}:{minutes:02d}:{secs:05.2f}"
