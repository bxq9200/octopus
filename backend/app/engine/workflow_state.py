"""
交互式工作流状态管理器

管理整个项目的执行状态，支持：
1. 逐步创建节点（不是一次性生成所有节点）
2. 节点状态跟踪（idle → running → completed → confirmed）
3. 创作者确认机制
4. Agent 工作分配和状态同步
5. 结果存储和传递
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Callable
from enum import Enum

logger = logging.getLogger(__name__)


class NodeStatus(str, Enum):
    """节点状态枚举"""
    IDLE = "idle"           # 节点已创建，等待执行
    RUNNING = "running"     # 节点正在执行
    COMPLETED = "completed" # 节点执行完成，等待创作者确认
    CONFIRMED = "confirmed" # 创作者已确认，可以推进下一步
    FAILED = "failed"       # 执行失败
    SKIPPED = "skipped"     # 被跳过


class ProjectPhase(str, Enum):
    """项目阶段枚举"""
    INITIAL = "initial"           # 初始对话收集需求
    PLAN_CONFIRMED = "plan_confirmed"  # 方案已确认
    MV_AUDIO = "mv_audio"         # 1.1.0 MV 模式：歌曲解析（歌词轴 + 曲式 + 风格锁定），在剧本之前
    SCRIPT = "script"             # 剧本生成阶段
    ART = "art"                   # 美术设定阶段
    STORYBOARD = "storyboard"     # 分镜设计阶段
    KEYFRAME = "keyframe"         # 分镜关键帧生成阶段（按需，requires_keyframe=True 的镜头才进入）
    VIDEO = "video"               # 视频生成阶段
    AUDIO = "audio"               # 0.6.1: 音频导演 + Mubert BGM 阶段（与 keyframe/video 并行）
    MERGE = "merge"               # 视频合并阶段
    PUBLISH = "publish"           # 发布元数据 + 导出阶段
    COMPLETED = "completed"       # 全部完成


class WorkflowStateManager:
    """
    工作流状态管理器

    内存为主，外部可注入 persist_callback(project_id, snapshot_dict)
    在每次节点/项目变更后异步落库。
    """

    def __init__(self):
        # 项目状态存储: project_id -> ProjectState
        self._projects: Dict[str, Dict[str, Any]] = {}
        # 节点状态存储: project_id -> {node_id -> NodeState}
        self._nodes: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # WebSocket 回调
        self._ws_callbacks: Dict[str, Callable] = {}
        # 持久化回调 — 由 main.py 注入；签名 (project_id, snapshot_dict) -> awaitable
        self._persist_callback: Optional[Callable] = None

    def register_persist_callback(self, callback: Callable):
        """注入持久化回调（main.py 启动时调用）"""
        self._persist_callback = callback

    def snapshot(self, project_id: str) -> Dict[str, Any]:
        """把项目内存状态打包成可序列化的 dict（含所有 nodes 和 project 元数据）"""
        project = self._projects.get(project_id) or {}
        nodes = self._nodes.get(project_id, {})
        return {
            "project": {
                "phase": project.get("phase"),
                "status": project.get("status"),
                "config": project.get("config", {}),
                "chat_history": project.get("chat_history", []),
                "execution_plan": project.get("execution_plan"),
                "current_agent": project.get("current_agent"),
                "node_count": len(nodes),
                "confirmed_nodes": project.get("confirmed_nodes", []),
                "collected_info": project.get("collected_info", {}),
                "advanced_outline": project.get("advanced_outline"),
                # 0.6.5: 成本统计明细，按调用追加；cost_summary 节点 runner 读它算总账
                "cost_log": project.get("cost_log", []),
            },
            "nodes": {nid: self._serialize_node(n) for nid, n in nodes.items()},
        }

    def restore(self, project_id: str, snapshot: Dict[str, Any], name: str = ""):
        """从 snapshot 恢复项目状态到内存。已存在则覆盖。"""
        if not snapshot:
            return
        proj_data = snapshot.get("project") or {}
        nodes_data = snapshot.get("nodes") or {}

        existing = self._projects.get(project_id)
        if existing:
            existing.update({
                "phase": proj_data.get("phase") or existing.get("phase"),
                "status": proj_data.get("status") or existing.get("status"),
                "config": proj_data.get("config") or existing.get("config", {}),
                "chat_history": proj_data.get("chat_history") or [],
                "execution_plan": proj_data.get("execution_plan"),
                "current_agent": proj_data.get("current_agent"),
                "confirmed_nodes": proj_data.get("confirmed_nodes", []),
                "collected_info": proj_data.get("collected_info") or existing.get("collected_info", {}),
                "advanced_outline": proj_data.get("advanced_outline") or existing.get("advanced_outline"),
                "cost_log": proj_data.get("cost_log") or existing.get("cost_log", []),
            })
        else:
            self._projects[project_id] = {
                "id": project_id,
                "name": name or f"项目_{project_id[:8]}",
                "phase": proj_data.get("phase", ProjectPhase.INITIAL),
                "status": proj_data.get("status", "active"),
                "config": proj_data.get("config", {}),
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
                "node_count": 0,
                "confirmed_nodes": proj_data.get("confirmed_nodes", []),
                "current_agent": proj_data.get("current_agent"),
                "chat_history": proj_data.get("chat_history", []),
                "execution_plan": proj_data.get("execution_plan"),
                "collected_info": proj_data.get("collected_info", {}),
                "advanced_outline": proj_data.get("advanced_outline"),
                "cost_log": proj_data.get("cost_log", []),
            }

        self._nodes[project_id] = {nid: dict(n) for nid, n in nodes_data.items()}
        self._projects[project_id]["node_count"] = len(self._nodes[project_id])

        # 0.6.4 自愈：如果节点 status=running 但 result 已完整写入（非空 dict + 无 error），
        # 说明 runner 跑完了，但进程在 set_node_status(COMPLETED) 前被 kill / 崩溃，
        # status 卡在 running。加载时检测并修正为 completed，让用户看到正确状态。
        # restore 只在 workflow_state 冷加载（内存无该项目）时跑 —— 此刻任何 RUNNING 都是
        # 上次进程被 kill / 崩溃 / 重启遗留的"僵尸"，绝无活跃 task 在跑，所以可以安全处理。
        n_fixed = 0
        for nid, node in self._nodes[project_id].items():
            if node.get("status") != NodeStatus.RUNNING and node.get("status") != "running":
                continue
            result = node.get("result")
            # 占位 result 子节点（本集美术/角色/场景/道具图）：result 只有元数据、没真出 image_url 前不算跑完，
            # 否则会被误判为 completed / 卡在 running（本集美术子图自动跑被重启打断就是这个坑）。
            placeholder = (node.get("type") == "result"
                           and isinstance(result, dict) and not result.get("image_url"))
            if isinstance(result, dict) and result and not result.get("error") and not placeholder:
                # result 完整 → runner 实际跑完了，只是没来得及 set COMPLETED → 修为 completed
                node["status"] = NodeStatus.COMPLETED
                n_fixed += 1
                logger.warning(
                    "restore: 节点 %s (type=%s) status=running 但 result 完整，自愈为 completed",
                    nid, node.get("type"),
                )
            else:
                # result 空/无效/仅占位 → 上次执行被中断（如重启）→ 重置 IDLE 让用户可重跑
                node["status"] = NodeStatus.IDLE
                node["error"] = None
                n_fixed += 1
                logger.warning(
                    "restore: 节点 %s (type=%s) status=running 但无真实产出（执行被中断），自愈为 idle 可重跑",
                    nid, node.get("type"),
                )
        if n_fixed > 0:
            # 异步把修正写回 DB
            self._trigger_persist(project_id)

    def _trigger_persist(self, project_id: str):
        """触发持久化（异步、容错）"""
        cb = self._persist_callback
        if not cb:
            return
        try:
            snap = self.snapshot(project_id)
            result = cb(project_id, snap)
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)
                except RuntimeError:
                    result.close()
        except Exception as e:
            logger.warning("持久化失败 (project=%s): %s", project_id, e)
    
    # ═══════════════════════════════════════════════════════
    # 项目管理
    # ═══════════════════════════════════════════════════════
    
    def create_project(self, project_id: str, name: str = None, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """创建新项目。config 可由调用方（hydrate 时）从 DB 同步进来。"""
        project = {
            "id": project_id,
            "name": name or f"项目_{project_id[:8]}",
            "phase": ProjectPhase.INITIAL,
            "status": "active",
            "config": dict(config) if config else {},
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "node_count": 0,
            "confirmed_nodes": [],
            "current_agent": None,
            "chat_history": [],
            "execution_plan": None,
        }
        self._projects[project_id] = project
        self._nodes[project_id] = {}
        return project
    
    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        """获取项目状态"""
        return self._projects.get(project_id)
    
    def update_project(self, project_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """更新项目状态"""
        project = self._projects.get(project_id)
        if not project:
            raise ValueError(f"项目 {project_id} 不存在")

        project.update(updates)
        project["updated_at"] = datetime.utcnow().isoformat()
        self._trigger_persist(project_id)
        return project
    
    def set_project_phase(self, project_id: str, phase: ProjectPhase):
        """设置项目阶段"""
        return self.update_project(project_id, {"phase": phase})
    
    # ═══════════════════════════════════════════════════════
    # 节点管理
    # ═══════════════════════════════════════════════════════
    
    def create_node(self, project_id: str, node_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        创建新节点
        
        节点数据结构：
        {
            "id": "node_xxx",
            "type": "script_generation|art_design|storyboard|video_generation|video_merge",
            "label": "节点名称",
            "status": "idle",
            "position": {"x": 100, "y": 200},
            "params": {},      # 节点执行参数
            "inputs": {},      # 输入数据（来自父节点）
            "outputs": {},     # 输出结果
            "result": None,    # 执行结果
            "error": None,     # 错误信息
            "depends_on": [],  # 依赖的节点ID列表
            "children": [],    # 子节点ID列表
            "need_confirm": True,  # 是否需要创作者确认
            "confirmed": False,    # 是否已确认
            "agent": None,     # 当前负责执行的 Agent
            "created_at": "...",
            "updated_at": "...",
        }
        """
        node_id = node_data.get("id") or node_data.get("node_id") or f"node_{str(uuid.uuid4())[:8]}"

        node = {
            "id": node_id,
            "node_id": node_data.get("node_id", node_id),  # 模板节点ID（如 node_001）
            "project_id": project_id,
            "type": node_data.get("type", "unknown"),
            "label": node_data.get("label", "未命名节点"),
            "status": node_data.get("status", NodeStatus.IDLE),
            "position": node_data.get("position", {"x": 0, "y": 0}),
            "params": node_data.get("params", {}),
            "inputs": node_data.get("inputs", {}),
            "outputs": node_data.get("outputs", {}),
            "result": node_data.get("result"),
            "error": node_data.get("error"),
            "depends_on": node_data.get("depends_on", []),
            "children": node_data.get("children", []),
            "need_confirm": node_data.get("need_confirm", True),
            "confirmed": node_data.get("confirmed", False),
            "agent": node_data.get("agent"),
            # 透传可选的扩展字段（连接点、父节点引用等）
            "phase": node_data.get("phase"),
            "parent_result_id": node_data.get("parent_result_id"),
            "parent_shot": node_data.get("parent_shot"),
            "result_index": node_data.get("result_index"),
            "has_children": node_data.get("has_children", False),
            "connection_points": node_data.get("connection_points", {}),
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        
        if project_id not in self._nodes:
            self._nodes[project_id] = {}
        self._nodes[project_id][node_id] = node
        
        # 更新项目节点计数
        project = self._projects.get(project_id)
        if project:
            project["node_count"] = len(self._nodes[project_id])
        
        # 通知前端新节点创建
        self._notify(project_id, {
            "type": "node_created",
            "node": self._serialize_node(node)
        })
        self._trigger_persist(project_id)

        return node
    
    def get_node(self, project_id: str, node_id: str) -> Optional[Dict[str, Any]]:
        """获取节点状态"""
        return self._nodes.get(project_id, {}).get(node_id)
    
    def update_node(self, project_id: str, node_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """更新节点状态"""
        node = self.get_node(project_id, node_id)
        if not node:
            raise ValueError(f"节点 {node_id} 不存在")

        node.update(updates)
        node["updated_at"] = datetime.utcnow().isoformat()

        # 通知前端节点更新
        self._notify(project_id, {
            "type": "node_updated",
            "node": self._serialize_node(node)
        })
        self._trigger_persist(project_id)

        return node
    
    def set_node_status(self, project_id: str, node_id: str, status: NodeStatus, 
                        result: Any = None, error: str = None):
        """设置节点状态"""
        updates = {"status": status}
        if result is not None:
            updates["result"] = result
        if error is not None:
            updates["error"] = error
        
        node = self.update_node(project_id, node_id, updates)
        
        # 特殊状态处理
        if status == NodeStatus.COMPLETED:
            # 执行完成，等待确认
            pass
        elif status == NodeStatus.CONFIRMED:
            # 创作者已确认
            project = self._projects.get(project_id)
            if project and node_id not in project.get("confirmed_nodes", []):
                project["confirmed_nodes"] = project.get("confirmed_nodes", []) + [node_id]
        
        return node
    
    def confirm_node(self, project_id: str, node_id: str) -> Dict[str, Any]:
        """创作者确认节点"""
        node = self.get_node(project_id, node_id)
        if not node:
            raise ValueError(f"节点 {node_id} 不存在")
        
        if node["status"] != NodeStatus.COMPLETED:
            raise ValueError(f"节点 {node_id} 尚未完成，无法确认")
        
        return self.set_node_status(project_id, node_id, NodeStatus.CONFIRMED)
    
    def get_all_nodes(self, project_id: str) -> List[Dict[str, Any]]:
        """获取项目所有节点"""
        nodes = self._nodes.get(project_id, {})
        return [self._serialize_node(n) for n in nodes.values()]

    def delete_node(self, project_id: str, node_id: str, cascade: bool = True) -> List[str]:
        """删除节点。

        cascade=True 时，连带删除依赖它的下游节点（避免出现孤儿依赖）。
        返回所有被删除的 node_id 列表。
        """
        nodes = self._nodes.get(project_id, {})
        if node_id not in nodes:
            return []

        # 计算级联范围（深度优先：node 及所有以 node 为 depends_on/parent 的下游）
        to_delete: List[str] = []
        stack = [node_id]
        seen = {node_id}
        while stack:
            curr = stack.pop()
            to_delete.append(curr)
            if not cascade:
                continue
            for other_id, other in nodes.items():
                if other_id in seen:
                    continue
                deps = other.get("depends_on") or []
                if curr in deps or other.get("parent_result_id") == curr or other.get("parent_shot") == curr:
                    seen.add(other_id)
                    stack.append(other_id)

        for nid in to_delete:
            nodes.pop(nid, None)
            # 从其它节点的 children / depends_on 里把它摘掉
            for other in nodes.values():
                if other.get("children"):
                    other["children"] = [c for c in other["children"] if c != nid]
                if other.get("depends_on"):
                    other["depends_on"] = [d for d in other["depends_on"] if d != nid]

        # 项目元数据：confirmed_nodes 也清掉
        project = self._projects.get(project_id)
        if project:
            project["confirmed_nodes"] = [n for n in project.get("confirmed_nodes", []) if n not in to_delete]
            project["node_count"] = len(nodes)

        # 通知前端
        self._notify(project_id, {"type": "nodes_deleted", "node_ids": to_delete})
        self._trigger_persist(project_id)
        return to_delete
    
    def get_nodes_by_phase(self, project_id: str, phase: str) -> List[Dict[str, Any]]:
        """获取特定阶段的节点"""
        nodes = self._nodes.get(project_id, {})
        phase_nodes = []
        for node in nodes.values():
            if node.get("phase") == phase:
                phase_nodes.append(self._serialize_node(node))
        return phase_nodes
    
    def get_dag_structure(self, project_id: str) -> Dict[str, Any]:
        """获取项目的 DAG 结构（用于前端渲染）"""
        nodes = self.get_all_nodes(project_id)
        
        # 构建 edges
        edges = []
        for node in nodes:
            for dep_id in node.get("depends_on", []):
                edges.append({
                    "id": f"edge_{dep_id}_{node['id']}",
                    "source": dep_id,
                    "target": node["id"],
                    "type": "default"
                })
        
        return {
            "nodes": nodes,
            "edges": edges
        }
    
    # ═══════════════════════════════════════════════════════
    # WebSocket 通知
    # ═══════════════════════════════════════════════════════

    def register_ws_callback(self, project_id: str, callback: Callable):
        """注册 WebSocket 回调（callback 可同步可异步，状态变化时自动调用）"""
        self._ws_callbacks[project_id] = callback

    def unregister_ws_callback(self, project_id: str):
        self._ws_callbacks.pop(project_id, None)

    def _notify(self, project_id: str, message: Dict[str, Any]):
        """发送通知到前端 — 支持同步/异步 callback。

        异步 callback 返回的 coroutine 会被调度到当前运行中的 event loop。
        """
        callback = self._ws_callbacks.get(project_id)
        if not callback:
            return
        try:
            result = callback(message)
            if asyncio.iscoroutine(result):
                # 在当前 event loop 中调度，不阻塞同步代码
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)
                except RuntimeError:
                    # 不在 event loop 中（理论上不会发生），关闭协程避免警告
                    result.close()
        except Exception as e:
            logger.warning("WebSocket 通知失败: %s", e)
    
    # ═══════════════════════════════════════════════════════
    # 序列化
    # ═══════════════════════════════════════════════════════
    
    def _serialize_node(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """序列化节点（用于前端展示）"""
        return {
            "id": node["id"],
            "node_id": node.get("node_id", node["id"]),
            "type": node["type"],
            "label": node["label"],
            "status": node["status"],
            "position": node["position"],
            "params": node.get("params", {}),
            "inputs": node.get("inputs", {}),
            "outputs": node.get("outputs", {}),
            "result": node.get("result"),
            "error": node.get("error"),
            "depends_on": node.get("depends_on", []),
            "children": node.get("children", []),
            "need_confirm": node.get("need_confirm", True),
            "confirmed": node.get("confirmed", False),
            "agent": node.get("agent"),
            "phase": node.get("phase"),
            "parent_result_id": node.get("parent_result_id"),
            "parent_shot": node.get("parent_shot"),
            "result_index": node.get("result_index"),
            "has_children": node.get("has_children", False),
            "connection_points": node.get("connection_points", {}),
            "created_at": node.get("created_at"),
            "updated_at": node.get("updated_at"),
        }
    
    # ═══════════════════════════════════════════════════════
    # 调试和清理
    # ═══════════════════════════════════════════════════════
    
    def get_all_projects(self) -> List[Dict[str, Any]]:
        """获取所有项目"""
        return list(self._projects.values())
    
    def delete_project(self, project_id: str):
        """删除项目"""
        self._projects.pop(project_id, None)
        self._nodes.pop(project_id, None)
        self._ws_callbacks.pop(project_id, None)


# 全局状态管理器实例
workflow_state = WorkflowStateManager()
