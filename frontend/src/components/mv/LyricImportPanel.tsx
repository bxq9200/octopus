/**
 * 导入歌词（1.1.0 MV）
 *
 * 🔴 为什么这是主路径而不是补充：
 *    语音识别对着伴奏唱歌只能听出三成、还带同音错字（「风儿拍拍手」→「老师点点头」），
 *    而创作者手里本来就有自己的歌词。所以：**文字你给，时间系统算**。
 *
 *    时间怎么算：先从人声轨检测「哪一刻在唱」（准确率九成），
 *    再拿识别结果里对得上的句子当锚点把歌词钉在正确位置，中间按乐句铺开。
 *
 *    Suno 这类生成的歌词还自带 [Verse]/[Chorus] 标记，那等于曲式结构也是现成的，
 *    直接拿去驱动「副歌切得密、主歌用长镜」，不用模型猜。
 */
import React, { useMemo, useRef, useState } from 'react'
import { X, Loader2, FileText, Clock, Upload, Check } from 'lucide-react'
import { mvAPI } from '@/services/api'

interface Props {
  projectId: string
  onClose: () => void
  onImported: (lines: { start: number; end: number; text: string }[], info: any) => void
}

type Fmt = 'text' | 'lrc'

const SECTION_HINT = ['verse', 'chorus', 'pre-chorus', 'bridge', 'intro', 'outro',
                      '主歌', '副歌', '桥段']

export default function LyricImportPanel({ projectId, onClose, onImported }: Props) {
  const [fmt, setFmt] = useState<Fmt>('text')
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  // 实时给点反馈：数出多少行、认出几个段落标记
  const stats = useMemo(() => {
    const raw = content.split('\n').map((s) => s.trim()).filter(Boolean)
    const marks = raw.filter((s) => {
      const inner = s.replace(/^[\[\(【（]|[\]\)】）:：]+$/g, '').trim().toLowerCase()
      return /^[\[\(【（]/.test(s) && SECTION_HINT.some((h) => inner.startsWith(h))
    })
    const lrcTags = raw.filter((s) => /^\[\d{1,3}:\d{1,2}/.test(s))
    return { lines: raw.length - marks.length, marks: marks.length, lrcTags: lrcTags.length }
  }, [content])

  const pickFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (!f) return
    const r = new FileReader()
    r.onload = () => {
      const text = String(r.result || '')
      setContent(text)
      // .lrc 或内容里有时间标签 → 自动切到 LRC 模式，省得用户自己判断
      if (/\.lrc$/i.test(f.name) || /^\[\d{1,3}:\d{1,2}/m.test(text)) setFmt('lrc')
    }
    r.readAsText(f, 'utf-8')
  }

  const submit = async () => {
    if (!content.trim()) { setError('先把歌词贴进来'); return }
    setBusy(true); setError('')
    try {
      const res = await mvAPI.importLyrics(projectId, { content, format: fmt })
      onImported(res.data?.lines || [], res.data)
      onClose()
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || '导入失败')
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[9998] flex items-center justify-center bg-black/80 backdrop-blur-sm p-4"
         onClick={onClose}>
      <div className="w-[min(94vw,760px)] max-h-[92vh] flex flex-col rounded-2xl border border-octopus-border bg-[#0d0d14] shadow-2xl"
           onClick={(e) => e.stopPropagation()}>

        <div className="flex items-start justify-between px-6 py-4 border-b border-white/5">
          <div>
            <div className="text-white font-medium">导入歌词</div>
            <div className="text-xs text-gray-500 mt-0.5 leading-relaxed">
              语音识别对着伴奏唱歌只能听出三成、还会认错字。<br />
              把你自己的歌词贴进来，时间轴由系统从人声里算 —— 这样文字和时间都是准的。
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-white/5 text-gray-400">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="px-6 py-4 space-y-3">
          <div className="flex gap-2">
            {([
              { v: 'text' as Fmt, icon: FileText, label: '纯文本歌词',
                desc: '一行一句，系统自动对时间' },
              { v: 'lrc' as Fmt, icon: Clock, label: 'LRC 文件',
                desc: '自带时间戳，直接用' },
            ]).map((o) => {
              const Icon = o.icon
              const on = fmt === o.v
              return (
                <button key={o.v} onClick={() => setFmt(o.v)}
                        className={`flex-1 text-left p-3 rounded-xl border transition ${
                          on ? 'border-cyan-400/50 bg-cyan-400/[0.07]' : 'border-white/8 bg-white/[0.02] hover:border-white/20'
                        }`}>
                  <div className={`flex items-center gap-2 ${on ? 'text-cyan-300' : 'text-gray-300'}`}>
                    <Icon className="w-4 h-4" />
                    <span className="text-sm font-medium">{o.label}</span>
                    {on && <Check className="w-3.5 h-3.5 ml-auto" />}
                  </div>
                  <div className="text-[11px] text-gray-500 mt-1">{o.desc}</div>
                </button>
              )
            })}
          </div>

          <div className="flex items-center justify-between">
            <button onClick={() => fileRef.current?.click()}
                    className="text-xs text-gray-400 hover:text-cyan-300 flex items-center gap-1.5">
              <Upload className="w-3.5 h-3.5" />
              从文件读取（.txt / .lrc）
            </button>
            <div className="text-xs text-gray-600">
              {fmt === 'lrc'
                ? `识别到 ${stats.lrcTags} 个时间标签`
                : `${stats.lines} 行歌词${stats.marks > 0 ? ` · ${stats.marks} 个段落标记` : ''}`}
            </div>
          </div>
          <input ref={fileRef} type="file" hidden accept=".txt,.lrc,text/plain" onChange={pickFile} />

          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={14}
            spellCheck={false}
            placeholder={fmt === 'lrc'
              ? '[00:02.90]静静的小姑娘\n[00:06.40]背着小竹篮\n…'
              : '[Verse 1]\n金果小姑娘，背着小竹篮\n走进果园里，脚步轻又欢\n\n[Chorus]\n金果数果果，果果真多多\n…\n\n（Suno / AI 生成的歌词直接整段贴进来即可，\n  方括号里的 Verse / Chorus 标记会被识别成曲式结构，不会当成歌词）'}
            className="w-full px-3 py-2.5 rounded-xl bg-white/[0.03] border border-white/8 text-sm text-white placeholder:text-gray-600 focus:outline-none focus:border-cyan-400/40 resize-none font-mono leading-relaxed"
          />

          {stats.marks > 0 && fmt === 'text' && (
            <div className="p-2.5 rounded-lg border border-cyan-500/25 bg-cyan-500/[0.05] text-xs text-cyan-300/90">
              认出 {stats.marks} 个段落标记 —— 曲式结构会直接用它，
              副歌会切得比主歌密，不用模型去猜。
            </div>
          )}

          {error && (
            <div className="p-2.5 rounded-lg border border-red-500/30 bg-red-500/[0.06] text-xs text-red-300">
              {error}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between px-6 py-4 border-t border-white/5">
          <div className="text-xs text-gray-600">
            导入后会覆盖当前歌词轴，仍可以在校对界面逐句微调
          </div>
          <div className="flex gap-2">
            <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm text-gray-400 hover:text-white hover:bg-white/5">
              取消
            </button>
            <button onClick={submit} disabled={busy || !content.trim()}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-cyan-500/90 hover:bg-cyan-400 text-black disabled:opacity-40 flex items-center gap-2">
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
              导入并对齐
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
