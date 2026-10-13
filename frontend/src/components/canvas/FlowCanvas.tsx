import React, { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  Node,
  Edge,
  useNodesState,
  useEdgesState,
  useReactFlow,
  useStore as useRFStore,
  Handle,
  Position,
} from 'reactflow'
import 'reactflow/dist/style.css'
import { useStore } from '@/store/useStore'
import { WorkflowNode } from '@/types'
import { connectWebSocket, disconnectWebSocket } from '@/services/websocket'
import {
  layoutDAG,
  convertWorkflowNodes,
  generateEdges,
  generatePhaseBands,
  buildShotCards,
  buildShotCardEdges,
  buildShotGroupNode,
  buildArtGroup,
} from '@/utils/dagLayout'
import { CheckCircle, RotateCcw, Eye, Target, ListVideo, ChevronDown, ClipboardList, LayoutGrid } from 'lucide-react'
import { workflowAPI } from '@/services/api'
import CanvasBreadcrumb from './CanvasBreadcrumb'
import FanoutEpisodesButton from './FanoutEpisodesButton'
import BatchGenerateButton from './BatchGenerateButton'

// ═══════════════════════════════════════════════════════
// 节点类型颜色映射
// ═══════════════════════════════════════════════════════
const nodeColors: Record<string, string> = {
  episode_breakdown: '#8b5cf6',   // 0.7.0 专家模式：分集（紫罗兰）
  script_generation: '#6366f1',
  art_design: '#a855f7',
  episode_art_design: '#c084fc',  // 0.7.0 轮 1：本集美术（浅紫）
  storyboard_design: '#3b82f6',
  // 1.1.0 MV：三个新阶段（青色系，跟音乐相关的都用青）
  mv_audio_analysis: '#22d3ee',
  mv_script_design: '#0ea5e9',
  mv_storyboard_design: '#3b82f6',
  storyboard_shot: '#60a5fa',
  keyframe_generation: '#eab308',
  video_generation: '#f59e0b',
  // 0.6.1: 音频导演 + Mubert BGM
  audio_plan: '#14b8a6',
  bgm_fetch: '#06b6d4',
  // 0.7.2 ②：专家分层 BGM —— series 风格方向 + episode 场景计划
  bgm_direction: '#2dd4bf',
  episode_audio_plan: '#14b8a6',
  video_merge: '#ef4444',
  publish: '#ec4899',
  cost_summary: '#facc15',  // 0.6.5: 金色，"成本"主题
  text_to_text: '#22c55e',
  default: '#64748b',
}

// 0.7.1 语义缩放：按状态上色（小卡圆点 + MiniMap 共用）。缩小时整集状态一目了然
const statusDotColors: Record<string, string> = {
  idle: '#64748b',       // 灰：待生成
  pending: '#64748b',
  queued: '#64748b',
  running: '#22d3ee',    // 青：执行中
  completed: '#818cf8',  // 靛：待确认
  confirmed: '#34d399',  // 翠：已确认
  error: '#fb7185',      // 玫红：失败
  skipped: '#6b7280',
}

// 0.7.1 语义缩放阈值：zoom < 0.5 → 收成小卡（概览找位置）；≥ 0.5 → 全尺寸大卡（看结果/操作）
const LOD_DETAIL_ZOOM = 0.5

const agentIcons: Record<string, string> = {
  screenwriter: '📝',
  art_director: '🎨',
  storyboard: '🎬',
  audio: '🔊',
  audio_director: '🎼',
  module_router: '⚙️',
  review: '✅',
  localization: '🌍',
  director: '🎭',
  cost_aggregator: '💰',  // 0.6.5
}

