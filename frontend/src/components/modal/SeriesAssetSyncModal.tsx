/**
 * 0.7.0 Step 11：总项目改了 art 重新 confirm → ws series_assets_changed → 弹此对话框
 *
 * 三选项：
 *  - 同步到所有未开工的子集（默认，安全）
 *  - 同步到所有子集（含已开工，慎用）
 *  - 不同步（仅留新版，下次 fan-out 用）
 */
import React, { useEffect, useState } from 'react'
import { X, Loader2, AlertTriangle, Tv } from 'lucide-react'
import { useStore } from '@/store/useStore'
import { seriesAPI } from '@/services/api'

type SyncTarget = 'all_unstarted' | 'all' | 'none'

interface SyncEvent {
  series_id: string
  characters_count?: number
  scenes_count?: number
  props_count?: number
  existing_episodes_count?: number
  message?: string
}

export default function SeriesAssetSyncModal() {
  const currentSeries = useStore((s) => s.currentSeries)
  const setCurrentSeries = useStore((s) => s.setCurrentSeries)
  const addMessage = useStore((s) => s.addMessage)
  const [event, setEvent] = useState<SyncEvent | null>(null)
  const [target, setTarget] = useState<SyncTarget>('all_unstarted')
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as SyncEvent
      setEvent(detail)
      setErr(null)
      setTarget('all_unstarted')
    }
    window.addEventListener('series_assets_changed', handler)
    return () => window.removeEventListener('series_assets_changed', handler)
  }, [])

  if (!event) return null

  const close = () => {
    setEvent(null)
    setErr(null)
  }

  const handleApply = async () => {
    setSubmitting(true)
    setErr(null)
    try {
      if (target === 'none') {
        // 不同步 — 只是关闭，新版 shared_assets 已保存在后端
        addMessage({
          id: `nosync_${Date.now()}`,
          role: 'agent',
          content: '已选择不同步：新美术只用于以后新建的子集',
          timestamp: Date.now(),
          type: 'text',
        })
        close()
        return
      }
      const res = await seriesAPI.syncAssets(event.series_id, { target: target as 'all_unstarted' | 'all' })
      const data = res.data
      addMessage({
        id: `sync_${Date.now()}`,
        role: 'agent',
        content: data?.message || `已同步 ${data?.updated?.length || 0} 集`,
        timestamp: Date.now(),
        type: 'text',
      })
      // 同步后刷新一下 series 详情（episode 列表 status 可能变）
      if (currentSeries?.id === event.series_id) {
        try {
          const det = await seriesAPI.get(event.series_id)
          setCurrentSeries(det.data)
        } catch {}
      }
      close()
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || '同步失败')
    } finally {
      setSubmitting(false)
    }
  }

  const epCount = event.existing_episodes_count ?? 0

  return (
    <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="w-full max-w-md bg-octopus-panel border border-octopus-border rounded-xl shadow-2xl overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-octopus-border">
          <div className="flex items-center gap-2 text-sm font-medium text-octopus-text">
            <AlertTriangle className="w-4 h-4 text-amber-400" />
            总项目美术已更新
          </div>
          <button onClick={close} className="text-octopus-muted hover:text-octopus-text">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-3">
          <div className="text-xs text-octopus-chat leading-snug">
            你刚刚改动了总项目的美术（角色 / 场景 / 道具）。当前有 <b className="text-octopus-text">{epCount}</b> 个子集，
            选择如何同步：
          </div>

          <div className="text-[10px] text-octopus-muted flex flex-wrap gap-3 pb-1 border-b border-octopus-border">
            <span>👥 {event.characters_count || 0} 角色</span>
            <span>🏞️ {event.scenes_count || 0} 场景</span>
            <span>🎁 {event.props_count || 0} 道具</span>
          </div>

          <label className="flex items-start gap-2 cursor-pointer text-xs">
            <input
              type="radio"
              checked={target === 'all_unstarted'}
              onChange={() => setTarget('all_unstarted')}
              className="mt-0.5"
            />
            <div>
              <div className="text-octopus-text font-medium">
                同步到所有<span className="text-green-400">未开工</span>的子集（默认 · 安全）
              </div>
              <div className="text-octopus-muted text-[10px]">
                跳过已经跑了 storyboard / video 的集，避免下游不一致
              </div>
            </div>
          </label>

          <label className="flex items-start gap-2 cursor-pointer text-xs">
            <input
              type="radio"
              checked={target === 'all'}
              onChange={() => setTarget('all')}
              className="mt-0.5"
            />
            <div>
              <div className="text-octopus-text font-medium">
                同步到<span className="text-amber-400">所有子集</span>（含已开工 · 慎用）
              </div>
              <div className="text-octopus-muted text-[10px]">
                所有子集 art 子节点会被新美术覆盖，下游 storyboard / 视频结果不会自动重跑
              </div>
            </div>
          </label>

          <label className="flex items-start gap-2 cursor-pointer text-xs">
            <input
              type="radio"
              checked={target === 'none'}
              onChange={() => setTarget('none')}
              className="mt-0.5"
            />
            <div>
              <div className="text-octopus-text font-medium">不同步</div>
              <div className="text-octopus-muted text-[10px]">
                新美术只用于以后新建的子集，已有子集保持旧版
              </div>
            </div>
          </label>

          {err && (
            <div className="text-xs text-red-300 bg-red-600/15 border border-red-500/40 rounded p-2">
              {err}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 px-4 py-3 border-t border-octopus-border bg-octopus-bg/40">
          <button
            onClick={close}
            disabled={submitting}
            className="px-3 py-1.5 rounded-md text-xs text-octopus-muted hover:text-octopus-text"
          >
            稍后再说
          </button>
          <button
            onClick={handleApply}
            disabled={submitting}
            className="px-3 py-1.5 rounded-md text-xs bg-octopus-brand text-white hover:opacity-90 disabled:opacity-50 flex items-center gap-1"
          >
            {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            应用
          </button>
        </div>
      </div>
    </div>
  )
}
