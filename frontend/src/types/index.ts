export interface WorkflowNode {
  id: string
  node_id?: string  // 后端模板节点ID（如 node_001）
  type: string
  phase?: string  // 后端阶段标签（art / episode_art / ...）。hydrate 一直带；WS patch 现在也带上，消除"实时 vs 刷新"不一致
  label: string
  position: { x: number; y: number }
  params?: Record<string, any>
  inputs?: Record<string, any>
  outputs?: Record<string, any>
  depends_on?: string[]
  children?: string[]
  status?: 'idle' | 'running' | 'completed' | 'confirmed' | 'error' | 'skipped'
  result?: any
  error?: string
  // 交互式工作流字段
  agent?: string
  module?: string
  need_confirm?: boolean
  confirmed?: boolean
  need_user_review?: boolean
  output?: any
  next_nodes?: string[]
  parent_node?: string
  has_children?: boolean
  // 进度（视频生成等长任务）：0-100，由后端 node_progress 事件推送
  progress?: number
  progress_message?: string
}

export interface DAG {
  nodes: WorkflowNode[]
  edges?: Edge[]
}

export interface Edge {
  id: string
  source: string
  target: string
}

export interface ChatAttachment {
  url: string
  name: string
  mime: string
  size: number
  // 0.7.0：上传分类（image / script）
  kind?: 'image' | 'script'
  // 仅 kind=script：前 2000 字符预览 + 完整字符数
  parsed_text?: string
  parsed_chars?: number
}

export interface ChatMessage {
  id: string
  role: 'user' | 'agent'
  content: string
  dag?: DAG
  timestamp: number
  attachments?: ChatAttachment[]
  // 发言的 agent 标识（director / screenwriter / …），用于聊天栏显示对应头像与名字
  agent?: string
  // 支持三种模式
  mode?: 'creative' | 'story' | 'file'
  // 扩展消息类型
  type?: 'plan' | 'ask' | 'suggest' | 'text' | 'plan_confirmed' | 'node_completed' | 'error' | 'script_confirmed' | 'art_confirmed' | 'art_children_spawned' | 'art_assets_all_confirmed' | 'storyboard_confirmed' | 'all_videos_confirmed' | 'merge_started' | 'project_completed' | 'execution_started' | 'node_created' | 'outline_draft' | 'advanced_confirmed' | 'creator_chat' | 'episode_breakdown_draft' | 'episode_breakdown_confirmed' | 'series_assets_frozen' | 'series_episodes_created'
  defaultPlan?: any
  // 进阶模式：剧本草稿
  outline?: {
    title?: string
    logline?: string
    tone?: string
    characters?: Array<{ name: string; role?: string; description?: string }>
    scenes?: Array<{ scene_id: string; location?: string; beat?: string; duration_seconds?: number; key_action?: string }>
  }
  readyToConfirm?: boolean
  missingAspects?: string[]
  // 进阶模式协作：router 路由到的目标 agent + 改了哪些节点
  targetAgent?: 'director' | 'screenwriter' | 'art_director' | 'storyboard' | 'publisher'
  updatedNodes?: string[]
  rerunNodes?: string[]
  // agent.chat 实际下发给生图/视频模型的 patch（含 prompt、参考图等）— debug/透明用
  appliedUpdates?: Array<{
    node_id?: string
    reason?: string
    should_rerun?: boolean
    prompt?: string
    reference_image_urls?: string[]
    other_fields?: Record<string, any>
  }>
  suggestions?: string[]
  questions?: string[]
  // 节点相关
  nodeId?: string
  needConfirm?: boolean
  // 对话框确认
  needDialogConfirm?: boolean
  confirmAction?: string
  cancelAction?: string
  // 项目
  projectId?: string
  // 0.7.0 专家模式：分集草案
  scriptSummary?: ScriptSummary
  episodes?: EpisodeBreakdownItem[]
  seriesId?: string
}

export type Mode = 'simple' | 'advanced' | 'expert' | 'mv'

export interface ProjectConfig {
  mode?: Mode
  phase?: string
  // 后端 project.config 还可能存任意键，留通用扩展位
  [key: string]: any
}

export interface Project {
  id: string
  name: string
  status: string
  created_at: string
  config?: ProjectConfig
  // 0.7.0 专家模式关联：series_id 非空 + is_episode False → master；+ True → episode
  series_id?: string | null
  is_episode?: boolean
  episode_index?: number | null
}

// ═══════════════════════════════════════════════════════
// 0.7.0 专家模式：Series（剧集）
// ═══════════════════════════════════════════════════════

export type SeriesStatus = 'draft' | 'breakdown' | 'visual_dev' | 'producing' | 'done'

export interface EpisodeBrief {
  id: string
  name: string
  status: string
  episode_index?: number | null
  created_at?: string | null
  // 0.7.0：项目级 phase（initial / plan_confirmed / script / art / storyboard / keyframe / video / audio / merge / publish / completed）
  phase?: string | null
}

export interface EpisodeBreakdownItem {
  ep_id: string
  title: string
  story_arc?: string
  scene_range?: string
  est_duration_min?: number
  cliff_hanger?: string
}

export interface ScriptSummary {
  logline?: string
  genre?: string
  themes?: string[]
  tone?: string
  characters?: Array<{ name: string; role?: string; personality?: string; arc?: string }>
  scene_outline?: Array<{ chapter?: string; summary?: string; key_events?: string[] }>
  est_total_min?: number
  suggested_episode_count?: number
  suggested_aspect_ratio?: string
  notes?: string
}

export interface SeriesBrief {
  id: string
  name: string
  description?: string | null
  status: SeriesStatus | string
  master_project_id?: string | null
  episode_count?: number
  created_at?: string | null
}

export interface SeriesDetail extends SeriesBrief {
  script_summary?: ScriptSummary
  episodes_breakdown?: EpisodeBreakdownItem[]
  shared_assets_frozen?: boolean
  episodes?: EpisodeBrief[]
}
