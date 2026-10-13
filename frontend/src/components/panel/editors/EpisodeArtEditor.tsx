/**
 * 0.7.0 轮 1：episode_art_design 节点 — 展示本集美术细化的"骨架"（角色+装束 / 场景变化 / 道具状态）
 * 子节点（ep_character / ep_scene / ep_prop）在 canvas 上单独展开，这个 editor 只看汇总。
 */
import React from 'react'
import { Shirt } from 'lucide-react'
import { useStore } from '@/store/useStore'
import type { WorkflowNode } from '@/types'

interface Props {
  node: WorkflowNode
}

interface EpisodeArtData {
  episode_summary?: string
  characters_in_episode?: Array<{
    master_character_id?: string
    name?: string
    appearance_notes?: string
    outfits?: Array<{
      outfit_id?: string
      name?: string
      when?: string
      outfit_prompt?: string
    }>
  }>
  scenes_in_episode?: Array<{
    master_scene_id?: string
    location?: string
    variation_id?: string
    variation_name?: string
    time_of_day?: string
    lighting?: string
    camera_angle?: string
    image_prompt?: string
  }>
  props_in_episode?: Array<{
    master_prop_id?: string
    name?: string
    state_id?: string
    state_name?: string
    state_description?: string
  }>
  notes?: string
}

export default function EpisodeArtEditor({ node }: Props) {
  const setAssetManagerSeriesId = useStore((s) => s.setAssetManagerSeriesId)
  const seriesId = useStore((s) => (s.currentProject as any)?.series_id as string | undefined)
  const result = (node.result || {}) as EpisodeArtData
  const params = (node.params || {}) as Record<string, any>
  const master = params.master_shared_assets || {}

  const chars = result.characters_in_episode || []
  const scenes = result.scenes_in_episode || []
  const props = result.props_in_episode || []
  const outfitTotal = chars.reduce((s, c) => s + ((c.outfits || []).length), 0)

  return (
    <div className="space-y-3">
      <div className="text-[10px] text-octopus-muted leading-snug pb-2 border-b border-octopus-border">
        本集美术 — 在总美术基础上细化。下面的每一项已自动 spawn 为子节点出图，在画布上单独 confirm。
      </div>

      {result.episode_summary && (
        <div className="p-2 rounded bg-octopus-bg/40 border border-octopus-border">
          <div className="text-[10px] text-octopus-muted mb-0.5">本集视觉重点</div>
          <div className="text-xs text-octopus-text leading-snug">{result.episode_summary}</div>
        </div>
      )}

      {/* 概览 chip + 资产管理捷径 */}
      <div className="flex flex-wrap items-center gap-2 text-[10px]">
        <span className="px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-300">
          🎭 {chars.length} 角色 · {outfitTotal} 套装束
        </span>
        <span className="px-2 py-0.5 rounded-full bg-blue-500/20 text-blue-300">
          🏞️ {scenes.length} 场景变化
        </span>
        <span className="px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300">
          🎁 {props.length} 道具状态
        </span>
        {seriesId && (
          <button
            onClick={() => setAssetManagerSeriesId(seriesId)}
            className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-octopus-primary/15 text-octopus-primary hover:bg-octopus-primary/25 transition-colors"
            title="打开本剧资产管理（增删服装/道具、单套重生）"
          >
            <Shirt className="w-3 h-3" /> 管理
          </button>
        )}
      </div>

      {/* 角色 + outfits */}
      {chars.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs font-medium text-octopus-text">🎭 本集角色 + 装束</div>
          {chars.map((c, i) => (
            <div key={i} className="rounded border border-octopus-border bg-octopus-panel/40 p-2 space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="text-sm text-octopus-text">{c.name}</span>
                {c.master_character_id && (
                  <span className="text-[10px] text-octopus-muted">← {c.master_character_id}</span>
                )}
              </div>
              {c.appearance_notes && (
                <div className="text-[11px] text-octopus-chat italic">本集状态：{c.appearance_notes}</div>
              )}
              <div className="flex flex-wrap gap-1">
                {(c.outfits || []).map((o, j) => (
                  <span
                    key={j}
                    title={o.outfit_prompt}
                    className="px-1.5 py-0.5 rounded bg-purple-500/15 text-purple-200 text-[10px]"
                  >
                    👗 {o.name}
                    {o.when && <span className="text-purple-300/60 ml-1">· {o.when}</span>}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 场景变化 */}
      {scenes.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs font-medium text-octopus-text">🏞️ 本集场景变化</div>
          {scenes.map((s, i) => (
            <div key={i} className="rounded border border-octopus-border bg-octopus-panel/40 p-2 space-y-1">
              <div className="flex items-center gap-2 text-sm">
                <span className="text-octopus-text">{s.location}</span>
                <span className="text-octopus-muted text-[10px]">· {s.variation_name || s.variation_id}</span>
                {s.master_scene_id ? (
                  <span className="text-[10px] text-octopus-muted">← {s.master_scene_id}</span>
                ) : (
                  <span className="text-[10px] px-1 rounded bg-green-500/20 text-green-300">新增</span>
                )}
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-octopus-muted">
                {s.time_of_day && <span>时间：{s.time_of_day}</span>}
                {s.lighting && <span>光线：{s.lighting}</span>}
                {s.camera_angle && <span>镜头：{s.camera_angle}</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 道具状态 */}
      {props.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs font-medium text-octopus-text">🎁 本集道具状态变化</div>
          {props.map((p, i) => (
            <div key={i} className="rounded border border-octopus-border bg-octopus-panel/40 p-2 space-y-1">
              <div className="flex items-center gap-2 text-sm">
                <span className="text-octopus-text">{p.name}</span>
                <span className="text-octopus-muted text-[10px]">· {p.state_name}</span>
                {p.master_prop_id ? (
                  <span className="text-[10px] text-octopus-muted">← {p.master_prop_id}</span>
                ) : (
                  <span className="text-[10px] px-1 rounded bg-green-500/20 text-green-300">新增</span>
                )}
              </div>
              {p.state_description && (
                <div className="text-[10px] text-octopus-chat italic">{p.state_description}</div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* master 总览参考 */}
      <details className="pt-2 border-t border-octopus-border">
        <summary className="cursor-pointer text-[10px] text-octopus-muted hover:text-octopus-text">
          📚 总美术参考库（{(master.characters || []).length} 角色 / {(master.scenes || []).length} 场景 / {(master.props || []).length} 道具）
        </summary>
        <div className="mt-2 flex flex-wrap gap-1">
          {(master.characters || []).map((c: any, i: number) => (
            <span key={`c${i}`} className="px-1.5 py-0.5 rounded bg-octopus-bg/60 text-[10px] text-octopus-muted">
              🎭 {c.name}
            </span>
          ))}
          {(master.scenes || []).map((s: any, i: number) => (
            <span key={`s${i}`} className="px-1.5 py-0.5 rounded bg-octopus-bg/60 text-[10px] text-octopus-muted">
              🏞️ {s.location}
            </span>
          ))}
          {(master.props || []).map((p: any, i: number) => (
            <span key={`p${i}`} className="px-1.5 py-0.5 rounded bg-octopus-bg/60 text-[10px] text-octopus-muted">
              🎁 {p.name}
            </span>
          ))}
        </div>
      </details>

      {result.notes && (
        <div className="pt-2 border-t border-octopus-border text-[10px] text-octopus-muted">
          <b>备注：</b>{result.notes}
        </div>
      )}
    </div>
  )
}
