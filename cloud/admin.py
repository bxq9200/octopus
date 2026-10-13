"""简易管理后台 —— 平台方管理账户 / 充值 / 退款 / 用量。

鉴权：所有 `/v1/admin/*` 接口用 **X-Admin-Token** 头（= secrets.env 的 ADMIN_TOKEN），
**不是用户 JWT**。这是运维接口，跟用户体系完全隔离。
UI：`GET /admin` 返回一个自包含单页（输入 admin token 后本地存、每次请求带上）。

充值/退款都走 `db.grant_credits`（正=充值、负=退款），落 credit_ledger 流水，可对账。
自动支付（微信/支付宝）上线后，其回调里也调 grant_credits —— 这个后台始终是"人工兜底 + 查账"入口。
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select, func

import config
import db

router = APIRouter(tags=["admin"])


async def require_admin(request: Request) -> None:
    if not config.admin_configured():
        raise HTTPException(503, "未配置 ADMIN_TOKEN")
    tok = request.headers.get("x-admin-token", "")
    if not tok or tok != config.ADMIN_TOKEN:
        raise HTTPException(403, "管理员 token 无效")


class GrantRequest(BaseModel):
    email: str
    amount: int          # 正=充值 / 负=退款
    reason: str = "topup"


class StatusRequest(BaseModel):
    status: str          # active | disabled


@router.get("/v1/admin/stats")
async def stats(_: None = Depends(require_admin),
                session=Depends(db.get_session)) -> Dict[str, Any]:
    users = int((await session.execute(select(func.count(db.User.id)))).scalar() or 0)
    credits = int((await session.execute(select(func.coalesce(func.sum(db.User.credits), 0)))).scalar() or 0)
    videos = int((await session.execute(select(func.count(db.VideoTask.id)))).scalar() or 0)
    vid_ok = int((await session.execute(
        select(func.count(db.VideoTask.id)).where(db.VideoTask.status == "succeeded"))).scalar() or 0)
    charged = int((await session.execute(
        select(func.coalesce(func.sum(db.VideoTask.credits_charged), 0)))).scalar() or 0)
    return {
        "users": users, "credits_outstanding": credits,
        "video_tasks": videos, "video_succeeded": vid_ok,
        "credits_charged_total": charged,
    }


@router.get("/v1/admin/users")
async def list_users(_: None = Depends(require_admin),
                     session=Depends(db.get_session), q: str = "", limit: int = 100) -> Dict[str, Any]:
    stmt = select(db.User).order_by(db.User.id.desc()).limit(min(limit, 500))
    if q.strip():
        stmt = select(db.User).where(db.User.email.like(f"%{db.norm_email(q)}%")).order_by(db.User.id.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return {"users": [{
        "id": u.id, "email": u.email, "plan": u.plan, "status": u.status,
        "credits": u.credits,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    } for u in rows]}


@router.post("/v1/admin/grant")
async def grant(payload: GrantRequest, _: None = Depends(require_admin),
                session=Depends(db.get_session)) -> Dict[str, Any]:
    """充值(amount>0) / 退款(amount<0)。"""
    user = await db.get_user_by_email(session, payload.email)
    if not user:
        raise HTTPException(404, f"用户不存在: {payload.email}")
    reason = payload.reason or ("refund" if payload.amount < 0 else "topup")
    new_bal = await db.grant_credits(session, user.id, int(payload.amount), reason=reason)
    return {"ok": True, "email": user.email, "credits": new_bal, "delta": payload.amount}


@router.get("/v1/admin/users/{user_id}/ledger")
async def user_ledger(user_id: int, _: None = Depends(require_admin),
                      session=Depends(db.get_session)) -> Dict[str, Any]:
    rows = (await session.execute(
        select(db.CreditLedger).where(db.CreditLedger.user_id == user_id)
        .order_by(db.CreditLedger.id.desc()).limit(100))).scalars().all()
    return {"entries": [{
        "delta": e.delta, "balance_after": e.balance_after, "reason": e.reason,
        "ref": e.ref, "created_at": e.created_at.isoformat() if e.created_at else None,
    } for e in rows]}


@router.post("/v1/admin/users/{user_id}/status")
async def set_status(user_id: int, payload: StatusRequest, _: None = Depends(require_admin),
                     session=Depends(db.get_session)) -> Dict[str, Any]:
    if payload.status not in ("active", "disabled"):
        raise HTTPException(400, "status 只能是 active / disabled")
    r = await session.execute(select(db.User).where(db.User.id == user_id))
    user = r.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "用户不存在")
    user.status = payload.status
    await session.commit()
    return {"ok": True, "id": user_id, "status": payload.status}


@router.get("/admin", response_class=HTMLResponse)
async def admin_page() -> str:
    return _ADMIN_HTML


_ADMIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Octopus 管理后台</title>
<style>
 :root{--bg:#0d0d14;--card:#16161f;--bd:#2a2a38;--fg:#e6e6ef;--mut:#8a8a9a;--cy:#22d3ee;--red:#f87171;--grn:#34d399}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
 .wrap{max-width:1100px;margin:0 auto;padding:24px}
 h1{font-size:18px;margin:0 0 4px} .sub{color:var(--mut);font-size:12px;margin-bottom:20px}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px;margin-bottom:16px}
 input,select,button{font:inherit;color:var(--fg);background:#0f0f18;border:1px solid var(--bd);border-radius:6px;padding:8px 10px}
 button{cursor:pointer;background:#1a2530;border-color:#2f4a5a;color:var(--cy)} button:hover{background:#22323f}
 button.danger{color:var(--red);border-color:#5a2f2f} button.danger:hover{background:#3a2222}
 table{width:100%;border-collapse:collapse;font-size:13px} th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--bd)}
 th{color:var(--mut);font-weight:500} tr:hover td{background:#1a1a24}
 .stat{display:inline-block;margin-right:24px} .stat b{font-size:20px;color:var(--cy);display:block}
 .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center} .mut{color:var(--mut)} .grn{color:var(--grn)} .red{color:var(--red)}
 .badge{font-size:11px;padding:2px 7px;border-radius:10px;background:#1a2530;color:var(--cy)}
 .badge.dis{background:#3a2222;color:var(--red)}
 #login{max-width:420px;margin:80px auto}
</style></head><body><div class="wrap">
<div id="login" class="card">
  <h1>Octopus 管理后台</h1><div class="sub">输入管理员 Token 登录</div>
  <div class="row"><input id="tok" type="password" placeholder="ADMIN_TOKEN" style="flex:1"><button onclick="login()">进入</button></div>
  <div id="loginErr" class="red" style="margin-top:8px"></div>
</div>
<div id="app" style="display:none">
  <h1>Octopus 管理后台 <button class="danger" style="float:right" onclick="logout()">登出</button></h1>
  <div class="sub">充值/退款走 credits 账本（1 credit = 0.01 元 · 视频 0.8 元/秒）</div>
  <div class="card" id="stats"></div>
  <div class="card">
    <div class="row" style="margin-bottom:12px">
      <input id="q" placeholder="搜邮箱" onkeydown="if(event.key==='Enter')loadUsers()">
      <button onclick="loadUsers()">搜索</button>
      <span class="mut">充值/退款：填邮箱→金额(退款填负数)→确认</span>
    </div>
    <div class="row" style="margin-bottom:12px">
      <input id="gEmail" placeholder="邮箱" style="width:240px">
      <input id="gAmt" type="number" placeholder="credits(+充值/-退款)" style="width:180px">
      <input id="gReason" placeholder="备注(可选)" style="width:140px">
      <button onclick="grant()">确认充值/退款</button>
      <span id="gMsg"></span>
    </div>
    <table><thead><tr><th>ID</th><th>邮箱</th><th>套餐</th><th>状态</th><th>credits</th><th>≈¥</th><th>注册</th><th>操作</th></tr></thead>
    <tbody id="users"></tbody></table>
  </div>
</div>
<script>
let TOK = localStorage.getItem('octo_admin_tok') || '';
const H = () => ({'X-Admin-Token': TOK, 'Content-Type':'application/json'});
async function api(path, opts={}){ const r = await fetch(path, {...opts, headers:{...H(), ...(opts.headers||{})}});
  if(!r.ok){ const e = await r.json().catch(()=>({})); throw new Error(e.detail||('HTTP '+r.status)); } return r.json(); }
function login(){ TOK = document.getElementById('tok').value.trim();
  api('/v1/admin/stats').then(()=>{ localStorage.setItem('octo_admin_tok',TOK); show(); })
    .catch(e=>{ document.getElementById('loginErr').textContent = e.message; }); }
function logout(){ localStorage.removeItem('octo_admin_tok'); TOK=''; location.reload(); }
function show(){ document.getElementById('login').style.display='none'; document.getElementById('app').style.display='block'; loadStats(); loadUsers(); }
async function loadStats(){ const s = await api('/v1/admin/stats');
  document.getElementById('stats').innerHTML =
    `<span class="stat"><b>${s.users}</b>用户</span><span class="stat"><b>${s.credits_outstanding}</b>在账credits (≈¥${(s.credits_outstanding/100).toFixed(0)})</span>`+
    `<span class="stat"><b>${s.video_succeeded}/${s.video_tasks}</b>视频出片/总</span><span class="stat"><b>${s.credits_charged_total}</b>已消费credits (≈¥${(s.credits_charged_total/100).toFixed(0)})</span>`; }
async function loadUsers(){ const q = document.getElementById('q').value.trim();
  const d = await api('/v1/admin/users'+(q?('?q='+encodeURIComponent(q)):''));
  document.getElementById('users').innerHTML = d.users.map(u=>`<tr>
    <td>${u.id}</td><td>${u.email}</td><td>${u.plan}</td>
    <td>${u.status==='active'?'<span class=badge>正常</span>':'<span class="badge dis">停用</span>'}</td>
    <td>${u.credits}</td><td class=mut>¥${(u.credits/100).toFixed(2)}</td>
    <td class=mut>${(u.created_at||'').slice(0,10)}</td>
    <td class=row>
      <button onclick="fill('${u.email}')">充值</button>
      <button onclick="ledger(${u.id})">流水</button>
      <button class="${u.status==='active'?'danger':''}" onclick="toggle(${u.id},'${u.status==='active'?'disabled':'active'}')">${u.status==='active'?'停用':'启用'}</button>
    </td></tr>`).join(''); }
function fill(e){ document.getElementById('gEmail').value=e; document.getElementById('gAmt').focus(); }
async function grant(){ const email=document.getElementById('gEmail').value.trim();
  const amount=parseInt(document.getElementById('gAmt').value,10); const reason=document.getElementById('gReason').value.trim();
  const msg=document.getElementById('gMsg');
  if(!email||!amount){ msg.innerHTML='<span class=red>填邮箱和金额</span>'; return; }
  const label = amount>0?('充值 '+amount):('退款 '+(-amount));
  if(!confirm(email+' '+label+' credits (≈¥'+(Math.abs(amount)/100).toFixed(2)+')，确认？')) return;
  try{ const r=await api('/v1/admin/grant',{method:'POST',body:JSON.stringify({email,amount,reason})});
    msg.innerHTML='<span class=grn>成功 → 新余额 '+r.credits+'</span>'; loadUsers(); loadStats();
    document.getElementById('gAmt').value=''; }catch(e){ msg.innerHTML='<span class=red>'+e.message+'</span>'; } }
async function toggle(id,status){ if(!confirm('确认将用户 '+id+' 设为 '+status+'？'))return;
  await api('/v1/admin/users/'+id+'/status',{method:'POST',body:JSON.stringify({status})}); loadUsers(); }
async function ledger(id){ const d=await api('/v1/admin/users/'+id+'/ledger');
  alert('最近流水:\\n'+(d.entries.map(e=>`${(e.created_at||'').slice(0,19)}  ${e.delta>0?'+':''}${e.delta}  (${e.reason})  余额${e.balance_after}`).join('\\n')||'无')); }
if(TOK) show();
</script></div></body></html>"""
