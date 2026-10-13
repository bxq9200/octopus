"""
用户配置 API — 给前端"系统设置"侧栏 + Setup Wizard 用

GET  /api/settings                       获取所有分类（敏感字段遮罩）
GET  /api/settings/{category}            获取某个分类
PATCH /api/settings/{category}           批量更新某分类下多个字段
POST /api/settings/{category}/{key}      单字段更新（含敏感）
DELETE /api/settings/{category}/{key}    清除某项
GET  /api/settings/status                配置完整性检查（向导用）
POST /api/settings/test/{provider}       测试某 provider 的 key 是否有效

设计：
- 单用户假设，不需要 user_id 路径参数
- 敏感字段（API key/token）落库前加密；GET 时只回 has_value=true 不回明文
- POST 后立即 hot-reload（user_settings.set 内部已做）
"""

from typing import Any, Dict, List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.user_settings_service import user_settings
from app.core.runtime_config import is_cloud_client, is_enterprise_key


router = APIRouter(prefix="/api/settings", tags=["settings"])


# ─── 配置元数据：UI 渲染用 ───
# (key, label, sensitive, kind="string"|"select", options?, hint?, provider_for?)
# provider_for: 该字段只在某个 provider 被选中时才显示（前端按此过滤）。
class FieldDef(BaseModel):
    key: str
    label: str
    sensitive: bool = False
    kind: str = "string"
    options: List[Dict[str, str]] = []
    placeholder: str = ""
    hint: str = ""
    provider_for: str = ""  # 留空 = 始终显示（如 provider 自身、跨厂商共用字段）


def _publisher_fields(platform_cls) -> List[Dict[str, Any]]:
    """从 PublisherBase 子类拿 credential_fields，避免在 settings.py 里重复写一遍"""
    return list(platform_cls.credential_fields)


# 延迟 import 防循环依赖（publishers 包内不需要 settings.py，OK）
from app.publishers.youtube import YouTubePublisher
from app.publishers.tiktok import TikTokPublisher


