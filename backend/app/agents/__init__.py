"""
Agent 注册与初始化

职责：
1. 注册所有 Agent 到消息总线
2. 提供统一的 Agent 获取接口

当前注册的核心 Agent：
- director         导演（创作调度 + 决策核心）
- module_router    模块调度
- screenwriter     编剧
- art_director     美术
- storyboard       分镜
- mv_director      MV 导演（1.1.0：歌曲理解 / 风格锁定 / 卡点分镜）

后续按需扩展：audio / localization / review 等。
"""

from app.agents.base.bus import AgentBus

from app.agents.director.director_agent import DirectorAgent
from app.agents.screenwriter.screenwriter_agent import ScreenwriterAgent
from app.agents.art_director.art_director_agent import ArtDirectorAgent
from app.agents.storyboard.storyboard_agent import StoryboardAgent
from app.agents.mv_director.mv_director_agent import MVDirectorAgent
from app.agents.module_router.module_router_agent import ModuleRouterAgent


def init_agents() -> AgentBus:
    """初始化所有 Agent 并注册到总线"""
    bus = AgentBus()
    bus.register("director", DirectorAgent())
    bus.register("module_router", ModuleRouterAgent())
    bus.register("screenwriter", ScreenwriterAgent())
    bus.register("art_director", ArtDirectorAgent())
    bus.register("storyboard", StoryboardAgent())
    bus.register("mv_director", MVDirectorAgent())
    return bus


_agent_bus: AgentBus | None = None


def get_agent_bus() -> AgentBus:
    """获取全局 Agent 总线（单例，懒加载）"""
    global _agent_bus
    if _agent_bus is None:
        _agent_bus = init_agents()
    return _agent_bus


def get_agent(name: str):
    """快捷获取 Agent"""
    return get_agent_bus().get(name)
