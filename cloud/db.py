"""云端数据层 —— 用户 / 验证码 / 限频。

SQLite（aiosqlite）起步：单机、几百到几千用户量级完全够，省一个 MySQL 进程的内存。
真到需要多实例了再换 Postgres —— SQLAlchemy 这层不用动。

设计要点：
- **验证码只存哈希**，不存明文。库被读到也拿不到能用的码。
- 邮箱统一小写存，避免 `A@x.com` / `a@x.com` 注册出两个号。
"""

import hashlib
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, Integer, DateTime, Text, select, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    plan: Mapped[str] = mapped_column(String(32), default="free")
    status: Mapped[str] = mapped_column(String(32), default="active")   # active | disabled
    credits: Mapped[int] = mapped_column(Integer, default=0)            # 视频 credits 余额
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailCode(Base):
    """邮箱验证码。同一邮箱同时只保留最新一条（发新码即覆盖旧的）。"""
    __tablename__ = "email_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))      # sha256(code + email)，不存明文
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SendLog(Base):
    """发信流水 —— 限频用（按邮箱/按 IP）。每封信都是钱，必须限。"""
    __tablename__ = "send_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    ip: Mapped[str] = mapped_column(String(64), index=True, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class UsageRecord(Base):
    """计量流水（缝6 上报落这里）。Phase 3 接 credits 扣费时用。"""
    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(32), default="")
    seconds: Mapped[int] = mapped_column(Integer, default=0)
    resolution: Mapped[str] = mapped_column(String(32), default="")
    credits_charged: Mapped[int] = mapped_column(Integer, default=0)
    raw: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class VideoTask(Base):
    """视频中转任务。客户端提交 → 云端用企业 key 转发厂商 → 客户端凭 task_id 轮询。

    **字节不过服务器**：这里只存厂商回的远程 URL（origin_url），客户端直连下载。
    """
    __tablename__ = "video_tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)       # uuid hex
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")   # queued|running|succeeded|failed
    kind: Mapped[str] = mapped_column(String(8), default="i2v")         # t2v|i2v
    params: Mapped[str] = mapped_column(Text, default="")              # 提交参数 JSON（不含企业 key）
    remote_url: Mapped[str] = mapped_column(Text, default="")          # 厂商远程视频 URL（客户端下载用）
    seconds: Mapped[int] = mapped_column(Integer, default=0)
    resolution: Mapped[str] = mapped_column(String(16), default="720p")
    est_credits: Mapped[int] = mapped_column(Integer, default=0)        # 提交时估算（用于查余额）
    credits_charged: Mapped[int] = mapped_column(Integer, default=0)    # 实扣（成功才扣；>0 = 已扣，幂等哨兵）
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CreditLedger(Base):
    """credits 流水（append-only）—— 充值为正、扣费为负。user.credits 是它的缓存余额。
    对账时以此表求和为准。"""
    __tablename__ = "credit_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    delta: Mapped[int] = mapped_column(Integer)                         # +充值 / -扣费
    balance_after: Mapped[int] = mapped_column(Integer)                 # 记账后余额（对账用）
    reason: Mapped[str] = mapped_column(String(64), default="")         # topup | video | refund | grant
    ref: Mapped[str] = mapped_column(String(64), default="")            # 关联 id（如 video_task.id），扣费幂等键
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Order(Base):
    """充值订单（支付宝电脑网站支付）。

    幂等靠两层：①订单自身 status 只从 pending → paid 单向流转
    ②credits 入账走 ledger 的 (reason=topup, ref=order.id) 哨兵 —— 支付宝会重复推送
    异步通知直到收到 success，重复推送必须不重复到账。
    """
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)        # 商户订单号 out_trade_no
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    amount_fen: Mapped[int] = mapped_column(Integer)                     # 实付金额（分）
    credits: Mapped[int] = mapped_column(Integer)                        # 到账 credits（含赠送）
    bonus: Mapped[int] = mapped_column(Integer, default=0)               # 其中赠送部分
    tier: Mapped[str] = mapped_column(String(32), default="")            # 档位标识
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|paid|closed
    trade_no: Mapped[str] = mapped_column(String(64), default="")        # 支付宝交易号
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


