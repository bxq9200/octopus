/**
 * 节点编辑面板 — 按节点类型分发到专门编辑器
 */
import React, { useState } from 'react'
import { X, RotateCcw, CheckCircle, Settings2, Play } from 'lucide-react'
import { useStore } from '@/store/useStore'
import { workflowAPI } from '@/services/api'
import StoryboardEditor from './editors/StoryboardEditor'
import ScriptEditor from './editors/ScriptEditor'
import ArtEditor from './editors/ArtEditor'
import ResultChildEditor from './editors/ResultChildEditor'
import MergeEditor from './editors/MergeEditor'
import KeyframeEditor from './editors/KeyframeEditor'
import PublishEditor from './editors/PublishEditor'
import CostSummaryEditor from './editors/CostSummaryEditor'
import AudioPlanEditor from './editors/AudioPlanEditor'
import BgmFetchEditor from './editors/BgmFetchEditor'
import EpisodeBreakdownEditor from './editors/EpisodeBreakdownEditor'
import EpisodeArtEditor from './editors/EpisodeArtEditor'
import EpisodeAudioEditor from './editors/EpisodeAudioEditor'
import BgmDirectionEditor from './editors/BgmDirectionEditor'
import GenericEditor from './editors/GenericEditor'
import LocalizationEditor from './editors/LocalizationEditor'

const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  idle: { label: '等待中', cls: 'bg-gray-800 text-gray-400' },
  running: { label: '执行中', cls: 'bg-yellow-900/30 text-yellow-400' },
  completed: { label: '待确认', cls: 'bg-blue-900/30 text-blue-400' },
  confirmed: { label: '已确认', cls: 'bg-green-900/30 text-green-400' },
  error: { label: '失败', cls: 'bg-red-900/30 text-red-400' },
  skipped: { label: '已跳过', cls: 'bg-gray-800 text-gray-500' },
}

