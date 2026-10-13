import logging
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.database import init_db
from app.core.websocket import ws_manager
from app.api import mv as mv_api, projects, agents, workflow, publishers, uploads, series, auth
from app.api import settings as settings_api
from app.api import app_control


# 0.6.1+: 让 app.* 的 INFO/WARNING/ERROR 日志写到 uvicorn stdout，
# 这样 backend.log 里能看到 Mubert / agent / executor 的关键调试信息
# （之前只有 uvicorn 自带 + sqlalchemy.engine.Engine 写入，应用 logger 完全沉默）
_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, force=True)
for noisy in ("httpx", "httpcore", "watchfiles"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
# sqlalchemy.engine 已经太啰嗦，保持现状（uvicorn 配的就是这俩 INFO）
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)

app = FastAPI(
    title="Octopus API",
    description="对话式 AI 视频创作操作系统 API",
    version=settings.APP_VERSION,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件服务 — 输出的图片、视频
output_dir = settings.OUTPUT_DIR
os.makedirs(os.path.join(output_dir, "images"), exist_ok=True)
os.makedirs(os.path.join(output_dir, "videos"), exist_ok=True)
app.mount("/output", StaticFiles(directory=output_dir), name="output")

# 路由
app.include_router(projects.router)
app.include_router(agents.router)
app.include_router(workflow.router)
app.include_router(settings_api.router)
app.include_router(publishers.router)
app.include_router(uploads.router)
app.include_router(series.router)  # 0.7.0 专家模式：Series CRUD
app.include_router(auth.router)    # 0.8 缝5：账号 / token 管道（local 下不鉴权，仅落会话）
app.include_router(mv_api.router)   # 1.1.0 MV 模式：开跑 / 歌词轴校对 / 本地试算镜头
app.include_router(app_control.router)  # 应用自身：版本 / 退出 / 检查更新 / 一键升级
# 旧的 /api/chat 路由（一次性 DAG 流程）已退役，前端使用 /api/workflow/* 走交互式流程


@app.on_event("startup")
async def startup():
    # 显式 import 所有 SQLAlchemy 模型，确保 init_db() 能 create_all
    from app.models import Project, WorkflowNode, UserSetting, Series  # noqa: F401
    await init_db()

    # 把用户级配置（API key/模型选择/平台凭证）灌进内存缓存，
    # 之后模块同步 user_settings.get(...) 不再访问 DB
    from app.core.user_settings_service import user_settings
    await user_settings.hydrate()
    # 注入持久化回调：状态变更时把 snapshot 写到 projects.dag 字段
    from app.engine.workflow_state import workflow_state
    from app.models.project import Project
    from app.core.database import async_session
    from sqlalchemy import select

    async def _persist(project_id: str, snapshot: dict):
        try:
            async with async_session() as db:
                result = await db.execute(select(Project).where(Project.id == project_id))
                project = result.scalar_one_or_none()
                if not project:
                    return
                project.dag = snapshot
                # 同步 phase/status 到 Project 顶层字段
                phase = (snapshot.get("project") or {}).get("phase")
                status = (snapshot.get("project") or {}).get("status")
                if phase:
                    project.config = {**(project.config or {}), "phase": phase}
                if status:
                    project.status = status
                await db.commit()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("持久化失败 (project=%s): %s", project_id, e)

    workflow_state.register_persist_callback(_persist)


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}


# WebSocket 端点 — DAG 执行状态实时推送
@app.websocket("/ws/{project_id}")
async def websocket_endpoint(websocket: WebSocket, project_id: str):
    await ws_manager.connect(websocket, project_id)
    try:
        while True:
            # 接收前端消息（必要保持心跳）
            data = await websocket.receive_text()
            # 可以根据需要处理前端发来的命令
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, project_id)


# ─────────────────────────────────────────────────────────────
# 桌面版：服务打包好的前端 dist（仅当 OCTOPUS_FRONTEND_DIST 指向有效目录时启用）。
# dev 模式不设这个 env → 前端仍由 vite(5173) 服务，此段完全不生效，行为零变化。
# 必须放在所有 /api /output /ws /health 路由**之后**注册，catch-all 只兜前端路由。
# ─────────────────────────────────────────────────────────────
_frontend_dist = os.environ.get("OCTOPUS_FRONTEND_DIST", "")
if _frontend_dist and os.path.isdir(_frontend_dist):
    from fastapi.responses import FileResponse

    _assets_dir = os.path.join(_frontend_dist, "assets")
    if os.path.isdir(_assets_dir):
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")
    _index_html = os.path.join(_frontend_dist, "index.html")

    from fastapi import HTTPException as _HTTPException

    @app.get("/{full_path:path}")
    async def _serve_spa(full_path: str):
        # 后端保留前缀不兜底：让未命中的 API 返回真 404，而不是把 index.html 当 JSON 喂前端
        if full_path.split("/", 1)[0] in ("api", "output", "ws", "health"):
            raise _HTTPException(status_code=404)
        # 防目录穿越；命中真实文件就返回，否则回 index.html（SPA 前端路由兜底）
        safe = os.path.normpath(full_path).lstrip("\\/").replace("..", "")
        candidate = os.path.join(_frontend_dist, safe)
        if safe and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(_index_html)

    logging.getLogger(__name__).info("[desktop] 前端 dist 已挂载: %s", _frontend_dist)
