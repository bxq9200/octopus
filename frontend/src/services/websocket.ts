/**
 * WebSocket 服务 — 连接后端实时推送的 DAG 执行状态
 * 支持交互式工作流：节点创建、状态更新、结果推送、确认通知
 */
import { useStore } from '@/store/useStore'

// 连接状态枚举
const enum WSState {
  IDLE = 0,
  CONNECTING = 1,
  OPEN = 2,
  CLOSING = 3,
  CLOSED = 4,
}

class WebSocketClient {
  private ws: WebSocket | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null
  private state: WSState = WSState.IDLE
  private projectId: string = ''
  private reconnectAttempts = 0
  private maxReconnectAttempts = 5
  private reconnectDelay = 3000
  private intentionalClose = false

  private get WS_BASE(): string {
    const host = window.location.hostname
    const port = 8000
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${host}:${port}`
  }

  connect(projectId: string) {
    if (this.projectId === projectId && (this.state === WSState.CONNECTING || this.state === WSState.OPEN)) {
      console.log('[WS] 已连接同一项目，跳过:', projectId)
      return
    }

    this.disconnect(true)
    this.projectId = projectId
    this.intentionalClose = false
    this.reconnectAttempts = 0
    this._connect()
  }

  private _connect() {
    if (this.state === WSState.CONNECTING || this.state === WSState.OPEN) {
      return
    }

    const url = `${this.WS_BASE}/ws/${this.projectId}`
    console.log('[WS] 连接:', url)
    this.state = WSState.CONNECTING

    try {
      this.ws = new WebSocket(url)
    } catch (err) {
      console.error('[WS] 创建失败:', err)
      this._scheduleReconnect()
      return
    }

    this.ws.onopen = () => {
      console.log('[WS] 已连接')
      this.state = WSState.OPEN
      this.reconnectAttempts = 0
      this._startHeartbeat()
    }

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        handleMessage(msg)
      } catch {
        // ping/pong 或非 JSON 消息忽略
      }
    }

    this.ws.onclose = (event) => {
      const wasOpen = this.state === WSState.OPEN
      this.state = WSState.CLOSED
      this._cleanupTimers()
      console.log(`[WS] 连接已关闭 (code=${event.code}, wasOpen=${wasOpen})`)

      if (!this.intentionalClose && wasOpen && this.reconnectAttempts < this.maxReconnectAttempts) {
        this._scheduleReconnect()
      }
    }

    this.ws.onerror = (err) => {
      console.error('[WS] 错误:', err)
    }
  }

  disconnect(intentional = true) {
    this.intentionalClose = intentional
    this._cleanupTimers()

    if (this.ws) {
      this.ws.onopen = null
      this.ws.onmessage = null
      this.ws.onclose = null
      this.ws.onerror = null

      if (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING) {
        this.ws.close()
      }
      this.ws = null
    }

    this.state = WSState.CLOSED
    this.projectId = ''
    this.reconnectAttempts = 0
  }

  send(data: any) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data))
    }
  }

  private _startHeartbeat() {
    this._cleanupTimers()
    this.heartbeatTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send('ping')
      }
    }, 30000)
  }

  private _cleanupTimers() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private _scheduleReconnect() {
    if (this.reconnectTimer) return

    this.reconnectAttempts++
    const delay = Math.min(this.reconnectDelay * this.reconnectAttempts, 30000)
    console.log(`[WS] ${this.reconnectAttempts}/${this.maxReconnectAttempts} 秒后重连...`)

    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      if (!this.intentionalClose) {
        this._connect()
      }
    }, delay)
  }

  getReadyState(): number {
    return this.ws?.readyState ?? WebSocket.CLOSED
  }
}

// 单例实例
const wsClient = new WebSocketClient()

export function connectWebSocket(projectId: string) {
  wsClient.connect(projectId)
}

export function disconnectWebSocket() {
  wsClient.disconnect(true)
}

export function getWebSocketState(): number {
  return wsClient.getReadyState()
}

export function sendWebSocketMessage(data: any) {
  wsClient.send(data)
}

/**
 * 处理 WebSocket 消息
 * 支持交互式工作流的所有消息类型
 */
// 没钱信号：视频/关键帧因 credits 不足降级 mock → 弹「余额不足」提示；
// 任何视频出片(credits 变了) → 派发刷新余额事件（CreditsBadge 监听）。
function checkCreditsSignals(node: any) {
  const r = node?.result || {}
  const reason = String(r.mock_reason || r.error || node?.error || '')
  if (r.mock && /credits|402|余额|充值/.test(reason)) {
    window.dispatchEvent(new CustomEvent('octopus:out-of-credits', { detail: { message: reason } }))
  }
  const id = String(node?.id || '')
  if ((id.includes('video') || id.includes('keyframe')) && (node?.status === 'completed' || r.video_url || r.image_url)) {
    window.dispatchEvent(new Event('octopus:credits-refresh'))
  }
}

function handleMessage(msg: any) {
  const {
    updateNodeStatus,
    addNode,
    updateNode,
    removeNodes,
    addMessage,
    setIsTyping,
    setCurrentPhase,
    addConfirmedNode,
    setSelectedNode,
  } = useStore.getState()

  console.log('[WS] 收到消息:', msg.type, msg)

  switch (msg.type) {
    // 新节点创建（逐步生成）
    case 'new_node': {
      const node = msg.node
      if (node) {
        addNode({
          id: node.id,
          node_id: node.node_id || node.id,
          type: node.type,
          phase: node.phase,   // 带上后端阶段标签，批量生成按钮等实时判定要用（否则要刷新才出现）
          label: node.label,
          status: node.status || 'idle',
          position: node.position,
          params: node.params || {},
          inputs: node.inputs || {},
          outputs: node.outputs || {},
          result: node.result,
          error: node.error,
          depends_on: node.depends_on || [],
          children: node.children || [],
          need_confirm: node.need_confirm,
          confirmed: node.confirmed,
          agent: node.agent,
        })
        // 添加系统消息
        addMessage({
          id: `new_${Date.now()}`,
          role: 'agent',
          content: msg.message || `新节点已创建: ${node.label}`,
          timestamp: Date.now(),
          type: 'node_created',
        })
      }
      break
    }

    // 节点创建（兼容旧格式）
    case 'node_created': {
      const node = msg.node
      if (node) {
        addNode({
          id: node.id,
          node_id: node.node_id || node.id,
          type: node.type,
          phase: node.phase,   // 同上：WS 补 phase，消除"实时 vs 刷新"不一致
          label: node.label,
          status: node.status,
          position: node.position,
          params: node.params || {},
          inputs: node.inputs || {},
          outputs: node.outputs || {},
          result: node.result,
          error: node.error,
          depends_on: node.depends_on || [],
          children: node.children || [],
          need_confirm: node.need_confirm,
          confirmed: node.confirmed,
          agent: node.agent,
        })
      }
      break
    }

    // 节点更新
    case 'node_updated': {
      const node = msg.node
      if (node) {
        updateNode(node.id, {
          phase: node.phase,   // 补 phase：批量生成按钮等实时判定依赖它
          status: node.status,
          result: node.result,
          outputs: node.outputs,
          error: node.error,
          confirmed: node.confirmed,
          children: node.children,
          depends_on: node.depends_on,
          params: node.params, // W5 即时传帧：链镜 first_frame_url 更新要刷到卡
        })
        checkCreditsSignals(node)   // 没钱降级检测 + 视频出片刷余额
      }
      break
    }

    // 节点删除（含级联）
    case 'nodes_deleted': {
      const ids: string[] = msg.node_ids || []
      if (ids.length > 0) removeNodes(ids)
      break
    }

    // 节点进度（视频/关键帧生成时按轮询周期推送）
    case 'node_progress': {
      const { node_id, progress, message } = msg
      if (node_id) {
        updateNode(node_id, {
          progress: typeof progress === 'number' ? progress : undefined,
          progress_message: message,
        } as any)
      }
      break
    }

    // 节点状态变化
    case 'node_status_changed': {
      const { node_id, status, output, error } = msg
      updateNodeStatus(node_id, status, { output, error })
      break
    }

    // 节点执行完成
    case 'node_completed': {
      const { node_id, node_type, result_preview, need_confirm, result } = msg
      // 更新节点状态和结果（清掉进度条字段）
      updateNode(node_id, {
        status: 'completed',
        result: result || msg.outputs || msg.result,
        progress: undefined,
        progress_message: undefined,
      } as any)

      // 添加系统消息通知创作者
      addMessage({
        id: `sys_${Date.now()}`,
        role: 'agent',
        content: result_preview || `节点 ${node_id} 执行完成`,
        timestamp: Date.now(),
        type: 'node_completed',
        nodeId: node_id,
        needConfirm: need_confirm,
      })

      // 自动打开节点编辑器（含结果预览）
      if (need_confirm) {
        setSelectedNode(node_id)
      }
      break
    }

    // 节点执行失败
    case 'node_failed': {
      const { node_id, error } = msg
      updateNodeStatus(node_id, 'error', { error })
      addMessage({
        id: `err_${Date.now()}`,
        role: 'agent',
        content: `❌ 节点执行失败: ${error}`,
        timestamp: Date.now(),
        type: 'error',
      })
      break
    }

    // 方案已确认
    case 'plan_confirmed': {
      setCurrentPhase('plan_confirmed')
      addMessage({
        id: `plan_${Date.now()}`,
        role: 'agent',
        content: msg.message || '方案已确认，开始创作！',
        timestamp: Date.now(),
        type: 'plan_confirmed',
      })
      break
    }

    // 剧本已确认
    case 'script_confirmed': {
      setCurrentPhase('script')
      addConfirmedNode(msg.node_id)
      addMessage({
        id: `script_${Date.now()}`,
        role: 'agent',
        content: msg.message || '剧本已确认，开始美术设定...',
        timestamp: Date.now(),
        type: 'script_confirmed',
      })
      break
    }

    // 美术设定已确认
    case 'art_confirmed': {
      setCurrentPhase('art')
      addConfirmedNode(msg.node_id)
      addMessage({
        id: `art_${Date.now()}`,
        role: 'agent',
        content: msg.message || '美术设定已确认，开始分镜设计...',
        timestamp: Date.now(),
        type: 'art_confirmed',
      })
      break
    }

    // 分镜已确认
    case 'storyboard_confirmed': {
      setCurrentPhase('storyboard')
      addConfirmedNode(msg.node_id)
      addMessage({
        id: `sb_${Date.now()}`,
        role: 'agent',
        content: msg.message || '分镜已确认，开始生成视频...',
        timestamp: Date.now(),
        type: 'storyboard_confirmed',
      })
      break
    }

    // 所有视频已确认
    case 'all_videos_confirmed': {
      addMessage({
        id: `all_vid_${Date.now()}`,
        role: 'agent',
        content: msg.message || '所有视频已确认！',
        timestamp: Date.now(),
        type: 'all_videos_confirmed',
        needDialogConfirm: true,
        confirmAction: msg.confirm_action,
        cancelAction: msg.cancel_action,
      })
      break
    }

    // 所有角色/场景物料已确认 → 询问是否开始分镜
    case 'art_assets_all_confirmed': {
      addMessage({
        id: `all_art_${Date.now()}`,
        role: 'agent',
        content: msg.message || '所有美术物料已确认！',
        timestamp: Date.now(),
        type: 'art_assets_all_confirmed',
        needDialogConfirm: true,
        confirmAction: msg.confirm_action,
        cancelAction: msg.cancel_action,
      })
      break
    }

    // 美术主节点确认后子节点已 fan-out
    case 'art_children_spawned': {
      addMessage({
        id: `art_spawn_${Date.now()}`,
        role: 'agent',
        content: msg.message || '美术物料已开始生成...',
        timestamp: Date.now(),
        type: 'art_children_spawned',
      })
      break
    }

    // 0.7.2 ②：场景 BGM 计划确认结果（多少新生成/多少复用 —— 全复用时不出新节点，必须有这条提示）
    case 'episode_audio_confirmed': {
      addMessage({
        id: `ep_audio_${Date.now()}`,
        role: 'agent',
        content: msg.message || '场景 BGM 计划已确认',
        timestamp: Date.now(),
        type: 'text',
      })
      break
    }

    // 合并开始
    case 'merge_started': {
      setCurrentPhase('merge')
      addMessage({
        id: `merge_${Date.now()}`,
        role: 'agent',
        content: msg.message || '开始视频合并...',
        timestamp: Date.now(),
        type: 'merge_started',
      })
      break
    }

    // 项目完成
    case 'project_completed': {
      setCurrentPhase('completed')
      addMessage({
        id: `done_${Date.now()}`,
        role: 'agent',
        content: msg.message || '🎉 项目全部完成！',
        timestamp: Date.now(),
        type: 'project_completed',
      })
      setIsTyping(false)
      break
    }

    // ─── 0.7.0 专家模式相关 ─────────────────────────────
    // 通用"思考中..."提示（parse / breakdown 之类长耗时操作的中途反馈）
    case 'agent_thinking': {
      addMessage({
        id: `think_${Date.now()}`,
        role: 'agent',
        content: msg.message || '思考中...',
        timestamp: Date.now(),
        type: 'text',
      })
      break
    }

    // 分集已确认 → 后端会跑视觉开发；前端刷新 series 状态
    case 'episode_breakdown_confirmed': {
      const { setCurrentSeries, currentSeries } = useStore.getState()
      if (currentSeries && currentSeries.id === msg.series_id) {
        setCurrentSeries({ ...currentSeries, status: 'visual_dev' })
      }
      addMessage({
        id: `epconf_${Date.now()}`,
        role: 'agent',
        content: msg.message || `✅ 分集已确认（共 ${msg.episode_count || '?'} 集）`,
        timestamp: Date.now(),
        type: 'episode_breakdown_confirmed',
      })
      break
    }

    // 总项目美术全 confirm → 资产冻结，可 fan-out
    case 'series_assets_frozen': {
      const { setCurrentSeries, currentSeries } = useStore.getState()
      if (currentSeries && currentSeries.id === msg.series_id) {
        setCurrentSeries({ ...currentSeries, shared_assets_frozen: true })
      }
      addMessage({
        id: `frozen_${Date.now()}`,
        role: 'agent',
        content: msg.message || '🎨 总项目美术全部确认！可以「开始制作分集」了。',
        timestamp: Date.now(),
        type: 'series_assets_frozen',
      })
      break
    }

    // 用户改了总项目 art → 弹同步对话框（SeriesAssetSyncModal 监听这个事件）
    case 'series_assets_changed': {
      // 仅触发，对话框组件会通过监听 store 或自己的 subscriber 渲染
      window.dispatchEvent(new CustomEvent('series_assets_changed', { detail: msg }))
      break
    }

    // Episodes 已 fan-out（Step 10）
    case 'series_episodes_created': {
      addMessage({
        id: `epc_${Date.now()}`,
        role: 'agent',
        content: msg.message || `📺 已创建 ${msg.count || '?'} 集子项目`,
        timestamp: Date.now(),
        type: 'series_episodes_created',
      })
      // 让 sidebar / fanout 按钮听到事件，重新拉 series 详情和列表
      window.dispatchEvent(new CustomEvent('series_episodes_created', { detail: msg }))
      break
    }

    default:
      console.log('[WS] 未知消息类型:', msg.type)
  }
}
