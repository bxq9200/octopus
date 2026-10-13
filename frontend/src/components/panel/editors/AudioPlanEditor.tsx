/**
 * 音频计划编辑器 (0.6.1)
 *
 * 展示 audio_director_agent 输出的：
 * - overall_mood / style_tags
 * - segments 时间窗（mood / intensity / notes）
 * - mubert_playlist_index_hint
 * - reasoning
 *
 * 暂只读展示；后续可加 segments 拖拽 / 单段 mood 编辑（0.6.2）。
 */
import React from 'react'
import { Field, safeSlice } from './common'
import { WorkflowNode } from '@/types'

interface Segment {
  t0?: number
  t1?: number
  mood?: string
  intensity?: string
  notes?: string
}

const INTENSITY_CLASS: Record<string, string> = {
  low:     'bg-slate-500/20 text-slate-300 border-slate-500/40',
  medium:  'bg-sky-500/20 text-sky-300 border-sky-500/40',
  rising:  'bg-amber-500/20 text-amber-300 border-amber-500/40',
  high:    'bg-red-500/20 text-red-300 border-red-500/40',
  falling: 'bg-cyan-500/20 text-cyan-300 border-cyan-500/40',
}

export default function AudioPlanEditor({
  node,
  readOnly,
}: {
  node: WorkflowNode
  onSave?: (newResult: any) => Promise<void> | void
  readOnly?: boolean
}) {
  const result = (node.result || {}) as any
  const segments: Segment[] = Array.isArray(result.segments) ? result.segments : []
  const overallMood: string = result.overall_mood || ''
  const styleTags: string[] = Array.isArray(result.style_tags) ? result.style_tags : []
  const hint: string = result.mubert_playlist_index_hint || ''
  const reasoning: string = result.reasoning || ''
  const totalDur: number = Number(result.total_duration || 0)

  // 时间轴最大值
  const maxT = Math.max(totalDur, ...segments.map((s) => Number(s.t1 || 0)), 1)

  return (
    <div className="space-y-3 text-xs">
      <Field label="整体基调">
        <div className="text-octopus-text">{overallMood || '—'}</div>
      </Field>

      {styleTags.length > 0 && (
        <Field label="风格标签">
          <div className="flex flex-wrap gap-1">
            {styleTags.map((t) => (
              <span
                key={t}
                className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-300 border border-purple-500/40"
              >
                {t}
              </span>
            ))}
          </div>
        </Field>
      )}

      <Field label={`音频叙事时间轴（共 ${segments.length} 段 · 总 ${totalDur}s）`}>
        {segments.length === 0 ? (
          <div className="text-octopus-muted py-2">尚无 segments</div>
        ) : (
          <div className="space-y-1.5">
            {/* 时间轴可视化 */}
            <div className="relative h-3 rounded bg-octopus-bg border border-octopus-border overflow-hidden">
              {segments.map((s, i) => {
                const t0 = Number(s.t0 || 0)
                const t1 = Number(s.t1 || 0)
                if (t1 <= t0) return null
                const leftPct = (t0 / maxT) * 100
                const widthPct = ((t1 - t0) / maxT) * 100
                const intensityCls = INTENSITY_CLASS[s.intensity || 'medium'] || INTENSITY_CLASS.medium
                return (
                  <div
                    key={i}
                    className={`absolute top-0 bottom-0 ${intensityCls.split(' ')[0]} opacity-80`}
                    style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                    title={`${t0}-${t1}s · ${s.mood} · ${s.intensity}`}
                  />
                )
              })}
            </div>

            {/* segments 列表 */}
            <div className="space-y-1">
              {segments.map((s, i) => (
                <div
                  key={i}
                  className="flex items-center gap-2 px-2 py-1 rounded bg-octopus-bg border border-octopus-border"
                >
                  <span className="text-octopus-muted font-mono w-20 flex-shrink-0">
                    {s.t0 ?? 0}–{s.t1 ?? 0}s
                  </span>
                  <span className="text-octopus-text w-20 flex-shrink-0 truncate">{s.mood || '?'}</span>
                  <span
                    className={`text-[10px] px-1.5 py-0.5 rounded border flex-shrink-0 ${INTENSITY_CLASS[s.intensity || 'medium'] || INTENSITY_CLASS.medium}`}
                  >
                    {s.intensity || 'medium'}
                  </span>
                  {s.notes && (
                    <span className="text-octopus-muted text-[10px] truncate flex-1">
                      {safeSlice(s.notes, 80)}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </Field>

      {(result.mubert_prompt || hint) && (
        <Field label={result.mubert_prompt ? "音乐 prompt (text-to-music)" : "Mubert playlist_index 提示"}>
          {result.mubert_prompt ? (
            <div className="space-y-1">
              <div className="px-2 py-1.5 rounded bg-octopus-bg border border-octopus-border text-octopus-text leading-relaxed">
                {result.mubert_prompt}
              </div>
              <div className="text-[10px] text-octopus-muted">
                intensity: <code className="text-octopus-text">{result.mubert_intensity || 'medium'}</code>
                {hint && <> · 备用 playlist_index（Mubert 兜底）: <code className="text-octopus-text">{hint}</code></>}
              </div>
            </div>
          ) : (
            <code className="text-octopus-text font-mono">{hint}</code>
          )}
        </Field>
      )}

      {reasoning && (
        <Field label="规划说明">
          <div className="text-octopus-muted leading-relaxed">{reasoning}</div>
        </Field>
      )}

      {!readOnly && (
        <div className="text-[10px] text-octopus-muted pt-1 border-t border-octopus-border">
          提示：此节点只读展示。如需调整请重跑节点（右上方 ⋮）— 0.6.2 将开放手动编辑 segments。
        </div>
      )}
    </div>
  )
}
