"""0.8 缝4：能力门控抽象（entitlements）。

`local`（默认）= **全放行 no-op**，行为零变化。本地开发/自用永远用它。
`cloud_client`  = 校验本机存放的 **Ed25519 签名授权**，按 capability 逐项放行。

═══ 为什么验签这部分 0.8 就能做完整 ═══

验签是**纯本地密码学**（公钥内置在客户端），不需要云端在线；云端只负责**签发**。
所以这里把校验逻辑写完整，0.9 只需补两件事：① 真实公钥 ② 签发/购买流程。
这跟缝1/缝0 的 stub 不同——那两条的核心逻辑在服务端，本地只能留插口。

═══ 授权凭据格式 ═══

    {
      "payload": {
        "sub":   "<用户ID>",
        "plan":  "free" | "pro" | ...,
        "caps":  ["video.generate", "audio.generate", ...],
        "exp":   <unix 秒>,
      },
      "sig": "<base64(Ed25519 签名，签的是 payload 的 canonical JSON)>"
    }

存在 `user_settings("license", "token")`。canonical JSON = `sort_keys=True, separators=(',',':')`，
签发端必须用同样的序列化方式，否则验签必失败。

═══ 设计原则 ═══

- **fail-closed**：`cloud_client` 下拿不到有效授权 = 拒绝。付费闸门宁可错拒不可错放。
- **local 永远 fail-open**：`require()` 直接 return，连授权都不读，零开销零风险。
- 拒绝时给**可操作**的话术（过期了 / 没这个权益 / 没登录），别只说 "denied"。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class Capability:
    """能力标识。与云端签发的 caps 字符串一一对应。"""
    VIDEO_GENERATE = "video.generate"        # 视频生成（付费主闸，credits）
    AUDIO_GENERATE = "audio.generate"        # BGM / 音频规划
    LOCALIZATION = "video.localize"          # 视频翻译 / 多语言本地化
    AGENT_RUN = "agent.run"                  # agent 能力（订阅：秘密 prompt）
    STORYBOARD_FANOUT = "storyboard.fanout"  # 分镜扇出（可选闸）


# 人话名称，用于拒绝话术
_CAP_LABELS = {
    Capability.VIDEO_GENERATE: "视频生成",
    Capability.AUDIO_GENERATE: "音频生成",
    Capability.LOCALIZATION: "视频翻译",
    Capability.AGENT_RUN: "AI 创作（agent）",
    Capability.STORYBOARD_FANOUT: "分镜扇出",
}


class EntitlementError(RuntimeError):
    """无权使用某能力。message 是给用户看的可操作话术。"""

    def __init__(self, cap: str, message: str):
        super().__init__(message)
        self.cap = cap
        self.message = message


# 授权签发公钥（Ed25519 raw 32 字节 base64）。
# 2026-07-21 生成，**私钥只在云端** `/octopus/keys/license_ed25519.key`（600，绝不进任何仓库）。
# 公钥公开无妨 —— 它只能验签、不能签发。空字符串 = 没有签发体系 → cloud_client 下一律拒。
_LICENSE_PUBLIC_KEY_B64 = "UgLv2+4Iv4YnTgMKWKo0hY/YhxTBSfhaZX32pkmDLXs="


def _load_license() -> Optional[Dict[str, Any]]:
    from app.core.user_settings_service import user_settings
    raw = user_settings.get("license", "token") or ""
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning("entitlements: 授权凭据不是合法 JSON: %s", e)
        return None


def _verify(blob: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """验签 + 验过期，通过则返回 payload，否则 None。"""
    if not _LICENSE_PUBLIC_KEY_B64:
        return None
    payload = blob.get("payload")
    sig_b64 = blob.get("sig")
    if not isinstance(payload, dict) or not sig_b64:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(_LICENSE_PUBLIC_KEY_B64))
        # canonical JSON —— 签发端必须用同样的序列化方式
        msg = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        pub.verify(base64.b64decode(sig_b64), msg)
    except Exception as e:
        logger.warning("entitlements: 授权验签失败: %s", e)
        return None
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and exp and time.time() > float(exp):
        logger.warning("entitlements: 授权已过期（exp=%s）", exp)
        return None
    return payload


def _granted_caps() -> List[str]:
    blob = _load_license()
    if not blob:
        return []
    payload = _verify(blob)
    if not payload:
        return []
    caps = payload.get("caps") or []
    return [str(c) for c in caps] if isinstance(caps, list) else []


def _deny_reason(cap: str) -> str:
    """拒绝话术 —— 分清"没登录/没授权""过期""这档套餐没有"，别一律 denied。"""
    label = _CAP_LABELS.get(cap, cap)
    blob = _load_license()
    if not blob:
        return f"「{label}」需要登录并订阅后使用（当前未找到有效授权）"
    if not _LICENSE_PUBLIC_KEY_B64:
        # 客户端没内置公钥 = 这个构建没有签发体系，只能本地用
        return f"「{label}」授权校验未启用（此构建未内置签发公钥）；请把部署模式切回 local"
    if not _verify(blob):
        return f"「{label}」授权无效或已过期，请重新登录 / 续订"
    return f"当前套餐不包含「{label}」，请升级后使用"


# ═══════════════════════════════════════════════════════
# 公开 API
# ═══════════════════════════════════════════════════════

def is_enforced() -> bool:
    """是否启用门控。local 恒 False —— 全放行，零开销。"""
    try:
        from app.core.runtime_config import is_cloud_client
        return is_cloud_client()
    except Exception as e:
        logger.warning("entitlements.is_enforced 异常，按不启用处理: %s", e)
        return False


def check(cap: str) -> bool:
    """有没有这个能力。local 恒 True。"""
    if not is_enforced():
        return True
    return cap in _granted_caps()


def require(cap: str) -> None:
    """没有该能力就抛 EntitlementError。local 直接 return（连授权都不读）。"""
    if not is_enforced():
        return
    if cap in _granted_caps():
        return
    raise EntitlementError(cap, _deny_reason(cap))


def deny_reason(cap: str) -> str:
    """拒绝话术。给那些"不抛异常、只把 error 写进 result"的调用点用（如 _run_localization）。"""
    return _deny_reason(cap)


def status() -> Dict[str, Any]:
    """诊断用：当前门控状态（不泄露签名本体）。"""
    enforced = is_enforced()
    if not enforced:
        return {"enforced": False, "plan": "local", "caps": "*", "valid": True}
    blob = _load_license()
    payload = _verify(blob) if blob else None
    return {
        "enforced": True,
        "has_license": bool(blob),
        "valid": bool(payload),
        "plan": (payload or {}).get("plan") or "",
        "exp": (payload or {}).get("exp"),
        "caps": (payload or {}).get("caps") or [],
    }
