/**
 * 发布节点编辑器
 *
 * 用户在这里：
 *   1. 看 publisher_agent 给的标题/简介/标签建议，可编辑覆盖
 *   2. 在封面候选里挑一张（关键帧 / 视频首帧），或让 AI 重新生成
 *   3. 选好平台 → 点"导出"，把视频 + 封面 + metadata.json 落到 output/publish/<id>/
 */
import React, { useEffect, useMemo, useState } from 'react'
import { Save, Sparkles, Package, ImageIcon, Check, Star } from 'lucide-react'
import { Field, Select, TextInput, TextArea } from './common'
import { WorkflowNode } from '@/types'
import { workflowAPI } from '@/services/api'
import { useStore } from '@/store/useStore'

const PLATFORM_OPTIONS = [
  { value: '本地', label: '仅本地导出' },
  { value: 'youtube', label: 'YouTube（自动上传）' },
  { value: 'tiktok', label: 'TikTok（上传到草稿）' },
  { value: '抖音', label: '抖音（仅记录元数据）' },
  { value: '快手', label: '快手（仅记录元数据）' },
  { value: '小红书', label: '小红书（仅记录元数据）' },
  { value: 'B站', label: 'B 站（仅记录元数据）' },
  { value: '视频号', label: '微信视频号（仅记录元数据）' },
]

type CoverCandidate = {
  idx: number
  source: string
  image_url: string
  shot_id?: string
  prompt?: string
  mock?: boolean
}

type CoverRec = { idx: number; rank: number; reason: string }

