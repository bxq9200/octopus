"""agent_type → agent 类 的注册表。

agent 代码**不在这个目录里** —— 它们仍住在 `backend/app/agents/`，这里只是 import。
这是有意的：agents 只保留一份，local 模式（用户自己的开发机）和云端模式跑的是**同一份代码**，
不会出现"云端出的分镜跟本地不一样"这种极难查的漂移 bug。

护城河靠**打包时排除**保护：桌面端构建产物里不含 `backend/app/agents/` 与 `cloud/`，
只有云端镜像才包含。构建脚本必须带断言（产物里发现 agents 就中止构建）。
"""

from typing import Dict, Type

from app.agents.base.base_agent import BaseAgent
from app.agents.art_director.art_director_agent import ArtDirectorAgent
from app.agents.audio_director.audio_director_agent import AudioDirectorAgent
from app.agents.director.director_agent import DirectorAgent
from app.agents.module_router.module_router_agent import ModuleRouterAgent
from app.agents.mv_director.mv_director_agent import MVDirectorAgent
from app.agents.publisher.publisher_agent import PublisherAgent
from app.agents.router.router_agent import RouterAgent
from app.agents.screenwriter.screenwriter_agent import ScreenwriterAgent
from app.agents.storyboard.storyboard_agent import StoryboardAgent


REGISTRY: Dict[str, Type[BaseAgent]] = {
    "art_director": ArtDirectorAgent,
    "audio_director": AudioDirectorAgent,
    "director": DirectorAgent,
    "module_router": ModuleRouterAgent,
    "mv_director": MVDirectorAgent,
    "publisher": PublisherAgent,
    "router": RouterAgent,
    "screenwriter": ScreenwriterAgent,
    "storyboard": StoryboardAgent,
}


def get_agent_class(agent_type: str) -> Type[BaseAgent]:
    cls = REGISTRY.get((agent_type or "").strip())
    if cls is None:
        raise KeyError(agent_type)
    return cls


def known_agents() -> list:
    return sorted(REGISTRY.keys())
