/**
 * 视频合并节点编辑器 — 转场选择、时长、分辨率
 */
import React, { useEffect, useMemo, useState } from 'react'
import { Save, Film, GripVertical, ArrowUp, ArrowDown } from 'lucide-react'
import { Field, Select, NumberInput, TextInput } from './common'
import { WorkflowNode } from '@/types'

const TRANSITION_OPTIONS: { value: string; label: string }[] = [
  { value: 'none', label: '无转场（硬切）' },
  { value: 'hard_cut', label: 'hard_cut（接近硬切）' },
  { value: 'fade', label: '淡入淡出 fade' },
  { value: 'dissolve', label: '溶解 dissolve（叠化）' },
  { value: 'fadeblack', label: '黑场过渡 fadeblack' },
  { value: 'fadewhite', label: '白场过渡 fadewhite' },
  { value: 'wipeleft', label: '左擦除 wipeleft' },
  { value: 'wiperight', label: '右擦除 wiperight' },
  { value: 'wipeup', label: '上擦除 wipeup' },
  { value: 'wipedown', label: '下擦除 wipedown' },
  { value: 'slideleft', label: '左推 slideleft' },
  { value: 'slideright', label: '右推 slideright' },
  { value: 'slideup', label: '上推 slideup' },
  { value: 'slidedown', label: '下推 slidedown' },
  { value: 'circleopen', label: '圆形展开 circleopen' },
  { value: 'circleclose', label: '圆形收缩 circleclose' },
  { value: 'radial', label: '雷达扫描 radial' },
  { value: 'smoothleft', label: '平滑左 smoothleft' },
  { value: 'smoothright', label: '平滑右 smoothright' },
  { value: 'smoothup', label: '平滑上 smoothup' },
  { value: 'smoothdown', label: '平滑下 smoothdown' },
  { value: 'zoomin', label: '推近 zoomin' },
  { value: 'pixelize', label: '像素化 pixelize' },
]

// 0.6.1: 标准化前端别名到后端期望的值
const _ALIAS_FOR_DISPLAY: Record<string, string> = {
  wipe_left: 'wipeleft', wipe_right: 'wiperight', wipe_up: 'wipeup', wipe_down: 'wipedown',
  slide_left: 'slideleft', slide_right: 'slideright', slide_up: 'slideup', slide_down: 'slidedown',
  smooth_left: 'smoothleft', smooth_right: 'smoothright', smooth_up: 'smoothup', smooth_down: 'smoothdown',
  circle_open: 'circleopen', circle_close: 'circleclose',
}
const normalizeTrans = (t: string | undefined | null): string => {
  if (!t) return 'fade'
  return _ALIAS_FOR_DISPLAY[t] || t
}

const RESOLUTION_PRESETS = [
  { value: '720x1280', label: '720×1280（竖屏 9:16）' },
  { value: '1080x1920', label: '1080×1920（竖屏 9:16 高清）' },
  { value: '1280x720', label: '1280×720（横屏 16:9）' },
  { value: '1920x1080', label: '1920×1080（横屏 16:9 高清）' },
  { value: '1080x1080', label: '1080×1080（方形 1:1）' },
]