// ═══════════════════════════════════════════════════════
// 结果预览渲染（按节点类型分发）
// ═══════════════════════════════════════════════════════
function renderNodeResultPreview(data: any): React.ReactNode {
  const result = data.result
  if (result == null) return null
  if (typeof result === 'string') return result

  // 子节点（角色/场景/道具图）—— 含 0.7.0 episode 类型
  if (data.type === 'result' && result.image_url) {
    const altText = typeof result.prompt === 'string' ? result.prompt.slice(0, 30) : '生成的图片'
    const labelName = typeof (result.name ?? result.location) === 'string'
      ? (result.name ?? result.location)
      : ''
    const typeLabel = ({
      character: '🎭 角色',
      scene: '🏞️ 场景',
      prop: '🎁 道具',
      ep_character: '🎭 本集角色',
      ep_scene: '🏞️ 本集场景',
      ep_prop: '🎁 本集道具',
    } as Record<string, string>)[result.result_type] || '🏞️ 场景'
    return (
      <div className="flex flex-col gap-1">
        <span className="text-[10px] text-octopus-accent">
          {typeLabel}: {labelName}
        </span>
        <img
          src={result.image_url}
          alt={altText}
          className="w-full h-auto object-contain rounded bg-black/30"
          onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
        />
        {result.three_view_image_url && (
          <img
            src={result.three_view_image_url}
            alt="三视图"
            className="w-full h-auto object-contain rounded bg-black/30 opacity-95"
            onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
        )}
        {result.mock && <span className="text-[10px] text-yellow-400">[Mock 图片]</span>}
      </div>
    )
  }

  // result 子节点还没出图（运行中/排队）→ 干净标签，别 dump 原始 JSON 撑破节点
  if (data.type === 'result') {
    const labelName = typeof (result.name ?? result.location) === 'string'
      ? (result.name ?? result.location) : ''
    const typeLabel = ({
      character: '🎭 角色', scene: '🏞️ 场景', prop: '🎁 道具',
      ep_character: '🎭 本集角色', ep_scene: '🏞️ 本集场景', ep_prop: '🎁 本集道具',
    } as Record<string, string>)[result.result_type] || '🖼 素材'
    return (
      <div className="text-[11px] text-octopus-muted truncate">
        {typeLabel}{labelName ? `：${labelName}` : ''} · 生成中…
      </div>
    )
  }

  // 0.7.0 轮 1：本集美术 episode_art_design 节点
  if (data.type === 'episode_art_design') {
    const chars = result.characters_in_episode || []
    const scenes = result.scenes_in_episode || []
    const props = result.props_in_episode || []
    const outfitTotal = chars.reduce((s: number, c: any) => s + ((c.outfits || []).length), 0)
    return (
      <div className="space-y-0.5">
        {result.episode_summary && (
          <div className="text-octopus-text text-[11px] line-clamp-2">{result.episode_summary}</div>
        )}
        <div className="text-[10px] text-octopus-muted">
          🎭 {chars.length} 角色 · {outfitTotal} 装束 · 🏞️ {scenes.length} 场景变化 · 🎁 {props.length} 道具状态
        </div>
      </div>
    )
  }

  // 1.1.0 MV：歌曲解析节点
  if (data.type === 'mv_audio_analysis' || Array.isArray(result.lyrics)) {
    const lyrics = Array.isArray(result.lyrics) ? result.lyrics : []
    const song = result.song || {}
    const secs = Array.isArray(song.sections) ? song.sections : []
    const d = Number(result.duration || 0)
    return (
      <div className="space-y-0.5">
        <div className="text-octopus-text font-medium">
          🎵 {Math.floor(d / 60)}分{String(Math.floor(d % 60)).padStart(2, '0')}秒
          {result.lyrics_edited && <span className="ml-1.5 text-[9px] text-cyan-400">已校对</span>}
        </div>
        {song.theme && <div className="line-clamp-2">{song.theme}</div>}
        <div className="text-[10px] text-octopus-muted">
          🎤 {lyrics.length} 句歌词 · 🎼 {secs.length} 段 · {song.genre || '—'}
        </div>
        <button
          onClick={(e) => {
            e.stopPropagation()
            // 校对界面挂在 App 层（要播音频、要调试算接口），这里只发事件
            window.dispatchEvent(new CustomEvent('octopus:edit-lyrics', { detail: { nodeId: data.id } }))
          }}
          className="mt-1 w-full text-[10px] py-1 rounded border border-cyan-400/30 text-cyan-300 hover:bg-cyan-400/10"
        >
          校对歌词时间轴
        </button>
      </div>
    )
  }

  // 1.1.0 MV：剧本节点（叙事型有 logline / 意象型有 concept）
  if (data.type === 'mv_script_design') {
    const chars = Array.isArray(result.characters) ? result.characters : []
    const scenes = Array.isArray(result.scenes) ? result.scenes : []
    return (
      <div className="space-y-0.5">
        <div className="line-clamp-2 text-octopus-text">
          {result.logline || result.concept || '—'}
        </div>
        <div className="text-[10px] text-octopus-muted">
          {result.type === 'visual' ? '意象型' : '叙事型'} · 🏞️ {scenes.length} 场景
          {chars.length > 0 && ` · 🎭 ${chars.length} 角色`}
        </div>
      </div>
    )
  }

  // 剧本节点
  if (data.type === 'script_generation' || result.script_title) {
    const propsCount = result.props?.length || 0
    return (
      <div className="space-y-0.5">
        <div className="text-octopus-text font-medium truncate">📖 {result.script_title || '未命名'}</div>
        {result.logline && <div className="line-clamp-2">{result.logline}</div>}
        <div className="text-[10px] text-octopus-muted">
          🏞️ {result.scenes?.length || 0} 场景 · 🎭 {result.characters?.length || 0} 角色
          {propsCount > 0 && ` · 🎁 ${propsCount} 道具`}
        </div>
      </div>
    )
  }

  // 美术节点（visual_style 是 dict）
  if (data.type === 'art_design' || result.visual_style) {
    const vs = result.visual_style
    const keywords = Array.isArray(vs?.keywords) ? vs.keywords.join('、') : (typeof vs === 'string' ? vs : '')
    const palette = Array.isArray(vs?.color_palette) ? vs.color_palette.slice(0, 3).join('、') : ''
    const propsCount = result.props?.length || 0
    return (
      <div className="space-y-0.5">
        {keywords && <div className="text-octopus-text">🎨 {keywords}</div>}
        {palette && <div className="text-[10px]">🎨 {palette}</div>}
        <div className="text-[10px] text-octopus-muted">
          🎭 {result.characters?.length || 0} 角色 · 🏞️ {result.scenes?.length || 0} 场景
          {propsCount > 0 && ` · 🎁 ${propsCount} 道具`}
        </div>
      </div>
    )
  }

  // 分镜节点（result.storyboard 是 array）
  if (data.type === 'storyboard_design' || Array.isArray(result.storyboard)) {
    const shots = Array.isArray(result.storyboard) ? result.storyboard : []
    return (
      <div className="space-y-0.5">
        <div className="text-octopus-text">🎬 {shots.length} 个分镜</div>
        {result.total_duration_seconds && (
          <div className="text-[10px] text-octopus-muted">
            ⏱ 总时长 {result.total_duration_seconds}s
          </div>
        )}
      </div>
    )
  }

  // 关键帧节点（image_to_image 多图融合）
  if (data.type === 'keyframe_generation' || (result.image_url && data.type !== 'art_design' && data.type !== 'result')) {
    if (result.image_url) {
      return (
        <div className="space-y-1">
          <img
            src={result.image_url}
            alt="keyframe"
            className="w-full h-auto object-contain rounded bg-black"
            onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
          {result.mock && <span className="text-[10px] text-yellow-400">[Mock 图片]</span>}
          {typeof result.reference_count === 'number' && (
            <div className="text-[10px] text-octopus-muted">融合 {result.reference_count} 张参考图</div>
          )}
        </div>
      )
    }
    if (result.skipped) {
      return <div className="text-[11px] text-yellow-400">⚠ 跳过：{result.reason || '无参考图'}</div>
    }
  }

  // 视频节点
  if (data.type === 'video_generation' || result.video_url || result.task_id) {
    const url = result.video_url
    const isMock = result.mock || result.status === 'mock'
    return (
      <div className="space-y-1">
        {url ? (
          <video
            src={url}
            className="w-full h-auto object-contain rounded bg-black"
            controls
            muted
            playsInline
            preload="metadata"
            onClick={(e) => e.stopPropagation()}
            onError={(e) => { (e.target as HTMLVideoElement).style.display = 'none' }}
          />
        ) : (
          <div className="text-octopus-text">🎬 视频生成完成</div>
        )}
        {isMock && <span className="text-[10px] text-yellow-400">[Mock 视频]</span>}
      </div>
    )
  }

  // 视频合并节点
  if (data.type === 'video_merge') {
    return result.video_url ? (
      <video
        src={result.video_url}
        className="w-full h-auto object-contain rounded bg-black"
        controls
        muted
        playsInline
        preload="metadata"
        onClick={(e) => e.stopPropagation()}
        onError={(e) => { (e.target as HTMLVideoElement).style.display = 'none' }}
      />
    ) : (
      <div className="text-octopus-text">🎬 合并完成</div>
    )
  }

  // 0.6.1: 音频规划节点
  if (data.type === 'audio_plan') {
    const segs = Array.isArray(result.segments) ? result.segments : []
    return (
      <div className="space-y-0.5">
        <div className="text-octopus-text">🎼 {result.overall_mood || '音频规划'}</div>
        <div className="text-[10px] text-octopus-muted">
          {segs.length} 段 · 共 {result.total_duration || 0}s
        </div>
        {Array.isArray(result.style_tags) && result.style_tags.length > 0 && (
          <div className="text-[10px] text-octopus-muted truncate">
            {result.style_tags.slice(0, 4).join(' · ')}
          </div>
        )}
      </div>
    )
  }

  // 0.6.1: BGM 节点
  if (data.type === 'bgm_fetch') {
    if (result.error) {
      return (
        <div className="text-[11px] text-red-400 break-words">
          ❌ {String(result.error).slice(0, 120)}
        </div>
      )
    }
    if (result.public_url) {
      return (
        <div className="space-y-1">
          <div className="text-[10px] text-octopus-muted">
            🎵 {result.mood || 'BGM'} · {result.duration || 0}s
          </div>
          <audio
            src={result.public_url}
            controls
            className="w-full"
            preload="metadata"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )
    }
    return <div className="text-octopus-text">🎵 BGM 生成中...</div>
  }

  // 0.7.2 ②：series 级 BGM 风格方向（master canvas）
  if (data.type === 'bgm_direction') {
    const palette = Array.isArray(result.mood_palette) ? result.mood_palette : []
    return (
      <div className="space-y-0.5">
        <div className="text-octopus-text">🎵 {result.overall_style || 'BGM 风格规划中...'}</div>
        {Array.isArray(result.style_tags) && result.style_tags.length > 0 && (
          <div className="text-[10px] text-octopus-muted truncate">{result.style_tags.slice(0, 5).join(' · ')}</div>
        )}
        {palette.slice(0, 3).map((p: any, i: number) => (
          <div key={i} className="text-[10px] text-octopus-muted truncate">· {p?.mood}：{p?.music}</div>
        ))}
        {result.motif_notes && (
          <div className="text-[10px] text-octopus-chat line-clamp-2" title={result.motif_notes}>♻ {result.motif_notes}</div>
        )}
      </div>
    )
  }

  // 0.7.2 ②：episode 场景 BGM 计划（中文场景名 → 中文主题名，复用标 ♻）
  if (data.type === 'episode_audio_plan') {
    const plan = Array.isArray(result.scene_bgm_plan) ? result.scene_bgm_plan : []
    if (!plan.length) return <div className="text-octopus-text">🎼 按场景规划本集 BGM...</div>
    const mmss = (v: any) => { const s = Math.max(0, Number(v) || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` }
    return (
      <div className="space-y-0.5">
        <div className="text-[10px] text-octopus-muted">
          {plan.length} 段配乐 · {result.track_count || 0} 个主题
          {result.reuse_count ? ` · ♻ 复用 ${result.reuse_count}` : ''}
        </div>
        {plan.slice(0, 5).map((s: any, i: number) => (
          <div key={i} className="text-[10px] text-octopus-muted truncate">
            {mmss(s.t0)}-{mmss(s.t1)} {s.scene_label || s.scene_id} · 🎵{s.track_label || s.track_key}{s.source === 'library' ? ' ♻' : ''}
          </div>
        ))}
        {plan.length > 5 && <div className="text-[10px] text-octopus-muted">… 共 {plan.length} 段</div>}
      </div>
    )
  }

  // 发布节点
  if (data.type === 'publish') {
    const title = result.title || result.title_suggested
    const exported = result.exported
    const exportUrl = result.export_url
    return (
      <div className="space-y-1">
        {title && (
          <div className="text-octopus-text font-medium line-clamp-2">📢 {title}</div>
        )}
        {Array.isArray(result.tags_suggested) && result.tags_suggested.length > 0 && (
          <div className="text-[10px] text-octopus-muted">
            {result.tags_suggested.slice(0, 5).map((t: string) => `#${t}`).join(' ')}
          </div>
        )}
        {exported ? (
          <div className="text-[10px] text-green-400">
            ✓ 已导出 {exportUrl ? <span className="font-mono">{exportUrl}</span> : ''}
          </div>
        ) : (
          <div className="text-[10px] text-octopus-muted">点击节点编辑文案与封面，再导出</div>
        )}
      </div>
    )
  }

  // 成本总结节点（0.6.5）
  if (data.type === 'cost_summary' || typeof result.total_cny === 'number') {
    const total = result.total_cny ?? 0
    const calls = result.entries?.length ?? 0
    return (
      <div className="space-y-0.5">
        <div className="text-octopus-text">💰 ¥{total.toFixed(2)}</div>
        <div className="text-[10px] text-octopus-muted">
          {calls} 次调用 · ≈ ${(result.total_usd ?? 0).toFixed(2)}
        </div>
      </div>
    )
  }

  // 0.7.0 专家模式：分集节点（episode_breakdown）—— 友好列出每集，不 dump JSON
  if (data.type === 'episode_breakdown' || Array.isArray(result.episodes)) {
    const eps = Array.isArray(result.episodes) ? result.episodes : []
    return (
      <div className="space-y-0.5">
        <div className="text-octopus-text">🎬 共 {eps.length} 集</div>
        {eps.map((ep: any, i: number) => (
          <div key={i} className="text-[10px] text-octopus-muted truncate">
            <span className="text-octopus-accent">{ep.ep_id || `EP${i + 1}`}</span>
            {' '}{ep.title || ''}
            {ep.est_duration_min ? ` · ${ep.est_duration_min}min` : ''}
          </div>
        ))}
      </div>
    )
  }

  // 0.7.7 本地化翻译节点 —— 友好列出各语言译后视频链接，不 dump JSON
  if (data.type === 'localization' || Array.isArray(result.translations)) {
    const trans = Array.isArray(result.translations) ? result.translations : []
    const LANG: Record<string, string> = {
      en: '英语', ja: '日语', ko: '韩语', es: '西班牙语', pt: '葡萄牙语', fr: '法语', de: '德语',
      ru: '俄语', ar: '阿拉伯语', vi: '越南语', th: '泰语', id: '印尼语', ms: '马来语',
      tr: '土耳其语', 'zh-tw': '繁体中文', yue: '粤语',
    }
    if (trans.length === 0) {
      return <div className="text-[10px] text-octopus-muted">🌍 未翻译 · 点击节点选语言开始</div>
    }
    const done = trans.filter((t: any) => t.video_url).length
    const ttype = result.translate_type || 'VOICE'
    return (
      <div className="space-y-0.5">
        <div className="text-octopus-text">🌍 本地化 · {ttype} · {done}/{trans.length} 完成</div>
        {trans.slice(0, 6).map((t: any, i: number) => (
          <div key={i} className="text-[10px] truncate">
            <span className="text-octopus-accent">{LANG[t.target_language] || t.target_language}</span>{' '}
            {t.video_url ? (
              <a
                href={t.video_url}
                target="_blank"
                rel="noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="text-sky-400 hover:underline"
              >
                打开视频 ↗
              </a>
            ) : (
              <span className="text-rose-400" title={t.error}>{t.error ? String(t.error).slice(0, 32) : '失败'}</span>
            )}
          </div>
        ))}
        {trans.length > 6 && <div className="text-[10px] text-octopus-muted">… 共 {trans.length} 语言</div>}
      </div>
    )
  }

  // 兜底：避免对象直接渲染崩溃
  try {
    return <pre className="text-[10px] whitespace-pre-wrap break-all">{JSON.stringify(result).slice(0, 120)}</pre>
  } catch {
    return <span>—</span>
  }
}


