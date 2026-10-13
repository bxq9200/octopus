/**
 * 0.7.2 资产管理面板（剧集级）—— 资产从属剧集，跨 master + 所有分集聚合展示。
 * 结构：👗 角色 → 基础设定（master 形象/三视图/prompt）→ 第N集 → 该集服装；
 *      🎁 道具 → master 基础 → 各集状态。
 * 操作均按目标集的 project_id 调用：生成/重跑=nodeAction rerun、删除=deleteNode cascade（警告级联删
 * 引用它的关键帧/视频）、新增=asset-action（create_outfit/create_prop_state 需选目标集；create_prop 落 master）。
 * 增删后需在对应集重跑分镜刷新 @ 引用。打开方式：setAssetManagerSeriesId(seriesId)。
 */
import React, { useEffect, useMemo, useState } from 'react'
import { X, Shirt, RefreshCw, Trash2, Plus, AlertTriangle, Loader2 } from 'lucide-react'
import { useStore } from '@/store/useStore'
import { workflowAPI, projectAPI, seriesAPI } from '@/services/api'

const DOT: Record<string, string> = {
  idle: '#64748b', pending: '#64748b', queued: '#64748b',
  running: '#22d3ee', completed: '#818cf8', confirmed: '#34d399', error: '#fb7185',
}
const statusDot = (s?: string) => DOT[String(s || 'idle').toLowerCase()] || '#64748b'

const INPUT_CLS =
  'w-full bg-octopus-bg border border-octopus-border rounded px-2 py-1.5 text-xs text-octopus-text ' +
  'placeholder:text-octopus-muted focus:outline-none focus:border-octopus-primary/60'

interface AssetRow { nodeId: string; projectId: string; status?: string; title: string; sub?: string; image?: string }
interface EpBucket { pid: string; label: string; idx: number; rows: AssetRow[] }
interface CharGroup { key: string; name: string; image?: string; threeView?: string; basePrompt?: string; eps: EpBucket[] }
interface PropGroup {
  key: string; name: string; image?: string; useCase?: string; basePrompt?: string
  masterRow?: AssetRow            // master 道具节点（可重跑；不提供删除，避免与 shared_assets 快照不一致）
  eps: EpBucket[]                  // 各集状态
}

