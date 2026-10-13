/**
 * 0.7.0 专家模式：episode_breakdown 节点的 NodeEditorPanel 编辑器
 *
 * 复用 chat 里的 EpisodeBreakdownCard 渲染，但这里是只读展示
 *（节点已 CONFIRMED，不允许在 canvas 上再改；改要回 chat 跟导演聊）。
 */
import React from 'react'
import EpisodeBreakdownCard from '@/components/chat/EpisodeBreakdownCard'
import type { WorkflowNode, ScriptSummary, EpisodeBreakdownItem } from '@/types'

interface Props {
  node: WorkflowNode
}

export default function EpisodeBreakdownEditor({ node }: Props) {
  const params = (node.params || {}) as Record<string, any>
  const result = (node.result || {}) as Record<string, any>

  const scriptSummary = (params.script_summary || {}) as ScriptSummary
  const episodes = (result.episodes || []) as EpisodeBreakdownItem[]

  return (
    <div className="space-y-2">
      <div className="text-[10px] text-octopus-muted leading-snug pb-2 border-b border-octopus-border">
        分集草案已敲定（CONFIRMED）。在总项目 chat 里跟导演说"加一集 / 合并 X、Y 集"
        可重新协商，但需要先把美术阶段未冻结的资产先撤回。
      </div>
      <EpisodeBreakdownCard
        scriptSummary={scriptSummary}
        episodes={episodes}
        readyToConfirm={false}
        missingAspects={[]}
        isAlreadyConfirmed={true}
      />
    </div>
  )
}
