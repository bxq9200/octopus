import React, { useState, useRef, useEffect } from 'react'
import { Send, Bot, User, Loader2, Play, CheckCircle, HelpCircle, RotateCcw, ChevronDown, Sparkles, MessageSquareMore, FileUp, Palette, Film, PenLine, Share2, RefreshCw, Plus, X, ImageIcon, Music } from 'lucide-react'
import OctopusIcon from './OctopusIcon'
import DirectorIcon from './DirectorIcon'
import { useStore } from '@/store/useStore'
import { workflowAPI, projectAPI, uploadAPI } from '@/services/api'
import { ChatMessage, ChatAttachment, DAG, Mode } from '@/types'
import { connectWebSocket, getWebSocketState, sendWebSocketMessage } from '@/services/websocket'
import { convertWorkflowNodes } from '@/utils/dagLayout'
import EpisodeBreakdownCard from './EpisodeBreakdownCard'
import MVSetupPanel from '../mv/MVSetupPanel'
import LyricTimelineEditor from '../mv/LyricTimelineEditor'

const MODE_OPTIONS: { value: Mode; label: string; icon: React.ComponentType<any>; disabled?: boolean; hint?: string }[] = [
  { value: 'simple',   label: '简易', icon: Sparkles,         hint: '一两句话直接生成视频' },
  { value: 'advanced', label: '进阶', icon: MessageSquareMore, hint: '导演多轮协商剧本，确认后才进入下游' },
  { value: 'expert',   label: '专家', icon: FileUp,            hint: '上传小说/剧本协商分集（漫剧专业模式）' },
  { value: 'mv',       label: 'MV',   icon: Music,              hint: '上传一首歌，按歌词与曲式生成音乐录影带' },
]

const LS_MODE_KEY = 'octopus_mode'

const AGENT_INFO: Record<string, { label: string; icon: React.ComponentType<any>; color: string }> = {
  director:     { label: '导演',   icon: Bot,      color: 'text-cyan-300' },
  screenwriter: { label: '编剧',   icon: PenLine,  color: 'text-purple-300' },
  art_director: { label: '美术',   icon: Palette,  color: 'text-pink-300' },
  storyboard:   { label: '分镜',   icon: Film,     color: 'text-orange-300' },
  publisher:    { label: '发布',   icon: Share2,   color: 'text-green-300' },
}

