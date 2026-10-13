"""ComfyUI Qwen-Image-Edit 多角度 provider（Tier 1：场景机位板真·转机位）。

输入一张场景图 + 角度提示词（`<sks> ...`）→ 输出该房间另一机位的图。
- 工作流从磁盘 json（API 格式）**运行时加载**：用户在 ComfyUI 里调的质量/步数/cfg/LoRA 强度直接跟随，代码不写死。
- 关键节点按 class_type 定位（LoadImage / KSampler / 正向 TextEncode），不依赖固定 node_id。
- 连本地 ComfyUI（默认 127.0.0.1:8188，可在 WSL）；失败抛异常，调用方回退 Seedream。
"""
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

from app.core.paths import images_dir
from app.core.runtime_config import comfyui_config, scene_camera_workflow
from app.modules._comfyui_video import _upload_image  # 复用上传逻辑（本地/远程图都能传进 ComfyUI input）

logger = logging.getLogger(__name__)

_WORKFLOW_FILENAME = "comfyui-workflow-multiple-angles.json"


def resolve_workflow_path(user_path: str = "") -> str:
    """转视角工作流 JSON 路径解析：用户设置 > 打包内置(_MEIPASS) > 仓库根默认。返回第一个存在的。
    这样 dev（仓库根有 json）和打包版（json 进 bundle）都能用，不再写死 D:\\BELLE 绝对路径。"""
    # 1) 用户在「本地 ComfyUI」设置里指定的
    if user_path and os.path.isfile(user_path):
        return user_path
    # 2) 打包内置：PyInstaller 把 json 打进 bundle 根（sys._MEIPASS）
    if getattr(sys, "frozen", False):
        cand = os.path.join(getattr(sys, "_MEIPASS", ""), _WORKFLOW_FILENAME)
        if os.path.isfile(cand):
            return cand
    # 3) 仓库根（dev）：backend/app/modules/_comfyui_qwen.py → parents[3] = octopus/
    try:
        cand = Path(__file__).resolve().parents[3] / _WORKFLOW_FILENAME
        if cand.is_file():
            return str(cand)
    except Exception:
        pass
    # 4) 兜底：返回用户路径（哪怕不存在，让 _load_workflow 报清楚的错）
    return user_path or ""
_POLL_TIMEOUT_S = 600      # 单张转机位：Lightning ~20s / 满血 cfg4·20步可到几分钟，放宽到 10min
_POLL_INTERVAL_S = 2.0
# 全局串行：24G 卡上 Qwen(fp8 19.6G)+文本编码器(9G)装不下、每次要换模型；多场景并发出板会把显存
# 挤爆 → lowvram 龟速。串行(Semaphore1)让 Octopus 像网页一样一次只发一张。
_QWEN_SEM = asyncio.Semaphore(1)


