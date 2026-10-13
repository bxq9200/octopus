"""应用自身的控制面：版本信息、退出、检查更新、下载并安装新版。

为什么需要「退出」：桌面版是「本地后端 + 浏览器界面」，用户关掉网页时后端进程还活着 ——
①任务管理器里一直挂着，观感奇怪 ②装新版时 exe 被占用，安装程序覆盖不了（用户得手动杀进程）。
所以给一个明确的退出入口，并让升级流程自动调用它。
"""
import asyncio
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/app", tags=["app"])

# 下载进度（简单单例：同一时间只允许一个下载任务）
_dl_state: Dict[str, Any] = {"status": "idle", "percent": 0, "path": "", "error": "", "version": ""}


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _parse_ver(v: str) -> tuple:
    """'1.2.10' → (1,2,10)，非数字段落按 0。用于版本比较（字符串比较会把 '1.10' 判小于 '1.9'）。"""
    out = []
    for part in str(v or "0").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


@router.get("/info")
async def app_info() -> Dict[str, Any]:
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "packaged": _is_frozen(),          # 源码运行时不提供升级/退出（那是开发态）
        "pid": os.getpid(),
    }


@router.post("/quit")
async def app_quit() -> Dict[str, Any]:
    """退出应用（关掉本地后端进程）。

    先把 HTTP 响应回给前端，再延迟真正退出 —— 否则前端拿不到结果、会显示网络错误。
    用 `os._exit` 而非 SIGTERM：uvicorn.run 是阻塞主线程跑的，`os._exit` 最直接可靠，
    且此时没有需要 flush 的状态（DB 每步都已提交）。
    """
    logger.info("[app] 收到退出请求，0.6s 后关闭进程 pid=%s", os.getpid())

    def _bye():
        time.sleep(0.6)
        os._exit(0)

    threading.Thread(target=_bye, daemon=True).start()
    return {"ok": True, "message": "正在退出…"}


# ═══════════════════════════════════════════════════════
# 升级
# ═══════════════════════════════════════════════════════

def _gateway() -> str:
    from app.core.runtime_config import octopus_cloud_config
    return (octopus_cloud_config().gateway_url or "").rstrip("/")


@router.get("/update/check")
async def update_check() -> Dict[str, Any]:
    """问云端最新版本，和本机版本比对。网络不通不算错误，回 available=False。"""
    cur = settings.APP_VERSION
    gw = _gateway()
    if not gw:
        return {"current": cur, "available": False, "reason": "未配置云端网关"}
    try:
        from app.core import cloud_http
        resp = await cloud_http.get(f"{gw}/v1/app/latest", timeout=15.0)
        if resp.status_code != 200:
            return {"current": cur, "available": False, "reason": f"HTTP {resp.status_code}"}
        d = resp.json() or {}
    except Exception as e:
        return {"current": cur, "available": False, "reason": f"连不上云端：{str(e)[:100]}"}

    latest = str(d.get("version") or "")
    newer = bool(latest) and _parse_ver(latest) > _parse_ver(cur)
    return {
        "current": cur,
        "latest": latest,
        "available": newer,
        "url": d.get("url") or "",
        "notes": d.get("notes") or "",
        "size_mb": d.get("size_mb") or 0,
        "packaged": _is_frozen(),
    }


class DownloadRequest(BaseModel):
    url: str
    version: str = ""


async def _download_installer(url: str, version: str) -> None:
    """后台下载安装包到临时目录，进度写进 _dl_state。"""
    import tempfile
    _dl_state.update(status="downloading", percent=0, path="", error="", version=version)
    try:
        tmp_dir = os.path.join(tempfile.gettempdir(), "octopus_update")
        os.makedirs(tmp_dir, exist_ok=True)
        name = url.split("/")[-1] or f"OctopusSetup-{version}.exe"
        dest = os.path.join(tmp_dir, name)

        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as c:
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                done = 0
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes(1024 * 256):
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            _dl_state["percent"] = min(99, int(done * 100 / total))
        _dl_state.update(status="ready", percent=100, path=dest)
        logger.info("[app] 新版已下载: %s", dest)
    except Exception as e:
        logger.warning("[app] 下载新版失败: %s", e)
        _dl_state.update(status="failed", error=str(e)[:200])


@router.post("/update/download")
async def update_download(req: DownloadRequest) -> Dict[str, Any]:
    if not _is_frozen():
        raise HTTPException(400, "开发模式不支持自动升级")
    if not (req.url or "").startswith(("http://", "https://")):
        raise HTTPException(400, "无效的下载地址")
    if _dl_state["status"] == "downloading":
        return {"ok": True, "status": "downloading", "percent": _dl_state["percent"]}
    asyncio.create_task(_download_installer(req.url, req.version))
    return {"ok": True, "status": "downloading", "percent": 0}


@router.get("/update/progress")
async def update_progress() -> Dict[str, Any]:
    return dict(_dl_state)


@router.post("/update/install")
async def update_install() -> Dict[str, Any]:
    """启动安装程序，然后退出自己 —— 这样安装程序才能覆盖被占用的 exe。

    安装包是 per-user 的 Inno Setup，双击即装、不弹 UAC；装完会自动拉起新版本。
    """
    path = _dl_state.get("path") or ""
    if _dl_state.get("status") != "ready" or not os.path.isfile(path):
        raise HTTPException(400, "安装包尚未下载完成")

    import subprocess

    def _run_and_exit():
        time.sleep(0.5)
        try:
            # /SILENT：只显示进度条不问问题；/NOCANCEL 防中途取消留下半截
            subprocess.Popen([path, "/SILENT", "/NOCANCEL"],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            logger.info("[app] 已启动安装程序: %s", path)
        except Exception as e:
            logger.warning("[app] 启动安装程序失败: %s", e)
        time.sleep(1.0)
        os._exit(0)          # 让出 exe 占用，安装程序才能覆盖

    threading.Thread(target=_run_and_exit, daemon=True).start()
    return {"ok": True, "message": "安装程序已启动，Octopus 即将退出并自动更新"}
