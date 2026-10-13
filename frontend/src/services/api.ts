import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
  // 0.6.2: 给所有 API 设 240s 兜底超时。Kimi/视频生成等长操作正常 30-90s；
  // 240s 是"真挂死"的红线，超过就抛 timeout 让前端 finally 解锁 isTyping。
  // 否则没 timeout 时 axios 默认 0 = 永久挂起，导致前端 send 按钮锁死。
  timeout: 240000,
})

// ═══════════════════════════════════════════════════════
// 0.8 缝5：统一带上 Authorization
// 拆分前 LoginPage 把 token 写进 localStorage 但这里从来不发，后端也没有 auth 中间件。
// 现在统一附上：local 模式后端忽略它（本机单用户不鉴权），行为零变化；
// cloud_client 下这就是转发给云端网关的凭据。
// ═══════════════════════════════════════════════════════
export const TOKEN_KEY = 'octopus_token'

api.interceptors.request.use((config) => {
  try {
    const token = localStorage.getItem(TOKEN_KEY)
    if (token) {
      config.headers = config.headers || {}
      ;(config.headers as any).Authorization = `Bearer ${token}`
    }
  } catch {
    // localStorage 不可用（隐私模式等）不该阻断请求
  }
  return config
})

// ═══════════════════════════════════════════════════════
// 账号 API（0.8 缝5）
// ═══════════════════════════════════════════════════════
export const authAPI = {
  status: () => api.get('/auth/status'),
  // local 模式：账号密码（任意非空即可，本机不鉴权）
  login: (username: string, password: string) =>
    api.post('/auth/login', { username, password }),
  // cloud_client 模式：邮箱验证码两步流（后端代理到云端 /v1/auth/*）
  sendCode: (email: string, inviteCode = '') =>
    api.post('/auth/send-code', { email, invite_code: inviteCode }),
  verify: (email: string, code: string) =>
    api.post('/auth/verify', { email, code }),
  logout: () => api.post('/auth/logout'),
}

// 应用自身：版本 / 退出 / 检查更新 / 一键升级
export const appAPI = {
  info: () => api.get('/app/info'),
  quit: () => api.post('/app/quit'),
  checkUpdate: () => api.get('/app/update/check'),
  download: (url: string, version: string) => api.post('/app/update/download', { url, version }),
  progress: () => api.get('/app/update/progress'),
  install: () => api.post('/app/update/install'),
}

// credits 余额 / 流水（cloud_client 模式；local 返回 unlimited）
export const billingAPI = {
  balance: () => api.get('/auth/billing/balance'),
  history: () => api.get('/auth/billing/history'),
  // 充值（支付宝电脑网站支付）：拿档位 → 下单拿 pay_url → 浏览器打开 → 轮询订单
  payTiers: () => api.get('/auth/pay/tiers'),
  payCreate: (tier: string) => api.post('/auth/pay/create', { tier }),
  payOrder: (orderId: string) => api.get(`/auth/pay/orders/${orderId}`),
}

