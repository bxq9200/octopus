/**
 * 0.7.0：专家模式 — chat 里渲染 episode_breakdown_draft 消息
 *
 * 显示：
 * - script_summary（logline / 类型 / 主要角色 chips）
 * - episodes 列表（每集 title / arc / scene_range / 时长 / cliff_hanger）
 * - "✅ 确认分集" 按钮（仅 ready_to_confirm 时）
 */
import React from 'react'
import { Bot, CheckCircle } from 'lucide-react'
import type { ScriptSummary, EpisodeBreakdownItem } from '@/types'

export interface EpisodeBreakdownCardProps {
  scriptSummary?: ScriptSummary | null
  episodes: EpisodeBreakdownItem[]
  readyToConfirm?: boolean
  missingAspects?: string[]
  isAlreadyConfirmed?: boolean
  onConfirm?: () => void
}

export default function EpisodeBreakdownCard(props: EpisodeBreakdownCardProps) {
  const {
    scriptSummary, episodes, readyToConfirm,
    missingAspects, isAlreadyConfirmed, onConfirm,
  } = props

  const totalMin = episodes.reduce((s, e) => s + (e.est_duration_min || 0), 0)

  return (
    <div className="mt-3 p-3 rounded-lg bg-octopus-bg/50 border border-octopus-border space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs text-octopus-muted flex items-center gap-1.5">
          <Bot className="w-3 h-3" />
          分集草案 · {episodes.length} 集 / 约 {totalMin} 分钟
        </div>
        {readyToConfirm && !isAlreadyConfirmed && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-600/20 text-green-300 border border-green-600/40">
            已可确认
          </span>
        )}
        {isAlreadyConfirmed && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-600/30 text-green-200 border border-green-600/50">
            已确认
          </span>
        )}
      </div>

      {/* 剧本摘要 */}
      {scriptSummary && (scriptSummary.logline || (scriptSummary.characters || []).length > 0) && (
        <div className="space-y-1.5 pb-2 border-b border-octopus-border">
          {scriptSummary.logline && (
            <div className="text-sm text-octopus-text italic leading-snug">
              {scriptSummary.logline}
            </div>
          )}
          <div className="flex items-center gap-3 text-[10px] text-octopus-muted">
            {scriptSummary.genre && <span>类型: {scriptSummary.genre}</span>}
            {scriptSummary.tone && <span>基调: {scriptSummary.tone}</span>}
            {scriptSummary.est_total_min ? <span>原本约 {scriptSummary.est_total_min} 分钟</span> : null}
            {scriptSummary.suggested_aspect_ratio && <span>{scriptSummary.suggested_aspect_ratio}</span>}
          </div>
          {(scriptSummary.characters || []).length > 0 && (
            <div className="flex flex-wrap gap-1">
              {(scriptSummary.characters || []).slice(0, 8).map((c, i) => (
                <span
                  key={i}
                  title={[c.role, c.personality, c.arc].filter(Boolean).join(' · ')}
                  className="px-1.5 py-0.5 rounded-full bg-octopus-primary/15 text-octopus-primary text-[10px]"
                >
                  {c.name}{c.role ? ` · ${c.role}` : ''}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 集列表 */}
      <div className="space-y-2">
        {episodes.map((ep) => (
          <div
            key={ep.ep_id}
            className="rounded border border-octopus-border bg-octopus-panel/40 p-2 space-y-1"
          >
            <div className="flex items-baseline gap-2">
              <span className="text-[10px] font-mono text-octopus-muted">{ep.ep_id}</span>
              <span className="text-sm font-medium text-octopus-text flex-1 truncate">{ep.title}</span>
              {typeof ep.est_duration_min === 'number' && (
                <span className="text-[10px] text-octopus-muted whitespace-nowrap">{ep.est_duration_min} 分钟</span>
              )}
            </div>
            {ep.story_arc && (
              <div className="text-[11px] text-octopus-chat leading-snug">{ep.story_arc}</div>
            )}
            <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-octopus-muted">
              {ep.scene_range && <span>原剧本: {ep.scene_range}</span>}
              {ep.cliff_hanger && <span>钩子: {ep.cliff_hanger}</span>}
            </div>
          </div>
        ))}
      </div>

      {missingAspects && missingAspects.length > 0 && !readyToConfirm && (
        <div className="text-[10px] text-octopus-muted pt-1 border-t border-octopus-border">
          还需确认：{missingAspects.join('、')}
        </div>
      )}

      {/* 确认按钮 */}
      <div className="pt-1 flex gap-2">
        {isAlreadyConfirmed ? (
          <span className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-octopus-bg/40 text-octopus-muted text-xs border border-octopus-border cursor-not-allowed select-none">
            <CheckCircle className="w-3.5 h-3.5 text-green-500/60" />
            已确认
          </span>
        ) : readyToConfirm && onConfirm ? (
          <button
            onClick={onConfirm}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-green-600/80 hover:bg-green-500 text-white text-xs transition-colors"
          >
            <CheckCircle className="w-3.5 h-3.5" />
            确认分集，开始美术
          </button>
        ) : (
          <span className="text-[10px] text-octopus-muted">
            继续跟导演聊聊（"把 3、4 集合并 / 第 2 集太长拆成两集"），满意了再确认
          </span>
        )}
      </div>
    </div>
  )
}
