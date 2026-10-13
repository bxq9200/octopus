/**
 * 美术编辑器 — visual_style + 角色 + 场景 + 关键道具（0.6.4）
 */
import React, { useState, useEffect } from 'react'
import { Plus, Save } from 'lucide-react'
import { Card, Field, TextInput, TextArea, Select, safeSlice } from './common'
import { WorkflowNode } from '@/types'

interface VisualStyle {
  keywords?: string[]
  color_palette?: string[]
  lighting?: string
  texture?: string
  camera_feel?: string
}

interface ArtCharacter {
  character_id?: string
  name?: string
  identity?: string
  appearance?: string
  clothing?: string
  recognizable_features?: string
  subject_type?: string
  image_prompt?: string
  negative_prompt?: string
}

interface ArtScene {
  scene_id?: string
  location?: string
  environment?: string
  props?: string         // 场景内描述性道具（旧字段，字符串）
  lighting?: string
  mood?: string
  image_prompt?: string
  negative_prompt?: string
}

// 0.6.4: 关键道具（独立子节点，每个单独生图，可在分镜 @ 引用）
interface ArtProp {
  prop_id?: string
  name?: string
  description?: string
  use_case?: string
  image_prompt?: string
  negative_prompt?: string
}

// 把任意值规范化为字符串数组（兼容数组 / 字符串 / 对象 / null）
const arr = (v: any): string[] => {
  if (Array.isArray(v)) return v.map((x) => (typeof x === 'string' ? x : JSON.stringify(x)))
  if (typeof v === 'string') return v.split(/[,，、]/).map((s) => s.trim()).filter(Boolean)
  if (v && typeof v === 'object') {
    // 例如 {primary: "...", secondary: "..."} 这种对象，把值取出来
    return Object.values(v).map((x) => (typeof x === 'string' ? x : JSON.stringify(x)))
  }
  return []
}

