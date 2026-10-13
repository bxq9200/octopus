/**
 * SetupWizard — 首次启动配置向导
 *
 * 当 /api/settings/status.needs_wizard 为 true 时显示。
 * 终端风格沿用 LoginPage 的窗口骨架（交通灯 + CRT），核心交互两步：
 *   1. LLM:    选 provider(kimi / deepseek / openai) → 显示外链 → 回填 key → 测试 → 下一步
 *   2. Image:  文生图 provider(zhipu / google) + Seedream 图生图(角色三视图/关键帧，始终可填)
 * 视频生成走云端中转（credits 计费），无需在此配置。
 * 完成后调 onComplete，由 App.tsx 把 wizard 关掉跳主界面。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react'
import { ExternalLink, CheckCircle2, AlertCircle, RotateCw, ArrowRight, SkipForward } from 'lucide-react'
import { settingsAPI } from '@/services/api'
import HintText from '@/components/common/HintText'

/** 官网文档根地址：图文教程放云端，改文档不用重新发版 */
export const DOCS_BASE = 'https://github.com/bxq9200/octopus/blob/main/docs'

type FieldDef = {
  key: string
  label: string
  sensitive?: boolean
  kind?: 'string' | 'select'
  options?: { value: string; label: string }[]
  placeholder?: string
  hint?: string
}

type FieldValue = { value: string; has_value: boolean; sensitive: boolean }

type Category = {
  label: string
  icon?: string
  fields: FieldDef[]
  values: Record<string, FieldValue>
}

type StepId = 'llm' | 'image'

const STEPS: { id: StepId; title: string; subtitle: string; skippable: boolean }[] = [
  { id: 'llm',   title: '大模型 (LLM)', subtitle: '驱动导演 / 编剧 / 美术 / 分镜的核心', skippable: false },
  { id: 'image', title: '图片生成',     subtitle: '角色三视图(Seedream) · 场景/道具(智谱)', skippable: false },
]
// 视频生成走云端中转（credits 计费），无需在此配置

const PROVIDER_BY_STEP: Record<StepId, string[]> = {
  llm: ['kimi', 'deepseek', 'openai'],
  image: ['zhipu', 'google'],
}

// provider 后端标识 → wizard 按钮上显示的型号名
const PROVIDER_LABELS: Record<string, string> = {
  kimi: 'kimi',
  deepseek: 'deepseek',
  openai: 'openai',
  zhipu: 'zhipu',
  google: 'google',
}

// image 步骤除文生图 provider(智谱/google) 外，始终附带的 Seedream 图生图字段（角色三视图/关键帧专用）
const SEEDREAM_FIELDS = ['seedream_api_key', 'seedream_base_url', 'seedream_image_edit_model']

// 每个 step + provider 对应：要在这一步里填哪些字段（按显示顺序）
const FIELDS_BY_PROVIDER: Record<StepId, Record<string, string[]>> = {
  llm: {
    kimi:     ['kimi_api_key', 'kimi_base_url', 'kimi_model'],
    deepseek: ['deepseek_api_key', 'deepseek_base_url', 'deepseek_model'],
    openai:   ['openai_api_key', 'openai_base_url', 'openai_model'],
  },
  image: {
    zhipu:  ['zhipu_api_key', 'zhipu_base_url', 'zhipu_model'],
    google: ['google_api_key', 'google_model'],
  },
}

