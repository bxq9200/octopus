from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.project import Project
from app.models.node import WorkflowNode

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = ""
    description: str = ""
    config: Dict[str, Any] = {}
    message: str = ""  # 用户输入的第一句话，用于智能生成名称


class ProjectOut(BaseModel):
    id: str
    name: str
    status: str
    created_at: str
    config: Dict[str, Any] = {}
    # 0.7.0：Series 关联字段（前端按这些字段判定 master / episode / 独立项目）
    series_id: Optional[str] = None
    is_episode: bool = False
    episode_index: Optional[int] = None

    class Config:
        from_attributes = True


def generate_project_name(message: str) -> str:
    """从用户输入中提取关键词生成项目名称"""
    import re
    
    if not message:
        return ""
    
    # 去除常见动词和语气词
    stop_words = [
        "帮我", "给我", "想要", "需要", "请", "能不能", "可以", "一下",
        "一个", "一条", "一段", "一些", "份", "个", "条", "段",
        "生成", "创建", "制作", "做", "搞", "弄", "来",
        "视频", "短片", "动画", "图片", "音频", "音乐",
        "的", "了", "吗", "吧", "呢", "啊", "哦", "嗯",
        "关于", "有关", "涉及", "主题", "内容",
        "秒", "分钟", "小时", "天",
        "风格", "类型", "模式", "方式",
        "写实", "卡通", "3d", "二维", "三维", "简约", "极简",
        "一下", "看看", "试试", "想要", "希望"
    ]
    
    # 清理文本
    text = message.strip()
    
    # 去除标点
    text = re.sub(r'[，。！？、；：""''（）【】《》]', ' ', text)
    
    # 去除停用词
    for word in stop_words:
        text = text.replace(word, ' ')
    
    # 去除多余空格
    text = ' '.join(text.split())
    
    # 提取关键词（取前3-5个有意义的词）
    words = [w for w in text.split() if len(w) >= 2 and not w.isdigit()]
    
    if not words:
        return ""
    
    # 组合成项目名称（最多4个词）
    name_words = words[:4]
    return ''.join(name_words)


@router.get("", response_model=List[ProjectOut])
async def list_projects(
    db: AsyncSession = Depends(get_db),
    series_id: Optional[str] = Query(None, description="过滤 series_id；传 'null' 表示 IS NULL（独立项目）"),
    is_episode: Optional[bool] = Query(None, description="True=只取子项目；False=只取非子项目"),
):
    """获取项目列表

    0.7.0：支持按 series_id / is_episode 过滤。
    - 前端「项目」tab 用 ?series_id=null&is_episode=false 拿老独立项目
    - 前端「剧集」tab 用 GET /api/series 拉 Series，不走这里
    """
    from sqlalchemy import select
    stmt = select(Project)
    if series_id is not None:
        if series_id.lower() == "null":
            stmt = stmt.where(Project.series_id.is_(None))
        else:
            stmt = stmt.where(Project.series_id == series_id)
    if is_episode is not None:
        stmt = stmt.where(Project.is_episode == is_episode)
    stmt = stmt.order_by(Project.created_at.desc())
    result = await db.execute(stmt)
    projects = result.scalars().all()
    return [
        ProjectOut(
            id=p.id,
            name=p.name,
            status=p.status,
            created_at=p.created_at.isoformat() if p.created_at else "",
            config=p.config or {},
            series_id=p.series_id,
            is_episode=bool(p.is_episode),
            episode_index=p.episode_index,
        ) for p in projects
    ]


@router.post("", response_model=ProjectOut)
async def create_project(req: ProjectCreate, db: AsyncSession = Depends(get_db)):
    """创建新项目
    
    如果 name 为空，则从 message 中提取关键词生成名称
    """
    # 智能生成名称
    name = req.name
    if not name and req.message:
        name = generate_project_name(req.message)
    
    # 如果还是空，使用默认格式
    if not name:
        from datetime import datetime
        name = f"创作 {datetime.now().strftime('%Y/%m/%d')}"
    
    project = Project(
        name=name,
        description=req.description or req.message,
        config=req.config
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return ProjectOut(
        id=project.id,
        name=project.name,
        status=project.status,
        created_at=project.created_at.isoformat() if project.created_at else "",
        config=project.config or {},
    )


@router.get("/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """获取项目详情"""
    from sqlalchemy import select
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        return {"error": "Project not found"}
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "config": project.config,
        "dag": project.dag,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        # 0.7.0：series 关联
        "series_id": project.series_id,
        "is_episode": bool(project.is_episode),
        "episode_index": project.episode_index,
    }


@router.delete("/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """删除项目"""
    from sqlalchemy import select
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        return {"error": "Project not found"}
    
    await db.delete(project)
    await db.commit()
    return {"status": "deleted", "id": project_id}


@router.put("/{project_id}/dag")
async def update_project_dag(project_id: str, dag: Dict[str, Any], db: AsyncSession = Depends(get_db)):
    """更新项目 DAG"""
    from sqlalchemy import select
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        return {"error": "Project not found"}

    project.dag = dag
    await db.commit()
    return {"status": "updated"}


@router.get("/{project_id}/workflow")
async def get_project_workflow(project_id: str, db: AsyncSession = Depends(get_db)):
    """获取项目工作流快照（用于前端切换/刷新后重建 UI 状态）

    数据来源优先级：
    1. 内存中的 workflow_state（最新）
    2. 数据库 projects.dag（持久化快照）
    """
    from sqlalchemy import select
    from app.engine.workflow_state import workflow_state

    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        return {"error": "Project not found"}

    # 内存里没有 → 先从 DB hydrate 进内存（restore 会跑自愈，把 stale status 修正）
    # 0.6.4：必须 restore 后再 snapshot 拿快照，否则会拿到 restore 前的 stale 数据
    if not workflow_state.get_project(project_id):
        db_dag = project.dag or {}
        if db_dag:
            workflow_state.restore(project_id, db_dag, name=project.name)
    snapshot = workflow_state.snapshot(project_id) if workflow_state.get_project(project_id) else (project.dag or {})

    nodes_dict = (snapshot.get("nodes") or {}) if isinstance(snapshot, dict) else {}
    edges = []
    for node in nodes_dict.values():
        for dep_id in (node.get("depends_on") or []):
            edges.append({
                "id": f"edge_{dep_id}_{node['id']}",
                "source": dep_id,
                "target": node["id"],
                "type": "default",
            })

    project_meta = (snapshot.get("project") or {}) if isinstance(snapshot, dict) else {}
    return {
        "project_id": project_id,
        "name": project.name,
        "phase": project_meta.get("phase"),
        "status": project_meta.get("status") or project.status,
        "chat_history": project_meta.get("chat_history", []),
        "nodes": list(nodes_dict.values()),
        "edges": edges,
    }
