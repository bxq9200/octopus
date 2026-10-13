/**
 * MV 生成配置面板（1.1.0）
 *
 * MV 的输入是「一首歌 + 几张风格图 + 一个类型」，不是对话——所以它不走聊天流，
 * 用这个面板一次收齐再开跑。
 */
import React, { useRef, useState } from 'react'
import { Music, ImagePlus, X, Loader2, Film, Sparkles, Play } from 'lucide-react'
import { mvAPI, projectAPI } from '@/services/api'
import { useStore } from '@/store/useStore'

interface Props {
  onClose: () => void
  onStarted: (projectId: string, result: any) => void
}

type MVType = 'narrative' | 'visual'

const TYPE_OPTIONS: { value: MVType; label: string; icon: React.ComponentType<any>; desc: string }[] = [
  {
    value: 'narrative', label: '叙事型', icon: Film,
    desc: '有具体人物和一条情感线索。会生成角色三视图并全片锁脸，适合有故事的歌。',
  },
  {
    value: 'visual', label: '意象型', icon: Sparkles,
    desc: '没有固定主角，靠场景、物件、光影承载情绪。适合氛围向、器乐比重大的歌。',
  },
]

// 常用风格预设。选了之后仍可以在下面手写补充，两者会一起喂给风格分析。
const STYLE_PRESETS = [
  '国风水墨，青灰调', '电影感写实，柔光浅景深', '赛博霓虹，高对比冷调',
  '胶片颗粒，暖黄复古', '日系动画，通透干净', '油画质感，厚涂笔触',
]

// MV 默认横屏。竖屏留给短视频平台。
const RATIOS = [
  { v: '16:9', label: '横屏 16:9', hint: 'YouTube / B站，MV 常用' },
  { v: '9:16', label: '竖屏 9:16', hint: '抖音 / 视频号' },
  { v: '1:1',  label: '方形 1:1',  hint: '社交平台' },
]

