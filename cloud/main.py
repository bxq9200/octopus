"""Octopus 云端网关 —— 0.9 Phase 1：agent 即服务。

对应 0.8 缝0 埋好的客户端插口（`backend/app/core/agent_runtime.py`）。
客户端只发 `{agent_type, entry, payload, llm_config}`，**秘密 prompt 从不下发**；
云端加载 agent、用**用户转发上来的 key** 调 LLM、解析完只回结构化 JSON。

═══ 三个必须记住的点 ═══

**1. 本服务内部强制 `local` 模式 —— 否则会递归调用自己。**
`BaseAgent.run()` / `chat()` 里有 cloud_client 分叉；如果本服务也处于 cloud_client，
它跑 agent 时会再往"云端"发一次请求 → 打回自己 → 无限递归。
启动时把 `DEPLOYMENT_MODE` 钉成 `local`（本服务**就是**执行方）。

**2. LLM key 用完即弃。**
`llm_config` 随请求转发上来，用 `runtime_config.llm_override` 压进 ContextVar，
**只在这一次请求的作用域内有效，不落库、不写日志**。ContextVar 保证并发请求互不串号。

**3. entry 的两种语义不一样。**
- `execute`：客户端已在本地做了 `_preprocess`，云端只需跑 `agent.execute()`，
  `_postprocess` 回客户端做。**不要调 `agent.run()`** —— 那会重复 preprocess。
- `chat`：客户端的分叉在 `chat()` 最开头（拼 prompt 之前），所以云端要跑**完整的 `agent.chat()`**。

本地开发跑法（不需要服务器）：
    cd octopus/cloud
    python -m uvicorn main:app --host 127.0.0.1 --port 9000
然后把客户端设置里「部署模式」切 cloud_client、Octopus 网关地址填 http://127.0.0.1:9000
"""

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# ── 让本服务能 import 到 backend/app/**（agents 只保留一份，不复制） ──
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# ⚠️ 必须在 import app.* 之前钉死：本服务是执行方，绝不能自己再走 cloud_client 分叉
os.environ["OCTOPUS_DEPLOYMENT_MODE"] = "local"

import inspect                                        # noqa: E402
from fastapi import Depends, FastAPI, HTTPException   # noqa: E402
from pydantic import BaseModel                        # noqa: E402

from app.core.config import settings                  # noqa: E402
from app.core.runtime_config import LLMConfig, llm_override, deployment_mode  # noqa: E402

from agent_registry import get_agent_class, known_agents  # noqa: E402
import config                                             # noqa: E402
import db                                                  # noqa: E402
import licensing                                           # noqa: E402
from auth import router as auth_router, current_user       # noqa: E402
from video import router as video_router                    # noqa: E402
from admin import router as admin_router                    # noqa: E402
from site_pages import router as site_router                # noqa: E402
from payment import router as pay_router                    # noqa: E402

# 🔴 必须 force=True：`uvicorn main:app` 会**先**装好自己的 logging handler，
# 此后 basicConfig() 变成空操作 → 本服务所有 logger.info（[pay]/[video]/[asset]/[agent/call]）
# 全被吞掉，只剩 uvicorn 的访问日志。2026-07-30 排查支付回调时才发现（客户端
# backend/app/main.py 早就带了 force=True，云端漏了）。日志落 /octopus/logs/cloud.log
# （systemd StandardOutput=append，**不进 journald**，别用 journalctl 找）。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    force=True,
)
logger = logging.getLogger("octopus.cloud")

# 双保险：即使 env 被外部覆盖，也强制回 local
settings.DEPLOYMENT_MODE = "local"

app = FastAPI(title="Octopus Cloud Gateway", version="0.9.0-phase3")
app.include_router(auth_router)
app.include_router(video_router)
app.include_router(admin_router)
app.include_router(pay_router)       # 充值支付（支付宝电脑网站支付）
app.include_router(site_router)      # 主站落地页 / + 用户文档 /docs/*（含 catch-all 性质的 /，放最后）


