"""0.8 缝0：agent 运行时抽象 —— agent 上云的唯一插口（护城河所在）。

`local`（默认）= 本地跑 agent（现状，行为零变化）
`cloud_client`  = POST 云端 `/v1/agent/run`，**秘密 prompt 只在云端**

═══ 为什么插口在 run()/chat()，而不是 base_agent.llm_chat ═══

0.8 计划最初把插口定在 `base_agent.llm_chat`（LLM 调用漏斗）。**那个位置保护不了 prompt。**

护城河的实际所在：
  - 各 agent 文件里的模块级 prompt 常量（实测 ~4 万字）
  - 以及体量更大的**拼装逻辑**（director 79KB / storyboard 85KB，绝大部分是 prompt 工程）

执行流到达 `llm_chat` 时，prompt 早就拼好躺在 `messages` 里了；而那些 `.py` 本来就要
随桌面端分发到用户机器上。所以在 `llm_chat` 分叉 = 护城河照发不误，只是多绕一次云端
——一点都没护住，还多一跳延迟。

真正的插口必须在**拼 prompt 之前**，也就是 agent 的两个入口：
  - `run()` → `execute()`  主生成路径
  - `chat()`               进阶模式对话式修改（`_chat_system_prompt()` 同样是护城河）

这么做成立的前提（已核查全部 9 个 agent）：**agent 都是纯函数**——零文件 IO、零 DB、
零 module 调用，纯粹 `task/context → 拼 prompt → LLM → 解析 JSON → dict`。
所以整个 agent body 可以整体搬到云端，本机只留一个 HTTP 调用。这正是路 A。

═══ 0.9 要填的端点契约 ═══

    POST {gateway}/v1/agent/run
      headers: Authorization: Bearer {token}
      body: {
        agent_type: "screenwriter" | "storyboard" | ...,
        entry:      "execute" | "chat",
        payload:    {...},                    # 见 _payload_for_* 注释
        llm_config: {provider, api_key, base_url, model},   # 用户 BYOK key，转发即弃
      }
      → {ok: bool, data: {...}, usage?: {...}, error?: str}

**llm_config 是用户自己的 LLM key**（商业模型锁定"走 Y"）：本地存、按调用转发上云、
云端只在内存里用那一次、不落库。物理约束——一次 LLM 调用要 prompt + key 在同处碰头；
prompt 在云端，key 就必须转发，否则等于把 prompt 下发本地。

`data` 直接就是 agent 的返回 dict（execute → 结果 dict；chat → {reply, node_updates}），
所以本机拿到就能用，不需要再解析——解析也在云端做完了。
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def should_run_remote() -> bool:
    """agent 是否该走云端。local 恒 False → 完全不影响现状。"""
    try:
        from app.core.runtime_config import is_cloud_client
        return is_cloud_client()
    except Exception as e:
        logger.warning("agent_runtime.should_run_remote 异常，回退本地: %s", e)
        return False


def _require_config():
    """网关 + token 校验；缺什么说什么。复用 octopus_cloud 那套账号配置（同一个平台账号）。"""
    from app.core.runtime_config import octopus_cloud_config
    cfg = octopus_cloud_config()
    if not cfg.gateway_url:
        raise ValueError("agent 云端运行未配置网关地址（设置 → 视频生成 → Octopus 网关地址）")
    if not cfg.token:
        raise ValueError("agent 云端运行缺少账号 Token（设置 → 视频生成 → Octopus 账号 Token）")
    return cfg


def _llm_config_for_forward() -> Dict[str, Any]:
    """取用户的 BYOK LLM 配置，随请求转发上云（用完即弃，云端不落库）。

    注意这里要的是**明文 key** —— user_settings 里敏感字段是 Fernet 加密存的，
    `active_llm()` 已经解好密，直接用。
    """
    from app.core.runtime_config import active_llm
    cfg = active_llm()
    return {
        "provider": cfg.provider,
        "api_key": cfg.api_key,
        "base_url": cfg.base_url,
        "model": cfg.model,
    }


# agent 调用里套着 LLM 调用，而且不止一次 —— storyboard.execute 内部有节奏蓝图 + 分批 +
# 3 个全局精修 pass，一整集下来十几分钟很正常。网关超时必须覆盖**整个方法**的耗时，
# 否则云端还在干活就被掐断。云端 nginx 侧同步放到 1860s。
_GATEWAY_TIMEOUT = 1800.0


async def run_remote(agent, entry: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """把一次 agent 调用交给云端执行，返回 agent 的结果 dict。

    agent：BaseAgent 实例（取 name 作 agent_type，并回填 _last_usage 供计量用）
    entry："execute"（主生成）| "chat"（对话式修改）
    payload：
        execute → {task, context}
        chat    → {message, summary, project_meta, target_nodes, attachments}

    0.8：校验配置后抛 NotImplementedError（不假装能跑）。0.9 在这里发 HTTP。
    """
    import httpx

    agent_type = getattr(agent, "name", "") or "unknown"
    # 0.8 缝4：agent 能力是两项付费服务之一（订阅制），先过门控再谈配置
    from app.core.entitlements import require, Capability
    require(Capability.AGENT_RUN)
    cfg = _require_config()          # 先校验配置，报错才有可操作性
    llm = _llm_config_for_forward()  # 用户 BYOK key，转发即弃

    url = f"{cfg.gateway_url}/v1/agent/run"
    body = {
        "agent_type": agent_type,
        "entry": entry,
        "payload": payload or {},
        "llm_config": llm,
    }
    headers = {"Content-Type": "application/json"}
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"

    # 日志只记不敏感元信息 —— 绝不记 api_key、绝不记 payload 内容
    logger.info("[agent_runtime] cloud → %s.%s @ %s", agent_type, entry, cfg.gateway_url)

    # 连接建立失败会自动重试（安全：请求还没发出）；ReadTimeout 等不重试（云端可能已在跑）
    from app.core import cloud_http
    try:
        resp = await cloud_http.post(url, json=body, headers=headers, timeout=_GATEWAY_TIMEOUT)
    except Exception as e:
        raise RuntimeError(f"连不上 Octopus 云端网关（{cfg.gateway_url}）：{str(e)[:160]}") from e

    if resp.status_code != 200:
        detail = ""
        try:
            detail = (resp.json() or {}).get("detail") or ""
        except Exception:
            detail = (resp.text or "")[:200]
        raise RuntimeError(f"云端 agent 调用失败 HTTP {resp.status_code}：{str(detail)[:240]}")

    envelope = resp.json() or {}
    if not envelope.get("ok"):
        raise RuntimeError(f"云端 agent 返回失败：{str(envelope.get('error'))[:240]}")

    # 计量：云端回传的 usage 直接落到 _last_usage，executor 的成本统计照常工作
    usage = envelope.get("usage")
    if isinstance(usage, dict) and usage:
        try:
            agent._last_usage = usage
        except Exception:
            pass

    return envelope.get("data")


# ═══════════════════════════════════════════════════════
# 通用方法 RPC + @cloud_method 装饰器（0.9 Phase A，2026-07-24）
#
# 为什么还要这一层：run()/chat() 的分叉只覆盖 execute/chat，但 executor 直接调了十几个
# agent 方法（refine_outline / review_art / breakdown_into_episodes / plan_* …），每个都是
# 独立的 prompt 工程入口 —— 绕过 run() 就等于绕过护城河。给这些方法挂 @cloud_method，
# cloud_client 下整体路由云端，本机永不执行其 body（→ 打包时 body 可整体剥掉）。
# ═══════════════════════════════════════════════════════

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def _looks_like_local_image(v: Any) -> bool:
    """判断一个值是不是「云端取不到的本地图引用」（需要转公网 URL）。
    已是 http(s) 的不动（铁律1 透传）；data:image 也要转（payload 会爆）。"""
    if not isinstance(v, str) or not v:
        return False
    if v.startswith(("http://", "https://")):
        return False
    if v.startswith("data:image/"):
        return True
    low = v.lower()
    return (v.startswith("/output/") or ":" in v[:3] or v.startswith("/")) and low.endswith(_IMG_EXTS)


async def _sanitize_for_cloud(obj: Any) -> Any:
    """递归把 args/kwargs 里的本地图引用换成公网 URL —— 云端 agent 才取得到图。
    走 media_gateway（平台 OSS，用户零配置）；拿不到公网 URL 时它自己回退 base64（铁律2）。"""
    if isinstance(obj, str):
        if _looks_like_local_image(obj):
            try:
                from app.core import media_gateway
                return await media_gateway.to_model_input(obj, public=True)
            except Exception as e:
                logger.warning("[agent_runtime] 图转公网 URL 失败，原样上送: %s", str(e)[:100])
        return obj
    if isinstance(obj, (list, tuple)):
        return [await _sanitize_for_cloud(x) for x in obj]
    if isinstance(obj, dict):
        return {k: await _sanitize_for_cloud(v) for k, v in obj.items()}
    return obj


async def agent_call(agent, method: str, args: Any = None, kwargs: Optional[Dict[str, Any]] = None) -> Any:
    """把一次**任意 agent 方法**调用交给云端执行，返回该方法的原返回值。

    复用 run_remote 的全部约定：缝4 门控、网关/token 校验、BYOK key 转发即弃、
    600s 超时、日志脱敏（绝不记 key/参数内容）、usage 回填。
    """
    agent_type = getattr(agent, "name", "") or "unknown"
    from app.core.entitlements import require, Capability
    require(Capability.AGENT_RUN)
    cfg = _require_config()
    llm = _llm_config_for_forward()

    url = f"{cfg.gateway_url}/v1/agent/call"
    body = {
        "agent_type": agent_type,
        "method": method,
        "args": list(args or []),
        "kwargs": dict(kwargs or {}),
        "llm_config": llm,
    }
    headers = {"Content-Type": "application/json"}
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"

    logger.info("[agent_runtime] cloud → %s.%s @ %s", agent_type, method, cfg.gateway_url)

    from app.core import cloud_http
    try:
        resp = await cloud_http.post(url, json=body, headers=headers, timeout=_GATEWAY_TIMEOUT)
    except Exception as e:
        raise RuntimeError(f"连不上 Octopus 云端网关（{cfg.gateway_url}）：{str(e)[:160]}") from e

    if resp.status_code != 200:
        detail = ""
        try:
            detail = (resp.json() or {}).get("detail") or ""
        except Exception:
            detail = (resp.text or "")[:200]
        raise RuntimeError(f"云端 agent 调用失败 HTTP {resp.status_code}：{str(detail)[:240]}")

    envelope = resp.json() or {}
    if not envelope.get("ok"):
        raise RuntimeError(f"云端 agent 返回失败：{str(envelope.get('error'))[:240]}")

    usage = envelope.get("usage")
    if isinstance(usage, dict) and usage:
        try:
            agent._last_usage = usage
        except Exception:
            pass
    return envelope.get("data")


def cloud_method(fn):
    """护城河标记：被标记的 agent 方法在 cloud_client 下**整体路由云端**，本机不执行其 body。

    - local（dev / 云端服务器自身）→ 跑真身，**行为零变化**（这是回归安全网）。
    - cloud_client（分发给用户的桌面版）→ 参数消毒（本地图转公网 URL）后走 /v1/agent/call。

    因为本机永不执行 body，打包分发时可以把 body 整体剥成 `pass`（Phase B）。
    """
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        if not should_run_remote():
            return await fn(self, *args, **kwargs)

        # 回调参数（如 storyboard.execute 的 progress_cb）是 callable，没法序列化上云：
        # 先在本地发一条粗粒度进度，再把它从 payload 摘掉（云端模式暂无细粒度进度，
        # 后续可用 SSE 增强）。位置参数里的 callable 用 None 占位，保持元数不变。
        cb = None
        send_kwargs: Dict[str, Any] = {}
        for k, v in kwargs.items():
            if callable(v):
                if cb is None:
                    cb = v
                continue
            send_kwargs[k] = v
        send_args = [None if callable(a) else a for a in args]
        if cb is not None:
            try:
                r = cb(f"☁️ 云端生成中（{getattr(self, 'name', 'agent')}.{fn.__name__}）……")
                if inspect.isawaitable(r):
                    await r
            except Exception:
                pass

        s_args = await _sanitize_for_cloud(send_args)
        s_kwargs = await _sanitize_for_cloud(send_kwargs)
        return await agent_call(self, fn.__name__, s_args, s_kwargs)

    wrapper._is_cloud_method = True      # Phase B 的 stub 生成器靠它识别要剥的方法
    return wrapper


def apply_usage(agent, result: Optional[Dict[str, Any]]) -> None:
    """把云端回传的 usage 回填到 agent._last_usage —— executor 的成本统计照常工作（缝6 计量口）。"""
    if not isinstance(result, dict):
        return
    usage = result.get("usage")
    if isinstance(usage, dict) and usage:
        try:
            agent._last_usage = usage
        except Exception:
            pass
