/**
 * 通用编辑器 — 用于 video_generation / video_merge / 未知类型
 * 把 params 顶层字段做友好渲染，复杂对象用 JSON 兜底
 */
import React, { useState, useEffect } from 'react'
import { Save } from 'lucide-react'
import { Field, TextInput, TextArea, NumberInput, humanizeKey } from './common'
import { WorkflowNode } from '@/types'

export default function GenericEditor({
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

  const renderField = (k: string, v: any) => {
    const label = humanizeKey(k)
    if (typeof v === 'boolean') {
      return (
        <Field key={k} label={label}>
          <label className="flex items-center gap-2 text-sm text-octopus-text">
            <input
              type="checkbox"
              checked={!!params[k]}
              onChange={(e) => update(k, e.target.checked)}
            />
            {params[k] ? '开启' : '关闭'}
          </label>
        </Field>
      )
    }
    if (typeof v === 'number') {
      return (
        <Field key={k} label={label}>
          <NumberInput value={params[k]} onChange={(n) => update(k, n)} />
        </Field>
      )
    }
    if (typeof v === 'string') {
      const long = v.length > 60
      return (
        <Field key={k} label={label}>
          {long ? (
            <TextArea value={params[k] ?? ''} rows={3} onChange={(s) => update(k, s)} />
          ) : (
            <TextInput value={params[k] ?? ''} onChange={(s) => update(k, s)} />
          )}
        </Field>
      )
    }
    // 对象/数组：JSON 兜底
    return (
      <Field key={k} label={label} hint="复杂结构暂以 JSON 编辑">
        <TextArea
          value={JSON.stringify(params[k] ?? v, null, 2)}
          rows={5}
          onChange={(s) => {
            try {
              update(k, JSON.parse(s))
            } catch {
              // 暂时保留字符串，待 JSON 合法时再 parse
            }
          }}
        />
      </Field>
    )
  }

  const entries = Object.entries(params)
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

      {entries.length === 0 ? (
        <div className="text-xs text-octopus-muted text-center py-4">无参数</div>
      ) : (
        entries.map(([k, v]) => renderField(k, v))
      )}

      {/* 视频结果直接预览 */}
      {node.result?.video_url && (
        <div className="space-y-1">
          <div className="text-xs text-octopus-muted">视频结果</div>
          <video
            src={node.result.video_url}
            controls
            className="w-full rounded-lg border border-octopus-border"
          />
        </div>
      )}
    </div>
  )
}
