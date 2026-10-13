/**
 * 成本总结编辑器（0.6.5）— 展示项目累计花费 + 按模型 + 按阶段 + 调用明细
 */
import React, { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { WorkflowNode } from '@/types'

interface ByModel {
  provider: string
  model: string
  kind: string
  calls: number
  tokens: number
  images: number
  seconds: number
  cost_cny: number
  cost_usd: number
  price_known: boolean
}

interface ByPhase {
  phase: string
  calls: number
  cost_cny: number
  cost_usd: number
}

interface CostEntry {
  timestamp: string
  node_id: string
  node_type: string
  phase: string
  usage: any
  cost_cny: number
  cost_usd: number
  price_known: boolean
}

interface CostSummary {
  total_cny: number
  total_usd: number
  exchange_rate: number
  by_model: ByModel[]
  by_phase: ByPhase[]
  entries: CostEntry[]
}

const PHASE_LABEL: Record<string, string> = {
  script: '剧本',
  art: '美术',
  storyboard: '分镜',
  keyframe: '关键帧',
  video: '视频',
  audio: '音频',
  bgm: 'BGM',
  merge: '合成',
  publish: '发布',
  cost: '成本',
  unknown: '其他',
}

const KIND_LABEL: Record<string, string> = {
  llm: 'LLM',
  image: '图像',
  video: '视频',
  audio: '音频',
}

function fmtCny(v: number): string {
  return `¥${(v || 0).toFixed(v < 0.01 ? 4 : 2)}`
}
function fmtUsd(v: number): string {
  return `$${(v || 0).toFixed(v < 0.01 ? 4 : 2)}`
}
function fmtUsage(m: ByModel): string {
  const parts: string[] = []
  if (m.tokens) parts.push(`${m.tokens.toLocaleString()} tokens`)
  if (m.images) parts.push(`${m.images} 张`)
  if (m.seconds) parts.push(`${m.seconds}s`)
  return parts.join(' · ') || '-'
}
function fmtEntryUsage(u: any): string {
  if (!u) return '-'
  if (u.kind === 'llm') return `${u.prompt_tokens || 0}+${u.completion_tokens || 0} tokens`
  if (u.kind === 'image') return `${u.images || 1} 张 ${u.size || ''}`
  if (u.kind === 'video') return `${u.seconds || 0}s ${u.resolution || ''}`
  if (u.kind === 'audio') return `${u.seconds || 0}s`
  return JSON.stringify(u).slice(0, 60)
}

export default function CostSummaryEditor({
  node,
}: {
  node: WorkflowNode
  readOnly?: boolean
}) {
  const r: CostSummary | null = (node.result as any) ?? null
  const [showEntries, setShowEntries] = useState(false)

  if (!r || !r.entries || r.entries.length === 0) {
    return (
      <div className="text-sm text-octopus-muted p-4 text-center">
        暂无成本数据（项目运行时未启用成本统计，或本次未产生付费调用）。
      </div>
    )
  }

  return (
    <div className="space-y-4 text-sm">
      {/* 顶部 hero */}
      <div className="rounded-lg border border-octopus-border bg-octopus-bg/40 p-4">
        <div className="flex items-baseline gap-3">
          <span className="text-2xl">💰</span>
          <div className="flex-1 min-w-0">
            <div className="text-2xl font-semibold text-octopus-accent">{fmtCny(r.total_cny)}</div>
            <div className="text-[11px] text-octopus-muted mt-0.5">
              ≈ {fmtUsd(r.total_usd)} · {r.entries.length} 次调用 · 汇率 1 USD = ¥{r.exchange_rate}
            </div>
          </div>
        </div>
        <div className="text-[10px] text-octopus-muted mt-2 leading-snug">
          ⚠ 价格按公开报价估算，仅供参考；以各平台真实账单为准。
        </div>
      </div>

      {/* 按模型 */}
      <div>
        <div className="text-xs text-octopus-text font-medium mb-2">按模型</div>
        <div className="space-y-1 text-[12px]">
          <div className="grid grid-cols-[1fr_auto_auto] gap-2 px-2 py-1 text-[10px] text-octopus-muted border-b border-octopus-border/40">
            <span>模型</span><span>用量</span><span className="text-right">成本</span>
          </div>
          {r.by_model.map((m, i) => (
            <div
              key={`${m.provider}-${m.model}-${i}`}
              className="grid grid-cols-[1fr_auto_auto] gap-2 px-2 py-1.5 rounded hover:bg-octopus-border/30"
            >
              <div className="min-w-0">
                <div className="truncate text-octopus-text">
                  {m.provider} / {m.model}
                </div>
                <div className="text-[10px] text-octopus-muted">
                  {KIND_LABEL[m.kind] || m.kind} · {m.calls} 次
                  {!m.price_known && <span className="ml-1 text-yellow-500">· 价格未知</span>}
                </div>
              </div>
              <div className="text-[11px] text-octopus-muted self-center whitespace-nowrap">
                {fmtUsage(m)}
              </div>
              <div className="text-right whitespace-nowrap self-center">
                <div className="text-octopus-accent">{fmtCny(m.cost_cny)}</div>
                <div className="text-[9px] text-octopus-muted">{fmtUsd(m.cost_usd)}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* 按阶段 */}
      <div>
        <div className="text-xs text-octopus-text font-medium mb-2">按阶段</div>
        <div className="space-y-1 text-[12px]">
          {r.by_phase.map((p, i) => (
            <div
              key={`${p.phase}-${i}`}
              className="grid grid-cols-[1fr_auto_auto] gap-2 px-2 py-1 rounded hover:bg-octopus-border/30"
            >
              <span className="text-octopus-text">{PHASE_LABEL[p.phase] || p.phase}</span>
              <span className="text-[11px] text-octopus-muted self-center">{p.calls} 次</span>
              <span className="text-right text-octopus-accent">{fmtCny(p.cost_cny)}</span>
            </div>
          ))}
        </div>
      </div>

      {/* 明细折叠 */}
      <div>
        <button
          onClick={() => setShowEntries((v) => !v)}
          className="flex items-center gap-1 text-xs text-octopus-muted hover:text-octopus-text"
        >
          {showEntries ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
          调用明细 ({r.entries.length})
        </button>
        {showEntries && (
          <div className="mt-2 space-y-1 max-h-[400px] overflow-y-auto text-[11px]">
            {[...r.entries].reverse().map((e, i) => (
              <div
                key={i}
                className="grid grid-cols-[auto_1fr_auto] gap-2 px-2 py-1 border-b border-octopus-border/30"
              >
                <span className="text-[10px] text-octopus-muted whitespace-nowrap">
                  {e.timestamp.slice(11, 19)}
                </span>
                <div className="min-w-0">
                  <div className="truncate">
                    {PHASE_LABEL[e.phase] || e.phase} · {e.usage?.provider}/{e.usage?.model}
                  </div>
                  <div className="text-[10px] text-octopus-muted truncate">{fmtEntryUsage(e.usage)}</div>
                </div>
                <span className="text-right text-octopus-accent whitespace-nowrap self-center">
                  {fmtCny(e.cost_cny)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
