"""模型成本核算 — 2025/2026 公开价 + 项目实测账单参考的硬编码版。

提供给 cost_summary 节点用于把 project.cost_log 里的 usage 翻译成 ¥ / $。

后续可挪到 user_settings 让用户改（0.6.6 扩展点）。

价格信息更新日期：2026-05
"""

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────
# 价目表：key = (provider, model)
# 计费维度按 kind:
#   llm   → in_per_1m + out_per_1m (单位：元/USD per 1M token)
#   image → per_image (单价)
#   video → per_second + 可选分辨率档位 per_second_720p / per_second_1080p
#   audio → per_second
# currency: "CNY" 或 "USD"，最终展示按 DEFAULT_USD_TO_CNY 互转
# ─────────────────────────────────────────────────────
PRICING: Dict[Tuple[str, str], Dict[str, Any]] = {
    # ── LLM ──
    ("kimi", "moonshot-v1-32k"):       {"currency": "CNY", "in_per_1m": 12.0, "out_per_1m": 12.0},
    ("kimi", "moonshot-v1-8k"):        {"currency": "CNY", "in_per_1m": 12.0, "out_per_1m": 12.0},
    ("kimi", "moonshot-v1-128k"):      {"currency": "CNY", "in_per_1m": 60.0, "out_per_1m": 60.0},
    ("kimi", "kimi-k2-0905-preview"):  {"currency": "CNY", "in_per_1m": 4.0, "out_per_1m": 16.0},
    ("kimi", "kimi-k2-turbo-preview"): {"currency": "CNY", "in_per_1m": 4.0, "out_per_1m": 16.0},
    ("kimi", "kimi-k2.6"):             {"currency": "CNY", "in_per_1m": 4.0, "out_per_1m": 16.0},
    ("deepseek", "deepseek-chat"):     {"currency": "CNY", "in_per_1m": 2.0, "out_per_1m": 8.0},
    ("deepseek", "deepseek-reasoner"): {"currency": "CNY", "in_per_1m": 4.0, "out_per_1m": 16.0},
    ("openai", "gpt-4o"):              {"currency": "USD", "in_per_1m": 2.5, "out_per_1m": 10.0},
    ("openai", "gpt-4o-mini"):         {"currency": "USD", "in_per_1m": 0.15, "out_per_1m": 0.6},

    # ── Image（每张）──
    ("zhipu", "glm-image"):       {"currency": "CNY", "per_image": 0.06},
    ("zhipu", "cogview-3-plus"):  {"currency": "CNY", "per_image": 0.06},
    ("zhipu", "cogview-4"):       {"currency": "CNY", "per_image": 0.10},
    ("ark", "seedream-3.0"):      {"currency": "CNY", "per_image": 0.20},
    ("ark", "doubao-seedream-3-0-t2i-250415"): {"currency": "CNY", "per_image": 0.20},
    ("ark", "doubao-seedream-5-0-260128"):     {"currency": "CNY", "per_image": 0.30},  # 2026-01 Seedream 5.0
    ("google", "imagen-3"):                   {"currency": "USD", "per_image": 0.04},
    ("google", "gemini-3-pro-image-preview"): {"currency": "USD", "per_image": 0.04},

    # ── Video（每秒，按分辨率档位）──
    ("ark", "doubao-seedance-1.0"):                {"currency": "CNY", "per_second_720p": 0.10, "per_second_1080p": 0.20},
    ("ark", "doubao-seedance-1-0-pro-250528"):     {"currency": "CNY", "per_second_720p": 0.10, "per_second_1080p": 0.20},
    ("ark", "doubao-seedance-1-0-pro-fast-251015"): {"currency": "CNY", "per_second_720p": 0.08, "per_second_1080p": 0.16},  # fast 档稍便宜
    ("ark", "doubao-seedance-1-0-lite-i2v-250428"): {"currency": "CNY", "per_second_720p": 0.05, "per_second_1080p": 0.10},
    ("ark", "doubao-seedance-1-0-lite-t2v-250428"): {"currency": "CNY", "per_second_720p": 0.05, "per_second_1080p": 0.10},
    ("ark", "doubao-seedance-2-0-260128"):          {"currency": "CNY", "per_second_720p": 0.15, "per_second_1080p": 0.30},  # 2026-01 Seedance 2.0
    # 0.7.3 蚂蚁数科渠道 Seedance 2.0（占位价=与 ark 同；拿到渠道实际报价后改这里）
    ("antchain", "doubao-seedance-2-0-260128"):     {"currency": "CNY", "per_second_720p": 0.15, "per_second_1080p": 0.30},

    # ── Audio（每秒）──
    ("elevenlabs", "music_v1"): {"currency": "USD", "per_second": 0.04},
    ("mubert", "default"):      {"currency": "USD", "per_second": 0.005},
}

