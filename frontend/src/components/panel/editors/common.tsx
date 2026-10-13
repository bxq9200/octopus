/**
 * 编辑器通用工具与基础组件
 */
import React from 'react'

// 把任意值安全转成字符串，避免对象/数组直接传给 input value 导致崩溃
export function toStr(v: any): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

// 安全截断字符串（兼容非字符串输入）
export function safeSlice(v: any, n: number): string {
  return toStr(v).slice(0, n)
}

// 标签 + 单行输入
export function Field({
  label,
  children,
  hint,
}: {
  label: string
  children: React.ReactNode
  hint?: string
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs text-octopus-muted">{label}</label>
      {children}
      {hint && <div className="text-[10px] text-octopus-muted/70">{hint}</div>}
    </div>
  )
}

export function TextInput({
  value,
  onChange,
  placeholder,
}: {
  value: any
  onChange: (v: string) => void
  placeholder?: string
}) {
  return (
    <input
      type="text"
      value={toStr(value)}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className="bg-octopus-bg border border-octopus-border rounded px-2 py-1.5 text-sm text-octopus-text w-full focus:border-octopus-primary outline-none"
    />
  )
}

export function TextArea({
  value,
  onChange,
  rows = 3,
  placeholder,
}: {
  value: any
  onChange: (v: string) => void
  rows?: number
  placeholder?: string
}) {
  return (
    <textarea
      value={toStr(value)}
      placeholder={placeholder}
      rows={rows}
      onChange={(e) => onChange(e.target.value)}
      className="bg-octopus-bg border border-octopus-border rounded px-2 py-1.5 text-sm text-octopus-text w-full focus:border-octopus-primary outline-none resize-y"
    />
  )
}

export function NumberInput({
  value,
  onChange,
  step = 1,
  min,
  max,
}: {
  value: any
  onChange: (v: number) => void
  step?: number
  min?: number
  max?: number
}) {
  const n = typeof value === 'number' ? value : Number(value) || 0
  return (
    <input
      type="number"
      value={n}
      step={step}
      min={min}
      max={max}
      onChange={(e) => {
        let v = Number(e.target.value)
        // 真正夹到 [min, max]（浏览器的 min/max 只挡箭头微调，手输仍能超界）
        if (typeof min === 'number' && v < min) v = min
        if (typeof max === 'number' && v > max) v = max
        onChange(v)
      }}
      className="bg-octopus-bg border border-octopus-border rounded px-2 py-1.5 text-sm text-octopus-text w-full focus:border-octopus-primary outline-none"
    />
  )
}

export function Select({
  value,
  options,
  onChange,
}: {
  value: any
  options: { value: string; label: string }[]
  onChange: (v: string) => void
}) {
  return (
    <select
      value={toStr(value)}
      onChange={(e) => onChange(e.target.value)}
      className="bg-octopus-bg border border-octopus-border rounded px-2 py-1.5 text-sm text-octopus-text w-full focus:border-octopus-primary outline-none"
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  )
}

// 折叠卡片（如分镜中的每一个 shot）
export function Card({
  title,
  subtitle,
  children,
  defaultOpen = false,
  onRemove,
  badge,
}: {
  title: string
  subtitle?: string
  children: React.ReactNode
  defaultOpen?: boolean
  onRemove?: () => void
  badge?: React.ReactNode
}) {
  const [open, setOpen] = React.useState(defaultOpen)
  return (
    <div className="rounded-lg border border-octopus-border bg-octopus-bg overflow-hidden">
      <div
        className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-white/5"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="text-xs text-octopus-muted">{open ? '▼' : '▶'}</span>
        <div className="flex-1 min-w-0">
          <div className="text-sm text-octopus-text truncate">{title}</div>
          {subtitle && <div className="text-[10px] text-octopus-muted truncate">{subtitle}</div>}
        </div>
        {badge}
        {onRemove && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              onRemove()
            }}
            className="text-octopus-muted hover:text-red-400 text-xs"
            title="删除"
          >
            ✕
          </button>
        )}
      </div>
      {open && <div className="px-3 pb-3 pt-1 space-y-2 border-t border-octopus-border">{children}</div>}
    </div>
  )
}

// 通用：尝试美化字段名
export function humanizeKey(k: string): string {
  const dict: Record<string, string> = {
    topic: '主题',
    style: '风格',
    duration: '时长',
    audience: '受众',
    mood: '情绪',
    project_type: '项目类型',
    platform: '平台',
    aspect_ratio: '画面比例',
    scene_description: '场景描述',
    characters: '角色',
    scenes: '场景',
    prompt: '提示词',
    visual_prompt: '画面提示词',
    video_prompt: '视频提示词',
    duration_seconds: '时长（秒）',
    shot_id: '镜头编号',
    scene_id: '场景编号',
    shot_size: '景别',
    camera_angle: '机位',
    camera_movement: '运镜',
    subject: '主体',
    action: '动作',
    emotion: '情绪',
    environment: '环境',
    requires_keyframe: '需关键帧',
  }
  return dict[k] ?? k
}
