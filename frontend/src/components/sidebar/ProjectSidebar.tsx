import React, { useEffect, useCallback } from 'react'
import {
  Folder, Plus, ChevronLeft, ChevronRight, Settings, X,
  ChevronDown, ChevronUp, Film, Tv, Play, Shirt,
} from 'lucide-react'
import { useStore } from '@/store/useStore'
import { projectAPI, seriesAPI, workflowAPI } from '@/services/api'
import CreditsBadge from './CreditsBadge'
import type { SeriesBrief, SeriesDetail, Project, SeriesStatus } from '@/types'


// 0.7.0：剧集 status → 简短中文徽章
const SERIES_STATUS_LABEL: Record<string, { text: string; color: string }> = {
  draft:      { text: '草稿',   color: 'bg-gray-500/30 text-gray-300' },
  breakdown:  { text: '协商中', color: 'bg-blue-500/30 text-blue-300' },
  visual_dev: { text: '美术中', color: 'bg-purple-500/30 text-purple-300' },
  producing:  { text: '制作中', color: 'bg-amber-500/30 text-amber-300' },
  done:       { text: '已完成', color: 'bg-green-500/30 text-green-300' },
}

// 0.7.0：episode 项目 phase → 徽章
const EPISODE_PHASE_LABEL: Record<string, { text: string; color: string }> = {
  initial:         { text: '待开工',   color: 'bg-gray-600/30 text-gray-400' },
  plan_confirmed:  { text: '待开工',   color: 'bg-gray-600/30 text-gray-400' },
  script:          { text: '剧本中',   color: 'bg-indigo-500/30 text-indigo-300' },
  art:             { text: '美术中',   color: 'bg-purple-500/30 text-purple-300' },
  storyboard:      { text: '分镜中',   color: 'bg-blue-500/30 text-blue-300' },
  keyframe:        { text: '关键帧中', color: 'bg-yellow-500/30 text-yellow-300' },
  video:           { text: '视频中',   color: 'bg-amber-500/30 text-amber-300' },
  audio:           { text: '音频中',   color: 'bg-teal-500/30 text-teal-300' },
  merge:           { text: '合并中',   color: 'bg-red-500/30 text-red-300' },
  publish:         { text: '待发布',   color: 'bg-pink-500/30 text-pink-300' },
  completed:       { text: '已完成',   color: 'bg-green-500/30 text-green-300' },
}