# 0.6.5: 前缀匹配兜底 — 厂家每出新版我们就缺一次精确条目；按 (provider, model_prefix) → 用最长匹配的家族基础价
# 这样未来出 kimi-k3 / seedance-3.0 之类还能算出一个合理估算（带 price_known=False 提示）
PREFIX_PRICING: List[Tuple[str, str, Dict[str, Any]]] = [
    # LLM 家族基础价（按 1M token）
    ("kimi",  "kimi-k2",       {"currency": "CNY", "in_per_1m": 4.0, "out_per_1m": 16.0}),
    ("kimi",  "moonshot-v1",   {"currency": "CNY", "in_per_1m": 12.0, "out_per_1m": 12.0}),
    ("kimi",  "kimi",          {"currency": "CNY", "in_per_1m": 4.0, "out_per_1m": 16.0}),  # 任何 kimi-* 兜底
    ("deepseek", "deepseek",   {"currency": "CNY", "in_per_1m": 2.0, "out_per_1m": 8.0}),
    ("openai", "gpt-4o",       {"currency": "USD", "in_per_1m": 2.5, "out_per_1m": 10.0}),
    # Image 家族基础价
    ("ark",   "doubao-seedream-5", {"currency": "CNY", "per_image": 0.30}),
    ("ark",   "doubao-seedream-3", {"currency": "CNY", "per_image": 0.20}),
    ("ark",   "doubao-seedream",   {"currency": "CNY", "per_image": 0.20}),  # 任何 seedream 兜底
    ("ark",   "seedream",          {"currency": "CNY", "per_image": 0.20}),
    ("zhipu", "glm-image",         {"currency": "CNY", "per_image": 0.06}),
    ("zhipu", "cogview",           {"currency": "CNY", "per_image": 0.06}),
    ("google", "gemini",           {"currency": "USD", "per_image": 0.04}),
    ("google", "imagen",           {"currency": "USD", "per_image": 0.04}),
    # Video 家族基础价
    ("ark",   "doubao-seedance-2",      {"currency": "CNY", "per_second_720p": 0.15, "per_second_1080p": 0.30}),
    ("ark",   "doubao-seedance-1-0-pro-fast", {"currency": "CNY", "per_second_720p": 0.08, "per_second_1080p": 0.16}),
    ("ark",   "doubao-seedance-1-0-pro", {"currency": "CNY", "per_second_720p": 0.10, "per_second_1080p": 0.20}),
    ("ark",   "doubao-seedance-1-0-lite", {"currency": "CNY", "per_second_720p": 0.05, "per_second_1080p": 0.10}),
    ("ark",   "doubao-seedance",         {"currency": "CNY", "per_second_720p": 0.10, "per_second_1080p": 0.20}),  # 兜底
    ("ark",   "seedance",                {"currency": "CNY", "per_second_720p": 0.10, "per_second_1080p": 0.20}),
    # 0.7.3 蚂蚁数科渠道（占位价；拿到实际报价后改）
    ("antchain", "doubao-seedance-2",    {"currency": "CNY", "per_second_720p": 0.15, "per_second_1080p": 0.30}),
    ("antchain", "seedance",             {"currency": "CNY", "per_second_720p": 0.15, "per_second_1080p": 0.30}),
    ("antchain", "wan",                  {"currency": "CNY", "per_second_720p": 0.10, "per_second_1080p": 0.20}),
    ("antchain", "happyhorse",           {"currency": "CNY", "per_second_720p": 0.05, "per_second_1080p": 0.10}),
    # 0.7.7 蚂蚁视频翻译(videotranslate) —— 占位价：蚂蚁未报价，按 (合成片时长 × 成功语言数) 秒数估算，
    # 前缀匹配 → price_known=False → 结算标"估算"。拿到实际报价后在 PRICING 加精确条目
    # （可按 VOICE/FACE/SUBTITLE 分档，FACE 更真更贵）。放在 antchain 兜底之前更醒目（排序按 prefix 长度，位置其实无关）。
    ("antchain", "videotranslate",       {"currency": "CNY", "per_second": 0.10}),
    ("antchain", "",                     {"currency": "CNY", "per_second_720p": 0.15, "per_second_1080p": 0.30}),  # 任何 antchain 兜底
    # Audio 家族基础价
    ("elevenlabs", "music",  {"currency": "USD", "per_second": 0.04}),
    ("mubert", "",           {"currency": "USD", "per_second": 0.005}),  # 任何 mubert 模型
]


def _lookup_pricing(provider: Optional[str], model: Optional[str]) -> Tuple[Optional[Dict[str, Any]], bool]:
    """优先精确匹配；失败时按前缀匹配最长 prefix。
    Returns (cfg, exact_match)。exact_match=False 时算出来的成本不算"已知价"。"""
    if not provider or not model:
        return None, False
    cfg = PRICING.get((provider, model))
    if cfg is not None:
        return cfg, True
    # 前缀匹配：按 prefix 长度倒序，第一个 model.startswith(prefix) 命中即用
    candidates = [(p, m, c) for p, m, c in PREFIX_PRICING if p == provider and (not m or model.startswith(m))]
    if not candidates:
        return None, False
    candidates.sort(key=lambda x: -len(x[1]))  # 最长 prefix 优先
    return candidates[0][2], False

