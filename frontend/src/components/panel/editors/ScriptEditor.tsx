/**
 * 剧本编辑器 — 标题/logline + 场景列表 + 角色列表
 */
import React, { useState, useEffect } from 'react'
import { Plus, Save } from 'lucide-react'
import { Card, Field, TextInput, TextArea, NumberInput, safeSlice } from './common'
import { WorkflowNode } from '@/types'

interface Scene {
  scene_id?: string
  scene_summary?: string
  location?: string
  characters?: string[]
  action?: string
  emotion?: string
  dialogue?: string
  voiceover?: string
  visual_notes?: string
  duration_seconds?: number
}

interface Character {
  character_id?: string
  name?: string
  description?: string
  personality?: string
}

export default function ScriptEditor({
  node,
  onSave,
  readOnly,
}: {
  node: WorkflowNode
  onSave: (newResult: any) => Promise<void> | void
  readOnly?: boolean
}) {
  const r = node.result ?? {}
  const [title, setTitle] = useState<string>(r.script_title ?? '')
  const [logline, setLogline] = useState<string>(r.logline ?? '')
  const [scenes, setScenes] = useState<Scene[]>(r.scenes ?? [])
  const [characters, setCharacters] = useState<Character[]>(r.characters ?? [])
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    const r = node.result ?? {}
    setTitle(r.script_title ?? '')
    setLogline(r.logline ?? '')
    setScenes(r.scenes ?? [])
    setCharacters(r.characters ?? [])
    setDirty(false)
  }, [node.id, node.result])

  const updateScene = (i: number, patch: Partial<Scene>) => {
    setScenes((prev) => prev.map((s, idx) => (idx === i ? { ...s, ...patch } : s)))
    setDirty(true)
  }
  const updateChar = (i: number, patch: Partial<Character>) => {
    setCharacters((prev) => prev.map((c, idx) => (idx === i ? { ...c, ...patch } : c)))
    setDirty(true)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const newResult = {
        ...(node.result ?? {}),
        script_title: title,
        logline,
        scenes,
        characters,
      }
      await onSave(newResult)
      setDirty(false)
    } finally {
      setSaving(false)
    }
  }

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

      <Field label="剧本标题">
        <TextInput value={title} onChange={(v) => { setTitle(v); setDirty(true) }} />
      </Field>
      <Field label="一句话梗概（logline）">
        <TextArea value={logline} rows={2} onChange={(v) => { setLogline(v); setDirty(true) }} />
      </Field>

      {/* 角色 */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-octopus-text">角色 ({characters.length})</span>
          {!readOnly && (
            <button
              onClick={() => {
                setCharacters((p) => [...p, { character_id: `C${String(p.length + 1).padStart(2, '0')}`, name: '' }])
                setDirty(true)
              }}
              className="flex items-center gap-1 text-xs text-octopus-primary hover:text-octopus-accent"
            >
              <Plus className="w-3 h-3" />
              添加角色
            </button>
          )}
        </div>
        <div className="space-y-2">
          {characters.map((c, i) => (
            <Card
              key={`char-${i}`}
              title={c.name || c.character_id || `角色${i + 1}`}
              subtitle={safeSlice(c.description, 60)}
              onRemove={readOnly ? undefined : () => { setCharacters((p) => p.filter((_, k) => k !== i)); setDirty(true) }}
            >
              <Field label="姓名">
                <TextInput value={c.name ?? ''} onChange={(v) => updateChar(i, { name: v })} />
              </Field>
              <Field label="描述">
                <TextArea value={c.description ?? ''} rows={2} onChange={(v) => updateChar(i, { description: v })} />
              </Field>
              <Field label="性格">
                <TextInput value={c.personality ?? ''} onChange={(v) => updateChar(i, { personality: v })} />
              </Field>
            </Card>
          ))}
        </div>
      </div>

      {/* 场景 */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-octopus-text">场景 ({scenes.length})</span>
          {!readOnly && (
            <button
              onClick={() => {
                setScenes((p) => [...p, { scene_id: `S${String(p.length + 1).padStart(2, '0')}`, duration_seconds: 5 }])
                setDirty(true)
              }}
              className="flex items-center gap-1 text-xs text-octopus-primary hover:text-octopus-accent"
            >
              <Plus className="w-3 h-3" />
              添加场景
            </button>
          )}
        </div>
        <div className="space-y-2">
          {scenes.map((s, i) => (
            <Card
              key={`scene-${i}`}
              title={`${s.scene_id || `S${i + 1}`} · ${s.location || ''}`}
              subtitle={s.scene_summary}
              onRemove={readOnly ? undefined : () => { setScenes((p) => p.filter((_, k) => k !== i)); setDirty(true) }}
            >
              <Field label="场景摘要">
                <TextArea value={s.scene_summary ?? ''} rows={2} onChange={(v) => updateScene(i, { scene_summary: v })} />
              </Field>
              <Field label="地点">
                <TextInput value={s.location ?? ''} onChange={(v) => updateScene(i, { location: v })} />
              </Field>
              <Field label="动作">
                <TextArea value={s.action ?? ''} rows={2} onChange={(v) => updateScene(i, { action: v })} />
              </Field>
              <Field label="对白">
                <TextArea value={s.dialogue ?? ''} rows={2} onChange={(v) => updateScene(i, { dialogue: v })} />
              </Field>
              <Field label="旁白">
                <TextArea value={s.voiceover ?? ''} rows={2} onChange={(v) => updateScene(i, { voiceover: v })} />
              </Field>
              <Field label="情绪">
                <TextInput value={s.emotion ?? ''} onChange={(v) => updateScene(i, { emotion: v })} />
              </Field>
              <Field label="时长（秒）">
                <NumberInput value={s.duration_seconds ?? 5} onChange={(v) => updateScene(i, { duration_seconds: v })} />
              </Field>
            </Card>
          ))}
        </div>
      </div>
    </div>
  )
}
