/**
 * 0.7.2 ②：episode_audio_plan 节点 — 本集场景 BGM 计划的创作者友好视图。
 * 每段=一个场景时间窗（窗内多镜共用一段音乐）；同主题可跨场景/跨集复用（♻）。
 * 确认节点后才会真正生成音乐（spawn bgm_fetch）。
 */
import React from 'react'
import type { WorkflowNode } from '@/types'

interface Props {
  node: WorkflowNode
}

const INTENSITY_LABEL: Record<string, string> = {
  low: '轻', medium: '中', high: '强', rising: '渐强', falling: '渐弱',
}

const mmss = (v: any) => {
  const s = Math.max(0, Number(v) || 0)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export default function EpisodeAudioEditor({ node }: Props) {
  const result = (node.result || {}) as Record<string, any>
  const plan: any[] = Array.isArray(result.scene_bgm_plan) ? result.scene_bgm_plan : []

  if (!plan.length) {
    return (
      <div className="text-xs text-octopus-muted py-4 text-center">
        🎼 音频导演正在按场景规划本集 BGM...
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="text-[10px] text-octopus-muted leading-snug pb-2 border-b border-octopus-border">
        按<b>场景</b>规划的本集配乐：每段音乐铺过该场景的所有镜头（不是每镜一段）；
        同主题可跨场景/跨集复用（标 ♻）。<b>确认后才会真正生成音乐。</b>
      </div>

      {/* 概览 */}
      <div className="flex flex-wrap gap-2 text-[10px]">
        {result.overall_style && (
          <span className="px-2 py-0.5 rounded-full bg-teal-500/20 text-teal-300">
            🎵 全剧基调：{result.overall_style}
          </span>
        )}
        <span className="px-2 py-0.5 rounded-full bg-cyan-500/20 text-cyan-300">
          {plan.length} 段配乐 · {result.track_count || 0} 个主题
        </span>
        {!!result.reuse_count && (
          <span className="px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300">
            ♻ 复用 {result.reuse_count} 个主题（不再花钱生成）
          </span>
        )}
      </div>

      {/* 分段列表 */}
      <div className="space-y-2">
        {plan.map((s, i) => {
          const dur = Math.max(0, (Number(s.t1) || 0) - (Number(s.t0) || 0))
          const isReuse = s.source === 'library'
          return (
            <div key={i} className="rounded border border-octopus-border bg-octopus-panel/40 p-2 space-y-1">
              <div className="flex items-center gap-2 text-xs">
                <span className="font-mono text-octopus-muted flex-shrink-0">
                  {mmss(s.t0)}–{mmss(s.t1)}
                </span>
                <span className="text-octopus-text truncate flex-1">{s.scene_label || s.scene_id}</span>
                <span className="text-[10px] text-octopus-muted flex-shrink-0">{dur}s · {(s.shot_ids || []).length} 镜</span>
              </div>
              <div className="flex items-center gap-2 text-[11px]">
                <span className="text-teal-300">🎵 {s.track_label || s.track_key}</span>
                {isReuse ? (
                  <span className="px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-300 text-[9px]">♻ 复用已有主题</span>
                ) : (
                  <span className="px-1.5 py-0.5 rounded bg-cyan-500/20 text-cyan-300 text-[9px]">🆕 新主题</span>
                )}
                {s.mood && <span className="text-octopus-muted text-[10px]">{s.mood}</span>}
                {s.intensity && (
                  <span className="text-octopus-muted text-[10px]">强度·{INTENSITY_LABEL[s.intensity] || s.intensity}</span>
                )}
              </div>
              {!isReuse && s.prompt && (
                <details>
                  <summary className="cursor-pointer text-[9px] text-octopus-muted hover:text-octopus-text">
                    音乐描述（英文，供生成用）
                  </summary>
                  <div className="mt-1 text-[10px] text-octopus-chat leading-snug">{s.prompt}</div>
                </details>
              )}
            </div>
          )
        })}
      </div>

      {result.reasoning && (
        <details className="pt-2 border-t border-octopus-border">
          <summary className="cursor-pointer text-[10px] text-octopus-muted hover:text-octopus-text">
            🎬 音频导演的思路
          </summary>
          <div className="mt-1 text-[10px] text-octopus-chat leading-snug">{result.reasoning}</div>
        </details>
      )}
    </div>
  )
}
