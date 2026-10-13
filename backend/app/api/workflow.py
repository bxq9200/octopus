"""
交互式工作流 API

支持：
1. 对话接口（收集需求）
2. 方案确认
3. 节点确认/重跑
4. 状态查询
5. WebSocket 实时推送
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import Dict, Any, List, Optional

from app.engine.interactive_executor import InteractiveExecutor
from app.engine.workflow_state import workflow_state, NodeStatus
from app.core.websocket import ws_manager
from app.core.database import async_session
from app.models.project import Project
from sqlalchemy import select

router = APIRouter(prefix="/api/workflow", tags=["workflow"])


async def _ensure_project_hydrated(project_id: str):
    """确保项目在内存中（内存无 → 从 DB 恢复 / 创建）。
    **DB.config 永远是 source-of-truth**：无论内存里有没有，**每次**都用 DB.config 覆盖内存 project.config，
    修正 dag 快照里可能残缺/过期的 config —— 否则 is_expert 等专家门控会因内存 config 缺 expert_seeded/series_id 而误判，
    导致 keyframe/video/audio/art 在专家模式下被错误地自动生成。
    """
    in_memory = workflow_state.get_project(project_id) is not None
    async with async_session() as db:
        result = await db.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not in_memory:
            if project and project.dag:
                workflow_state.restore(project_id, project.dag, name=project.name)
            elif project:
                workflow_state.create_project(project_id, name=project.name, config=project.config or {})
            else:
                workflow_state.create_project(project_id)
        # 每次都用 DB.config 覆盖内存 config（含项目已在内存、但 config 残缺/过期的情况）
        if project and project.config:
            ws_project = workflow_state.get_project(project_id)
            if ws_project is not None:
                ws_project["config"] = {**ws_project.get("config", {}), **project.config}

router = APIRouter(prefix="/api/workflow", tags=["workflow"])


# ═══════════════════════════════════════════════════════
# 请求/响应模型
# ═══════════════════════════════════════════════════════

class ChatRequest(BaseModel):
    message: str
    project_id: Optional[str] = None
    context: Dict[str, Any] = {}
    attachments: List[Dict[str, Any]] = []  # [{url, name, mime, size}]


class ConfirmRequest(BaseModel):
    project_id: str
    node_id: Optional[str] = None  # 确认特定节点，None 表示确认方案


class RerunRequest(BaseModel):
    project_id: str
    node_id: str


class NodeActionRequest(BaseModel):
    project_id: str
    node_id: str
    action: str  # "confirm" | "rerun" | "edit"
    params: Dict[str, Any] = {}  # 编辑时的参数


class DeleteNodeRequest(BaseModel):
    project_id: str
    node_id: str
    cascade: bool = True


class PublishExportRequest(BaseModel):
    project_id: str
    node_id: str  # publish 节点 id
    title: str
    description: str
    tags: list[str] = []
    cover_image_url: str = ""
    platform: str = "本地"


class GenerateCoverRequest(BaseModel):
    project_id: str
    node_id: str  # publish 节点 id
    prompt: str  # 英文 prompt（来自 cover_prompt_en 或用户编辑）
    reference_image_urls: list[str] = []  # 可选：参考帧（融合关键帧/首帧）


# ═══════════════════════════════════════════════════════
# 对话接口
# ═══════════════════════════════════════════════════════

@router.post("/chat")
async def workflow_chat(request: ChatRequest):
    """
    工作流对话入口

    处理创作者和 Director Agent 的对话
    """
    project_id = request.project_id or f"proj_{id(request) % 10000:04d}"

    await _ensure_project_hydrated(project_id)

    # 创建执行器
    executor = InteractiveExecutor(
        project_id=project_id,
        ws_callback=lambda msg: ws_manager.broadcast(project_id, msg)
    )

    # 处理对话
    result = await executor.handle_initial_chat(
        request.message, request.context, attachments=request.attachments,
    )
    result["project_id"] = project_id

    return result


# ═══════════════════════════════════════════════════════
# 方案确认
# ═══════════════════════════════════════════════════════

@router.post("/confirm-plan")
async def confirm_plan(request: ConfirmRequest):
    """
    创作者确认执行方案

    触发：
    1. 创建剧本生成节点
    2. 开始执行剧本生成
    """
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg)
    )

    result = await executor.confirm_plan()
    return result


@router.post("/confirm-advanced-outline")
async def confirm_advanced_outline(request: ConfirmRequest):
    """进阶模式：创作者明确确认剧本大纲 → 创建 script 节点（params 带 outline）→ 跑下游"""
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg)
    )
    return await executor.confirm_advanced_outline()


@router.post("/start-episode-script")
async def start_episode_script(request: ConfirmRequest):
    """0.7.0：触发 Episode 的剧本生成（找 IDLE script_generation 节点 → _execute_node）。

    Episode 在 fan-out 时已经 pre-create 了 script 节点（IDLE）。这里让用户从 sidebar
    点 ▶ 直接启动制作，省去先切进 canvas 再点编辑器里"运行"两步。
    """
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg)
    )
    return await executor.start_episode_script()


class ReplaceNodeImageRequest(BaseModel):
    project_id: str
    node_id: str
    image_url: str
    field: str = "image_url"  # 默认 result.image_url；可传 "three_view_image_url"


@router.post("/replace-node-image")
async def replace_node_image(request: ReplaceNodeImageRequest):
    """用用户上传的图直接替换节点的某个 image 字段（result.image_url / three_view_image_url 等）。

    用于：用户对 AI 生成的角色立绘 / 三视图 / 场景图 / 关键帧不满意，想直接传一张自己的图。
    跳过模型生成，节省一次 LLM/Seedream 调用。
    """
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg)
    )
    return await executor.replace_node_image(
        node_id=request.node_id,
        image_url=request.image_url,
        field=request.field,
    )


# ═══════════════════════════════════════════════════════
# 节点操作
# ═══════════════════════════════════════════════════════

@router.post("/node-action")
async def node_action(request: NodeActionRequest):
    """
    节点操作：确认、重跑、编辑
    """
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg)
    )
    
    if request.action == "confirm":
        return await executor.confirm_node(request.node_id)

    if request.action == "rerun":
        return await executor.retry_node(request.node_id)

    if request.action == "skip":
        return await executor.skip_node(request.node_id)

    if request.action == "edit":
        # request.params 既支持单纯 params 编辑，也支持 result 编辑
        # 约定：顶层 key 是要更新的字段（如 "params" / "result" / "outputs"）
        updates: Dict[str, Any] = {}
        for k in ("params", "result", "outputs"):
            if k in request.params:
                updates[k] = request.params[k]
        if not updates:
            # 兼容旧调用：整个 params 当作要更新的 params
            updates["params"] = request.params
        workflow_state.update_node(request.project_id, request.node_id, updates)
        return {"type": "node_edited", "node_id": request.node_id}

    return {"error": f"未知操作: {request.action}"}


class BatchActionRequest(BaseModel):
    project_id: str
    phase: str = "all"          # "all"(keyframe+video) | "keyframe" | "video"
    action: str = "generate"
    node_ids: Optional[List[str]] = None


@router.post("/batch-action")
async def batch_action(request: BatchActionRequest):
    """0.7.1 专家模式：批量触发某 phase 的 IDLE 节点开始生成（手动 fan-out）。"""
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg),
    )
    return await executor.batch_execute_nodes(phase=request.phase, node_ids=request.node_ids)


class LocalizeRequest(BaseModel):
    project_id: str
    node_id: str
    target_languages: List[str] = ["en"]
    translate_type: str = "VOICE"      # VOICE / FACE / SUBTITLE


@router.post("/localize")
async def localize(request: LocalizeRequest):
    """0.7.7 手动触发本地化：把合成成片翻译成用户选的语言（蚂蚁 videotranslate）。"""
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg),
    )
    return await executor.run_localization(
        request.node_id, request.target_languages, request.translate_type,
    )


class AssetActionRequest(BaseModel):
    project_id: str
    action: str                      # create_outfit | create_prop_state | create_prop
    fields: Dict[str, Any] = {}


@router.post("/asset-action")
async def asset_action(request: AssetActionRequest):
    """0.7.2 资产管理：新增服装/道具状态/道具（IDLE 子节点）。删除/单套重生复用 delete-node / node-action rerun。"""
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg),
    )
    return await executor.create_asset(request.action, request.fields)


@router.post("/delete-node")
async def delete_node(request: DeleteNodeRequest):
    """删除节点（默认级联删除下游依赖节点）。

    用途：流程跑歪了想从中间某一步重来，先把出错节点及其下游全删掉，
    再去重跑上游节点重新触发 fan-out。
    """
    await _ensure_project_hydrated(request.project_id)
    deleted = workflow_state.delete_node(
        request.project_id, request.node_id, cascade=request.cascade
    )
    if not deleted:
        return {"error": "节点不存在", "node_id": request.node_id}
    return {
        "type": "nodes_deleted",
        "node_ids": deleted,
        "count": len(deleted),
    }


# ═══════════════════════════════════════════════════════
# 发布
# ═══════════════════════════════════════════════════════

@router.post("/publish/export")
async def publish_export(request: PublishExportRequest):
    """用户在 PublishEditor 编辑完文案 + 选好封面后，触发实际导出"""
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg),
    )
    return await executor.publish_export(
        node_id=request.node_id,
        title=request.title,
        description=request.description,
        tags=request.tags,
        cover_image_url=request.cover_image_url,
        platform=request.platform,
    )


@router.post("/publish/generate-cover")
async def publish_generate_cover(request: GenerateCoverRequest):
    """让 AI 重新生成一张封面（image_to_image 多图融合）"""
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg),
    )
    return await executor.publish_generate_cover(
        node_id=request.node_id,
        prompt=request.prompt,
        reference_image_urls=request.reference_image_urls,
    )


# ═══════════════════════════════════════════════════════
# 视频合并
# ═══════════════════════════════════════════════════════

@router.post("/start-merge")
async def start_merge(request: ConfirmRequest):
    """
    开始视频合并

    在对话框中用户确认合并后调用
    """
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg)
    )

    return await executor.start_merge()


@router.post("/start-storyboard")
async def start_storyboard(request: ConfirmRequest):
    """开始分镜设计

    art_design 主节点 + 所有角色/场景子节点全部确认后，前端弹的对话框确认后调用。
    会找最新一个已确认的 art_design 节点 → 建 storyboard 节点并执行。
    """
    await _ensure_project_hydrated(request.project_id)
    executor = InteractiveExecutor(
        project_id=request.project_id,
        ws_callback=lambda msg: ws_manager.broadcast(request.project_id, msg)
    )
    return await executor.start_storyboard()


# ═══════════════════════════════════════════════════════
# 状态查询
# ═══════════════════════════════════════════════════════

@router.get("/status/{project_id}")
async def get_status(project_id: str):
    """获取项目完整状态"""
    executor = InteractiveExecutor(project_id=project_id)
    return executor.get_project_status()


@router.get("/dag/{project_id}")
async def get_dag(project_id: str):
    """获取项目 DAG 结构"""
    dag = workflow_state.get_dag_structure(project_id)
    return dag


@router.get("/node/{project_id}/{node_id}")
async def get_node(project_id: str, node_id: str):
    """获取节点详情"""
    node = workflow_state.get_node(project_id, node_id)
    if not node:
        return {"error": "节点不存在"}
    return node


@router.post("/reregister-assets/{project_id}")
async def reregister_character_assets(project_id: str):
    """0.7.3 人物一致性实验：清角色旧 asset_id → 用 composite(正脸+三视图)重新入火山 asset 库。
    返回每个角色的新 asset_id（None=composite 入库失败，需回退 face_close_up）。同步清 video 节点 first_frame_asset_id。"""
    # 启动不预加载项目 → 按需 restore from DB dag（自包含，不依赖前端是否打开过）
    if not workflow_state.get_project(project_id):
        from app.models.project import Project
        from app.core.database import async_session
        from sqlalchemy import select
        async with async_session() as db:
            p = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
            if p and p.dag:
                workflow_state.restore(project_id, p.dag, p.name)
    if not workflow_state.get_project(project_id):
        return {"ok": False, "msg": "项目不存在"}
    executor = InteractiveExecutor(
        project_id=project_id,
        ws_callback=lambda msg: ws_manager.broadcast(project_id, msg),
    )
    char_ids = []
    for n in workflow_state.get_all_nodes(project_id):
        r = n.get("result") or {}
        if r.get("result_type") == "character":
            char_ids.append(n["id"])
            new_r = {**r}
            new_r.pop("character_asset_id", None)  # 清旧 face asset_id，否则 _register 会 skip
            workflow_state.update_node(project_id, n["id"], {"result": new_r})
        # 清 video 节点关键帧 asset 缓存（新旧字段都清），让关键帧重新入库拿火山 id
        elif n.get("type") == "video" and (r.get("first_frame_volc_id") or r.get("first_frame_asset_id")):
            new_r = {**r}
            new_r.pop("first_frame_volc_id", None)
            new_r.pop("first_frame_asset_id", None)
            workflow_state.update_node(project_id, n["id"], {"result": new_r})
    if not char_ids:
        return {"ok": False, "msg": "无角色节点"}
    await executor._register_all_character_assets(char_ids)
    result = {}
    for nid in char_ids:
        r = (workflow_state.get_node(project_id, nid) or {}).get("result") or {}
        result[r.get("name") or nid[:8]] = r.get("character_asset_id")
    return {"ok": True, "assets": result}


@router.post("/reregister-assets-tv/{project_id}")
async def reregister_with_three_view(project_id: str):
    """0.7.3 实验：重新生成三视图(新鲜火山 TOS origin_url) → 入 asset 库 → 当角色锚定。
    测「火山 TOS 上新鲜的多角度图」能否过 asset 校验(之前 composite 是本地拼图卡处理、老三视图是过期卡下载，都没测到这个)。"""
    if not workflow_state.get_project(project_id):
        from app.models.project import Project
        from app.core.database import async_session
        from sqlalchemy import select
        async with async_session() as db:
            p = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
            if p and p.dag:
                workflow_state.restore(project_id, p.dag, p.name)
    if not workflow_state.get_project(project_id):
        return {"ok": False, "msg": "项目不存在"}
    executor = InteractiveExecutor(
        project_id=project_id,
        ws_callback=lambda msg: ws_manager.broadcast(project_id, msg),
    )
    from app.modules._antchain_video import register_character_asset
    result = {}
    for n in workflow_state.get_all_nodes(project_id):
        r = n.get("result") or {}
        if r.get("result_type") != "character" or (r.get("subject_type") or "human").lower() != "human":
            continue
        name = r.get("name") or n["id"][:8]
        front = r.get("image_url")
        if not front:
            result[name] = "无正面图，跳过"
            continue
        # 1. 重新生成三视图 → 新鲜火山 origin_url（火山 Seedream 图生图，产物是 TOS 原生）
        try:
            tv_img = await executor._generate_image_from_image(
                front, executor.THREE_VIEW_IMG2IMG_PROMPT, width=1344, height=768,
            )
        except Exception as e:
            result[name] = f"三视图重生成失败: {e}"
            continue
        tv_origin = tv_img.get("origin_url")
        tv_local = tv_img.get("image_url")
        if not tv_origin:
            result[name] = f"三视图无 origin_url(provider 非火山?) local={tv_local}"
            continue
        # 2. 更新节点（新三视图 + 清旧 asset_id + 清 first_frame 缓存）
        new_r = {**r, "three_view_image_url": tv_local, "three_view_origin_url": tv_origin}
        new_r.pop("character_asset_id", None)
        workflow_state.update_node(project_id, n["id"], {"result": new_r})
        # 3. 三视图 origin_url 入库（火山 TOS 新鲜多角度图）
        aid = await register_character_asset(tv_origin, f"{name}_tv")
        if aid:
            r2 = (workflow_state.get_node(project_id, n["id"]) or {}).get("result") or {}
            workflow_state.update_node(project_id, n["id"], {"result": {**r2, "character_asset_id": aid}})
            result[name] = f"OK asset_id={aid}"
        else:
            result[name] = "三视图入库失败/卡PROCESSING(多figure校验拒?)"
    return {"ok": True, "results": result}


# ═══════════════════════════════════════════════════════
# WebSocket
# ═══════════════════════════════════════════════════════

@router.websocket("/ws/{project_id}")
async def workflow_websocket(websocket: WebSocket, project_id: str):
    """
    工作流 WebSocket
    
    实时推送：
    - 节点状态变化
    - 新节点创建
    - 执行进度
    - 需要创作者确认的通知
    """
    await ws_manager.connect(websocket, project_id)

    try:
        while True:
            # 接收前端消息
            data = await websocket.receive_json()
            
            action = data.get("action")
            node_id = data.get("node_id")
            
            if action == "confirm_node" and node_id:
                executor = InteractiveExecutor(
                    project_id=project_id,
                    ws_callback=lambda msg: ws_manager.broadcast(project_id, msg)
                )
                await executor.confirm_node(node_id)

            elif action == "rerun_node" and node_id:
                executor = InteractiveExecutor(
                    project_id=project_id,
                    ws_callback=lambda msg: ws_manager.broadcast(project_id, msg)
                )
                await executor.retry_node(node_id)
            
            elif action == "start_merge":
                # 前端开始合并
                executor = InteractiveExecutor(
                    project_id=project_id,
                    ws_callback=lambda msg: ws_manager.broadcast(project_id, msg)
                )
                await executor.start_merge()

            elif action == "start_storyboard":
                # 前端确认 art 物料全部完成 → 进入分镜
                executor = InteractiveExecutor(
                    project_id=project_id,
                    ws_callback=lambda msg: ws_manager.broadcast(project_id, msg)
                )
                await executor.start_storyboard()

            elif action == "ping":
                await websocket.send_json({"type": "pong"})
    
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, project_id)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("WebSocket 错误: %s", e)
        ws_manager.disconnect(websocket, project_id)


# ═══════════════════════════════════════════════════════
# 调试接口
# ═══════════════════════════════════════════════════════

@router.get("/projects")
async def list_projects():
    """列出所有项目（调试用）"""
    return workflow_state.get_all_projects()