CATEGORIES: Dict[str, Dict[str, Any]] = {
    "llm": {
        "label": "大模型 (LLM)",
        "icon": "brain",
        "fields": [
            {"key": "provider", "label": "服务商", "kind": "select",
             "options": [{"value": "kimi", "label": "Kimi (Moonshot)"},
                         {"value": "openai", "label": "OpenAI"},
                         {"value": "deepseek", "label": "DeepSeek"}],
             "hint": "任选一家即可（都要你自己去注册、自己充值，Octopus 不代收这部分费用）。"
                     "拿不准就选 DeepSeek —— 国内直连、最便宜、注册最简单。"},
            # Kimi
            {"key": "kimi_api_key", "label": "Kimi API Key", "sensitive": True,
             "placeholder": "sk-...",
             "hint": "① 打开 https://platform.moonshot.cn 用手机号注册登录\n"
                     "② 左侧「API Key 管理」→ 右上「新建 API Key」→ 取个名字 → 创建\n"
                     "③ 复制出来的 sk- 开头的串，粘到这里（只显示一次，记得存好）\n"
                     "④ 左侧「账户充值」充点钱（新号一般有免费额度，用完才需要充）",
             "provider_for": "kimi"},
            {"key": "kimi_base_url", "label": "Kimi Base URL",
             "placeholder": "https://api.kimi.com/coding/v1",
             "provider_for": "kimi"},
            {"key": "kimi_model", "label": "Kimi 模型", "placeholder": "kimi-for-coding",
             "provider_for": "kimi"},
            {"key": "kimi_vision_model", "label": "Kimi 视觉模型 (Vision)",
             "placeholder": "kimi-k2.6 / kimi-latest 等支持视觉的 model ID",
             "hint": "仅在创作者上传参考图时使用，让 LLM 真正\"看见\"图。文本对话仍走 Kimi 主模型。",
             "provider_for": "kimi"},
            {"key": "kimi_vision_base_url", "label": "Kimi 视觉 Base URL（可选）",
             "placeholder": "https://api.kimi.com/coding/v1（留空则用主 Base URL）",
             "provider_for": "kimi"},
            # OpenAI
            {"key": "openai_api_key", "label": "OpenAI API Key", "sensitive": True,
             "placeholder": "sk-...",
             "hint": "① 打开 https://platform.openai.com/api-keys 登录（国内需要梯子）\n"
                     "② 「Create new secret key」→ 命名 → 创建 → 复制 sk- 开头的串\n"
                     "③ Billing 页面绑卡充值（OpenAI 必须先充值才能调 API）\n"
                     "⚠️ 国内网络直连不通，用它需要全程科学上网；嫌麻烦就用 DeepSeek 或 Kimi",
             "provider_for": "openai"},
            {"key": "openai_base_url", "label": "OpenAI Base URL",
             "placeholder": "https://api.openai.com/v1",
             "provider_for": "openai"},
            {"key": "openai_model", "label": "OpenAI 模型", "placeholder": "gpt-4o",
             "provider_for": "openai"},
            {"key": "openai_vision_model", "label": "OpenAI 视觉模型 (Vision)",
             "placeholder": "gpt-4o（多数 OpenAI vision 用同一 model）",
             "hint": "创作者上传参考图时使用。",
             "provider_for": "openai"},
            # DeepSeek（OpenAI 兼容）—— Kimi 限流/挂掉时切到这里
            {"key": "deepseek_api_key", "label": "DeepSeek API Key", "sensitive": True,
             "placeholder": "sk-...",
             "hint": "【推荐给新手：国内直连、不用梯子、最便宜】\n"
                     "① 打开 https://platform.deepseek.com 用手机号注册登录\n"
                     "② 左侧「API keys」→「创建 API key」→ 命名 → 创建\n"
                     "③ 复制 sk- 开头的串粘到这里（只显示一次，务必先存好）\n"
                     "④ 左侧「充值」充 10-20 元就够用很久（创作一集大约几毛钱）",
             "provider_for": "deepseek"},
            {"key": "deepseek_base_url", "label": "DeepSeek Base URL",
             "placeholder": "https://api.deepseek.com/v1",
             "provider_for": "deepseek"},
            {"key": "deepseek_model", "label": "DeepSeek 模型",
             "placeholder": "deepseek-chat（V3，快）/ deepseek-reasoner（R1，强推理）",
             "provider_for": "deepseek"},
        ],
    },
    "image": {
        "label": "图片生成",
        "icon": "image",
        "fields": [
            {"key": "provider", "label": "服务商", "kind": "select",
             "options": [{"value": "zhipu", "label": "智谱 GLM-Image"},
                         {"value": "google", "label": "Google Imagen"}],
             "hint": "画场景图和道具图用（智谱为主，Google 作兜底）。"
                     "⚠️ 下面的 Seedream 是另一件事 —— 专画角色三视图和镜头关键帧，**两个都要配**。"},
            # 智谱
            {"key": "zhipu_api_key", "label": "智谱 API Key", "sensitive": True,
             "placeholder": "xxxxxx.xxxxxx",
             "hint": "【画场景 / 道具图用】\n"
                     "① 打开 https://open.bigmodel.cn 用手机号注册登录\n"
                     "② 右上角头像 →「API Keys」→ 复制那串 key（形如 xxxx.xxxx，中间有个点）\n"
                     "③ 首次实名认证后送免费额度；用完在「财务中心」充值即可",
             "provider_for": "zhipu"},
            {"key": "zhipu_base_url", "label": "智谱 Base URL",
             "placeholder": "https://open.bigmodel.cn/api/paas/v4",
             "provider_for": "zhipu"},
            {"key": "zhipu_model", "label": "智谱图片模型", "placeholder": "glm-image",
             "provider_for": "zhipu"},
            # Google
            {"key": "google_api_key", "label": "Google API Key", "sensitive": True,
             "hint": "① 打开 https://aistudio.google.com/apikey 用 Google 账号登录（国内需要梯子）\n"
                     "②「Create API key」→ 选一个项目 → 复制生成的 key\n"
                     "⚠️ 国内直连不通，需要全程科学上网；嫌麻烦用上面的智谱",
             "provider_for": "google"},
            {"key": "google_model", "label": "Google 图片模型",
             "placeholder": "gemini-3-pro-image-preview",
             "provider_for": "google"},
            # Seedream 图生图（火山方舟）—— 三视图/角色参考图/关键帧图生图专用，独立于上面的文生图 provider。
            # provider_for 留空=始终显示。0.8 缝3：Seedream 属 BYOK（用户自己的火山账号），
            # 留空只在 local 模式回落到「视频生成」的火山 key（一个账号不用填两次）；
            # cloud_client 下企业 key 不下发本机，必须填自己的。
            {"key": "seedream_api_key", "label": "Seedream Key（火山方舟）· 画角色和关键帧用", "sensitive": True,
             "placeholder": "以 ark- 开头",
             "hint": "【必填 —— 角色三视图和每个镜头的关键帧都靠它，不填出不了片】\n"
                     "① 打开 https://console.volcengine.com/ark 注册登录（要实名认证）\n"
                     "② 左侧「开通管理」→ 找到 **Doubao-Seedream** 系列 → 点「开通服务」\n"
                     "   ⚠️ 这步最容易漏！没开通的话 key 是有的但调用会报错\n"
                     "③ 左侧「API Key 管理」→「创建 API Key」→ 复制 ark- 开头的串\n"
                     "④ 账号需要有余额，出图按张计费（一张几分钱）"},
            {"key": "seedream_base_url", "label": "Seedream Base URL（可留空）",
             "placeholder": "留空复用视频区 Ark Base URL"},
            {"key": "seedream_image_edit_model", "label": "Seedream 图生图模型",
             "placeholder": "doubao-seedream-4-0-250828（留空复用视频区）"},
            # 0.7.5.2 场景机位板生成器（Tier 0 Seedream / Tier 1 Qwen 转机位）
            {"key": "scene_camera_provider", "label": "场景机位板生成器", "kind": "select",
             "options": [{"value": "seedream", "label": "Seedream（图生图·同墙换取景）"},
                         {"value": "qwen", "label": "Qwen 多角度（本地 ComfyUI·真换机位）"}],
             "hint": "选 Qwen 用本地 ComfyUI 真换机位（失败自动回退 Seedream）；ComfyUI 地址和工作流在下面「本地 ComfyUI」分类里配"},
        ],
    },
    # 0.7.5.2+：本地 ComfyUI（转视角/Qwen 多角度用；未来 ffmpeg 等本地工具也放这）。云端模式仍可见（本地能力，非平台企业 key）。
    "comfyui": {
        "label": "本地 ComfyUI",
        "icon": "cpu",
        "fields": [
            # hint 整条是 URL 时前端渲染成可点链接 → 直接跳线上教程（含各模型的下载直链）
            {"key": "base_url", "label": "ComfyUI 地址（可选进阶功能 · 点下方链接看安装教程）",
             "placeholder": "http://127.0.0.1:8188",
             "hint": "https://github.com/bxq9200/octopus/blob/main/docs/ComfyUI转视角_安装指南.md"},
            {"key": "scene_camera_workflow", "label": "转视角工作流(.json)",
             "placeholder": "留空 = 用内置默认工作流",
             "hint": "多角度转视角的 ComfyUI 工作流 JSON 路径。留空则用内置 comfyui-workflow-multiple-angles.json"},
        ],
    },
    "storage": {
        "label": "对象存储 (OSS)",
        "icon": "database",
        "fields": [
            {"key": "oss_access_key_id", "label": "OSS AccessKey ID", "sensitive": True,
             "placeholder": "LTAI...",
             "hint": "阿里云 OSS，用于 asset 入库中转（图→OSS公网→蚂蚁 upload_asset）。建议 RAM 子账号只授 PutObject。"},
            {"key": "oss_access_key_secret", "label": "OSS AccessKey Secret", "sensitive": True,
             "placeholder": "AccessKey Secret"},
            {"key": "oss_bucket", "label": "Bucket 名称", "placeholder": "your-bucket"},
            {"key": "oss_endpoint", "label": "Endpoint",
             "placeholder": "oss-cn-beijing.aliyuncs.com"},
            {"key": "oss_domain", "label": "访问域名（可选/CDN）",
             "placeholder": "https://your-bucket.oss-cn-beijing.aliyuncs.com"},
            {"key": "oss_base_dir", "label": "对象路径前缀",
             "placeholder": "octopus/assets"},
        ],
    },
    "video": {
        "label": "视频生成",
        "icon": "video",
        "fields": [
            {"key": "provider", "label": "服务商", "kind": "select",
             "options": [
                 {"value": "ark", "label": "火山方舟 Seedance"},
                 {"value": "dashscope", "label": "阿里云 DashScope (HappyHorse)"},
                 {"value": "comfyui", "label": "ComfyUI（本地 Wan2.2 · 开发）"},
                 {"value": "antchain", "label": "蚂蚁数科 Seedance（渠道）"},
                 {"value": "octopus_cloud", "label": "Octopus 云端中转（平台 credits）"},
             ],
             "hint": "切换后请填对应厂商的 key；ComfyUI 为本地开发省算力用；蚂蚁数科为 Seedance 2.0 渠道通道（AK/SK）；Octopus 云端中转用平台账号的 credits，本机不需要任何视频厂商 key"},
            # 0.8 缝1 Octopus 云端中转 —— 本机**不持企业 key**，只持网关地址 + 用户账号 token。
            # 这两项属用户自己的账号凭据（非企业 key），故不在 ENTERPRISE_KEYS 内，cloud_client 下必须可见。
            {"key": "octopus_cloud_gateway", "label": "Octopus 网关地址",
             "placeholder": "https://api.octopus.art",
             "hint": "平台云端网关；留空则用内置默认地址",
             "provider_for": "octopus_cloud"},
            {"key": "octopus_cloud_token", "label": "Octopus 账号 Token", "sensitive": True,
             "placeholder": "登录后自动填充",
             "hint": "你的平台账号凭据（不是视频厂商 key）；网关据此验权限、扣 credits",
             "provider_for": "octopus_cloud"},
            {"key": "octopus_cloud_model", "label": "视频模型",
             "placeholder": "seedance-2.0",
             "hint": "计量/展示用标识；实际用哪个模型由云端决定",
             "provider_for": "octopus_cloud"},
            # 火山方舟 Seedance
            {"key": "ark_api_key", "label": "火山方舟 API Key", "sensitive": True,
             "placeholder": "ark-...", "hint": "https://www.volcengine.com/product/ark",
             "provider_for": "ark"},
            {"key": "ark_base_url", "label": "Ark Base URL",
             "placeholder": "https://ark.cn-beijing.volces.com/api/v3",
             "provider_for": "ark"},
            {"key": "seedance_model", "label": "视频模型 (Seedance)",
             "placeholder": "doubao-seedance-2-0-260128",
             "provider_for": "ark"},
            {"key": "seedance_fast_model", "label": "视频快速模型",
             "placeholder": "doubao-seedance-1-0-pro-fast-251015",
             "provider_for": "ark"},
            {"key": "ark_image_edit_model", "label": "图生图模型 (Seedream)",
             "placeholder": "doubao-seedream-4-0-250828",
             "provider_for": "ark"},
            # 阿里云 DashScope (HappyHorse)
            {"key": "dashscope_api_key", "label": "DashScope API Key", "sensitive": True,
             "placeholder": "sk-...", "hint": "https://bailian.console.aliyun.com/",
             "provider_for": "dashscope"},
            {"key": "dashscope_base_url", "label": "DashScope Base URL",
             "placeholder": "https://dashscope.aliyuncs.com/api/v1",
             "provider_for": "dashscope"},
            {"key": "dashscope_i2v_model", "label": "图生视频模型",
             "placeholder": "happyhorse-1.0-i2v",
             "provider_for": "dashscope"},
            {"key": "dashscope_t2v_model", "label": "文生视频模型（可空）",
             "placeholder": "wanx2.1-t2v-turbo",
             "provider_for": "dashscope"},
            # ComfyUI（本地 Wan 2.2，开发期省算力）
            {"key": "comfyui_base_url", "label": "ComfyUI 地址",
             "placeholder": "http://127.0.0.1:8188",
             "provider_for": "comfyui"},
            {"key": "comfyui_model", "label": "视频模型",
             "placeholder": "wan2.2_ti2v_5B_fp16.safetensors",
             "hint": "按名字自动选档：默认/含 'ti2v'/'5b' → Wan2.2 5B（快，开发）；含 '14b' → Wan2.2 14B MoE（质量高但慢 ~24min/镜）；含 'ltx'/'sulphur' → LTX-2.3 i2v（两段式·自带音频·质量高较快）。例：wan2.2_ti2v_5B_fp16.safetensors / wan2.2_i2v_14B / ltx-2.3",
             "provider_for": "comfyui"},
            {"key": "comfyui_fps", "label": "帧率 fps",
             "placeholder": "16", "provider_for": "comfyui"},
            {"key": "comfyui_max_frames", "label": "最大帧数(4n+1)",
             "placeholder": "241", "hint": "封顶时长≈max_frames/fps；16fps 下 241≈15s，太小会把规划时长砍短",
             "provider_for": "comfyui"},
            # 0.7.3 蚂蚁数科 AIGC（antchain OpenAPI 网关）—— 渠道商 Seedance 2.0
            {"key": "antchain_ak", "label": "蚂蚁数科 AccessKey ID", "sensitive": True,
             "placeholder": "AccessKey ID",
             "hint": "蚂蚁数科服务账号的 AK/SK；多模态接口需提供租户编码加白",
             "provider_for": "antchain"},
            {"key": "antchain_sk", "label": "蚂蚁数科 AccessKey Secret", "sensitive": True,
             "placeholder": "AccessKey Secret",
             "provider_for": "antchain"},
            {"key": "antchain_endpoint", "label": "网关域名",
             "placeholder": "openapi.antchain.antgroup.com",
             "hint": "测试环境：openapi-sit.antchain.dl.alipaydev.com",
             "provider_for": "antchain"},
            {"key": "antchain_model", "label": "视频模型码",
             "placeholder": "doubao-seedance-2-0-260128",
             "hint": "可选：wan2.7-i2v-2026-04-25 / happyhorse-1.0-t2v / happyhorse-1.0-i2v",
             "provider_for": "antchain"},
        ],
    },
    "audio": {
        "label": "音频生成",
        "icon": "music",
        "fields": [
            {"key": "provider", "label": "服务商", "kind": "select",
             "options": [
                 {"value": "elevenlabs", "label": "ElevenLabs Music（推荐）"},
                 {"value": "mubert", "label": "Mubert"},
             ],
             "hint": "配了才有背景音乐；**不配也能正常出片**（成片就是没有 BGM）。"
                     "ElevenLabs 是真生成，质量明显好于 Mubert。"},
            # ElevenLabs Music — 官方 API：POST /v1/music，xi-api-key 鉴权，直接返二进制 mp3
            {"key": "elevenlabs_api_key", "label": "ElevenLabs API Key", "sensitive": True,
             "placeholder": "sk_...",
             "hint": "【可选 —— 不配就没有背景音乐，其它一切照常】\n"
                     "① 打开 https://elevenlabs.io 注册登录（国内需要梯子）\n"
                     "② 必须订阅 **Starter（$5/月）以上**，免费版没有音乐生成权限\n"
                     "③ 头像 → Settings → API Keys →「Create API Key」→ 复制 sk_ 开头的串\n"
                     "④ 创建时**记得勾上 Music 相关权限**，否则调用会报 missing_permission",
             "provider_for": "elevenlabs"},
            {"key": "elevenlabs_model_id", "label": "ElevenLabs 音乐模型",
             "placeholder": "music_v1",
             "provider_for": "elevenlabs"},
            {"key": "elevenlabs_output_format", "label": "ElevenLabs 输出格式",
             "placeholder": "mp3_44100_128",
             "hint": "支持 mp3_44100_128 / mp3_44100_192 / pcm_44100 等",
             "provider_for": "elevenlabs"},
            {"key": "elevenlabs_base_url", "label": "ElevenLabs Base URL",
             "placeholder": "https://api.elevenlabs.io",
             "provider_for": "elevenlabs"},
            # Mubert：邮件给的是 Company ID + License Token（公司级凭证），
            # 后端会自动调 /customers 换出 customer_id/access_token 并缓存到本地。
            {"key": "mubert_company_id", "label": "Mubert Company ID", "sensitive": True,
             "placeholder": "邮件里的 Company ID",
             "hint": "邮件里 Mubert 发给你的 Company ID（公司级凭证）",
             "provider_for": "mubert"},
            {"key": "mubert_license_token", "label": "Mubert License Token", "sensitive": True,
             "placeholder": "邮件里的 License Token",
             "hint": "邮件里 Mubert 发给你的 License Token；后端首次调用时会用它换出 customer 凭证缓存到本地",
             "provider_for": "mubert"},
            {"key": "mubert_base_url", "label": "Mubert Base URL",
             "placeholder": "https://music-api.mubert.com/api/v3",
             "provider_for": "mubert"},
        ],
    },
    "publish_youtube": {
        "label": "发布：YouTube",
        "icon": "youtube",
        "fields": _publisher_fields(YouTubePublisher),
    },
    "publish_tiktok": {
        "label": "发布：TikTok",
        "icon": "tiktok",
        "fields": _publisher_fields(TikTokPublisher),
    },
    # 账户：credits 余额 / 登录登出 由前端在此 tab 渲染（credits card + 登录）。
    # 「运行模式」默认云端，作为高级/开发开关保留（本地开发才切 local）。
    # 账户：credits 余额 / 充值 / 退出登录，都由前端定制渲染，没有可填字段。
    # 「运行模式」开关已于 2026-07-24 移除 —— 用户只该用云端；开发调试用
    # 环境变量 OCTOPUS_DEPLOYMENT_MODE=local（deployment_mode() 里 env 优先）。
    "deploy": {
        "label": "账户",
        "icon": "server",
        "fields": [],
    },
}