def _inject_enterprise_video_creds() -> None:
    """把企业 antchain 凭据注入 backend 的 user_settings 内存缓存 —— 让 _antchain_video
    模块（云端复用它出片）读到企业 key。存**明文**即可：decrypt_if_needed 对无前缀原样返回，
    故 antchain_video() 的 get_with_fallback(sensitive=True) 能拿到明文，无需 master key。"""
    if not config.video_vendor_configured():
        return
    from app.core.user_settings_service import user_settings

    def _inj(cat, key, val):
        user_settings._cache.setdefault(cat, {})[key] = {"value": val, "sensitive": False}

    _inj("video", "antchain_ak", config.ANTCHAIN_AK)
    _inj("video", "antchain_sk", config.ANTCHAIN_SK)
    _inj("video", "antchain_endpoint", config.ANTCHAIN_ENDPOINT)
    _inj("video", "antchain_model", config.ANTCHAIN_MODEL)
    _inj("video", "provider", "antchain")
    # 平台 OSS：让云端的 media_gateway/oss_client 能上传首帧（客户端没传公网 URL 时兜底）
    if config.oss_configured():
        _inj("storage", "oss_access_key_id", config.OSS_ACCESS_KEY_ID)
        _inj("storage", "oss_access_key_secret", config.OSS_ACCESS_KEY_SECRET)
        _inj("storage", "oss_bucket", config.OSS_BUCKET)
        _inj("storage", "oss_endpoint", config.OSS_ENDPOINT)
        _inj("storage", "oss_domain", config.OSS_DOMAIN)
        _inj("storage", "oss_base_dir", config.OSS_BASE_DIR)
    logger.info("[startup] 企业视频凭据已注入（antchain=%s, oss=%s）",
                config.video_vendor_configured(), config.oss_configured())


@app.on_event("startup")
async def _startup() -> None:
    await db.init_db()
    logger.info("[startup] DB 就绪: %s", config.DATABASE_URL)
    _inject_enterprise_video_creds()
    if not config.video_vendor_configured():
        logger.warning("[startup] 企业视频凭据未配置 —— /v1/video/generate 用 stub 出片（不产真视频）")
    if not config.smtp_configured():
        logger.warning("[startup] SMTP 未配置 —— 验证码发不出去")
    if not config.license_key_available():
        logger.warning("[startup] 签发私钥不存在 —— 登录时无法下发授权")
    if not config.secret("JWT_SECRET"):
        logger.warning(
            "[startup] JWT_SECRET 未固定，本次启动随机生成 —— **重启后所有用户被登出**，"
            "生产请在 secrets.env 里固定它"
        )


class LLMConfigIn(BaseModel):
    provider: str = "kimi"
    api_key: str = ""
    base_url: str = ""
    model: str = ""


class AgentRunRequest(BaseModel):
    agent_type: str
    entry: str = "execute"                 # "execute" | "chat"
    payload: Dict[str, Any] = {}
    llm_config: Optional[LLMConfigIn] = None


@app.get("/health")
async def health() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "status": "ok",
        "service": "octopus-cloud",
        "version": app.version,
        # 必须恒为 local，否则就是上面说的递归隐患
        "deployment_mode": deployment_mode(),
        "agents": known_agents(),
        "smtp": config.smtp_configured(),
        "licensing": config.license_key_available(),
        "video_vendor": config.video_vendor_configured(),   # False = stub 出片
    }
    # 公钥指纹：跟客户端内置的那份核对是否配套（配错的话所有授权都验不过，很难查）
    if config.license_key_available():
        try:
            out["license_pubkey"] = licensing.public_key_b64()
        except Exception:
            pass
    return out


