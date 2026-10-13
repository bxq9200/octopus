/**
 * 0.7.0：Canvas 顶部 breadcrumb
 *
 * 显示规则：
 * - 老项目（is_episode 未定义 / series_id 空）→ 不显示
 * - Series 总项目（is_episode=false，series_id 非空）→ 📺 剧名 · 总项目
 * - Episode（is_episode=true）→ 📺 剧名 › 第 N 集 · 名 ＋ [← 返回总项目] 按钮
 */
import React from 'react'
import { ChevronLeft, Tv, Film } from 'lucide-react'
import { useStore } from '@/store/useStore'
import { seriesAPI, projectAPI } from '@/services/api'

export default function CanvasBreadcrumb() {
  const currentProject = useStore((s) => s.currentProject)
  const currentSeries = useStore((s) => s.currentSeries)
  const setCurrentSeries = useStore((s) => s.setCurrentSeries)
  const setCurrentProject = useStore((s) => s.setCurrentProject)
  const resetWorkflow = useStore((s) => s.resetWorkflow)
  const hydrateWorkflow = useStore((s) => s.hydrateWorkflow)

  if (!currentProject) return null
  // 老项目（独立 simple/advanced）— series_id null && !is_episode
  const seriesId = currentProject.series_id
  if (!seriesId) return null

  const isMaster = !currentProject.is_episode
  const isEpisode = !!currentProject.is_episode
  const seriesName = currentSeries?.id === seriesId ? currentSeries.name : ''

  const backToMaster = async () => {
    if (!currentSeries?.master_project_id) {
      // 没缓存 → 兜底拉一遍 series 详情
      try {
        const res = await seriesAPI.get(seriesId)
        setCurrentSeries(res.data)
        const masterId = res.data.master_project_id
        if (masterId) await switchTo(masterId)
      } catch {}
      return
    }
    await switchTo(currentSeries.master_project_id)
  }

  const switchTo = async (pid: string) => {
    if (currentProject.id === pid) return
    resetWorkflow()
    setCurrentProject(null)
    try {
      const detail = await projectAPI.get(pid)
      setCurrentProject(detail.data)
      const wf = await projectAPI.getWorkflow(pid)
      hydrateWorkflow(wf.data)
    } catch (err) {
      console.warn('返回总项目失败', err)
    }
  }

  return (
    <div className="pointer-events-auto absolute top-3 left-1/2 -translate-x-1/2 z-10 max-w-[80%] flex items-center gap-2 px-3 py-1.5 rounded-full bg-octopus-panel/90 backdrop-blur border border-octopus-border shadow-lg text-xs">
      <Tv className="w-3.5 h-3.5 text-octopus-brand flex-shrink-0" />
      <span className="text-octopus-menu font-medium truncate max-w-[200px]">
        {seriesName || '（加载中...）'}
      </span>
      <span className="text-octopus-muted">›</span>
      {isMaster && (
        <span className="flex items-center gap-1 text-amber-400">
          <Film className="w-3 h-3" />
          总项目（筹划）
        </span>
      )}
      {isEpisode && (
        <>
          <span className="text-octopus-text whitespace-nowrap">
            {typeof currentProject.episode_index === 'number' ? `第${currentProject.episode_index}集 · ` : ''}
            <span className="truncate max-w-[160px] inline-block align-middle">{currentProject.name}</span>
          </span>
          <button
            onClick={backToMaster}
            className="ml-1 px-2 py-0.5 rounded bg-octopus-bg/60 hover:bg-octopus-bg text-octopus-muted hover:text-octopus-text flex items-center gap-1 transition-colors"
            title="返回总项目"
          >
            <ChevronLeft className="w-3 h-3" />
            总项目
          </button>
        </>
      )}
    </div>
  )
}