export default function FloatingChat() {
  const [input, setInput] = useState('')
  const messages = useStore((s) => s.messages)
  const addMessage = useStore((s) => s.addMessage)
  const isTyping = useStore((s) => s.isTyping)
  const setIsTyping = useStore((s) => s.setIsTyping)
  const setDag = useStore((s) => s.setDag)
  const dag = useStore((s) => s.dag)
  const currentProject = useStore((s) => s.currentProject)
  const setCurrentProject = useStore((s) => s.setCurrentProject)
  const projects = useStore((s) => s.projects)
  const setProjects = useStore((s) => s.setProjects)
  const setCurrentPhase = useStore((s) => s.setCurrentPhase)
  const currentPhase = useStore((s) => s.currentPhase)
  const confirmedNodes = useStore((s) => s.confirmedNodes)
  const scrollRef = useRef<HTMLDivElement>(null)

  // 模式选择器（简易 / 进阶 / 专家）—— 影响项目创建时的 config.mode
  const [mode, setModeState] = useState<Mode>(
    () => (localStorage.getItem(LS_MODE_KEY) as Mode) || 'simple',
  )
  const [modeOpen, setModeOpen] = useState(false)
  // 1.1.0 MV：配置面板（MV 的输入是歌+图，不走对话）
  const [mvPanelOpen, setMvPanelOpen] = useState(false)
  // 画布上「校对歌词时间轴」按钮 → 事件 → 这里打开编辑器（编辑器要播音频，挂在聊天层更稳）
  const [lyricNodeId, setLyricNodeId] = useState<string | null>(null)
  useEffect(() => {
    const open = (e: any) => setLyricNodeId(e?.detail?.nodeId || 'mv_audio')
    window.addEventListener('octopus:edit-lyrics', open)
    return () => window.removeEventListener('octopus:edit-lyrics', open)
  }, [])
  const modeMenuRef = useRef<HTMLDivElement>(null)

  // 聊天附件（参考图） — 0.6.0 阶段 ②d
  const [pendingAttachments, setPendingAttachments] = useState<ChatAttachment[]>([])
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  // creator_chat 消息展开"查看 prompt" 的 message id 集合
  const [expandedPrompts, setExpandedPrompts] = useState<Set<string>>(new Set())
  const togglePromptExpand = (msgId: string) => {
    setExpandedPrompts((prev) => {
      const next = new Set(prev)
      if (next.has(msgId)) next.delete(msgId); else next.add(msgId)
      return next
    })
  }

  const handlePickFiles = () => {
    if (uploading) return
    // 1.1.0：MV 模式下歌曲和风格参考图都归 MV 面板管，别让用户从聊天附件传——
    // 传进来也不知道哪个是歌哪个是风格图。
    if (mode === 'mv') {
      setMvPanelOpen(true)
      return
    }
    fileInputRef.current?.click()
  }

  const handleFilesSelected = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || [])
    e.target.value = ''  // 允许同一文件被再次选择
    if (!files.length) return

    // 0.7.0：支持 image / script 两类
    const IMAGE_MIME = ['image/png', 'image/jpeg', 'image/jpg', 'image/webp', 'image/gif']
    const IMAGE_EXT = ['.png', '.jpg', '.jpeg', '.webp', '.gif']
    const SCRIPT_EXT = ['.txt', '.md', '.markdown', '.docx', '.doc']
    const AUDIO_EXT = ['.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg']
    const MAX_IMAGE_SIZE = 8 * 1024 * 1024
    const MAX_SCRIPT_SIZE = 5 * 1024 * 1024
    const MAX_IMAGES = 5

    const lowerExt = (name: string) => {
      const i = name.lastIndexOf('.')
      return i >= 0 ? name.slice(i).toLowerCase() : ''
    }
    const classify = (f: File): 'image' | 'script' | 'audio' | 'unknown' => {
      if (IMAGE_MIME.includes(f.type)) return 'image'
      const ext = lowerExt(f.name)
      if (IMAGE_EXT.includes(ext)) return 'image'
      if (AUDIO_EXT.includes(ext) || f.type.startsWith('audio/')) return 'audio'
      if (SCRIPT_EXT.includes(ext)) return 'script'
      return 'unknown'
    }

    const validImages: File[] = []
    const validScripts: File[] = []
    for (const f of files) {
      const kind = classify(f)
      // 音频只在 MV 模式下有意义（拖拽进来也会走到这里）——直接把人送到 MV 面板，
      // 而不是甩一句"不支持的文件类型"让人不知道该怎么办
      if (kind === 'audio') {
        if (mode === 'mv') {
          setMvPanelOpen(true)
        } else {
          alert(`「${f.name}」是音频文件。要用一首歌生成 MV，请把左下角的模式切换到「MV」。`)
        }
        continue
      }
      if (kind === 'unknown') {
        alert(`不支持的文件类型：${f.name}（仅 png/jpg/webp/gif 或 txt/md/docx）`)
        continue
      }
      if (kind === 'image') {
        if (f.size > MAX_IMAGE_SIZE) {
          alert(`图片 ${f.name} 超过 8MB，跳过`)
          continue
        }
        validImages.push(f)
      } else {
        if (f.size > MAX_SCRIPT_SIZE) {
          alert(`文档 ${f.name} 超过 5MB，跳过`)
          continue
        }
        validScripts.push(f)
      }
    }

    if (validScripts.length > 1) {
      alert('一次只能上传 1 个剧本/文档（多个会让导演混淆）')
      return
    }
    if (pendingAttachments.filter((a) => a.kind !== 'script').length + validImages.length > MAX_IMAGES) {
      alert(`最多 ${MAX_IMAGES} 张参考图，已有 ${pendingAttachments.filter((a) => a.kind !== 'script').length} 张`)
      return
    }
    // 一次最多 1 个 script，已贴了 1 个就不再加
    if (validScripts.length && pendingAttachments.some((a) => a.kind === 'script')) {
      alert('已经贴了一个剧本/文档，先发送或移除再传新的')
      return
    }
    const all = [...validImages, ...validScripts]
    if (!all.length) return

    // 需要 project_id 来构造上传路径 —— 没有就先建一个空项目
    setUploading(true)
    try {
      let projectId = currentProject?.id
      if (!projectId) {
        // 0.7.0：expert 模式且没 project → 提示先建剧集（避免误建独立项目）
        if (mode === 'expert') {
          alert('专家模式下，请先在左侧「剧集」tab「+ 新建剧集」后再上传剧本')
          setUploading(false)
          return
        }
        const created = await projectAPI.create({
          message: '',
          description: '（用户上传了参考图先创建项目）',
          config: { mode },
        })
        const newProject = created.data
        setProjects([newProject, ...projects])
        setCurrentProject(newProject)
        projectId = newProject.id
        if (getWebSocketState() !== WebSocket.OPEN) {
          connectWebSocket(projectId!)
        }
      }

      const resp = await uploadAPI.uploadImages(projectId!, all)
      const uploaded: ChatAttachment[] = (resp.data?.attachments || []) as ChatAttachment[]
      setPendingAttachments((prev) => [...prev, ...uploaded])
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || String(err)
      alert(`上传失败：${detail}`)
    } finally {
      setUploading(false)
    }
  }

  const removePendingAttachment = (idx: number) => {
    setPendingAttachments((prev) => prev.filter((_, i) => i !== idx))
  }

  const setMode = (m: Mode) => {
    setModeState(m)
    try { localStorage.setItem(LS_MODE_KEY, m) } catch {}
  }

  // 切到一个已有项目时，把项目里的 mode 同步到 chat input 的状态
  useEffect(() => {
    const pmode = currentProject?.config?.mode
    if (pmode && (pmode === 'simple' || pmode === 'advanced' || pmode === 'expert')) {
      setModeState(pmode)
    }
  }, [currentProject?.id])

  // 关闭模式弹窗（点外部）
  useEffect(() => {
    if (!modeOpen) return
    const handler = (e: MouseEvent) => {
      if (modeMenuRef.current && !modeMenuRef.current.contains(e.target as Node)) {
        setModeOpen(false)
      }
    }
    window.addEventListener('mousedown', handler)
    return () => window.removeEventListener('mousedown', handler)
  }, [modeOpen])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  // 历史消息按钮的"已处理"判断：避免节点已确认后历史按钮还能误触
  const isAlreadyHandled = (msg: ChatMessage): boolean => {
    if (msg.type === 'node_completed' && msg.nodeId) {
      if (confirmedNodes.includes(msg.nodeId)) return true
      // 联动：节点卡/编辑面板确认也会同步 dag 节点的 confirmed/status，据此让聊天按钮一起变"已确认"
      const nodes = Array.isArray((dag as any)?.nodes)
        ? (dag as any).nodes
        : Object.values((dag as any)?.nodes || {})
      const node = nodes.find((n: any) => n?.id === msg.nodeId)
      const st = String(node?.status || '').toLowerCase()
      return !!node && (node.confirmed === true || st === 'confirmed')
    }
    if (msg.type === 'plan') {
      // 方案已确认 → 必然有 dag 节点或 phase 已推进
      // 注意：currentPhase 默认 'initial'，且从来不会被设成 'plan'（直接跳到 'plan_confirmed'/'script'），
      // 所以这里必须把 'initial' 也排除，否则方案按钮一出现就会被误判为"已确认"。
      const hasNodes = !!dag?.nodes && dag.nodes.length > 0
      const phaseAdvanced = !!currentPhase && currentPhase !== 'initial' && currentPhase !== 'plan'
      return hasNodes || phaseAdvanced
    }
    if (msg.type === 'all_videos_confirmed') {
      return currentPhase === 'merge' || currentPhase === 'completed'
    }
    if (msg.type === 'art_assets_all_confirmed') {
      // 已开始分镜 → 按钮 disable
      const hasStoryboard = !!dag?.nodes?.some((n: any) => n.type === 'storyboard_design')
      const phaseAfterArt = ['storyboard','keyframe','video','merge','publish','completed'].includes(currentPhase)
      return hasStoryboard || phaseAfterArt
    }
    if (msg.type === 'outline_draft') {
      // 进阶模式：剧本已经确认进入下游 → outline_draft 历史消息上的"确认剧本"按钮该 disabled
      const hasNodes = !!dag?.nodes && dag.nodes.length > 0
      const phaseAdvanced = !!currentPhase && currentPhase !== 'initial' && currentPhase !== 'plan'
      return hasNodes || phaseAdvanced
    }
    if (msg.type === 'episode_breakdown_draft') {
      // 0.7.0 专家模式：series.status 推进到 visual_dev 之后历史按钮 disable
      const cs = useStore.getState().currentSeries
      if (cs && cs.id === msg.seriesId) {
        return cs.status === 'visual_dev' || cs.status === 'producing' || cs.status === 'done'
      }
      return false
    }
    return false
  }

  // 0.7.0：确认分集 — 走 chat 让 detect_confirm_intent 触发
  const handleConfirmEpisodeBreakdown = async (projectId: string) => {
    const confirmText = '确认分集，开始下一步'
    addMessage({
      id: Date.now().toString(),
      role: 'user',
      content: confirmText,
      timestamp: Date.now(),
    })
    setIsTyping(true)
    try {
      const res = await workflowAPI.chat(confirmText, projectId)
      const data = res.data
      addMessage({
        id: (Date.now() + 1).toString(),
        role: 'agent',
        content: data.message || '已确认',
        timestamp: Date.now(),
        type: data.type,
        seriesId: data.series_id,
      })
    } catch (err) {
      addMessage({
        id: Date.now().toString(),
        role: 'agent',
        content: '确认失败，请重试',
        timestamp: Date.now(),
      })
    } finally {
      setIsTyping(false)
    }
  }

  // 确认进阶剧本大纲
  const handleConfirmAdvancedOutline = async (projectId: string) => {
    try {
      setIsTyping(true)
      const res = await workflowAPI.confirmAdvancedOutline(projectId)
      const data = res.data
      addMessage({
        id: Date.now().toString(),
        role: 'agent',
        content: data.message || '剧本已确认，开始创作！',
        timestamp: Date.now(),
        type: 'advanced_confirmed',
      })
      setCurrentPhase(data.phase || 'script')
    } catch (err) {
      addMessage({
        id: Date.now().toString(),
        role: 'agent',
        content: '确认失败，请重试',
        timestamp: Date.now(),
      })
    } finally {
      setIsTyping(false)
    }
  }

  const handleSend = async () => {
    // 1.1.0 MV：**还没开工**的 MV 项目才弹配置面板（输入是歌+图，没什么可"聊"的）。
    // 🔴 别只看 currentProject.config.mv —— 项目列表接口返回的 config 未必带 mv 字段，
    //    那会让已经在跑的项目每次打字都弹一次面板。以画布上有没有歌曲解析节点为准，
    //    这个信号在本地就有、不依赖接口返回什么。
    if (mode === 'mv') {
      const started =
        !!(currentProject?.config as any)?.mv ||
        ((dag?.nodes as any[]) || []).some(
          (n: any) => (n?.type || n?.data?.type) === 'mv_audio_analysis',
        )
      if (!started) {
        setMvPanelOpen(true)
        return
      }
    }
    // 允许"只有附件无文字"也能发：用户单纯传图征求 agent 看法
    // 0.6.2: 不再因为 isTyping 拒绝（之前若 agent 卡死按钮就锁死）。
    // 如果上一条还在路上，新请求并行发出去；后端各请求独立处理，前端按返回顺序 addMessage。
    if (!input.trim() && pendingAttachments.length === 0) return

    const attachmentsToSend = pendingAttachments.slice()
    const userMsg: ChatMessage = {
      id: Date.now().toString(),
      role: 'user',
      content: input.trim(),
      timestamp: Date.now(),
      attachments: attachmentsToSend.length ? attachmentsToSend : undefined,
    }
    addMessage(userMsg)
    setInput('')
    setPendingAttachments([])
    setIsTyping(true)

    try {
      // 自动创建项目（如果没有）
      let projectId = currentProject?.id
      if (!projectId) {
        const tempProject = await projectAPI.create({
          message: userMsg.content,  // 传入用户消息，后端会智能生成名称
          description: userMsg.content.slice(0, 100),
          config: { mode },  // 创作模式落到 project.config，阶段 ② RouterAgent 会读
        })
        const newProject = tempProject.data
        setProjects([newProject, ...projects])
        setCurrentProject(newProject)
        projectId = newProject.id
        if (getWebSocketState() !== WebSocket.OPEN) {
          connectWebSocket(projectId!)
        }
      }

      // 调用新的工作流 API
      const res = await workflowAPI.chat(userMsg.content, projectId!, {}, attachmentsToSend)
      const data = res.data

      // 构建 Agent 消息
      const agentMsg: ChatMessage = {
        id: (Date.now() + 1).toString(),
        role: 'agent',
        content: data.message || '',
        timestamp: Date.now(),
        type: data.type,
      }

      // 根据类型处理
      if (data.type === 'plan') {
        // 生成方案，需要创作者确认
        agentMsg.needConfirm = true
        agentMsg.projectId = projectId
        
        // 不再一次性设置全部节点到画布
        // 节点将通过 WebSocket 逐步推送
        console.log('[Chat] 方案已生成，等待确认后逐步创建节点')
        
      } else if (data.type === 'ask') {
        // 需要追问
        agentMsg.questions = data.questions || []
        agentMsg.defaultPlan = data.default_plan
        
        // 追问阶段不显示任何节点
        console.log('[Chat] 追问阶段，画布保持为空')
      } else if (data.type === 'execution_started') {
        // 执行已开始
        setCurrentPhase(data.phase || 'script')
        
        // 不再一次性设置全部节点，等待 WebSocket 推送
        console.log('[Chat] 执行已开始，等待节点通过 WebSocket 推送')
        
      } else if (data.type === 'node_created' || data.type === 'node_updated') {
        // 单个节点更新 — 已由 WebSocket 处理，这里不再重复设置
        console.log('[Chat] 节点更新已由 WebSocket 处理')
      } else if (data.type === 'outline_draft') {
        // 进阶模式：导演返回剧本草稿
        agentMsg.outline = data.outline
        agentMsg.readyToConfirm = !!data.ready_to_confirm
        agentMsg.missingAspects = data.missing_aspects || []
        agentMsg.projectId = projectId
      } else if (data.type === 'episode_breakdown_draft') {
        // 0.7.0 专家模式：分集草案
        agentMsg.type = 'episode_breakdown_draft'
        agentMsg.scriptSummary = data.script_summary
        agentMsg.episodes = data.episodes || []
        agentMsg.readyToConfirm = !!data.ready_to_confirm
        agentMsg.missingAspects = data.missing_aspects || []
        agentMsg.projectId = projectId
        agentMsg.seriesId = data.series_id
        // 同步到 store（让 sidebar / canvas 其它地方都能用）
        useStore.getState().setExpertBreakdown(data.script_summary, data.episodes || [])
      } else if (data.type === 'episode_breakdown_confirmed') {
        agentMsg.type = 'episode_breakdown_confirmed'
        agentMsg.episodes = data.episodes || []
        agentMsg.seriesId = data.series_id
      } else if (data.type === 'advanced_confirmed') {
        // 进阶模式：剧本已确认，进入下游
        setCurrentPhase(data.phase || 'script')
      } else if (data.type === 'creator_chat') {
        // 进阶模式 plan_confirmed 后：router 把消息路由到了某个 agent
        agentMsg.targetAgent = data.target_agent
        agentMsg.updatedNodes = data.updated_nodes || []
        agentMsg.rerunNodes = data.rerun_nodes || []
        agentMsg.appliedUpdates = data.applied_updates || []
      }

      addMessage(agentMsg)
    } catch (err) {
      addMessage({
        id: (Date.now() + 1).toString(),
        role: 'agent',
        content: '抱歉，处理出错了，请重试。',
        timestamp: Date.now(),
      })
    } finally {
      setIsTyping(false)
    }
  }

  // 确认方案
  const handleConfirmPlan = async (projectId: string) => {
    try {
      setIsTyping(true)
      const res = await workflowAPI.confirmPlan(projectId)
      const data = res.data

      addMessage({
        id: Date.now().toString(),
        role: 'agent',
        content: data.message || '方案已确认，开始创作！',
        timestamp: Date.now(),
        type: 'plan_confirmed',
      })

      setCurrentPhase('plan_confirmed')
      
      // 如果响应包含节点，更新 DAG
      if (data.node_id) {
        // 等待 WebSocket 推送新节点，不在这里手动添加
        // WebSocket 的 new_node 处理会正确添加节点
      }
    } catch (err) {
      addMessage({
        id: Date.now().toString(),
        role: 'agent',
        content: '确认失败：' + (err as Error).message,
        timestamp: Date.now(),
      })
    } finally {
      setIsTyping(false)
    }
  }

  // 确认节点
  const handleConfirmNode = async (projectId: string, nodeId: string) => {
    setIsTyping(true)
    try {
      await workflowAPI.nodeAction(projectId, nodeId, 'confirm')
      // 后续结果通过 WebSocket 推送，但 isTyping 在 API 返回时就该解锁
    } catch (err) {
      addMessage({
        id: Date.now().toString(),
        role: 'agent',
        content: '确认失败：' + (err as Error).message,
        timestamp: Date.now(),
      })
    } finally {
      setIsTyping(false)
    }
  }

  // 重跑节点
  const handleRerunNode = async (projectId: string, nodeId: string) => {
    setIsTyping(true)
    try {
      await workflowAPI.nodeAction(projectId, nodeId, 'rerun')
      addMessage({
        id: Date.now().toString(),
        role: 'agent',
        content: `🔄 节点正在重新执行...`,
        timestamp: Date.now(),
      })
    } catch (err) {
      addMessage({
        id: Date.now().toString(),
        role: 'agent',
        content: '重跑失败：' + (err as Error).message,
        timestamp: Date.now(),
      })
    } finally {
      setIsTyping(false)
    }
  }

  // 开始视频合并
  const handleStartMerge = async (projectId: string) => {
    setIsTyping(true)
    try {
      await workflowAPI.startMerge(projectId)
    } catch (err) {
      addMessage({
        id: Date.now().toString(),
        role: 'agent',
        content: '合并失败：' + (err as Error).message,
        timestamp: Date.now(),
      })
    } finally {
      setIsTyping(false)
    }
  }

  // 开始分镜设计（art 物料全确认后弹的对话框）
  const handleStartStoryboard = async (projectId: string) => {
    setIsTyping(true)
    try {
      await workflowAPI.startStoryboard(projectId)
    } catch (err) {
      addMessage({
        id: Date.now().toString(),
        role: 'agent',
        content: '开始分镜失败：' + (err as Error).message,
        timestamp: Date.now(),
      })
    } finally {
      setIsTyping(false)
    }
  }

  // 渲染消息内容
  const renderMessageContent = (msg: ChatMessage) => {
  return (
      <div className="space-y-2">
        {msg.content && <p className="whitespace-pre-wrap">{msg.content}</p>}

        {/* 附件缩略图 — 图片走缩略图，脚本/文档走文件 chip */}
        {msg.attachments && msg.attachments.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-1">
            {msg.attachments.map((a, i) => {
              const isScript = a.kind === 'script'
              if (isScript) {
                return (
                  <a
                    key={i}
                    href={a.url}
                    target="_blank"
                    rel="noreferrer"
                    title={`${a.name} · ${(a.size / 1024).toFixed(0)}KB${a.parsed_chars ? ` · ${a.parsed_chars} 字符` : ''}`}
                    className="inline-flex items-center gap-1.5 max-w-[200px] px-2 py-1 rounded-md border border-octopus-border bg-octopus-bg hover:border-octopus-primary/60 transition-colors"
                  >
                    <FileUp className="w-3.5 h-3.5 text-octopus-brand flex-shrink-0" />
                    <span className="truncate text-[11px] text-octopus-text">{a.name}</span>
                    {a.parsed_chars ? (
                      <span className="text-[10px] text-octopus-muted whitespace-nowrap">
                        · {(a.parsed_chars / 1000).toFixed(1)}k 字
                      </span>
                    ) : null}
                  </a>
                )
              }
              return (
                <a
                  key={i}
                  href={a.url}
                  target="_blank"
                  rel="noreferrer"
                  title={`${a.name} · ${(a.size / 1024).toFixed(0)}KB`}
                  className="block w-14 h-14 rounded-md overflow-hidden border border-octopus-border bg-octopus-bg hover:border-octopus-primary/60 transition-colors"
                >
                  <img
                    src={a.url}
                    alt={a.name}
                    className="w-full h-full object-cover"
                    onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.3' }}
                  />
                </a>
              )
            })}
          </div>
        )}

        {/* 方案确认按钮 */}
        {msg.type === 'plan' && msg.needConfirm && msg.projectId && (
          <div className="mt-3 flex gap-2">
            {isAlreadyHandled(msg) ? (
              <span className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-octopus-bg/40 text-octopus-muted text-sm border border-octopus-border cursor-not-allowed select-none">
                <CheckCircle className="w-4 h-4 text-green-500/60" />
                已确认
              </span>
            ) : (
              <button
                onClick={() => handleConfirmPlan(msg.projectId!)}
                className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 text-white text-sm font-medium shadow-[0_0_14px_rgba(52,211,153,0.45)] transition-all"
              >
                <CheckCircle className="w-4 h-4" />
                确认此方案
              </button>
            )}
          </div>
        )}

        {/* 节点完成 - 显示确认/重跑按钮 */}
        {msg.type === 'node_completed' && msg.needConfirm && msg.nodeId && (
          <div className="mt-3 flex gap-2">
            {isAlreadyHandled(msg) ? (
              <span className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-octopus-bg/40 text-octopus-muted text-sm border border-octopus-border cursor-not-allowed select-none">
                <CheckCircle className="w-4 h-4 text-green-500/60" />
                已确认
              </span>
            ) : (
              <>
                <button
                  onClick={() => handleConfirmNode(currentProject?.id!, msg.nodeId!)}
                  className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 text-white text-sm font-medium shadow-[0_0_14px_rgba(52,211,153,0.45)] transition-all"
                >
                  <CheckCircle className="w-4 h-4" />
                  确认结果
                </button>
                <button
                  onClick={() => handleRerunNode(currentProject?.id!, msg.nodeId!)}
                  className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-octopus-panel hover:bg-octopus-border text-octopus-text text-sm transition-colors border border-octopus-border"
                >
                  <RotateCcw className="w-4 h-4" />
                  重新生成
                </button>
              </>
            )}
          </div>
        )}

        {/* 所有美术物料确认 - 询问是否开始分镜 */}
        {msg.type === 'art_assets_all_confirmed' && msg.needDialogConfirm && (
          <div className="mt-3 flex gap-2">
            {isAlreadyHandled(msg) ? (
              <span className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-octopus-bg/40 text-octopus-muted text-sm border border-octopus-border cursor-not-allowed select-none">
                <CheckCircle className="w-4 h-4 text-green-500/60" />
                已开始
              </span>
            ) : (
              <>
                <button
                  onClick={() => handleStartStoryboard(currentProject?.id!)}
                  className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 text-white text-sm font-medium shadow-[0_0_14px_rgba(52,211,153,0.45)] transition-all"
                >
                  <CheckCircle className="w-4 h-4" />
                  开始分镜设计
                </button>
                <button
                  onClick={() => {
                    addMessage({
                      id: Date.now().toString(),
                      role: 'agent',
                      content: '好的，您可以继续调整角色/场景物料后再开始分镜。',
                      timestamp: Date.now(),
                    })
                  }}
                  className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-octopus-panel hover:bg-octopus-border text-octopus-text text-sm transition-colors border border-octopus-border"
                >
                  暂不开始
                </button>
              </>
            )}
          </div>
        )}

        {/* 所有视频确认 - 询问是否合并 */}
        {msg.type === 'all_videos_confirmed' && msg.needDialogConfirm && (
          <div className="mt-3 flex gap-2">
            {isAlreadyHandled(msg) ? (
              <span className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-octopus-bg/40 text-octopus-muted text-sm border border-octopus-border cursor-not-allowed select-none">
                <CheckCircle className="w-4 h-4 text-green-500/60" />
                已合并
              </span>
            ) : (
              <>
                <button
                  onClick={() => handleStartMerge(currentProject?.id!)}
                  className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 text-white text-sm font-medium shadow-[0_0_14px_rgba(52,211,153,0.45)] transition-all"
                >
                  <CheckCircle className="w-4 h-4" />
                  确认合并
                </button>
                <button
                  onClick={() => {
                    addMessage({
                      id: Date.now().toString(),
                      role: 'agent',
                      content: '好的，您可以继续调整视频后再合并。',
                      timestamp: Date.now(),
                    })
                  }}
                  className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-octopus-panel hover:bg-octopus-border text-octopus-text text-sm transition-colors border border-octopus-border"
                >
                  暂不合并
                </button>
              </>
            )}
          </div>
        )}

        {/* 进阶模式：剧本大纲草稿 */}
        {msg.type === 'outline_draft' && msg.outline && (
          <div className="mt-3 p-3 rounded-lg bg-octopus-bg/50 border border-octopus-border space-y-2">
            <div className="flex items-center justify-between">
              <div className="text-xs text-octopus-muted flex items-center gap-1.5">
                <Bot className="w-3 h-3" /> 剧本大纲 · 草稿
              </div>
              {msg.readyToConfirm && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-600/20 text-green-300 border border-green-600/40">
                  已可确认
                </span>
              )}
            </div>
            {msg.outline.title && (
              <div className="text-sm font-semibold text-octopus-text">
                《{msg.outline.title}》
                {msg.outline.tone && (
                  <span className="ml-2 text-[10px] text-octopus-muted">[{msg.outline.tone}]</span>
                )}
              </div>
            )}
            {msg.outline.logline && (
              <div className="text-xs text-octopus-chat italic">{msg.outline.logline}</div>
            )}
            {msg.outline.characters && msg.outline.characters.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {msg.outline.characters.map((c, i) => (
                  <span
                    key={i}
                    title={c.description}
                    className="px-1.5 py-0.5 rounded-full bg-octopus-primary/15 text-octopus-primary text-[10px]"
                  >
                    {c.name}{c.role ? ` · ${c.role}` : ''}
                  </span>
                ))}
              </div>
            )}
            {msg.outline.scenes && msg.outline.scenes.length > 0 && (
              <div className="space-y-1 mt-1">
                {msg.outline.scenes.map((s, i) => (
                  <div key={i} className="text-[11px] text-octopus-text flex items-baseline gap-1.5">
                    <span className="font-mono text-octopus-muted">{s.scene_id || `S${i + 1}`}</span>
                    {s.location && <span className="text-octopus-muted">[{s.location}]</span>}
                    {s.duration_seconds != null && (
                      <span className="text-octopus-muted">{s.duration_seconds}s</span>
                    )}
                    <span className="flex-1 truncate">{s.key_action || s.beat || ''}</span>
                  </div>
                ))}
              </div>
            )}
            {msg.missingAspects && msg.missingAspects.length > 0 && !msg.readyToConfirm && (
              <div className="text-[10px] text-octopus-muted pt-1 border-t border-octopus-border">
                还需确认：{msg.missingAspects.join('、')}
              </div>
            )}

            {/* 确认按钮 */}
            <div className="pt-2 flex gap-2">
              {isAlreadyHandled(msg) ? (
                <span className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-octopus-bg/40 text-octopus-muted text-xs border border-octopus-border cursor-not-allowed select-none">
                  <CheckCircle className="w-3.5 h-3.5 text-green-500/60" />
                  已确认
                </span>
              ) : msg.readyToConfirm ? (
                <button
                  onClick={() => handleConfirmAdvancedOutline(msg.projectId || currentProject?.id!)}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 text-white text-xs font-medium shadow-[0_0_12px_rgba(52,211,153,0.45)] transition-all"
                >
                  <CheckCircle className="w-3.5 h-3.5" />
                  确认剧本，开始美术
                </button>
              ) : (
                <span className="text-[10px] text-octopus-muted">
                  继续跟导演聊聊，完善剧本后再确认
                </span>
              )}
            </div>
          </div>
        )}

        {/* 0.7.0 专家模式：分集草案卡片 */}
        {msg.type === 'episode_breakdown_draft' && (msg.episodes?.length ?? 0) > 0 && (
          <EpisodeBreakdownCard
            scriptSummary={msg.scriptSummary}
            episodes={msg.episodes || []}
            readyToConfirm={msg.readyToConfirm}
            missingAspects={msg.missingAspects}
            isAlreadyConfirmed={isAlreadyHandled(msg)}
            onConfirm={() => handleConfirmEpisodeBreakdown(msg.projectId || currentProject?.id!)}
          />
        )}

        {/* 进阶模式协作：creator_chat — router 把消息派给了某个 agent，可能改了节点 */}
        {msg.type === 'creator_chat' && msg.targetAgent && (
          <div className="mt-2 space-y-1.5">
            <div className="flex items-start gap-2">
              {(() => {
                const info = AGENT_INFO[msg.targetAgent!] || AGENT_INFO.director
                const Icon = info.icon
                return (
                  <div className={`flex-shrink-0 flex items-center gap-1 px-2 py-0.5 rounded-full bg-octopus-bg/60 border border-octopus-border text-[10px] ${info.color}`}>
                    <Icon className="w-3 h-3" />
                    <span>{info.label}</span>
                  </div>
                )
              })()}
              {msg.updatedNodes && msg.updatedNodes.length > 0 && (
                <div className="flex-1 min-w-0 text-[10px] text-octopus-muted flex items-center gap-1">
                  {msg.rerunNodes && msg.rerunNodes.length > 0 ? (
                    <RefreshCw className="w-3 h-3 animate-spin text-cyan-400/70" />
                  ) : null}
                  <span>
                    已修改 {msg.updatedNodes.length} 个节点
                    {msg.rerunNodes && msg.rerunNodes.length > 0
                      ? `，${msg.rerunNodes.length} 个正在重跑`
                      : ''}
                  </span>
                </div>
              )}
            </div>

            {/* "查看 prompt" 展开 — debug/透明用 */}
            {msg.appliedUpdates && msg.appliedUpdates.length > 0 && (
              <div>
                <button
                  onClick={() => togglePromptExpand(msg.id)}
                  className="text-[10px] text-cyan-300/70 hover:text-cyan-200 underline-offset-2 hover:underline flex items-center gap-1"
                >
                  <ChevronDown className={`w-3 h-3 transition-transform ${expandedPrompts.has(msg.id) ? 'rotate-180' : ''}`} />
                  {expandedPrompts.has(msg.id) ? '收起 prompt' : '查看发送给生图模型的 prompt'}
                </button>
                {expandedPrompts.has(msg.id) && (
                  <div className="mt-1.5 space-y-2 p-2 rounded-lg bg-octopus-bg/40 border border-octopus-border text-[10px]">
                    {msg.appliedUpdates.map((u, i) => (
                      <div key={i} className="space-y-1">
                        <div className="text-octopus-muted">
                          节点 <span className="font-mono text-octopus-text">{u.node_id}</span>
                          {u.should_rerun ? <span className="ml-1.5 text-cyan-400">· 已重跑</span> : <span className="ml-1.5">· 仅改 params</span>}
                        </div>
                        {u.prompt && (
                          <div className="font-mono text-[10px] text-octopus-text/80 whitespace-pre-wrap break-words leading-snug bg-black/30 rounded p-1.5 border border-octopus-border/50">
                            {u.prompt}
                          </div>
                        )}
                        {u.reference_image_urls && u.reference_image_urls.length > 0 && (
                          <div className="text-octopus-muted">
                            参考图 ({u.reference_image_urls.length}):{' '}
                            {u.reference_image_urls.map((url, j) => (
                              <a key={j} href={url} target="_blank" rel="noreferrer" className="text-cyan-300/70 hover:text-cyan-200 mr-1">
                                图{j + 1}
                              </a>
                            ))}
                          </div>
                        )}
                        {u.reason && (
                          <div className="text-octopus-muted italic">理由：{u.reason}</div>
                        )}
                        {u.other_fields && Object.keys(u.other_fields).length > 0 && (
                          <div className="text-octopus-muted">
                            其他字段：{Object.keys(u.other_fields).join(', ')}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* 追问问题 */}
        {msg.questions && msg.questions.length > 0 && (
          <div className="mt-2 space-y-1">
            <div className="text-xs text-octopus-muted">💬 您可以回答以下问题，或直接确认上方方案</div>
            {msg.questions.map((q, i) => (
              <div key={i} className="flex items-start gap-2 text-sm">
                <HelpCircle className="w-4 h-4 text-octopus-accent mt-0.5 flex-shrink-0" />
                <span className="text-octopus-chat">{q}</span>
              </div>
            ))}
          </div>
        )}

        {/* 默认方案展示 */}
        {msg.defaultPlan && (
          <div className="mt-2 p-3 rounded-lg bg-octopus-bg/50 border border-octopus-border">
            <div className="text-xs text-octopus-muted mb-2">📋 默认方案</div>
            <div className="space-y-1 text-sm">
              {msg.defaultPlan.theme && (
                <div className="flex gap-2">
                  <span className="text-octopus-muted">主题:</span>
                  <span className="text-octopus-text">{msg.defaultPlan.theme}</span>
                </div>
              )}
              {msg.defaultPlan.style && (
                <div className="flex gap-2">
                  <span className="text-octopus-muted">风格:</span>
                  <span className="text-octopus-text">{msg.defaultPlan.style}</span>
                </div>
              )}
              {msg.defaultPlan.duration && (
                <div className="flex gap-2">
                  <span className="text-octopus-muted">时长:</span>
                  <span className="text-octopus-text">{msg.defaultPlan.duration}</span>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    )
  }

  // 歌词校对：从画布节点里取解析结果（lyrics / duration / 音频路径 / 段落）
  const lyricNode = lyricNodeId
    ? ((dag?.nodes || []) as any[]).find(
        (n: any) => n?.id === lyricNodeId || n?.data?.id === lyricNodeId ||
                    (n?.type || n?.data?.type) === 'mv_audio_analysis',
      )
    : null
  const lyricResult = (lyricNode?.result || lyricNode?.data?.result) || {}
  const lyricPanel = lyricNodeId && Array.isArray(lyricResult.lyrics) && lyricResult.lyrics.length ? (
    <LyricTimelineEditor
      projectId={currentProject?.id || ''}
      audioUrl={lyricResult.audio_path || (currentProject?.config as any)?.mv?.audio_path || ''}
      duration={Number(lyricResult.duration || 0)}
      lines={lyricResult.lyrics}
      sections={(lyricResult.song || {}).sections}
      onClose={() => setLyricNodeId(null)}
      onSaved={() => {
        addMessage({
          id: `mvlyr_${Date.now()}`,
          role: 'agent',
          content: '✅ 歌词时间轴已更新。重跑「MV 分镜」就会按新的时间轴切镜头。',
          timestamp: Date.now(),
        })
      }}
    />
  ) : null

  const mvPanel = mvPanelOpen ? (
    <MVSetupPanel
      onClose={() => setMvPanelOpen(false)}
      onStarted={(pid, res) => {
        // 歌曲解析节点已在后端建好并开跑，这里只把它画到画布上
        if (res?.node) {
          setDag({ ...(dag || {}), nodes: [...((dag?.nodes as any[]) || []), res.node] } as any)
        }
        addMessage({
          id: `mv_${Date.now()}`,
          role: 'agent',
          content: res?.message || '🎵 开始解析歌曲…',
          timestamp: Date.now(),
        })
        setCurrentPhase('mv_audio')
      }}
    />
  ) : null
  return (
    <div className="h-full w-full flex flex-col p-3">
      {mvPanel}
      {lyricPanel}
      {/* 聊天栏标题：优先显示正在运行节点的活动，否则回退到最近发言 agent */}
      {(() => {
        const NODE_ACTIVITY: Record<string, { label: string; color: string; icon: any }> = {
          script_generation:  { label: '编剧 · 生成中',   color: 'text-purple-300', icon: PenLine },
          art_design:         { label: '美术 · 生成中',   color: 'text-pink-300',   icon: Palette },
          episode_art_design: { label: '美术 · 生成中',   color: 'text-pink-300',   icon: Palette },
          storyboard_design:  { label: '分镜 · 生成中',   color: 'text-orange-300', icon: Film },
          keyframe_generation:{ label: '关键帧 · 生成中', color: 'text-yellow-300', icon: ImageIcon },
          video_generation:   { label: '视频 · 生成中',   color: 'text-cyan-300',   icon: Film },
        }
        const nodeList: any[] = Array.isArray((dag as any)?.nodes)
          ? (dag as any).nodes
          : Object.values((dag as any)?.nodes || {})
        const running = nodeList.find((n) => String(n?.status).toLowerCase() === 'running')
        const act = running ? NODE_ACTIVITY[running.type] : null
        const lastAgent = [...messages].reverse().find((m) => m.role === 'agent' && !!m.agent)
        const base = (lastAgent?.agent && AGENT_INFO[lastAgent.agent]) || AGENT_INFO.director
        const Icon = act?.icon || base.icon
        const label = act?.label || base.label
        const color = act?.color || base.color
        return (
          <div className="flex items-center gap-2.5 px-1 pb-2.5 mb-2 border-b border-octopus-border/70 flex-shrink-0">
            <div className="w-8 h-8 rounded-lg bg-octopus-primary/15 border border-octopus-primary/25 flex items-center justify-center flex-shrink-0">
              <Icon className={`w-4 h-4 ${color}`} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold text-octopus-text leading-tight flex items-center gap-1.5">
                {label}
                {!act && <span className="text-[10px] font-normal text-octopus-muted">· AI 协作台</span>}
              </div>
              <div className="text-[11px] text-octopus-muted truncate">
                {currentProject?.name || '未选择项目'}
              </div>
            </div>
            {(isTyping || running) && <Loader2 className="w-3.5 h-3.5 text-octopus-primary animate-spin flex-shrink-0" />}
          </div>
        )
      })()}

      {/* Message history */}
      <div
        ref={scrollRef}
        className="flex-1 min-h-0 overflow-y-auto mb-2 px-1 space-y-2 scrollbar-hide"
      >
        {messages.map((msg, idx) => (
          <div
            key={`${msg.id}-${idx}`}
            className={`flex gap-2 ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}
          >
            <div className="flex-shrink-0 w-7 h-7 flex items-center justify-center">
              {msg.role === 'user' ? (
                <DirectorIcon size={22} className="text-octopus-chat" />
              ) : (
                <OctopusIcon size={22} className="text-octopus-accent" />
              )}
            </div>
            <div
              className={`max-w-[80%] rounded-xl px-4 py-2 text-sm ${
                msg.role === 'user'
                  ? 'bg-octopus-primary/20 text-octopus-chat'
                  : 'bg-octopus-panel/80 backdrop-blur text-octopus-chat border border-octopus-border'
              }`}
            >
              {renderMessageContent(msg)}
            </div>
          </div>
        ))}
        {isTyping && (
          <div className="flex gap-2">
            <div className="w-7 h-7 flex items-center justify-center">
              <OctopusIcon size={22} className="text-octopus-accent" />
            </div>
            <div className="bg-octopus-panel/80 backdrop-blur border border-octopus-border rounded-xl px-4 py-2">
              <Loader2 className="w-4 h-4 text-octopus-muted animate-spin" />
            </div>
          </div>
        )}
      </div>

      {/* 隐藏的文件 input — 通过 + 按钮触发 */}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/png,image/jpeg,image/jpg,image/webp,image/gif,text/plain,text/markdown,.txt,.md,.markdown,.docx,.doc,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        multiple
        onChange={handleFilesSelected}
        className="hidden"
      />

      {/* Pending 附件 chips（在 input bar 上方）— 图片/脚本两种 chip 形态 */}
      {pendingAttachments.length > 0 && (() => {
        const imgs = pendingAttachments.filter((a) => a.kind !== 'script')
        const scripts = pendingAttachments.filter((a) => a.kind === 'script')
        const label = (() => {
          if (imgs.length && scripts.length) return `剧本 + 参考图（${pendingAttachments.length}）`
          if (scripts.length) return `剧本/文档（${scripts.length}/1）`
          return `参考图（${imgs.length}/5）`
        })()
        return (
          <div className="bg-octopus-panel/80 backdrop-blur border border-octopus-border rounded-xl p-2 flex flex-wrap items-center gap-2 mb-1 shadow-lg">
            <div className="text-[10px] text-octopus-muted whitespace-nowrap">
              {label}
            </div>
            {pendingAttachments.map((a, i) => {
              const isScript = a.kind === 'script'
              if (isScript) {
                return (
                  <div
                    key={i}
                    className="relative group flex-shrink-0 inline-flex items-center gap-1.5 max-w-[200px] pl-2 pr-3 py-1 rounded-md bg-octopus-bg border border-octopus-border"
                    title={`${a.name} · ${(a.size / 1024).toFixed(0)}KB${a.parsed_chars ? ` · ${a.parsed_chars} 字符` : ''}`}
                  >
                    <FileUp className="w-3.5 h-3.5 text-octopus-brand flex-shrink-0" />
                    <span className="truncate text-[11px] text-octopus-text">{a.name}</span>
                    {a.parsed_chars ? (
                      <span className="text-[10px] text-octopus-muted whitespace-nowrap">
                        · {(a.parsed_chars / 1000).toFixed(1)}k 字
                      </span>
                    ) : null}
                    <button
                      onClick={() => removePendingAttachment(i)}
                      className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-red-500/90 hover:bg-red-500 text-white flex items-center justify-center opacity-80 hover:opacity-100"
                    >
                      <X className="w-2.5 h-2.5" />
                    </button>
                  </div>
                )
              }
              return (
                <div
                  key={i}
                  className="relative group flex-shrink-0"
                  title={`${a.name} · ${(a.size / 1024).toFixed(0)}KB`}
                >
                  <img
                    src={a.url}
                    alt={a.name}
                    className="w-10 h-10 rounded-md object-cover border border-octopus-border"
                  />
                  <button
                    onClick={() => removePendingAttachment(i)}
                    className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-red-500/90 hover:bg-red-500 text-white flex items-center justify-center opacity-80 hover:opacity-100"
                  >
                    <X className="w-2.5 h-2.5" />
                  </button>
                </div>
              )
            })}
            {uploading && (
              <Loader2 className="w-3 h-3 text-octopus-muted animate-spin" />
            )}
          </div>
        )
      })()}

      {/* Input bar */}
      <div className="bg-octopus-panel/90 backdrop-blur-xl border border-octopus-border rounded-2xl p-2 flex items-center gap-2 shadow-2xl">
        {/* + 按钮：上传图片（独立于 chat 状态，等 agent 回复时也能预上传）*/}
        <button
          onClick={handlePickFiles}
          disabled={uploading || pendingAttachments.length >= 5}
          title={pendingAttachments.length >= 5 ? '最多 5 张参考图' : '上传参考图（最多 5 张）'}
          className="w-9 h-9 flex-shrink-0 rounded-xl border border-octopus-border bg-octopus-bg/40 hover:bg-octopus-bg disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center transition-colors"
        >
          {uploading ? (
            <Loader2 className="w-4 h-4 text-octopus-muted animate-spin" />
          ) : (
            <Plus className="w-4 h-4 text-octopus-muted" />
          )}
        </button>

        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSend()}
          placeholder={(() => {
            if (mode === 'mv') {
              return (currentProject?.config as any)?.mv
                ? 'MV 生成中——歌曲解析完可在画布上校对歌词时间轴'
                : '点 + 上传歌曲开始生成 MV（或直接回车打开配置面板）...'
            }
            if (mode === 'expert') {
              return '点 + 上传剧本/小说（.txt / .md / .docx），让 AI 帮你分集制作...'
            }
            if (mode !== 'advanced') return '告诉导演你想创作什么...'
            // advanced 模式按 phase 给提示
            if (!currentPhase || currentPhase === 'initial' || currentPhase === 'plan_confirmed') {
              return '跟导演聊聊你的故事想法...'
            }
            const hints: Record<string, string> = {
              script: '剧本进行中，可以让导演调整方向',
              art: '试试：@美术 把橘猫眼睛改大一点',
              storyboard: '试试：@分镜 SHOT_03 加推镜头',
              keyframe: '试试：@美术 这帧再加点夕阳光',
              video: '试试：@分镜 重新生成 SHOT_02',
              merge: '可以让导演调整合成顺序',
              publish: '试试：@发布 标题改短点',
            }
            return hints[currentPhase] || '跟相应的 agent 协作完善作品...'
          })()}
          className="flex-1 min-w-0 bg-transparent text-octopus-text placeholder-octopus-muted px-3 py-2.5 text-sm outline-none"
        />

        {/* 创作模式下拉 */}
        <div className="relative flex-shrink-0" ref={modeMenuRef}>
          <button
            onClick={() => setModeOpen((o) => !o)}
            title={MODE_OPTIONS.find((m) => m.value === mode)?.hint}
            className="h-9 px-2.5 rounded-xl border border-octopus-border bg-octopus-bg/40 hover:bg-octopus-bg text-octopus-text text-xs flex items-center gap-1 whitespace-nowrap flex-shrink-0 transition-colors"
          >
            {(() => {
              const m = MODE_OPTIONS.find((x) => x.value === mode) || MODE_OPTIONS[0]
              const Icon = m.icon
              return (
                <>
                  <Icon className="w-3 h-3 text-octopus-primary" />
                  <span>{m.label}</span>
                  <ChevronDown className={`w-3 h-3 text-octopus-muted transition-transform ${modeOpen ? 'rotate-180' : ''}`} />
                </>
              )
            })()}
          </button>
          {modeOpen && (
            <div className="absolute bottom-full right-0 mb-2 w-52 rounded-xl border border-octopus-border bg-octopus-panel/95 backdrop-blur-xl shadow-2xl overflow-hidden z-20">
              <div className="px-3 py-2 text-[10px] text-octopus-muted tracking-wider uppercase border-b border-octopus-border">
                创作模式
              </div>
              {MODE_OPTIONS.map((opt) => {
                const Icon = opt.icon
                const selected = opt.value === mode
                return (
                  <button
                    key={opt.value}
                    disabled={opt.disabled}
                    onClick={() => {
                      if (opt.disabled) return
                      setMode(opt.value)
                      setModeOpen(false)
                    }}
                    className={`w-full px-3 py-2 flex items-start gap-2 text-left text-xs transition-colors ${
                      opt.disabled
                        ? 'opacity-40 cursor-not-allowed'
                        : selected
                        ? 'bg-octopus-primary/15 text-octopus-text'
                        : 'text-octopus-text hover:bg-white/5'
                    }`}
                  >
                    <Icon className={`w-3.5 h-3.5 mt-0.5 flex-shrink-0 ${selected ? 'text-octopus-primary' : 'text-octopus-muted'}`} />
                    <span className="flex-1 min-w-0">
                      <span className="block">
                        {opt.label}
                        {opt.disabled && <span className="ml-1 text-[10px] text-octopus-muted">(0.7.0)</span>}
                      </span>
                      {opt.hint && (
                        <span className="block text-[10px] text-octopus-muted mt-0.5 leading-snug">
                          {opt.hint}
                        </span>
                      )}
                    </span>
                    {selected && !opt.disabled && (
                      <CheckCircle className="w-3 h-3 text-octopus-primary mt-1 flex-shrink-0" />
                    )}
                  </button>
                )
              })}
            </div>
          )}
        </div>

        <button
          onClick={handleSend}
          disabled={(!input.trim() && pendingAttachments.length === 0) || uploading}
          title={isTyping ? "上一条还在处理中（可强制再发）" : "发送"}
          className={`w-9 h-9 flex-shrink-0 rounded-xl bg-gradient-to-br from-cyan-400 to-violet-500 hover:from-cyan-300 hover:to-violet-400 shadow-[0_0_12px_rgba(34,211,238,0.5)] disabled:opacity-40 disabled:shadow-none flex items-center justify-center transition-all ${isTyping ? 'ring-2 ring-amber-400/40' : ''}`}
        >
          <Send className={`w-4 h-4 text-white ${isTyping ? 'animate-pulse' : ''}`} />
        </button>
      </div>
    </div>
  )
}
