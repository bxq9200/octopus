"""0.8 缝6：计量上报 hook。

`local`（默认）= **no-op，零开销**（一次布尔判断就返回）。
`cloud_client`  = 把**视频**用量排进上报队列 → 云端按秒扣 credits（0.9 填 flush）。

═══ 为什么这条几乎不用改代码 ═══

`interactive_executor._capture_usage_to_cost_log` 产出的 `usage` dict 本来就是
云端计量的完美 payload：

    {"provider", "model", "kind": "video", "seconds", "resolution"}

**仪表早就装好了，缺的只是账本。** 所以这里只是在它旁边挂一行 `meter.report(...)`，
不动既有的 cost_log 逻辑。

═══ 两条铁律 ═══

**铁律1：永不抛异常、永不阻塞。** 计量失败绝不能打断用户的生成 —— 那一刻钱已经花了、
片子已经出了，因为记账失败而让节点 FAILED 是纯粹的伤害。所有异常在这里咽掉。

**铁律2：只上报视频。** 视频是平台中转、按 credits 计费，必须上报；
LLM / 生图 / 音频是用户 BYOK（用户自己付钱给厂商），平台不该也没资格记账，
它们的 usage 继续只进本地 cost_log 供用户自己看成本。

═══ 0.9 要填的 ═══

`_flush()` —— 把队列 POST 到 `{gateway}/v1/usage/report`（带 Authorization）。
建议做成**后台定时 + 退避重试**，并把队列落盘，避免进程退出丢账。
0.8 只在内存里排队 + 计数，进程退出即丢（此时云端本来也还不存在）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 0.8：内存队列。0.9 换成落盘队列 + 后台 flush。
_PENDING: List[Dict[str, Any]] = []
_MAX_PENDING = 5000          # 上限保护：云端没上线时别把内存吃光
_dropped = 0
_warned = False

# 只有这些 kind 需要上报（平台中转、按 credits 计费）
_BILLABLE_KINDS = ("video",)


def is_enabled() -> bool:
    """是否需要上报。local 恒 False —— 一次判断就返回，零开销。"""
    try:
        from app.core.runtime_config import is_cloud_client
        return is_cloud_client()
    except Exception:
        return False


def _is_billable(usage: Dict[str, Any]) -> bool:
    """只有平台中转的视频才计入 credits；BYOK 的 LLM/图/音频不上报（铁律2）。"""
    if not isinstance(usage, dict):
        return False
    if str(usage.get("kind") or "").lower() not in _BILLABLE_KINDS:
        return False
    # mock 不计费（上游一般已过滤，这里再兜一道）
    if usage.get("mock"):
        return False
    return True


def report(
    usage: Optional[Dict[str, Any]],
    *,
    node_id: str = "",
    node_type: str = "",
    phase: str = "",
    project_id: str = "",
) -> None:
    """旁挂上报一笔用量。**永不抛异常、永不阻塞**（铁律1）。"""
    global _dropped, _warned
    try:
        if not usage or not is_enabled():
            return
        if not _is_billable(usage):
            return

        if len(_PENDING) >= _MAX_PENDING:
            _dropped += 1
            if _dropped == 1:
                logger.warning("meter: 上报队列已满(%d)，开始丢弃计量记录", _MAX_PENDING)
            return

        _PENDING.append({
            "project_id": project_id,
            "node_id": node_id,
            "node_type": node_type,
            "phase": phase,
            "usage": dict(usage),
        })

        if not _warned:
            _warned = True
            logger.warning(
                "meter: cloud_client 计量上报尚未实现（0.9 填 /v1/usage/report），"
                "记录先在内存排队；进程退出会丢。"
            )
    except Exception as e:                       # 铁律1：咽掉一切
        logger.debug("meter.report 忽略异常: %s", e)


def pending_count() -> int:
    return len(_PENDING)


def drain() -> List[Dict[str, Any]]:
    """取出并清空队列（0.9 的 flush 用；测试也用它断言）。"""
    items = list(_PENDING)
    _PENDING.clear()
    return items


def stats() -> Dict[str, Any]:
    return {"enabled": is_enabled(), "pending": len(_PENDING), "dropped": _dropped}


async def _flush() -> None:
    """0.9：POST {gateway}/v1/usage/report，带 Authorization: Bearer <identity.cloud_token()>。
    应做后台定时 + 失败退避重试 + 队列落盘。0.8 未实现。"""
    raise NotImplementedError("计量上报 flush 在 0.9 实现")
