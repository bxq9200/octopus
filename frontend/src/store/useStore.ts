import { WorkflowNode } from "@/types"
import { create } from 'zustand'
import { ChatMessage, DAG, Project, SeriesBrief, SeriesDetail, EpisodeBreakdownItem, ScriptSummary } from '@/types'

interface AppState {
  // Sidebar
  sidebarOpen: boolean
  toggleSidebar: () => void
  // 0.7.0：sidebar tab —— 「项目」/「剧集」
  sidebarTab: 'projects' | 'series'
  setSidebarTab: (tab: 'projects' | 'series') => void

  // Projects
  projects: Project[]
  currentProject: Project | null
  setProjects: (projects: Project[]) => void
  setCurrentProject: (project: Project | null) => void

  // 0.7.0：Series（剧集）
  series: SeriesBrief[]
  currentSeries: SeriesDetail | null
  expandedSeriesIds: string[]                              // 哪些 series 展开列了 episodes
  setSeries: (series: SeriesBrief[]) => void
  setCurrentSeries: (series: SeriesDetail | null) => void
  upsertSeries: (series: SeriesBrief) => void              // 创建/patch 后更新列表
  removeSeries: (id: string) => void
  toggleSeriesExpanded: (id: string) => void
  // 专家模式：当前协商中的分集草案 + 剧本摘要（episode_breakdown_draft 收到时更新）
  expertScriptSummary: ScriptSummary | null
  expertEpisodes: EpisodeBreakdownItem[]
  setExpertBreakdown: (summary: ScriptSummary | null, episodes: EpisodeBreakdownItem[]) => void

  // Chat
  messages: ChatMessage[]
  addMessage: (msg: ChatMessage) => void
  isTyping: boolean
  setIsTyping: (v: boolean) => void

  // Canvas / DAG
  dag: DAG | null
  setDag: (dag: DAG | null) => void
  updateNodeStatus: (nodeId: string, status: WorkflowNode["status"], result?: any) => void
  addNode: (node: WorkflowNode) => void
  updateNode: (nodeId: string, updates: Partial<WorkflowNode>) => void
  removeNodes: (nodeIds: string[]) => void

  // Selected node for editing (also auto-opens on node_completed)
  selectedNode: string | null
  setSelectedNode: (id: string | null) => void

  // 画布手动拖动位置（projectId → nodeId → {x,y}），localStorage 持久化；有记录的节点不被自动布局覆盖
  manualPositions: Record<string, Record<string, { x: number; y: number }>>
  setNodePosition: (projectId: string, nodeId: string, pos: { x: number; y: number }) => void
  resetNodePositions: (projectId: string) => void

  // Workflow state
  currentPhase: string
  setCurrentPhase: (phase: string) => void
  confirmedNodes: string[]
  addConfirmedNode: (nodeId: string) => void

  // Settings modal
  settingsOpen: boolean
  setSettingsOpen: (v: boolean) => void

  // 0.7.2 资产管理面板（剧集级：服装/道具从属剧集，跨 master+分集聚合）。值=打开的 seriesId，null=关闭
  assetManagerSeriesId: string | null
  setAssetManagerSeriesId: (id: string | null) => void

  // 重置所有工作流状态（切换项目前调用）
  resetWorkflow: () => void
  // 从 /projects/{id}/workflow API 的响应批量恢复
  hydrateWorkflow: (data: {
    nodes?: any[]
    edges?: any[]
    phase?: string
    chat_history?: any[]
  }) => void
}

