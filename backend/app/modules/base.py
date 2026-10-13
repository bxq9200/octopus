from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseModule(ABC):
    """所有 Octopus 模块的基类。
    
    每个模块必须实现：
    - name: 模块唯一标识
    - description: 模块描述（给 Agent 看的）
    - input_schema: 输入 JSON Schema
    - output_schema: 输出 JSON Schema
    - run: 执行逻辑
    """
    
    name: str = ""
    description: str = ""
    category: str = "basic"  # basic / advanced
    
    @property
    @abstractmethod
    def input_schema(self) -> Dict[str, Any]:
        """返回输入 JSON Schema"""
        pass
    
    @property
    @abstractmethod
    def output_schema(self) -> Dict[str, Any]:
        """返回输出 JSON Schema"""
        pass
    
    @abstractmethod
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """执行模块，返回 JSON 格式的结果"""
        pass
    
    def to_tool_definition(self) -> Dict[str, Any]:
        """生成给 LLM 的 Tool Calling 定义"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.input_schema.get("properties", {}),
                    "required": self.input_schema.get("required", [])
                }
            }
        }