const fmtTime = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`

export default function MVSetupPanel({ onClose, onStarted }: Props) {
  const currentProject = useStore((s) => s.currentProject)
  const setCurrentProject = useStore((s) => s.setCurrentProject)

  const [mvType, setMvType] = useState<MVType>('narrative')
  const [ratio, setRatio] = useState('16:9')
  const [preset, setPreset] = useState('')
  const [brief, setBrief] = useState('')
  const [audio, setAudio] = useState<{ url: string; name: string; duration: number } | null>(null)
  const [refs, setRefs] = useState<{ url: string; name: string }[]>([])
  const [busy, setBusy] = useState<'' | 'audio' | 'refs' | 'start'>('')
  const [error, setError] = useState('')

  const audioInput = useRef<HTMLInputElement>(null)
  const refsInput = useRef<HTMLInputElement>(null)

  /** 上传需要先有项目——MV 的附件是挂在项目下的。没有就先建一个。 */
  const ensureProject = async (): Promise<string> => {
    if (currentProject?.id) return currentProject.id
    const created = await projectAPI.create({
      name: '未命名 MV',
      config: { mode: 'mv' },
    })
    const p = created.data
    setCurrentProject(p)
    return p.id
  }

  const pickAudio = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    e.target.value = ''             // 允许重复选同一个文件
    if (!f) return
    setError(''); setBusy('audio')
    try {
      const pid = await ensureProject()
      const res = await mvAPI.upload(pid, f)
      const a = (res.data?.attachments || [])[0]
      if (!a || a.kind !== 'audio') throw new Error('这个文件没被识别成音频，请换 mp3 / wav / m4a / flac')
      setAudio({ url: a.url, name: a.name, duration: a.duration || 0 })
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || '歌曲上传失败')
    } finally { setBusy('') }
  }

  const pickRefs = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || [])
    e.target.value = ''
    if (!files.length) return
    setError(''); setBusy('refs')
    try {
      const pid = await ensureProject()
      // 逐个传：一张失败不影响其它，也避开后端单次多图的计数限制
      const more: { url: string; name: string }[] = []
      for (const f of files.slice(0, 3)) {
        const r = await mvAPI.upload(pid, f)
        for (const a of (r.data?.attachments || [])) {
          if (a.kind === 'image') more.push({ url: a.url, name: a.name })
        }
      }
      if (!more.length) throw new Error('这些文件没被识别成图片，请用 png / jpg / webp')
      // 封顶 3 张：Seedream 多图融合槽位有限，多了风格反而更飘（后端也会再截一次）
      setRefs((prev) => [...prev, ...more].slice(0, 3))
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || '参考图上传失败')
    } finally { setBusy('') }
  }

  const start = async () => {
    if (!audio) { setError('请先上传一首歌'); return }
    setError(''); setBusy('start')
    try {
      const pid = await ensureProject()
      const res = await mvAPI.start(pid, {
        audio_path: audio.url,
        type: mvType,
        style_refs: refs.map((r) => r.url),
        style_preset: preset.trim(),
        user_brief: brief.trim(),
        aspect_ratio: ratio,
      })
      onStarted(pid, res.data)
      onClose()
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || '启动失败')
      setBusy('')
    }
  }

  return (
    <div className="fixed inset-0 z-[9996] flex items-center justify-center bg-black/75 backdrop-blur-sm p-4"
         onClick={onClose}>
      <div className="w-[min(94vw,720px)] max-h-[90vh] overflow-y-auto rounded-2xl border border-octopus-border bg-[#0d0d14] shadow-2xl"
           onClick={(e) => e.stopPropagation()}>

        <div className="flex items-center justify-between px-6 py-4 border-b border-white/5">
          <div className="flex items-center gap-2.5">
            <Music className="w-5 h-5 text-cyan-300" />
            <div>
              <div className="text-white font-medium">生成 MV</div>
              <div className="text-xs text-gray-500">系统会识别歌词与曲式，让画面卡着歌走</div>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-white/5 text-gray-400">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-6 space-y-6">
          {/* 1. 歌曲 */}
          <section>
            <div className="text-sm text-gray-300 mb-2">
              1. 上传歌曲 <span className="text-red-400">*</span>
              <span className="ml-2 text-xs text-gray-500">mp3 / wav / m4a / flac，≤ 80MB</span>
            </div>
            {audio ? (
              <div className="flex items-center gap-3 p-3 rounded-xl bg-white/[0.03] border border-white/5">
                <Music className="w-4 h-4 text-cyan-300 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-white truncate">{audio.name}</div>
                  {audio.duration > 0 && (
                    <div className="text-xs text-gray-500">时长 {fmtTime(audio.duration)}</div>
                  )}
                </div>
                <button onClick={() => setAudio(null)}
                        className="text-xs text-gray-400 hover:text-white px-2 py-1 rounded hover:bg-white/5">
                  换一首
                </button>
              </div>
            ) : (
              <button onClick={() => audioInput.current?.click()} disabled={busy === 'audio'}
                      className="w-full p-4 rounded-xl border border-dashed border-white/15 hover:border-cyan-400/40 hover:bg-white/[0.02] text-gray-400 hover:text-gray-200 text-sm flex items-center justify-center gap-2 disabled:opacity-50">
                {busy === 'audio' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Music className="w-4 h-4" />}
                {busy === 'audio' ? '上传中…' : '选择歌曲文件'}
              </button>
            )}
            <input ref={audioInput} type="file" hidden accept="audio/*,.mp3,.wav,.m4a,.flac,.ogg" onChange={pickAudio} />
          </section>

          {/* 2. 类型 */}
          <section>
            <div className="text-sm text-gray-300 mb-2">2. MV 类型</div>
            <div className="grid grid-cols-2 gap-3">
              {TYPE_OPTIONS.map((t) => {
                const Icon = t.icon
                const on = mvType === t.value
                return (
                  <button key={t.value} onClick={() => setMvType(t.value)}
                          className={`text-left p-3 rounded-xl border transition ${
                            on ? 'border-cyan-400/50 bg-cyan-400/[0.07]' : 'border-white/8 bg-white/[0.02] hover:border-white/20'
                          }`}>
                    <div className={`flex items-center gap-2 mb-1 ${on ? 'text-cyan-300' : 'text-gray-300'}`}>
                      <Icon className="w-4 h-4" />
                      <span className="text-sm font-medium">{t.label}</span>
                    </div>
                    <div className="text-xs text-gray-500 leading-relaxed">{t.desc}</div>
                  </button>
                )
              })}
            </div>
          </section>

          {/* 2.5 画幅 */}
          <section>
            <div className="text-sm text-gray-300 mb-2">3. 画幅</div>
            <div className="flex gap-2">
              {RATIOS.map((r) => {
                const on = ratio === r.v
                return (
                  <button key={r.v} onClick={() => setRatio(r.v)}
                          className={`flex-1 p-2.5 rounded-xl border text-left transition ${
                            on ? 'border-cyan-400/50 bg-cyan-400/[0.07]' : 'border-white/8 bg-white/[0.02] hover:border-white/20'
                          }`}>
                    <div className={`text-sm ${on ? 'text-cyan-300' : 'text-gray-300'}`}>{r.label}</div>
                    <div className="text-[11px] text-gray-500 mt-0.5">{r.hint}</div>
                  </button>
                )
              })}
            </div>
          </section>

          {/* 4. 风格 */}
          <section>
            <div className="text-sm text-gray-300 mb-2">
              4. 视觉风格
              <span className="ml-2 text-xs text-gray-500">参考图和文字描述都会用上，可以只填一个</span>
            </div>

            <div className="flex flex-wrap gap-2 mb-3">
              {STYLE_PRESETS.map((p) => (
                <button key={p} onClick={() => setPreset(preset === p ? '' : p)}
                        className={`text-xs px-2.5 py-1 rounded-lg border transition ${
                          preset === p ? 'border-cyan-400/50 bg-cyan-400/10 text-cyan-300'
                                       : 'border-white/8 bg-white/[0.02] text-gray-400 hover:text-gray-200 hover:border-white/20'
                        }`}>
                  {p}
                </button>
              ))}
            </div>

            <input value={preset} onChange={(e) => setPreset(e.target.value)}
                   placeholder="也可以自己写，例如：黑白胶片，粗颗粒，强逆光"
                   className="w-full px-3 py-2 mb-3 rounded-lg bg-white/[0.03] border border-white/8 text-sm text-white placeholder:text-gray-600 focus:outline-none focus:border-cyan-400/40" />

            <div className="flex flex-wrap gap-2">
              {refs.map((r, i) => (
                <div key={r.url} className="relative group">
                  <img src={r.url} alt={r.name}
                       className="w-20 h-20 object-cover rounded-lg border border-white/10" />
                  <button onClick={() => setRefs(refs.filter((_, k) => k !== i))}
                          className="absolute -top-1.5 -right-1.5 p-0.5 rounded-full bg-black/80 border border-white/20 text-gray-300 opacity-0 group-hover:opacity-100 transition">
                    <X className="w-3 h-3" />
                  </button>
                </div>
              ))}
              {refs.length < 3 && (
                <button onClick={() => refsInput.current?.click()} disabled={busy === 'refs'}
                        className="w-20 h-20 rounded-lg border border-dashed border-white/15 hover:border-cyan-400/40 text-gray-500 hover:text-gray-300 flex flex-col items-center justify-center gap-1 disabled:opacity-50">
                  {busy === 'refs' ? <Loader2 className="w-4 h-4 animate-spin" /> : <ImagePlus className="w-4 h-4" />}
                  <span className="text-[10px]">参考图</span>
                </button>
              )}
            </div>
            {/* 隐藏的文件输入 —— 上面那个按钮靠 refsInput.current.click() 触发，缺了它点了没反应 */}
            <input ref={refsInput} type="file" hidden multiple
                   accept="image/png,image/jpeg,image/jpg,image/webp,.png,.jpg,.jpeg,.webp"
                   onChange={pickRefs} />
            <div className="text-xs text-gray-600 mt-2">
              最多 3 张。参考图只用来定画风（笔触/色调/光感），画面内容以剧本为准，不会把参考图里的人或物画进来。
            </div>
          </section>

          {/* 4. 补充要求 */}
          <section>
            <div className="text-sm text-gray-300 mb-2">
              5. 补充要求 <span className="text-xs text-gray-500">（选填）</span>
            </div>
            <textarea value={brief} onChange={(e) => setBrief(e.target.value)} rows={2}
                      placeholder="例如：主角是个骑摩托的女生；不要出现人脸；结尾要回到开头那个场景"
                      className="w-full px-3 py-2 rounded-lg bg-white/[0.03] border border-white/8 text-sm text-white placeholder:text-gray-600 focus:outline-none focus:border-cyan-400/40 resize-none" />
          </section>

          {error && (
            <div className="p-3 rounded-xl border border-red-500/30 bg-red-500/[0.06] text-sm text-red-300">
              {error}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between px-6 py-4 border-t border-white/5">
          <div className="text-xs text-gray-600">
            歌曲越长解析越久，4 分钟的歌约 20 秒
          </div>
          <div className="flex gap-2">
            <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm text-gray-400 hover:text-white hover:bg-white/5">
              取消
            </button>
            <button onClick={start} disabled={!audio || busy === 'start'}
                    className="px-4 py-2 rounded-lg text-sm font-medium bg-cyan-500/90 hover:bg-cyan-400 text-black disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-2">
              {busy === 'start' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              开始生成
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