export default function NodeEditorPanel() {
  const selectedNodeId = useStore((s) => s.selectedNode)
  const dag = useStore((s) => s.dag)
  const setSelectedNode = useStore((s) => s.setSelectedNode)
  const currentProject = useStore((s) => s.currentProject)
  const addMessage = useStore((s) => s.addMessage)
  const setIsTyping = useStore((s) => s.setIsTyping)
  const updateNode = useStore((s) => s.updateNode)

  const node = dag?.nodes?.find((n) => n.id === selectedNodeId)
  const [busy, setBusy] = useState(false)

  if (!node) return null
  const status = (node.status || 'idle') as keyof typeof STATUS_BADGE
  const badge = STATUS_BADGE[status] || STATUS_BADGE.idle
  const readOnly = status === 'running'

  // 0.6.2: bgm_fetch 节点的 title 跟着 result.provider 走（创建时 label 是历史快照可能不对）
  let displayLabel: string = node.label || ''
  if (node.type === 'bgm_fetch') {
    const provider = (node.result as any)?.provider
    const provLabel: Record<string, string> = { elevenlabs: 'ElevenLabs', mubert: 'Mubert' }
    if (provider && provLabel[provider]) {
      displayLabel = `BGM 生成 (${provLabel[provider]})`
    }
  }

  // 调 backend `node-action edit` 更新 params
  const saveParams = async (newParams: any) => {
    if (!currentProject?.id) return
    // 后端约定：params 顶层包含 'params'/'result'/'outputs' key 才走对应字段
    await workflowAPI.nodeAction(currentProject.id, node.id, 'edit', { params: newParams })
    updateNode(node.id, { params: newParams })
    addMessage({
      id: `edit_${Date.now()}`,
      role: 'agent',
      content: `✏️ 已更新节点「${node.label}」参数`,
      timestamp: Date.now(),
    })
  }

  // 调 backend 更新 result（用于剧本/美术/分镜的结构化编辑）
  const saveResult = async (newResult: any) => {
    if (!currentProject?.id) return
    await workflowAPI.nodeAction(currentProject.id, node.id, 'edit', { result: newResult })
    updateNode(node.id, { result: newResult })
    addMessage({
      id: `edit_${Date.now()}`,
      role: 'agent',
      content: `✏️ 已更新节点「${node.label}」内容`,
      timestamp: Date.now(),
    })
  }

  const handleConfirm = async () => {
    if (!currentProject?.id) return
    setBusy(true)
    setIsTyping(true)
    try {
      await workflowAPI.nodeAction(currentProject.id, node.id, 'confirm')
      setSelectedNode(null)
    } catch (err) {
      addMessage({
        id: `err_${Date.now()}`,
        role: 'agent',
        content: '确认失败：' + (err as Error).message,
        timestamp: Date.now(),
      })
    } finally {
      setBusy(false)
      setIsTyping(false)
    }
  }

  const handleRerun = async () => {
    if (!currentProject?.id) return
    setBusy(true)
    setIsTyping(true)
    try {
      await workflowAPI.nodeAction(currentProject.id, node.id, 'rerun')
      addMessage({
        id: `rerun_${Date.now()}`,
        role: 'agent',
        content: `🔄 节点「${node.label}」正在重新执行...`,
        timestamp: Date.now(),
      })
    } catch (err) {
      addMessage({
        id: `err_${Date.now()}`,
        role: 'agent',
        content: '重跑失败：' + (err as Error).message,
        timestamp: Date.now(),
      })
    } finally {
      setBusy(false)
      setIsTyping(false)
    }
  }

  // 按类型选编辑器
  const renderEditor = () => {
    switch (node.type) {
      case 'script_generation':
        return <ScriptEditor node={node} onSave={saveResult} readOnly={readOnly} />
      case 'art_design':
        return <ArtEditor node={node} onSave={saveResult} readOnly={readOnly} />
      case 'storyboard_design':
        return <StoryboardEditor node={node} onSave={saveResult} readOnly={readOnly} />
      case 'result':
        return (
          <ResultChildEditor
            node={node}
            onSave={saveResult}
            onRegenerate={handleRerun}
            readOnly={readOnly}
          />
        )
      case 'video_merge':
        return <MergeEditor node={node} onSaveParams={saveParams} readOnly={readOnly} />
      case 'keyframe_generation':
        return <KeyframeEditor node={node} onSaveParams={saveParams} readOnly={readOnly} />
      case 'publish':
        return <PublishEditor node={node} readOnly={readOnly} />
      case 'cost_summary':
        return <CostSummaryEditor node={node} readOnly={readOnly} />
      case 'audio_plan':
        return <AudioPlanEditor node={node} onSave={saveResult} readOnly={readOnly} />
      case 'bgm_fetch':
        return <BgmFetchEditor node={node} readOnly={readOnly} />
      case 'episode_breakdown':
        return <EpisodeBreakdownEditor node={node} />
      case 'episode_art_design':
        return <EpisodeArtEditor node={node} />
      case 'episode_audio_plan':
        return <EpisodeAudioEditor node={node} />
      case 'bgm_direction':
        return <BgmDirectionEditor node={node} />
      case 'localization':
        return <LocalizationEditor node={node} readOnly={readOnly} />
      default:
        return <GenericEditor node={node} onSaveParams={saveParams} readOnly={readOnly} />
    }
  }

  return (
    <div className="absolute right-4 top-4 w-[420px] max-h-[calc(100vh-2rem)] bg-[#141620]/97 backdrop-blur-xl border border-octopus-border rounded-2xl shadow-2xl z-40 flex flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-octopus-border">
        <div className="flex items-center gap-2 min-w-0">
          <Settings2 className="w-4 h-4 text-octopus-primary flex-shrink-0" />
          <span className="text-sm font-medium text-octopus-text truncate">{displayLabel}</span>
          <span className={`text-xs px-2 py-0.5 rounded-full border border-octopus-border flex-shrink-0 ${badge.cls}`}>
            {badge.label}
          </span>
        </div>
        <button
          onClick={() => setSelectedNode(null)}
          title="关闭"
          className="w-7 h-7 flex items-center justify-center rounded-lg bg-octopus-bg/70 border border-octopus-border text-octopus-text hover:bg-rose-500/20 hover:text-rose-300 hover:border-rose-500/40 transition-colors flex-shrink-0"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Meta info */}
      <div className="px-4 py-2 border-b border-octopus-border text-[10px] text-octopus-muted space-y-0.5">
        <div>ID: <span className="text-octopus-text/70 font-mono">{node.id}</span></div>
        <div>类型: <span className="text-octopus-text/70">{node.type}</span>{node.agent && <span className="ml-2">Agent: {node.agent}</span>}</div>
        {node.depends_on && node.depends_on.length > 0 && (
          <div>依赖: <span className="text-octopus-text/70">{node.depends_on.length} 个</span></div>
        )}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4">
        {node.error && (
          <div className="p-3 mb-3 rounded-lg bg-red-900/20 border border-red-500/30 text-red-300 text-xs">
            ❌ {node.error}
          </div>
        )}
        {renderEditor()}
      </div>

      {/* Footer */}
      {node.type === 'episode_breakdown' ? (
        <div className="px-4 py-3 border-t border-octopus-border text-[10px] text-octopus-muted text-center">
          展示节点，不可单独重跑。要改分集回 chat 跟导演说。
        </div>
      ) : (
        <div className="px-4 py-3 border-t border-octopus-border flex gap-2">
          {status === 'completed' && (
            <button
              onClick={handleConfirm}
              disabled={busy}
              className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 shadow-[0_0_14px_rgba(52,211,153,0.45)] disabled:opacity-40 disabled:shadow-none text-white text-sm font-medium transition-all"
            >
              <CheckCircle className="w-3.5 h-3.5" />
              确认结果
            </button>
          )}
          {node.type !== 'localization' && (
          <button
            onClick={handleRerun}
            disabled={busy || readOnly || !currentProject}
            className={`${status === 'completed' ? 'flex-1' : 'w-full'} flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-sm transition-all disabled:opacity-40 disabled:shadow-none ${
              status === 'idle'
                ? 'bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 text-white font-medium shadow-[0_0_14px_rgba(52,211,153,0.45)]'
                : 'bg-octopus-bg hover:bg-white/5 text-octopus-text border border-octopus-border'
            }`}
            title={status === 'idle' ? '生成此节点' : '重新生成此节点'}
          >
            {status === 'idle' ? (
              <Play className={`w-3.5 h-3.5 ${busy ? 'animate-pulse' : ''}`} />
            ) : (
              <RotateCcw className={`w-3.5 h-3.5 ${busy ? 'animate-spin' : ''}`} />
            )}
            {status === 'idle' ? '开始生成' : '重跑节点'}
          </button>
          )}
        </div>
      )}
    </div>
  )
}
