"""
Agent 层 API 路由

提供 Agent 协作的 HTTP 接口：
1. /api/agents/chat — 用户对话入口（导演 Agent 调度）
2. /api/agents/execute — 执行工作流
3. /api/agents/{agent_name}/run — 直接调用单个 Agent
"""

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel
from typing import Dict, Any, List, Optional

from app.agents import get_agent_bus, get_agent
from app.agents.base.bus import TaskPlan, TaskPattern

router = APIRouter(prefix="/api/agents", tags=["agents"])


# ── 请求模型 ──

class ChatRequest(BaseModel):
    message: str
    project_id: Optional[str] = None
    context: Dict[str, Any] = {}
    file_content: Optional[str] = None  # 文件内容（前端读取后传入）
    file_name: Optional[str] = None


class ChatResponse(BaseModel):
    type: str  # "plan" | "ask" | "suggest" | "text"
    mode: Optional[str] = None  # "creative" | "story" | "file"
    content: Dict[str, Any]
    workflow_nodes: Optional[List[Dict]] = None


class ExecuteRequest(BaseModel):
    workflow_nodes: List[Dict[str, Any]]
    project_id: Optional[str] = None
    start_from: Optional[str] = None  # 从指定节点开始执行


class AgentRunRequest(BaseModel):
    task: Dict[str, Any]
    context: Dict[str, Any] = {}


class ConfirmPlanRequest(BaseModel):
    """用户确认默认方案"""
    message: str  # 用户回复
    default_plan: Dict[str, Any]  # 之前的默认方案
    project_id: Optional[str] = None


# ── 路由 ──

