import { useEffect, useState, useCallback, useRef } from 'react'
import { RefreshCw, Download, Power, CheckCircle2, AlertCircle, Loader2 } from 'lucide-react'
import { appAPI } from '@/services/api'

type Info = { name: string; version: string; packaged: boolean }
type Upd = { current: string; latest?: string; available: boolean; url?: string; notes?: string; reason?: string }

/** 设置页「关于」：版本 / 检查更新 / 一键升级 / 退出应用。
 *  退出很重要 —— 桌面版是「本地后端 + 浏览器界面」，关掉网页后端还活着：
 *  ①任务管理器一直挂着 ②装新版时 exe 被占用覆盖不了。升级流程会自动调它。 */
export default function AboutPanel() {
  const [info, setInfo] = useState<Info | null>(null)
  const [upd, setUpd] = useState<Upd | null>(null)
  const [checking, setChecking] = useState(false)
  const [dl, setDl] = useState<{ status: string; percent: number; error?: string } | null>(null)
  const [quitting, setQuitting] = useState(false)
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    appAPI.info().then((r) => setInfo(r.data)).catch(() => {})
    return () => { if (timer.current) clearInterval(timer.current) }
  }, [])

  const check = useCallback(async () => {
    setChecking(true)
    try { setUpd((await appAPI.checkUpdate()).data) } catch { /* 网络问题不打断 */ }
    setChecking(false)
  }, [])

  // 打开面板自动查一次
  useEffect(() => { check() }, [check])

  const startDownload = async () => {
    if (!upd?.url) return
    setDl({ status: 'downloading', percent: 0 })
    try {
      await appAPI.download(upd.url, upd.latest || '')
      timer.current = setInterval(async () => {
        try {
          const d = (await appAPI.progress()).data
          setDl(d)
          if (d.status === 'ready' || d.status === 'failed') {
            if (timer.current) clearInterval(timer.current)
          }
        } catch { /* 轮询失败下一轮再试 */ }
      }, 800)
    } catch (e: any) {
      setDl({ status: 'failed', percent: 0, error: e?.response?.data?.detail || '下载失败' })
    }
  }

  const install = async () => {
    try {
      await appAPI.install()
      setQuitting(true)
    } catch (e: any) {
      setDl({ status: 'failed', percent: 100, error: e?.response?.data?.detail || '启动安装失败' })
    }
  }

  const quit = async () => {
    if (!window.confirm('确定要退出 Octopus 吗？\n\n正在生成中的任务会中断。')) return
    setQuitting(true)
    try { await appAPI.quit() } catch { /* 进程已退出，请求断掉是正常的 */ }
  }

  if (quitting) {
    return (
      <div className="text-center py-14">
        <Power className="w-10 h-10 text-cyan-400 mx-auto mb-3" />
        <div className="text-base font-semibold text-octopus-text">Octopus 已退出</div>
        <div className="text-[12px] text-octopus-muted mt-2 leading-relaxed">
          这个页面可以关掉了。<br />
          需要时从桌面或开始菜单重新打开即可。
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {/* 版本 */}
      <div className="p-4 rounded-lg border border-octopus-border bg-black/20">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[11px] text-octopus-muted">当前版本</div>
            <div className="text-xl font-semibold text-octopus-text mt-0.5">
              v{info?.version || '…'}
            </div>
            {info && !info.packaged && (
              <div className="text-[10px] text-amber-400/80 mt-1">开发模式运行 —— 升级/退出不可用</div>
            )}
          </div>
          <button onClick={check} disabled={checking}
            className="px-3 py-1.5 rounded border border-white/15 text-xs text-octopus-muted hover:text-octopus-text hover:border-white/30 flex items-center gap-1.5 disabled:opacity-50">
            <RefreshCw className={`w-3.5 h-3.5 ${checking ? 'animate-spin' : ''}`} />
            检查更新
          </button>
        </div>

        {/* 更新状态 */}
        {upd && (
          <div className="mt-3 pt-3 border-t border-white/10">
            {!upd.available ? (
              <div className="flex items-center gap-1.5 text-[12px] text-octopus-muted">
                <CheckCircle2 className="w-3.5 h-3.5 text-green-500/70" />
                {upd.reason ? `暂时查不到更新（${upd.reason}）` : '已是最新版本'}
              </div>
            ) : (
              <div>
                <div className="flex items-center gap-1.5 text-[13px] text-cyan-300 font-semibold">
                  <Download className="w-3.5 h-3.5" />
                  发现新版本 v{upd.latest}
                </div>
                {upd.notes && (
                  <div className="text-[11.5px] text-octopus-muted mt-1.5 leading-relaxed">{upd.notes}</div>
                )}

                {/* 下载 / 安装 */}
                {!dl && (
                  <button onClick={startDownload} disabled={!info?.packaged}
                    className="mt-3 px-4 py-1.5 rounded-lg bg-cyan-500/20 border border-cyan-400/50 text-cyan-300 text-xs hover:bg-cyan-500/30 disabled:opacity-40 flex items-center gap-1.5">
                    <Download className="w-3.5 h-3.5" />下载并安装
                  </button>
                )}
                {dl?.status === 'downloading' && (
                  <div className="mt-3">
                    <div className="flex items-center gap-2 text-[12px] text-octopus-muted">
                      <Loader2 className="w-3.5 h-3.5 animate-spin text-cyan-400" />
                      下载中 {dl.percent}%
                    </div>
                    <div className="mt-1.5 h-1 rounded bg-white/10 overflow-hidden">
                      <div className="h-full bg-cyan-400 transition-all" style={{ width: `${dl.percent}%` }} />
                    </div>
                  </div>
                )}
                {dl?.status === 'ready' && (
                  <div className="mt-3">
                    <div className="text-[11.5px] text-octopus-muted leading-relaxed mb-2">
                      下载完成。点下面的按钮会<b className="text-octopus-text">自动安装并重启</b> ——
                      Octopus 会先退出（这样安装程序才能覆盖），装完自动打开新版本。
                    </div>
                    <button onClick={install}
                      className="px-4 py-1.5 rounded-lg bg-green-500/20 border border-green-400/50 text-green-300 text-xs hover:bg-green-500/30">
                      立即安装并重启
                    </button>
                  </div>
                )}
                {dl?.status === 'failed' && (
                  <div className="mt-3 flex items-start gap-1.5 text-[12px] text-red-400">
                    <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                    <span>{dl.error || '下载失败'}，可以去官网手动下载安装包。</span>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 退出应用 */}
      <div className="p-4 rounded-lg border border-octopus-border bg-black/20 flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[13px] text-octopus-text">退出 Octopus</div>
          <div className="text-[11px] text-octopus-muted mt-1 leading-relaxed">
            只关浏览器窗口的话，后台程序还在运行。要完全退出请点这里。
          </div>
        </div>
        <button onClick={quit} disabled={!info?.packaged}
          className="shrink-0 px-4 py-1.5 rounded-lg border border-red-500/40 text-red-300 text-xs hover:bg-red-500/15 disabled:opacity-40 flex items-center gap-1.5">
          <Power className="w-3.5 h-3.5" />退出应用
        </button>
      </div>
    </div>
  )
}
