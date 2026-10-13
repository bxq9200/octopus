/**
 * 系统设置模态弹窗
 *
 * 分类 tabs（LLM / 图片 / 视频）→ 每个 tab 是一个表单
 * 敏感字段：未填写时显示空 + 占位符；DB 里已有值时占位符变成"●●●●（已配置）"
 * 保存时只把"有改动的字段"提交给后端
 */
import React, { useEffect, useState } from 'react'
import { X, Brain, Image as ImageIcon, Video, CheckCircle2, AlertCircle, RotateCw, ExternalLink, Youtube, Music2, Music, Link2, Unplug, Megaphone, Cpu, Plus } from 'lucide-react'
import { BookOpen, KeyRound, Info } from 'lucide-react'
import AboutPanel from './AboutPanel'
import TopUpModal from '@/components/sidebar/TopUpModal'
import HintText from '@/components/common/HintText'
import { DOCS_BASE } from '@/components/wizard/SetupWizard'
import { useStore } from '@/store/useStore'
import { settingsAPI, publishersAPI, billingAPI, authAPI } from '@/services/api'

type FieldDef = {
  key: string
  label: string
  sensitive?: boolean
  kind?: 'string' | 'select'
  options?: { value: string; label: string }[]
  placeholder?: string
  hint?: string
  // 后端注的：该字段只在某 provider 选中时显示；留空 = 始终显示
  provider_for?: string
}

type FieldValue = {
  value: string
  has_value: boolean
  sensitive: boolean
}

type Category = {
  label: string
  icon?: string
  fields: FieldDef[]
  values: Record<string, FieldValue>
}

const ICON_MAP: Record<string, React.ComponentType<any>> = {
  brain: Brain,
  image: ImageIcon,
  video: Video,
  youtube: Youtube,
  tiktok: Music2,
  publish: Megaphone,
  music: Music,
  cpu: Cpu,
  info: Info,
}

// 单平台的"发布管理"切换：前端虚拟分类，下拉切换实际渲染的 publish_youtube / publish_tiktok
const PUBLISH_PLATFORMS: { value: string; label: string }[] = [
  { value: 'youtube', label: 'YouTube' },
  { value: 'tiktok', label: 'TikTok' },
]

type PublisherStatus = {
  platform: string
  label: string
  configured: boolean
  authorized: boolean
  user_label: string
  expires_at?: string
}