export default function PublishEditor({
  node,
  readOnly,
}: {
  node: WorkflowNode
  readOnly?: boolean
}) {
  const currentProject = useStore((s) => s.currentProject)
  const updateNode = useStore((s) => s.updateNode)
  const addMessage = useStore((s) => s.addMessage)

  const result = (node.result || {}) as any
  const params = (node.params || {}) as any

  // 编辑态：优先用 params 里的用户编辑值，否则用 result 的 agent 建议
  const [title, setTitle] = useState<string>(params.title || result.title_suggested || '')
  const [description, setDescription] = useState<string>(params.description || result.description_suggested || '')
  const [tags, setTags] = useState<string[]>(
    Array.isArray(params.tags) && params.tags.length ? params.tags
    : (Array.isArray(result.tags_suggested) ? result.tags_suggested : [])
  )
  const [platform, setPlatform] = useState<string>(params.platform || '抖音')
  const [coverUrl, setCoverUrl] = useState<string>(params.selected_cover || '')
  const [aiPrompt, setAiPrompt] = useState<string>(result.cover_prompt_en || '')

  const [aiBusy, setAiBusy] = useState(false)
  const [exportBusy, setExportBusy] = useState(false)
  const [aiUseRef, setAiUseRef] = useState(true)

  const candidates: CoverCandidate[] = useMemo(() => {
    const fromResult = Array.isArray(result.cover_candidates) ? result.cover_candidates : []
    const fromParams = Array.isArray(params.cover_candidates) ? params.cover_candidates : []
    // params 是用户编辑后的最新（AI 生成会写到 params），优先 params
    return fromParams.length ? fromParams : fromResult
  }, [result.cover_candidates, params.cover_candidates])

  const recs: CoverRec[] = useMemo(
    () => (Array.isArray(result.cover_recommendations) ? result.cover_recommendations : []),
    [result.cover_recommendations],
  )

  // 把 agent 推荐的 idx 标记到 candidates 上
  const recByIdx = useMemo(() => {
    const map = new Map<number, CoverRec>()
    recs.forEach((r) => map.set(r.idx, r))
    return map
  }, [recs])

  // 默认选第一推荐
  useEffect(() => {
    if (!coverUrl && recs.length && candidates.length) {
      const top = recs.find((r) => r.rank === 1) || recs[0]
      const cand = candidates.find((c) => c.idx === top.idx) || candidates[0]
      if (cand?.image_url) setCoverUrl(cand.image_url)
    }
  }, [coverUrl, recs, candidates])

  const addTag = (t: string) => {
    const v = t.trim().replace(/^#/, '').slice(0, 10)
    if (!v) return
    if (tags.includes(v)) return
    if (tags.length >= 8) return
    setTags([...tags, v])
  }

  const removeTag = (i: number) => {
    setTags(tags.filter((_, idx) => idx !== i))
  }

  const handleGenerateCover = async () => {
    if (!currentProject?.id) return
    setAiBusy(true)
    try {
      // 把所选封面作为参考（如果有），否则用已选第一推荐的图
      const refs: string[] = []
      if (aiUseRef) {
        if (coverUrl) refs.push(coverUrl)
        // 再加一张分镜里"看起来电影感"的关键帧（top rec）作辅助
        if (recs[0]) {
          const cand = candidates.find((c) => c.idx === recs[0].idx)
          if (cand?.image_url && !refs.includes(cand.image_url)) refs.push(cand.image_url)
        }
      }

      const res = await workflowAPI.publishGenerateCover(currentProject.id, node.id, {
        prompt: aiPrompt,
        reference_image_urls: refs,
      })
      const cover = res.data?.cover
      if (cover?.image_url) {
        // 通过 WS 已经把 candidates 更新了，本地也保险更新一下
        const newCandidates = [...candidates, cover]
        updateNode(node.id, {
          params: { ...params, cover_candidates: newCandidates },
        } as any)
        setCoverUrl(cover.image_url)
        addMessage({
          id: `cover_${Date.now()}`,
          role: 'agent',
          content: '🎨 新封面已生成，已自动选中',
          timestamp: Date.now(),
        })
      } else if (res.data?.error) {
        addMessage({
          id: `cover_err_${Date.now()}`,
          role: 'agent',
          content: '封面生成失败：' + res.data.error,
          timestamp: Date.now(),
        })
      }
    } catch (err) {
      addMessage({
        id: `cover_err_${Date.now()}`,
        role: 'agent',
        content: '封面生成失败：' + (err as Error).message,
        timestamp: Date.now(),
      })
    } finally {
      setAiBusy(false)
    }
  }

  const handleExport = async () => {
    if (!currentProject?.id) return
    if (!title.trim()) {
      alert('请填写标题')
      return
    }
    setExportBusy(true)
    try {
      const res = await workflowAPI.publishExport(currentProject.id, node.id, {
        title: title.trim(),
        description: description.trim(),
        tags,
        cover_image_url: coverUrl,
        platform,
      })
      if (res.data?.error) {
        addMessage({
          id: `pub_err_${Date.now()}`,
          role: 'agent',
          content: '导出失败：' + res.data.error,
          timestamp: Date.now(),
        })
      } else {
        addMessage({
          id: `pub_${Date.now()}`,
          role: 'agent',
          content: `✅ 已导出到 ${res.data?.export_url || ''}`,
          timestamp: Date.now(),
        })
      }
    } catch (err) {
      addMessage({
        id: `pub_err_${Date.now()}`,
        role: 'agent',
        content: '导出失败：' + (err as Error).message,
        timestamp: Date.now(),
      })
    } finally {
      setExportBusy(false)
    }
  }

  const exported = result.exported
  const exportUrl = result.export_url

  return (
    <div className="space-y-3">
      {result.agent_error && (
        <div className="p-2 rounded bg-yellow-900/20 border border-yellow-500/30 text-yellow-300 text-[11px]">
          ⚠ Agent 调用失败，已使用兜底文案：{result.agent_error}
        </div>
      )}

      {/* 标题 */}
      <Field label="标题（≤30 字）" hint="Agent 建议见下方占位">
        <TextInput
          value={title}
          onChange={setTitle}
          placeholder={result.title_suggested || '输入标题...'}
        />
      </Field>

      {/* 简介 */}
      <Field label="简介">
        <TextArea
          value={description}
          onChange={setDescription}
          rows={5}
          placeholder={result.description_suggested || '介绍这条视频...'}
        />
      </Field>

      {/* 标签 */}
      <Field label={`标签（${tags.length}/8）`} hint="回车添加；点 × 删除">
        <div className="flex flex-wrap gap-1 mb-1">
          {tags.map((t, i) => (
            <span
              key={i}
              className="px-2 py-0.5 rounded-full bg-octopus-primary/15 text-octopus-primary text-[11px] flex items-center gap-1"
            >
              #{t}
              <button
                onClick={() => removeTag(i)}
                className="hover:text-red-400"
                disabled={readOnly}
              >×</button>
            </span>
          ))}
        </div>
        <input
          type="text"
          placeholder="输入新标签按回车..."
          disabled={readOnly || tags.length >= 8}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              addTag((e.target as HTMLInputElement).value)
              ;(e.target as HTMLInputElement).value = ''
            }
          }}
          className="w-full px-2 py-1 rounded border border-octopus-border bg-octopus-bg text-octopus-text text-xs"
        />
      </Field>

      {/* 平台 */}
      <Field
        label="发布平台"
        hint={platform === 'youtube' || platform === 'tiktok'
          ? '已选自动上传平台，导出后会自动调用 OAuth 上传'
          : '仅记录到 metadata.json，不真正上传'}
      >
        <Select
          value={platform}
          options={PLATFORM_OPTIONS}
          onChange={setPlatform}
        />
        {(platform === 'youtube' || platform === 'tiktok') && (
          <div className="mt-1 text-[10px] text-octopus-muted">
            注意：自动上传需要先在「系统设置 → 发布：{platform === 'youtube' ? 'YouTube' : 'TikTok'}」里完成 OAuth 授权
          </div>
        )}
      </Field>

      {/* 封面候选 */}
      <Field
        label={`封面（${candidates.length} 张候选）`}
        hint="⭐ = Agent 推荐；点击选中"
      >
        <div className="grid grid-cols-3 gap-1.5">
          {candidates.map((c, i) => {
            const idx = c.idx ?? i
            const rec = recByIdx.get(idx)
            const isSelected = coverUrl && coverUrl === c.image_url
            return (
              <button
                key={`${idx}-${c.image_url}`}
                onClick={() => setCoverUrl(c.image_url)}
                disabled={readOnly}
                title={rec?.reason || c.prompt || c.shot_id}
                className={`relative aspect-[9/16] rounded border-2 overflow-hidden bg-black ${
                  isSelected
                    ? 'border-octopus-primary ring-2 ring-octopus-primary/40'
                    : 'border-octopus-border hover:border-octopus-primary/60'
                }`}
              >
                {c.image_url ? (
                  <img
                    src={c.image_url}
                    alt={c.shot_id || `cover-${idx}`}
                    className="w-full h-full object-cover"
                    onError={(e) => { (e.target as HTMLImageElement).style.opacity = '0.2' }}
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-octopus-muted">
                    <ImageIcon className="w-4 h-4" />
                  </div>
                )}
                {rec && (
                  <span className="absolute top-1 left-1 px-1 py-0.5 rounded bg-yellow-500/90 text-black text-[9px] font-bold flex items-center gap-0.5">
                    <Star className="w-2.5 h-2.5 fill-current" />
                    {rec.rank}
                  </span>
                )}
                {c.source === 'ai_generated' && (
                  <span className="absolute top-1 right-1 px-1 py-0.5 rounded bg-pink-500/90 text-white text-[9px]">AI</span>
                )}
                {isSelected && (
                  <span className="absolute bottom-1 right-1 w-4 h-4 rounded-full bg-octopus-primary flex items-center justify-center">
                    <Check className="w-3 h-3 text-white" />
                  </span>
                )}
              </button>
            )
          })}
        </div>
        {recs[0] && (
          <div className="mt-1 text-[10px] text-octopus-muted">
            Agent 首推：「{recs[0].reason}」
          </div>
        )}
      </Field>

      {/* AI 生成新封面 */}
      <Field
        label="让 AI 重新生成一张封面"
        hint="基于英文 prompt（Agent 已给出建议），可选融合已选封面作为参考"
      >
        <TextArea
          value={aiPrompt}
          onChange={setAiPrompt}
          rows={3}
          placeholder="cinematic poster, eye-catching, dramatic lighting..."
        />
        <label className="flex items-center gap-1.5 mt-1 text-[10px] text-octopus-muted cursor-pointer">
          <input
            type="checkbox"
            checked={aiUseRef}
            onChange={(e) => setAiUseRef(e.target.checked)}
          />
          融合当前选中的封面作为参考（关闭则纯文生图）
        </label>
        <button
          onClick={handleGenerateCover}
          disabled={readOnly || aiBusy || !aiPrompt.trim()}
          className="mt-2 w-full flex items-center justify-center gap-1.5 px-2 py-1.5 rounded bg-pink-600/80 hover:bg-pink-500 disabled:opacity-40 text-white text-xs"
        >
          <Sparkles className={`w-3 h-3 ${aiBusy ? 'animate-pulse' : ''}`} />
          {aiBusy ? 'AI 生成中（约 30s）...' : '让 AI 生成新封面'}
        </button>
      </Field>

      {/* 导出 */}
      <div className="pt-2 border-t border-octopus-border">
        <button
          onClick={handleExport}
          disabled={readOnly || exportBusy || !title.trim()}
          className="w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-octopus-primary hover:bg-octopus-primary/80 disabled:opacity-40 text-white text-sm font-medium"
        >
          <Package className={`w-4 h-4 ${exportBusy ? 'animate-pulse' : ''}`} />
          {exportBusy ? '导出中...' : exported ? '重新导出' : '导出到本地'}
        </button>
        {exported && exportUrl && (
          <div className="mt-2 p-2 rounded bg-green-900/20 border border-green-500/30 text-[11px] text-green-300">
            <div className="flex items-center gap-1 mb-1">
              <Check className="w-3 h-3" /> 已导出
            </div>
            <div className="font-mono text-[10px] break-all">{exportUrl}</div>
            {result.exported_files && (
              <div className="mt-1 text-[10px] text-octopus-muted">
                {Object.entries(result.exported_files)
                  .filter(([k]) => k !== 'translations')
                  .map(([, v]) => String(v))
                  .join(' · ')}
              </div>
            )}
            {result.exported_files?.translations && Object.keys(result.exported_files.translations).length > 0 && (
              <div className="mt-2 pt-2 border-t border-green-500/20">
                <div className="text-[10px] text-octopus-muted mb-1">🌍 多语言版</div>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(result.exported_files.translations as Record<string, string>).map(([lang, fname]) => (
                    <a
                      key={lang}
                      href={`${exportUrl}/${fname}`}
                      target="_blank"
                      rel="noreferrer"
                      className="px-1.5 py-0.5 rounded bg-green-900/30 border border-green-500/30 text-green-200 hover:text-white text-[10px]"
                    >
                      {lang} ↗
                    </a>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
        {result.upload && (
          <div className={`mt-2 p-2 rounded text-[11px] ${
            result.upload.ok
              ? 'bg-cyan-900/20 border border-cyan-500/30 text-cyan-300'
              : 'bg-red-900/20 border border-red-500/30 text-red-300'
          }`}>
            <div className="flex items-center gap-1 mb-1 font-semibold">
              {result.upload.ok ? <Check className="w-3 h-3" /> : '✗'}
              {result.upload.ok
                ? `已上传到 ${result.upload.platform}（${result.upload.visibility || '默认可见性'}）`
                : `${result.upload.platform} 上传失败`}
            </div>
            {result.upload.video_url && (
              <a
                href={result.upload.video_url}
                target="_blank"
                rel="noreferrer"
                className="font-mono text-[10px] break-all underline hover:text-white"
              >{result.upload.video_url}</a>
            )}
            {!result.upload.video_url && result.upload.video_id && (
              <div className="font-mono text-[10px] break-all">id: {result.upload.video_id}</div>
            )}
            {result.upload.error && (
              <div className="text-[10px] text-red-200 mt-1 break-words">{result.upload.error}</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