@app.post("/v1/agent/run")
async def agent_run(req: AgentRunRequest) -> Dict[str, Any]:
    t0 = time.time()

    try:
        agent_cls = get_agent_class(req.agent_type)
    except KeyError:
        raise HTTPException(404, f"未知 agent_type: {req.agent_type}（已注册: {known_agents()}）")

    if req.entry not in ("execute", "chat"):
        raise HTTPException(400, f"未知 entry: {req.entry}（只支持 execute / chat）")

    lc = req.llm_config
    if not lc or not lc.api_key:
        # BYOK：平台不提供 LLM key，没转发上来就没法跑
        raise HTTPException(400, "缺少 llm_config.api_key —— agent 用你自己的 LLM key 运行（BYOK）")

    cfg = LLMConfig(
        provider=(lc.provider or "kimi"),
        api_key=lc.api_key,
        base_url=lc.base_url or "",
        model=lc.model or "",
    )

    agent = agent_cls()
    payload = req.payload or {}

    # 只记不敏感的元信息 —— **绝不记 api_key、绝不记 prompt**
    logger.info(
        "[agent/run] type=%s entry=%s provider=%s model=%s payload_keys=%s",
        req.agent_type, req.entry, cfg.provider, cfg.model, sorted(payload.keys()),
    )

    try:
        with llm_override(cfg):          # 用完即弃：只在此作用域内生效
            if req.entry == "execute":
                # 客户端已做过 _preprocess，这里只跑 execute（调 run() 会重复 preprocess）
                data = await agent.execute(payload.get("task") or {}, payload.get("context") or {})
            else:
                # chat 的分叉在客户端 chat() 最开头，所以云端跑完整 chat()
                data = await agent.chat(
                    message=payload.get("message") or "",
                    summary=payload.get("summary") or "",
                    project_meta=payload.get("project_meta") or {},
                    target_nodes=payload.get("target_nodes") or [],
                    attachments=payload.get("attachments") or [],
                )
    except Exception as e:
        logger.warning("[agent/run] %s.%s 失败: %s", req.agent_type, req.entry, e)
        raise HTTPException(500, f"agent 执行失败: {str(e)[:300]}")

    usage = getattr(agent, "_last_usage", None)
    out: Dict[str, Any] = {"ok": True, "data": data, "elapsed_ms": int((time.time() - t0) * 1000)}
    if usage:
        out["usage"] = usage
    return out


# ═══════════════════════════════════════════════════════
# 护城河核心：通用 agent 方法 RPC（0.9 Phase A，2026-07-24）
# ═══════════════════════════════════════════════════════

class AgentCallRequest(BaseModel):
    agent_type: str
    method: str
    args: list = []
    kwargs: Dict[str, Any] = {}
    llm_config: Optional[LLMConfigIn] = None


@app.post("/v1/agent/call")
async def agent_call(req: AgentCallRequest,
                     user: db.User = Depends(current_user)) -> Dict[str, Any]:
    """通用 agent 方法 RPC —— 护城河的真正落点。

    `/v1/agent/run` 只覆盖 execute/chat；但客户端 executor 直接调十几个 agent 方法
    （refine_outline / review_art / breakdown_into_episodes / plan_* …），每个都是独立的
    prompt 工程入口。这个端点让**任意 secret 方法**都在云端跑：
        {agent_type, method, args, kwargs} → getattr(agent, method)(*args, **kwargs)
    秘密 prompt 从不下发，客户端只留一次 HTTP 调用。

    鉴权：agent 能力是付费项 → 必须带用户 token。
    """
    t0 = time.time()
    try:
        agent_cls = get_agent_class(req.agent_type)
    except KeyError:
        raise HTTPException(404, f"未知 agent_type: {req.agent_type}（已注册: {known_agents()}）")

    # 只允许公开方法（挡住 __init__ / _private 之类的任意方法调用）
    method = (req.method or "").strip()
    if not method or method.startswith("_"):
        raise HTTPException(400, f"非法 method: {method or '(空)'}")
    agent = agent_cls()
    fn = getattr(agent, method, None)
    if fn is None or not callable(fn):
        raise HTTPException(400, f"{req.agent_type} 没有可调用的方法 {method}")

    lc = req.llm_config
    if not lc or not lc.api_key:
        raise HTTPException(400, "缺少 llm_config.api_key —— agent 用你自己的 LLM key 运行（BYOK）")
    cfg = LLMConfig(
        provider=(lc.provider or "kimi"),
        api_key=lc.api_key,
        base_url=lc.base_url or "",
        model=lc.model or "",
    )

    # 只记不敏感元信息 —— 绝不记 api_key、绝不记 args 内容
    logger.info("[agent/call] user=%s type=%s method=%s nargs=%d kwargs=%s",
                getattr(user, "email", "?"), req.agent_type, method,
                len(req.args or []), sorted((req.kwargs or {}).keys()))

    try:
        with llm_override(cfg):          # 用户 BYOK key，用完即弃
            result = fn(*(req.args or []), **(req.kwargs or {}))
            if inspect.isawaitable(result):
                result = await result
    except Exception as e:
        logger.warning("[agent/call] %s.%s 失败: %s", req.agent_type, method, e)
        raise HTTPException(500, f"agent 方法执行失败: {str(e)[:300]}")

    out2: Dict[str, Any] = {"ok": True, "data": result, "elapsed_ms": int((time.time() - t0) * 1000)}
    usage2 = getattr(agent, "_last_usage", None)
    if usage2:
        out2["usage"] = usage2
    return out2