DEFAULT_USD_TO_CNY = 7.2


def calc_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """给一条 cost_log 条目算钱。

    输入：{timestamp, node_id, node_type, phase, usage: {provider, model, kind, ...}}
    输出：原 entry + {cost_cny, cost_usd, price_known: bool}
    """
    usage = entry.get("usage") or {}
    provider = usage.get("provider")
    model = usage.get("model")
    kind = usage.get("kind")

    cfg, price_known = _lookup_pricing(provider, model)
    cfg = cfg or {}

    rate = 0.0
    if kind == "llm":
        in_t = usage.get("prompt_tokens", 0) or 0
        out_t = usage.get("completion_tokens", 0) or 0
        rate = cfg.get("in_per_1m", 0) * in_t / 1e6 + cfg.get("out_per_1m", 0) * out_t / 1e6
    elif kind == "image":
        rate = cfg.get("per_image", 0) * (usage.get("images", 1) or 1)
    elif kind == "video":
        resolution = usage.get("resolution") or "720p"
        per_sec = cfg.get(f"per_second_{resolution}") or cfg.get("per_second", 0)
        rate = per_sec * (usage.get("seconds", 0) or 0)
    elif kind == "audio":
        rate = cfg.get("per_second", 0) * (usage.get("seconds", 0) or 0)

    currency = cfg.get("currency", "CNY")
    if currency == "CNY":
        cny = rate
        usd = rate / DEFAULT_USD_TO_CNY
    else:
        usd = rate
        cny = rate * DEFAULT_USD_TO_CNY

    return {
        **entry,
        "cost_cny": round(cny, 4),
        "cost_usd": round(usd, 4),
        "price_known": price_known,
    }


def summarize(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按模型 + 按阶段汇总成本"""
    by_model_buckets: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(lambda: {
        "calls": 0, "cost_cny": 0.0, "cost_usd": 0.0,
        "tokens": 0, "images": 0, "seconds": 0,
        "price_known": True,
    })
    by_phase_buckets: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "calls": 0, "cost_cny": 0.0, "cost_usd": 0.0,
    })
    total_cny = 0.0
    total_usd = 0.0

    for e in entries:
        u = e.get("usage") or {}
        key = (u.get("provider"), u.get("model"))
        b = by_model_buckets[key]
        b["calls"] += 1
        b["cost_cny"] += e.get("cost_cny", 0)
        b["cost_usd"] += e.get("cost_usd", 0)
        b["tokens"] += (u.get("prompt_tokens", 0) or 0) + (u.get("completion_tokens", 0) or 0)
        b["images"] += u.get("images", 0) or 0
        b["seconds"] += u.get("seconds", 0) or 0
        if not e.get("price_known", True):
            b["price_known"] = False

        p = by_phase_buckets[e.get("phase") or "unknown"]
        p["calls"] += 1
        p["cost_cny"] += e.get("cost_cny", 0)
        p["cost_usd"] += e.get("cost_usd", 0)

        total_cny += e.get("cost_cny", 0)
        total_usd += e.get("cost_usd", 0)

    by_model_list = [
        {
            "provider": k[0] or "(unknown)",
            "model": k[1] or "(unknown)",
            "kind": _kind_for(k),
            **v,
            "cost_cny": round(v["cost_cny"], 4),
            "cost_usd": round(v["cost_usd"], 4),
        }
        for k, v in sorted(by_model_buckets.items(), key=lambda x: -x[1]["cost_cny"])
    ]
    by_phase_list = [
        {
            "phase": k,
            **v,
            "cost_cny": round(v["cost_cny"], 4),
            "cost_usd": round(v["cost_usd"], 4),
        }
        for k, v in sorted(by_phase_buckets.items(), key=lambda x: -x[1]["cost_cny"])
    ]

    return {
        "total_cny": round(total_cny, 2),
        "total_usd": round(total_usd, 2),
        "exchange_rate": DEFAULT_USD_TO_CNY,
        "by_model": by_model_list,
        "by_phase": by_phase_list,
        "entries": entries,
    }


def _kind_for(key: Tuple[Optional[str], Optional[str]]) -> str:
    cfg, _ = _lookup_pricing(key[0], key[1])
    cfg = cfg or {}
    if "in_per_1m" in cfg:
        return "llm"
    if "per_image" in cfg:
        return "image"
    if any(k.startswith("per_second_") for k in cfg) or "per_second" in cfg:
        # video 用 per_second_720p；audio 只用 per_second
        return "video" if any(k.startswith("per_second_") for k in cfg) else "audio"
    return "unknown"