// ═══════════════════════════════════════════════════════
// 自定义节点组件
// ═══════════════════════════════════════════════════════
function CustomNode({ data, id }: any) {
  const status: string = data.status || 'idle'
  const color = nodeColors[data.type] || nodeColors.default
  const icon = agentIcons[data.agent] || '🔧'
  const confirmed = data.confirmed || false
  const hasResult = data.result !== null && data.result !== undefined

  // 0.6.2: bgm_fetch 节点的 title 跟着 result.provider 走（节点创建时存的快照可能是老 provider）
  let displayLabel: string = data.label || ''
  if (data.type === 'bgm_fetch') {
    const provider = data.result?.provider
    const provLabel: Record<string, string> = {
      elevenlabs: 'ElevenLabs',
      mubert: 'Mubert',
    }
    if (provider && provLabel[provider]) {
      displayLabel = `BGM 生成 (${provLabel[provider]})`
    }
  }

  const statusConfig: Record<string, any> = {
    idle: {
      border: 'border-slate-600',
      bg: 'bg-[#191926]',
      glow: '',
      icon: '⏳',
      label: '等待中',
    },
    running: {
      border: 'border-cyan-400',
      bg: 'bg-[#191926]',
      glow: 'shadow-[0_0_22px_rgba(34,211,238,0.45)]',
      icon: '▶️',
      label: '执行中',
    },
    completed: {
      border: 'border-indigo-400',
      bg: 'bg-[#191926]',
      glow: 'shadow-[0_0_16px_rgba(129,140,248,0.35)]',
      icon: '✨',
      label: '待确认',
    },
    confirmed: {
      border: 'border-emerald-400',
      bg: 'bg-[#191926]',
      glow: 'shadow-[0_0_15px_rgba(52,211,153,0.30)]',
      icon: '✅',
      label: '已确认',
    },
    error: {
      border: 'border-rose-400',
      bg: 'bg-rose-950/30',
      glow: 'shadow-[0_0_15px_rgba(251,113,133,0.35)]',
      icon: '❌',
      label: '失败',
    },
    skipped: {
      border: 'border-gray-500',
      bg: 'bg-[#191926]',
      glow: '',
      icon: '⏭️',
      label: '已跳过',
    },
  }

  const config = statusConfig[status] || statusConfig.idle
  const setSelectedNode = useStore((s) => s.setSelectedNode)
  const currentProject = useStore((s) => s.currentProject)
  const setIsTyping = useStore((s) => s.setIsTyping)
  const addMessage = useStore((s) => s.addMessage)
  // 0.7.1 语义缩放：selector 返回 boolean，只在跨越阈值时 re-render（缩放途中不抖动、不卡）
  const showDetail = useRFStore((s) => s.transform[2] >= LOD_DETAIL_ZOOM)

  // 确认节点
  const handleConfirm = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!currentProject?.id) return
    try {
      setIsTyping(true)
      await workflowAPI.nodeAction(currentProject.id, id, 'confirm')
    } catch (err) {
      addMessage({
        id: `err_${Date.now()}`,
        role: 'agent',
        content: '确认失败：' + (err as Error).message,
        timestamp: Date.now(),
      })
      setIsTyping(false)
    }
  }

  // 重跑节点
  const handleRerun = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!currentProject?.id) return
    try {
      setIsTyping(true)
      await workflowAPI.nodeAction(currentProject.id, id, 'rerun')
      addMessage({
        id: `rerun_${Date.now()}`,
        role: 'agent',
        content: `🔄 节点 ${data.label} 正在重新执行...`,
        timestamp: Date.now(),
      })
    } catch (err) {
      addMessage({
        id: `err_${Date.now()}`,
        role: 'agent',
        content: '重跑失败：' + (err as Error).message,
        timestamp: Date.now(),
      })
      setIsTyping(false)
    }
  }

  // 查看结果（与点击节点同效果，打开 NodeEditorPanel）
  const handleViewResult = (e: React.MouseEvent) => {
    e.stopPropagation()
    setSelectedNode(id)
  }

  // 点击节点打开编辑器
  const handleClick = () => {
    setSelectedNode(id)
  }

  // 0.7.1 语义缩放：缩小到阈值以下 → 收成"小卡"（状态色点 + 标题 + 缩略图，不渲染大图/播放器/按钮）。
  // 整集几十镜可一屏概览、靠颜色找位置；放大或 focusNode 跳转（落 0.8）才展开成下面的大卡看结果。
  if (!showDetail) {
    const thumb = data.result?.image_url || data.result?.cover_url || data.result?.first_frame_url
    const dot = statusDotColors[status] || statusDotColors.idle
    return (
      <div
        className={`relative flex items-center gap-2 rounded-lg border-2 ${config.border} ${config.bg} ${config.glow} w-[210px] px-2.5 py-2 cursor-pointer hover:border-white/40 transition-colors`}
        onClick={handleClick}
        title={`${displayLabel} · ${config.label}`}
      >
        <Handle
          type="target"
          position={Position.Left}
          className="!w-2 !h-2 !bg-octopus-muted/70 !border !border-octopus-bg"
        />
        {thumb ? (
          <img src={thumb} alt="" draggable={false} className="w-9 h-9 rounded object-cover flex-shrink-0" />
        ) : (
          <span className="w-9 text-center text-xl flex-shrink-0">{icon}</span>
        )}
        <div className="flex-1 min-w-0">
          <div className="text-sm font-bold text-octopus-text truncate leading-tight">{displayLabel}</div>
          <div className="text-[10px] text-octopus-muted truncate">{config.label}</div>
        </div>
        <span className="w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ backgroundColor: dot }} />
        {status === 'running' && <div className="node-running-beam" aria-hidden />}
        <Handle
          type="source"
          position={Position.Right}
          className="!w-2 !h-2 !bg-octopus-primary/70 !border !border-octopus-bg"
        />
      </div>
    )
  }

  return (
    <div
      className={`relative rounded-xl border-2 ${config.border} ${config.bg} ${config.glow} px-4 py-3 ${data.fixedWidth ? 'w-[260px]' : 'min-w-[220px] max-w-[300px]'} cursor-pointer hover:border-white/40 transition-all duration-300 group`}
      onClick={handleClick}
    >
      {/* 输入连接点 - 左侧（单一汇入点，多依赖共用，避免堆叠溢出） */}
      <Handle
        type="target"
        position={Position.Left}
        className="!w-2.5 !h-2.5 !bg-octopus-muted/70 !border-2 !border-octopus-bg hover:!bg-octopus-primary transition-colors"
      />

      {/* 节点头部 */}
      <div className="flex items-center gap-2 mb-2">
        <span className="text-lg">{icon}</span>
        <div className="flex-1 min-w-0">
          <div className="text-xs text-octopus-muted uppercase tracking-wider truncate">
            {data.agent || 'Agent'}
          </div>
          <div className="text-sm text-octopus-text font-semibold truncate">
            {displayLabel}
          </div>
        </div>
        <div className="flex items-center gap-1">
          <span className="text-xs">{config.icon}</span>
          <span className={`text-[10px] px-1.5 py-0.5 rounded ${
            status === 'running' ? 'bg-cyan-500/20 text-cyan-300' :
            status === 'completed' ? 'bg-indigo-500/20 text-indigo-300' :
            status === 'confirmed' ? 'bg-emerald-500/20 text-emerald-300' :
            status === 'error' ? 'bg-rose-500/20 text-rose-300' :
            'bg-slate-500/20 text-slate-400'
          }`}>
            {config.label}
          </span>
        </div>
      </div>

      {/* 类型标签 */}
      <div className="flex items-center gap-1.5 mb-2">
        <div className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: color }} />
        <span className="text-[10px] text-octopus-muted uppercase">
          {data.type}
        </span>
      </div>

      {/* 结果预览 */}
      {hasResult && (
        <div
          className={`mb-2 p-2 rounded bg-octopus-bg/50 text-xs text-octopus-muted overflow-hidden ${
            ['result', 'video_generation', 'video_merge', 'keyframe_generation'].includes(data.type) ||
            (data.result && (data.result.image_url || data.result.video_url))
              ? ''
              : 'max-h-[140px] overflow-y-auto'
          }`}
        >
          {renderNodeResultPreview(data)}
        </div>
      )}

      {/* 操作按钮 */}
      {status === 'completed' && !confirmed && (
        <div className="nodrag flex gap-2 mt-2">
          <button
            onClick={handleConfirm}
            className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded-lg bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-400 hover:to-teal-400 text-white text-xs font-medium shadow-[0_0_14px_rgba(52,211,153,0.45)] transition-all"
          >
            <CheckCircle className="w-3 h-3" />
            确认
          </button>
          <button
            onClick={handleRerun}
            className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded-lg bg-octopus-panel hover:bg-octopus-border text-octopus-text text-xs transition-colors border border-octopus-border"
          >
            <RotateCcw className="w-3 h-3" />
            重跑
          </button>
        </div>
      )}

      {/* 已确认状态 */}
      {status === 'confirmed' && (
        <div className="flex items-center gap-1 mt-2 text-xs text-emerald-400">
          <CheckCircle className="w-3 h-3" />
          已确认
        </div>
      )}

      {/* 查看结果按钮 */}
      {hasResult && (
        <button
          onClick={handleViewResult}
          className="nodrag absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded bg-octopus-bg/80 hover:bg-octopus-border"
        >
          <Eye className="w-3 h-3 text-octopus-muted" />
        </button>
      )}

      {/* 运行中：白光束绕边框跑马灯 + 当前运行内容（替代旧进度条） */}
      {status === 'running' && (
        <>
          <div className="node-running-beam" aria-hidden />
          {(data.progress_message || typeof data.progress === 'number') && (
            <div className="relative mt-2 flex items-center justify-between text-[10px] text-cyan-300">
              <span className="truncate flex-1 mr-2">{data.progress_message || '生成中...'}</span>
              {typeof data.progress === 'number' && (
                <span className="font-mono flex-shrink-0">{data.progress.toFixed(0)}%</span>
              )}
            </div>
          )}
        </>
      )}

      {/* 输出连接点 - 右侧（单一引出点，多子节点共用，边自然扇出） */}
      <Handle
        type="source"
        position={Position.Right}
        className="!w-2.5 !h-2.5 !bg-octopus-primary/70 !border-2 !border-octopus-bg hover:!bg-octopus-primary transition-colors"
      />
    </div>
  )
}