export const useStore = create<AppState>((set) => ({
  sidebarOpen: false,
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  sidebarTab: (typeof localStorage !== 'undefined'
    && (localStorage.getItem('octopus.sidebarTab') as 'projects' | 'series'))
    || 'projects',
  setSidebarTab: (tab) => {
    if (typeof localStorage !== 'undefined') localStorage.setItem('octopus.sidebarTab', tab)
    set({ sidebarTab: tab })
  },

  projects: [],
  currentProject: null,
  setProjects: (projects) => set({ projects }),
  setCurrentProject: (project) => set({ currentProject: project }),

  // 0.7.0 Series
  series: [],
  currentSeries: null,
  expandedSeriesIds: [],
  setSeries: (series) => set({ series }),
  setCurrentSeries: (cs) => set({ currentSeries: cs }),
  upsertSeries: (s) => set((st) => {
    const idx = st.series.findIndex((x) => x.id === s.id)
    if (idx === -1) return { series: [s, ...st.series] }
    const next = [...st.series]
    next[idx] = { ...next[idx], ...s }
    return { series: next }
  }),
  removeSeries: (id) => set((st) => ({
    series: st.series.filter((s) => s.id !== id),
    expandedSeriesIds: st.expandedSeriesIds.filter((sid) => sid !== id),
    currentSeries: st.currentSeries?.id === id ? null : st.currentSeries,
  })),
  toggleSeriesExpanded: (id) => set((st) => ({
    expandedSeriesIds: st.expandedSeriesIds.includes(id)
      ? st.expandedSeriesIds.filter((sid) => sid !== id)
      : [...st.expandedSeriesIds, id],
  })),
  expertScriptSummary: null,
  expertEpisodes: [],
  setExpertBreakdown: (summary, episodes) =>
    set({ expertScriptSummary: summary, expertEpisodes: episodes }),

  messages: [],
  addMessage: (msg) => set((s) => ({ messages: [...s.messages, msg] })),
  isTyping: false,
  setIsTyping: (v) => set({ isTyping: v }),

  dag: null,
  setDag: (dag) => set({ dag }),
  updateNodeStatus: (nodeId, status, data) =>
    set((s) => {
      if (!s.dag) return s
      const nodes = s.dag.nodes.map((n) => {
        if (n.id !== nodeId) return n
        const updates: any = { status }
        if (data?.error) updates.error = data.error
        if (data?.output) {
          updates.outputs = data.output
          // 如果输出包含图片或文本，也更新 result 用于预览
          if (data.output.image_url || data.output.text) {
            updates.result = data.output
          }
        }
        return { ...n, ...updates }
      })
      return { dag: { ...s.dag, nodes } }
    }),
  addNode: (node) =>
    set((s) => {
      if (!s.dag) return { dag: { nodes: [node], edges: [] } }
      // 检查是否已存在（通过 id 或 node_id）
      const exists = s.dag.nodes.find((n) =>
        n.id === node.id || (node.node_id && n.node_id === node.node_id)
      )
      if (exists) {
        // 更新现有节点而不是添加
        const nodes = s.dag.nodes.map((n) =>
          n.id === node.id || (node.node_id && n.node_id === node.node_id)
            ? { ...n, ...node }
            : n
        )
        return { dag: { ...s.dag, nodes } }
      }
      return { dag: { ...s.dag, nodes: [...s.dag.nodes, node] } }
    }),
  updateNode: (nodeId, updates) =>
    set((s) => {
      if (!s.dag) return s
      const nodes = s.dag.nodes.map((n) =>
        n.id === nodeId ? { ...n, ...updates } : n
      )
      return { dag: { ...s.dag, nodes } }
    }),
  removeNodes: (nodeIds) =>
    set((s) => {
      if (!s.dag) return s
      const idSet = new Set(nodeIds)
      const nodes = s.dag.nodes
        .filter((n) => !idSet.has(n.id))
        .map((n) => ({
          ...n,
          // 把已删除节点从 children / depends_on 摘掉
          children: (n.children || []).filter((c: string) => !idSet.has(c)),
          depends_on: (n.depends_on || []).filter((d: string) => !idSet.has(d)),
        }))
      const edges = (s.dag.edges || []).filter(
        (e: any) => !idSet.has(e.source) && !idSet.has(e.target)
      )
      const confirmed = s.confirmedNodes.filter((id) => !idSet.has(id))
      const selected = s.selectedNode && idSet.has(s.selectedNode) ? null : s.selectedNode
      return {
        dag: { nodes, edges },
        confirmedNodes: confirmed,
        selectedNode: selected,
      }
    }),

  selectedNode: null,
  setSelectedNode: (id) => set({ selectedNode: id }),

  manualPositions: (() => {
    if (typeof localStorage === 'undefined') return {}
    try { return JSON.parse(localStorage.getItem('octopus.manualPositions') || '{}') } catch { return {} }
  })(),
  setNodePosition: (projectId, nodeId, pos) => set((s) => {
    const next = {
      ...s.manualPositions,
      [projectId]: { ...(s.manualPositions[projectId] || {}), [nodeId]: pos },
    }
    if (typeof localStorage !== 'undefined') {
      try { localStorage.setItem('octopus.manualPositions', JSON.stringify(next)) } catch {}
    }
    return { manualPositions: next }
  }),
  resetNodePositions: (projectId) => set((s) => {
    const next = { ...s.manualPositions }
    delete next[projectId]
    if (typeof localStorage !== 'undefined') {
      try { localStorage.setItem('octopus.manualPositions', JSON.stringify(next)) } catch {}
    }
    return { manualPositions: next }
  }),

  currentPhase: 'initial',
  setCurrentPhase: (phase) => set({ currentPhase: phase }),
  confirmedNodes: [],
  addConfirmedNode: (nodeId) =>
    set((s) => ({
      confirmedNodes: [...s.confirmedNodes, nodeId]
    })),

  settingsOpen: false,
  setSettingsOpen: (v) => set({ settingsOpen: v }),

  assetManagerSeriesId: null,
  setAssetManagerSeriesId: (id) => set({ assetManagerSeriesId: id }),

  resetWorkflow: () => set({
    messages: [],
    dag: null,
    selectedNode: null,
    currentPhase: 'initial',
    confirmedNodes: [],
    isTyping: false,
    expertScriptSummary: null,
    expertEpisodes: [],
  }),

  hydrateWorkflow: (data) => set(() => {
    const nodes = (data.nodes || []) as WorkflowNode[]
    const edges = (data.edges || []) as DAG['edges']
    const confirmed = nodes
      .filter((n: any) => n.status === 'confirmed' || n.confirmed === true)
      .map((n: any) => n.id as string)
    return {
      messages: (data.chat_history || []) as ChatMessage[],
      dag: { nodes, edges },
      selectedNode: null,
      currentPhase: data.phase || 'initial',
      confirmedNodes: confirmed,
      isTyping: false,
    }
  }),
}))
