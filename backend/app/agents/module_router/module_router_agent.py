"""
模块调度 Agent — 根据任务目标选择最合适的系统模块，并生成模块调用参数

职责：
1. 理解导演 Agent 或其他 Agent 发来的任务
2. 从系统可用模块中选择最合适的模块
3. 根据模块 schema 生成准确参数
4. 判断输入是否满足模块运行要求
5. 返回模块调用计划，而不是直接执行任务
"""

import json
from typing import Any, Dict, List

from app.agents.base.base_agent import BaseAgent
from app.engine.module_registry import ModuleRegistry


# ═══════════════════════════════════════════════════════
# 老白：模块调度 Agent 的 system prompt
# ═══════════════════════════════════════════════════════
MODULE_ROUTER_SYSTEM_PROMPT = """你是 Octopus Video OS 的模块调度 Agent（Module Router Agent），负责根据任务目标选择最合适的系统模块，并生成模块调用参数。

## 核心职责
1. 理解导演 Agent 或其他 Agent 发来的任务。
2. 从系统可用模块中选择最合适的模块。
3. 根据模块 schema 生成准确参数。
4. 判断输入是否满足模块运行要求。
5. 如果缺少必要参数，给出默认值或向导演 Agent 请求补全。
6. 返回模块调用计划，而不是直接执行任务。

## 可用模块
{modules}

## 你必须遵守
- 不要调用不存在的模块。
- 不要编造模块能力。
- 所有参数必须符合模块 schema。
- 如果多个模块都能完成任务，要说明推荐原因。
- 优先选择成本低、稳定性高、效果可控的模块。
- 对昂贵模块或耗时模块要提示风险。

## 输出格式（JSON）
{{
  "selected_module": "模块名称",
  "reason": "选择原因",
  "required_inputs": {{}},
  "optional_inputs": {{}},
  "estimated_cost_level": "low | medium | high",
  "estimated_time_level": "fast | normal | slow",
  "module_call": {{
    "module_name": "模块名称",
    "params": {{}}
  }}
}}
"""


class ModuleRouterAgent(BaseAgent):
    """
    模块调度 Agent
    
    使用方式：
        router = ModuleRouterAgent()
        result = await router.run({
            "task": "生成一张猫咪图片",
            "input_data": {"prompt": "一只橘猫"},
            "constraints": {"cost": "low", "speed": "fast"}
        })
    """
    
    name = "module_router"
    description = "模块调度 Agent"
    system_prompt = MODULE_ROUTER_SYSTEM_PROMPT
    
    async def execute(self, task: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        模块调度核心逻辑
        
        Args:
            task: {
                "task": "任务描述",
                "input_data": {},
                "constraints": {"cost": "low", "speed": "fast"}
            }
        """
        task_desc = task.get("task", "")
        input_data = task.get("input_data", {})
        constraints = task.get("constraints", {})
        
        # 获取可用模块列表
        available_modules = ModuleRegistry.list_all()
        
        # 构建提示
        modules_desc = json.dumps(available_modules, ensure_ascii=False, indent=2)
        prompt = self._build_prompt(task_desc, input_data, constraints, modules_desc)
        
        # 动态更新 system prompt
        system = self.system_prompt.format(modules=modules_desc)
        
        # 调用 LLM
        response = await self.llm_generate(
            prompt=prompt,
            system=system,
            json_mode=True,
            temperature=0.3
        )
        
        # 解析
        try:
            plan = json.loads(response)
        except json.JSONDecodeError:
            return {
                "error": "模块调度失败",
                "raw_response": response[:500]
            }

        # 验证模块存在
        module_name = plan.get("selected_module", "")
        if not ModuleRegistry.get(module_name):
            # 回退到默认模块
            plan["selected_module"] = "text_to_text"
            existing_reason = plan.get("reason", "")
            plan["reason"] = f"{existing_reason}（原模块 '{module_name}' 不存在，回退到默认）".strip()
        
        return {
            "routing_plan": plan,
            "summary": {
                "selected_module": plan.get("selected_module", ""),
                "cost_level": plan.get("estimated_cost_level", "medium"),
                "time_level": plan.get("estimated_time_level", "normal")
            }
        }
    
    def _build_prompt(self, task: str, input_data: Dict, constraints: Dict, modules: str) -> str:
        """构建调度提示"""
        return f"""请为以下任务选择最合适的模块并生成调用参数：

任务描述：{task}

输入数据：
{json.dumps(input_data, ensure_ascii=False, indent=2)}

约束条件：
{json.dumps(constraints, ensure_ascii=False, indent=2)}

可用模块：
{modules}

请返回模块调用计划。
"""
