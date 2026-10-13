/**
 * 本地化编辑器 — 把合成成片经蚂蚁 videotranslate 翻译成多语言（手动触发，用户选语言/类型）
 */
import React, { useState } from 'react'
import { Globe, Loader2 } from 'lucide-react'
import { useStore } from '@/store/useStore'
import { workflowAPI } from '@/services/api'

// 蚂蚁 videotranslate 常用目标语言（海外 + 港台/方言）
const LANGS: { code: string; label: string }[] = [
  { code: 'en', label: '英语' }, { code: 'ja', label: '日语' }, { code: 'ko', label: '韩语' },
  { code: 'es', label: '西班牙语' }, { code: 'pt', label: '葡萄牙语' }, { code: 'fr', label: '法语' },
  { code: 'de', label: '德语' }, { code: 'ru', label: '俄语' }, { code: 'ar', label: '阿拉伯语' },
  { code: 'vi', label: '越南语' }, { code: 'th', label: '泰语' }, { code: 'id', label: '印尼语' },
  { code: 'ms', label: '马来语' }, { code: 'tr', label: '土耳其语' }, { code: 'zh-tw', label: '繁体中文' },
  { code: 'yue', label: '粤语' },
]

export default function LocalizationEditor({ node, readOnly }: { node: any; readOnly?: boolean }) {
  const currentProject = useStore((s) => s.currentProject)
  const addMessage = useStore((s) => s.addMessage)
  const params = node.params || {}
  const result = node.result || {}
  const [langs, setLangs] = useState<string[]>(
    Array.isArray(params.target_languages) && params.target_languages.length ? params.target_languages : ['en'],
  )
  const [ttype, setTtype] = useState<string>(((params.translate_type as string) || 'VOICE').toUpperCase())
  const [busy, setBusy] = useState(false)
  const running = (node.status || '') === 'running'

  const toggle = (code: string) =>
    setLangs((prev) => (prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code].slice(0, 10)))

  const run = async () => {
    if (!currentProject?.id || !langs.length) return
    setBusy(true)
    try {
      await workflowAPI.localize(currentProject.id, node.id, langs, ttype)
      addMessage({
        id: `loc_${Date.now()}`, role: 'agent',
        content: `🌐 本地化启动：翻译成 ${langs.join('、')}（${ttype}）...`, timestamp: Date.now(),
      })
    } catch (e) {
      addMessage({
        id: `loce_${Date.now()}`, role: 'agent',
        content: '本地化失败：' + (e as Error).message, timestamp: Date.now(),
      })
    } finally {
      setBusy(false)
    }
  }

  const translations = Array.isArray(result.translations) ? result.translations : []

  return (
    <div className="space-y-3 text-sm">
      <div className="text-octopus-muted text-xs leading-relaxed">
        把合成成片翻译成其它语言（蚂蚁 videotranslate）。默认英语、可多选。翻译需蚂蚁开通接口白名单 + 成片上传 OSS。
      </div>

      {/* 翻译类型 */}
      <div>
        <div className="text-xs text-octopus-muted mb-1">翻译类型</div>
        <select
          value={ttype}
          onChange={(e) => setTtype(e.target.value)}
          disabled={readOnly || running}
          className="w-full bg-octopus-bg border border-octopus-border rounded-lg px-2 py-1.5 text-octopus-text text-xs"
        >
          <option value="VOICE">VOICE · 替换配音（最实用）</option>
          <option value="FACE">FACE · 替换配音 + 对口型（更真更贵，目标语言仅英语）</option>
          <option value="SUBTITLE">SUBTITLE · 只翻字幕</option>
        </select>
      </div>

      {/* 目标语言多选 */}
      <div>
        <div className="text-xs text-octopus-muted mb-1">目标语言（已选 {langs.length}，最多 10）</div>
        <div className="grid grid-cols-3 gap-1.5">
          {LANGS.map((l) => {
            const on = langs.includes(l.code)
            const disabled = ttype === 'FACE' && l.code !== 'en'
            return (
              <button
                key={l.code}
                disabled={disabled || readOnly || running}
                onClick={() => toggle(l.code)}
                className={`px-2 py-1 rounded-lg text-[11px] border transition-colors ${
                  on
                    ? 'bg-octopus-primary/20 border-octopus-primary/50 text-octopus-text'
                    : 'bg-octopus-bg border-octopus-border text-octopus-muted hover:text-octopus-text'
                } ${disabled ? 'opacity-30 cursor-not-allowed' : ''}`}
              >
                {l.label}
              </button>
            )
          })}
        </div>
      </div>

      {/* 触发 */}
      <button
        onClick={run}
        disabled={busy || running || readOnly || !langs.length}
        className="w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-gradient-to-r from-sky-500 to-indigo-500 hover:from-sky-400 hover:to-indigo-400 text-white text-sm font-medium disabled:opacity-40 shadow-[0_0_14px_rgba(56,189,248,0.4)]"
      >
        {busy || running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Globe className="w-4 h-4" />}
        {running ? '翻译中...' : '开始本地化'}
      </button>

      {/* 结果 */}
      {translations.length > 0 && (
        <div className="pt-2 border-t border-octopus-border space-y-1.5">
          <div className="text-xs text-octopus-muted">译后视频</div>
          {translations.map((t: any, i: number) => (
            <div key={i} className="flex items-center justify-between text-xs">
              <span className="text-octopus-text">{t.target_language}</span>
              {t.video_url ? (
                <a href={t.video_url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">
                  打开视频 ↗
                </a>
              ) : (
                <span className="text-rose-400 truncate max-w-[200px]" title={t.error}>{t.error || '失败'}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
