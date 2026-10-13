import { useEffect, useState } from 'react'
import { X, MessageSquare, Palette, Clapperboard, Film, BookOpen, ArrowRight } from 'lucide-react'

const SEEN_KEY = 'octopus_welcome_seen_v1'
const DOCS = 'https://github.com/bxq9200/octopus/blob/main/docs'

const STEPS = [
  {
    icon: MessageSquare,
    title: '① 说一句话',
    body: '在左边聊天框描述你想拍什么，比如「拍个 2 岁小姑娘和小猫在客厅玩的温馨短片，30 秒，竖屏」。AI 导演会追问几句，聊清楚就开工。',
  },
  {
    icon: Palette,
    title: '② 确认美术',
    body: 'AI 会先出剧本，再画角色三视图、场景图、道具图。每张都能单独重跑到满意为止 —— 角色三视图尤其重要，后面所有镜头都靠它锁脸。',
  },
  {
    icon: Clapperboard,
    title: '③ 分镜 → 关键帧',
    body: '确认美术后自动出分镜表，再为每个镜头画首帧。连续镜头会自动用上一镜的尾帧续接，画面接得上。',
  },
  {
    icon: Film,
    title: '④ 出视频 → 合成',
    body: '逐镜生成视频（这一步消耗 credits），最后拼接配乐导出成片。所有文件都存在你自己电脑上。',
  },
]

/** 首次进入主界面的欢迎引导。localStorage 记一次，之后不再打扰。
 *  设置页的「使用帮助」可以随时手动再打开（派发 octopus:open-welcome）。 */
export default function WelcomeGuide() {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!localStorage.getItem(SEEN_KEY)) setOpen(true)
    const on = () => setOpen(true)
    window.addEventListener('octopus:open-welcome', on)
    return () => window.removeEventListener('octopus:open-welcome', on)
  }, [])

  const close = () => {
    localStorage.setItem(SEEN_KEY, '1')
    setOpen(false)
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-[9997] flex items-center justify-center bg-black/75 backdrop-blur-sm p-4"
      onClick={close}>
      <div className="w-[min(94vw,660px)] max-h-[90vh] overflow-y-auto rounded-2xl border border-octopus-border bg-[#0d0d14] shadow-2xl"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between px-6 pt-6 pb-3">
          <div>
            <div className="text-xl font-bold text-octopus-text">欢迎使用 Octopus 🐙</div>
            <div className="text-[13px] text-octopus-muted mt-1">
              用一句话生成一整集短剧。四步走完，就有成片。
            </div>
          </div>
          <button onClick={close} className="text-octopus-muted hover:text-octopus-text shrink-0 mt-1">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="px-6 pb-2 space-y-2.5">
          {STEPS.map((s) => {
            const Icon = s.icon
            return (
              <div key={s.title} className="flex gap-3 p-3 rounded-xl bg-white/[0.03] border border-white/5">
                <Icon className="w-4 h-4 text-cyan-400 shrink-0 mt-0.5" />
                <div className="min-w-0">
                  <div className="text-[13px] font-semibold text-octopus-text">{s.title}</div>
                  <div className="text-[12px] text-octopus-muted leading-relaxed mt-0.5">{s.body}</div>
                </div>
              </div>
            )
          })}
        </div>

        <div className="mx-6 my-3 p-3 rounded-xl border border-amber-500/30 bg-amber-500/[0.06]">
          <div className="text-[12.5px] text-amber-200 font-semibold">开始之前：先配好 API Key</div>
          <div className="text-[11.5px] text-amber-100/70 leading-relaxed mt-1">
            大模型、图片、音乐都用<b>你自己的账号</b>（按官方原价付费、没有中间加价）。
            只有视频生成用平台 credits。不知道去哪注册？下面有图文教程。
          </div>
        </div>

        <div className="px-6 pb-6 flex flex-wrap items-center gap-2.5">
          <a href={`${DOCS}/guide.md`} target="_blank" rel="noreferrer"
            className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-cyan-500/20 border border-cyan-400/50 text-cyan-300 text-[13px] hover:bg-cyan-500/30 transition-colors">
            <BookOpen className="w-3.5 h-3.5" />使用教程
          </a>
          <a href={`${DOCS}/api-keys.md`} target="_blank" rel="noreferrer"
            className="flex items-center gap-1.5 px-4 py-2 rounded-lg border border-octopus-border text-octopus-text text-[13px] hover:bg-white/5 transition-colors">
            怎么获取 API Key
          </a>
          <div className="flex-1" />
          <button onClick={close}
            className="flex items-center gap-1 px-4 py-2 rounded-lg bg-white/10 text-octopus-text text-[13px] hover:bg-white/15 transition-colors">
            开始创作 <ArrowRight className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>
  )
}