@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    用户对话入口 — 支持三种模式
    
    模式一：创意模式（模糊想法）
      - 用户："帮我生成一个小猫的视频"
      - 导演：给出默认方案 + 可调整项
      - 用户确认后生成工作流
    
    模式二：故事模式（完整/不完整故事）
      - 用户："讲一个宇航员在火星发现生命的故事..."
      - 导演：分析完整性 → 完整则 plan，不完整则 suggest
    
    模式三：文件模式（上传文件）
      - 用户：上传小说文件
      - 导演：通读 → 直接 plan
    """
    bus = get_agent_bus()
    
    # 调用导演 Agent
    result = await bus.call_agent("director", {
        "request": request.message,
        "file_content": request.file_content or "",
        "file_name": request.file_name or "",
        "history": request.context.get("history", [])
    }, request.context)
    
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)
    
    data = result.data
    decision = data.get("decision")
    mode = data.get("mode")
    
    if decision == "ask":
        # 模式一：需要追问
        return ChatResponse(
            type="ask",
            mode=mode,
            content={
                "message": "为了给您更好的创作方案，请回答以下问题（或直接确认默认方案）：",
                "default_plan": data.get("default_plan", {}),
                "adjustable_items": data.get("adjustable_items", []),
                "questions": data.get("questions", [])
            }
        )
    
    elif decision == "suggest":
        # 模式二：故事不完整，给出完善建议
        return ChatResponse(
            type="suggest",
            mode=mode,
            content={
                "message": "故事框架不错！建议完善以下几点：",
                "story_analysis": data.get("story_analysis", {}),
                "suggestions": data.get("suggestions", []),
                "project_intent": data.get("project_intent", ""),
                "creative_direction": data.get("creative_direction", "")
            }
        )
    
    elif decision == "plan":
        # 三种模式最终都返回 plan
        if mode == "creative":
            message = f"已为您生成默认方案！主题：{data.get('default_plan', {}).get('theme', '')}，风格：{data.get('default_plan', {}).get('style', '')}"
        elif mode == "story":
            message = "故事分析完成！已生成创作工作流。"
        elif mode == "file":
            message = f"已通读《{data.get('file_analysis', {}).get('title', '文件')}》，生成改编方案。"
        else:
            message = "已为您编排好创作流程。"
        
        return ChatResponse(
            type="plan",
            mode=mode,
            content={
                "message": message,
                "project_intent": data.get("project_intent", ""),
                "creative_direction": data.get("creative_direction", ""),
                "default_plan": data.get("default_plan", {}),
                "adaptation_plan": data.get("adaptation_plan", {}),
                "next_action": data.get("next_action", "")
            },
            workflow_nodes=data.get("workflow_nodes", [])
        )
    
    # 默认返回文本
    return ChatResponse(
        type="text",
        content={"message": data.get("thought", "处理完成")}
    )


@router.post("/confirm-plan", response_model=ChatResponse)
async def confirm_plan(request: ConfirmPlanRequest):
    """
    用户确认默认方案后，生成完整工作流
    
    用于模式一（创意模式）的第二步：
    1. 用户说"帮我做个视频"
    2. 导演返回默认方案
    3. 用户说"好的，就按这个来"
    4. 调用此接口生成工作流
    """
    bus = get_agent_bus()
    
    # 重新调用导演 Agent，带上默认方案
    result = await bus.call_agent("director", {
        "request": request.message,
        "default_plan": request.default_plan,
        "confirmed": True
    })
    
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)
    
    data = result.data
    
    return ChatResponse(
        type="plan",
        mode="creative",
        content={
            "message": "已确认方案，生成完整工作流！",
            "project_intent": data.get("project_intent", ""),
            "creative_direction": data.get("creative_direction", ""),
            "next_action": data.get("next_action", "")
        },
        workflow_nodes=data.get("workflow_nodes", [])
    )


@router.post("/execute")
async def execute(request: ExecuteRequest):
    """
    执行工作流
    
    支持：
    - 完整执行：从第一个节点开始
    - 部分执行：从指定节点开始（用于重跑）
    """
    bus = get_agent_bus()
    nodes = request.workflow_nodes
    
    # 构建执行计划
    # 按依赖关系排序
    executed = set()
    results = {}
    
    async def execute_node(node: Dict):
        """执行单个节点"""
        node_id = node["node_id"]
        agent_name = node.get("agent", "module_router")
        
        # 检查依赖是否满足
        for dep in node.get("depends_on", []):
            if dep not in executed:
                return {"error": f"依赖节点 {dep} 未执行"}
        
        # 准备输入（合并上游输出）
        node_input = node.get("input", {}).copy()
        for dep in node.get("depends_on", []):
            if dep in results:
                node_input[f"_{dep}_output"] = results[dep]
        
        # 调用 Agent
        result = await bus.call_agent(agent_name, node_input)
        
        executed.add(node_id)
        results[node_id] = result.data if result.success else {"error": result.error}
        
        return result
    
    # 拓扑排序执行
    while len(executed) < len(nodes):
        progress = False
        for node in nodes:
            if node["node_id"] in executed:
                continue
            
            # 检查是否可以执行
            deps_satisfied = all(d in executed for d in node.get("depends_on", []))
            if not deps_satisfied:
                continue
            
            # 执行
            await execute_node(node)
            progress = True
        
        if not progress:
            # 有循环依赖或无法执行的节点
            break
    
    return {
        "status": "completed",
        "executed_nodes": list(executed),
        "results": results
    }


@router.post("/{agent_name}/run")
async def run_agent(agent_name: str, request: AgentRunRequest):
    """
    直接调用单个 Agent
    
    用于：
    - 调试单个 Agent
    - 用户手动触发某个节点
    - 第三方系统集成
    """
    bus = get_agent_bus()
    
    # 检查 Agent 是否存在
    agent = bus.get(agent_name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' 不存在")
    
    # 执行
    result = await bus.call_agent(agent_name, request.task, request.context)
    
    return {
        "success": result.success,
        "data": result.data,
        "error": result.error,
        "duration_ms": result.duration_ms
    }


@router.get("/list")
async def list_agents():
    """列出所有可用 Agent"""
    bus = get_agent_bus()
    agents = []
    
    for name in bus.list_agents():
        agent = bus.get(name)
        agents.append({
            "name": name,
            "description": agent.description if agent else "",
            "status": agent.status.value if agent else "unknown"
        })
    
    return {"agents": agents}
