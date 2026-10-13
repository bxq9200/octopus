import { useEffect, useState, useCallback } from 'react'
import { Gem, RefreshCw, Plus } from 'lucide-react'
import { billingAPI } from '@/services/api'
import TopUpModal from './TopUpModal'

/** 显眼余额徽章：云端模式显示 credits（1 credit=1分，÷100=元）+ 低额预警；本地模式不显示。
 *  监听 window 'octopus:credits-refresh'（视频出片后 websocket 会派发）自动刷新。 */
export default function CreditsBadge({ collapsed = false }: { collapsed?: boolean }) {
  const [credits, setCredits] = useState<number | null>(null)
  const [unlimited, setUnlimited] = useState<boolean | null>(null)   // null=未知（还没拉到）
  const [spinning, setSpinning] = useState(false)
  const [topUp, setTopUp] = useState(false)

  const refresh = useCallback(async () => {
    setSpinning(true)
    try {
      const d = (await billingAPI.balance()).data || {}
      if (d.unlimited) { setUnlimited(true); setCredits(null) }
      else { setUnlimited(false); setCredits(typeof d.credits === 'number' ? d.credits : null) }
    } catch { /* 拉不到就保持原样 */ }
    setSpinning(false)
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 45000)
    const onEvt = () => refresh()
    const onOpenTopUp = () => setTopUp(true)      // 余额不足横幅的「去充值」按钮
    window.addEventListener('focus', onEvt)
    window.addEventListener('octopus:credits-refresh', onEvt)
    window.addEventListener('octopus:open-topup', onOpenTopUp)
    return () => {
      clearInterval(t)
      window.removeEventListener('focus', onEvt)
      window.removeEventListener('octopus:credits-refresh', onEvt)
      window.removeEventListener('octopus:open-topup', onOpenTopUp)
    }
  }, [refresh])

  // 本地模式 / 还没拉到：不占位
  if (unlimited || unlimited === null) return null

  const yuan = credits != null ? credits / 100 : null
  const low = credits != null && credits < 500          // < ¥5 预警
  const empty = credits != null && credits <= 0

  if (collapsed) {
    return (
      <>
        <button onClick={() => setTopUp(true)}
          title={credits != null ? `余额 ${credits} credits ≈ ¥${yuan?.toFixed(0)}（点击充值）` : '充值'}
          className={`w-full flex flex-col items-center py-1 hover:opacity-80 ${
            empty ? 'text-red-400' : low ? 'text-amber-400' : 'text-cyan-400'}`}>
          <Gem className="w-4 h-4" />
          <span className="text-[9px] mt-0.5">{yuan != null ? `¥${yuan.toFixed(0)}` : '—'}</span>
        </button>
        <TopUpModal open={topUp} onClose={() => { setTopUp(false); refresh() }} />
      </>
    )
  }

  return (
    <>
    <div className={`mx-2 mt-2 px-2.5 py-1.5 rounded-lg border flex items-center gap-2 text-[12px] ${
      empty ? 'border-red-500/50 bg-red-500/10'
        : low ? 'border-amber-500/40 bg-amber-500/10'
        : 'border-octopus-border bg-black/20'}`}>
      <Gem className={`w-3.5 h-3.5 shrink-0 ${empty ? 'text-red-400' : low ? 'text-amber-400' : 'text-cyan-400'}`} />
      <div className="flex-1 min-w-0">
        <span className={empty ? 'text-red-300' : 'text-octopus-text'}>
          {credits != null ? `${credits.toLocaleString()}` : '—'}
        </span>
        <span className="text-octopus-muted"> credits</span>
        {yuan != null && <span className="text-octopus-muted"> · ≈¥{yuan.toFixed(0)}</span>}
        {empty && <div className="text-[10px] text-red-400">余额用尽，请充值</div>}
        {low && !empty && <div className="text-[10px] text-amber-400">余额不足</div>}
      </div>
      <button onClick={() => setTopUp(true)} title="充值"
        className={`shrink-0 px-1.5 py-0.5 rounded border text-[11px] flex items-center gap-0.5 transition-colors ${
          empty || low ? 'border-amber-500/50 text-amber-300 hover:bg-amber-500/15'
                       : 'border-octopus-border text-octopus-muted hover:text-cyan-300 hover:border-cyan-400/50'}`}>
        <Plus className="w-2.5 h-2.5" />充值
      </button>
      <button onClick={refresh} title="刷新余额"
        className="shrink-0 text-octopus-muted hover:text-octopus-text">
        <RefreshCw className={`w-3 h-3 ${spinning ? 'animate-spin' : ''}`} />
      </button>
    </div>
    <TopUpModal open={topUp} onClose={() => { setTopUp(false); refresh() }} />
    </>
  )
}
