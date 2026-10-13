import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    APP_NAME: str = "Octopus"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True
    LLM_PROVIDER: str = "kimi"  # openai | kimi

    # 0.8 解耦：部署模式。
    #   local        = 全本地（现状，默认，行为零变化；本地开发/自用永远用它）
    #   cloud_client = 上线态：视频走云端中转、agent 走云端服务、企业 key 不下发本地
    # 0.8 只建“按此开关分叉”的接缝；cloud_client 的云端实现（网关/agent 服务）留到 0.9。
    # 开源版默认 **local**：所有 key 自带，不依赖任何云端服务。
    # cloud_client 需要自行部署 cloud/ 并在设置里填网关地址（见 cloud/README.md）。
    DEPLOYMENT_MODE: str = os.environ.get("OCTOPUS_DEPLOYMENT_MODE", "local")  # local | cloud_client

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./octopus.db"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # 输出目录（生成的图片/视频）— 默认 backend/output，可通过 OCTOPUS_OUTPUT_DIR 覆盖
    OUTPUT_DIR: str = os.environ.get(
        "OCTOPUS_OUTPUT_DIR",
        str(Path(__file__).resolve().parents[2] / "output")
    )

    # LLM - Kimi Code API
    KIMI_API_KEY: str = ""
    KIMI_BASE_URL: str = "https://api.kimi.com/coding/v1"
    KIMI_MODEL: str = "kimi-for-coding"

    # LLM - OpenAI (optional fallback)
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "gpt-4"

    # LLM - DeepSeek（OpenAI 兼容，Kimi 限流/挂掉时的备胎）
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # Video Generation - Seedance
    SEEDANCE_API_KEY: str = ""
    SEEDANCE_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/v3"
    SEEDANCE_MODEL: str = "doubao-seedance-2-0-260128"
    SEEDANCE_FAST_MODEL: str = "doubao-seedance-1-0-pro-fast-251015"
    # 火山方舟图像编辑（图生图）— Seedream 4.0 同时支持文生图与图生图
    # 注：这条属「视频区/企业侧」的模型名，由 ark_config() 消费；Seedream 自己的 BYOK 槽见下方 SEEDREAM_*
    ARK_IMAGE_EDIT_MODEL: str = "doubao-seedream-4-0-250828"

    # Image Generation - Seedream 图生图（0.8 缝3：从「复用视频区企业 key」解耦出来的 BYOK 槽）
    # 三视图/角色 composite/关键帧图生图用。留空时 local 模式仍会 fallback 到视频区火山 key（零回归），
    # cloud_client 模式下企业 key 不下发本机 → 必须填这里自己的 key。
    SEEDREAM_API_KEY: str = ""
    SEEDREAM_BASE_URL: str = ""
    SEEDREAM_IMAGE_EDIT_MODEL: str = ""

    # Image Generation - Google Imagen 3
    GOOGLE_API_KEY: str = ""
    GEMINI_IMAGE_MODEL: str = "gemini-3-pro-image-preview"
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"

    # Image Generation - 智谱 GLM-Image
    ZHIPU_API_KEY: str = ""
    ZHIPU_IMAGE_MODEL: str = "glm-image"
    ZHIPU_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"

    # Frontend URL for CORS
    FRONTEND_URL: str = "http://localhost:5173"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
