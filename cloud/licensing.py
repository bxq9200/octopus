"""授权签发 —— 用 Ed25519 私钥签一张客户端能离线验证的能力凭据。

对应客户端 `backend/app/core/entitlements.py` 的验签逻辑。**两边必须严格对齐**：
签名对象 = payload 的 **canonical JSON**（`sort_keys=True, separators=(",", ":")`）。
少一个空格都验不过。

私钥只在服务器 `/octopus/keys/license_ed25519.key`（600），**绝不进任何仓库、绝不下发客户端**。
客户端只有公钥 —— 能验签，不能签发。
"""

import base64
import json
import logging
import time
from typing import Any, Dict, List

import config

logger = logging.getLogger("octopus.cloud.licensing")


# 与客户端 entitlements.Capability 一一对应
CAP_VIDEO_GENERATE = "video.generate"
CAP_AUDIO_GENERATE = "audio.generate"
CAP_LOCALIZATION = "video.localize"
CAP_AGENT_RUN = "agent.run"
CAP_STORYBOARD_FANOUT = "storyboard.fanout"

# 套餐 → 能力。
# 商业模型：平台只卖两样 —— ①视频中转(credits，**按次付费、所有人可用**) ②agent 能力(订阅)。
# 所以 **video.generate 给所有套餐**（真正的闸是 credits 余额，在 /v1/video/generate 里查）——
# "视频 = pay per use" 的正确实现是余额门控，不是套餐门控。localize 暂留 pro（功能未建，接时再议）。
PLAN_CAPS: Dict[str, List[str]] = {
    "free": [CAP_AGENT_RUN, CAP_AUDIO_GENERATE, CAP_STORYBOARD_FANOUT, CAP_VIDEO_GENERATE],
    "pro": [CAP_AGENT_RUN, CAP_AUDIO_GENERATE, CAP_STORYBOARD_FANOUT,
            CAP_VIDEO_GENERATE, CAP_LOCALIZATION],
}


class LicensingError(RuntimeError):
    pass


def _load_private_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if not config.license_key_available():
        raise LicensingError(
            f"签发私钥不存在：{config.LICENSE_PRIVATE_KEY_FILE}（服务器上生成后才能签发授权）"
        )
    raw = base64.b64decode(config.LICENSE_PRIVATE_KEY_FILE.read_text().strip())
    return Ed25519PrivateKey.from_private_bytes(raw)


def caps_for_plan(plan: str) -> List[str]:
    return list(PLAN_CAPS.get((plan or "free").lower(), PLAN_CAPS["free"]))


def issue(*, sub: str, plan: str = "free", ttl_days: int = None) -> Dict[str, Any]:
    """签一张授权。返回客户端直接可存进 `user_settings("license","token")` 的结构。"""
    priv = _load_private_key()
    ttl = ttl_days if ttl_days is not None else config.LICENSE_TTL_DAYS
    payload = {
        "sub": sub,
        "plan": (plan or "free").lower(),
        "caps": caps_for_plan(plan),
        "exp": int(time.time()) + ttl * 86400,
    }
    # ⚠️ canonical JSON —— 必须与客户端验签时的序列化完全一致
    msg = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = base64.b64encode(priv.sign(msg)).decode()
    logger.info("[license] 签发 sub=%s plan=%s caps=%d exp=%s", sub, payload["plan"],
                len(payload["caps"]), payload["exp"])
    return {"payload": payload, "sig": sig}


def public_key_b64() -> str:
    """当前私钥对应的公钥 —— 用来跟客户端内置的那份核对是否配套。"""
    from cryptography.hazmat.primitives import serialization
    priv = _load_private_key()
    return base64.b64encode(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