# ─── DTO ───

class FieldUpdate(BaseModel):
    value: str
    sensitive: bool = False


class CategoryUpdate(BaseModel):
    fields: Dict[str, FieldUpdate]


# ─── 0.8 缝3：企业字段在 cloud_client 下不出现在设置 UI ───
# 企业凭证（视频区 火山/蚂蚁 + 视频数据面 OSS）0.9 只存在于云端：本机既读不到
# （runtime_config._enterprise_get 返回空），也不该让用户看到或填写。
# 权威清单在 runtime_config.ENTERPRISE_KEYS，这里只是它在 UI 上的投影。

# 2026-07-22 云端化：这些分类在 cloud_client 下**整类隐藏** —— 平台全包，用户不用配。
#   video   = 视频走云端中转（credits）
#   storage = OSS 云端自给自足（首帧上传云端已能自己做）
# 用户在云端模式只需配 BYOK：llm / image / audio / publish（+账户）。
HIDDEN_CATEGORIES_IN_CLOUD = {"video", "storage"}


def _category_hidden(cat_id: str) -> bool:
    return is_cloud_client() and cat_id in HIDDEN_CATEGORIES_IN_CLOUD


def _visible_fields(category: str, fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not is_cloud_client():
        return fields
    return [f for f in fields if not is_enterprise_key(category, f["key"])]


def _visible_values(category: str, values: Dict[str, Any]) -> Dict[str, Any]:
    if not is_cloud_client():
        return values
    return {k: v for k, v in values.items() if not is_enterprise_key(category, k)}


# ─── 路由 ───

@router.get("")
async def get_all_settings():
    """列出所有分类，每个分类下的字段值（敏感遮罩）+ 元数据"""
    out = {}
    for cat_id, meta in CATEGORIES.items():
        if _category_hidden(cat_id):
            continue  # 云端模式：video/storage 整类隐藏（平台全包）
        fields = _visible_fields(cat_id, meta["fields"])
        # deploy(账户) 是**故意零字段**的（余额/充值/退出登录由前端定制渲染）→ 不能被这条规则跳掉，
        # 否则设置页里整个「账户」tab 消失（2026-07-30 踩过）。
        if not fields and cat_id != "deploy":
            continue  # cloud_client 下整类都是企业字段 → 整张卡片不显示
        out[cat_id] = {
            "label": meta["label"],
            "icon": meta.get("icon"),
            "fields": fields,
            "values": _visible_values(cat_id, user_settings.list_category(cat_id, mask_sensitive=True)),
        }
    return out


@router.get("/status")
async def get_status():
    """配置完整性检查 — Setup Wizard 用来决定要不要进引导"""
    # 关键字段：LLM 必填一个 provider 的 key；视频 ark key 强烈推荐；图片至少一个
    llm_provider = user_settings.get("llm", "provider") or "kimi"
    llm_ok = user_settings.is_set("llm", f"{llm_provider}_api_key")
    image_ok = (
        user_settings.is_set("image", "zhipu_api_key")
        or user_settings.is_set("image", "google_api_key")
    )
    # 0.8 缝3：cloud_client 下视频走云端中转（企业 key 在云端），本地没有 ark key 也算配置完整
    video_ok = True if is_cloud_client() else user_settings.is_set("video", "ark_api_key")
    audio_provider = (user_settings.get("audio", "provider") or "elevenlabs").lower()
    if audio_provider == "elevenlabs":
        audio_ok = user_settings.is_set("audio", "elevenlabs_api_key")
    else:
        audio_ok = (
            user_settings.is_set("audio", "mubert_company_id")
            and user_settings.is_set("audio", "mubert_license_token")
        )
    return {
        "llm_configured": llm_ok,
        "image_configured": image_ok,
        "video_configured": video_ok,
        "audio_configured": audio_ok,
        "needs_wizard": not (llm_ok and image_ok),  # 没 LLM/图片就跳引导；音频可选
    }


@router.get("/{category}")
async def get_category(category: str):
    if category not in CATEGORIES:
        raise HTTPException(404, f"未知分类: {category}")
    if _category_hidden(category):
        raise HTTPException(404, f"云端模式下不需要配置该分类: {category}")
    return {
        "label": CATEGORIES[category]["label"],
        "fields": _visible_fields(category, CATEGORIES[category]["fields"]),
        "values": _visible_values(category, user_settings.list_category(category, mask_sensitive=True)),
    }


@router.patch("/{category}")
async def update_category(category: str, payload: CategoryUpdate):
    """批量更新某分类下多个字段。

    - 非敏感字段：空字符串 = 清除该项（清除后会回退到 .env 默认值）
    - 敏感字段：空字符串 = "不动"（保护用户误操作；要真清空请用 DELETE）
    """
    if category not in CATEGORIES:
        raise HTTPException(404, f"未知分类: {category}")
    # 0.8 缝3：cloud_client 下企业字段不在 schema 内 → 连写都拒（企业 key 不该落本机 DB）
    field_meta = {f["key"]: f for f in _visible_fields(category, CATEGORIES[category]["fields"])}
    updated = []
    skipped_sensitive = []
    rejected_enterprise = []
    for k, item in payload.fields.items():
        meta = field_meta.get(k)
        if not meta:
            if is_enterprise_key(category, k):
                rejected_enterprise.append(k)
            continue  # 忽略不在 schema 里的字段（防止前端乱传）
        sensitive = bool(meta.get("sensitive", False))
        if sensitive and not (item.value or "").strip():
            skipped_sensitive.append(k)
            continue
        await user_settings.set(category, k, item.value or "", sensitive=sensitive)
        updated.append(k)
    return {
        "updated": updated,
        "count": len(updated),
        "skipped_sensitive_blank": skipped_sensitive,
        "rejected_enterprise": rejected_enterprise,
    }


@router.delete("/{category}/{key}")
async def delete_field(category: str, key: str):
    if category not in CATEGORIES:
        raise HTTPException(404, f"未知分类: {category}")
    await user_settings.delete(category, key)
    return {"deleted": True, "category": category, "key": key}


# ─── 校验：测试某 provider 的 key 是否有效 ───

@router.post("/test/{category}/{provider}")
async def test_provider(category: str, provider: str):
    """轻量探测：用当前配置发一个最小请求，验证 key 是否有效。"""
    if category == "llm":
        return await _test_llm(provider)
    if category == "image":
        return await _test_image(provider)
    if category == "video":
        return await _test_video(provider)
    if category == "audio":
        return await _test_audio(provider)
    if category == "comfyui":
        return await _test_comfyui()
    raise HTTPException(400, f"不支持测试该分类: {category}")


async def _test_comfyui() -> Dict[str, Any]:
    """探测本地 ComfyUI 是否可达 + 内置/自定义转视角工作流是否可加载。"""
    from app.core.runtime_config import comfyui_config, scene_camera_workflow
    cfg = comfyui_config()
    base = (cfg.base_url or "http://127.0.0.1:8188").rstrip("/")
    import httpx
    # 1) 连通性：GET /system_stats
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{base}/system_stats")
    except Exception as e:
        return {"ok": False, "model": "comfyui",
                "error": f"连不上 {base} —— 请确认本地 ComfyUI 已启动。{str(e)[:120]}"}
    if r.status_code != 200:
        return {"ok": False, "model": "comfyui",
                "error": f"HTTP {r.status_code}（{base} 有服务但不是 ComfyUI?）"}
    gpu = ""
    try:
        devs = (r.json() or {}).get("devices") or []
        gpu = devs[0].get("name", "") if devs else ""
    except Exception:
        pass
    # 2) 转视角工作流是否可加载（内置或用户自定义）
    wf_note = ""
    try:
        from app.modules._comfyui_qwen import resolve_workflow_path
        wf = resolve_workflow_path(scene_camera_workflow())
        import os
        wf_note = f"；工作流 ✓（{os.path.basename(wf)}）" if wf and os.path.isfile(wf) else "；⚠️ 转视角工作流未找到"
    except Exception:
        wf_note = ""
    return {"ok": True, "model": "comfyui", "base_url": base,
            "reply": f"已连接 ComfyUI（{gpu or 'ok'}）{wf_note}"}


async def _test_llm(provider: str) -> Dict[str, Any]:
    from openai import AsyncOpenAI
    from app.core.runtime_config import active_llm
    cfg = active_llm()
    if cfg.provider != provider:
        return {"ok": False, "error": f"当前激活的是 {cfg.provider}，请先切换 provider"}
    if not cfg.api_key:
        return {"ok": False, "error": "未配置 API Key"}
    try:
        client = (
            AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url,
                        default_headers={"User-Agent": "KimiCLI/1.30.0"})
            if cfg.provider == "kimi"
            else AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
        )
        resp = await client.chat.completions.create(
            model=cfg.model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        return {"ok": True, "model": cfg.model, "reply": resp.choices[0].message.content[:50]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


async def _test_image(provider: str) -> Dict[str, Any]:
    """图片 provider 的轻量校验：直接看 key 是否非空，真正调用太贵。"""
    from app.core.runtime_config import zhipu_image, google_image
    if provider == "zhipu":
        cfg = zhipu_image()
        return {"ok": bool(cfg.api_key), "model": cfg.model,
                "error": None if cfg.api_key else "未配置智谱 API Key"}
    if provider == "google":
        cfg = google_image()
        return {"ok": bool(cfg.api_key), "model": cfg.model,
                "error": None if cfg.api_key else "未配置 Google API Key"}
    return {"ok": False, "error": f"未知 image provider: {provider}"}


async def _test_video(provider: str = "") -> Dict[str, Any]:
    """视频 provider 校验：当前实现仅做 key 非空 / DashScope key 真探测。
    真正生成调用要 30s+，不在测试连接里做。
    """
    from app.core.runtime_config import ark_config, dashscope_video, active_video_provider_name
    target = (provider or active_video_provider_name() or "ark").lower()
    if target == "dashscope":
        cfg = dashscope_video()
        if not cfg.api_key:
            return {"ok": False, "model": cfg.i2v_model, "error": "未配置 DashScope API Key"}
        # 用 GET /api/v1/tasks/dummy 探测 key —— 401 才是 key 错
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{cfg.base_url.rstrip('/')}/tasks/__octopus_ping__",
                    headers={"Authorization": f"Bearer {cfg.api_key}"},
                )
            if r.status_code == 401:
                return {"ok": False, "model": cfg.i2v_model, "error": "DashScope API Key 无效（401）"}
            # 404/400 都说明 key 是被接受了，只是 task_id 不存在
            return {"ok": True, "model": cfg.i2v_model}
        except Exception as e:
            return {"ok": False, "model": cfg.i2v_model, "error": f"探测失败: {e}"}
    if target == "comfyui":
        from app.core.runtime_config import comfyui_config
        ccfg = comfyui_config()
        import httpx
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(f"{ccfg.base_url.rstrip('/')}/system_stats")
            if r.status_code == 200:
                dev = ""
                try:
                    dev = (r.json().get("devices") or [{}])[0].get("name", "")
                except Exception:
                    pass
                return {"ok": True, "model": ccfg.model, "error": None, "detail": dev}
            return {"ok": False, "model": ccfg.model, "error": f"ComfyUI HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "model": ccfg.model, "error": f"连不上 ComfyUI: {str(e)[:120]}"}
    if target == "antchain":
        # 0.7.3 蚂蚁数科：校验 AK/SK 非空 + SDK 是否安装（真生成 30s+ 不在此做）
        from app.core.runtime_config import antchain_video
        acfg = antchain_video()
        if not acfg.ak or not acfg.sk:
            return {"ok": False, "model": acfg.model, "error": "未配置蚂蚁数科 AccessKey（AK/SK）"}
        try:
            import antchain_sdk_aigc  # noqa: F401
        except ImportError:
            return {"ok": False, "model": acfg.model,
                    "error": "AK/SK 已填，但未安装 SDK：pip install antchain_aigc"}
        return {"ok": True, "model": acfg.model,
                "detail": f"AK/SK 已配置 · 网关 {acfg.endpoint}（真连通需提交任务验证）"}
    if target == "octopus_cloud":
        # 0.8 缝1：只校验网关/token 是否填了；真连通要等 0.9 云端网关上线
        from app.core.runtime_config import octopus_cloud_config
        occfg = octopus_cloud_config()
        if not occfg.gateway_url:
            return {"ok": False, "model": occfg.model, "error": "未配置 Octopus 网关地址"}
        if not occfg.token:
            return {"ok": False, "model": occfg.model, "error": "未配置 Octopus 账号 Token"}
        return {"ok": True, "model": occfg.model,
                "detail": f"网关 {occfg.gateway_url}（云端服务 0.9 上线，当前仅本地插口就位）"}
    # 默认 ark：key 非空就算 ok
    cfg = ark_config()
    return {
        "ok": bool(cfg.api_key),
        "model": cfg.video_model,
        "error": None if cfg.api_key else "未配置火山方舟 API Key",
    }


async def _test_audio(provider: str = "") -> Dict[str, Any]:
    """音频 provider 校验：
    - mubert: 调 POST /service/customers 验证 + 缓存 customer 凭证
    - elevenlabs: 调 GET /v1/user 验证 xi-api-key 有效（轻量，不消耗音乐配额）
    """
    from app.core.runtime_config import (
        mubert_config, elevenlabs_music_config, active_audio_provider_name,
    )
    target = (provider or active_audio_provider_name() or "elevenlabs").lower()

    if target == "elevenlabs":
        cfg = elevenlabs_music_config()
        if not cfg.api_key:
            return {"ok": False, "model": "elevenlabs", "error": "未配置 ElevenLabs API Key"}
        import httpx
        try:
            # 用实际生成端点 /v1/music 发空 body 做探针：只验「鉴权 + 音乐权限」，
            # 缺 prompt 必被校验拦下(422)，绝不会真生成、不耗额度。
            # 不用 /v1/user —— 那需要 user_read 权限，只开了音乐权限的 key 会 401 missing_permission。
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"{cfg.base_url.rstrip('/')}/v1/music",
                    headers={"xi-api-key": cfg.api_key, "Content-Type": "application/json"},
                    json={},
                )
            if r.status_code in (400, 422):
                # 鉴权 + 音乐权限已过，仅因空 body 被校验拦下 → key 可用于音乐生成
                return {"ok": True, "model": cfg.model_id, "base_url": cfg.base_url,
                        "reply": f"已连接，音乐权限正常; model={cfg.model_id}"}
            if r.status_code == 200:
                return {"ok": True, "model": cfg.model_id, "base_url": cfg.base_url,
                        "reply": f"已连接; model={cfg.model_id}"}
            if r.status_code == 401:
                if "missing_permission" in r.text.lower():
                    return {"ok": False, "model": "elevenlabs",
                            "error": "key 缺【音乐 Music】权限：请在 ElevenLabs 后台给该 API Key 勾选 Music 权限"}
                return {"ok": False, "model": "elevenlabs", "error": "API Key 无效（401）"}
            return {"ok": False, "model": "elevenlabs",
                    "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:
            return {"ok": False, "model": "elevenlabs", "error": f"探测失败: {str(e)[:200]}"}

    if target == "mubert":
        from app.modules.mubert_music import _ensure_customer
        cfg = mubert_config()
        if not cfg.company_id or not cfg.license_token:
            return {
                "ok": False,
                "model": "mubert",
                "error": "未配置 Mubert Company ID / License Token",
            }
        try:
            customer_id, _ = await _ensure_customer(
                cfg.company_id, cfg.license_token, cfg.base_url,
                force_refresh=True,  # 测试时强制走 /customers 真实换一次
            )
            return {"ok": True, "model": "mubert",
                    "base_url": cfg.base_url,
                    "reply": f"已换出 customer_id={customer_id[:12]}... 并缓存"}
        except Exception as e:
            return {
                "ok": False,
                "model": "mubert",
                "error": f"调 /service/customers 失败: {str(e)[:200]}",
            }
    return {"ok": False, "error": f"未知 audio provider: {target}"}
