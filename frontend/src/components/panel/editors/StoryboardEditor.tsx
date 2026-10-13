/**
 * 分镜编辑器 — 以列表卡片形式展示 shots，每个 shot 可独立编辑
 */
import React, { useState, useEffect } from 'react'
import { Plus, Save } from 'lucide-react'
import { Card, Field, TextInput, TextArea, NumberInput, Select, safeSlice } from './common'
import MentionTextArea, { MentionItem, mentionAliases } from './MentionTextArea'
import { WorkflowNode } from '@/types'

const SHOT_SIZES = [
  { value: 'ELS', label: 'ELS 极远景' },
  { value: 'LS', label: 'LS 远景' },
  { value: 'MS', label: 'MS 中景' },
  { value: 'CU', label: 'CU 近景' },
  { value: 'ECU', label: 'ECU 特写' },
]

const CAMERA_MOVEMENTS = [
  { value: 'static', label: 'static 固定' },
  { value: 'dolly_in', label: 'dolly_in 推近' },
  { value: 'dolly_out', label: 'dolly_out 拉远' },
  { value: 'pan_left', label: 'pan_left 左摇' },
  { value: 'pan_right', label: 'pan_right 右摇' },
  { value: 'crane_up', label: 'crane_up 升' },
  { value: 'crane_down', label: 'crane_down 降' },
  { value: 'handheld', label: 'handheld 手持' },
  { value: 'follow', label: 'follow 跟拍' },
]

// 0.6.1: 节奏角色（pacing_role）UI
const PACING_ROLES = [
  { value: 'establish', label: 'establish 建立' },
  { value: 'build', label: 'build 推进' },
  { value: 'climax', label: 'climax 高潮' },
  { value: 'rest', label: 'rest 缓冲' },
  { value: 'release', label: 'release 释放' },
]
const PACING_BADGE_CLASS: Record<string, string> = {
  establish: 'bg-sky-500/20 text-sky-300 border-sky-500/40',
  build: 'bg-amber-500/20 text-amber-300 border-amber-500/40',
  climax: 'bg-red-500/20 text-red-300 border-red-500/40',
  rest: 'bg-slate-500/20 text-slate-300 border-slate-500/40',
  release: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40',
}

// 0.6.1: 连续性 + 转场（与后端 _valid_continuity / _valid_transitions 对齐）
const CONTINUITY_OPTIONS = [
  { value: 'match_cut', label: 'match_cut 动作/形状延续' },
  { value: 'scale_jump', label: 'scale_jump 景别大跳' },
  { value: 'movement_continuation', label: 'movement_continuation 运镜延续' },
  { value: 'contrast', label: 'contrast 反差' },
  { value: 'hard_cut', label: 'hard_cut 硬切' },
]
const TRANSITION_OPTIONS = [
  { value: 'fade', label: 'fade' },
  { value: 'dissolve', label: 'dissolve' },
  { value: 'fadeblack', label: 'fadeblack' },
  { value: 'fadewhite', label: 'fadewhite' },
  { value: 'wipe_left', label: 'wipe_left' },
  { value: 'wipe_right', label: 'wipe_right' },
  { value: 'wipe_up', label: 'wipe_up' },
  { value: 'wipe_down', label: 'wipe_down' },
  { value: 'slide_left', label: 'slide_left' },
  { value: 'slide_right', label: 'slide_right' },
  { value: 'slide_up', label: 'slide_up' },
  { value: 'slide_down', label: 'slide_down' },
  { value: 'circleopen', label: 'circleopen' },
  { value: 'circleclose', label: 'circleclose' },
  { value: 'radial', label: 'radial' },
  { value: 'smoothleft', label: 'smoothleft' },
  { value: 'smoothright', label: 'smoothright' },
  { value: 'smoothup', label: 'smoothup' },
  { value: 'smoothdown', label: 'smoothdown' },
  { value: 'hard_cut', label: 'hard_cut（接近硬切）' },
]