// 安全把任意值转成字符串（用于单值字段）
const str = (v: any): string => {
  if (v == null) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

export default function ArtEditor({
  node,
  onSave,
  readOnly,
}: {
  node: WorkflowNode
  onSave: (newResult: any) => Promise<void> | void
  readOnly?: boolean
}) {
  const r = node.result ?? {}
  const [vs, setVs] = useState<VisualStyle>(r.visual_style ?? {})
  const [characters, setCharacters] = useState<ArtCharacter[]>(r.characters ?? [])
  const [scenes, setScenes] = useState<ArtScene[]>(r.scenes ?? [])
  const [props, setProps] = useState<ArtProp[]>(r.props ?? [])  // 0.6.4
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    const r = node.result ?? {}
    setVs(r.visual_style ?? {})
    setCharacters(r.characters ?? [])
    setScenes(r.scenes ?? [])
    setProps(r.props ?? [])  // 0.6.4
    setDirty(false)
  }, [node.id, node.result])

  const updateChar = (i: number, patch: Partial<ArtCharacter>) => {
    setCharacters((p) => p.map((c, idx) => (idx === i ? { ...c, ...patch } : c)))
    setDirty(true)
  }
  const updateScene = (i: number, patch: Partial<ArtScene>) => {
    setScenes((p) => p.map((s, idx) => (idx === i ? { ...s, ...patch } : s)))
    setDirty(true)
  }
  const updateProp = (i: number, patch: Partial<ArtProp>) => {
    setProps((p) => p.map((x, idx) => (idx === i ? { ...x, ...patch } : x)))
    setDirty(true)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      await onSave({
        ...(node.result ?? {}),
        visual_style: vs,
        characters,
        scenes,
        props,  // 0.6.4
      })
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

      {/* 视觉风格 */}
      <Card title="视觉风格" defaultOpen={true}>
        <Field label="风格关键词" hint="用逗号分隔">
          <TextInput
            value={arr(vs.keywords).join(', ')}
            onChange={(v) => { setVs({ ...vs, keywords: arr(v) }); setDirty(true) }}
          />
        </Field>
        <Field label="色彩方案" hint="用逗号分隔">
          <TextInput
            value={arr(vs.color_palette).join(', ')}
            onChange={(v) => { setVs({ ...vs, color_palette: arr(v) }); setDirty(true) }}
          />
        </Field>
        <Field label="光影">
          <TextInput value={str(vs.lighting)} onChange={(v) => { setVs({ ...vs, lighting: v }); setDirty(true) }} />
        </Field>
        <Field label="质感">
          <TextInput value={str(vs.texture)} onChange={(v) => { setVs({ ...vs, texture: v }); setDirty(true) }} />
        </Field>
        <Field label="镜头感">
          <TextInput value={str(vs.camera_feel)} onChange={(v) => { setVs({ ...vs, camera_feel: v }); setDirty(true) }} />
        </Field>
      </Card>

      {/* 角色 */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-octopus-text">🎭 角色 ({characters.length})</span>
          {!readOnly && (
            <button
              onClick={() => {
                setCharacters((p) => [...p, { character_id: `C${String(p.length + 1).padStart(2, '0')}`, subject_type: 'human' }])
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
              subtitle={safeSlice(c.identity || c.appearance, 60)}
              onRemove={readOnly ? undefined : () => { setCharacters((p) => p.filter((_, k) => k !== i)); setDirty(true) }}
            >
              <Field label="姓名">
                <TextInput value={c.name ?? ''} onChange={(v) => updateChar(i, { name: v })} />
              </Field>
              <Field label="角色类型" hint="决定视频生成时是否触发去写实化（仅 human 触发，避免 PrivacyInformation 审核）">
                <Select
                  value={c.subject_type ?? 'human'}
                  options={[
                    { value: 'human', label: 'human 真人/类人' },
                    { value: 'animal', label: 'animal 动物' },
                    { value: 'creature', label: 'creature 奇幻生物' },
                    { value: 'mech', label: 'mech 机械/机器人' },
                    { value: 'object', label: 'object 拟人化物体' },
                  ]}
                  onChange={(v) => updateChar(i, { subject_type: v })}
                />
              </Field>
              <Field label="身份">
                <TextInput value={c.identity ?? ''} onChange={(v) => updateChar(i, { identity: v })} />
              </Field>
              <Field label="外观">
                <TextArea value={c.appearance ?? ''} rows={2} onChange={(v) => updateChar(i, { appearance: v })} />
              </Field>
              <Field label="服装">
                <TextArea value={c.clothing ?? ''} rows={2} onChange={(v) => updateChar(i, { clothing: v })} />
              </Field>
              <Field label="辨识特征">
                <TextArea value={c.recognizable_features ?? ''} rows={2} onChange={(v) => updateChar(i, { recognizable_features: v })} />
              </Field>
              <Field label="图像生成提示词（英文）">
                <TextArea value={c.image_prompt ?? ''} rows={3} onChange={(v) => updateChar(i, { image_prompt: v })} />
              </Field>
              <Field label="负面提示词">
                <TextArea value={c.negative_prompt ?? ''} rows={1} onChange={(v) => updateChar(i, { negative_prompt: v })} />
              </Field>
            </Card>
          ))}
        </div>
      </div>

      {/* 场景 */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-octopus-text">🏞️ 场景 ({scenes.length})</span>
          {!readOnly && (
            <button
              onClick={() => {
                setScenes((p) => [...p, { scene_id: `S${String(p.length + 1).padStart(2, '0')}` }])
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
              title={s.location || s.scene_id || `场景${i + 1}`}
              subtitle={safeSlice(s.environment, 60)}
              onRemove={readOnly ? undefined : () => { setScenes((p) => p.filter((_, k) => k !== i)); setDirty(true) }}
            >
              <Field label="地点">
                <TextInput value={s.location ?? ''} onChange={(v) => updateScene(i, { location: v })} />
              </Field>
              <Field label="环境">
                <TextArea value={s.environment ?? ''} rows={2} onChange={(v) => updateScene(i, { environment: v })} />
              </Field>
              <Field label="道具">
                <TextInput value={s.props ?? ''} onChange={(v) => updateScene(i, { props: v })} />
              </Field>
              <Field label="光影">
                <TextInput value={s.lighting ?? ''} onChange={(v) => updateScene(i, { lighting: v })} />
              </Field>
              <Field label="氛围">
                <TextInput value={s.mood ?? ''} onChange={(v) => updateScene(i, { mood: v })} />
              </Field>
              <Field label="图像生成提示词（英文）">
                <TextArea value={s.image_prompt ?? ''} rows={3} onChange={(v) => updateScene(i, { image_prompt: v })} />
              </Field>
            </Card>
          ))}
        </div>
      </div>

      {/* 0.6.4: 关键道具 */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-octopus-text">🎁 道具 ({props.length})</span>
          {!readOnly && (
            <button
              onClick={() => {
                setProps((p) => [...p, { prop_id: `P${String(p.length + 1).padStart(2, '0')}` }])
                setDirty(true)
              }}
              className="flex items-center gap-1 text-xs text-octopus-primary hover:text-octopus-accent"
            >
              <Plus className="w-3 h-3" />
              添加道具
            </button>
          )}
        </div>
        <div className="space-y-2">
          {props.map((p, i) => (
            <Card
              key={`prop-${i}`}
              title={p.name || p.prop_id || `道具${i + 1}`}
              subtitle={safeSlice(p.description || p.use_case, 60)}
              onRemove={readOnly ? undefined : () => { setProps((prev) => prev.filter((_, k) => k !== i)); setDirty(true) }}
            >
              <Field label="名称">
                <TextInput value={p.name ?? ''} onChange={(v) => updateProp(i, { name: v })} />
              </Field>
              <Field label="描述" hint="材质、颜色、尺寸、关键细节">
                <TextArea value={p.description ?? ''} rows={2} onChange={(v) => updateProp(i, { description: v })} />
              </Field>
              <Field label="用途" hint="在剧情中的作用、在哪几场出现">
                <TextArea value={p.use_case ?? ''} rows={2} onChange={(v) => updateProp(i, { use_case: v })} />
              </Field>
              <Field label="图像生成提示词（中文）" hint="单道具特写、纯白背景、无人物无场景">
                <TextArea value={p.image_prompt ?? ''} rows={3} onChange={(v) => updateProp(i, { image_prompt: v })} />
              </Field>
              <Field label="负面提示词">
                <TextArea value={p.negative_prompt ?? ''} rows={1} onChange={(v) => updateProp(i, { negative_prompt: v })} />
              </Field>
            </Card>
          ))}
        </div>
      </div>
    </div>
  )
}