// 0.7.1：阶段分区背景带节点（半透明底 + 左上角标题），压在普通节点后面、不拦点击
function PhaseBandNode({ data }: any) {
  return (
    <div
      style={{ width: data.w, height: data.h, background: data.bg, borderColor: data.border }}
      className="relative rounded-2xl border-2 border-dashed pointer-events-none select-none"
    >
      <div
        className="absolute top-2 left-3 text-xs font-semibold tracking-wider"
        style={{ color: data.text }}
      >
        {data.label} · {data.count}
      </div>
    </div>
  )
}

// 0.7.x：镜头网格组容器（subflow parent）—— 可拖、可连线；分镜卡作为子节点跟随移动。
// overflow:visible 绝不能改 hidden（否则裁掉子卡 / 运行电光圈 / Handle）。
function ShotGroupNode({ data }: any) {
  return (
    <div
      className="relative h-full w-full rounded-2xl border-2 border-dashed"
      style={{ borderColor: 'rgba(245,158,11,0.45)', background: 'rgba(245,158,11,0.04)', overflow: 'visible' }}
    >
      <Handle type="target" position={Position.Left} className="!w-2.5 !h-2.5 !bg-octopus-muted/70 !border-2 !border-octopus-bg" />
      <div className="pointer-events-none absolute top-2 left-3 text-xs font-semibold tracking-wider text-amber-400">
        {data.label || '🎬 镜头网格'} · {data.count}
      </div>
      <Handle type="source" position={Position.Right} className="!w-2.5 !h-2.5 !bg-octopus-primary/70 !border-2 !border-octopus-bg" />
    </div>
  )
}

// ═══════════════════════════════════════════════════════
// 0.7.x：分镜卡 —— 一镜一张合成卡（关键帧左 / 视频右），各段连真实节点 id 做确认/重跑/选中。
// 合成节点 id=shot-<idx>，绝不进 selectedNode（NodeEditorPanel 靠真实 id 开面板）。
// ═══════════════════════════════════════════════════════
function ShotHalf({
  label,
  node,
  imageUrl,
  placeholder,
  badge,
  cornerLabel,
  dot,
  onSelect,
  onAct,
  onBadgeClick,
}: {
  label: string
  node: any
  imageUrl?: string
  placeholder?: string | null
  badge?: string
  cornerLabel?: string
  dot: string
  onSelect: (id: string) => void
  onAct: (id: string, action: 'confirm' | 'rerun', e: React.MouseEvent) => void
  onBadgeClick?: () => void
}) {
  const status = String(node?.status || (node ? 'idle' : 'none')).toLowerCase()
  const running = status === 'running'
  const needConfirm = status === 'completed' && !node?.confirmed
  const isIdle = status === 'idle' || status === 'pending' || status === 'queued'
  const spinner = <span className="h-6 w-6 rounded-full border-2 border-cyan-400/25 border-t-cyan-400 animate-spin" />
  // 纯 relative 容器 + 全 absolute 子元素：图片不会撑高把 footer 顶出 overflow-hidden（之前有图就没 footer 的根因）。
  return (
    <div
      className={`relative flex-1 min-w-0 overflow-hidden ${node ? 'cursor-pointer' : ''}`}
      onClick={node ? (e) => { e.stopPropagation(); onSelect(node.id) } : undefined}
    >
      {imageUrl ? (
        <img src={imageUrl} alt="" draggable={false} className="absolute inset-0 h-full w-full object-cover" />
      ) : (
        <div className="absolute inset-0 flex items-center justify-center bg-octopus-bg/40">
          {running ? (
            spinner
          ) : placeholder ? (
            <span className="px-1 text-center text-[10px] leading-tight text-octopus-muted">{placeholder}</span>
          ) : (
            <span className="h-3 w-3 rounded-full" style={{ backgroundColor: dot }} />
          )}
        </div>
      )}
      {running && imageUrl && (
        <span className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2">{spinner}</span>
      )}
      {cornerLabel && (
        <span className="absolute top-1 left-1 rounded bg-black/55 px-1 py-0.5 text-[8px] text-amber-200">
          {cornerLabel}
        </span>
      )}
      {badge &&
        (onBadgeClick ? (
          <button
            onClick={(e) => { e.stopPropagation(); onBadgeClick() }}
            title="播放视频"
            className="nodrag absolute top-1 right-1 flex h-5 w-5 items-center justify-center rounded-full bg-black/60 text-[9px] text-white transition-colors hover:bg-octopus-primary hover:text-black"
          >
            {badge}
          </button>
        ) : (
          <span className="absolute top-1 right-1 flex h-4 w-4 items-center justify-center rounded-full bg-black/55 text-[8px] text-white">
            {badge}
          </span>
        ))}
      {/* 底部浮层：标签 + 操作（abs 贴底，永远可见、不受图片高度挤压）。nodrag：按钮区不触发卡片拖动 */}
      <div className="nodrag absolute inset-x-0 bottom-0 flex items-center justify-between gap-1 bg-gradient-to-t from-black/80 to-transparent px-1.5 pt-3 pb-1">
        <span className="truncate text-[9px] text-white/85">{label}</span>
        {needConfirm ? (
          <span className="flex flex-shrink-0 gap-0.5">
            <button
              onClick={(e) => onAct(node.id, 'confirm', e)}
              className="rounded bg-emerald-500/90 px-1.5 py-0.5 text-[9px] text-white hover:bg-emerald-400"
            >
              确认
            </button>
            <button
              onClick={(e) => onAct(node.id, 'rerun', e)}
              className="rounded border border-white/20 bg-black/40 px-1.5 py-0.5 text-[9px] text-white/90 hover:bg-black/60"
            >
              重跑
            </button>
          </span>
        ) : isIdle ? (
          <button
            onClick={(e) => onAct(node.id, 'rerun', e)}
            className="flex-shrink-0 rounded bg-octopus-primary/90 px-1.5 py-0.5 text-[9px] font-medium text-black hover:bg-octopus-primary"
          >
            ▶ 生成
          </button>
        ) : (
          <span className="h-1.5 w-1.5 flex-shrink-0 rounded-full" style={{ backgroundColor: dot }} />
        )}
      </div>
    </div>
  )
}

