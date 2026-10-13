/**
 * 关键帧节点编辑器 — 展示参考图（角色+场景）、prompt 和融合结果
 *
 * 用户在这里：
 *   - 看到本镜头将融合的参考图列表（来自 @ 引用的角色和场景）
 *   - 编辑 prompt（即融合指令）
 *   - 调整输出宽高
 *   - 看到关键帧成果，不满意可以"重跑节点"
 */
import React, { useEffect, useState } from 'react'
import { Save, ImageIcon } from 'lucide-react'
import { Field, NumberInput } from './common'
import { WorkflowNode } from '@/types'
import UploadImageOverrideButton from './UploadImageOverrideButton'

export default function KeyframeEditor({
  node,
  onSaveParams,
  readOnly,
}: {
  node: WorkflowNode
  onSaveParams: (newParams: any) => Promise<void> | void
  readOnly?: boolean
}) {
  const [params, setParams] = useState<Record<string, any>>(node.params ?? {})
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setParams(node.params ?? {})
    setDirty(false)
  }, [node.id, node.params])

  const imageUrls: string[] = Array.isArray(params.image_urls) ? params.image_urls : []
  const referencedCharacters: any[] = Array.isArray(params.referenced_characters) ? params.referenced_characters : []
  const referencedScenes: any[] = Array.isArray(params.referenced_scenes) ? params.referenced_scenes : []
  const referencedProps: any[] = Array.isArray(params.referenced_props) ? params.referenced_props : []

  // 去重：后端 referenced_* 常把同一场景/角色重复登记多次（@ 引用被多处匹配 + 自动补登），
  // 按名字去重，避免列表里冒出一堆重复的"出租屋"，且让计数与实际融合图一致
  const dedupeBy = <T,>(arr: T[], keyFn: (x: T) => string | undefined): T[] => {
    const seen = new Set<string>()
    return arr.filter((x) => {
      const k = keyFn(x)
      if (!k || seen.has(k)) return false
      seen.add(k)
      return true
    })
  }
  const scenes = dedupeBy(referencedScenes, (s: any) => s.location || s.image_url)
  const props = dedupeBy(referencedProps, (p: any) => p.name || p.image_url)
  const chars = dedupeBy(referencedCharacters, (c: any) => c.name || c.image_url)
  const refCount = scenes.length + props.length + chars.length

  const update = (k: string, v: any) => {
    setParams((p) => ({ ...p, [k]: v }))
    setDirty(true)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      await onSaveParams(params)
      setDirty(false)
    } finally {
      setSaving(false)
    }
  }

  const result = (node.result || {}) as any

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        {!readOnly && (
          <button
            onClick={handleSave}
            disabled={!dirty || saving}
            className="flex items-center gap-1 px-2 py-1 rounded bg-green-600/80 hover:bg-green-500 disabled:opacity-40 text-white text-xs"
          >
            <Save className="w-3 h-3" />
            {saving ? '保存中...' : dirty ? '保存修改' : '已保存'}
          </button>
        )}
      </div>

      {/* 参考图列表（顺序与后端 image_urls 一致：场景→道具→角色，0.6.4+） */}
      <Field label={`融合参考图（${refCount || imageUrls.length}）`} hint="Seedream 按顺序融合：场景作背景，道具作配角，角色作主体">
        <div className="space-y-3">
          {/* 场景：横图，2 列宽展示 */}
          {scenes.length > 0 && (
            <div>
              <div className="text-[10px] text-octopus-muted mb-1">🏞️ 场景 · {scenes.length}</div>
              <div className="grid grid-cols-2 gap-2">
                {scenes.map((s, i) => (
                  <RefThumb key={`scene-${i}`} imageUrl={s.image_url} label={s.location || `场景 ${i + 1}`} aspect="landscape" />
                ))}
              </div>
            </div>
          )}
          {/* 人物：竖图，单独一块、用竖向比例不被压小 */}
          {chars.length > 0 && (
            <div>
              <div className="text-[10px] text-octopus-muted mb-1">🎭 人物 · {chars.length}</div>
              <div className="grid grid-cols-2 gap-2">
                {chars.map((c, i) => (
                  <RefThumb key={`char-${i}`} imageUrl={c.image_url} label={c.name || `角色 ${i + 1}`} aspect="portrait" />
                ))}
              </div>
            </div>
          )}
          {/* 道具：单独一块 */}
          {props.length > 0 && (
            <div>
              <div className="text-[10px] text-octopus-muted mb-1">🎁 道具 · {props.length}</div>
              <div className="grid grid-cols-2 gap-2">
                {props.map((p, i) => (
                  <RefThumb key={`prop-${i}`} imageUrl={p.image_url} label={p.name || `道具 ${i + 1}`} aspect="square" />
                ))}
              </div>
            </div>
          )}
          {refCount === 0 && (
            <div className="text-xs text-octopus-muted py-3 text-center border border-dashed border-octopus-border rounded">
              无参考图（分镜未引用任何 @ 角色 / 场景 / 道具）
            </div>
          )}
        </div>
      </Field>

      {/* Prompt */}
      <Field label="融合 Prompt（英文）" hint="描述本镜头要呈现的画面，会和参考图一起送给 Seedream 4.0">
        <textarea
          value={params.prompt ?? ''}
          onChange={(e) => update('prompt', e.target.value)}
          rows={5}
          disabled={readOnly}
          className="w-full px-2 py-1.5 rounded border border-octopus-border bg-octopus-bg text-octopus-text text-xs font-mono resize-y"
        />
      </Field>

      {/* 尺寸 */}
      <div className="grid grid-cols-2 gap-2">
        <Field label="输出宽" hint="像素，16 的倍数">
          <NumberInput
            value={params.width ?? 720}
            min={512}
            max={2048}
            step={16}
            onChange={(v) => update('width', v)}
          />
        </Field>
        <Field label="输出高" hint="像素，16 的倍数">
          <NumberInput
            value={params.height ?? 1280}
            min={512}
            max={2048}
            step={16}
            onChange={(v) => update('height', v)}
          />
        </Field>
      </div>

      {/* 结果展示 */}
      <div className="space-y-2 pt-2 border-t border-octopus-border">
        <div className="flex items-center justify-between">
          <div className="text-xs text-octopus-muted">关键帧结果</div>
          {!readOnly && (
            <UploadImageOverrideButton nodeId={node.id} field="image_url" label={result.image_url ? '上传替换' : '上传关键帧'} />
          )}
        </div>
        {result.image_url ? (
          <>
            <img
              src={result.image_url}
              alt="keyframe"
              className="w-full h-auto rounded border border-octopus-border bg-black"
            />
            {result.mock && (
              <div className="text-[10px] text-yellow-400">[Mock 图片 — 未配置真实 API key]</div>
            )}
            {result.user_uploaded && (
              <div className="text-[10px] text-cyan-300">📤 用户上传</div>
            )}
            {typeof result.reference_count === 'number' && !result.user_uploaded && (
              <div className="text-[10px] text-octopus-muted">
                融合了 {result.reference_count} 张参考图 · 模型 {result.model || 'mock'}
              </div>
            )}
          </>
        ) : (
          <div className="px-2 py-3 text-[10px] text-octopus-muted bg-octopus-bg/40 border border-dashed border-octopus-border rounded text-center">
            还没有关键帧，可直接上传一张
          </div>
        )}
      </div>

      {result.skipped && (
        <div className="p-2 rounded bg-yellow-900/20 border border-yellow-500/30 text-yellow-300 text-xs">
          ⚠ 已跳过：{result.reason || '没有可用的参考图'}
        </div>
      )}
    </div>
  )
}

function RefThumb({ imageUrl, label, aspect = 'square' }: {
  imageUrl?: string; label: string; aspect?: 'landscape' | 'portrait' | 'square'
}) {
  const box = aspect === 'portrait' ? 'aspect-[3/4]' : aspect === 'landscape' ? 'aspect-video' : 'aspect-square'
  return (
    <div className="space-y-1">
      {imageUrl ? (
        <img
          src={imageUrl}
          alt={label}
          className="w-full h-auto rounded border border-octopus-border bg-black/40"
          onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
        />
      ) : (
        <div className={`w-full ${box} flex items-center justify-center rounded border border-octopus-border bg-black/40 text-octopus-muted`}>
          <ImageIcon className="w-5 h-5" />
        </div>
      )}
      <div className="text-[10px] text-octopus-text truncate">{label}</div>
    </div>
  )
}
