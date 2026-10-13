"""云端配置 —— 只从**服务器上的文件**读，绝不硬编码密钥。

密钥文件：`/octopus/config/secrets.env`（600 权限，不进任何仓库）
密钥目录：`/octopus/keys/`（700，Ed25519 签发私钥在这里）

本地开发跑云端服务时这些文件不存在，各项为空 —— 相关功能（发信/签授权）会明确报错，
不会静默降级成"假装成功"。
"""

import os
import secrets as _secrets
from pathlib import Path
from typing import Dict

CONFIG_DIR = Path(os.environ.get("OCTOPUS_CONFIG_DIR", "/octopus/config"))
KEYS_DIR = Path(os.environ.get("OCTOPUS_KEYS_DIR", "/octopus/keys"))
DATA_DIR = Path(os.environ.get("OCTOPUS_DATA_DIR", "/octopus/data"))

SECRETS_FILE = CONFIG_DIR / "secrets.env"


def _load_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


_SECRETS = _load_env_file(SECRETS_FILE)


def secret(key: str, default: str = "") -> str:
    """优先环境变量，其次 secrets.env。"""
    return os.environ.get(key) or _SECRETS.get(key, default)


# ── SMTP（全部在 secrets.env 里配）──
SMTP_HOST = secret("SMTP_HOST", "")
SMTP_PORT = int(secret("SMTP_PORT", "465") or 465)   # 阿里云 ECS 封 25，只能 465/587
SMTP_USER = secret("SMTP_USER")
SMTP_PASSWORD = secret("SMTP_PASSWORD")
SMTP_FROM_NAME = secret("SMTP_FROM_NAME", "Octopus")

# ── 数据库 ──
DATABASE_URL = secret("DATABASE_URL") or f"sqlite+aiosqlite:///{DATA_DIR}/octopus_cloud.db"

# ── JWT ──
# 没配就**每次启动随机生成** —— 意味着重启后所有 token 失效。
# 生产必须在 secrets.env 里固定 JWT_SECRET，否则重启一次用户全被登出。
JWT_SECRET = secret("JWT_SECRET") or _secrets.token_urlsafe(48)
JWT_ALGORITHM = "HS256"
JWT_TTL_DAYS = int(secret("JWT_TTL_DAYS", "30") or 30)

# ── 授权（Ed25519 签发私钥）──
LICENSE_PRIVATE_KEY_FILE = KEYS_DIR / "license_ed25519.key"
LICENSE_TTL_DAYS = int(secret("LICENSE_TTL_DAYS", "30") or 30)

# ── 验证码策略 ──
CODE_TTL_SECONDS = int(secret("CODE_TTL_SECONDS", "600") or 600)        # 10 分钟
CODE_MAX_ATTEMPTS = int(secret("CODE_MAX_ATTEMPTS", "5") or 5)          # 错 5 次作废
CODE_RESEND_COOLDOWN = int(secret("CODE_RESEND_COOLDOWN", "60") or 60)  # 同邮箱 60s 才能再发
CODE_DAILY_LIMIT_EMAIL = int(secret("CODE_DAILY_LIMIT_EMAIL", "10") or 10)
CODE_HOURLY_LIMIT_IP = int(secret("CODE_HOURLY_LIMIT_IP", "20") or 20)

# ── 视频 credits 定价（占位，可 env 覆盖）──
# credits 是平台内部计费单位。真实定价（每 credit 值多少钱、每秒视频几 credits）**由用户拍板**，
# 这里先给占位值让钱的流转跑通。720p 便宜、1080p 贵；新用户免费额度用于试用。
CREDITS_PER_SEC_720P = int(secret("CREDITS_PER_SEC_720P", "10") or 10)
CREDITS_PER_SEC_1080P = int(secret("CREDITS_PER_SEC_1080P", "20") or 20)
SIGNUP_FREE_CREDITS = int(secret("SIGNUP_FREE_CREDITS", "0") or 0)   # 注册赠送（0=不送）

# ── 企业视频厂商凭据（放服务器，不下发客户端）──
# 用户本地一直用蚂蚁 antchain 通道 → 把 antchain AK/SK 放 secrets.env 即可让云端出片。
# 留空 = 云端用 stub 出片（只验 credits/任务流转，不产真视频）。
ANTCHAIN_AK = secret("ANTCHAIN_AK", "")
ANTCHAIN_SK = secret("ANTCHAIN_SK", "")
ANTCHAIN_ENDPOINT = secret("ANTCHAIN_ENDPOINT", "openapi.antchain.antgroup.com")
ANTCHAIN_MODEL = secret("ANTCHAIN_MODEL", "doubao-seedance-2-0-260128")


def video_vendor_configured() -> bool:
    return bool(ANTCHAIN_AK and ANTCHAIN_SK)


# ── 平台 OSS（云端自给自足：客户端没传公网首帧时，云端自己传）──
# 未来 SaaS 用户不会有自己的 OSS → 平台提供存储。客户端发 base64/本地路径时，
# 云端解码上传到这个 OSS 拿公网 URL 再喂厂商。客户端已发公网 URL 则原样透传。
OSS_ACCESS_KEY_ID = secret("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = secret("OSS_ACCESS_KEY_SECRET", "")
OSS_BUCKET = secret("OSS_BUCKET", "")
OSS_ENDPOINT = secret("OSS_ENDPOINT", "")
OSS_DOMAIN = secret("OSS_DOMAIN", "")
OSS_BASE_DIR = secret("OSS_BASE_DIR", "octopus/cloud-assets")


def oss_configured() -> bool:
    return bool(OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET and OSS_BUCKET)


# ── 管理员 token（admin 手动授信用；自动支付上线前的过渡充值手段）──
ADMIN_TOKEN = secret("ADMIN_TOKEN", "")


def admin_configured() -> bool:
    return bool(ADMIN_TOKEN)


# ── 注册开关 ──
# 内测期可设 INVITE_CODE=xxx 只放邀请者进来；留空 = 任何邮箱都能注册（但仍有限频）。
INVITE_CODE = secret("INVITE_CODE", "")

# 邮箱域名白名单（逗号分隔），留空 = 不限制。防批量注册可用。
EMAIL_DOMAIN_ALLOWLIST = [
    d.strip().lower() for d in secret("EMAIL_DOMAIN_ALLOWLIST", "").split(",") if d.strip()
]


# ── 支付宝（电脑网站支付）──
# open.alipay.com 建应用 → 签约「电脑网站支付」→ 生成 RSA2 密钥对 → 上传应用公钥 → 拿支付宝公钥。
# 私钥/公钥填**纯 base64 主体**（不含 -----BEGIN----- 头尾、不含换行）。
ALIPAY_APP_ID = secret("ALIPAY_APP_ID", "")
ALIPAY_PRIVATE_KEY = secret("ALIPAY_PRIVATE_KEY", "")   # 应用私钥（我方签名用）
ALIPAY_PUBLIC_KEY = secret("ALIPAY_PUBLIC_KEY", "")     # 支付宝公钥（验异步通知签名用）
ALIPAY_GATEWAY = secret("ALIPAY_GATEWAY", "https://openapi.alipay.com/gateway.do")
# 站点根地址：拼 notify_url / return_url。必须**公网可达 + 已备案**。
SITE_BASE_URL = secret("SITE_BASE_URL", "")


def alipay_configured() -> bool:
    return bool(ALIPAY_APP_ID and ALIPAY_PRIVATE_KEY and ALIPAY_PUBLIC_KEY)


def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def license_key_available() -> bool:
    return LICENSE_PRIVATE_KEY_FILE.exists()
