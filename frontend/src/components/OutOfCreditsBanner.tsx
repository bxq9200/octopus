import { useEffect, useState } from 'react'
import { AlertCircle, X } from 'lucide-react'

/** 余额不足横幅：监听 websocket 派发的 'octopus:out-of-credits'（视频/关键帧因 credits 不足降级 mock）
 *  → 顶部醒目提示。8 秒后自动消失，也可手动关。 */
export default function OutOfCreditsBanner() {
  const [msg, setMsg] = useState<string | null>(null)

  useEffect(() => {
    let timer: number | undefined
    const on = (e: any) => {
      setMsg(e?.detail?.message || 'credits 余额不足，请充值后重试')
      if (timer) window.clearTimeout(timer)
      timer = window.setTimeout(() => setMsg(null), 8000)
    }
    window.addEventListener('octopus:out-of-credits', on)
    return () => { window.removeEventListener('octopus:out-of-credits', on); if (timer) window.clearTimeout(timer) }
  }, [])

  if (!msg) return null
  return (
    <div className="fixed top-4 left-1/2 -translate-x-1/2 z-[9999] w-[min(92vw,560px)]">
      <div className="flex items-start gap-2.5 px-4 py-3 rounded-xl border border-red-500/50 bg-red-950/95 backdrop-blur text-red-100 shadow-[0_8px_30px_rgba(0,0,0,0.5)]">
        <AlertCircle className="w-5 h-5 mt-0.5 shrink-0 text-red-400" />
        <div className="flex-1 min-w-0">
          <div className="font-semibold text-red-200 text-sm">余额不足，视频已降级为占位</div>
          <div className="text-[12px] text-red-200/75 mt-1 break-words">{msg.slice(0, 180)}</div>
          <div className="text-[11px] text-red-200/55 mt-1.5">充值后在镜头卡点「重跑」重新生成即可。</div>
          <button
            onClick={() => { setMsg(null); window.dispatchEvent(new Event('octopus:open-topup')) }}
            className="mt-2.5 px-3 py-1 rounded-lg bg-red-500/25 border border-red-400/40 text-red-100 text-[12px] hover:bg-red-500/35 transition-colors">
            去充值
          </button>
        </div>
        <button onClick={() => setMsg(null)} title="关闭"
          className="shrink-0 text-red-300/50 hover:text-red-200 transition-colors">
          <X className="w-4 h-4" />
        </button>
      </div>
    </div>
  )
}