export default function ProjectSidebar() {
  const sidebarOpen = useStore((s) => s.sidebarOpen)
  const toggleSidebar = useStore((s) => s.toggleSidebar)
  const sidebarTab = useStore((s) => s.sidebarTab)
  const setSidebarTab = useStore((s) => s.setSidebarTab)
  const projects = useStore((s) => s.projects)
  const setProjects = useStore((s) => s.setProjects)
  const currentProject = useStore((s) => s.currentProject)
  const setCurrentProject = useStore((s) => s.setCurrentProject)
  const series = useStore((s) => s.series)
  const setSeries = useStore((s) => s.setSeries)
  const upsertSeries = useStore((s) => s.upsertSeries)
  const removeSeries = useStore((s) => s.removeSeries)
  const currentSeries = useStore((s) => s.currentSeries)
  const setCurrentSeries = useStore((s) => s.setCurrentSeries)
  const expandedSeriesIds = useStore((s) => s.expandedSeriesIds)
  const toggleSeriesExpanded = useStore((s) => s.toggleSeriesExpanded)
  const resetWorkflow = useStore((s) => s.resetWorkflow)
  const hydrateWorkflow = useStore((s) => s.hydrateWorkflow)
  const setSettingsOpen = useStore((s) => s.setSettingsOpen)
  const setAssetManagerSeriesId = useStore((s) => s.setAssetManagerSeriesId)
  const dag = useStore((s) => s.dag)
  // (b) 当前打开的项目里是否有节点在跑 → 给侧栏对应集加运行脉冲
  const currentRunning = (Array.isArray((dag as any)?.nodes)
    ? (dag as any).nodes
    : Object.values((dag as any)?.nodes || {})
  ).some((n: any) => String(n?.status).toLowerCase() === 'running')

  // 拉「项目」列表 — 0.7.0 后只取独立项目（series_id IS NULL 且非 episode）
  useEffect(() => {
    projectAPI.list({ series_id: 'null', is_episode: false })
      .then((res) => setProjects(res.data))
      .catch(() => {})
  }, [setProjects])

  // 拉「剧集」列表
  useEffect(() => {
    seriesAPI.list()
      .then((res) => setSeries(res.data))
      .catch(() => {})
  }, [setSeries])

  // 收到 series_episodes_created 自动刷新列表 + 自动展开该 series（拉详情看到 episodes）
  useEffect(() => {
    const onCreated = async (e: Event) => {
      const detail = (e as CustomEvent).detail || {}
      try {
        const listRes = await seriesAPI.list()
        setSeries(listRes.data)
      } catch {}
      if (detail.series_id) {
        if (!expandedSeriesIds.includes(detail.series_id)) {
          toggleSeriesExpanded(detail.series_id)
        }
        try {
          const detailRes = await seriesAPI.get(detail.series_id)
          setCurrentSeries(detailRes.data as SeriesDetail)
        } catch {}
      }
    }
    window.addEventListener('series_episodes_created', onCreated)
    return () => window.removeEventListener('series_episodes_created', onCreated)
  }, [setSeries, expandedSeriesIds, toggleSeriesExpanded, setCurrentSeries])

  // ─── 切项目（共用：master / episode / 独立项目）───
  const switchToProject = useCallback(async (pid: string, projForState?: Project | null) => {
    if (currentProject?.id === pid) return
    resetWorkflow()
    setCurrentProject(projForState || null)
    try {
      // 拿 project 详情补全 currentProject
      if (!projForState) {
        const detailRes = await projectAPI.get(pid)
        setCurrentProject(detailRes.data as Project)
      }
      const wf = await projectAPI.getWorkflow(pid)
      hydrateWorkflow(wf.data)
    } catch (err) {
      console.warn('加载项目工作流失败', err)
    }
  }, [currentProject, resetWorkflow, setCurrentProject, hydrateWorkflow])

  // ─── 项目 tab ───
  // 「+ 新对话」：不再预建空项目 —— 预建的 project.config 没有 mode，
  // 对话会被迫走简易 checklist（简易/进阶分流靠 config.mode）。
  // 改为清空当前选中、回到空白对话态；用户在对话框选好模式、发第一句话时，
  // 由 FloatingChat 的 handleSend 自动建项目并带上正确的 config.mode。
  const handleCreateProject = () => {
    resetWorkflow()
    setCurrentProject(null)
  }

  const handleDeleteProject = async (e: React.MouseEvent, projectId: string) => {
    e.stopPropagation()
    if (!confirm('确定要删除这个项目吗？')) return
    try {
      await projectAPI.delete(projectId)
      const updated = projects.filter((p) => p.id !== projectId)
      setProjects(updated)
      if (currentProject?.id === projectId) {
        resetWorkflow()
        setCurrentProject(null)
      }
    } catch {
      alert('删除失败')
    }
  }

  // ─── 剧集 tab ───
  const handleCreateSeries = async () => {
    const name = prompt('新剧集名称（可留空，会按第一条消息智能命名）:')
    // 用户取消（按 ESC / 点 Cancel）prompt 返回 null；空字符串 "" 表示用户确认空名
    if (name === null) return
    try {
      const res = await seriesAPI.create({ name: name || '' })
      const detail = res.data as SeriesDetail
      upsertSeries({
        id: detail.id,
        name: detail.name,
        description: detail.description,
        status: detail.status,
        master_project_id: detail.master_project_id,
        episode_count: 0,
        created_at: detail.created_at,
      })
      setCurrentSeries(detail)
      // 自动切到 master 项目
      if (detail.master_project_id) {
        await switchToProject(detail.master_project_id)
      }
    } catch (err: any) {
      alert(`创建剧集失败: ${err?.response?.data?.detail || err?.message || '未知错误'}`)
    }
  }

  const handleDeleteSeries = async (e: React.MouseEvent, sid: string, sname: string) => {
    e.stopPropagation()
    const epCount = series.find((x) => x.id === sid)?.episode_count ?? 0
    const warn = [
      `确定删除剧集「${sname}」？`,
      '',
      '将一并删除：',
      `  · 总项目（含全部美术图）`,
      `  · ${epCount} 个子集项目及其分镜 / 视频 / 音频`,
      `  · 上传的剧本文件 + 所有生成的图片（${epCount > 0 ? 'Episode 引用 master 图片，会一并失效' : ''}）`,
      '',
      '此操作不可恢复。',
    ].join('\n')
    if (!confirm(warn)) return
    try {
      await seriesAPI.delete(sid)
      removeSeries(sid)
      if (currentSeries?.id === sid) {
        setCurrentSeries(null)
        setCurrentProject(null)
        resetWorkflow()
      }
    } catch (err: any) {
      alert(`删除剧集失败：${err?.response?.data?.detail || err?.message || '未知错误'}`)
    }
  }

  const handleExpandSeries = async (e: React.MouseEvent, sid: string) => {
    e.stopPropagation()
    toggleSeriesExpanded(sid)
    // 首次展开 → 拉详情（含 episodes）
    if (!expandedSeriesIds.includes(sid)) {
      try {
        const res = await seriesAPI.get(sid)
        const detail = res.data as SeriesDetail
        if (currentSeries?.id === sid) {
          setCurrentSeries(detail)
        }
        // 顺手同步 list 里的 episode_count
        upsertSeries({
          id: detail.id,
          name: detail.name,
          description: detail.description,
          status: detail.status,
          master_project_id: detail.master_project_id,
          episode_count: detail.episodes?.length || 0,
          created_at: detail.created_at,
        })
      } catch (err) {
        console.warn('加载剧集详情失败', err)
      }
    }
  }

  const handleSelectMaster = async (s: SeriesBrief) => {
    if (!s.master_project_id) return
    // 拉一次最新详情同步到 currentSeries
    try {
      const res = await seriesAPI.get(s.id)
      setCurrentSeries(res.data as SeriesDetail)
    } catch {}
    await switchToProject(s.master_project_id)
  }

  const handleSelectEpisode = async (s: SeriesBrief, epId: string) => {
    try {
      const res = await seriesAPI.get(s.id)
      setCurrentSeries(res.data as SeriesDetail)
    } catch {}
    await switchToProject(epId)
  }

  // 0.7.0：点 ▶ → 切到这集 + 立即启动剧本生成（找该集 IDLE 的 script_generation 节点开跑）
  const handleStartEpisode = async (s: SeriesBrief, epId: string) => {
    await handleSelectEpisode(s, epId)
    try {
      await workflowAPI.startEpisodeScript(epId)
    } catch (err: any) {
      alert(`启动失败：${err?.response?.data?.detail || err?.message || '未知错误'}`)
    }
  }

  return (
    <div
      className={`h-full bg-octopus-panel border-r border-octopus-border flex flex-col transition-all duration-300 ${
        sidebarOpen ? 'w-64' : 'w-14'
      }`}
    >
      {/* Header */}
      <div className="h-14 flex items-center justify-between px-2 border-b border-octopus-border">
        {sidebarOpen ? (
          <>
            <div className="flex items-center gap-2 pl-1">
              <img src="/logo.jpg" alt="Octopus" className="w-6 h-6 rounded object-cover" />
              <span className="text-sm font-semibold text-octopus-brand">Octopus</span>
            </div>
            <button
              onClick={toggleSidebar}
              title="收起"
              className="w-6 h-6 flex items-center justify-center text-octopus-muted hover:text-octopus-text transition-colors"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
          </>
        ) : (
          <button
            onClick={toggleSidebar}
            title="展开"
            className="w-10 h-10 mx-auto flex items-center justify-center rounded-lg hover:bg-octopus-border transition-colors"
          >
            <img src="/logo.jpg" alt="Octopus" className="w-7 h-7 rounded object-cover" />
          </button>
        )}
      </div>
      {sidebarOpen ? <CreditsBadge /> : <div className="pt-1"><CreditsBadge collapsed /></div>}

      {/* 折叠态：图标栏 */}
      {!sidebarOpen && (
        <div className="flex-1 flex flex-col items-center gap-1 py-3">
          <button
            onClick={() => { setSidebarTab('projects'); toggleSidebar() }}
            title="项目"
            className="w-10 h-10 flex items-center justify-center rounded-lg text-octopus-muted hover:text-octopus-brand hover:bg-octopus-border transition-colors"
          >
            <Folder className="w-5 h-5" />
          </button>
          <button
            onClick={() => { setSidebarTab('series'); toggleSidebar() }}
            title="剧集"
            className="relative w-10 h-10 flex items-center justify-center rounded-lg text-octopus-muted hover:text-octopus-brand hover:bg-octopus-border transition-colors"
          >
            <Tv className="w-5 h-5" />
            {series.length > 0 && (
              <span className="absolute top-1.5 right-1.5 w-1.5 h-1.5 rounded-full bg-octopus-primary" />
            )}
          </button>
          <div className="flex-1" />
          <button
            onClick={() => setSettingsOpen(true)}
            title="系统配置"
            className="w-10 h-10 flex items-center justify-center rounded-lg text-octopus-menu hover:text-octopus-brand hover:bg-octopus-border transition-colors"
          >
            <Settings className="w-5 h-5" />
          </button>
        </div>
      )}

      {sidebarOpen && (
        <>
          {/* Tab bar */}
          <div className="flex border-b border-octopus-border">
            <button
              onClick={() => setSidebarTab('projects')}
              className={`flex-1 py-2 text-xs font-medium flex items-center justify-center gap-1.5 transition-colors ${
                sidebarTab === 'projects'
                  ? 'text-octopus-brand border-b-2 border-octopus-brand'
                  : 'text-octopus-muted hover:text-octopus-text'
              }`}
            >
              <Folder className="w-3.5 h-3.5" />
              项目
            </button>
            <button
              onClick={() => setSidebarTab('series')}
              className={`flex-1 py-2 text-xs font-medium flex items-center justify-center gap-1.5 transition-colors ${
                sidebarTab === 'series'
                  ? 'text-octopus-brand border-b-2 border-octopus-brand'
                  : 'text-octopus-muted hover:text-octopus-text'
              }`}
            >
              <Tv className="w-3.5 h-3.5" />
              剧集
              {series.length > 0 && (
                <span className="text-[10px] px-1 rounded bg-octopus-border text-octopus-muted">
                  {series.length}
                </span>
              )}
            </button>
          </div>

          {/* Tab content */}
          {sidebarTab === 'projects' ? (
            <ProjectsTab
              projects={projects}
              currentProjectId={currentProject?.id}
              onCreate={handleCreateProject}
              onSelect={(p) => switchToProject(p.id, p)}
              onDelete={handleDeleteProject}
            />
          ) : (
            <SeriesTab
              series={series}
              currentSeriesId={currentSeries?.id}
              currentProjectId={currentProject?.id}
              expandedIds={expandedSeriesIds}
              onCreate={handleCreateSeries}
              onExpand={handleExpandSeries}
              onSelectMaster={handleSelectMaster}
              onSelectEpisode={handleSelectEpisode}
              onAssets={(sid) => setAssetManagerSeriesId(sid)}
              onStartEpisode={handleStartEpisode}
              onDelete={handleDeleteSeries}
              currentSeriesDetail={currentSeries}
              currentRunning={currentRunning}
            />
          )}

          {/* Bottom settings */}
          <div className="p-2 border-t border-octopus-border">
            <button
              onClick={() => setSettingsOpen(true)}
              className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-octopus-menu hover:text-octopus-menu hover:bg-octopus-border transition-colors"
            >
              <Settings className="w-4 h-4" />
              系统配置
            </button>
          </div>
        </>
      )}
    </div>
  )
}