export default function SettingsModal() {
  const open = useStore((s) => s.settingsOpen)
  const setOpen = useStore((s) => s.setSettingsOpen)
  const [data, setData] = useState<Record<string, Category> | null>(null)
  // activeTab 可以是 'llm' / 'image' / 'video' / 'publish'（虚拟）
  const [activeTab, setActiveTab] = useState<string>('llm')
  // 当 activeTab === 'publish' 时，实际渲染的子分类（publish_youtube / publish_tiktok）
  const [publishPlatformValue, setPublishPlatformValue] = useState<string>('youtube')
  const [edits, setEdits] = useState<Record<string, Record<string, string>>>({})
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)
  const [publishers, setPublishers] = useState<PublisherStatus[]>([])
  const [oauthBusy, setOauthBusy] = useState<string | null>(null)
  // credits 余额（仅 cloud_client 模式有意义）
  const [credits, setCredits] = useState<number | null>(null)
  // 'local'=本地不计费 / 'ok'=已取到余额 / 'unauth'=云端但未登录/查询失败
  const [creditsState, setCreditsState] = useState<'local' | 'ok' | 'unauth'>('local')
  const [creditsLoading, setCreditsLoading] = useState(false)
  const [topUpOpen, setTopUpOpen] = useState(false)      // 账户 tab 的「支付宝充值」
  const [account, setAccount] = useState<{ email: string; id?: number } | null>(null)

  const refreshCredits = () => {
    setCreditsLoading(true)
    billingAPI.balance()
      .then(({ data }) => {
        if (data?.unlimited) { setCreditsState('local'); setCredits(null) }
        else { setCreditsState('ok'); setCredits(data?.credits ?? 0) }
        // 账号标识：云端随余额一起回；拿不到就退回登录时存的那份
        setAccount({ email: data?.email || localStorage.getItem('octopus_user') || '', id: data?.user_id })
      })
      .catch(() => { setCreditsState('unauth'); setCredits(null) })   // 云端但没登录/查不到
      .finally(() => setCreditsLoading(false))
  }
  useEffect(() => {
    if (open && activeTab === 'deploy') refreshCredits()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, activeTab])

  const reloadAll = async () => {
    const [s, p] = await Promise.all([
      settingsAPI.getAll().catch(() => null),
      publishersAPI.list().catch(() => null),
    ])
    if (s) setData(s.data)
    if (p) setPublishers(p.data?.platforms || [])
  }

  useEffect(() => {
    if (!open) return
    reloadAll().catch((err) => console.warn('加载配置失败', err))
    setEdits({})
    setTestResult(null)
  }, [open])

  if (!open) return null

  const cats = data ? Object.entries(data) : []
  // 侧栏菜单：把 publish_* 折叠成单个"发布管理"虚拟项
  const sidebarItems: { key: string; label: string; iconKey: string }[] = []
  for (const [k, meta] of cats) {
    if (k.startsWith('publish_')) continue
    sidebarItems.push({ key: k, label: meta.label, iconKey: meta.icon || '' })
  }
  if (cats.some(([k]) => k.startsWith('publish_'))) {
    sidebarItems.push({ key: 'publish', label: '发布管理', iconKey: 'publish' })
  }
  // 「关于」是前端虚拟分类（版本/升级/退出，没有后端字段）
  sidebarItems.push({ key: 'about', label: '关于与升级', iconKey: 'info' })
  // 云端化排序：账户(deploy) 置顶，然后 LLM/图片/音频/发布。video/storage 云端模式后端已不下发。
  const _order = ['deploy', 'llm', 'image', 'comfyui', 'audio', 'publish', 'video', 'storage', 'about']
  sidebarItems.sort((a, b) => {
    const ia = _order.indexOf(a.key); const ib = _order.indexOf(b.key)
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
  })

  // 实际拉数据的分类：activeTab='publish' 时映射到具体子分类
  const effectiveTab = activeTab === 'publish' ? `publish_${publishPlatformValue}` : activeTab
  const currentCat = data?.[effectiveTab]

  const handleEdit = (cat: string, key: string, value: string) => {
    setEdits((prev) => ({
      ...prev,
      [cat]: { ...(prev[cat] || {}), [key]: value },
    }))
  }

  const handleSave = async () => {
    if (!data) return
    setSaving(true)
    try {
      for (const [cat, fields] of Object.entries(edits)) {
        if (Object.keys(fields).length === 0) continue
        const payload: Record<string, { value: string; sensitive?: boolean }> = {}
        for (const [k, v] of Object.entries(fields)) {
          payload[k] = { value: v }
        }
        await settingsAPI.updateCategory(cat, payload)
      }
      // 重新拉一次最新值
      const res = await settingsAPI.getAll()
      setData(res.data)
      setEdits({})
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (cat: string, provider: string) => {
    setTesting(`${cat}:${provider}`)
    setTestResult(null)
    try {
      const res = await settingsAPI.testProvider(cat, provider)
      if (res.data?.ok) {
        setTestResult({ ok: true, msg: res.data.reply ? `✓ 连接正常（${res.data.model}）` : `✓ 已配置（${res.data.model || provider}）` })
      } else {
        setTestResult({ ok: false, msg: '✗ ' + (res.data?.error || '未知错误') })
      }
    } catch (err) {
      setTestResult({ ok: false, msg: '✗ ' + (err as Error).message })
    } finally {
      setTesting(null)
    }
  }

  const dirtyCount = Object.values(edits).reduce((sum, f) => sum + Object.keys(f).length, 0)

  // 当前 tab 是否是发布平台，对应的 publisher status
  // 兼容旧 activeTab='publish_xxx' 写法和新的 activeTab='publish' + publishPlatformValue
  const publishPlatform = activeTab === 'publish'
    ? publishPlatformValue
    : activeTab.startsWith('publish_')
      ? activeTab.replace('publish_', '')
      : null
  const currentPub = publishPlatform ? publishers.find((p) => p.platform === publishPlatform) : null

  const handleConnect = async () => {
    if (!publishPlatform) return
    // 关键：必须在 onClick 同步路径里同步打开窗口，否则浏览器会拦截 popup。
    // 先开一个 about:blank 占位，await 后再把真实 URL 赋给它。
    const win = window.open('about:blank', '_blank', 'width=720,height=820')
    if (!win) {
      setTestResult({ ok: false, msg: '浏览器拦截了弹窗 — 请允许 localhost:5173 弹窗后重试' })
      return
    }
    setOauthBusy(publishPlatform)
    try {
      if (dirtyCount > 0) await handleSave()
      const res = await publishersAPI.getAuthUrl(publishPlatform)
      const url = res.data?.auth_url
      if (!url) {
        try { win.close() } catch {}
        setTestResult({ ok: false, msg: '获取授权 URL 失败（未拿到 auth_url）' })
        setOauthBusy(null)
        return
      }
      win.location.href = url

      const intv = window.setInterval(async () => {
        try {
          const r = await publishersAPI.list()
          setPublishers(r.data?.platforms || [])
          const fresh = (r.data?.platforms || []).find((p: PublisherStatus) => p.platform === publishPlatform)
          if (fresh?.authorized) {
            window.clearInterval(intv)
            setOauthBusy(null)
            setTestResult({ ok: true, msg: `已连接 ${publishPlatform}:${fresh.user_label || '账号已授权'}` })
            try { win.close() } catch {}
          }
        } catch {}
      }, 2000)
      window.setTimeout(() => { window.clearInterval(intv); setOauthBusy(null) }, 5 * 60 * 1000)
    } catch (e: any) {
      try { win.close() } catch {}
      setOauthBusy(null)
      // 尽量把后端 detail 也带出来
      const detail = e?.response?.data?.detail || e?.message || String(e)
      setTestResult({ ok: false, msg: '连接失败：' + detail })
    }
  }

  const handleRevoke = async () => {
    if (!publishPlatform) return
    if (!window.confirm(`确定要撤销 ${publishPlatform} 的授权吗？需要重新连接才能继续发布。`)) return
    try {
      await publishersAPI.revoke(publishPlatform)
      await reloadAll()
      setTestResult({ ok: true, msg: `已撤销 ${publishPlatform} 授权` })
    } catch (e) {
      setTestResult({ ok: false, msg: '撤销失败：' + (e as Error).message })
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={() => setOpen(false)}
    >
      <div
        className="w-full max-w-3xl max-h-[85vh] bg-octopus-panel border border-octopus-border rounded-2xl shadow-2xl flex overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 左侧分类导航 */}
        <div className="w-44 bg-octopus-bg/50 border-r border-octopus-border flex flex-col">
          <div className="px-4 py-3 border-b border-octopus-border">
            <div className="text-sm font-semibold text-octopus-text">系统设置</div>
            <div className="text-[10px] text-octopus-muted mt-0.5">本地配置 · 加密存储</div>
          </div>
          <div className="flex-1 p-2 space-y-1">
            {sidebarItems.map((item) => {
              const Icon = ICON_MAP[item.iconKey] || Brain
              return (
                <button
                  key={item.key}
                  onClick={() => { setActiveTab(item.key); setTestResult(null) }}
                  className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors ${
                    activeTab === item.key
                      ? 'bg-octopus-primary/20 text-octopus-menu'
                      : 'text-octopus-menu hover:bg-octopus-border'
                  }`}
                >
                  <Icon className="w-4 h-4" />
                  <span className="truncate">{item.label}</span>
                </button>
              )
            })}

            {/* 帮助入口 —— 小白最需要的两条路：怎么用 / 去哪拿 key（都是官网图文，可更新不用发版） */}
            <div className="pt-2 mt-2 border-t border-octopus-border space-y-0.5">
              <button
                onClick={() => window.dispatchEvent(new Event('octopus:open-welcome'))}
                className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-octopus-menu hover:bg-octopus-border transition-colors"
              >
                <BookOpen className="w-4 h-4" />
                <span className="truncate">新手引导</span>
              </button>
              <a
                href={`${DOCS_BASE}/guide.md`} target="_blank" rel="noreferrer"
                className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-octopus-menu hover:bg-octopus-border transition-colors"
              >
                <BookOpen className="w-4 h-4" />
                <span className="truncate flex-1 text-left">操作说明</span>
                <ExternalLink className="w-3 h-3 opacity-50" />
              </a>
              <a
                href={`${DOCS_BASE}/api-keys.md`} target="_blank" rel="noreferrer"
                className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-octopus-menu hover:bg-octopus-border transition-colors"
              >
                <KeyRound className="w-4 h-4 shrink-0" />
                <span className="flex-1 text-left whitespace-nowrap">获取 API Key</span>
                <ExternalLink className="w-3 h-3 opacity-50 shrink-0" />
              </a>
            </div>
          </div>
        </div>

        {/* 右侧表单 */}
        <div className="flex-1 flex flex-col min-h-0">
          <div className="flex items-center justify-between px-5 py-3 border-b border-octopus-border">
            <div className="text-sm font-semibold text-octopus-text">
              {activeTab === 'about' ? '关于与升级' : (currentCat?.label || '')}
            </div>
            <button
              onClick={() => setOpen(false)}
              className="w-7 h-7 flex items-center justify-center rounded hover:bg-white/10"
            >
              <X className="w-4 h-4 text-octopus-muted" />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto p-5 space-y-3">
            {/* 关于与升级：版本 / 检查更新 / 一键升级 / 退出应用（纯前端虚拟分类，无后端字段） */}
            {activeTab === 'about' && <AboutPanel />}

            {/* 部署模式：credits 余额 + 充值说明 */}
            {activeTab === 'deploy' && (
              <div className="mb-2 p-4 rounded border border-cyan-400/25 bg-cyan-400/5">
                {/* 当前登录账号 —— 多账号时一眼看清是谁，报问题时也方便提供 */}
                {creditsState !== 'local' && (
                  <div className="mb-3 pb-3 border-b border-white/10">
                    <div className="text-[11px] text-octopus-muted">当前账号</div>
                    <div className="flex items-center gap-2 mt-1">
                      <span className="text-[13px] text-octopus-text font-mono truncate">
                        {account?.email || (creditsState === 'unauth' ? '未登录' : '…')}
                      </span>
                      {account?.id != null && (
                        <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-white/10 text-octopus-muted font-mono">
                          ID {account.id}
                        </span>
                      )}
                    </div>
                  </div>
                )}
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-[11px] text-octopus-muted">云端 credits 余额</div>
                    <div className={`text-2xl font-semibold mt-0.5 ${creditsState === 'unauth' ? 'text-yellow-400' : 'text-cyan-300'}`}>
                      {creditsLoading ? '…'
                        : creditsState === 'local' ? '本地模式 · 不计费'
                        : creditsState === 'unauth' ? '未登录 · 请退出重新登录'
                        : `${credits} credits`}
                    </div>
                    {creditsState === 'ok' && credits !== null && (
                      <div className="text-[10px] text-octopus-muted mt-1">
                        约 ¥{(credits / 100).toFixed(2)}（1 credit = 0.01 元；视频 0.8 元/秒）
                      </div>
                    )}
                    {creditsState === 'unauth' && (
                      <div className="text-[10px] text-yellow-400/80 mt-1">
                        云端模式已开，但登录态失效 → 下方「退出登录」再用邮箱登录一次
                      </div>
                    )}
                  </div>
                  <button
                    onClick={refreshCredits}
                    className="px-3 py-1.5 rounded border border-white/15 text-xs text-octopus-muted hover:text-octopus-text hover:border-white/30 transition-colors flex-shrink-0"
                  >
                    <RotateCw className="w-3.5 h-3.5" />
                  </button>
                </div>
                {creditsState === 'ok' && (
                  <div className="mt-3 pt-3 border-t border-white/10 flex items-center justify-between gap-3">
                    <div className="text-[11px] text-octopus-muted leading-relaxed">
                      视频生成按秒计费（0.8 元/秒）；大模型/图片用你自己的 key，不消耗 credits。
                    </div>
                    <button
                      onClick={() => setTopUpOpen(true)}
                      className="shrink-0 px-3.5 py-1.5 rounded-lg bg-cyan-500/20 border border-cyan-400/50 text-cyan-300 text-xs hover:bg-cyan-500/30 transition-colors flex items-center gap-1"
                    >
                      <Plus className="w-3 h-3" />支付宝充值
                    </button>
                  </div>
                )}
                {/* 退出登录 —— 切换账号 / 从本地登录切到云端邮箱登录都靠它 */}
                <div className="mt-3 pt-3 border-t border-white/10 flex items-center justify-between">
                  <span className="text-[11px] text-octopus-muted">
                    {creditsState === 'local' ? '本地模式：切到云端后请退出重新用邮箱登录' : '切换账号 / 登录态失效请退出重登'}
                  </span>
                  <button
                    onClick={async () => {
                      try { await authAPI.logout() } catch { /* 忽略，照样清本地 */ }
                      localStorage.removeItem('octopus_token')
                      localStorage.removeItem('octopus_user')
                      window.location.reload()   // 回到登录页；云端模式会显示邮箱验证码登录
                    }}
                    className="px-3 py-1.5 rounded border border-red-400/30 text-xs text-red-300/90 hover:bg-red-400/10 hover:border-red-400/60 transition-colors flex-shrink-0"
                  >
                    退出登录
                  </button>
                </div>
              </div>
            )}

            {/* 发布平台：当前授权状态面板 */}
            {publishPlatform && currentPub && (
              <div className="mb-2 p-3 rounded border border-octopus-border bg-black/30">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2 min-w-0">
                    {currentPub.authorized ? (
                      <CheckCircle2 className="w-4 h-4 text-green-400 flex-shrink-0" />
                    ) : currentPub.configured ? (
                      <Link2 className="w-4 h-4 text-yellow-400 flex-shrink-0" />
                    ) : (
                      <AlertCircle className="w-4 h-4 text-octopus-muted flex-shrink-0" />
                    )}
                    <div className="text-xs text-octopus-text truncate">
                      {currentPub.authorized
                        ? `已连接 · ${currentPub.user_label || '账号已授权'}`
                        : currentPub.configured
                        ? '已填凭证，未授权'
                        : '尚未配置开发者凭证'}
                    </div>
                  </div>
                  <div className="flex gap-2 flex-shrink-0">
                    {currentPub.authorized ? (
                      <button
                        onClick={handleRevoke}
                        className="px-2.5 py-1 rounded border border-octopus-border text-octopus-muted hover:text-red-300 hover:border-red-400/50 text-[11px] flex items-center gap-1"
                      >
                        <Unplug className="w-3 h-3" />
                        撤销
                      </button>
                    ) : (() => {
                      // 按钮可点条件：后端已 configured，或当前 edits 里至少塞了 client_id + client_secret
                      const editFields = edits[effectiveTab] || {}
                      const hasDraftCreds =
                        !!(editFields.client_id?.trim()) &&
                        !!(editFields.client_secret?.trim() || currentPub.user_label /* 已有保存 */)
                      const canClick = currentPub.configured || hasDraftCreds || !!editFields.client_id
                      const reasonHint = !canClick
                        ? '请先填写 Client ID 和 Client Secret'
                        : (oauthBusy === publishPlatform ? '等待授权回调...' : '点击后将新开窗口跳转到授权页')
                      return (
                      <button
                        onClick={handleConnect}
                        disabled={!canClick || oauthBusy === publishPlatform}
                        title={reasonHint}
                        className="px-2.5 py-1 rounded bg-cyan-400/15 border border-cyan-400/40 text-cyan-200 hover:bg-cyan-400/25 text-[11px] flex items-center gap-1 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        {oauthBusy === publishPlatform ? (
                          <><RotateCw className="w-3 h-3 animate-spin" />等待授权...</>
                        ) : (
                          <><Link2 className="w-3 h-3" />连接 {publishPlatform}</>
                        )}
                      </button>
                      )
                    })()}
                  </div>
                </div>
              </div>
            )}

            {/* 发布管理虚拟分类：顶部平台切换 */}
            {activeTab === 'publish' && (
              <div className="space-y-1 mb-1">
                <label className="text-xs text-octopus-text">发布平台</label>
                <select
                  value={publishPlatformValue}
                  onChange={(e) => { setPublishPlatformValue(e.target.value); setTestResult(null) }}
                  className="w-full px-2 py-1.5 rounded border border-octopus-border bg-octopus-bg text-octopus-text text-xs"
                >
                  {PUBLISH_PLATFORMS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
                <div className="text-[10px] text-octopus-muted">两个平台凭证独立保存，切换不会丢失另一个平台的设置</div>
              </div>
            )}

            {(() => {
              // 当前 provider 值（来自该 category 自带的 provider 字段；无则 ''）
              const providerField = currentCat?.fields.find((x) => x.key === 'provider')
              const providerValue = providerField
                ? (edits[effectiveTab]?.provider
                    ?? currentCat?.values.provider?.value
                    ?? providerField.options?.[0]?.value
                    ?? '')
                : ''
              const visibleFields = (currentCat?.fields || []).filter((f) =>
                !f.provider_for || f.provider_for === providerValue
              )
              return visibleFields.map((f) => {
                const current = currentCat!.values[f.key]
                const editing = edits[effectiveTab]?.[f.key]
                const displayValue = editing !== undefined ? editing : (current?.value || '')

                // 占位符：敏感字段已有值 → ●●●●（已配置）
                const placeholder = f.sensitive && current?.has_value && !editing
                  ? '●●●●●●●● （已配置，留空保持不变）'
                  : f.placeholder || ''

                return (
                  <div key={f.key} className="space-y-1">
                    <label className="text-xs text-octopus-text flex items-center gap-1">
                      {f.label}
                      {f.sensitive && (
                        <span className="text-[9px] text-yellow-400/70">🔐</span>
                      )}
                      {current?.has_value && !f.sensitive && (
                        <CheckCircle2 className="w-3 h-3 text-green-500" />
                      )}
                    </label>

                    {f.kind === 'select' ? (
                      <select
                        value={displayValue || (f.options?.[0]?.value ?? '')}
                        onChange={(e) => { handleEdit(effectiveTab, f.key, e.target.value); if (f.key === 'provider') setTestResult(null) }}
                        className="w-full px-2 py-1.5 rounded border border-octopus-border bg-octopus-bg text-octopus-text text-xs"
                      >
                        {f.options?.map((o) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </select>
                    ) : (
                      <input
                        type={f.sensitive ? 'password' : 'text'}
                        value={displayValue}
                        placeholder={placeholder}
                        onChange={(e) => handleEdit(effectiveTab, f.key, e.target.value)}
                        autoComplete="off"
                        className="w-full px-2 py-1.5 rounded border border-octopus-border bg-octopus-bg text-octopus-text text-xs font-mono"
                      />
                    )}

                    {f.hint && (
                      <div className="text-[10.5px] text-octopus-muted leading-relaxed whitespace-pre-line">
                        <HintText text={f.hint} />
                      </div>
                    )}
                  </div>
                )
              })
            })()}

            {/* 测试连接 */}
            {activeTab === 'llm' && (
              <TestRow
                label="测试 LLM 连接"
                onTest={() => {
                  const provider = (edits.llm?.provider) || currentCat?.values.provider?.value || 'kimi'
                  return handleTest('llm', provider)
                }}
                testing={testing?.startsWith('llm:') || false}
              />
            )}
            {activeTab === 'image' && (
              <TestRow
                label="测试图片生成"
                onTest={() => {
                  const provider = (edits.image?.provider) || currentCat?.values.provider?.value || 'zhipu'
                  return handleTest('image', provider)
                }}
                testing={testing?.startsWith('image:') || false}
              />
            )}
            {activeTab === 'video' && (
              <TestRow
                label="测试视频生成"
                onTest={() => {
                  const provider = (edits.video?.provider) || currentCat?.values.provider?.value || 'ark'
                  return handleTest('video', provider)
                }}
                testing={testing?.startsWith('video:') || false}
              />
            )}
            {activeTab === 'audio' && (
              <TestRow
                label="测试音频生成"
                onTest={() => {
                  const provider = (edits.audio?.provider) || currentCat?.values.provider?.value || 'elevenlabs'
                  return handleTest('audio', provider)
                }}
                testing={testing?.startsWith('audio:') || false}
              />
            )}
            {activeTab === 'comfyui' && (
              <TestRow
                label="测试 ComfyUI 连接"
                onTest={() => handleTest('comfyui', 'comfyui')}
                testing={testing?.startsWith('comfyui:') || false}
              />
            )}
            {publishPlatform && !currentPub?.configured && (
              <div className="text-[10px] text-octopus-muted mt-2">
                填好 client_id / client_secret 并保存后，点上方"连接 {publishPlatform}"按钮跳转授权页。
              </div>
            )}

            {testResult && (
              <div
                className={`mt-2 p-2 rounded text-xs flex items-start gap-2 ${
                  testResult.ok
                    ? 'bg-green-900/20 border border-green-500/30 text-green-300'
                    : 'bg-red-900/20 border border-red-500/30 text-red-300'
                }`}
              >
                {testResult.ok ? <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" /> : <AlertCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />}
                <span className="break-all">{testResult.msg}</span>
              </div>
            )}
          </div>

          {/* footer */}
          <div className="px-5 py-3 border-t border-octopus-border flex items-center justify-between">
            <div className="text-[11px] text-octopus-muted">
              {dirtyCount > 0 ? `${dirtyCount} 个字段待保存` : '无未保存修改'}
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => setOpen(false)}
                className="px-3 py-1.5 rounded border border-octopus-border text-octopus-text text-xs hover:bg-white/5"
              >
                关闭
              </button>
              <button
                onClick={handleSave}
                disabled={saving || dirtyCount === 0}
                className="px-3 py-1.5 rounded bg-octopus-primary hover:bg-octopus-primary/80 disabled:opacity-40 text-white text-xs"
              >
                {saving ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        </div>
      </div>
      {/* 充值弹窗（z-9998，盖在设置面板之上）；关闭后刷一次余额 */}
      <TopUpModal open={topUpOpen} onClose={() => { setTopUpOpen(false); refreshCredits() }} />
    </div>
  )
}

function TestRow({
  label, onTest, testing, inline,
}: {
  label: string
  onTest: () => void
  testing: boolean
  inline?: boolean
}) {
  return (
    <button
      onClick={onTest}
      disabled={testing}
      className={`${inline ? '' : 'w-full'} flex items-center justify-center gap-1.5 px-3 py-1.5 rounded border border-octopus-border text-xs text-octopus-text hover:bg-white/5 disabled:opacity-40`}
    >
      <RotateCw className={`w-3 h-3 ${testing ? 'animate-spin' : ''}`} />
      {testing ? '测试中...' : label}
    </button>
  )
}