function ShotCardNode({ data }: any) {
  const showDetail = useRFStore((s) => s.transform[2] >= LOD_DETAIL_ZOOM)
  const setSelectedNode = useStore((s) => s.setSelectedNode)
  const currentProject = useStore((s) => s.currentProject)
  const setIsTyping = useStore((s) => s.setIsTyping)
  const addMessage = useStore((s) => s.addMessage)
  const [playing, setPlaying] = useState(false) // ▶ 弹窗播放视频

  const { shotLabel, kf, video, chainRole } = data
  const isLink = chainRole === 'link'
  // 0.7.2：横屏项目（16:9）卡片内部改上下两半 —— 横图塞进左右窄半区会被裁成细条
  const wide = String(data.aspect || '').startsWith('16')
  // 选中高亮：本卡的关键帧/视频节点正打开在面板时给个主色描边（selector 返 boolean，仅翻转时重渲染）
  const isSelected = useStore((s) => !!s.selectedNode && (s.selectedNode === kf?.id || s.selectedNode === video?.id))
  const kdot = kf ? statusDotColors[String(kf.status || 'idle').toLowerCase()] || '#3a3a4a' : '#2b2b3a'
  const vdot = video ? statusDotColors[String(video.status || 'idle').toLowerCase()] || '#3a3a4a' : '#2b2b3a'

  const act = async (nodeId: string, action: 'confirm' | 'rerun', e: React.MouseEvent) => {
    e.stopPropagation()
    if (!currentProject?.id) return
    try {
      setIsTyping(true)
      await workflowAPI.nodeAction(currentProject.id, nodeId, action)
      if (action === 'rerun') {
        addMessage({ id: `rerun_${Date.now()}`, role: 'agent', content: `🔄 重新执行 ${shotLabel}...`, timestamp: Date.now() })
      }
    } catch (err) {
      addMessage({ id: `err_${Date.now()}`, role: 'agent', content: '操作失败：' + (err as Error).message, timestamp: Date.now() })
      setIsTyping(false)
    }
  }

  // 小卡（zoom < 阈值）：缩略图 + 编号 + 两状态点
  if (!showDetail) {
    const thumb = kf?.result?.image_url || video?.firstFrame || video?.result?.image_url
    return (
      <div
        className={`relative w-[240px] h-[200px] rounded-xl border-2 bg-[#191926] flex flex-col items-center justify-center gap-2 cursor-pointer hover:border-white/40 ${
          isSelected ? 'border-octopus-primary ring-2 ring-octopus-primary/50' : 'border-slate-600'
        }`}
        onClick={(e) => { e.stopPropagation(); setSelectedNode(video?.id || kf?.id) }}
      >
        <Handle type="target" position={Position.Left} className="!opacity-0" />
        {thumb ? (
          <img src={thumb} alt="" draggable={false} className="w-24 h-24 rounded-lg object-cover" />
        ) : (
          <span className="text-3xl">🎬</span>
        )}
        <div className="text-base font-mono text-octopus-text">{shotLabel}</div>
        <div className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: kdot }} />
          <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: vdot }} />
        </div>
        <Handle type="source" position={Position.Right} className="!opacity-0" />
      </div>
    )
  }

  // 大卡：头部 + 关键帧/视频两段
  const videoUrl = video?.result?.video_url
  const vHasVideo = !!videoUrl
  // 视频半区：只在视频已出片时显示首帧（idle 不拿 fan-out 兜底场景图误导）
  const vImg = vHasVideo ? video?.firstFrame : undefined
  // 链镜左半 = 上一镜真尾帧，仅当已注入(first_frame_from_chain)才显示；否则占位。普通镜 = 关键帧图
  const kfImg = isLink ? (video?.firstFrameFromChain ? video?.firstFrame : undefined) : kf?.result?.image_url
  const anyRunning =
    String(kf?.status || '').toLowerCase() === 'running' || String(video?.status || '').toLowerCase() === 'running'
  return (
    // 外层不裁剪：放运行电光圈(inset:-3px 外扩)+ Handle，避免被内层 overflow-hidden 裁掉看着错位
    <div className="relative w-[240px] h-[200px]">
      <Handle type="target" position={Position.Left} className="!w-2 !h-2 !bg-octopus-muted/70 !border !border-octopus-bg" />
      {anyRunning && <div className="node-running-beam" aria-hidden />}
      <div
        className={`absolute inset-0 rounded-xl border-2 bg-[#191926] overflow-hidden flex flex-col ${
          isSelected ? 'border-octopus-primary ring-2 ring-octopus-primary/50' : 'border-slate-600'
        }`}
        onClick={(e) => { e.stopPropagation(); setSelectedNode(video?.id || kf?.id) }}
      >
        <div className="flex items-center justify-between px-2.5 py-1.5 border-b border-white/5">
          <span className="text-xs font-mono font-semibold text-octopus-text">{shotLabel}</span>
          {chainRole && (
            <span className="text-[9px] px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300">
              {chainRole === 'head' ? '链首' : '🔗 链'}
            </span>
          )}
        </div>
        <div className={`flex-1 flex ${wide ? 'flex-col' : ''}`}>
          <ShotHalf
            label={isLink ? '续接帧' : '关键帧'}
            node={kf}
            imageUrl={kfImg}
            placeholder={isLink && !kfImg ? '🔗 续上一镜' : null}
            cornerLabel={isLink ? '🔗 续上一镜' : undefined}
            dot={kdot}
            onSelect={setSelectedNode}
            onAct={act}
          />
          <div className={wide ? 'h-px bg-white/5' : 'w-px bg-white/5'} />
          <ShotHalf
            label="视频"
            node={video}
            imageUrl={vImg}
            placeholder={!vImg && vHasVideo ? '🎬 已生成' : null}
            badge={vHasVideo ? '▶' : undefined}
            onBadgeClick={videoUrl ? () => setPlaying(true) : undefined}
            dot={vdot}
            onSelect={setSelectedNode}
            onAct={act}
          />
        </div>
      </div>
      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !bg-octopus-primary/70 !border !border-octopus-bg" />
      {playing && videoUrl &&
        createPortal(
          <div
            className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/80 backdrop-blur-sm"
            onClick={() => setPlaying(false)}
          >
            <div className="relative" onClick={(e) => e.stopPropagation()}>
              <video src={videoUrl} controls autoPlay className="max-h-[80vh] max-w-[80vw] rounded-lg shadow-2xl" />
              <button
                onClick={() => setPlaying(false)}
                className="absolute -right-3 -top-3 flex h-8 w-8 items-center justify-center rounded-full border border-octopus-border bg-octopus-panel text-octopus-text hover:border-octopus-primary"
              >
                ✕
              </button>
              <div className="mt-2 text-center text-xs text-octopus-muted">{shotLabel} · 点击空白处关闭</div>
            </div>
          </div>,
          document.body,
        )}
    </div>
  )
}

// 0.7.x Stage2：上游"筹划"折叠芯片（点击展开成迷你管线）
function GroupChipNode({ data }: any) {
  return (
    <div
      className="w-[300px] rounded-2xl border border-octopus-border bg-octopus-panel/95 backdrop-blur px-4 py-3 cursor-pointer hover:border-octopus-primary/60 shadow-lg flex items-center justify-between gap-3"
      onClick={(e) => { e.stopPropagation(); data.onToggle?.() }}
      title="展开筹划阶段（总美术 / 分集 / 分镜脚本）"
    >
      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !bg-octopus-primary/70 !border !border-octopus-bg" />
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-lg">📋</span>
        <div className="min-w-0">
          <div className="text-sm font-semibold text-octopus-text">
            筹划 <span className="text-xs font-normal text-octopus-muted">· {data.count} 节点</span>
          </div>
          <div className="text-[10px] text-octopus-muted truncate">总美术 · 分集 · 分镜脚本</div>
        </div>
      </div>
      <ChevronDown className="w-4 h-4 flex-shrink-0 -rotate-90 text-octopus-muted" />
    </div>
  )
}

const nodeTypes = {
  custom: CustomNode,
  phaseBand: PhaseBandNode,
  shotCard: ShotCardNode,
  groupChip: GroupChipNode,
  shotGroup: ShotGroupNode,
}

