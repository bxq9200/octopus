"""
WebSocket 管理器 — 负责 DAG 执行状态的实时推送
"""

import json
from typing import Dict, Set
from fastapi import WebSocket


class WebSocketManager:
    """管理所有 WebSocket 连接，支持按 project_id 分组广播"""
    
    def __init__(self):
        # project_id -> {websocket}
        self._connections: Dict[str, Set[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket, project_id: str):
        await websocket.accept()
        if project_id not in self._connections:
            self._connections[project_id] = set()
        self._connections[project_id].add(websocket)
    
    def disconnect(self, websocket: WebSocket, project_id: str):
        if project_id in self._connections:
            self._connections[project_id].discard(websocket)
            if not self._connections[project_id]:
                del self._connections[project_id]
    
    async def broadcast(self, project_id: str, message: dict):
        """向某个项目的所有连接广播消息"""
        if project_id not in self._connections:
            return
        
        dead = set()
        for ws in self._connections[project_id]:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        
        # 清理断开的连接
        for ws in dead:
            self._connections[project_id].discard(ws)
    
    async def send_to(self, websocket: WebSocket, message: dict):
        """向单个连接发送消息"""
        try:
            await websocket.send_json(message)
        except Exception:
            pass


# 全局实例
ws_manager = WebSocketManager()
