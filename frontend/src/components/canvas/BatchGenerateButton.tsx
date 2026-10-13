/**
 * 0.7.1 专家(分集)模式：分镜确认后 keyframe/video 节点停在 IDLE，由此按钮批量触发生成。
 *
 * 显示规则：
 *  - currentProject 是分集项目（is_episode + series_id）
 *  - 存在 storyboard_design 已 confirmed
 *  - 存在 ≥1 个待生成（非 running/completed/confirmed/error）的 keyframe/video 节点
 * 简易/进阶模式不显示（它们全自动生成，无需手动触发）。
 */
import React, { useState } from 'react'
import { Clapperboard, Loader2, AlertCircle, Palette } from 'lucide-react'
import { useStore } from '@/store/useStore'
import { workflowAPI } from '@/services/api'

const PENDING = (s: any) =>
  !['running', 'completed', 'confirmed', 'error'].includes(String(s || '').toLowerCase())

export default function BatchGenerateButton() {
  const currentProject = useStore((s) => s.currentProject)
  const dag = useStore((s) => s.dag)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 0.7.1：系列项目（专家总项目/分集，有 series_id）或独立进阶(advanced)项目都显示手动批量按钮
  const isSeries = !!currentProject?.series_id
  const isAdvanced = currentProject?.config?.mode === 'advanced'
  if (!currentProject || (!isSeries && !isAdvanced)) return null

  const list: any[] = (dag?.nodes as any[]) || []
  // 美术子图（专家模式建成 IDLE）：本集 phase=episode_art / result_type ep_*；总项目 phase=art + result_type character/scene/prop
  const isArt = (n: any) =>
    n?.phase === 'episode_art' ||
    String(n?.result?.result_type || '').startsWith('ep_') ||
    (n?.phase === 'art' && ['character', 'scene', 'prop'].includes(String(n?.result?.result_type || '')))
  // art 分支只对系列项目：进阶美术维持自动、不在此手动触发（避免误命中独立进阶的美术节点）
  const pendingArt = isSeries ? list.filter((n) => isArt(n) && (PENDING(n?.status) || n?.result?.mock)) : []
  const sbConfirmed = list.some(
    (n) => n?.type === 'storyboard_design' && String(n?.status || '').toLowerCase() === 'confirmed',
  )
  const pendingShots = list.filter(
    (n) => (n?.type === 'keyframe_generation' || n?.type === 'video_generation') && PENDING(n?.status),
  )

  // 本集美术(早) 优先于 分镜镜头(晚)；都没有就不显示
  const mode: 'art' | 'shots' | null =
    pendingArt.length > 0 ? 'art' : (sbConfirmed && pendingShots.length > 0 ? 'shots' : null)
  if (!mode) return null
  const count = mode === 'art' ? pendingArt.length : pendingShots.length
  const artNoun = currentProject.is_episode ? '本集美术' : '总项目美术'

  const handleClick = async () => {
    if (submitting) return
    const ok = confirm(
      mode === 'art'
        ? `将批量生成${artNoun}的 ${count} 张子图（角色/场景/道具${currentProject.is_episode ? '，在总美术基础上细化' : ''}）。继续？`
        : `将批量生成 ${count} 个镜头的关键帧/视频（走当前视频服务，可先切本地 ComfyUI 省算力）。\n连贯链会按顺序生成。继续？`,
    )
    if (!ok) return
    setSubmitting(true)
    setError(null)
    try {
      await workflowAPI.batchAction(currentProject.id, mode === 'art' ? 'art' : 'all')
      // 进度通过 WebSocket 实时推送
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || '批量生成启动失败')
    } finally {
      setSubmitting(false)
    }
  }

  const label = mode === 'art' ? `🎨 生成${artNoun} ${count}` : `🎬 开始生成 ${count} 镜头`
  const cls = mode === 'art'
    ? 'from-pink-500 to-fuchsia-500 hover:from-pink-400 hover:to-fuchsia-400 shadow-[0_0_20px_rgba(236,72,153,0.5)] hover:shadow-[0_0_28px_rgba(236,72,153,0.7)]'
    : 'from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 shadow-[0_0_20px_rgba(52,211,153,0.5)] hover:shadow-[0_0_28px_rgba(52,211,153,0.7)]'

  return (
    <div className="pointer-events-auto absolute top-3 right-3 z-10 flex flex-col items-end gap-1">
      <button
        onClick={handleClick}
        disabled={submitting}
        className={`flex items-center gap-2 px-4 py-2 rounded-full text-sm font-semibold transition-all bg-gradient-to-r text-white disabled:opacity-60 disabled:cursor-not-allowed disabled:shadow-none ${cls}`}
        title={label}
      >
        {submitting ? <Loader2 className="w-4 h-4 animate-spin" />
          : mode === 'art' ? <Palette className="w-4 h-4" /> : <Clapperboard className="w-4 h-4" />}
        {submitting ? '启动中...' : label}
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