export default function SetupWizard({ onComplete }: { onComplete: () => void }) {
  const [data, setData] = useState<Record<string, Category> | null>(null)
  const [currentStep, setCurrentStep] = useState<StepId>('llm')
  // 用户在每个 step 选择的 provider
  const [providerByStep, setProviderByStep] = useState<Record<StepId, string>>({
    llm: 'kimi', image: 'zhipu',
  })
  // 草稿：每个字段当前输入的值（与 SettingsModal 一致：未触摸则用 DB 值或空）
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [stepValidated, setStepValidated] = useState<Record<StepId, boolean>>({
    llm: false, image: false,
  })
  const [finishing, setFinishing] = useState<string[]>([])

  // 启动时拉一次配置 + 决定每步初始 provider
  useEffect(() => {
    settingsAPI.getAll().then((res) => {
      const d = res.data as Record<string, Category>
      setData(d)
      // 根据已存在的值，把初始 provider 设到"已经配过的那个"
      setProviderByStep({
        llm: (d.llm?.values?.provider?.value as string) || 'kimi',
        image: (d.image?.values?.provider?.value as string) ||
               (d.image?.values?.zhipu_api_key?.has_value ? 'zhipu' :
                d.image?.values?.google_api_key?.has_value ? 'google' : 'zhipu'),
      })
      // 已经 has_value 的 step 直接标 validated（用户在 SettingsModal 配过了）
      const stepDone = (cat: 'llm' | 'image', providers: string[]): boolean => {
        return providers.some((p) => d[cat]?.values?.[`${p}_api_key`]?.has_value)
      }
      setStepValidated({
        llm: stepDone('llm', PROVIDER_BY_STEP.llm),
        image: stepDone('image', PROVIDER_BY_STEP.image),
      })
    }).catch((err) => console.warn('加载配置失败', err))
  }, [])

  const currentProvider = providerByStep[currentStep]
  const currentCat = data?.[currentStep]
  const fieldKeys = useMemo(() => {
    const base = FIELDS_BY_PROVIDER[currentStep][currentProvider] || []
    // image 步骤：文生图字段之外，始终追加 Seedream 图生图字段（角色三视图/关键帧）
    return currentStep === 'image' ? [...base, ...SEEDREAM_FIELDS] : base
  }, [currentStep, currentProvider])
  const fieldDefs = useMemo(() => {
    if (!currentCat) return []
    const map = Object.fromEntries(currentCat.fields.map((f) => [f.key, f]))
    return fieldKeys.map((k) => map[k]).filter(Boolean) as FieldDef[]
  }, [currentCat, fieldKeys])

  // 这一步里选择 provider 时，清理上一组 provider 的草稿（避免误把 zhipu 输入提交到 google）
  const setProvider = (p: string) => {
    setProviderByStep((prev) => ({ ...prev, [currentStep]: p }))
    setTestResult(null)
  }

  const handleEdit = (key: string, value: string) => {
    setEdits((prev) => ({ ...prev, [key]: value }))
    // 字段一旦改动，重置测试状态：必须重新测一遍
    setStepValidated((prev) => ({ ...prev, [currentStep]: false }))
    setTestResult(null)
  }

  // 保存当前 step 编辑过的字段到后端
  const saveCurrentEdits = async (): Promise<boolean> => {
    const cat = currentStep
    const payload: Record<string, { value: string }> = {}
    for (const k of fieldKeys) {
      if (edits[k] !== undefined) {
        payload[k] = { value: edits[k] }
      }
    }
    // provider 字段（LLM / image 都有 select）
    const savedProvider = data?.[cat]?.values?.provider?.value as string | undefined
    if ((cat === 'llm' || cat === 'image') && providerByStep[cat] !== savedProvider) {
      payload['provider'] = { value: providerByStep[cat] }
    }
    if (Object.keys(payload).length === 0) return true
    setSaving(true)
    try {
      await settingsAPI.updateCategory(cat, payload)
      // 重新拉一次
      const res = await settingsAPI.getAll()
      setData(res.data)
      // 已保存的字段从 edits 里移除
      setEdits((prev) => {
        const next = { ...prev }
        for (const k of Object.keys(payload)) delete next[k]
        return next
      })
      return true
    } catch (e) {
      setTestResult({ ok: false, msg: '保存失败：' + (e as Error).message })
      return false
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      // 先把当前编辑提交到 DB（test 接口走 runtime_config → DB 值）
      const ok = await saveCurrentEdits()
      if (!ok) return
      const res = await settingsAPI.testProvider(currentStep, currentProvider)
      if (res.data?.ok) {
        setTestResult({
          ok: true,
          msg: res.data.reply
            ? `连接正常 ✓ (${res.data.model})`
            : `已配置 ✓ (${res.data.model || currentProvider})`,
        })
        setStepValidated((prev) => ({ ...prev, [currentStep]: true }))
      } else {
        setTestResult({ ok: false, msg: res.data?.error || '未知错误' })
      }
    } catch (err) {
      setTestResult({ ok: false, msg: (err as Error).message })
    } finally {
      setTesting(false)
    }
  }

  const handleNext = async () => {
    // 没测过的话，先尝试自动测一次（如果有 has_value 也允许）
    if (!stepValidated[currentStep]) {
      const step = STEPS.find((s) => s.id === currentStep)!
      if (!step.skippable) {
        setTestResult({ ok: false, msg: '请先测试连接' })
        return
      }
    }
    const idx = STEPS.findIndex((s) => s.id === currentStep)
    if (idx < STEPS.length - 1) {
      setCurrentStep(STEPS[idx + 1].id)
      setTestResult(null)
    } else {
      // 最后一步：完成
      await runFinishAnimation()
    }
  }

  const handleSkip = async () => {
    const step = STEPS.find((s) => s.id === currentStep)
    if (!step?.skippable) return
    const idx = STEPS.findIndex((s) => s.id === currentStep)
    if (idx < STEPS.length - 1) {
      setCurrentStep(STEPS[idx + 1].id)
      setTestResult(null)
    } else {
      await runFinishAnimation()
    }
  }

  const runFinishAnimation = async () => {
    const lines = [
      '> writing config to ~/.octopus/master.key...',
      '> warming up agents...',
      '> entering octopus.',
    ]
    for (const l of lines) {
      setFinishing((prev) => [...prev, l])
      await new Promise((r) => setTimeout(r, 350))
    }
    await new Promise((r) => setTimeout(r, 400))
    onComplete()
  }

  // 渲染 ────────────────────────────────────────
  const stepIdx = STEPS.findIndex((s) => s.id === currentStep)
  const stepMeta = STEPS[stepIdx]

  return (
    <div className="relative w-screen h-screen overflow-hidden bg-black text-octopus-text">
      <div className="relative z-10 w-full h-full flex flex-col items-center justify-center px-4 py-6">
        <div className="w-full max-w-[640px] max-h-[90vh] bg-[#0a0a0f]/90 border border-white/10 rounded-xl shadow-[0_0_60px_rgba(99,102,241,0.18)] overflow-hidden backdrop-blur-sm flex flex-col">
          {/* 标题栏 */}
          <div className="flex items-center gap-2 px-4 py-2.5 border-b border-white/10 bg-black/40 flex-shrink-0">
            <span className="w-3 h-3 rounded-full bg-red-500/80" />
            <span className="w-3 h-3 rounded-full bg-yellow-500/80" />
            <span className="w-3 h-3 rounded-full bg-green-500/80" />
            <div className="flex-1 text-center text-xs text-white/50 font-mono select-none">
              octopus@local — /usr/local/bin/octopus-setup
            </div>
            <div className="w-12" />
          </div>

          {/* 主体 */}
          <div className="px-6 py-4 font-mono text-sm flex flex-col gap-3 overflow-y-auto">
            {/* 顶部：单行 logo */}
            <div className="flex items-center justify-center gap-2 select-none">
              <span className="text-base">🐙</span>
              <span className="text-[11px] tracking-[0.3em] text-white/70">OCTOPUS · SETUP WIZARD</span>
            </div>

            {/* 步骤指示器 */}
            <div className="flex items-center justify-center gap-2 mb-2">
              {STEPS.map((s, i) => (
                <React.Fragment key={s.id}>
                  <div
                    className={`flex items-center gap-1.5 px-2.5 py-1 rounded border text-[11px] ${
                      currentStep === s.id
                        ? 'border-cyan-400/70 text-cyan-300 bg-cyan-400/10'
                        : i < stepIdx
                        ? 'border-green-500/50 text-green-300/80 bg-green-500/5'
                        : 'border-white/15 text-white/40'
                    }`}
                  >
                    {i < stepIdx ? <CheckCircle2 className="w-3 h-3" /> : <span className="text-[9px] opacity-60">{i + 1}</span>}
                    <span>{s.title.split(' ')[0]}</span>
                  </div>
                  {i < STEPS.length - 1 && <span className="text-white/20">─</span>}
                </React.Fragment>
              ))}
            </div>

            {/* 当前 step 头部 */}
            <div className="border-t border-white/10 pt-3">
              <div className="text-cyan-300/90 text-[13px]">
                Step {stepIdx + 1} / {STEPS.length} — {stepMeta?.title}
              </div>
              <div className="text-white/40 text-[11px] mt-0.5">{stepMeta?.subtitle}</div>
            </div>

            {/* Provider 选择 */}
            {PROVIDER_BY_STEP[currentStep].length > 1 && (
              <div className="flex gap-2 items-center text-[12px]">
                <span className="text-cyan-400/80">$</span>
                <span className="text-white/60">provider:</span>
                {PROVIDER_BY_STEP[currentStep].map((p) => (
                  <button
                    key={p}
                    onClick={() => setProvider(p)}
                    className={`px-2.5 py-0.5 rounded border text-[11px] transition-colors ${
                      currentProvider === p
                        ? 'border-cyan-400/70 text-cyan-300 bg-cyan-400/10'
                        : 'border-white/15 text-white/50 hover:text-white/80 hover:border-white/30'
                    }`}
                  >
                    {PROVIDER_LABELS[p] || p}
                  </button>
                ))}
              </div>
            )}

            {/* 怎么拿 Key —— 分步说明（来自后端 hint，URL 自动可点）+ 图文教程入口 */}
            {(() => {
              const guide = fieldDefs.find((f) => f.hint && f.hint.includes('http'))?.hint
              return (
                <div className="rounded-lg border border-cyan-400/25 bg-cyan-400/[0.04] px-3 py-2.5 space-y-1.5">
                  <div className="flex items-center gap-1.5 text-[11px] text-cyan-300">
                    <ExternalLink className="w-3 h-3" />
                    <span className="font-semibold">怎么拿到这个 Key？</span>
                    <a href={`${DOCS_BASE}/api-keys.md`} target="_blank" rel="noreferrer"
                       className="ml-auto text-[10.5px] text-cyan-400/80 hover:text-cyan-200 underline underline-offset-2">
                      看图文教程 →
                    </a>
                  </div>
                  {guide && (
                    <div className="text-[10.5px] text-white/55 leading-relaxed whitespace-pre-line">
                      <HintText text={guide} />
                    </div>
                  )}
                </div>
              )
            })()}

            {/* 字段输入 */}
            <div className="space-y-2 mt-1">
              {fieldDefs.map((f) => {
                const dbValue = currentCat?.values?.[f.key]
                const editing = edits[f.key]
                const displayValue = editing !== undefined ? editing : (dbValue?.value || '')
                const placeholder =
                  f.sensitive && dbValue?.has_value && editing === undefined
                    ? '●●●●●●●● （已配置，留空保持不变）'
                    : f.placeholder || ''
                return (
                  <div key={f.key} className="flex items-center gap-2">
                    <span className="text-cyan-400/80 w-3 text-[12px]">$</span>
                    <span className="text-white/60 text-[11px] w-32 truncate">{f.label}:</span>
                    <div className="flex-1 relative">
                      <input
                        type={f.sensitive ? 'password' : 'text'}
                        value={displayValue}
                        placeholder={placeholder}
                        onChange={(e) => handleEdit(f.key, e.target.value)}
                        autoComplete="off"
                        spellCheck={false}
                        className="w-full px-2 py-1 rounded border border-white/10 bg-black/40 text-white/90 text-[11px] font-mono focus:border-cyan-400/60 focus:outline-none"
                      />
                      {f.sensitive && dbValue?.has_value && (
                        <CheckCircle2 className="w-3 h-3 text-green-500/80 absolute right-2 top-1/2 -translate-y-1/2" />
                      )}
                    </div>
                  </div>
                )
              })}
            </div>

            {/* 测试结果 */}
            {testResult && (
              <div
                className={`mt-2 p-2 rounded text-[11px] flex items-start gap-2 ${
                  testResult.ok
                    ? 'bg-green-900/20 border border-green-500/30 text-green-300'
                    : 'bg-red-900/20 border border-red-500/30 text-red-300'
                }`}
              >
                {testResult.ok ? <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" /> : <AlertCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />}
                <span className="break-all">{testResult.msg}</span>
              </div>
            )}

            {/* 完成动画日志 */}
            {finishing.length > 0 && (
              <div className="border-t border-white/10 pt-2 mt-2 space-y-1">
                {finishing.map((l, i) => (
                  <div key={i} className="text-green-400/90 text-[11px]">{l}</div>
                ))}
              </div>
            )}
          </div>

          {/* footer */}
          <div className="px-6 py-3 border-t border-white/10 bg-black/30 flex items-center justify-between flex-shrink-0">
            <div className="text-[10px] text-white/30">
              {saving ? '保存中...' : stepValidated[currentStep] ? '此步骤已通过' : '完成测试后进入下一步'}
            </div>
            <div className="flex gap-2 items-center">
              <button
                onClick={handleTest}
                disabled={testing || saving}
                className="flex items-center gap-1.5 px-3 py-1 rounded border border-cyan-400/40 bg-cyan-400/10 hover:bg-cyan-400/20 hover:border-cyan-400/70 text-cyan-300 text-[11px] disabled:opacity-40 transition-colors"
              >
                <RotateCw className={`w-3 h-3 ${testing ? 'animate-spin' : ''}`} />
                {testing ? '测试中...' : '测试连接'}
              </button>
              {stepMeta?.skippable && !stepValidated[currentStep] && (
                <button
                  onClick={handleSkip}
                  disabled={saving || finishing.length > 0}
                  className="flex items-center gap-1 px-3 py-1 rounded border border-white/15 text-white/50 hover:text-white/80 hover:border-white/30 text-[11px] disabled:opacity-40"
                >
                  <SkipForward className="w-3 h-3" />
                  跳过
                </button>
              )}
              <button
                onClick={handleNext}
                disabled={(!stepValidated[currentStep] && !stepMeta?.skippable) || saving || finishing.length > 0}
                className="flex items-center gap-1 px-3 py-1 rounded bg-cyan-400/20 border border-cyan-400/50 hover:bg-cyan-400/30 text-cyan-200 text-[11px] disabled:opacity-30 transition-colors"
              >
                {stepIdx === STEPS.length - 1 ? '完成' : '下一步'}
                <ArrowRight className="w-3 h-3" />
              </button>
            </div>
          </div>
        </div>

        <div className="mt-3 text-[10px] text-white/30 font-mono tracking-wider select-none">
          octopus · 视频生成由云端提供 · © 2026
        </div>
      </div>

      <div className="crt-scanlines" />
      <div className="crt-vignette" />
    </div>
  )
}
