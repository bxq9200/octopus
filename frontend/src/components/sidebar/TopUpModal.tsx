import { useEffect, useState } from 'react'
import { X, Gem, Loader2, CheckCircle2, ExternalLink } from 'lucide-react'
import { billingAPI } from '@/services/api'

type Tier = { id: string; yuan: number; credits: number; bonus: number; label: string }

/** 充值弹窗：选档位 → 下单 → 系统浏览器打开支付宝收银台 → 轮询订单到账 → 刷新余额。
 *  到账以云端异步通知为准，这里只是轮询看结果。 */
export default function TopUpModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [tiers, setTiers] = useState<Tier[]>([])
  const [enabled, setEnabled] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)      // 正在下单的 tier id
  const [waiting, setWaiting] = useState<{ orderId: string; yuan: number } | null>(null)
  const [done, setDone] = useState<number | null>(null)      // 到账 credits
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setDone(null); setWaiting(null); setErr(null)
    billingAPI.payTiers()
      .then((r) => { setTiers(r.data?.tiers || []); setEnabled(!!r.data?.enabled) })
      .catch(() => setErr('拉取充值档位失败'))
  }, [open])

  // 轮询订单：支付宝异步通知到账后 status 变 paid
  useEffect(() => {
    if (!waiting) return
    let stop = false
    const t = setInterval(async () => {
      try {
        const d = (await billingAPI.payOrder(waiting.orderId)).data || {}
        if (d.status === 'paid' && !stop) {
          stop = true; clearInterval(t)
          setDone(d.credits || 0); setWaiting(null)
          window.dispatchEvent(new Event('octopus:credits-refresh'))
        }
      } catch { /* 轮询失败不打断，下一轮再试 */ }
    }, 3000)
    return () => { stop = true; clearInterval(t) }
  }, [waiting])

  if (!open) return null

  const buy = async (t: Tier) => {
    setBusy(t.id); setErr(null)
    try {
      const d = (await billingAPI.payCreate(t.id)).data || {}
      if (!d.pay_url) throw new Error('未拿到支付链接')
      window.open(d.pay_url, '_blank')          // 系统浏览器打开支付宝收银台
      setWaiting({ orderId: d.order_id, yuan: t.yuan })
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || '下单失败')
    }
    setBusy(null)
  }

  return (
    <div className="fixed inset-0 z-[9998] flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={onClose}>
      <div className="w-[min(94vw,520px)] rounded-2xl border border-octopus-border bg-[#0d0d14] shadow-2xl"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-octopus-border">
          <div className="flex items-center gap-2">
            <Gem className="w-4 h-4 text-cyan-400" />
            <span className="font-semibold">充值 credits</span>
          </div>
          <button onClick={onClose} className="text-octopus-muted hover:text-octopus-text">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5">
          {done !== null ? (
            <div className="text-center py-8">
              <CheckCircle2 className="w-12 h-12 text-green-400 mx-auto mb-3" />
              <div className="text-lg font-semibold mb-1">充值成功</div>
              <div className="text-octopus-muted text-sm">
                已到账 <span className="text-cyan-400">{done.toLocaleString()}</span> credits
              </div>
              <button onClick={onClose}
                className="mt-6 px-6 py-2 rounded-lg bg-cyan-500/20 border border-cyan-500/40 text-cyan-300 text-sm hover:bg-cyan-500/30">
                完成
              </button>
            </div>
          ) : waiting ? (
            <div className="text-center py-8">
              <Loader2 className="w-10 h-10 text-cyan-400 mx-auto mb-3 animate-spin" />
              <div className="font-semibold mb-1">等待支付完成…</div>
              <div className="text-octopus-muted text-sm leading-relaxed">
                已在浏览器打开支付宝收银台，付款 ¥{waiting.yuan} 后自动到账。<br />
                <span className="text-octopus-muted/70 text-[12px]">
                  付完这里会自动刷新，不用手动操作。
                </span>
              </div>
              <button onClick={() => setWaiting(null)}
                className="mt-5 text-[12px] text-octopus-muted hover:text-octopus-text underline">
                取消等待
              </button>
            </div>
          ) : !enabled ? (
            <div className="text-center py-8 text-octopus-muted text-sm leading-relaxed">
              在线充值暂未开放。<br />请联系客服人工充值。
            </div>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-3">
                {tiers.map((t) => (
                  <button key={t.id} onClick={() => buy(t)} disabled={!!busy}
                    className="relative text-left p-4 rounded-xl border border-octopus-border bg-black/30
                               hover:border-cyan-400/60 hover:bg-cyan-400/5 transition-colors disabled:opacity-50">
                    {t.bonus > 0 && (
                      <span className="absolute top-2 right-2 text-[10px] px-1.5 py-0.5 rounded
                                       bg-amber-500/20 text-amber-300 border border-amber-500/30">
                        送 {Math.round((t.bonus / (t.credits - t.bonus)) * 100)}%
                      </span>
                    )}
                    <div className="text-xl font-bold text-octopus-text">¥{t.yuan}</div>
                    <div className="text-[12px] text-cyan-400 mt-1">
                      {t.credits.toLocaleString()} credits
                    </div>
                    <div className="text-[11px] text-octopus-muted mt-0.5">
                      {t.label} · 约 {Math.round(t.credits / 80)} 秒成片
                    </div>
                    {busy === t.id && (
                      <Loader2 className="w-4 h-4 animate-spin text-cyan-400 absolute bottom-3 right-3" />
                    )}
                  </button>
                ))}
              </div>
              <div className="mt-4 text-[11px] text-octopus-muted leading-relaxed">
                <div className="flex items-center gap-1">
                  <ExternalLink className="w-3 h-3" /> 点击档位会在浏览器打开支付宝收银台
                </div>
                <div className="mt-1">credits 用于视频生成（约 0.8 元/秒）；大模型和图片用你自己的 API Key，不消耗 credits。</div>
              </div>
            </>
          )}
          {err && <div className="mt-3 text-[12px] text-red-400">{err}</div>}
        </div>
      </div>
    </div>
  )
}