// ═══════════════════════════════════════════════════════
// 交互式工作流 API
// ═══════════════════════════════════════════════════════
export const workflowAPI = {
  // 对话入口
  chat: (message: string, projectId?: string, context?: any, attachments?: Array<{ url: string; name: string; mime: string; size: number }>) =>
    api.post('/workflow/chat', {
      message,
      project_id: projectId,
      context: context || {},
      attachments: attachments || []
    }),

  // 确认方案
  confirmPlan: (projectId: string) =>
    api.post('/workflow/confirm-plan', {
      project_id: projectId
    }),

  // 进阶模式：确认剧本大纲，开始下游创作
  confirmAdvancedOutline: (projectId: string) =>
    api.post('/workflow/confirm-advanced-outline', {
      project_id: projectId
    }),

  // 用用户上传的图替换节点 result 里的某个 image 字段（无需重跑模型）
  replaceNodeImage: (projectId: string, nodeId: string, imageUrl: string, field: 'image_url' | 'three_view_image_url' = 'image_url') =>
    api.post('/workflow/replace-node-image', {
      project_id: projectId,
      node_id: nodeId,
      image_url: imageUrl,
      field,
    }),

  // 节点操作：确认、重跑、编辑
  nodeAction: (projectId: string, nodeId: string, action: 'confirm' | 'rerun' | 'edit', params?: any) =>
    api.post('/workflow/node-action', {
      project_id: projectId,
      node_id: nodeId,
      action,
      params: params || {}
    }),

  // 0.7.1 专家模式：批量触发 IDLE 镜头节点开始生成（手动 fan-out）
  batchAction: (projectId: string, phase: 'all' | 'art' | 'keyframe' | 'video' = 'all') =>
    api.post('/workflow/batch-action', {
      project_id: projectId,
      phase,
      action: 'generate',
    }),

  // 0.7.7 本地化：把合成成片翻译成用户选的语言（蚂蚁 videotranslate，手动触发）
  localize: (projectId: string, nodeId: string, targetLanguages: string[], translateType: string = 'VOICE') =>
    api.post('/workflow/localize', {
      project_id: projectId,
      node_id: nodeId,
      target_languages: targetLanguages,
      translate_type: translateType,
    }),

  // 删除节点（默认级联删除下游）
  deleteNode: (projectId: string, nodeId: string, cascade = true) =>
    api.post('/workflow/delete-node', {
      project_id: projectId,
      node_id: nodeId,
      cascade,
    }),

  // 0.7.2 资产管理：新增服装/道具状态/道具（建 IDLE 子节点；删除/单套重生复用 deleteNode / nodeAction rerun）
  assetAction: (
    projectId: string,
    action: 'create_outfit' | 'create_prop_state' | 'create_prop',
    fields: Record<string, any>,
  ) =>
    api.post('/workflow/asset-action', {
      project_id: projectId,
      action,
      fields,
    }),

  // 发布：导出到 output/publish/<id>/
  publishExport: (projectId: string, nodeId: string, payload: {
    title: string
    description: string
    tags: string[]
    cover_image_url: string
    platform: string
  }) =>
    api.post('/workflow/publish/export', {
      project_id: projectId,
      node_id: nodeId,
      ...payload,
    }),

  // 发布：AI 重新生成封面
  publishGenerateCover: (projectId: string, nodeId: string, payload: {
    prompt: string
    reference_image_urls?: string[]
  }) =>
    api.post('/workflow/publish/generate-cover', {
      project_id: projectId,
      node_id: nodeId,
      prompt: payload.prompt,
      reference_image_urls: payload.reference_image_urls || [],
    }),

  // 开始视频合并
  startMerge: (projectId: string) =>
    api.post('/workflow/start-merge', {
      project_id: projectId
    }),

  // 开始分镜设计（art 物料全确认后弹的对话框点确认）
  startStoryboard: (projectId: string) =>
    api.post('/workflow/start-storyboard', {
      project_id: projectId
    }),

  // 0.7.0：启动 Episode 的剧本生成（找该项目 IDLE 的 script_generation 节点跑）
  startEpisodeScript: (projectId: string) =>
    api.post('/workflow/start-episode-script', {
      project_id: projectId
    }),

  // 获取项目状态
  getStatus: (projectId: string) =>
    api.get(`/workflow/status/${projectId}`),

  // 获取 DAG 结构
  getDAG: (projectId: string) =>
    api.get(`/workflow/dag/${projectId}`),

  // 获取节点详情
  getNode: (projectId: string, nodeId: string) =>
    api.get(`/workflow/node/${projectId}/${nodeId}`),
}

// ═══════════════════════════════════════════════════════
// Agents API（仅保留 NodeEditorPanel 单节点重跑使用的 execute）
// ═══════════════════════════════════════════════════════
export const agentAPI = {
  execute: (workflowNodes: any[], projectId?: string, startFrom?: string) =>
    api.post('/agents/execute', {
      workflow_nodes: workflowNodes,
      project_id: projectId,
      start_from: startFrom
    }),

  runAgent: (agentName: string, task: any, context?: any) =>
    api.post(`/agents/${agentName}/run`, { task, context }),

  listAgents: () => api.get('/agents/list'),
}

