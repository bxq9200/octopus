/**
 * 歌词时间轴校对（1.1.0）
 *
 * 为什么必须有这个界面：带伴奏的歌做语音识别不可能全对——同音字会错
 * （「未愈的伤」→「卫浴的伤」），而且音乐直接转人声时起音检测会**吃掉第一个字**
 * （「清晨」→「晨」）。这些都得人耳听着改。
 *
 * 所以这里的重点不是"能编辑"，而是**能边听边改**：点一句就跳到那个时刻播放，
 * 播放时当前句自动高亮。改完立刻能看到镜头切分跟着变（本地算，不花钱）。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react'
import { Play, Pause, Save, Loader2, X, Scissors, RotateCcw, Plus, Trash2, Upload } from 'lucide-react'
import { mvAPI } from '@/services/api'
import LyricImportPanel from './LyricImportPanel'

export interface LyricLine { start: number; end: number; text: string }

interface Props {
  projectId: string
  audioUrl: string
  duration: number
  lines: LyricLine[]
  sections?: any[]
  onClose: () => void
  onSaved: (lines: LyricLine[]) => void
}

const fmt = (s: number) => {
  const m = Math.floor(s / 60)
  const sec = s - m * 60
  return `${m}:${sec.toFixed(2).padStart(5, '0')}`
}

export default function LyricTimelineEditor({
  projectId, audioUrl, duration, lines: initial, sections, onClose, onSaved,
}: Props) {
  const [lines, setLines] = useState<LyricLine[]>(() => initial.map((l) => ({ ...l })))
  const [playing, setPlaying] = useState(false)
  const [now, setNow] = useState(0)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [shots, setShots] = useState<any[] | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [importNote, setImportNote] = useState('')

  const audioRef = useRef<HTMLAudioElement>(null)
  const dirty = useMemo(
    () => JSON.stringify(lines) !== JSON.stringify(initial),
    [lines, initial],
  )

  // 播放进度：只在播放时跟，避免拖动时抖
  useEffect(() => {
    const el = audioRef.current
    if (!el) return
    const tick = () => setNow(el.currentTime)
    el.addEventListener('timeupdate', tick)
    el.addEventListener('ended', () => setPlaying(false))
    return () => { el.removeEventListener('timeupdate', tick) }
  }, [])

  const activeIdx = useMemo(
    () => lines.findIndex((l) => now >= l.start - 0.05 && now < l.end + 0.25),
    [lines, now],
  )

  const seekTo = (t: number) => {
    const el = audioRef.current
    if (!el) return
    el.currentTime = Math.max(0, Math.min(duration - 0.05, t))
    setNow(el.currentTime)
    if (!playing) { el.play(); setPlaying(true) }
  }

  const toggle = () => {
    const el = audioRef.current
    if (!el) return
    if (playing) { el.pause(); setPlaying(false) } else { el.play(); setPlaying(true) }
  }

  const patch = (i: number, k: keyof LyricLine, v: any) =>
    setLines((prev) => prev.map((l, idx) => (idx === i ? { ...l, [k]: v } : l)))

  const nudge = (i: number, k: 'start' | 'end', delta: number) =>
    patch(i, k, Math.max(0, Math.min(duration, +(lines[i][k] + delta).toFixed(2))))

  const removeLine = (i: number) => setLines((prev) => prev.filter((_, k) => k !== i))

  /** 在第 i 句后面插一句（ASR 漏词时补） */
  const insertAfter = (i: number) => {
    const cur = lines[i]
    const nxt = lines[i + 1]
    const gapEnd = nxt ? nxt.start : Math.min(duration, cur.end + 3)
    const st = +(cur.end + 0.1).toFixed(2)
    const en = +Math.max(st + 1, gapEnd - 0.1).toFixed(2)
    setLines((prev) => [...prev.slice(0, i + 1), { start: st, end: en, text: '' }, ...prev.slice(i + 1)])
  }

  const preview = async () => {
    setPreviewing(true); setError('')
    try {
      const res = await mvAPI.previewShots(projectId, { lines, sections, duration })
      setShots(res.data?.shots || [])
      const problems: string[] = res.data?.problems || []
      // 后端把契约体检结果一起回来了——有问题就直说，别等成片出来才发现画面跟歌错位
      if (problems.length) setError('镜头切分异常：' + problems.slice(0, 2).join('；'))
    } catch (e: any) {
      setError(e?.response?.data?.detail || '试算失败')
    } finally { setPreviewing(false) }
  }

  const save = async () => {
    const bad = lines.find((l) => l.end <= l.start)
    if (bad) { setError(`「${bad.text || '空行'}」的结束时间不能早于开始时间`); return }
    setSaving(true); setError('')
    try {
      await mvAPI.updateLyrics(projectId, lines.filter((l) => l.text.trim()))
      onSaved(lines)
      onClose()
    } catch (e: any) {
      setError(e?.response?.data?.detail || '保存失败')
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[9996] flex items-center justify-center bg-black/75 backdrop-blur-sm p-4"
         onClick={onClose}>
      <div className="w-[min(96vw,900px)] max-h-[92vh] flex flex-col rounded-2xl border border-octopus-border bg-[#0d0d14] shadow-2xl"
           onClick={(e) => e.stopPropagation()}>

        {/* 头 + 播放器 */}
        <div className="px-6 py-4 border-b border-white/5">
          <div className="flex items-center justify-between mb-3">
            <div>
              <div className="text-white font-medium">校对歌词时间轴</div>
              <div className="text-xs text-gray-500">
                点任意一句可跳到那一刻试听。识别难免有错字、句首的字也可能被吃掉，听着改最准。
              </div>
            </div>
            <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-white/5 text-gray-400">
              <X className="w-4 h-4" />
            </button>
          </div>

          <div className="flex items-center gap-3">
            <button onClick={toggle}
                    className="p-2 rounded-lg bg-cyan-500/90 hover:bg-cyan-400 text-black shrink-0">
              {playing ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
            </button>
            <input type="range" min={0} max={duration || 0} step={0.01} value={now}
                   onChange={(e) => seekTo(parseFloat(e.target.value))}
                   className="flex-1 accent-cyan-400" />
            <div className="text-xs text-gray-400 tabular-nums shrink-0 w-24 text-right">
              {fmt(now)} / {fmt(duration)}
            </div>
          </div>
          <audio ref={audioRef} src={audioUrl} preload="metadata" />
        </div>

        {/* 歌词表 */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-1.5">
          {lines.map((l, i) => {
            const on = i === activeIdx
            return (
              <div key={i}
                   className={`group flex items-center gap-2 px-2 py-1.5 rounded-lg border transition ${
                     on ? 'border-cyan-400/50 bg-cyan-400/[0.08]' : 'border-transparent hover:bg-white/[0.03]'
                   }`}>
                <button onClick={() => seekTo(l.start)} title="跳到这一句"
                        className={`shrink-0 text-[11px] tabular-nums px-1.5 py-0.5 rounded ${
                          on ? 'text-cyan-300 bg-cyan-400/10' : 'text-gray-500 hover:text-cyan-300 hover:bg-white/5'
                        }`}>
                  {fmt(l.start)}
                </button>

                <div className="flex flex-col gap-0.5 shrink-0">
                  <button onClick={() => nudge(i, 'start', -0.1)}
                          className="text-[9px] leading-none px-1 text-gray-600 hover:text-white">▲</button>
                  <button onClick={() => nudge(i, 'start', 0.1)}
                          className="text-[9px] leading-none px-1 text-gray-600 hover:text-white">▼</button>
                </div>

                <input value={l.text} onChange={(e) => patch(i, 'text', e.target.value)}
                       className="flex-1 min-w-0 bg-transparent text-sm text-white focus:outline-none focus:bg-white/5 rounded px-2 py-0.5" />

                <span className="shrink-0 text-[11px] tabular-nums text-gray-600">{fmt(l.end)}</span>
                <div className="flex flex-col gap-0.5 shrink-0">
                  <button onClick={() => nudge(i, 'end', -0.1)}
                          className="text-[9px] leading-none px-1 text-gray-600 hover:text-white">▲</button>
                  <button onClick={() => nudge(i, 'end', 0.1)}
                          className="text-[9px] leading-none px-1 text-gray-600 hover:text-white">▼</button>
                </div>

                <div className="shrink-0 flex gap-0.5 opacity-0 group-hover:opacity-100 transition">
                  <button onClick={() => insertAfter(i)} title="在下面插一句（识别漏词时用）"
                          className="p-1 rounded text-gray-500 hover:text-cyan-300 hover:bg-white/5">
                    <Plus className="w-3.5 h-3.5" />
                  </button>
                  <button onClick={() => removeLine(i)} title="删掉这一句"
                          className="p-1 rounded text-gray-500 hover:text-red-400 hover:bg-white/5">
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            )
          })}
        </div>

        {/* 镜头切分预览 */}
        {shots && (
          <div className="px-6 py-3 border-t border-white/5 max-h-44 overflow-y-auto">
            <div className="text-xs text-gray-400 mb-2">
              按当前歌词轴切出 <span className="text-cyan-300">{shots.length}</span> 个镜头
            </div>
            <div className="space-y-1">
              {shots.map((s: any) => (
                <div key={s.index} className="flex items-center gap-2 text-[11px]">
                  <span className="text-gray-600 w-8 shrink-0">#{s.index}</span>
                  <span className="text-gray-500 tabular-nums w-28 shrink-0">
                    {fmt(s.start)}–{fmt(s.end)}
                  </span>
                  <span className="text-cyan-400/70 tabular-nums w-12 shrink-0">{s.duration}s</span>
                  <span className="text-gray-600 w-16 shrink-0">{s.section}</span>
                  <span className="text-gray-400 truncate">
                    {(s.lyrics || []).join(' ｜ ') || '（器乐）'}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {importNote && (
          <div className="mx-6 mb-2 p-2.5 rounded-lg border border-cyan-500/25 bg-cyan-500/[0.05] text-xs text-cyan-300/90">
            {importNote}
          </div>
        )}

        {error && (
          <div className="mx-6 mb-2 p-2.5 rounded-lg border border-amber-500/30 bg-amber-500/[0.06] text-xs text-amber-300">
            {error}
          </div>
        )}

        {/* 底栏 */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-white/5">
          <div className="flex items-center gap-2">
            <button onClick={() => setImportOpen(true)}
                    className="px-3 py-1.5 rounded-lg text-xs text-cyan-300 border border-cyan-400/30 hover:bg-cyan-400/10 flex items-center gap-1.5">
              <Upload className="w-3.5 h-3.5" />
              导入歌词
            </button>
            <button onClick={preview} disabled={previewing}
                    className="px-3 py-1.5 rounded-lg text-xs text-gray-300 border border-white/10 hover:border-cyan-400/40 hover:text-cyan-300 flex items-center gap-1.5 disabled:opacity-50">
              {previewing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Scissors className="w-3.5 h-3.5" />}
              试算镜头切分
            </button>
            <button onClick={() => { setLines(initial.map((l) => ({ ...l }))); setShots(null); setError('') }}
                    disabled={!dirty}
                    className="px-3 py-1.5 rounded-lg text-xs text-gray-500 hover:text-gray-300 flex items-center gap-1.5 disabled:opacity-30">
              <RotateCcw className="w-3.5 h-3.5" />
              还原
            </button>
          </div>
          <div className="flex items-center gap-2">
            {dirty && <span className="text-xs text-amber-400/80">有未保存的修改</span>}
            <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm text-gray-400 hover:text-white hover:bg-white/5">
              取消
            </button>
            <button onClick={save} disabled={saving || !dirty}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-cyan-500/90 hover:bg-cyan-400 text-black disabled:opacity-40 flex items-center gap-2">
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              保存
            </button>
          </div>
        </div>
      </div>

      {importOpen && (
        <LyricImportPanel
          projectId={projectId}
          onClose={() => setImportOpen(false)}
          onImported={(newLines, info) => {
            setLines(newLines.map((l) => ({ ...l })))
            setShots(null); setError('')
            // 时间是怎么定出来的，逐条说清楚——用户才知道哪里最需要人工核对
            const how: string[] = []
            if (info?.phrases_detected) how.push(`${info.phrases_detected} 个人声乐句`)
            if (info?.anchors_used) how.push(`${info.anchors_used} 个识别锚点`)
            const bits = [`已导入 ${info?.count ?? newLines.length} 句`]
            if (info?.sections?.length) bits.push(`曲式按段落标记确定（${info.sections.length} 段）`)
            bits.push(how.length ? `时间由${how.join(' + ')}定位` : '⚠ 没能从音频定位，时间是均分的')
            // 导入接口是写完库才返回的，这里别再让用户去点「保存」——那按钮此刻本来就是灰的
            bits.push(info?.saved === false ? '记得点「保存」' : '已自动保存')
            setImportNote(bits.join(' · ') + '。逐句听一遍，有偏差就在上面直接改。')
          }}
        />
      )}
    </div>
  )
}
