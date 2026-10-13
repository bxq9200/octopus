"""充值支付（支付宝·电脑网站支付）。

流程：
  客户端点充值 → `POST /v1/pay/create` 建订单 → 回一个 `pay_url`
  → 客户端用系统浏览器打开它 → 支付宝收银台 → 用户付款
  → 支付宝**异步通知** `POST /v1/pay/notify`（验签 → 幂等加 credits）
  → 用户浏览器被跳回 `GET /pay/return`（只是给人看的结果页，**不作为到账依据**）
  → 客户端轮询 `GET /v1/pay/orders/{id}` 看到 paid → 刷新余额

安全要点（别动）：
  1. **到账只认异步通知**（return_url 参数可被用户伪造）。
  2. 通知必须 **RSA2 验签 + 校验 app_id + 校验金额与订单一致**，三者缺一不可。
  3. 加 credits 走 `db.settle_order` 的 ledger 哨兵幂等 —— 支付宝会重复推送直到收到 "success"。
"""
import base64
import logging
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

import config
import db
from auth import current_user

logger = logging.getLogger("octopus.cloud.pay")
router = APIRouter(tags=["pay"])


# ═══════════════════════════════════════════════════════
# 充值档位：1 credit = 1 分钱。视频约 0.8 元/秒 → ¥50 ≈ 60 秒成片。
# 改价只动这张表（客户端从 /v1/pay/tiers 拉，不写死）。
# ═══════════════════════════════════════════════════════

TIERS: List[Dict[str, Any]] = [
    {"id": "t50",   "yuan": 50,   "bonus_pct": 0,  "label": "体验"},
    {"id": "t200",  "yuan": 200,  "bonus_pct": 5,  "label": "常用"},
    {"id": "t500",  "yuan": 500,  "bonus_pct": 10, "label": "推荐"},
    {"id": "t1000", "yuan": 1000, "bonus_pct": 15, "label": "工作室"},
]


def _tier(tier_id: str) -> Optional[Dict[str, Any]]:
    return next((t for t in TIERS if t["id"] == tier_id), None)


def _tier_credits(t: Dict[str, Any]) -> tuple[int, int]:
    """→ (总 credits, 其中赠送)。1 元 = 100 credits。"""
    base = int(t["yuan"]) * 100
    bonus = base * int(t["bonus_pct"]) // 100
    return base + bonus, bonus


# ═══════════════════════════════════════════════════════
# 支付宝签名 / 验签（RSA2 = SHA256withRSA）
# ═══════════════════════════════════════════════════════

def _load_private_key():
    from cryptography.hazmat.primitives.serialization import load_der_private_key
    raw = base64.b64decode("".join(config.ALIPAY_PRIVATE_KEY.split()))
    try:
        return load_der_private_key(raw, password=None)          # PKCS#8
    except Exception:
        # PKCS#1（"BEGIN RSA PRIVATE KEY"）→ 用 PEM 头重新解析
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
               + "\n".join(config.ALIPAY_PRIVATE_KEY[i:i + 64]
                           for i in range(0, len(config.ALIPAY_PRIVATE_KEY), 64))
               + "\n-----END RSA PRIVATE KEY-----\n")
        return load_pem_private_key(pem.encode(), password=None)


def _load_alipay_public_key():
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    raw = base64.b64decode("".join(config.ALIPAY_PUBLIC_KEY.split()))
    return load_der_public_key(raw)


def _sign_content(params: Dict[str, str], *, drop_sign_type: bool) -> str:
    """签名原文：剔除指定参数与空值 → 按 key 升序 → k=v& 拼接（**不做 URL 编码**）。

    🔴 **请求签名与通知验签的剔除规则不同**（踩过：两边都剔 sign_type → 支付宝报「验签出错」）：
      - 请求签名（我们发给支付宝）：**只剔 `sign`**，`sign_type` 必须参与签名
      - 异步通知验签（验支付宝发来的）：剔 `sign` **和** `sign_type`
    """
    drop = {"sign", "sign_type"} if drop_sign_type else {"sign"}
    items = sorted((k, v) for k, v in params.items()
                   if k not in drop and v not in (None, ""))
    return "&".join(f"{k}={v}" for k, v in items)


def _sign(params: Dict[str, str]) -> str:
    """给**我们发出的请求**签名 —— sign_type 参与签名。"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    content = _sign_content(params, drop_sign_type=False)
    sig = _load_private_key().sign(content.encode("utf-8"),
                                   padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def verify_notify(params: Dict[str, str]) -> bool:
    """验支付宝异步通知签名 —— sign 和 sign_type 都不参与验签。"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.exceptions import InvalidSignature
    sign = params.get("sign") or ""
    if not sign:
        return False
    content = _sign_content(params, drop_sign_type=True)
    try:
        _load_alipay_public_key().verify(
            base64.b64decode(sign), content.encode("utf-8"),
            padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, Exception) as e:      # noqa: B014
        logger.warning("[pay] 验签失败: %s", str(e)[:120])
        return False