async def settle_order(session: AsyncSession, order: "Order", trade_no: str = "") -> bool:
    """订单支付成功 → 加 credits。**幂等**：ledger (reason=topup, ref=order.id) 哨兵防重复到账
    （支付宝会重复推送异步通知，必须扛得住）。首次到账返回 True，重复调用返回 False。"""
    dup = await session.execute(
        select(CreditLedger.id).where(CreditLedger.reason == "topup",
                                      CreditLedger.ref == order.id))
    if dup.scalar_one_or_none() is not None:
        return False
    r = await session.execute(select(User).where(User.id == order.user_id))
    user = r.scalar_one_or_none()
    if not user:
        return False
    user.credits = int(user.credits or 0) + int(order.credits)
    order.status = "paid"
    order.trade_no = trade_no or order.trade_no
    order.paid_at = utcnow()
    session.add(CreditLedger(user_id=order.user_id, delta=int(order.credits),
                             balance_after=user.credits, reason="topup", ref=order.id))
    await session.commit()
    return True


# ── engine / session ──

os.makedirs(config.DATA_DIR, exist_ok=True)
engine = create_async_engine(config.DATABASE_URL, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with SessionLocal() as s:
        yield s


# ── 工具 ──

def norm_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_code(code: str, email: str) -> str:
    """验证码加盐哈希（盐用邮箱本身，够了——码只活 10 分钟且限次）。"""
    return hashlib.sha256(f"{code}:{norm_email(email)}".encode("utf-8")).hexdigest()


async def get_user_by_email(session: AsyncSession, email: str) -> Optional[User]:
    r = await session.execute(select(User).where(User.email == norm_email(email)))
    return r.scalar_one_or_none()


async def count_sends(session: AsyncSession, *, email: str = "", ip: str = "",
                      since: datetime) -> int:
    q = select(func.count(SendLog.id)).where(SendLog.created_at >= since)
    if email:
        q = q.where(SendLog.email == norm_email(email))
    if ip:
        q = q.where(SendLog.ip == ip)
    r = await session.execute(q)
    return int(r.scalar() or 0)


# ── credits 账本 ──

async def grant_credits(session: AsyncSession, user_id: int, amount: int,
                        reason: str = "grant", ref: str = "") -> int:
    """给用户加 credits（充值/赠送/退款）。返回新余额。写一条 ledger。"""
    r = await session.execute(select(User).where(User.id == user_id))
    user = r.scalar_one_or_none()
    if not user:
        raise ValueError(f"用户不存在: {user_id}")
    user.credits = int(user.credits or 0) + int(amount)
    session.add(CreditLedger(user_id=user_id, delta=int(amount),
                             balance_after=user.credits, reason=reason, ref=ref))
    await session.commit()
    return user.credits


async def charge_for_task(session: AsyncSession, task: "VideoTask", amount: int) -> bool:
    """为一个视频任务扣费 —— **幂等**：靠 ledger 的 (reason=video, ref=task.id) 唯一性防重复扣。

    成功扣费返回 True；已扣过（幂等命中）返回 False。余额可能被并发打到略负（MVP 容忍，
    真实系统应在提交时预留）。扣费与 task.credits_charged 落库在同一事务。
    """
    # 幂等哨兵：这个 task 已经扣过就不再扣
    dup = await session.execute(
        select(CreditLedger.id).where(CreditLedger.reason == "video",
                                      CreditLedger.ref == task.id))
    if dup.scalar_one_or_none() is not None:
        return False
    r = await session.execute(select(User).where(User.id == task.user_id))
    user = r.scalar_one_or_none()
    if not user:
        return False
    user.credits = int(user.credits or 0) - int(amount)
    task.credits_charged = int(amount)
    session.add(CreditLedger(user_id=task.user_id, delta=-int(amount),
                             balance_after=user.credits, reason="video", ref=task.id))
    session.add(UsageRecord(user_id=task.user_id, provider="octopus_cloud",
                            model="seedance", kind="video", seconds=task.seconds,
                            resolution=task.resolution, credits_charged=int(amount),
                            raw=task.params))
    await session.commit()
    return True
