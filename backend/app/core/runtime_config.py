"""
配置领域服务 — 把 settings + user_settings 包成"语义化访问器"

模块代码不应该直接调 settings.KIMI_API_KEY 也不应该直接调
user_settings.get_with_fallback("llm", "kimi_api_key", ...)，
而是调 active_llm() / zhipu_creds() 这种语义清晰的函数。

这样将来加新厂商、改优先级、改加密策略都不用动模块代码。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from app.core.config import settings
from app.core.user_settings_service import user_settings


# ═══════════════════════════════════════════════════════
# 0.8 解耦：部署模式（各接缝按它分叉）
#   local        = 全本地（默认，行为零变化）
#   cloud_client = 上线态：视频走云端中转 / agent 走云端服务 / 企业 key 不下发本地
# 优先 user_settings(deploy.mode)（便于设置 UI 切换），否则 config.DEPLOYMENT_MODE。
# ═══════════════════════════════════════════════════════

def deployment_mode() -> str:
    """部署模式。**env 优先** —— 桌面启动器固定注入 cloud_client、云端服务固定注入 local，
    两者都不该被库里的历史值翻盘。

    2026-07-24 反转优先级（原来是 user_settings > env）：设置页已去掉「运行模式」开关
    （用户只该用云端），若仍让库里残留的 `deploy.mode=local` 生效，用户会**卡在本地模式且
    没有 UI 能切回来**。dev 想跑本地就显式 `OCTOPUS_DEPLOYMENT_MODE=local`。
    """
    import os as _os
    env = (_os.environ.get("OCTOPUS_DEPLOYMENT_MODE") or "").strip()
    if env:
        return env
    return (user_settings.get("deploy", "mode") or settings.DEPLOYMENT_MODE or "local").strip()


def is_cloud_client() -> bool:
    return deployment_mode() == "cloud_client"


# ═══════════════════════════════════════════════════════
# 0.8 缝3：企业凭证 vs BYOK 分类（唯一权威清单）
#   企业凭证 = 平台自己的账号，0.9 只存在于云端，**永不下发本机**
#              → video.ark_* / video.antchain_* / storage.oss_*（视频数据面）
#   BYOK     = 用户自己的账号，永远留本地，平台不碰
#              → llm.* / image.*（智谱/Google/Seedream）/ audio.*
# 三处消费者：① _enterprise_get（读）② settings API（UI 隐藏 + 拒写）
#             ③ _migrate_from_env（不把企业 key 迁进本地 DB）
# ═══════════════════════════════════════════════════════

ENTERPRISE_KEYS: frozenset = frozenset({
    ("video", "ark_api_key"),
    ("video", "ark_base_url"),
    ("video", "seedance_model"),
    ("video", "seedance_fast_model"),
    ("video", "ark_image_edit_model"),
    ("video", "antchain_ak"),
    ("video", "antchain_sk"),
    ("video", "antchain_endpoint"),
    ("video", "antchain_model"),
    # 注：storage.oss_* **不是**企业凭据。
    # 2026-07-22 修正：OSS 是用户自己的存储（BYOK），cloud_client 下**必须可用** ——
    # 视频首帧/成片要传公网 URL 给厂商，靠的就是它；STS 数据面（缝2）还没建，OSS 是唯一出路。
    # 原来把 OSS 归企业 → 云端模式首帧传不上公网 → 回退本地路径/base64 → antchain 报
    # "content[1].image_url is not valid"（i2v 直接失败降级 mock）。
})


def is_enterprise_key(category: str, key: str) -> bool:
    return (category, key) in ENTERPRISE_KEYS


def _enterprise_get(category: str, key: str, env_default: str = "", sensitive: bool = False) -> str:
    """企业凭证读取口 —— cloud_client 下一律返回空。

    local（默认）= 与直接 get_with_fallback 完全等价，零行为变化。
    cloud_client = 本机拿不到企业 key，视频只能走云端中转（缝1）、数据面走 STS（缝2）。
    """
    if is_cloud_client():
        return ""
    return user_settings.get_with_fallback(category, key, env_default, sensitive=sensitive)


# ═══════════════════════════════════════════════════════
# LLM 配置
# ═══════════════════════════════════════════════════════

@dataclass
class LLMConfig:
    provider: str       # "kimi" | "openai"
    api_key: str
    base_url: str
    model: str


# ═══════════════════════════════════════════════════════
# 0.9 云端：per-request LLM 覆盖
# 云端 agent 服务用**用户随请求转发上来的 key** 跑 agent —— 服务器自己根本没有用户的 key，
# 这是 BYOK 商业模型的物理体现。用 ContextVar 而非全局变量：FastAPI 并发请求各自隔离不串号，
# 且 asyncio 子任务会继承创建时的上下文副本（agent 内部 create_task 也拿得到）。
# local 恒为 None → 现有路径完全不受影响。
# ═══════════════════════════════════════════════════════

_llm_override: ContextVar[Optional[LLMConfig]] = ContextVar("octopus_llm_override", default=None)


class llm_override:
    """with llm_override(cfg): ... —— 作用域内 active_llm()/vision_llm() 返回 cfg。"""

    def __init__(self, cfg: Optional[LLMConfig]):
        self._cfg = cfg
        self._token = None

    def __enter__(self):
        self._token = _llm_override.set(self._cfg)
        return self._cfg

    def __exit__(self, *exc):
        if self._token is not None:
            _llm_override.reset(self._token)
        return False


def active_llm() -> LLMConfig:
    """根据 user_settings.llm.provider 选当前 LLM 配置。

    云端 agent 服务会用 `llm_override` 把用户转发的配置压进来（见上）。
    """
    _ov = _llm_override.get()
    if _ov is not None:
        return _ov
    provider = (
        user_settings.get("llm", "provider")
        or settings.LLM_PROVIDER
        or "kimi"
    ).lower()

    if provider == "openai":
        return LLMConfig(
            provider="openai",
            api_key=user_settings.get_with_fallback(
                "llm", "openai_api_key", settings.OPENAI_API_KEY, sensitive=True,
            ),
            base_url=user_settings.get_with_fallback(
                "llm", "openai_base_url", settings.OPENAI_BASE_URL,
            ),
            model=user_settings.get_with_fallback(
                "llm", "openai_model", settings.OPENAI_MODEL,
            ),
        )
    if provider == "deepseek":
        return LLMConfig(
            provider="deepseek",
            api_key=user_settings.get_with_fallback(
                "llm", "deepseek_api_key", settings.DEEPSEEK_API_KEY, sensitive=True,
            ),
            base_url=user_settings.get_with_fallback(
                "llm", "deepseek_base_url", settings.DEEPSEEK_BASE_URL,
            ),
            model=user_settings.get_with_fallback(
                "llm", "deepseek_model", settings.DEEPSEEK_MODEL,
            ),
        )
    # 默认 kimi
    return LLMConfig(
        provider="kimi",
        api_key=user_settings.get_with_fallback(
            "llm", "kimi_api_key", settings.KIMI_API_KEY, sensitive=True,
        ),
        base_url=user_settings.get_with_fallback(
            "llm", "kimi_base_url", settings.KIMI_BASE_URL,
        ),
        model=user_settings.get_with_fallback(
            "llm", "kimi_model", settings.KIMI_MODEL,
        ),
    )


# ═══════════════════════════════════════════════════════
# Vision LLM 配置（0.6.0 阶段 ②e）
# - 跟主 LLM 同 key，但允许独立的 model 和 base_url
# - 仅在 agent.chat / director.refine_outline 等场景看到 attachments 时使用
# ═══════════════════════════════════════════════════════

@dataclass
class VisionLLMConfig:
    provider: str        # "kimi" | "openai"
    api_key: str
    base_url: str
    model: str


def vision_llm() -> VisionLLMConfig:
    """返回当前 LLM provider 的 vision 配置（model 和 base_url 可独立设置）。"""
    # 云端 agent 服务的 per-request 覆盖：vision 沿用同一份转发配置
    # （用户只转发了一套 key；vision 专用 model 名云端无从得知，退回主 model）。
    _ov = _llm_override.get()
    if _ov is not None:
        return VisionLLMConfig(
            provider=_ov.provider, api_key=_ov.api_key,
            base_url=_ov.base_url, model=_ov.model,
        )
    provider = (
        user_settings.get("llm", "provider")
        or settings.LLM_PROVIDER
        or "kimi"
    ).lower()

    if provider == "openai":
        return VisionLLMConfig(
            provider="openai",
            api_key=user_settings.get_with_fallback(
                "llm", "openai_api_key", settings.OPENAI_API_KEY, sensitive=True,
            ),
            base_url=user_settings.get_with_fallback(
                "llm", "openai_base_url", settings.OPENAI_BASE_URL,
            ),
            model=user_settings.get("llm", "openai_vision_model") or "gpt-4o",
        )
    if provider == "deepseek":
        # DeepSeek 主 chat 模型不支持图像输入；vision 走 best-effort（上层 catch 到报错后降级纯文本）
        return VisionLLMConfig(
            provider="deepseek",
            api_key=user_settings.get_with_fallback(
                "llm", "deepseek_api_key", settings.DEEPSEEK_API_KEY, sensitive=True,
            ),
            base_url=user_settings.get_with_fallback(
                "llm", "deepseek_base_url", settings.DEEPSEEK_BASE_URL,
            ),
            model=(user_settings.get("llm", "deepseek_vision_model")
                   or user_settings.get_with_fallback("llm", "deepseek_model", settings.DEEPSEEK_MODEL)),
        )
    # 默认 kimi
    # vision base_url 优先用 vision 字段，没有就 fallback 主 base_url
    vision_base_url = (
        user_settings.get("llm", "kimi_vision_base_url")
        or user_settings.get_with_fallback("llm", "kimi_base_url", settings.KIMI_BASE_URL)
    )
    return VisionLLMConfig(
        provider="kimi",
        api_key=user_settings.get_with_fallback(
            "llm", "kimi_api_key", settings.KIMI_API_KEY, sensitive=True,
        ),
        base_url=vision_base_url,
        model=user_settings.get("llm", "kimi_vision_model") or "kimi-k2.6",
    )


# ═══════════════════════════════════════════════════════
# 图片生成（Zhipu / Google Imagen）
# ═══════════════════════════════════════════════════════

@dataclass
class ZhipuConfig:
    api_key: str
    base_url: str
    model: str


def zhipu_image() -> ZhipuConfig:
    return ZhipuConfig(
        api_key=user_settings.get_with_fallback(
            "image", "zhipu_api_key", settings.ZHIPU_API_KEY, sensitive=True,
        ),
        base_url=user_settings.get_with_fallback(
            "image", "zhipu_base_url", settings.ZHIPU_BASE_URL,
        ),
        model=user_settings.get_with_fallback(
            "image", "zhipu_model", settings.ZHIPU_IMAGE_MODEL,
        ),
    )


@dataclass
class GoogleImageConfig:
    api_key: str
    base_url: str
    model: str


def google_image() -> GoogleImageConfig:
    return GoogleImageConfig(
        api_key=user_settings.get_with_fallback(
            "image", "google_api_key", settings.GOOGLE_API_KEY, sensitive=True,
        ),
        base_url=user_settings.get_with_fallback(
            "image", "google_base_url", settings.GEMINI_BASE_URL,
        ),
        model=user_settings.get_with_fallback(
            "image", "google_model", settings.GEMINI_IMAGE_MODEL,
        ),
    )


# ═══════════════════════════════════════════════════════
# 视频生成（火山方舟 Seedance / Seedream 共享 ARK key）
# ═══════════════════════════════════════════════════════

@dataclass
class ArkConfig:
    api_key: str
    base_url: str
    video_model: str        # doubao-seedance-2-0-260128
    video_fast_model: str   # doubao-seedance-1-0-pro-fast-251015
    image_edit_model: str   # doubao-seedream-4-0-250828


def ark_config() -> ArkConfig:
    return ArkConfig(
        # 0.8 缝3：企业凭证 —— cloud_client 下为空（视频走云端中转，见缝1）
        api_key=_enterprise_get(
            "video", "ark_api_key", settings.SEEDANCE_API_KEY, sensitive=True,
        ),
        base_url=user_settings.get_with_fallback(
            "video", "ark_base_url", settings.SEEDANCE_BASE_URL,
        ),
        video_model=user_settings.get_with_fallback(
            "video", "seedance_model", settings.SEEDANCE_MODEL,
        ),
        video_fast_model=user_settings.get_with_fallback(
            "video", "seedance_fast_model", settings.SEEDANCE_FAST_MODEL,
        ),
        image_edit_model=user_settings.get_with_fallback(
            "video", "ark_image_edit_model", settings.ARK_IMAGE_EDIT_MODEL,
        ),
    )


# ═══════════════════════════════════════════════════════
# DashScope 视频生成（阿里云 HappyHorse / Wanx）
# ═══════════════════════════════════════════════════════

@dataclass
class DashScopeVideoConfig:
    api_key: str
    base_url: str        # https://dashscope.aliyuncs.com/api/v1
    i2v_model: str       # happyhorse-1.0-i2v
    t2v_model: str       # wanx2.1-t2v-turbo 等（可空）


_DASHSCOPE_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
_DASHSCOPE_DEFAULT_I2V_MODEL = "happyhorse-1.0-i2v"
_DASHSCOPE_DEFAULT_T2V_MODEL = ""


def dashscope_video() -> DashScopeVideoConfig:
    return DashScopeVideoConfig(
        api_key=user_settings.get_with_fallback(
            "video", "dashscope_api_key", "", sensitive=True,
        ),
        base_url=user_settings.get("video", "dashscope_base_url") or _DASHSCOPE_DEFAULT_BASE_URL,
        i2v_model=user_settings.get("video", "dashscope_i2v_model") or _DASHSCOPE_DEFAULT_I2V_MODEL,
        t2v_model=user_settings.get("video", "dashscope_t2v_model") or _DASHSCOPE_DEFAULT_T2V_MODEL,
    )


# ═══════════════════════════════════════════════════════
# ComfyUI 本地视频（0.7.x 开发期替代 Seedance；Wan 2.2）
# ═══════════════════════════════════════════════════════

@dataclass
class ComfyUIVideoConfig:
    base_url: str        # http://127.0.0.1:8188
    model: str           # diffusion 模型文件名（默认 wan2.2_ti2v_5B_fp16）
    fps: int             # 默认 16（Wan 常用档；24 同样秒数要更多帧、更慢）
    max_frames: int      # length 封顶（4n+1）；默认 241≈15s@16fps，避免把规划时长砍短（10s 别被砍成 5s）


_COMFYUI_DEFAULT_BASE_URL = "http://127.0.0.1:8188"
_COMFYUI_DEFAULT_MODEL = "wan2.2_ti2v_5B_fp16.safetensors"


def comfyui_config() -> ComfyUIVideoConfig:
    def _int(key, default):
        try:
            return int(user_settings.get("video", key) or default)
        except Exception:
            return default
    # 地址优先读新「本地 ComfyUI」分类（云端模式也可见）；回落旧「视频」分类的 comfyui_base_url
    base_url = (
        user_settings.get("comfyui", "base_url")
        or user_settings.get("video", "comfyui_base_url")
        or _COMFYUI_DEFAULT_BASE_URL
    )
    return ComfyUIVideoConfig(
        base_url=base_url,
        model=user_settings.get("video", "comfyui_model") or _COMFYUI_DEFAULT_MODEL,
        fps=_int("comfyui_fps", 16),
        max_frames=_int("comfyui_max_frames", 241),
    )


VIDEO_PROVIDERS = ("ark", "dashscope", "comfyui", "antchain", "octopus_cloud")


def active_video_provider_name() -> str:
    """当前激活的视频 provider：'ark' | 'dashscope' | 'comfyui' | 'antchain' | 'octopus_cloud'。"""
    val = (user_settings.get("video", "provider") or "").lower()
    if val in VIDEO_PROVIDERS:
        return val
    # 0.8 缝1：cloud_client 下本机没有企业 key（缝3 已清空），只能走平台中转；
    # local 仍默认 ark，与拆分前一致。
    return "octopus_cloud" if is_cloud_client() else "ark"


# ═══════════════════════════════════════════════════════
# 0.8 缝1：octopus_cloud —— 平台视频中转（Seedance 中转 + credits 计费）
# 与其它 provider 的根本区别：**本机不持任何企业 key**，只持
#   gateway_url（云端网关地址）+ token（用户账号 JWT，缝5 接真）
# 企业火山 key 只在云端；客户端只说"我要生成"，网关验 token → 查余额 → 用企业 key 转发
# → 按秒计 credits → 回视频 URL。字节不过我们的服务器，客户端直连下载。
# ═══════════════════════════════════════════════════════

@dataclass
class OctopusCloudConfig:
    gateway_url: str      # 云端网关根地址，如 https://api.octopus.art
    token: str            # 用户账号 token（不是企业 key）
    model: str            # 计量/展示用标识；真正用哪个模型由云端决定


_OCTOPUS_CLOUD_DEFAULT_GATEWAY = ""   # 开源版不内置网关：用 cloud_client 模式需自行部署 cloud/ 并在设置里填地址
_OCTOPUS_CLOUD_DEFAULT_MODEL = "seedance-2.0"


def octopus_cloud_config() -> OctopusCloudConfig:
    # 0.8 缝5：token 的单一真相移到 identity —— 优先登录写入的 account.token，
    # 兜底缝1 的手填 video.octopus_cloud_token（已经填过的用户不受影响）。
    from app.core import identity
    return OctopusCloudConfig(
        gateway_url=(user_settings.get("video", "octopus_cloud_gateway")
                     or _OCTOPUS_CLOUD_DEFAULT_GATEWAY).rstrip("/"),
        # 用户自己的账号 token —— 属 BYOK 侧，**不在 ENTERPRISE_KEYS 里**，
        # cloud_client 下必须可见可填（否则用户没法登录用平台服务）。
        token=identity.cloud_token(),
        model=user_settings.get("video", "octopus_cloud_model") or _OCTOPUS_CLOUD_DEFAULT_MODEL,
    )


# ═══════════════════════════════════════════════════════
# 0.7.3: 蚂蚁数科 AIGC（antchain OpenAPI 网关）—— 渠道商 Seedance 2.0
# AK/SK + HmacSHA1（SDK 内部签名）；endpoint 网关域名。详见记忆 octopus-073-antchain-seedance
# ═══════════════════════════════════════════════════════

@dataclass
class AntchainVideoConfig:
    ak: str               # AccessKey ID
    sk: str               # AccessKey Secret
    endpoint: str         # 网关域名（不含 https://），如 openapi.antchain.antgroup.com
    model: str            # 模型码，默认 doubao-seedance-2-0-260128


_ANTCHAIN_DEFAULT_ENDPOINT = "openapi.antchain.antgroup.com"
_ANTCHAIN_DEFAULT_MODEL = "doubao-seedance-2-0-260128"


def antchain_video() -> AntchainVideoConfig:
    return AntchainVideoConfig(
        # 0.8 缝3：企业凭证 —— cloud_client 下为空（视频走云端中转，见缝1）
        ak=_enterprise_get("video", "antchain_ak", "", sensitive=True),
        sk=_enterprise_get("video", "antchain_sk", "", sensitive=True),
        endpoint=user_settings.get("video", "antchain_endpoint") or _ANTCHAIN_DEFAULT_ENDPOINT,
        model=user_settings.get("video", "antchain_model") or _ANTCHAIN_DEFAULT_MODEL,
    )


@dataclass
class VideoTranslateConfig:
    target_languages: str   # 默认目标语言，逗号分隔（en / en,ja）
    translate_type: str     # VOICE / SUBTITLE / FACE
    source_language: str    # 源语言，默认 zh
    erase_subtitle: bool    # 擦除原字幕
    embed_subtitle: bool    # 嵌入翻译字幕


def video_translate() -> VideoTranslateConfig:
    """0.7.7 视频翻译（本地化）默认配置；AK/SK/endpoint 复用 antchain_video()（同蚂蚁账号，走 videotranslate 接口）。
    这些只是默认值——实际触发时用户在本地化节点选的语言/类型会覆盖。"""
    def _b(key, default):
        v = user_settings.get("localization", key)
        if v in (None, ""):
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    return VideoTranslateConfig(
        target_languages=user_settings.get("localization", "target_languages") or "en",
        translate_type=(user_settings.get("localization", "translate_type") or "VOICE").upper(),
        source_language=user_settings.get("localization", "source_language") or "zh",
        erase_subtitle=_b("erase_subtitle", True),
        embed_subtitle=_b("embed_subtitle", True),
    )


# ═══════════════════════════════════════════════════════
# 0.7.3: 阿里云 OSS —— asset 入库中转（图 → OSS 短公网 URL → 蚂蚁 upload_asset）
# 火山 origin_url 24h 过期、大 base64 炸网关；OSS 签名 URL 稳定喂 asset.upload。
# ═══════════════════════════════════════════════════════

@dataclass
class OSSConfig:
    access_key_id: str
    access_key_secret: str
    bucket: str
    endpoint: str        # oss-cn-beijing.aliyuncs.com（不含 https://）
    domain: str          # 公网访问域名（可选，走 CDN 时用）
    base_dir: str        # 对象 key 前缀


def oss_config() -> OSSConfig:
    return OSSConfig(
        # 2026-07-22：OSS 是用户自己的存储（BYOK），**cloud_client 下也要可用** ——
        # 首帧/成片传公网 URL 给厂商靠它（STS 数据面未建前的唯一出路）。故用普通读取，不走 _enterprise_get。
        access_key_id=user_settings.get_with_fallback(
            "storage", "oss_access_key_id", getattr(settings, "OSS_ACCESS_KEY_ID", ""), sensitive=True),
        access_key_secret=user_settings.get_with_fallback(
            "storage", "oss_access_key_secret", getattr(settings, "OSS_ACCESS_KEY_SECRET", ""), sensitive=True),
        bucket=user_settings.get("storage", "oss_bucket") or getattr(settings, "OSS_BUCKET", ""),
        endpoint=user_settings.get("storage", "oss_endpoint") or getattr(settings, "OSS_ENDPOINT", ""),
        domain=user_settings.get("storage", "oss_domain") or getattr(settings, "OSS_DOMAIN", ""),
        base_dir=user_settings.get("storage", "oss_base_dir")
                 or getattr(settings, "OSS_BASE_DIR", "") or "octopus/assets",
    )


# ═══════════════════════════════════════════════════════
# 0.7.3: Seedream 图片生成（图生图：三视图/composite/关键帧）—— 从 video.ark 归位到 image 分类
# 0.8 缝3：Seedream 是 **BYOK**（用户自己的火山账号），已与视频区的企业 key 解耦。
#   key 链：image.seedream_api_key(DB) → SEEDREAM_API_KEY(env) → 【仅 local】视频区企业 ark key
#   最后一跳走 _enterprise_get，故 local 保持"一个火山账号不用填两次"的现状（零回归），
#   cloud_client 下企业 key 不下发本机 → 必须填自己的 seedream key。
#   base_url / model 是公开常量不是凭证，照常兜底，保证 cloud 模式只填 key 就能用。
# ═══════════════════════════════════════════════════════

@dataclass
class SeedreamImageConfig:
    api_key: str
    base_url: str
    image_edit_model: str


def seedream_image() -> SeedreamImageConfig:
    return SeedreamImageConfig(
        api_key=user_settings.get_with_fallback(
            "image", "seedream_api_key", settings.SEEDREAM_API_KEY, sensitive=True)
            or _enterprise_get("video", "ark_api_key", settings.SEEDANCE_API_KEY, sensitive=True),
        base_url=user_settings.get("image", "seedream_base_url") or settings.SEEDREAM_BASE_URL
                 or user_settings.get("video", "ark_base_url") or settings.SEEDANCE_BASE_URL,
        image_edit_model=user_settings.get("image", "seedream_image_edit_model")
                 or settings.SEEDREAM_IMAGE_EDIT_MODEL
                 or user_settings.get("video", "ark_image_edit_model") or settings.ARK_IMAGE_EDIT_MODEL,
    )


def scene_camera_provider() -> str:
    """0.7.5.2 场景机位板生成器：
    'seedream'(图生图，同墙换取景，零依赖) | 'qwen'(本地 ComfyUI Qwen 多角度 LoRA，真换机位/中角度)。默认 seedream。"""
    return (user_settings.get("image", "scene_camera_provider") or "seedream").strip().lower()


def scene_camera_workflow() -> str:
    """用户在「本地 ComfyUI」分类设的转视角工作流 JSON 路径（空=用内置默认）。"""
    return (user_settings.get("comfyui", "scene_camera_workflow") or "").strip()


# ═══════════════════════════════════════════════════════
# 0.6.1: Audio / Music — Mubert
# ═══════════════════════════════════════════════════════

@dataclass
class MubertConfig:
    # 公司级凭证（Mubert 邮件给的）
    company_id: str
    license_token: str
    base_url: str


_MUBERT_DEFAULT_BASE_URL = "https://music-api.mubert.com/api/v3"
# 缓存 key：customer_id / access_token / expired_at 都不在 settings CATEGORIES 里暴露，
# 后端首次调 /service/customers 拿到后写回 user_settings，由 mubert_music 直接读
_CACHE_CUSTOMER_ID_KEY = "_cached_customer_id"
_CACHE_ACCESS_TOKEN_KEY = "_cached_access_token"
_CACHE_EXPIRES_AT_KEY = "_cached_token_expires_at"


def mubert_config() -> MubertConfig:
    return MubertConfig(
        company_id=user_settings.get_with_fallback(
            "audio", "mubert_company_id", "", sensitive=True,
        ),
        license_token=user_settings.get_with_fallback(
            "audio", "mubert_license_token", "", sensitive=True,
        ),
        base_url=user_settings.get("audio", "mubert_base_url") or _MUBERT_DEFAULT_BASE_URL,
    )


def active_audio_provider_name() -> str:
    """当前激活的音频 provider：'mubert' | 'elevenlabs'（默认 elevenlabs）"""
    val = (user_settings.get("audio", "provider") or "elevenlabs").lower()
    return val if val in ("mubert", "elevenlabs") else "elevenlabs"


# ─── ElevenLabs Music ───
# 官方 API：POST /v1/music，xi-api-key 鉴权，直接返二进制音频（无轮询无 customer 换取）
# 比 Mubert 简单且生成质量明显更高
@dataclass
class ElevenLabsMusicConfig:
    api_key: str
    base_url: str
    model_id: str
    output_format: str


_ELEVENLABS_DEFAULT_BASE_URL = "https://api.elevenlabs.io"
_ELEVENLABS_DEFAULT_MODEL = "music_v1"
_ELEVENLABS_DEFAULT_FORMAT = "mp3_44100_128"


def elevenlabs_music_config() -> ElevenLabsMusicConfig:
    return ElevenLabsMusicConfig(
        api_key=user_settings.get_with_fallback(
            "audio", "elevenlabs_api_key", "", sensitive=True,
        ),
        base_url=user_settings.get("audio", "elevenlabs_base_url") or _ELEVENLABS_DEFAULT_BASE_URL,
        model_id=user_settings.get("audio", "elevenlabs_model_id") or _ELEVENLABS_DEFAULT_MODEL,
        output_format=user_settings.get("audio", "elevenlabs_output_format") or _ELEVENLABS_DEFAULT_FORMAT,
    )
