"""Series API（0.7.0 专家模式）

POST   /api/series                          创建 Series + 同步建 master Project
GET    /api/series                          列出所有 Series（含 episode_count）
GET    /api/series/{id}                     Series 详情 + episodes[]
PATCH  /api/series/{id}                     改 name / description
DELETE /api/series/{id}                     级联删除 episodes + master + uploads
POST   /api/series/{id}/fanout-episodes     fan-out N 个 Episode（Step 10 实现）
POST   /api/series/{id}/sync-assets         同步资产到子集（Step 11 实现）
"""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.project import Project
from app.models.series import Series
from app.models.node import WorkflowNode

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/series", tags=["series"])


# ─────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────

class SeriesCreate(BaseModel):
    name: str = ""
    description: str = ""
    message: str = ""  # 给智能命名用


class SeriesPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class EpisodeBrief(BaseModel):
    id: str
    name: str
    status: str
    episode_index: Optional[int] = None
    created_at: Optional[str] = None
    # 0.7.0：本集当前阶段（项目级 phase，前端按此渲染徽章）
    phase: Optional[str] = None


class SeriesBrief(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    status: str
    master_project_id: Optional[str] = None
    episode_count: int = 0
    created_at: Optional[str] = None


class SeriesDetail(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    status: str
    master_project_id: Optional[str] = None
    script_summary: Dict[str, Any] = {}
    episodes_breakdown: List[Dict[str, Any]] = []
    shared_assets_frozen: bool = False  # True 说明 art 已冻结，可 fan-out
    episodes: List[EpisodeBrief] = []
    created_at: Optional[str] = None


# ─────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────

@router.get("", response_model=List[SeriesBrief])
async def list_series(db: AsyncSession = Depends(get_db)) -> List[SeriesBrief]:
    rows = (await db.execute(select(Series).order_by(Series.created_at.desc()))).scalars().all()
    # 一次性算 episode_count（避免 N+1）
    counts: Dict[str, int] = {}
    if rows:
        ep_rows = (await db.execute(
            select(Project.series_id).where(
                Project.series_id.in_([s.id for s in rows]),
                Project.is_episode == True,  # noqa: E712
            )
        )).all()
        for (sid,) in ep_rows:
            counts[sid] = counts.get(sid, 0) + 1
    return [
        SeriesBrief(
            id=s.id, name=s.name, description=s.description,
            status=s.status, master_project_id=s.master_project_id,
            episode_count=counts.get(s.id, 0),
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in rows
    ]


@router.post("", response_model=SeriesDetail)
async def create_series(req: SeriesCreate, db: AsyncSession = Depends(get_db)) -> SeriesDetail:
    """建 Series + 同步建一个 master Project（is_episode=False, mode=expert）"""
    # 智能命名
    from app.api.projects import generate_project_name
    name = req.name or generate_project_name(req.message) or f"剧集 {datetime.now().strftime('%Y/%m/%d')}"

    # 1. 先建 series 行（master_project_id 留 null）
    series = Series(
        id=str(uuid.uuid4()),
        name=name,
        description=req.description or req.message or "",
        status="draft",
    )
    db.add(series)
    await db.flush()  # 拿到 id 但还没 commit

    # 2. 建 master project（指向 series.id）
    master = Project(
        id=str(uuid.uuid4()),
        name=f"{name} · 总项目",
        description=f"{name} 的视觉开发与分集协商",
        config={"mode": "expert"},
        series_id=series.id,
        is_episode=False,
    )
    db.add(master)
    await db.flush()

    # 3. 回填 series.master_project_id
    series.master_project_id = master.id
    await db.commit()
    await db.refresh(series)

    logger.info("created series %s with master_project %s", series.id, master.id)
    return _series_to_detail(series, episodes=[])


@router.get("/{series_id}", response_model=SeriesDetail)
async def get_series(series_id: str, db: AsyncSession = Depends(get_db)) -> SeriesDetail:
    series = (await db.execute(select(Series).where(Series.id == series_id))).scalar_one_or_none()
    if not series:
        raise HTTPException(404, "Series not found")
    # 反查 episodes
    ep_rows = (await db.execute(
        select(Project).where(
            Project.series_id == series_id,
            Project.is_episode == True,  # noqa: E712
        ).order_by(Project.episode_index.asc().nulls_last())
    )).scalars().all()
    episodes = [
        EpisodeBrief(
            id=p.id, name=p.name, status=p.status,
            episode_index=p.episode_index,
            created_at=p.created_at.isoformat() if p.created_at else None,
            # 0.7.0：从 config.phase 取本集当前阶段（workflow_state 推进时持久化进 config）
            phase=(p.config or {}).get("phase"),
        )
        for p in ep_rows
    ]
    return _series_to_detail(series, episodes=episodes)


@router.patch("/{series_id}", response_model=SeriesDetail)
async def patch_series(series_id: str, req: SeriesPatch, db: AsyncSession = Depends(get_db)) -> SeriesDetail:
    series = (await db.execute(select(Series).where(Series.id == series_id))).scalar_one_or_none()
    if not series:
        raise HTTPException(404, "Series not found")
    if req.name is not None:
        series.name = req.name
    if req.description is not None:
        series.description = req.description
    await db.commit()
    await db.refresh(series)
    return await get_series(series_id, db)


@router.delete("/{series_id}")
async def delete_series(series_id: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """级联：所有 Episode 项目 → master Project → series → uploads / output 目录"""
    series = (await db.execute(select(Series).where(Series.id == series_id))).scalar_one_or_none()
    if not series:
        raise HTTPException(404, "Series not found")

    # 1. 收集所有相关 project id（master + episodes）
    project_ids_rows = (await db.execute(
        select(Project.id).where(Project.series_id == series_id)
    )).all()
    project_ids = [pid for (pid,) in project_ids_rows]

    # 2. 删 workflow_nodes（如果有 FK 关联，直接 DELETE）
    if project_ids:
        await db.execute(delete(WorkflowNode).where(WorkflowNode.project_id.in_(project_ids)))

    # 3. 删 projects（含 master + episodes）
    if project_ids:
        await db.execute(delete(Project).where(Project.id.in_(project_ids)))

    # 4. 删 series 本身
    await db.delete(series)
    await db.commit()

    # 5. 清理 uploads 目录（master 名下持有所有 series 期资产；episode 名下可能存自己的素材）
    uploads_root = os.path.join(settings.OUTPUT_DIR, "uploads")
    for pid in project_ids:
        try:
            d = os.path.join(uploads_root, pid)
            if os.path.isdir(d):
                shutil.rmtree(d)
        except Exception as e:
            logger.warning("删 uploads %s 失败: %s", pid, e)

    # 6. 内存里清理 workflow_state
    try:
        from app.engine.workflow_state import workflow_state
        for pid in project_ids:
            workflow_state._projects.pop(pid, None)
            workflow_state._nodes.pop(pid, None)
    except Exception as e:
        logger.warning("清 workflow_state 失败: %s", e)

    return {"status": "deleted", "series_id": series_id, "deleted_projects": len(project_ids)}


# ─────────────────────────────────────────
# Step 10 / 11 占位（实际实现在那两步）
# ─────────────────────────────────────────

@router.post("/{series_id}/reset-episodes")
async def reset_episodes(series_id: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """删掉该 series 下的所有 Episode 项目（保留 master + breakdown + shared_assets），
    通常用于：架构变更后想用新 fanout 重做，但不想重新通读剧本 / 重跑 master 视觉开发。
    """
    series = (await db.execute(select(Series).where(Series.id == series_id))).scalar_one_or_none()
    if not series:
        raise HTTPException(404, "Series not found")

    # 收集所有 Episode 项目 id
    rows = (await db.execute(
        select(Project.id).where(
            Project.series_id == series_id,
            Project.is_episode == True,  # noqa: E712
        )
    )).all()
    ep_ids = [pid for (pid,) in rows]

    if not ep_ids:
        return {"status": "no_episodes", "deleted": 0, "series_id": series_id}

    # 删 workflow_nodes（如果有 FK）
    await db.execute(delete(WorkflowNode).where(WorkflowNode.project_id.in_(ep_ids)))
    # 删 Episode 项目行
    await db.execute(delete(Project).where(Project.id.in_(ep_ids)))
    # series 状态退回 visual_dev（资产还冻结，但已无子集）
    series.status = "visual_dev"
    await db.commit()

    # 清 workflow_state in-memory
    try:
        from app.engine.workflow_state import workflow_state
        for pid in ep_ids:
            workflow_state._projects.pop(pid, None)
            workflow_state._nodes.pop(pid, None)
    except Exception as e:
        logger.warning("清 workflow_state 失败：%s", e)

    # 清 uploads（episode 名下，通常为空，但保险删一遍）
    import os, shutil
    uploads_root = os.path.join(settings.OUTPUT_DIR, "uploads")
    for pid in ep_ids:
        try:
            d = os.path.join(uploads_root, pid)
            if os.path.isdir(d):
                shutil.rmtree(d)
        except Exception as e:
            logger.warning("删 uploads %s 失败：%s", pid, e)

    return {
        "status": "reset",
        "deleted": len(ep_ids),
        "series_id": series_id,
        "message": f"已清空 {len(ep_ids)} 集子项目，可以重新「开始制作」",
    }


@router.post("/{series_id}/fanout-episodes")
async def fanout_episodes(series_id: str, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """fan-out N 个 Episode（0.7.0 Step 10）"""
    series = (await db.execute(select(Series).where(Series.id == series_id))).scalar_one_or_none()
    if not series:
        raise HTTPException(404, "Series not found")
    if not series.master_project_id:
        raise HTTPException(400, "Series 缺 master_project_id")

    # 通过 master project 上的 InteractiveExecutor 跑 fan-out（沿用其 state + ws_callback）
    from app.api.workflow import _ensure_project_hydrated  # 复用 workflow.py 的内存兜底逻辑
    from app.engine.interactive_executor import InteractiveExecutor
    from app.core.websocket import ws_manager
    await _ensure_project_hydrated(series.master_project_id)
    executor = InteractiveExecutor(
        project_id=series.master_project_id,
        ws_callback=lambda msg: ws_manager.broadcast(series.master_project_id, msg),
    )
    result = await executor.fanout_episodes()
    if "error" in result:
        # 把 InteractiveExecutor 返回的业务错误转成 4xx
        raise HTTPException(400, result["error"])
    return result


class SyncAssetsReq(BaseModel):
    target: str = "all_unstarted"  # all_unstarted | all | selected
    episode_ids: List[str] = []


@router.post("/{series_id}/sync-assets")
async def sync_assets(series_id: str, req: SyncAssetsReq, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """同步资产到子集（0.7.0 Step 11）"""
    series = (await db.execute(select(Series).where(Series.id == series_id))).scalar_one_or_none()
    if not series:
        raise HTTPException(404, "Series not found")
    if not series.master_project_id:
        raise HTTPException(400, "Series 缺 master_project_id")

    from app.api.workflow import _ensure_project_hydrated
    from app.engine.interactive_executor import InteractiveExecutor
    from app.core.websocket import ws_manager
    await _ensure_project_hydrated(series.master_project_id)
    # 同步前也尽量 hydrate 各 Episode（不存在的会被 sync 跳过）
    for ep in (await db.execute(
        select(Project.id).where(Project.series_id == series_id, Project.is_episode == True)  # noqa: E712
    )).all():
        try:
            await _ensure_project_hydrated(ep[0])
        except Exception:
            pass
    executor = InteractiveExecutor(
        project_id=series.master_project_id,
        ws_callback=lambda msg: ws_manager.broadcast(series.master_project_id, msg),
    )
    result = await executor.sync_episodes_assets(target=req.target, episode_ids=req.episode_ids)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def _series_to_detail(series: Series, episodes: List[EpisodeBrief]) -> SeriesDetail:
    shared = series.shared_assets or {}
    return SeriesDetail(
        id=series.id, name=series.name, description=series.description,
        status=series.status, master_project_id=series.master_project_id,
        script_summary=series.script_summary or {},
        episodes_breakdown=series.episodes_breakdown or [],
        shared_assets_frozen=bool(shared.get("frozen_at")),
        episodes=episodes,
        created_at=series.created_at.isoformat() if series.created_at else None,
    )