export default function MergeEditor({
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
  const [dragIdx, setDragIdx] = useState<number | null>(null)
  const [dragOverIdx, setDragOverIdx] = useState<number | null>(null)

  useEffect(() => {
    setParams(node.params ?? {})
    setDirty(false)
  }, [node.id, node.params])

  const videos = useMemo<any[]>(
    () => (Array.isArray(params.videos) ? params.videos : []),
    [params.videos],
  )

  const transition = params.transition ?? 'fade'
  const transDur = typeof params.transition_duration === 'number'
    ? params.transition_duration
    : 0.6
  const resolution = params.resolution ?? '720x1280'
  // 0.6.1: BGM 音量（dB，-40~0）
  const bgmVolumeDb = typeof params.bgm_volume_db === 'number' ? params.bgm_volume_db : -12
  const hasBgm = !!params.bgm_path
  const bgmOnly = params.bgm_only !== false  // 默认 true（有 BGM 时丢 Seedance 音轨）
  const audioSegments: any[] = Array.isArray(params.audio_segments) ? params.audio_segments : []

  const update = (k: string, v: any) => {
    setParams((p) => ({ ...p, [k]: v }))
    setDirty(true)
  }

  // 0.6.1+: 改一些"语义关键"字段（如 bgm_only 这种切换音轨来源）时立即落库，
  // 避免用户改了 checkbox 直接点"重跑"忘了点"保存修改"，新值丢失
  const updateAndSave = async (k: string, v: any) => {
    const next = { ...params, [k]: v }
    setParams(next)
    setDirty(false)
    try {
      await onSaveParams(next)
    } catch {
      // 失败时回滚 dirty 标记，提示用户手动保存
      setDirty(true)
    }
  }

  const reorder = (from: number, to: number) => {
    if (from === to || from < 0 || to < 0) return
    setParams((p) => {
      const arr = Array.isArray(p.videos) ? [...p.videos] : []
      if (from >= arr.length || to >= arr.length) return p
      const [moved] = arr.splice(from, 1)
      arr.splice(to, 0, moved)
      return { ...p, videos: arr }
    })
    setDirty(true)
  }

  const sortByShotId = () => {
    setParams((p) => {
      const arr = Array.isArray(p.videos) ? [...p.videos] : []
      arr.sort((a, b) => {
        const idA = typeof a === 'string' ? '' : (a?.shot_id || '')
        const idB = typeof b === 'string' ? '' : (b?.shot_id || '')
        // 按 shot_id 里的数字升序；没数字退化字典序
        const numA = parseInt((idA.match(/(\d+)/) || ['', '99999'])[1], 10)
        const numB = parseInt((idB.match(/(\d+)/) || ['', '99999'])[1], 10)
        if (numA !== numB) return numA - numB
        return idA.localeCompare(idB)
      })
      return { ...p, videos: arr }
    })
    setDirty(true)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      // 0.6.1: 从 videos[i].transition_to_next 派生 transitions 数组（length=N-1）
      const arr = Array.isArray(params.videos) ? params.videos : []
      const transitions = arr.length >= 2
        ? arr.slice(0, -1).map((v: any) => normalizeTrans(typeof v === 'string' ? null : v?.transition_to_next))
        : []
      const out = { ...params, transitions }
      await onSaveParams(out)
      setDirty(false)
    } finally {
      setSaving(false)
    }
  }

  // 0.6.1: 给单个 video 设置 transition_to_next
  const setPairTransition = (i: number, value: string) => {
    setParams((p) => {
      const arr = Array.isArray(p.videos) ? [...p.videos] : []
      if (i < 0 || i >= arr.length) return p
      const cur = arr[i]
      if (typeof cur === 'string') {
        arr[i] = { video_url: cur, transition_to_next: value }
      } else {
        arr[i] = { ...cur, transition_to_next: value }
      }
      return { ...p, videos: arr }
    })
    setDirty(true)
  }

  // 0.6.1: 重置每段转场为 storyboard 的默认值（清掉 video.transition_to_next，让后端兜底 fade）
  // 注：这里其实是"全部回落为 fade"。若需要"恢复 storyboard 推荐值"，要在创建 merge 节点时把原始
  // transition_to_next 备份到另一个字段；当前 _enriched() 写的就是 storyboard 的原值，所以"恢复"
  // 等价于"刷新页面"（重新从 node.params 读）。这里提供"全部硬切"+"全部 fade"两个快捷。
  const resetAllTo = (value: string) => {
    setParams((p) => {
      const arr = Array.isArray(p.videos) ? [...p.videos] : []
      const updated = arr.map((v: any, idx: number) =>
        idx === arr.length - 1
          ? v
          : (typeof v === 'string'
              ? { video_url: v, transition_to_next: value }
              : { ...v, transition_to_next: value })
      )
      return { ...p, videos: updated }
    })
    setDirty(true)
  }

  const isCustomResolution = !RESOLUTION_PRESETS.find((p) => p.value === resolution)

  const result = node.result || {}
  const usedTransition = result.transition as string | undefined
  const segments = result.segments as number | undefined
  const duration = result.duration as number | undefined

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

      {/* 待合并视频列表 */}
      <Field
        label={`待合并片段（${videos.length}）`}
        hint="拖动行调整顺序；每对之间可独立选转场"
      >
        {videos.length > 1 && !readOnly && (
          <div className="flex justify-end gap-2 mb-1">
            <button
              onClick={() => resetAllTo('fade')}
              className="text-[10px] text-octopus-muted hover:text-octopus-text"
              title="把所有转场重置为 fade"
            >
              全部 fade
            </button>
            <button
              onClick={() => resetAllTo('hard_cut')}
              className="text-[10px] text-octopus-muted hover:text-octopus-text"
              title="把所有转场重置为硬切"
            >
              全部硬切
            </button>
            <button
              onClick={sortByShotId}
              className="text-[10px] text-cyan-300/80 hover:text-cyan-200 underline-offset-2 hover:underline"
            >
              按 SHOT 排序
            </button>
          </div>
        )}
        <div className="space-y-1 max-h-72 overflow-y-auto pr-1">
          {videos.length === 0 ? (
            <div className="text-xs text-octopus-muted py-2 text-center">无视频片段</div>
          ) : (
            videos.map((v, i) => {
              const url = typeof v === 'string' ? v : v?.video_url
              const shotId = typeof v === 'string' ? '' : (v?.shot_id || '')
              const pairTrans = typeof v === 'string' ? '' : normalizeTrans(v?.transition_to_next)
              const isDragging = dragIdx === i
              const isDropTarget = dragOverIdx === i && dragIdx !== null && dragIdx !== i
              const isLast = i === videos.length - 1
              return (
                <React.Fragment key={i}>
                  <div
                    draggable={!readOnly}
                    onDragStart={() => setDragIdx(i)}
                    onDragEnd={() => { setDragIdx(null); setDragOverIdx(null) }}
                    onDragOver={(e) => { e.preventDefault(); setDragOverIdx(i) }}
                    onDragLeave={() => setDragOverIdx((d) => (d === i ? null : d))}
                    onDrop={(e) => {
                      e.preventDefault()
                      if (dragIdx !== null) reorder(dragIdx, i)
                      setDragIdx(null); setDragOverIdx(null)
                    }}
                    className={`flex items-center gap-2 px-2 py-1.5 rounded border bg-octopus-bg text-xs text-octopus-text transition-all ${
                      isDragging ? 'opacity-40' : ''
                    } ${
                      isDropTarget ? 'border-cyan-400/70 bg-cyan-400/10' : 'border-octopus-border'
                    } ${!readOnly ? 'cursor-grab active:cursor-grabbing' : ''}`}
                  >
                    {!readOnly && (
                      <GripVertical className="w-3 h-3 text-octopus-muted flex-shrink-0" />
                    )}
                    <span className="text-octopus-muted flex-shrink-0 w-5 text-right">{i + 1}.</span>
                    {shotId ? (
                      <span className="px-1.5 py-0.5 rounded bg-octopus-primary/15 text-octopus-primary text-[10px] font-mono flex-shrink-0">
                        {shotId}
                      </span>
                    ) : (
                      <Film className="w-3 h-3 text-octopus-muted flex-shrink-0" />
                    )}
                    <span className="truncate font-mono text-[10px] flex-1">{url || '(无 URL)'}</span>
                    {!readOnly && videos.length > 1 && (
                      <div className="flex flex-shrink-0 gap-0.5">
                        <button
                          title="上移"
                          disabled={i === 0}
                          onClick={() => reorder(i, i - 1)}
                          className="p-0.5 rounded hover:bg-white/10 disabled:opacity-20 disabled:cursor-not-allowed"
                        >
                          <ArrowUp className="w-3 h-3" />
                        </button>
                        <button
                          title="下移"
                          disabled={i === videos.length - 1}
                          onClick={() => reorder(i, i + 1)}
                          className="p-0.5 rounded hover:bg-white/10 disabled:opacity-20 disabled:cursor-not-allowed"
                        >
                          <ArrowDown className="w-3 h-3" />
                        </button>
                      </div>
                    )}
                  </div>
                  {/* 0.6.1: 每对之间的转场下拉 */}
                  {!isLast && (
                    <div className="flex items-center gap-2 pl-7 pr-2 py-0.5">
                      <span className="text-[10px] text-octopus-muted flex-shrink-0">↓ 转场</span>
                      <select
                        value={pairTrans || 'fade'}
                        disabled={readOnly}
                        onChange={(e) => setPairTransition(i, e.target.value)}
                        className="flex-1 px-1.5 py-0.5 rounded border border-octopus-border bg-octopus-panel text-octopus-text text-[10px]"
                      >
                        {TRANSITION_OPTIONS.map((o) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </select>
                    </div>
                  )}
                </React.Fragment>
              )
            })
          )}
        </div>
      </Field>

      <Field
        label="全局兜底转场"
        hint="0.6.1 起每段独立转场（上方列表里改），此处仅作为缺失/未指定时的兜底"
      >
        <Select
          value={transition}
          options={TRANSITION_OPTIONS}
          onChange={(v) => update('transition', v)}
        />
      </Field>

      <Field
        label={`转场时长（${transDur.toFixed(2)} 秒）`}
        hint="0.1–3.0 秒；过长会被自动降级为硬拼接"
      >
        <input
          type="range"
          min={0.1}
          max={3.0}
          step={0.1}
          value={transDur}
          onChange={(e) => update('transition_duration', Number(e.target.value))}
          className="w-full accent-octopus-primary"
          disabled={readOnly || transition === 'none'}
        />
      </Field>

      {/* 0.6.2: BGM 开关 — ON=保留 Seedance 原音(对白/口型/环境音) + 叠加 BGM(ducking)；OFF=只用 Seedance 不混 BGM */}
      {hasBgm && (
        <>
          <Field
            label="BGM 配乐"
            hint="✓ = 保留 Seedance 原音（对白/口型/环境音）+ 在上方叠加 AI BGM；对白响起时 BGM 自动压低 (sidechain ducking)。取消勾选 = 完全不混 BGM，只用 Seedance 原音"
          >
            <label className="flex items-center gap-2 text-xs text-octopus-text cursor-pointer">
              <input
                type="checkbox"
                checked={bgmOnly}
                onChange={(e) => updateAndSave('bgm_only', e.target.checked)}
                disabled={readOnly}
                className="accent-octopus-primary"
              />
              使用 AI 配乐 BGM
              <span className="text-[10px] text-octopus-muted ml-1">（切换立即保存）</span>
            </label>
          </Field>

          {bgmOnly && (
            <Field
              label={`BGM 音量（${bgmVolumeDb.toFixed(0)} dB）`}
              hint="0 dB = 原始音量；-12 dB 适合常规；-30 dB 以下基本听不到（松开滑块即自动保存）"
            >
              <input
                type="range"
                min={-40}
                max={0}
                step={1}
                value={bgmVolumeDb}
                onChange={(e) => update('bgm_volume_db', Number(e.target.value))}
                onPointerUp={() => updateAndSave('bgm_volume_db', bgmVolumeDb)}
                className="w-full accent-octopus-primary"
                disabled={readOnly}
              />
              {audioSegments.length > 0 && (
                <div className="text-[10px] text-octopus-muted mt-1">
                  BGM 来自音频导演 {audioSegments.length} 段叙事
                </div>
              )}
            </Field>
          )}
        </>
      )}

      <Field label="输出分辨率">
        <Select
          value={isCustomResolution ? '__custom__' : resolution}
          options={[
            ...RESOLUTION_PRESETS,
            { value: '__custom__', label: '自定义...' },
          ]}
          onChange={(v) => {
            if (v === '__custom__') {
              update('resolution', resolution)
            } else {
              update('resolution', v)
            }
          }}
        />
        {isCustomResolution && (
          <div className="mt-1">
            <TextInput
              value={resolution}
              onChange={(s) => update('resolution', s)}
              placeholder="例如 1080x1920"
            />
          </div>
        )}
      </Field>

      {/* 结果信息 */}
      {result.video_url && (
        <div className="space-y-2 pt-2 border-t border-octopus-border">
          <div className="text-xs text-octopus-muted">合并结果</div>
          <div className="grid grid-cols-3 gap-2 text-[11px]">
            <div className="rounded border border-octopus-border bg-octopus-bg px-2 py-1">
              <div className="text-octopus-muted">片段</div>
              <div className="text-octopus-text font-medium">{segments ?? '-'}</div>
            </div>
            <div className="rounded border border-octopus-border bg-octopus-bg px-2 py-1">
              <div className="text-octopus-muted">时长</div>
              <div className="text-octopus-text font-medium">
                {duration ? `${duration.toFixed(1)}s` : '-'}
              </div>
            </div>
            <div className="rounded border border-octopus-border bg-octopus-bg px-2 py-1">
              <div className="text-octopus-muted">实际转场</div>
              <div className="text-octopus-text font-medium truncate">{usedTransition ?? '-'}</div>
            </div>
          </div>
          <video
            src={result.video_url}
            controls
            className="w-full rounded-lg border border-octopus-border"
          />
        </div>
      )}

      {result.error && (
        <div className="p-2 rounded bg-red-900/20 border border-red-500/30 text-red-300 text-xs">
          ❌ {result.error}
        </div>
      )}
    </div>
  )
}