// ═══════════════════════════════════════════════════════
// 0.7.1 镜头导航面板：始终清晰、可滚动的镜头列表（不靠画布缩放找镜头）。
// 点一行 → 跳到该镜并选中（focusNode 落 0.8 = 大卡）。每行：缩略图 + SHOT_NN + 关键帧/视频两段状态点。
// ═══════════════════════════════════════════════════════
function ShotNavigator({
  nodes,
  selectedId,
  onJump,
}: {
  nodes: WorkflowNode[]
  selectedId: string | null
  onJump: (id: string) => void
}) {
  const [open, setOpen] = useState(true)
  const [filter, setFilter] = useState<'all' | 'todo' | 'error'>('all')

  const shots = React.useMemo(() => {
    const byShot = new Map<number, { idx: number; kf?: WorkflowNode; video?: WorkflowNode }>()
    for (const n of nodes || []) {
      if (n.type !== 'keyframe_generation' && n.type !== 'video_generation') continue
      const si = (n as any).params?.shot_index
      if (typeof si !== 'number') continue
      const e = byShot.get(si) || { idx: si }
      if (n.type === 'keyframe_generation') e.kf = n
      else e.video = n
      byShot.set(si, e)
    }
    return Array.from(byShot.values()).sort((a, b) => a.idx - b.idx)
  }, [nodes])

  const lc = (n?: WorkflowNode) => String(n?.status || '').toLowerCase()
  const isTodo = (n?: WorkflowNode) =>
    !!n && (['idle', 'pending', 'queued'].includes(lc(n)) || (lc(n) === 'completed' && !n.confirmed))
  const isErr = (n?: WorkflowNode) => lc(n) === 'error'
  const dotOf = (n?: WorkflowNode) => (n ? statusDotColors[lc(n)] || '#3a3a4a' : '#2b2b3a')

  const todoCount = shots.filter((sh) => isTodo(sh.kf) || isTodo(sh.video)).length
  const errCount = shots.filter((sh) => isErr(sh.kf) || isErr(sh.video)).length

  const filtered = shots.filter((sh) => {
    if (filter === 'all') return true
    if (filter === 'todo') return isTodo(sh.kf) || isTodo(sh.video)
    return isErr(sh.kf) || isErr(sh.video)
  })

  if (!shots.length) return null

  return (
    <div className="pointer-events-auto w-[206px] rounded-2xl border border-octopus-border/70 bg-octopus-panel/95 backdrop-blur-md shadow-[0_8px_32px_rgba(0,0,0,0.5)] overflow-hidden">
      {/* 头部 */}
      <button
        onClick={() => setOpen((v) => !v)}
        className="relative flex w-full items-center justify-center gap-2 bg-gradient-to-r from-octopus-primary/12 via-transparent to-transparent px-3 py-2 transition-colors hover:from-octopus-primary/20"
      >
        <ListVideo className="h-[18px] w-[18px] text-octopus-primary" />
        <span className="text-sm font-semibold text-octopus-text">镜头</span>
        <span className="rounded-full bg-octopus-primary/15 px-1.5 py-0.5 text-[11px] font-medium text-octopus-primary">
          {shots.length}
        </span>
        <ChevronDown
          className={`absolute right-3 h-4 w-4 text-octopus-muted transition-transform duration-200 ${open ? '' : '-rotate-90'}`}
        />
      </button>

      {open && (
        <>
          {/* 分段筛选 */}
          <div className="mx-2 mt-1.5 mb-2 flex gap-0.5 p-0.5 rounded-lg bg-octopus-bg/70">
            {([['all', '全部', 0], ['todo', '待办', todoCount], ['error', '失败', errCount]] as const).map(
              ([k, label, cnt]) => (
                <button
                  key={k}
                  onClick={() => setFilter(k)}
                  className={`flex-1 flex items-center justify-center gap-1 text-[11px] py-1 rounded-md transition-all ${
                    filter === k ? 'bg-octopus-primary/25 text-octopus-primary' : 'text-octopus-muted hover:text-octopus-text'
                  }`}
                >
                  {label}
                  {cnt > 0 && <span className="text-[9px] opacity-70 tabular-nums">{cnt}</span>}
                </button>
              ),
            )}
          </div>

          {/* 镜头行 */}
          <div className="max-h-[400px] overflow-y-auto pb-1.5 [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-white/15 hover:[&::-webkit-scrollbar-thumb]:bg-white/25">
            {filtered.map((sh) => {
              const jumpId = sh.kf?.id || sh.video?.id
              // 连续镜无关键帧 → 用视频真尾帧兜底（仅 first_frame_from_chain，不用 fan-out 兜底场景图）
              const thumb =
                (sh.kf as any)?.result?.image_url ||
                (sh.video as any)?.result?.first_frame_url ||
                (sh.video as any)?.result?.image_url ||
                ((sh.video as any)?.params?.first_frame_from_chain
                  ? (sh.video as any)?.params?.first_frame_url
                  : undefined)
              const active = !!selectedId && (sh.kf?.id === selectedId || sh.video?.id === selectedId)
              const m = (sh.kf?.label || sh.video?.label || '').match(/SHOT[_ ]?(\d+)/i)
              const shotLabel = m ? `SHOT_${m[1].padStart(2, '0')}` : `SHOT_${String(sh.idx + 1).padStart(2, '0')}`
              const mainDot = dotOf(sh.video || sh.kf)
              return (
                <button
                  key={sh.idx}
                  onClick={() => jumpId && onJump(jumpId)}
                  className={`relative w-full flex items-center gap-2.5 pl-3 pr-2.5 py-1.5 text-left transition-colors ${
                    active ? 'bg-octopus-primary/15' : 'hover:bg-white/[0.06]'
                  }`}
                >
                  {active && <span className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r-full bg-octopus-primary" />}
                  {/* 缩略图 / 空态状态色块 */}
                  {thumb ? (
                    <img
                      src={thumb}
                      alt=""
                      draggable={false}
                      className="w-7 h-7 rounded-md object-cover ring-1 ring-white/10 flex-shrink-0"
                    />
                  ) : (
                    <span
                      className="w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 border border-white/5"
                      style={{ backgroundColor: mainDot + '1f' }}
                    >
                      <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: mainDot }} />
                    </span>
                  )}
                  {/* 编号 */}
                  <span
                    className={`flex-1 truncate text-xs font-mono tracking-tight ${
                      active ? 'text-octopus-primary' : 'text-octopus-text'
                    }`}
                  >
                    {shotLabel}
                  </span>
                  {/* 管线两段：关键帧 | 视频 */}
                  <span className="flex items-center gap-0.5 flex-shrink-0" title="关键帧 / 视频">
                    <span className="w-3 h-1.5 rounded-sm" style={{ backgroundColor: dotOf(sh.kf) }} />
                    <span className="w-3 h-1.5 rounded-sm" style={{ backgroundColor: dotOf(sh.video) }} />
                  </span>
                </button>
              )
            })}
            {!filtered.length && (
              <div className="px-3 py-5 text-center text-xs text-octopus-muted">
                无{filter === 'todo' ? '待办' : '失败'}镜头 🎉
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════════
// 主画布组件
// ═══════════════════════════════════════════════════════
export default function FlowCanvas() {
  const dag = useStore((s) => s.dag)
  const currentProject = useStore((s) => s.currentProject)
  const selectedNode = useStore((s) => s.selectedNode)
  const setSelectedNode = useStore((s) => s.setSelectedNode)
  const removeNodes = useStore((s) => s.removeNodes)
  const addMessage = useStore((s) => s.addMessage)
  const manualPositions = useStore((s) => s.manualPositions)
  const setNodePosition = useStore((s) => s.setNodePosition)
  const resetNodePositions = useStore((s) => s.resetNodePositions)
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  // 0.7.x Stage2：上游"筹划"段是否折叠成芯片（仅当已 fan-out 出分镜网格时生效）。默认折叠，专注网格。
  const [planningCollapsed, setPlanningCollapsed] = useState(true)
  const { fitView, setCenter, getNode, getViewport } = useReactFlow()

  // 上一帧节点 id 集合（检测"确认关键帧后凭空多出的视频节点"）+ 待办导航游标
  const prevNodeIdsRef = useRef<Set<string>>(new Set())
  const navCursorRef = useRef(0)
  // 0.7.x：真实 keyframe/video 节点 id → 所属分镜卡 shot-<idx>（导航/聚焦居中到卡、但选中真实节点）
  const cardOfNodeRef = useRef<Map<string, string>>(new Map())

  // 把某节点平移到视图中央（保持当前缩放，过小则微调到可读），可选同时选中以打开编辑面板
  const focusNode = useCallback(
    (nodeId: string, opts?: { select?: boolean; fallback?: { x: number; y: number } }) => {
      const n = getNode(nodeId)
      const px = n?.positionAbsolute?.x ?? n?.position?.x ?? opts?.fallback?.x
      const py = n?.positionAbsolute?.y ?? n?.position?.y ?? opts?.fallback?.y
      if (px == null || py == null) return
      const w = n?.width ?? 260
      const h = n?.height ?? 220
      const z = Math.max(getViewport().zoom, 0.8)
      setCenter(px + w / 2, py + h / 2, { zoom: z, duration: 600 })
      if (opts?.select) setSelectedNode(nodeId)
    },
    [getNode, getViewport, setCenter, setSelectedNode],
  )

  // 切换项目：重置视口 diff 基线，让新项目首帧能 fitView（不依赖 resetWorkflow 是否被调用）
  useEffect(() => {
    prevNodeIdsRef.current = new Set()
  }, [currentProject?.id])

  // 键盘删除（Delete / Backspace）— 删当前选中的节点 + 级联下游
  useEffect(() => {
    const onKey = async (e: KeyboardEvent) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      // 输入框内不拦截
      const tag = (e.target as HTMLElement)?.tagName
      const editable = (e.target as HTMLElement)?.isContentEditable
      if (tag === 'INPUT' || tag === 'TEXTAREA' || editable) return
      if (!selectedNode || !currentProject?.id) return
      e.preventDefault()
      const node = dag?.nodes.find((n) => n.id === selectedNode)
      const label = node?.label || selectedNode
      if (!confirm(`删除节点「${label}」及其下游所有节点？`)) return
      try {
        const res = await workflowAPI.deleteNode(currentProject.id, selectedNode, true)
        const ids: string[] = res.data?.node_ids || [selectedNode]
        removeNodes(ids)
        setSelectedNode(null)
        addMessage({
          id: `del_${Date.now()}`,
          role: 'agent',
          content: `🗑️ 已删除 ${ids.length} 个节点（含级联下游）`,
          timestamp: Date.now(),
        })
      } catch (err) {
        addMessage({
          id: `del_err_${Date.now()}`,
          role: 'agent',
          content: '删除失败：' + (err as Error).message,
          timestamp: Date.now(),
        })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selectedNode, currentProject?.id, dag, removeNodes, setSelectedNode, addMessage])

  // Sync DAG to ReactFlow nodes/edges — 支持动态添加
  useEffect(() => {
    if (!dag?.nodes || dag.nodes.length === 0) {
      setNodes([])
      setEdges([])
      prevNodeIdsRef.current = new Set()  // 清空/切项目：重置基线，让下个项目首帧能 fitView
      return
    }

    // 0.7.x 网格模式：keyframe/video 合并进"分镜卡"；Stage2：已出网格时上游"筹划"段可折叠成芯片。
    const SHOT_TYPES = ['keyframe_generation', 'video_generation']
    const hasCards = dag.nodes.some(
      (n) =>
        (n.type === 'keyframe_generation' || n.type === 'video_generation') &&
        typeof (n.params as any)?.shot_index === 'number',
    )
    const collapsed = planningCollapsed && hasCards

    // 上游节点（非 shot / 非 merge）—— 展开时用于连边（保持宽集合，含 audio/publish 等下游，才有连线）。
    const upstreamNodes = dag.nodes.filter((n) => !SHOT_TYPES.includes(n.type) && n.type !== 'video_merge' && (collapsed || n.type !== 'result'))
    // 折叠时收进芯片的**只有真正的筹划类型**（剧集拆分/剧本/美术/分镜脚本/美术子图）。
    // 老 bug：以前把"除 shot/merge 外的所有节点"都收进芯片 → audio/bgm/publish/localization/cost_summary
    // 这些下游独立节点被一起藏掉。改用显式白名单，下游节点不再被"收起筹划"波及。
    const PLANNING_TYPES = ['episode_breakdown', 'script_generation', 'art_design', 'episode_art_design', 'storyboard_design', 'result']
    const planningNodes = dag.nodes.filter((n) => PLANNING_TYPES.includes(n.type))
    const planningTypes = collapsed ? Array.from(new Set(planningNodes.map((n) => n.type))) : []
    // 展开时美术子图(result)进 art-group 组容器，不走普通 DAG 布局
    const layoutedNodes = layoutDAG(dag.nodes, { excludeTypes: [...SHOT_TYPES, ...planningTypes, ...(collapsed ? [] : ['result'])] })

    // 折叠芯片
    let chipNodes: Node[] = []
    if (collapsed) {
      chipNodes = [
        {
          id: 'chip-planning',
          type: 'groupChip',
          position: { x: 80, y: 80 },
          width: 300,
          height: 76,
          data: { count: planningNodes.length, onToggle: () => setPlanningCollapsed(false) },
          draggable: false,
          selectable: true,
        } as Node,
      ]
    }

    // 组容器宽度 layoutDAG 不感知 → 建完每个容器后，把它右侧的所有列整体右移让位，否则会压列堆叠。
    const shiftRightOf = (baseX: number, clearRight: number) => {
      const rights = layoutedNodes.filter((n) => n.position.x > baseX + 10)
      if (!rights.length) return
      const minX = Math.min(...rights.map((n) => n.position.x))
      const delta = clearRight + 100 - minX
      if (delta > 0) rights.forEach((n) => { n.position = { ...n.position, x: n.position.x + delta } })
    }

    // 美术素材组：美术子图(result)网格排进 art-group 容器，接在美术设计右侧；右侧列让位
    let artGroupNodes: Node[] = []
    let artChildren: Node[] = []
    if (!collapsed) {
      const ad = layoutedNodes.find((n) => {
        const t = (n.data as any)?.type
        return t === 'art_design' || t === 'episode_art_design'
      })
      const artPos = ad ? { x: ad.position.x + 260 + 80, y: ad.position.y } : { x: 80, y: 80 }
      const ag = buildArtGroup(dag.nodes, 'art-group', artPos, '🎨 美术素材')
      artGroupNodes = ag.group
      artChildren = ag.children
      const artW = (artGroupNodes[0] as any)?.width || 0
      if (ad && artW) shiftRightOf(ad.position.x, artPos.x + artW)
    }

    // 镜头网格组位置：storyboard(已让位右移)右侧；建完后合并/发布等再让位
    let gridGroupPos = { x: 80, y: 80 }
    if (collapsed) {
      gridGroupPos = { x: 460, y: 80 } // 芯片(80+300)右侧
    } else {
      const sb = layoutedNodes.find((n) => (n.data as any)?.type === 'storyboard_design')
      if (sb) {
        gridGroupPos = { x: sb.position.x + 260 + 80, y: sb.position.y } // storyboard 右侧一列
      } else if (layoutedNodes.length) {
        const maxX = Math.max(...layoutedNodes.map((n) => n.position.x))
        const minY = Math.min(...layoutedNodes.map((n) => n.position.y))
        gridGroupPos = { x: maxX + 340, y: minY }
      }
    }
    // 分镜卡用相对坐标挂到 grid-group 容器下（subflow），拖容器由 RF 原生带动所有卡
    const { cards, gridBox, cardOfNode } = buildShotCards(dag.nodes, { x: 0, y: 0 }, 'grid-group')
    cardOfNodeRef.current = cardOfNode
    const groupNodes = buildShotGroupNode('grid-group', gridBox, gridGroupPos, cards.length, '🎬 镜头网格')
    if (gridBox) {
      if (collapsed) {
        // 折叠：上游全收进芯片，merge 等残余节点会被 layoutDAG 排进网格区域 → 让位到网格右侧 + 垂直对齐网格中线
        shiftRightOf(gridGroupPos.x - 1, gridGroupPos.x + gridBox.w)
        for (const n of layoutedNodes) {
          if ((n.data as any)?.type === 'video_merge') {
            const h = ((n.data as any)?.estHeight as number) || 220
            n.position = { ...n.position, y: gridGroupPos.y + Math.max(0, (gridBox.h - h) / 2) }
          }
        }
      } else {
        const sb = layoutedNodes.find((n) => (n.data as any)?.type === 'storyboard_design')
        if (sb) shiftRightOf(sb.position.x, gridGroupPos.x + gridBox.w)
      }
    }

    // 整段音频（audio_plan / bgm_fetch，简易/进阶模式）归位到镜头网格组正下方、横排一行（绝对坐标）
    if (gridBox) {
      let ax = gridGroupPos.x
      const ay = gridGroupPos.y + gridBox.h + 60
      // 计划节点（audio_plan/episode_audio_plan）放行首；bgm_fetch 每行 3 个紧凑网格排右侧
      const bgmNodes: any[] = []
      for (const n of layoutedNodes) {
        const t = (n.data as any)?.type
        if (t === 'audio_plan' || t === 'episode_audio_plan') {
          n.position = { x: ax, y: ay }
          ax += 320
        } else if (t === 'bgm_fetch') {
          bgmNodes.push(n)
        }
      }
      bgmNodes.forEach((n, i) => {
        const col = i % 3
        const row = Math.floor(i / 3)
        n.position = { x: ax + 40 + col * 300, y: ay + row * 240 }
      })
      // 折叠模式：下游尾部节点（发布/本地化/结算/BGM方向）不再被收进芯片，
      // 排在音频行下方一行，免得落在 layoutDAG 的筹划空位里错位（展开模式仍走 DAG 布局）。
      if (collapsed) {
        let tx = gridGroupPos.x
        const ty = ay + 260
        for (const n of layoutedNodes) {
          const t = (n.data as any)?.type
          if (t === 'publish' || t === 'localization' || t === 'cost_summary' || t === 'bgm_direction') {
            n.position = { x: tx, y: ty }
            tx += 320
          }
        }
      }
    }

    // 边：上游 depends_on（展开）+ storyboard/芯片 → 网格组 → 视频合成 + 分镜卡连贯链
    const upstreamEdges = collapsed ? [] : generateEdges(upstreamNodes)
    const bridgeEdges: any[] = []
    if (cards.length) {
      const edgeStyle = { stroke: 'rgba(214,222,244,0.18)', strokeWidth: 1.5 }
      const srcId = collapsed ? 'chip-planning' : dag.nodes.find((n) => n.type === 'storyboard_design')?.id
      if (srcId) {
        bridgeEdges.push({
          id: `bridge-${srcId}-grid`, source: srcId, target: 'grid-group',
          type: 'smoothstep', animated: false, style: edgeStyle,
        })
      }
      const mergeId = dag.nodes.find((n) => n.type === 'video_merge')?.id
      if (mergeId) {
        bridgeEdges.push({
          id: `bridge-grid-${mergeId}`, source: 'grid-group', target: mergeId,
          type: 'smoothstep', animated: false, style: edgeStyle,
        })
      }
    }
    // 美术设计 → 美术素材组
    if (artChildren.length) {
      const adId = dag.nodes.find((n) => n.type === 'art_design' || n.type === 'episode_art_design')?.id
      if (adId) {
        bridgeEdges.push({
          id: `bridge-${adId}-art`, source: adId, target: 'art-group',
          type: 'smoothstep', animated: false, style: { stroke: 'rgba(214,222,244,0.18)', strokeWidth: 1.5 },
        })
      }
    }
    const shotEdges = buildShotCardEdges(dag.nodes)

    const bands = generatePhaseBands(layoutedNodes)
    // 注入用户手动拖动位置（按项目）：有记录的节点用记住的坐标覆盖自动布局
    const saved = (currentProject?.id ? manualPositions[currentProject.id] : null) || {}
    const withManual = (arr: any[]) => arr.map((n: any) => (saved[n.id] ? { ...n, position: saved[n.id] } : n))
    // grid-group 必须排在子卡(cards)之前 —— RF 要求 parent 先于 child
    setNodes([...withManual(bands), ...withManual(chipNodes), ...withManual(layoutedNodes), ...withManual(artGroupNodes), ...withManual(artChildren), ...withManual(groupNodes), ...withManual(cards)])
    setEdges([...upstreamEdges, ...bridgeEdges, ...shotEdges])

    // 视口策略（基于**原始 dag 节点 id** 集合 diff —— 含被合进卡的 video，才能检测"确认关键帧后新出的视频"）：
    const prevIds = prevNodeIdsRef.current
    const currentIds = dag.nodes.map((n) => n.id)
    const added = currentIds.filter((id) => !prevIds.has(id))
    prevNodeIdsRef.current = new Set(currentIds)

    if (prevIds.size === 0) {
      // 只在"画布从空 → 有节点"那一次 fitView（网格近方形，自然落 ~0.4-0.5）。之后尊重用户 viewport。
      setTimeout(() => fitView({ padding: 0.15, duration: 500, maxZoom: 1 }), 100)
    } else if (added.length > 2) {
      // 批量新增：若含 shot 节点 → 分镜网格刚(重)建，fitView 让用户看到整张网格（否则网格在下方看不到）
      const fannedOut = added.some((id) => {
        const n = dag.nodes.find((x) => x.id === id)
        return !!n && SHOT_TYPES.includes(n.type)
      })
      if (fannedOut) setTimeout(() => fitView({ padding: 0.15, duration: 500, maxZoom: 1 }), 150)
    } else if (added.length > 0) {
      // 确认关键帧后 eager 出 video 节点 → 落进对应分镜卡（卡已存在、不算新增渲染节点）。
      // 居中那张卡 + 选中真实视频节点（打开「开始生成」面板）。
      const newVideoId = added
        .map((id) => dag.nodes.find((n) => n.id === id))
        .find((n) => n && n.type === 'video_generation')?.id
      if (newVideoId) {
        const cardId = cardOfNode.get(newVideoId)
        setTimeout(() => {
          if (cardId) focusNode(cardId, { select: false })
          setSelectedNode(newVideoId)
        }, 160)
      }
    }
  }, [dag, planningCollapsed, manualPositions, currentProject?.id, setNodes, setEdges, fitView, focusNode, setSelectedNode])

  // WebSocket 连接
  const wsConnectedRef = useRef<string | null>(null)
  useEffect(() => {
    if (currentProject?.id && wsConnectedRef.current !== currentProject.id) {
      wsConnectedRef.current = currentProject.id
      connectWebSocket(currentProject.id)
    }
    return () => {
      disconnectWebSocket()
      wsConnectedRef.current = null
    }
  }, [currentProject?.id])

  // 待办导航：idle 的关键帧/视频（待生成）+ completed 未确认（待确认）—— 给"节点多找不到"一个跳转入口
  const actionable = React.useMemo(() => {
    const list = (dag?.nodes || []) as WorkflowNode[]
    return list.filter((n) => {
      const s = String(n.status || '').toLowerCase()
      const pendingMedia =
        (n.type === 'keyframe_generation' || n.type === 'video_generation') &&
        (s === 'idle' || s === 'pending' || s === 'queued')
      const pendingConfirm = s === 'completed' && !n.confirmed
      return pendingMedia || pendingConfirm
    })
  }, [dag])

  // 0.7.x Stage2：是否已 fan-out 出分镜网格（决定是否显示"折叠筹划"开关）
  const hasShotCards = React.useMemo(
    () =>
      (dag?.nodes || []).some(
        (n: any) =>
          (n.type === 'keyframe_generation' || n.type === 'video_generation') &&
          typeof n.params?.shot_index === 'number',
      ),
    [dag],
  )

  const gotoNextActionable = useCallback(() => {
    if (actionable.length === 0) return
    const cur = selectedNode ? actionable.findIndex((n) => n.id === selectedNode) : -1
    const idx = cur >= 0
      ? (cur + 1) % actionable.length
      : ((navCursorRef.current % actionable.length) + actionable.length) % actionable.length
    navCursorRef.current = idx + 1
    // 0.7.x：列表是真实 kf/video 节点；居中其所属分镜卡，但选中真实节点（合成 id 不能进 selectedNode）
    const target = actionable[idx]
    const cardId = cardOfNodeRef.current.get(target.id)
    focusNode(cardId || target.id, { select: false })
    setSelectedNode(target.id)
  }, [actionable, selectedNode, focusNode, setSelectedNode])

  // 拖动结束 → 记住该节点手动位置（按项目持久化，自动布局不再覆盖）
  const onNodeDragStop = useCallback((_e: any, node: any) => {
    if (!currentProject?.id || !node?.id) return
    setNodePosition(currentProject.id, node.id, node.position)
  }, [currentProject?.id, setNodePosition])

  // 「自动排版」：清掉本项目手动位置 → 回到算法布局 + 居中
  const handleAutoLayout = useCallback(() => {
    if (currentProject?.id) resetNodePositions(currentProject.id)
    setTimeout(() => fitView({ padding: 0.15, duration: 400 }), 80)
  }, [currentProject?.id, resetNodePositions, fitView])

  return (
    <div className="w-full h-full bg-octopus-bg relative">
      <CanvasBreadcrumb />
      <FanoutEpisodesButton />
      <BatchGenerateButton />

      {/* 左侧悬浮控件组：整体垂直居中（展开筹划 + 镜头列表一组） */}
      <div className="pointer-events-none absolute left-3 top-1/2 z-20 flex -translate-y-1/2 flex-col gap-2">
        {/* 0.7.x Stage2：折叠/展开上游"筹划"段（仅有分镜网格时显示） */}
        {hasShotCards && (
          <button
            onClick={() => setPlanningCollapsed((v) => !v)}
            className="pointer-events-auto flex w-[206px] items-center justify-center gap-2 rounded-xl border border-octopus-border bg-octopus-panel/95 px-3 py-2 text-sm font-semibold text-octopus-text shadow-lg backdrop-blur transition-colors hover:border-octopus-primary/60"
            title="折叠/展开上游筹划阶段（总美术 / 分集 / 分镜脚本）"
          >
            <ClipboardList className="h-[18px] w-[18px] text-octopus-primary" />
            {planningCollapsed ? '展开筹划' : '收起筹划'}
          </button>
        )}

        {/* 自动排版：清掉手动拖动、恢复算法布局并居中 */}
        <button
          onClick={handleAutoLayout}
          className="pointer-events-auto flex w-[206px] items-center justify-center gap-2 rounded-xl border border-octopus-border bg-octopus-panel/95 px-3 py-2 text-sm font-semibold text-octopus-text shadow-lg backdrop-blur transition-colors hover:border-octopus-primary/60"
          title="清除手动拖动、恢复自动排版并居中"
        >
          <LayoutGrid className="h-[18px] w-[18px] text-octopus-primary" />
          自动排版
        </button>

        {/* 0.7.1 镜头导航：左侧始终清晰的镜头列表，点一行直达（不靠画布缩放找镜头） */}
        <ShotNavigator
          nodes={dag?.nodes || []}
          selectedId={selectedNode}
          onJump={(realId) => {
            const cardId = cardOfNodeRef.current.get(realId)
            focusNode(cardId || realId, { select: false })
            setSelectedNode(realId)
          }}
        />
      </div>

      {/* 待办导航：节点多时一键跳到下一个待生成/待确认节点（平移 + 选中打开面板） */}
      {actionable.length > 0 && (
        <button
          onClick={gotoNextActionable}
          className="pointer-events-auto absolute bottom-5 left-1/2 -translate-x-1/2 z-10 flex items-center gap-2 px-4 py-2 rounded-full bg-octopus-panel/90 backdrop-blur border border-octopus-border text-octopus-text text-sm font-medium shadow-lg hover:border-octopus-primary/60 hover:shadow-[0_0_18px_rgba(34,211,238,0.35)] transition-all"
          title="跳到下一个待处理节点（待生成 / 待确认），逐个 step through"
        >
          <Target className="w-4 h-4 text-octopus-primary" />
          待办 {actionable.length} · 下一个
        </button>
      )}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeDragStop={onNodeDragStop}
        nodesDraggable
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.1}
        maxZoom={2}
        defaultEdgeOptions={{ style: { stroke: '#2a2a3a', strokeWidth: 1.5 } }}
        proOptions={{ hideAttribution: true }}
        onlyRenderVisibleElements
      >
        <Background variant={BackgroundVariant.Dots} color="#2a2a40" gap={20} size={2} />
        <Controls className="!bg-octopus-panel !border-octopus-border !text-octopus-text" />
        <MiniMap
          className="!bg-octopus-panel !border-octopus-border"
          style={{ width: 200, height: 152 }}
          pannable
          zoomable
          maskColor="rgba(8, 8, 12, 0.82)"
          nodeStrokeWidth={3}
          nodeColor={(n: any) =>
            n.type === 'phaseBand'
              ? 'rgba(120, 130, 165, 0.10)' // 分区带：极淡灰，当作背景区块、不抢眼
              : n.type === 'groupChip'
              ? 'rgba(34, 211, 238, 0.45)' // 筹划芯片：主色
              : n.type === 'shotCard'
              ? statusDotColors[String(n.data?.video?.status || n.data?.kf?.status || 'idle').toLowerCase()] ||
                nodeColors.default
              : statusDotColors[n.data?.status] || nodeColors[n.data?.type] || nodeColors.default
          }
        />
      </ReactFlow>
    </div>
  )
}