def _load_workflow(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        wf = json.load(f)
    if not isinstance(wf, dict) or not wf:
        raise ValueError("工作流 json 不是 API 格式（应为 {node_id: {class_type,inputs}}；ComfyUI 里用「保存(API格式)」导出）")
    return wf


def _find_node(wf: dict, class_type: str):
    for nid, node in wf.items():
        if isinstance(node, dict) and node.get("class_type") == class_type:
            return nid, node
    return None, None


def _resolve_positive_encode(wf: dict):
    """从 KSampler.positive 顺着 conditioning 链找到**正向** TextEncode 节点
    （区分正/负：两个 TextEncodeQwenImageEditPlus 里，KSampler.positive 能到达的那个才是正向）。"""
    _ks_id, ks = _find_node(wf, "KSampler")
    if not ks:
        return None
    link = (ks.get("inputs") or {}).get("positive")
    seen = set()
    while isinstance(link, list) and link and link[0] not in seen:
        nid = link[0]
        seen.add(nid)
        node = wf.get(nid)
        if not isinstance(node, dict):
            break
        if "TextEncode" in (node.get("class_type") or ""):
            return node
        # 透传类（FluxKontextMultiReferenceLatentMethod 等）继续沿 conditioning 往上找
        link = (node.get("inputs") or {}).get("conditioning")
    return None


async def generate_angle_view(
    image_url: str,
    angle_prompt: str,
    *,
    workflow_path: str = None,
    seed: int = -1,
    progress_cb=None,
) -> dict:
    """场景图 → 指定角度的新机位图。返回 {image_url, provider, prompt}。失败抛异常。
    全局串行(_QWEN_SEM)：避免多场景并发出板把 ComfyUI 24G 显存挤爆 → lowvram 龟速。"""
    async with _QWEN_SEM:
        return await _generate_angle_view_inner(
            image_url, angle_prompt, workflow_path=workflow_path, seed=seed, progress_cb=progress_cb,
        )


async def _generate_angle_view_inner(
    image_url: str,
    angle_prompt: str,
    *,
    workflow_path: str = None,
    seed: int = -1,
    progress_cb=None,
) -> dict:
    cfg = comfyui_config()
    base = (getattr(cfg, "base_url", None) or "http://127.0.0.1:8188").rstrip("/")
    # 工作流路径：显式传入 > 用户设置 > 内置默认（dev 仓库根 / 打包 bundle）
    wf_path = resolve_workflow_path(workflow_path or scene_camera_workflow())
    if not wf_path or not os.path.isfile(wf_path):
        raise FileNotFoundError(f"转视角工作流未找到（{wf_path or '空'}）—— 在「本地 ComfyUI」设置里指定 .json 路径")
    wf = _load_workflow(wf_path)

    _li_id, li = _find_node(wf, "LoadImage")
    _ks_id, ks = _find_node(wf, "KSampler")
    pos = _resolve_positive_encode(wf)
    if not li or not pos:
        raise RuntimeError("工作流缺 LoadImage 或正向文本编码节点，无法参数化")

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        ref = await _upload_image(client, base, image_url)   # 上传输入场景图
        li["inputs"]["image"] = ref
        pos["inputs"]["prompt"] = angle_prompt
        if ks is not None:
            _s = seed if (isinstance(seed, int) and seed >= 0) else (uuid.uuid4().int % (2**31 - 1))
            ks["inputs"]["seed"] = _s

        client_id = uuid.uuid4().hex
        r = await client.post(f"{base}/prompt", json={"prompt": wf, "client_id": client_id})
        r.raise_for_status()
        data = r.json()
        if data.get("node_errors"):
            raise RuntimeError(f"ComfyUI 工作流校验失败: {data['node_errors']}")
        pid = data.get("prompt_id")
        if not pid:
            raise RuntimeError(f"ComfyUI 未返回 prompt_id: {data}")

        img_info = None
        start = time.time()
        while time.time() - start < _POLL_TIMEOUT_S:
            await asyncio.sleep(_POLL_INTERVAL_S)
            hr = await client.get(f"{base}/history/{pid}")
            hist = hr.json()
            if pid in hist:
                entry = hist[pid]
                if ((entry.get("status") or {}).get("status_str")) == "error":
                    raise RuntimeError(f"ComfyUI 执行失败: {entry.get('status')}")
                for _nid, out in (entry.get("outputs") or {}).items():
                    for v in (out.get("images") or []):
                        if str(v.get("filename", "")).lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                            img_info = v
                            break
                    if img_info:
                        break
                if not img_info:
                    raise RuntimeError(f"ComfyUI 完成但无图片输出: {list((entry.get('outputs') or {}).keys())}")
                break
            if progress_cb:
                try:
                    await progress_cb(f"🎥 Qwen 转机位中... {int(time.time() - start)}s")
                except Exception:
                    pass
        if not img_info:
            raise RuntimeError(f"ComfyUI 转机位轮询超时（>{_POLL_TIMEOUT_S}s）")

        params = {
            "filename": img_info["filename"],
            "subfolder": img_info.get("subfolder", ""),
            "type": img_info.get("type", "output"),
        }
        dr = await client.get(f"{base}/view", params=params)
        dr.raise_for_status()
        content = dr.content
        # 生成完清缓存：防连生多张 VRAM 碎片累积 → lowvram 龟速（用户实测第2张卡15%/44s per it）。
        # 按工作流是否用 GGUF 自动切：GGUF(模型小、能常驻)只轻清缓存不卸载(避免白重载)；
        # fp8(装不下、每次要换模型)才用重的 unload_models 彻底重置。
        try:
            _gguf = any(isinstance(n, dict) and "GGUF" in (n.get("class_type") or "") for n in wf.values())
            await client.post(f"{base}/free", json={"unload_models": (not _gguf), "free_memory": True})
        except Exception:
            pass

    fn = f"qwenangle_{uuid.uuid4().hex[:8]}.png"
    path = os.path.join(images_dir(), fn)
    with open(path, "wb") as f:
        f.write(content)
    logger.info("Qwen 转机位出板 OK：%s ← 「%s」", fn, angle_prompt)
    return {"image_url": f"/output/images/{fn}", "origin_url": None, "provider": "comfyui_qwen", "prompt": angle_prompt}