def build_pay_url(out_trade_no: str, yuan: int, subject: str) -> str:
    """拼「电脑网站支付」跳转 URL（GET 方式，浏览器打开即到收银台）。"""
    import json as _json
    biz = {
        "out_trade_no": out_trade_no,
        "total_amount": f"{yuan:.2f}",
        "subject": subject,
        "product_code": "FAST_INSTANT_TRADE_PAY",
    }
    base = config.SITE_BASE_URL.rstrip("/")
    params = {
        "app_id": config.ALIPAY_APP_ID,
        "method": "alipay.trade.page.pay",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": f"{base}/v1/pay/notify",
        "return_url": f"{base}/pay/return",
        "biz_content": _json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
    }
    params["sign"] = _sign(params)
    return f"{config.ALIPAY_GATEWAY}?{urlencode(params, quote_via=quote_plus)}"


# ═══════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════

@router.get("/v1/pay/tiers")
async def pay_tiers() -> Dict[str, Any]:
    """充值档位（客户端渲染用）。enabled=False 时前端提示「暂未开放，请联系客服」。"""
    out = []
    for t in TIERS:
        credits, bonus = _tier_credits(t)
        out.append({**t, "credits": credits, "bonus": bonus})
    return {"enabled": config.alipay_configured(), "tiers": out}


class CreateOrderRequest(BaseModel):
    tier: str


@router.post("/v1/pay/create")
async def pay_create(req: CreateOrderRequest,
                     user: db.User = Depends(current_user),
                     session: AsyncSession = Depends(db.get_session)) -> Dict[str, Any]:
    if not config.alipay_configured():
        raise HTTPException(503, "支付未开放（管理员尚未配置支付宝）")
    t = _tier(req.tier)
    if not t:
        raise HTTPException(400, f"未知档位: {req.tier}")
    credits, bonus = _tier_credits(t)
    oid = "OCT" + uuid.uuid4().hex[:20]
    order = db.Order(id=oid, user_id=user.id, amount_fen=int(t["yuan"]) * 100,
                     credits=credits, bonus=bonus, tier=t["id"], status="pending")
    session.add(order)
    await session.commit()
    url = build_pay_url(oid, int(t["yuan"]), f"Octopus 充值 {t['yuan']} 元")
    logger.info("[pay] 建单 %s user=%s ¥%s → %s credits", oid, user.email, t["yuan"], credits)
    return {"order_id": oid, "pay_url": url, "yuan": t["yuan"], "credits": credits}


@router.get("/v1/pay/orders/{order_id}")
async def pay_order(order_id: str,
                    user: db.User = Depends(current_user),
                    session: AsyncSession = Depends(db.get_session)) -> Dict[str, Any]:
    """客户端轮询订单状态。只能查自己的单。"""
    r = await session.execute(select(db.Order).where(db.Order.id == order_id))
    o = r.scalar_one_or_none()
    if not o or o.user_id != user.id:
        raise HTTPException(404, "订单不存在")
    return {"order_id": o.id, "status": o.status, "credits": o.credits,
            "yuan": o.amount_fen / 100, "paid_at": o.paid_at.isoformat() if o.paid_at else None}


@router.post("/v1/pay/notify")
async def pay_notify(request: Request,
                     session: AsyncSession = Depends(db.get_session)):
    """支付宝异步通知 —— **唯一的到账依据**。必须回纯文本 "success"，否则支付宝会一直重推。"""
    from fastapi.responses import PlainTextResponse
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    if not verify_notify(params):
        logger.warning("[pay] 通知验签不通过 out_trade_no=%s", params.get("out_trade_no"))
        return PlainTextResponse("failure")
    if params.get("app_id") != config.ALIPAY_APP_ID:            # 防串号
        logger.warning("[pay] app_id 不匹配: %s", params.get("app_id"))
        return PlainTextResponse("failure")

    status = params.get("trade_status") or ""
    oid = params.get("out_trade_no") or ""
    r = await session.execute(select(db.Order).where(db.Order.id == oid))
    order = r.scalar_one_or_none()
    if not order:
        logger.warning("[pay] 通知里的订单不存在: %s", oid)
        return PlainTextResponse("failure")

    if status not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        logger.info("[pay] 订单 %s 状态=%s，不入账", oid, status)
        return PlainTextResponse("success")                      # 已收到，别再推

    # 金额必须与下单时一致（防篡改）
    try:
        paid_fen = int(round(float(params.get("total_amount") or 0) * 100))
    except ValueError:
        paid_fen = -1
    if paid_fen != order.amount_fen:
        logger.warning("[pay] 金额不符 订单=%s 应付=%s 实付=%s", oid, order.amount_fen, paid_fen)
        return PlainTextResponse("failure")

    first = await db.settle_order(session, order, params.get("trade_no") or "")
    logger.info("[pay] %s 到账 %s credits（%s）", oid, order.credits,
                "首次" if first else "重复通知·已幂等跳过")
    return PlainTextResponse("success")


_RESULT_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>支付结果 — Octopus</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{background:#07070b;color:#e6e6ef;
font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;display:flex;align-items:center;
justify-content:center;height:100vh;text-align:center}
.c{max-width:420px;padding:40px}.i{font-size:56px;margin-bottom:18px}
h1{font-size:23px;margin-bottom:12px;color:#fff}p{color:#9a9ab0;line-height:1.7;font-size:15px}
</style></head><body><div class="c">
<div class="i">✅</div><h1>支付完成</h1>
<p>额度会在几秒内到账。<br>回到 Octopus 就能看到新余额，这个页面可以关掉了。</p>
</div></body></html>"""


@router.get("/pay/return", response_class=HTMLResponse)
async def pay_return() -> str:
    """支付完成后用户浏览器被跳到这里 —— **纯展示**，到账以异步通知为准。"""
    return _RESULT_HTML