// Projects
// 1.1.0 MV 模式
export const mvAPI = {
  /** 传单个文件。歌和风格参考图共用一个 endpoint——后端按 mime/扩展名分流出 kind，
   *  音频还会回 duration 供界面回显。 */
  upload: (projectId: string, file: File) => {
    const fd = new FormData()
    fd.append('files', file)
    return api.post(`/uploads/${projectId}`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  start: (projectId: string, body: {
    audio_path: string
    type: 'narrative' | 'visual'
    style_refs?: string[]
    style_preset?: string
    user_brief?: string
    aspect_ratio?: string
  }) => api.post(`/mv/${projectId}/start`, body),

  /** 导入歌词。LRC 用它自带的时间戳；纯文本靠人声乐句 + ASR 锚点定位。 */
  importLyrics: (projectId: string, body: { content: string; format: 'lrc' | 'text' }) =>
    api.post(`/mv/${projectId}/lyrics/import`, body),

  /** 校对界面回写歌词轴 */
  updateLyrics: (projectId: string, lines: { start: number; end: number; text: string }[]) =>
    api.put(`/mv/${projectId}/lyrics`, { lines }),

  /** 按当前歌词轴试算镜头切分。纯本地算法、不调模型不花钱，所以可以随手拖随手看。 */
  previewShots: (projectId: string, body: {
    lines: { start: number; end: number; text: string }[]
    sections?: any[]
    duration: number
  }) => api.post(`/mv/${projectId}/preview-shots`, body),
}

export const projectAPI = {
  // 0.7.0：filter 支持 ?series_id=&is_episode= ；series_id='null' 表示 IS NULL（独立项目）
  list: (filter?: { series_id?: string | 'null'; is_episode?: boolean }) => {
    const params = new URLSearchParams()
    if (filter?.series_id !== undefined) params.set('series_id', String(filter.series_id))
    if (filter?.is_episode !== undefined) params.set('is_episode', String(filter.is_episode))
    const qs = params.toString()
    return api.get(qs ? `/projects?${qs}` : '/projects')
  },
  create: (data: { name?: string; description?: string; message?: string; config?: Record<string, any> }) =>
    api.post('/projects', data),
  get: (id: string) => api.get(`/projects/${id}`),
  updateDAG: (id: string, dag: any) =>
    api.put(`/projects/${id}/dag`, dag),
  delete: (id: string) => api.delete(`/projects/${id}`),
  // 完整工作流快照（含 nodes、edges、phase、chat_history）— 切项目/刷新时调用
  getWorkflow: (id: string) => api.get(`/projects/${id}/workflow`),
}

// 用户配置（API key、模型偏好等）
export const settingsAPI = {
  getAll: () => api.get('/settings'),
  getStatus: () => api.get('/settings/status'),
  getCategory: (cat: string) => api.get(`/settings/${cat}`),
  updateCategory: (cat: string, fields: Record<string, { value: string; sensitive?: boolean }>) =>
    api.patch(`/settings/${cat}`, { fields }),
  deleteField: (cat: string, key: string) => api.delete(`/settings/${cat}/${key}`),
  testProvider: (cat: string, provider: string) =>
    api.post(`/settings/test/${cat}/${provider}`),
}

// 聊天附件上传（0.6.0 ②d / 0.7.0 加 .txt/.md/.docx 剧本）
// 后端按 mime/扩展名自动分类 image / script；响应里的 attachment 含 kind 字段。
export const uploadAPI = {
  uploadImages: (projectId: string, files: File[]) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    return api.post(`/uploads/${projectId}`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  // 0.7.0：剧本/文档上传（同 endpoint，单独命名更直白）
  uploadScript: (projectId: string, file: File) => {
    const fd = new FormData()
    fd.append('files', file)
    return api.post(`/uploads/${projectId}`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
}

// ═══════════════════════════════════════════════════════
// 0.7.0 专家模式：Series（剧集）CRUD
// ═══════════════════════════════════════════════════════
export const seriesAPI = {
  list: () => api.get('/series'),
  create: (data: { name?: string; description?: string; message?: string }) =>
    api.post('/series', data),
  get: (id: string) => api.get(`/series/${id}`),
  patch: (id: string, data: { name?: string; description?: string }) =>
    api.patch(`/series/${id}`, data),
  delete: (id: string) => api.delete(`/series/${id}`),
  // Step 10 / 11 后端会实现
  fanoutEpisodes: (id: string) => api.post(`/series/${id}/fanout-episodes`),
  // 轮1: 清掉 episodes 子项目（保留 master + shared_assets），让新架构 fanout 重做
  resetEpisodes: (id: string) => api.post(`/series/${id}/reset-episodes`),
  syncAssets: (id: string, payload: {
    target: 'all_unstarted' | 'all' | 'selected'
    episode_ids?: string[]
  }) => api.post(`/series/${id}/sync-assets`, payload),
}

// 发布平台（OAuth + 上传）
export const publishersAPI = {
  list: () => api.get('/publishers'),
  getAuthUrl: (platform: string) => api.get(`/publishers/${platform}/auth-url`),
  revoke: (platform: string) => api.post(`/publishers/${platform}/revoke`),
  upload: (platform: string, payload: {
    video_path: string
    title: string
    description?: string
    tags?: string[]
    visibility?: string
  }) => api.post(`/publishers/${platform}/upload`, payload),
}

export default api
