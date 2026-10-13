"""
Agent 消息总线 — 负责 Agent 间的通信协调

职责：
1. 管理所有 Agent 实例
2. 路由消息（点对点 / 广播）
3. 支持同步调用和异步事件
4. 任务编排（顺序 / 并行 / 条件分支）
"""

from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
import asyncio
from collections import defaultdict

from app.agents.base.base_agent import BaseAgent, AgentMessage, AgentResult


class TaskPattern(Enum):
    """任务执行模式"""
    SEQUENTIAL = "sequential"   # 顺序执行
    PARALLEL = "parallel"       # 并行执行
    PIPELINE = "pipeline"       # 管道（前一个的输出作为后一个的输入）
    CONDITIONAL = "conditional" # 条件分支


@dataclass
class TaskPlan:
    """任务计划"""
    pattern: TaskPattern
    agents: List[str]           # Agent 名称列表（执行顺序）
    params: Dict[str, Any] = field(default_factory=dict)  # 各Agent的参数
    condition: Optional[Callable] = None  # 条件函数（CONDITIONAL模式）


class AgentBus:
    """
    Agent 消息总线（单例）
    
    使用方式：
        bus = AgentBus()
        bus.register("director", director_agent)
        bus.register("writer", writer_agent)
        
        # 发送消息
        bus.send("writer", msg)
        
        # 执行计划
        result = await bus.execute_plan(plan)
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        # Agent 注册表
        self._agents: Dict[str, BaseAgent] = {}
        
        # 消息队列（按 Agent）
        self._queues: Dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        
        # 事件订阅
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        
        # 执行中的任务
        self._running_tasks: Dict[str, asyncio.Task] = {}
    
    # ── Agent 管理 ──
    
    def register(self, name: str, agent: BaseAgent):
        """注册 Agent"""
        self._agents[name] = agent
        agent.name = name  # 同步名称
    
    def get(self, name: str) -> Optional[BaseAgent]:
        """获取 Agent"""
        return self._agents.get(name)
    
    def list_agents(self) -> List[str]:
        """列出所有已注册 Agent"""
        return list(self._agents.keys())
    
    # ── 消息通信 ──
    
    async def send(self, msg: AgentMessage):
        """
        发送消息
        
        - to_agent 为空：广播给所有 Agent
        - to_agent 指定：点对点发送
        """
        if msg.to_agent:
            # 点对点
            agent = self._agents.get(msg.to_agent)
            if agent:
                agent.receive_message(msg)
                await self._queues[msg.to_agent].put(msg)
                # 触发订阅回调
                for callback in self._subscribers[msg.to_agent]:
                    asyncio.create_task(callback(msg))
        else:
            # 广播
            for name, agent in self._agents.items():
                if name != msg.from_agent:  # 不发给自己
                    agent.receive_message(msg)
                    await self._queues[name].put(msg)
    
    def subscribe(self, agent_name: str, callback: Callable):
        """订阅某个 Agent 的消息"""
        self._subscribers[agent_name].append(callback)
    
    # ── 任务编排 ──
    
    async def execute_plan(
        self,
        plan: TaskPlan,
        initial_input: Dict[str, Any] = None
    ) -> Dict[str, AgentResult]:
        """
        执行编排好的任务计划
        
        Args:
            plan: 任务计划
            initial_input: 初始输入
        
        Returns:
            各 Agent 的执行结果
        """
        results = {}
        current_input = initial_input or {}
        
        if plan.pattern == TaskPattern.SEQUENTIAL:
            # 顺序执行
            for agent_name in plan.agents:
                agent = self._agents.get(agent_name)
                if not agent:
                    results[agent_name] = AgentResult(
                        success=False,
                        error=f"Agent '{agent_name}' 未注册"
                    )
                    continue
                
                # 获取该 Agent 的参数（如果有）
                agent_params = plan.params.get(agent_name, {})
                agent_params.update(current_input)
                
                # 执行
                result = await agent.run(agent_params)
                results[agent_name] = result
                
                # 输出作为下一个的输入
                if result.success:
                    current_input = {"_prev_result": result.data, **result.data}
        
        elif plan.pattern == TaskPattern.PARALLEL:
            # 并行执行
            tasks = []
            for agent_name in plan.agents:
                agent = self._agents.get(agent_name)
                if agent:
                    agent_params = plan.params.get(agent_name, {})
                    agent_params.update(current_input)
                    tasks.append(agent.run(agent_params))
            
            parallel_results = await asyncio.gather(*tasks, return_exceptions=True)
            for agent_name, result in zip(plan.agents, parallel_results):
                if isinstance(result, Exception):
                    results[agent_name] = AgentResult(
                        success=False,
                        error=str(result)
                    )
                else:
                    results[agent_name] = result
        
        elif plan.pattern == TaskPattern.PIPELINE:
            # 管道模式（类似 sequential，但数据流更明确）
            pipeline_data = current_input.copy()
            
            for agent_name in plan.agents:
                agent = self._agents.get(agent_name)
                if not agent:
                    continue
                
                # 构建任务：包含管道数据
                task = {
                    "input": pipeline_data,
                    **plan.params.get(agent_name, {})
                }
                
                result = await agent.run(task)
                results[agent_name] = result
                
                if result.success:
                    # 合并结果到管道数据
                    pipeline_data.update(result.data)
        
        return results
    
    async def call_agent(
        self,
        agent_name: str,
        task: Dict[str, Any],
        context: Dict[str, Any] = None
    ) -> AgentResult:
        """
        直接调用单个 Agent
        
        Args:
            agent_name: Agent 名称
            task: 任务参数
            context: 上下文
        """
        agent = self._agents.get(agent_name)
        if not agent:
            return AgentResult(
                success=False,
                error=f"Agent '{agent_name}' 未注册"
            )
        
        return await agent.run(task, context)
    
    # ── 快捷方法 ──
    
    async def director_plan(self, user_request: str) -> Dict[str, Any]:
        """
        导演 Agent 快速入口：解析用户需求 → 返回任务计划
        """
        director = self._agents.get("director")
        if not director:
            return {"error": "导演 Agent 未注册"}
        
        result = await director.run({"request": user_request})
        if result.success:
            return result.data
        return {"error": result.error}
    
    def reset(self):
        """重置总线状态"""
        self._running_tasks.clear()
        for agent in self._agents.values():
            agent.reset()


# 全局实例
agent_bus = AgentBus()
