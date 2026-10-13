/**
 * 0.7.2 ②：bgm_direction 节点（series 级 BGM 风格方向）— 创作者友好视图。
 * 配乐总监通读整剧定的音乐"宪法"：全剧基调 / 情绪调色板 / 主题复用建议。
 */
import React from 'react'
import type { WorkflowNode } from '@/types'

interface Props {
  node: WorkflowNode
}

export default function BgmDirectionEditor({ node }: Props) {
  const r = (node.result || {}) as Record<string, any>
  const palette: any[] = Array.isArray(r.mood_palette) ? r.mood_palette : []
  const avoid: any[] = Array.isArray(r.avoid) ? r.avoid : []

  if (!r.overall_style) {
    return <div className="text-xs text-octopus-muted py-4 text-center">🎵 配乐总监正在通读整剧、规划风格基调...</div>
  }

  return (
    <div className="space-y-3">
      <div className="text-[10px] text-octopus-muted leading-snug pb-2 border-b border-octopus-border">
        全剧 BGM 的"宪法"——之后每一集的场景配乐都会在这份方向约束下进行，保证整剧音乐统一。
      </div>

      <div className="p-2 rounded bg-octopus-bg/40 border border-octopus-border">
        <div className="text-[10px] text-octopus-muted mb-0.5">🎵 全剧音乐基调</div>
        <div className="text-sm text-octopus-text">{r.overall_style}</div>
      </div>

      {Array.isArray(r.style_tags) && r.style_tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {r.style_tags.map((t: string, i: number) => (
            <span key={i} className="px-1.5 py-0.5 rounded bg-teal-500/15 text-teal-200 text-[10px]">{t}</span>
          ))}
        </div>
      )}

      {palette.length > 0 && (
        <div className="space-y-1">
          <div className="text-xs font-medium text-octopus-text">🎚 情绪调色板（剧情情绪 → 音乐处理）</div>
          {palette.map((p, i) => (
            <div key={i} className="flex gap-2 text-[11px] rounded border border-octopus-border bg-octopus-panel/40 p-1.5">
              <span className="text-octopus-text flex-shrink-0">{p?.mood}</span>
              <span className="text-octopus-muted truncate" title={p?.music}>{p?.music}</span>
            </div>
          ))}
        </div>
      )}

      {r.motif_notes && (
        <div className="p-2 rounded bg-emerald-500/10 border border-emerald-500/30">
          <div className="text-[10px] text-emerald-300 mb-0.5">♻ 主题动机与复用建议</div>
          <div className="text-[11px] text-octopus-chat leading-snug">{r.motif_notes}</div>
        </div>
      )}

      {avoid.length > 0 && (
        <div className="text-[10px] text-octopus-muted">🚫 全剧避免：{avoid.join('、')}</div>
      )}

      {r.reasoning && (
        <details className="pt-2 border-t border-octopus-border">
          <summary className="cursor-pointer text-[10px] text-octopus-muted hover:text-octopus-text">为什么是这套基调</summary>
          <div className="mt-1 text-[10px] text-octopus-chat leading-snug">{r.reasoning}</div>
        </details>
      )}
    </div>
  )
}