// ═══════════════════════════════════════════════════════
// 「项目」tab —— 只列独立项目
// ═══════════════════════════════════════════════════════
function ProjectsTab(props: {
  projects: Project[]
  currentProjectId?: string
  onCreate: () => void
  onSelect: (p: Project) => void
  onDelete: (e: React.MouseEvent, pid: string) => void
}) {
  return (
    <>
      <div className="p-3">
        <button
          onClick={props.onCreate}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-octopus-primary/10 hover:bg-octopus-primary/20 text-octopus-menu text-sm transition-colors"
        >
          <Plus className="w-4 h-4" />
          新对话
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 space-y-1">
        {props.projects.length === 0 && (
          <div className="px-3 py-6 text-center text-xs text-octopus-muted">
            还没有独立项目<br/>点上方按钮新建
          </div>
        )}
        {props.projects.map((p) => (
          <div
            key={p.id}
            onClick={() => props.onSelect(p)}
            className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors cursor-pointer group ${
              props.currentProjectId === p.id
                ? 'bg-octopus-primary/20 text-octopus-menu'
                : 'text-octopus-menu hover:bg-octopus-border'
            }`}
          >
            <Folder className="w-4 h-4 flex-shrink-0" />
            <span className="truncate flex-1 text-left">{p.name}</span>
            <button
              onClick={(e) => props.onDelete(e, p.id)}
              className="w-5 h-5 flex items-center justify-center rounded text-octopus-muted opacity-0 group-hover:opacity-100 hover:bg-red-500/20 hover:text-red-400 transition-all"
              title="删除项目"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        ))}
      </div>
    </>
  )
}


// ═══════════════════════════════════════════════════════
// 「剧集」tab —— 可展开的卡片列表
// ═══════════════════════════════════════════════════════
function SeriesTab(props: {
  series: SeriesBrief[]
  currentSeriesId?: string
  currentProjectId?: string
  expandedIds: string[]
  onCreate: () => void
  onExpand: (e: React.MouseEvent, sid: string) => void
  onSelectMaster: (s: SeriesBrief) => void
  onSelectEpisode: (s: SeriesBrief, epId: string) => void
  onAssets: (sid: string) => void
  onStartEpisode: (s: SeriesBrief, epId: string) => void
  onDelete: (e: React.MouseEvent, sid: string, sname: string) => void
  currentSeriesDetail: SeriesDetail | null
  currentRunning?: boolean
}) {
  return (
    <>
      <div className="p-3">
        <button
          onClick={props.onCreate}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-octopus-primary/10 hover:bg-octopus-primary/20 text-octopus-menu text-sm transition-colors"
        >
          <Plus className="w-4 h-4" />
          新建剧集
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 space-y-2">
        {props.series.length === 0 && (
          <div className="px-3 py-6 text-center text-xs text-octopus-muted">
            还没有剧集<br/>专家模式从这里开始<br/>（上传剧本 → AI 协商分集）
          </div>
        )}
        {props.series.map((s) => {
          const expanded = props.expandedIds.includes(s.id)
          const isActiveSeries = props.currentSeriesId === s.id
          const isActiveMaster = isActiveSeries && props.currentProjectId === s.master_project_id
          const statusBadge = SERIES_STATUS_LABEL[s.status as SeriesStatus] || { text: s.status, color: 'bg-gray-500/30 text-gray-300' }
          const episodes = (isActiveSeries ? props.currentSeriesDetail?.episodes : null) || []

          return (
            <div
              key={s.id}
              className={`rounded-lg border ${isActiveSeries ? 'border-octopus-brand/50' : 'border-octopus-border'} bg-octopus-panel overflow-hidden`}
            >
              {/* 头：series 标题 + 展开/删除 */}
              <div
                className={`flex items-center gap-2 px-3 py-2 text-sm cursor-pointer group transition-colors ${
                  isActiveSeries ? 'bg-octopus-primary/10' : 'hover:bg-octopus-border/40'
                }`}
                onClick={(e) => props.onExpand(e, s.id)}
              >
                <Tv className="w-4 h-4 flex-shrink-0 text-octopus-brand" />
                <span className="truncate flex-1 text-octopus-menu font-medium">{s.name}</span>
                <span className={`text-[10px] px-1.5 py-0.5 rounded ${statusBadge.color} whitespace-nowrap`}>
                  {statusBadge.text}
                </span>
                {(s.episode_count ?? 0) > 0 && (
                  <span className="text-[10px] px-1 rounded bg-octopus-border text-octopus-muted whitespace-nowrap">
                    {s.episode_count} 集
                  </span>
                )}
                {expanded ? (
                  <ChevronUp className="w-3.5 h-3.5 text-octopus-muted" />
                ) : (
                  <ChevronDown className="w-3.5 h-3.5 text-octopus-muted" />
                )}
                {/* 0.7.2：剧集资产管理（角色服装 / 道具）— 资产从属剧集 */}
                <button
                  onClick={(e) => { e.stopPropagation(); props.onAssets(s.id) }}
                  className="w-5 h-5 flex items-center justify-center rounded text-octopus-muted opacity-0 group-hover:opacity-100 hover:bg-octopus-primary/20 hover:text-octopus-primary transition-all"
                  title="资产管理（角色服装 / 道具）"
                >
                  <Shirt className="w-3.5 h-3.5" />
                </button>
                <button
                  onClick={(e) => props.onDelete(e, s.id, s.name)}
                  className="w-5 h-5 flex items-center justify-center rounded text-octopus-muted opacity-0 group-hover:opacity-100 hover:bg-red-500/20 hover:text-red-400 transition-all"
                  title="删除剧集"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>

              {/* 展开：master + episodes */}
              {expanded && (
                <div className="border-t border-octopus-border bg-octopus-panel/50">
                  {/* 总项目 */}
                  {s.master_project_id && (
                    <div
                      onClick={() => props.onSelectMaster(s)}
                      className={`flex items-center gap-2 px-3 py-1.5 pl-7 text-xs cursor-pointer transition-colors ${
                        isActiveMaster
                          ? 'bg-octopus-primary/20 text-octopus-menu'
                          : 'text-octopus-menu hover:bg-octopus-border/40'
                      }`}
                    >
                      <Film className="w-3.5 h-3.5 flex-shrink-0 text-amber-400" />
                      <span className="truncate flex-1">总项目（筹划）</span>
                    </div>
                  )}
                  {/* episodes */}
                  {episodes.map((ep) => {
                    const phaseInfo = EPISODE_PHASE_LABEL[ep.phase || 'plan_confirmed']
                      || { text: ep.phase || '?', color: 'bg-gray-500/30 text-gray-300' }
                    const isCompleted = ep.phase === 'completed'
                    const notStarted = !ep.phase || ep.phase === 'initial' || ep.phase === 'plan_confirmed'
                    return (
                      <div
                        key={ep.id}
                        onClick={() => props.onSelectEpisode(s, ep.id)}
                        className={`group flex items-center gap-1 px-3 py-1.5 pl-7 text-xs cursor-pointer transition-colors ${
                          props.currentProjectId === ep.id
                            ? 'bg-octopus-primary/20 text-octopus-menu'
                            : 'text-octopus-menu hover:bg-octopus-border/40'
                        }`}
                      >
                        <Tv className="w-3.5 h-3.5 flex-shrink-0 text-purple-400" />
                        {/* 后端 ep.name 已经包含「第N集 · title」，前面不再加 */}
                        <span className="truncate flex-1">{ep.name}</span>
                        {/* (b) 当前集有节点在跑 → 青色运行脉冲 */}
                        {props.currentRunning && props.currentProjectId === ep.id && (
                          <span className="relative flex-shrink-0 w-2 h-2 mr-0.5" title="生成中">
                            <span className="absolute inset-0 rounded-full bg-cyan-400 animate-ping opacity-75" />
                            <span className="absolute inset-0 rounded-full bg-cyan-400" />
                          </span>
                        )}
                        {/* 0.7.0：阶段徽章 */}
                        <span className={`text-[9px] px-1 py-0.5 rounded ${phaseInfo.color} whitespace-nowrap flex-shrink-0`}>
                          {phaseInfo.text}
                        </span>
                        {/* ▶ 按钮 — 只在还没开工时显示 */}
                        {notStarted && !isCompleted && (
                          <button
                            onClick={(e) => { e.stopPropagation(); props.onStartEpisode(s, ep.id) }}
                            title="开始制作（切到这集 + 启动剧本生成）"
                            className="flex-shrink-0 inline-flex items-center justify-center w-5 h-5 rounded text-octopus-muted opacity-0 group-hover:opacity-100 hover:bg-octopus-primary/30 hover:text-octopus-brand transition-all"
                          >
                            <Play className="w-3 h-3 fill-current" />
                          </button>
                        )}
                      </div>
                    )
                  })}
                  {isActiveSeries && episodes.length === 0 && s.master_project_id && (
                    <div className="px-3 py-2 pl-7 text-[10px] text-octopus-muted">
                      还没有子集 — 在总项目完成视觉开发后可 fan-out
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </>
  )
}
