/**
 * 0.7.0：总项目 canvas 右上角浮按钮 — 资产冻结后亮起，点了 fan-out 出 N 集子项目
 *
 * 显示规则：
 *  - currentProject 必须是 master（series_id 非空 + is_episode=false）
 *  - currentSeries.shared_assets_frozen === true
 *  - 还没 fan-out 过（episodes 数为 0）
 */
import React, { useEffect, useState } from 'react'
import { Film, Loader2, AlertCircle } from 'lucide-react'
import { useStore } from '@/store/useStore'
import { seriesAPI } from '@/services/api'

export default function FanoutEpisodesButton() {
  const currentProject = useStore((s) => s.currentProject)
  const currentSeries = useStore((s) => s.currentSeries)
  const setCurrentSeries = useStore((s) => s.setCurrentSeries)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 收到 series_episodes_created WS 后刷新 currentSeries（拿到新 episodes 列表）
  useEffect(() => {
    if (!currentSeries?.id) return
    const onCreated = async () => {
      try {
        const res = await seriesAPI.get(currentSeries.id)
        setCurrentSeries(res.data)
      } catch {}
    }
    // 复用全局自定义事件（websocket.ts 收到 series_episodes_created 时也可派发）
    window.addEventListener('series_episodes_created', onCreated)
    return () => window.removeEventListener('series_episodes_created', onCreated)
  }, [currentSeries?.id, setCurrentSeries])

  if (!currentProject || !currentSeries) return null
  if (currentProject.is_episode) return null  // Episode 项目不显示
  if (!currentProject.series_id) return null  // 老独立项目不显示
  if (currentSeries.id !== currentProject.series_id) return null  // 不匹配
  if (!currentSeries.shared_assets_frozen) return null  // 美术还没全 confirm

  const epCount = currentSeries.episodes_breakdown?.length || 0
  const existingEpisodes = currentSeries.episodes?.length || 0
  const alreadyFanout = existingEpisodes > 0

  if (epCount === 0) return null

  const handleClick = async () => {
    if (submitting) return
    if (alreadyFanout) {
      const ok = confirm(
        `已存在 ${existingEpisodes} 集子项目。\n\n` +
        `点确认将：\n` +
        `  1. 清掉旧的 ${existingEpisodes} 集（保留 master + 视觉开发 + 分集草案）\n` +
        `  2. 用最新架构重新创建 ${epCount} 集\n\n` +
        `（适合：架构升级、之前 fanout 出问题、想重做的场景）`
      )
      if (!ok) return
    } else {
      const ok = confirm(`将创建 ${epCount} 集子项目（每集脚本独立创作，美术在 master 基础上细化）。继续？`)
      if (!ok) return
    }
    setSubmitting(true)
    setError(null)
    try {
      // 已有 episode → 先 reset 再 fanout
      if (alreadyFanout) {
        await seriesAPI.resetEpisodes(currentSeries.id)
      }
      const res = await seriesAPI.fanoutEpisodes(currentSeries.id)
      const data = res.data
      // 拉一次 series 详情同步 episodes
      try {
        const detailRes = await seriesAPI.get(currentSeries.id)
        setCurrentSeries(detailRes.data)
      } catch {}
      // 同时派发自定义事件，sidebar 可监听
      window.dispatchEvent(new CustomEvent('series_episodes_created', { detail: data }))
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || '创建子项目失败'
      setError(detail)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="pointer-events-auto absolute top-3 right-3 z-10 flex flex-col items-end gap-1">
      <button
        onClick={handleClick}
        disabled={submitting}
        className={`flex items-center gap-2 px-4 py-2 rounded-full text-sm font-semibold transition-all ${
          alreadyFanout
            ? 'bg-gradient-to-r from-amber-500 to-orange-500 hover:from-amber-400 hover:to-orange-400 text-white shadow-[0_0_20px_rgba(245,158,11,0.5)]'
            : 'bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 text-white shadow-[0_0_20px_rgba(52,211,153,0.5)]'
        } disabled:opacity-60 disabled:cursor-not-allowed disabled:shadow-none`}
        title={alreadyFanout ? '已 fan-out 过；再点会报错' : `创建 ${epCount} 集子项目`}
      >
        {submitting ? (
          <Loader2 className="w-4 h-4 animate-spin" />
        ) : (
          <Film className="w-4 h-4" />
        )}
        {submitting
          ? (alreadyFanout ? '清空旧版 + 重新创建中...' : '创建中...')
          : alreadyFanout
            ? `🔄 重做 ${existingEpisodes} 集`
            : `🎬 开始制作 ${epCount} 集`}
      </button>
      {error && (
        <div className="flex items-center gap-1 px-3 py-1.5 rounded-md bg-red-600/20 border border-red-500/40 text-red-200 text-xs max-w-[300px]">
          <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
          <span className="truncate" title={error}>{error}</span>
        </div>
      )}
    </div>
  )
}