export default function AssetManagerModal() {
  const seriesId = useStore((s) => s.assetManagerSeriesId)
  const setSeriesId = useStore((s) => s.setAssetManagerSeriesId)
  const currentProject = useStore((s) => s.currentProject)
  const removeNodes = useStore((s) => s.removeNodes)
  const addMessage = useStore((s) => s.addMessage)

  const [tab, setTab] = useState<'outfit' | 'prop'>('outfit')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState<Record<string, string>>({})
  const [tick, setTick] = useState(0)   // 自增触发重拉

  const [seriesName, setSeriesName] = useState('')
  const [episodes, setEpisodes] = useState<{ id: string; name: string }[]>([])
  const [masterPid, setMasterPid] = useState<string | null>(null)
  const [chars, setChars] = useState<CharGroup[]>([])
  const [props, setProps] = useState<PropGroup[]>([])

  // 打开/刷新：拉 series detail + master/各集 workflow，聚合成 角色→集→服装 / 道具→集→状态
  useEffect(() => {
    if (!seriesId) return
    let cancelled = false
    ;(async () => {
      setLoading(true)
      setError(null)
      try {
        const detail = (await seriesAPI.get(seriesId)).data || {}
        const eps: { id: string; name: string }[] = (detail.episodes || []).map((e: any, i: number) => ({
          id: e.id, name: e.name || `第${i + 1}集`,
        }))
        const masterId: string | null = detail.master_project_id || null
        const fetches: Promise<{ pid: string; label: string; idx: number; nodes: any[] } | null>[] = []
        if (masterId) {
          fetches.push(projectAPI.getWorkflow(masterId)
            .then((r) => ({ pid: masterId, label: '总项目', idx: -1, nodes: r.data?.nodes || [] }))
            .catch(() => null))
        }
        eps.forEach((ep, i) => {
          fetches.push(projectAPI.getWorkflow(ep.id)
            .then((r) => ({ pid: ep.id, label: ep.name, idx: i, nodes: r.data?.nodes || [] }))
            .catch(() => null))
        })
        const wfs = (await Promise.all(fetches)).filter(Boolean) as { pid: string; label: string; idx: number; nodes: any[] }[]
        if (cancelled) return

        const charMap = new Map<string, CharGroup>()
        const propMap = new Map<string, PropGroup>()
        const epBucket = (m: { eps: EpBucket[] }, wf: { pid: string; label: string; idx: number }) => {
          let b = m.eps.find((e) => e.pid === wf.pid)
          if (!b) { b = { pid: wf.pid, label: wf.label, idx: wf.idx, rows: [] }; m.eps.push(b) }
          return b
        }
        for (const wf of wfs) {
          for (const n of wf.nodes) {
            const r = n.result || n.params || {}
            if (wf.idx === -1 && r.result_type === 'character') {
              const key = r.character_id || r.name || n.id
              const g: CharGroup = charMap.get(key) || { key, name: r.name || key, eps: [] }
              g.image = r.image_url || g.image
              g.threeView = r.three_view_image_url || g.threeView
              g.basePrompt = r.prompt || g.basePrompt
              charMap.set(key, g)
            } else if (wf.idx === -1 && r.result_type === 'prop') {
              const key = r.prop_id || r.name || n.id
              const g: PropGroup = propMap.get(key) || { key, name: r.name || key, eps: [] }
              g.image = r.image_url || g.image
              g.useCase = r.use_case || g.useCase
              g.basePrompt = r.prompt || g.basePrompt
              g.masterRow = { nodeId: n.id, projectId: wf.pid, status: n.status, title: r.name || key, image: r.image_url }
              propMap.set(key, g)
            } else if (r.result_type === 'ep_character') {
              const key = r.master_character_id || r.character_name || '?'
              const g: CharGroup = charMap.get(key) || { key, name: r.character_name || key, eps: [] }
              if (!g.image) g.image = (r.parent_master || {}).image_url
              epBucket(g, wf).rows.push({
                nodeId: n.id, projectId: wf.pid, status: n.status,
                title: r.outfit_name || n.label, sub: r.outfit_when, image: r.image_url,
              })
              charMap.set(key, g)
            } else if (r.result_type === 'ep_prop') {
              const key = r.master_prop_id || r.name || '?'
              const g: PropGroup = propMap.get(key) || { key, name: r.name || key, eps: [] }
              if (!g.image) g.image = (r.parent_master || {}).image_url
              epBucket(g, wf).rows.push({
                nodeId: n.id, projectId: wf.pid, status: n.status,
                title: r.state_name || n.label, sub: r.state_description, image: r.image_url,
              })
              propMap.set(key, g)
            }
          }
        }
        charMap.forEach((g) => g.eps.sort((a, b) => a.idx - b.idx))
        propMap.forEach((g) => g.eps.sort((a, b) => a.idx - b.idx))
        setSeriesName(detail.name || '')
        setEpisodes(eps)
        setMasterPid(masterId)
        setChars(Array.from(charMap.values()))
        setProps(Array.from(propMap.values()))
      } catch (e: any) {
        if (!cancelled) setError(e?.response?.data?.detail || e?.message || String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [seriesId, tick])

  // 新增表单默认目标集 = 当前打开的集（若属于本剧）
  const defaultEp = useMemo(() => {
    if (currentProject && episodes.some((e) => e.id === currentProject.id)) return currentProject.id
    return episodes[0]?.id || ''
  }, [currentProject, episodes])

  if (!seriesId) return null
  const close = () => { setSeriesId(null); setShowForm(false); setForm({}) }

  const rerun = async (row: AssetRow) => {
    setBusy(row.nodeId)
    try { await workflowAPI.nodeAction(row.projectId, row.nodeId, 'rerun'); setTimeout(() => setTick((t) => t + 1), 800) }
    catch (e: any) { alert('生成失败：' + (e?.response?.data?.detail || e?.message || e)) }
    finally { setBusy(null) }
  }

  const del = async (row: AssetRow) => {
    if (!confirm(`删除「${row.title}」？\n⚠ 引用它的关键帧/视频节点会被级联删除；删除后需在该集重跑分镜刷新 @ 引用。`)) return
    setBusy(row.nodeId)
    try {
      const res = await workflowAPI.deleteNode(row.projectId, row.nodeId, true)
      const deleted: string[] = res.data?.deleted || [row.nodeId]
      if (currentProject?.id === row.projectId) removeNodes(deleted)  // 当前画布同步
      setTick((t) => t + 1)
    } catch (e: any) { alert('删除失败：' + (e?.response?.data?.detail || e?.message || e)) }
    finally { setBusy(null) }
  }

  const submitCreate = async () => {
    setBusy('create')
    try {
      if (tab === 'outfit') {
        const target = form.target_ep || defaultEp
        if (!form.master_character_id || !target) { alert('请选择角色和目标集'); setBusy(null); return }
        await workflowAPI.assetAction(target, 'create_outfit', {
          master_character_id: form.master_character_id,
          outfit_name: form.outfit_name || '新服装',
          outfit_when: form.outfit_when || '',
          outfit_prompt: form.outfit_prompt || '',
        })
      } else if (form.asset_kind === 'master_prop') {
        if (!masterPid) { alert('本剧没有总项目'); setBusy(null); return }
        if (!form.name) { alert('请填写道具名'); setBusy(null); return }
        await workflowAPI.assetAction(masterPid, 'create_prop', {
          name: form.name, prompt: form.prompt || '', use_case: form.use_case || '',
        })
      } else {
        const target = form.target_ep || defaultEp
        if (!form.master_prop_id || !target) { alert('请选择道具和目标集'); setBusy(null); return }
        await workflowAPI.assetAction(target, 'create_prop_state', {
          master_prop_id: form.master_prop_id,
          state_name: form.state_name || '新状态',
          state_description: form.state_description || '',
          image_prompt: form.image_prompt || '',
        })
      }
      addMessage({
        id: `asset_${Date.now()}`, role: 'agent', timestamp: Date.now(), type: 'text',
        content: '🆕 资产已创建（待生成）。在面板点「生成」出图并确认；之后在对应集重跑分镜即可在 @ 引用中使用。',
      })
      setShowForm(false)
      setForm({})
      setTick((t) => t + 1)
    } catch (e: any) { alert('创建失败：' + (e?.response?.data?.detail || e?.message || e)) }
    finally { setBusy(null) }
  }

  const Row = ({ row, deletable = true }: { row: AssetRow; deletable?: boolean }) => (
    <div className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-octopus-bg/50 border border-octopus-border/60">
      {row.image ? (
        <img src={row.image} alt="" className="w-10 h-10 rounded object-cover flex-shrink-0" />
      ) : (
        <div className="w-10 h-10 rounded bg-octopus-border/40 flex items-center justify-center text-[9px] text-octopus-muted flex-shrink-0">待生成</div>
      )}
      <div className="flex-1 min-w-0">
        <div className="text-xs text-octopus-text truncate">{row.title}</div>
        {row.sub && <div className="text-[10px] text-octopus-muted truncate">{row.sub}</div>}
      </div>
      <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: statusDot(row.status) }} />
      <button
        onClick={() => rerun(row)} disabled={busy === row.nodeId} title="生成 / 重跑"
        className="p-1.5 rounded hover:bg-octopus-border text-octopus-muted hover:text-octopus-text disabled:opacity-40"
      >
        {busy === row.nodeId ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
      </button>
      {deletable && (
        <button
          onClick={() => del(row)} disabled={busy === row.nodeId} title="删除"
          className="p-1.5 rounded hover:bg-rose-500/20 text-octopus-muted hover:text-rose-300 disabled:opacity-40"
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
      )}
    </div>
  )

  return (
    <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="w-full max-w-2xl max-h-[82vh] flex flex-col bg-octopus-panel border border-octopus-border rounded-xl shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-octopus-border">
          <div className="flex items-center gap-2 text-sm font-medium text-octopus-text">
            <Shirt className="w-4 h-4 text-octopus-primary" />
            剧集资产 · <span className="text-octopus-muted font-normal truncate max-w-[280px]">{seriesName || seriesId}</span>
          </div>
          <button onClick={close} className="text-octopus-muted hover:text-octopus-text">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-octopus-border text-xs">
          {([['outfit', '👗 服装'], ['prop', '🎁 道具']] as const).map(([k, label]) => (
            <button
              key={k}
              onClick={() => { setTab(k); setShowForm(false); setForm({}) }}
              className={`px-4 py-2 transition-colors ${
                tab === k
                  ? 'text-octopus-primary border-b-2 border-octopus-primary font-medium'
                  : 'text-octopus-muted hover:text-octopus-text'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {/* 提示条 */}
        <div className="px-4 py-2 text-[10px] text-amber-300/90 bg-amber-500/10 border-b border-octopus-border flex items-center gap-1.5">
          <AlertTriangle className="w-3 h-3 flex-shrink-0" />
          新增/删除后需在对应集「重跑分镜」刷新 @ 引用；删除会级联删除引用它的关键帧/视频。
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-4 space-y-5">
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-10 text-xs text-octopus-muted">
              <Loader2 className="w-4 h-4 animate-spin" /> 聚合本剧资产（总项目 + 各集）...
            </div>
          ) : error ? (
            <div className="text-xs text-rose-300 text-center py-8">{error}</div>
          ) : tab === 'outfit' ? (
            chars.length === 0 ? (
              <div className="text-xs text-octopus-muted text-center py-8">本剧还没有角色资产（先跑总项目美术）</div>
            ) : (
              chars.map((g) => (
                <div key={g.key} className="rounded-lg border border-octopus-border/70 p-3 space-y-2.5">
                  {/* 角色基础设定（master） */}
                  <div className="flex items-start gap-3">
                    {g.image ? (
                      <img src={g.image} alt="" className="w-14 h-14 rounded-lg object-cover flex-shrink-0" />
                    ) : (
                      <div className="w-14 h-14 rounded-lg bg-octopus-border/40 flex items-center justify-center text-lg flex-shrink-0">🎭</div>
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-semibold text-octopus-text">🎭 {g.name}</div>
                      {g.basePrompt && (
                        <div className="text-[10px] text-octopus-muted leading-snug line-clamp-2" title={g.basePrompt}>
                          基础设定：{g.basePrompt}
                        </div>
                      )}
                    </div>
                    {g.threeView && <img src={g.threeView} alt="" className="h-14 rounded object-cover flex-shrink-0" />}
                  </div>
                  {/* 各集服装 */}
                  {g.eps.length === 0 ? (
                    <div className="text-[10px] text-octopus-muted pl-1">各集暂无服装细化</div>
                  ) : (
                    g.eps.map((ep) => (
                      <div key={ep.pid} className="space-y-1">
                        <div className="text-[11px] font-medium text-octopus-chat">
                          📺 {ep.label} <span className="text-octopus-muted font-normal">· {ep.rows.length} 套</span>
                        </div>
                        {ep.rows.map((row) => <Row key={row.nodeId} row={row} />)}
                      </div>
                    ))
                  )}
                </div>
              ))
            )
          ) : (
            props.length === 0 ? (
              <div className="text-xs text-octopus-muted text-center py-8">本剧还没有道具资产</div>
            ) : (
              props.map((g) => (
                <div key={g.key} className="rounded-lg border border-octopus-border/70 p-3 space-y-2.5">
                  <div className="flex items-start gap-3">
                    {g.image ? (
                      <img src={g.image} alt="" className="w-14 h-14 rounded-lg object-cover flex-shrink-0" />
                    ) : (
                      <div className="w-14 h-14 rounded-lg bg-octopus-border/40 flex items-center justify-center text-lg flex-shrink-0">🎁</div>
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-semibold text-octopus-text">🎁 {g.name}</div>
                      {(g.useCase || g.basePrompt) && (
                        <div className="text-[10px] text-octopus-muted leading-snug line-clamp-2" title={g.basePrompt}>
                          {g.useCase ? `用途：${g.useCase}` : g.basePrompt}
                        </div>
                      )}
                    </div>
                    {/* master 道具：可重跑（删除不开放，避免与已冻结 shared_assets 快照不一致） */}
                    {g.masterRow && (
                      <button
                        onClick={() => rerun(g.masterRow!)} disabled={busy === g.masterRow.nodeId} title="重生基础图"
                        className="p-1.5 rounded hover:bg-octopus-border text-octopus-muted hover:text-octopus-text disabled:opacity-40 flex-shrink-0"
                      >
                        {busy === g.masterRow.nodeId ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                      </button>
                    )}
                  </div>
                  {g.eps.length === 0 ? (
                    <div className="text-[10px] text-octopus-muted pl-1">各集暂无状态变化</div>
                  ) : (
                    g.eps.map((ep) => (
                      <div key={ep.pid} className="space-y-1">
                        <div className="text-[11px] font-medium text-octopus-chat">
                          📺 {ep.label} <span className="text-octopus-muted font-normal">· {ep.rows.length} 个状态</span>
                        </div>
                        {ep.rows.map((row) => <Row key={row.nodeId} row={row} />)}
                      </div>
                    ))
                  )}
                </div>
              ))
            )
          )}
        </div>

        {/* 新增表单 */}
        <div className="border-t border-octopus-border p-3">
          {!showForm ? (
            <div className="flex gap-2">
              <button
                onClick={() => { setShowForm(true); setForm(tab === 'prop' ? { asset_kind: 'ep_state' } : {}) }}
                className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-octopus-primary/15 hover:bg-octopus-primary/25 text-octopus-primary text-xs font-medium transition-colors"
              >
                <Plus className="w-3.5 h-3.5" />
                {tab === 'outfit' ? '新增服装' : '新增道具状态'}
              </button>
              {tab === 'prop' && masterPid && (
                <button
                  onClick={() => { setShowForm(true); setForm({ asset_kind: 'master_prop' }) }}
                  className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-octopus-primary/10 hover:bg-octopus-primary/20 text-octopus-primary/90 text-xs font-medium transition-colors"
                >
                  <Plus className="w-3.5 h-3.5" /> 新增道具（总项目）
                </button>
              )}
            </div>
          ) : (
            <div className="space-y-2">
              {tab === 'outfit' ? (
                <>
                  <div className="flex gap-2">
                    <select className={INPUT_CLS} value={form.master_character_id || ''}
                      onChange={(e) => setForm({ ...form, master_character_id: e.target.value })}>
                      <option value="">选择角色...</option>
                      {chars.map((g) => <option key={g.key} value={g.key}>{g.name}</option>)}
                    </select>
                    <select className={INPUT_CLS} value={form.target_ep || defaultEp}
                      onChange={(e) => setForm({ ...form, target_ep: e.target.value })}>
                      {episodes.map((ep) => <option key={ep.id} value={ep.id}>{ep.name}</option>)}
                    </select>
                  </div>
                  <div className="flex gap-2">
                    <input className={INPUT_CLS} placeholder="服装名（如：雨衣）"
                      value={form.outfit_name || ''} onChange={(e) => setForm({ ...form, outfit_name: e.target.value })} />
                    <input className={INPUT_CLS} placeholder="何时穿（如：第三幕淋雨后）"
                      value={form.outfit_when || ''} onChange={(e) => setForm({ ...form, outfit_when: e.target.value })} />
                  </div>
                  <textarea className={INPUT_CLS} rows={2} placeholder="服装描述（仅写衣服，不写脸/体型）"
                    value={form.outfit_prompt || ''} onChange={(e) => setForm({ ...form, outfit_prompt: e.target.value })} />
                </>
              ) : form.asset_kind === 'master_prop' ? (
                <>
                  <div className="flex gap-2">
                    <input className={INPUT_CLS} placeholder="道具名（如：旧台灯）"
                      value={form.name || ''} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                    <input className={INPUT_CLS} placeholder="用途（如：卧室照明）"
                      value={form.use_case || ''} onChange={(e) => setForm({ ...form, use_case: e.target.value })} />
                  </div>
                  <textarea className={INPUT_CLS} rows={2} placeholder="出图描述（如：黄铜底座旧台灯，灯罩泛黄，暖光）"
                    value={form.prompt || ''} onChange={(e) => setForm({ ...form, prompt: e.target.value })} />
                </>
              ) : (
                <>
                  <div className="flex gap-2">
                    <select className={INPUT_CLS} value={form.master_prop_id || ''}
                      onChange={(e) => setForm({ ...form, master_prop_id: e.target.value })}>
                      <option value="">选择道具...</option>
                      {props.map((g) => <option key={g.key} value={g.key}>{g.name}</option>)}
                    </select>
                    <select className={INPUT_CLS} value={form.target_ep || defaultEp}
                      onChange={(e) => setForm({ ...form, target_ep: e.target.value })}>
                      {episodes.map((ep) => <option key={ep.id} value={ep.id}>{ep.name}</option>)}
                    </select>
                  </div>
                  <div className="flex gap-2">
                    <input className={INPUT_CLS} placeholder="状态名（如：脏污）"
                      value={form.state_name || ''} onChange={(e) => setForm({ ...form, state_name: e.target.value })} />
                    <input className={INPUT_CLS} placeholder="状态说明（如：被茶水弄脏）"
                      value={form.state_description || ''} onChange={(e) => setForm({ ...form, state_description: e.target.value })} />
                  </div>
                  <textarea className={INPUT_CLS} rows={2} placeholder="出图描述（如：白色纸巾被子，中央浅褐色茶渍）"
                    value={form.image_prompt || ''} onChange={(e) => setForm({ ...form, image_prompt: e.target.value })} />
                </>
              )}
              <div className="flex gap-2">
                <button
                  onClick={submitCreate} disabled={busy === 'create'}
                  className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg bg-octopus-primary text-black text-xs font-medium hover:opacity-90 disabled:opacity-50"
                >
                  {busy === 'create' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
                  创建
                </button>
                <button
                  onClick={() => { setShowForm(false); setForm({}) }}
                  className="px-3 py-1.5 rounded-lg border border-octopus-border text-octopus-muted text-xs hover:text-octopus-text"
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
