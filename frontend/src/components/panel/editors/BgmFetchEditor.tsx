/**
 * BGM 拉取节点编辑器 (0.6.1 / 0.6.2)
 *
 * 展示当前 provider 拉到的 BGM：音频播放器 + duration + mood + prompt。
 * provider 来自 result.provider（mubert / elevenlabs）；未知时按 Mubert 默认显示。
 * 失败时显示 error，用户可点节点上的"重跑"重试。
 */
import React from 'react'
import { AlertCircle, Music } from 'lucide-react'
import { Field } from './common'
import { WorkflowNode } from '@/types'

const PROVIDER_LABELS: Record<string, string> = {
  elevenlabs: 'ElevenLabs Music',
  mubert: 'Mubert',
}

export default function BgmFetchEditor({
  node,
}: {
  node: WorkflowNode
  readOnly?: boolean
}) {
  const result = (node.result || {}) as any
  const params = (node.params || {}) as any

  const provider: string = result.provider || 'mubert'  // 老数据无 provider 字段默认 mubert
  const providerLabel = PROVIDER_LABELS[provider] || provider

  const publicUrl: string = result.public_url || ''
  const duration: number = Number(result.duration || params.duration || 0)
  const mood: string = result.mood || params.mood || ''
  const playlistIndex: string = result.playlist_index || params.playlist_index_hint || ''
  const trackId: string = result.track_id || ''
  const error: string = result.error || ''
  // 0.6.1+: 实际给 music provider 的 text prompt（audio_director 输出）
  const musicPrompt: string = result.prompt || params.mubert_prompt || ''
  const musicIntensity: string = result.intensity || params.mubert_intensity || ''

  return (
    <div className="space-y-3 text-xs">
      <Field label="Provider">
        <code className="text-octopus-text">{providerLabel}</code>
      </Field>

      <Field label="状态">
        {error ? (
          <div className="flex items-start gap-1.5 text-red-300">
            <AlertCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
            <span className="break-words">{error}</span>
          </div>
        ) : publicUrl ? (
          <div className="flex items-center gap-1.5 text-green-300">
            <Music className="w-3.5 h-3.5" />
            BGM 已就绪
          </div>
        ) : (
          <div className="text-octopus-muted">等待生成...</div>
        )}
      </Field>

      <Field label="时长 / 基调">
        <div className="flex items-center gap-2">
          <span className="text-octopus-text">{duration}s</span>
          <span className="text-octopus-muted">·</span>
          <span className="text-octopus-text">{mood || '—'}</span>
        </div>
      </Field>

      {musicPrompt && (
        <Field label={`${providerLabel} prompt (text-to-music)`}>
          <div className="px-2 py-1.5 rounded bg-octopus-bg border border-octopus-border text-octopus-text leading-relaxed">
            {musicPrompt}
          </div>
          {musicIntensity && (
            <div className="text-[10px] text-octopus-muted mt-1">
              intensity: <code className="text-octopus-text">{musicIntensity}</code>
            </div>
          )}
        </Field>
      )}

      {!musicPrompt && playlistIndex && (
        <Field label="Mubert playlist_index（无 prompt 时的兜底）">
          <code className="text-octopus-text font-mono">{playlistIndex}</code>
        </Field>
      )}

      {trackId && (
        <Field label="Track ID">
          <code className="text-octopus-muted font-mono text-[10px] break-all">{trackId}</code>
        </Field>
      )}

      {publicUrl && (
        <Field label="预览">
          <audio
            src={publicUrl}
            controls
            className="w-full"
            preload="metadata"
            onClick={(e) => e.stopPropagation()}
          />
        </Field>
      )}

      <div className="text-[10px] text-octopus-muted pt-1 border-t border-octopus-border">
        {error
          ? `提示：${providerLabel} 拉取失败不会阻塞合成。merge 阶段会跳过 BGM 直接出片。点节点上"重跑"可重新尝试。`
          : '提示：BGM 会在 merge 阶段混入最终视频（可在合并节点切换"独占 / 不混入"）。'}
      </div>
    </div>
  )
}
