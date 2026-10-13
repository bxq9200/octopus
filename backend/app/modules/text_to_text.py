from typing import Any, Dict
from openai import AsyncOpenAI
from app.modules.base import BaseModule
from app.core.runtime_config import active_llm


class TextToTextModule(BaseModule):
    """文生文模块：根据提示生成/优化文本内容

    用于：脚本生成、分镜描述、角色分析等
    """

    name = "text_to_text"
    description = "根据输入提示生成或优化文本内容。输入为 prompt 和任务类型，输出为生成的文本。"
    category = "basic"

    def _get_client(self):
        """根据 user_settings 当前 LLM 配置返回客户端和模型名"""
        cfg = active_llm()
        if cfg.provider == "kimi":
            return (
                AsyncOpenAI(
                    api_key=cfg.api_key,
                    base_url=cfg.base_url,
                    default_headers={"User-Agent": "KimiCLI/1.30.0"},
                ),
                cfg.model,
            )
        return (
            AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url),
            cfg.model,
        )
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "生成提示/指令"},
                "context": {"type": "string", "description": "上下文/参考材料", "default": ""},
                "format": {"type": "string", "description": "输出格式要求", "default": "plain"},
                "max_length": {"type": "integer", "description": "最大字符数", "default": 2000}
            },
            "required": ["prompt"]
        }
    
    @property
    def output_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "生成的文本内容"},
                "tokens_used": {"type": "integer"}
            },
            "required": ["text"]
        }
    
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        client, model = self._get_client()
        
        prompt = input_data["prompt"]
        context = input_data.get("context", "")
        fmt = input_data.get("format", "plain")
        
        system_msg = "你是 Octopus 视频创作系统的文本生成专家。根据用户需求生成高质量的文本内容。"
        if fmt != "plain":
            system_msg += f" 请以 {fmt} 格式输出。"
        
        user_content = prompt
        if context:
            user_content = f"[上下文]\n{context}\n\n[任务]\n{prompt}"
        
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_content}
            ],
            max_tokens=input_data.get("max_length", 2000),
            temperature=0.7,
            extra_body={"thinking": {"type": "disabled"}}
        )
        
        return {
            "text": response.choices[0].message.content,
            "tokens_used": response.usage.total_tokens if response.usage else 0
        }