interface Shot {
  shot_id?: string
  scene_id?: string
  duration_seconds?: number
  shot_size?: string
  camera_angle?: string
  camera_movement?: string
  subject?: string
  action?: string
  emotion?: string
  environment?: string
  visual_prompt?: string
  video_prompt?: string
  requires_keyframe?: boolean
  // 0.6.1: 节奏规划与转场
  pacing_role?: string
  continuity_to_next?: string | null
  transition_to_next?: string | null
  // 0.6.2: 音频内容（对白/旁白/音效）
  dialogue?: string
  narration?: string
  sound_effects?: string[]
  [k: string]: any
}

export default function StoryboardEditor({
  node,
  onSave,
  readOnly,
}: {
  node: WorkflowNode
  onSave: (newResult: any) => Promise<void> | void
  readOnly?: boolean
}) {
  const initialShots: Shot[] = (node.result?.storyboard ?? []) as Shot[]
  const [shots, setShots] = useState<Shot[]>(initialShots)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setShots((node.result?.storyboard ?? []) as Shot[])
    setDirty(false)
  }, [node.id, node.result])

  const updateShot = (i: number, patch: Partial<Shot>) => {
    setShots((prev) => prev.map((s, idx) => (idx === i ? { ...s, ...patch } : s)))
    setDirty(true)
  }

  const removeShot = (i: number) => {
    setShots((prev) => prev.filter((_, idx) => idx !== i))
    setDirty(true)
  }

  const addShot = () => {
    const next: Shot = {
      shot_id: `SHOT_${String(shots.length + 1).padStart(2, '0')}`,
      duration_seconds: 5,
      shot_size: 'MS',
      camera_movement: 'static',
      subject: '',
      action: '',
      visual_prompt: '',
      video_prompt: '',
      requires_keyframe: false,
    }
    setShots((prev) => [...prev, next])
    setDirty(true)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const newResult = {
        ...(node.result ?? {}),
        storyboard: shots,
        total_duration_seconds: shots.reduce((sum, s) => sum + Number(s.duration_seconds || 0), 0),
      }
      await onSave(newResult)
      setDirty(false)
    } finally {
      setSaving(false)
    }
  }

  const totalDur = shots.reduce((sum, s) => sum + Number(s.duration_seconds || 0), 0)

  // 从节点 params 拿可引用的角色/场景/道具（0.6.4 新增 prop_refs）
  const charRefs: any[] = (node.params as any)?.character_refs ?? []
  const sceneRefs: any[] = (node.params as any)?.scene_refs ?? []
  const propRefs: any[] = (node.params as any)?.prop_refs ?? []
  const mentionItems: MentionItem[] = [
    ...charRefs.filter((c) => c?.name).flatMap((c) => {
      const st = (c.subject_type as string) || 'human'
      const typeIcon: Record<string, string> = {
        human: '👤', animal: '🐾', creature: '🐉', mech: '🤖', object: '📦',
      }
      const icon = typeIcon[st] || '🎭'
      const base: MentionItem = {
        value: c.name,
        label: `${icon} ${c.name}`,
        group: 'character' as const,
        imageUrl: c.image_url,
        hint: `${st}${typeof c.prompt === 'string' && c.prompt ? ' · ' + safeSlice(c.prompt, 50) : ''}`,
      }
      // 0.7.1 ①：多套服装 → 额外发 @角色#服装 候选项（各带本套缩略图）
      const outfits: any[] = Array.isArray(c.outfits) ? c.outfits : []
      if (outfits.length > 1) {
        return [
          base,
          ...outfits.filter((o) => o?.outfit_name).map((o) => ({
            value: `${c.name}#${o.outfit_name}`,
            label: `👗 ${c.name} · ${o.outfit_name}`,
            group: 'character' as const,
            imageUrl: o.image_url || c.image_url,
            hint: `${o.outfit_when ? o.outfit_when + ' · ' : ''}${typeof o.prompt === 'string' ? safeSlice(o.prompt, 40) : ''}`,
          })),
        ]
      }
      return [base]
    }),
    ...sceneRefs.filter((s) => s?.location).flatMap((s) => {
      const base: MentionItem = {
        value: s.location,
        label: `🏞️ ${s.location}`,
        group: 'scene' as const,
        imageUrl: s.image_url,
        hint: typeof s.prompt === 'string' ? safeSlice(s.prompt, 60) : '',
      }
      // 0.7.5：多机位板 → 额外发 @场景#机位 候选项（各带本机位缩略图），机制同 @角色#服装
      const cameras: any[] = Array.isArray(s.cameras) ? s.cameras : []
      if (cameras.length > 1) {
        return [
          base,
          ...cameras.slice(1).filter((cam) => cam?.camera_name).map((cam) => ({
            value: `${s.location}#${cam.camera_name}`,
            label: `🎥 ${s.location} · ${cam.camera_name}`,
            group: 'scene' as const,
            imageUrl: cam.image_url || s.image_url,
            hint: `${cam.when_to_use ? cam.when_to_use + ' · ' : ''}${typeof cam.prompt === 'string' ? safeSlice(cam.prompt, 40) : ''}`,
          })),
        ]
      }
      return [base]
    }),
    ...propRefs.filter((p) => p?.name).flatMap((p) => {
      const base: MentionItem = {
        value: p.name,
        label: `🎁 ${p.name}`,
        group: 'prop' as const,
        imageUrl: p.image_url,
        hint: typeof p.use_case === 'string' && p.use_case ? p.use_case : (typeof p.prompt === 'string' ? safeSlice(p.prompt, 60) : ''),
      }
      // 0.7.2：多状态道具 → 额外发 @道具#状态 候选项（各带本态缩略图），机制同 @角色#服装
      const states: any[] = Array.isArray(p.states) ? p.states : []
      if (states.length > 1) {
        return [
          base,
          ...states.filter((st) => st?.state_name).map((st) => ({
            value: `${p.name}#${st.state_name}`,
            label: `🎁 ${p.name} · ${st.state_name}`,
            group: 'prop' as const,
            imageUrl: st.image_url || p.image_url,
            hint: `${st.state_description ? st.state_description + ' · ' : ''}${typeof st.prompt === 'string' ? safeSlice(st.prompt, 40) : ''}`,
          })),
        ]
      }
      return [base]
    }),
  ]

  // 0.6.1: 节奏 arc 概览 + 整体规划
  const shotPlanSummary: string = (node.result as any)?.shot_plan_summary || ''
  const pacingCounts: Record<string, number> = (node.result as any)?.summary?.pacing_roles_count || {}

  // 0.6.5: 覆盖率（角色 / 场景 / 道具）
  const cov: any = (node.result as any)?.coverage || null
  const covRow = cov ? [
    { label: '🎭 角色', covered: cov.characters_covered, total: cov.characters_total, uncov: cov.uncovered_characters, optional: false },
    { label: '🏞️ 场景', covered: cov.scenes_covered, total: cov.scenes_total, uncov: cov.uncovered_scenes, optional: false },
    // 道具是尽力项：剧情用不到的道具缺失属正常，不当错误
    { label: '🎁 道具', covered: cov.props_covered ?? 0, total: cov.props_total ?? 0, uncov: cov.uncovered_props ?? [], optional: true },
  ].filter(r => r.total > 0) : []

  return (
    <div className="space-y-3">
      {(shotPlanSummary || Object.keys(pacingCounts).length > 0) && (
        <div className="rounded border border-octopus-border bg-octopus-bg/40 p-2 space-y-1">
          {shotPlanSummary && (
            <div className="text-[11px] text-octopus-text leading-relaxed">
              <span className="text-octopus-muted">🎬 节奏规划：</span>
              {shotPlanSummary}
            </div>
          )}
          {Object.keys(pacingCounts).length > 0 && (
            <div className="flex flex-wrap gap-1">
              {Object.entries(pacingCounts).map(([role, count]) => (
                <span
                  key={role}
                  className={`text-[10px] px-1.5 py-0.5 rounded border ${PACING_BADGE_CLASS[role] || 'bg-gray-500/20 text-gray-300 border-gray-500/40'}`}
                >
                  {role} × {count}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 0.6.5: 覆盖率 */}
      {covRow.length > 0 && (
        <div className="rounded border border-octopus-border bg-octopus-bg/40 p-2">
          <div className="text-[10px] text-octopus-muted mb-1">引用覆盖</div>
          <div className="flex flex-wrap gap-3 text-[11px]">
            {covRow.map((r, i) => {
              const full = r.covered === r.total
              // 道具是尽力项：未满用中性色 + (尽力)，不报红/黄警告
              const cls = full
                ? 'text-emerald-300'
                : (r.optional ? 'text-octopus-muted' : (r.covered === 0 ? 'text-red-400' : 'text-yellow-400'))
              return (
                <div key={i} className="flex items-center gap-1">
                  <span className="text-octopus-muted">{r.label}</span>
                  <span className={cls}>{r.covered}/{r.total}</span>
                  {full
                    ? <span className="text-emerald-400">✓</span>
                    : (r.optional ? <span className="text-octopus-muted">(尽力)</span> : <span>⚠</span>)}
                </div>
              )
            })}
          </div>
          {covRow.some(r => (r.uncov || []).length > 0) && (
            <div className="text-[10px] mt-1 leading-snug space-y-0.5">
              {covRow.filter(r => (r.uncov || []).length > 0).map((r, i) => (
                <div key={i} className={r.optional ? 'text-octopus-muted/80' : 'text-yellow-400/80'}>
                  {r.label} {r.optional ? '未用' : '缺'}：{Array.from(new Set(r.uncov || [])).join('、')}
                  {r.optional && <span className="text-octopus-muted/60">（剧情未用到，不影响）</span>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="flex items-center justify-between text-xs">
        <div className="text-octopus-muted">
          共 {shots.length} 个分镜，总时长 <span className="text-octopus-text">{totalDur}s</span>
        </div>
        {!readOnly && (
          <div className="flex gap-2">
            <button
              onClick={addShot}
              className="flex items-center gap-1 px-2 py-1 rounded bg-octopus-primary/20 hover:bg-octopus-primary/30 text-octopus-primary"
            >
              <Plus className="w-3 h-3" />
              新增
            </button>
            <button
              onClick={handleSave}
              disabled={!dirty || saving}
              className="flex items-center gap-1 px-2 py-1 rounded bg-green-600/80 hover:bg-green-500 disabled:opacity-40 text-white"
            >
              <Save className="w-3 h-3" />
              {saving ? '保存中...' : dirty ? '保存修改' : '已保存'}
            </button>
          </div>
        )}
      </div>

      <div className="space-y-2">
        {shots.map((shot, i) => {
          // 提取这个 shot 的 visual/video prompt 中所有 @ 引用，匹配到 character_refs/scene_refs
          const allText = `${shot.visual_prompt || ''} ${shot.video_prompt || ''}`
          const matched = Array.from(allText.matchAll(/@([\w一-鿿]+)/g)).map((m) => m[1])
          // 0.7.1 ①：别名匹配（@小峰 命中 "小峰（林悦老公）"）
          const usedChars = matched.filter((n) => charRefs.some((c: any) => c?.name && mentionAliases(c.name).includes(n)))
          const usedScenes = matched.filter((n) => sceneRefs.some((s: any) => s?.location && mentionAliases(s.location).includes(n)))
          const isLast = i === shots.length - 1
          const isChain = (shot.continuity_to_next || '').toLowerCase() === 'movement_continuation'
          // 折叠态只保留状态标（节奏 / 关键帧 / 续接）—— 角色信息放进 subtitle，避免一墙碎 pill
          const refChips = (
            <div className="flex flex-wrap items-center gap-1">
              {shot.pacing_role && (
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded border ${PACING_BADGE_CLASS[shot.pacing_role] || 'bg-gray-500/20 text-gray-300 border-gray-500/40'}`}
                  title="节奏角色"
                >
                  {shot.pacing_role}
                </span>
              )}
              {shot.requires_keyframe && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-yellow-500/15 text-yellow-300 border border-yellow-500/30" title="需要关键帧">🔑 关键帧</span>
              )}
              {isChain && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-500/15 text-cyan-300 border border-cyan-500/30" title="与下一镜首尾帧续接">🔗 续接</span>
              )}
            </div>
          )

          return (
            <Card
              key={`${shot.shot_id}-${i}`}
              title={`${shot.shot_id || `SHOT_${i + 1}`}  ·  ${shot.shot_size || 'MS'}  ·  ${shot.duration_seconds || 0}s`}
              subtitle={`${shot.subject ? shot.subject + ' · ' : ''}${safeSlice(shot.action || shot.visual_prompt, 50)}`}
              badge={refChips}
              onRemove={readOnly ? undefined : () => removeShot(i)}
              defaultOpen={false}
            >
              <Field label="镜头编号">
                <TextInput value={shot.shot_id ?? ''} onChange={(v) => updateShot(i, { shot_id: v })} />
              </Field>
              <Field label="场景编号">
                <TextInput value={shot.scene_id ?? ''} onChange={(v) => updateShot(i, { scene_id: v })} />
              </Field>
              <div className="grid grid-cols-2 gap-2">
                <Field label="时长（秒）">
                  <NumberInput
                    value={shot.duration_seconds ?? 5}
                    onChange={(v) => updateShot(i, { duration_seconds: v })}
                  />
                </Field>
                <Field label="景别">
                  <Select
                    value={shot.shot_size ?? 'MS'}
                    options={SHOT_SIZES}
                    onChange={(v) => updateShot(i, { shot_size: v })}
                  />
                </Field>
              </div>
              <Field label="运镜">
                <Select
                  value={shot.camera_movement ?? 'static'}
                  options={CAMERA_MOVEMENTS}
                onChange={(v) => updateShot(i, { camera_movement: v })}
              />
            </Field>
            {/* 0.6.1: 节奏角色 + 与下一镜的衔接 + 转场选择（最后一镜不显示后两项）*/}
            <div className="grid grid-cols-3 gap-2">
              <Field label="节奏角色">
                <Select
                  value={shot.pacing_role ?? (i === 0 ? 'establish' : isLast ? 'release' : 'build')}
                  options={PACING_ROLES}
                  onChange={(v) => updateShot(i, { pacing_role: v })}
                />
              </Field>
              {!isLast && (
                <>
                  <Field label="与下一镜衔接">
                    <Select
                      value={shot.continuity_to_next ?? 'hard_cut'}
                      options={CONTINUITY_OPTIONS}
                      onChange={(v) => updateShot(i, { continuity_to_next: v })}
                    />
                  </Field>
                  <Field label="转场">
                    <Select
                      value={shot.transition_to_next ?? 'fade'}
                      options={TRANSITION_OPTIONS}
                      onChange={(v) => updateShot(i, { transition_to_next: v })}
                    />
                  </Field>
                </>
              )}
            </div>
            <Field label="主体">
              <TextInput value={shot.subject ?? ''} onChange={(v) => updateShot(i, { subject: v })} />
            </Field>
            <Field label="动作">
              <TextArea value={shot.action ?? ''} rows={2} onChange={(v) => updateShot(i, { action: v })} />
            </Field>
            <Field label="情绪">
              <TextInput value={shot.emotion ?? ''} onChange={(v) => updateShot(i, { emotion: v })} />
            </Field>
            <Field label="环境">
              <TextInput value={shot.environment ?? ''} onChange={(v) => updateShot(i, { environment: v })} />
            </Field>
            <Field label="画面提示词（英文，用于生成关键帧/视频）" hint="输入 @ 可引用美术阶段的角色/场景，最终视频生成时会自动替换为详细描述">
              <MentionTextArea
                value={shot.visual_prompt ?? ''}
                rows={3}
                items={mentionItems}
                onChange={(v) => updateShot(i, { visual_prompt: v })}
              />
            </Field>
            <Field label="视频提示词（英文，描述动态）" hint="同样支持 @ 引用">
              <MentionTextArea
                value={shot.video_prompt ?? ''}
                rows={2}
                items={mentionItems}
                onChange={(v) => updateShot(i, { video_prompt: v })}
              />
            </Field>
            <label className="flex items-center gap-2 text-xs text-octopus-text">
              <input
                type="checkbox"
                checked={!!shot.requires_keyframe}
                onChange={(e) => updateShot(i, { requires_keyframe: e.target.checked })}
              />
              需要先生成关键帧
            </label>
          </Card>
          )
        })}

        {shots.length === 0 && (
          <div className="text-center text-xs text-octopus-muted py-6">
            还没有分镜。{!readOnly && '点击右上角「新增」添加。'}
          </div>
        )}
      </div>
    </div>
  )
}
