/**
 * 结果子节点编辑器 — 角色/场景图片
 */
import React, { useState, useEffect } from 'react'
import { Save, RotateCcw } from 'lucide-react'
import { Field, TextInput, TextArea, safeSlice } from './common'
import { WorkflowNode } from '@/types'
import UploadImageOverrideButton from './UploadImageOverrideButton'

export default function ResultChildEditor({
  node,
  onSave,
  onRegenerate,
  readOnly,
}: {
  node: WorkflowNode
  onSave: (newResult: any) => Promise<void> | void
  onRegenerate?: () => Promise<void> | void
  readOnly?: boolean
}) {
  const r = node.result ?? {}
  // 0.7.0：ep_character/scene/prop 旧结果只存了 outfit_prompt/image_prompt（没 prompt）→ 编辑器空。
  // 显示时回退到这些字段（fortified 含背景后缀，放最后兜底）。
  const pickPrompt = (res: any): string =>
    res?.prompt ?? res?.outfit_prompt ?? res?.image_prompt ?? res?.fortified_prompt ?? ''
  const [name, setName] = useState<string>(r.name ?? r.location ?? '')
  const [prompt, setPrompt] = useState<string>(pickPrompt(r))
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    const r = node.result ?? {}
    setName(r.name ?? r.location ?? '')
    setPrompt(pickPrompt(r))
    setDirty(false)
  }, [node.id, node.result])

  const isCharacter = r.result_type === 'character' || r.result_type === 'ep_character'
  const isEpisodeResult = (r.result_type || '').startsWith('ep_')
  const threeViewUrl: string | undefined = r.three_view_image_url

  const handleSave = async () => {
    setSaving(true)
    try {
      const newResult = {
        ...r,
        prompt,
        ...(isCharacter ? { name } : { location: name }),
      }
      await onSave(newResult)
      setDirty(false)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-3">
      {r.image_url && (
        <div className="space-y-1">
          <div className="flex items-center justify-between">
            <div className="text-[10px] text-octopus-muted">{isCharacter ? '正面图' : '场景图'}</div>
            {!readOnly && (
              <UploadImageOverrideButton nodeId={node.id} field="image_url" label="上传替换" />
            )}
          </div>
          <div className="rounded-lg overflow-hidden border border-octopus-border">
            <img
              src={r.image_url}
              alt={safeSlice(prompt, 30) || '生成的图片'}
              className="w-full h-auto"
              onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
            />
            {r.mock && (
              <div className="px-2 py-1 text-[10px] text-yellow-400 bg-yellow-900/20">[Mock 图片]</div>
            )}
            {r.user_uploaded && (
              <div className="px-2 py-1 text-[10px] text-cyan-300 bg-cyan-900/20">📤 用户上传</div>
            )}
          </div>
        </div>
      )}

      {/* 0.7.5 场景机位板：建立镜之外的额外机位（Qwen 转机位 / Seedream 出的板），分镜用 @场景#机位 引用 */}
      {Array.isArray((r as any).cameras) && (r as any).cameras.length > 1 && (
        <div className="space-y-1">
          <div className="text-[10px] text-octopus-muted">
            🎥 机位板（{(r as any).cameras.length - 1} 个额外机位 · 分镜用 @{r.location || '场景'}#机位 引用）
          </div>
          <div className="grid grid-cols-2 gap-2">
            {(r as any).cameras.slice(1).map((cam: any, i: number) => (
              <div key={cam.camera_id || i} className="rounded-lg overflow-hidden border border-octopus-border">
                <img
                  src={cam.image_url}
                  alt={cam.camera_name || `机位${i + 1}`}
                  className="w-full h-auto"
                  onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
                />
                <div className="px-1.5 py-0.5 text-[10px] text-octopus-muted truncate" title={cam.camera_name}>
                  {cam.camera_name || `机位${i + 1}`}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 没有 image_url 时也允许直接上传一张作为初始图（如 mock 失败 / 跳过场景） */}
      {!r.image_url && !readOnly && (
        <div className="p-2 rounded border border-dashed border-octopus-border text-center">
          <div className="text-xs text-octopus-muted mb-2">还没有图，可直接上传一张</div>
          <UploadImageOverrideButton nodeId={node.id} field="image_url" label={`上传${isCharacter ? '正面图' : '场景图'}`} />
        </div>
      )}

      {isCharacter && r.face_close_up_url && (
        <div className="space-y-1">
          <div className="text-[10px] text-octopus-muted">头部特写（角色脸部参考）</div>
          <div className="rounded-lg overflow-hidden border border-octopus-border">
            <img
              src={r.face_close_up_url}
              alt="face close-up"
              className="w-full h-auto"
              onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
            />
            {r.face_close_up_mock && (
              <div className="px-2 py-1 text-[10px] text-yellow-400 bg-yellow-900/20">[Mock 头部图]</div>
            )}
          </div>
        </div>
      )}

      {isCharacter && (
        <div className="space-y-1">
          <div className="flex items-center justify-between">
            <div className="text-[10px] text-octopus-muted">三视图（正/侧/背）</div>
            {!readOnly && (
              <UploadImageOverrideButton nodeId={node.id} field="three_view_image_url" label="上传替换" />
            )}
          </div>
          {threeViewUrl ? (
            <div className="rounded-lg overflow-hidden border border-octopus-border">
              <img
                src={threeViewUrl}
                alt="三视图"
                className="w-full h-auto"
                onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
              />
              {r.three_view_mock && (
                <div className="px-2 py-1 text-[10px] text-yellow-400 bg-yellow-900/20">[Mock 三视图]</div>
              )}
              {r.three_view_user_uploaded && (
                <div className="px-2 py-1 text-[10px] text-cyan-300 bg-cyan-900/20">📤 用户上传</div>
              )}
            </div>
          ) : (
            <div className="px-2 py-2 text-[10px] text-octopus-muted bg-octopus-bg/40 border border-dashed border-octopus-border rounded">
              {r.three_view_error ? `三视图生成失败：${safeSlice(r.three_view_error, 80)}` : '三视图未生成，可直接上传一张'}
            </div>
          )}
        </div>
      )}

      {/* 0.7.0 轮 2：角色参考图（face + 3-view 横向拼接）— 直接喂给关键帧/视频模型用 */}
      {isCharacter && r.composite_reference_url && (
        <div className="space-y-1">
          <div className="text-[10px] text-octopus-muted">
            🧩 角色参考图（脸 + 三视图，喂给关键帧/视频模型保一致性）
          </div>
          <div className="rounded-lg overflow-hidden border border-octopus-brand/50 bg-black/20">
            <img
              src={r.composite_reference_url}
              alt="composite reference"
              className="w-full h-auto"
              onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
            />
          </div>
        </div>
      )}

      <div className="text-[10px] text-octopus-muted">
        类型：<span className="text-octopus-text">
          {r.result_type === 'ep_character' ? '🎭 本集角色（含装束）' :
           r.result_type === 'ep_scene' ? '🏞️ 本集场景（变化）' :
           r.result_type === 'ep_prop' ? '🎁 本集道具（状态）' :
           isCharacter ? '🎭 角色' :
           r.result_type === 'prop' ? '🎁 道具' :
           '🏞️ 场景'}
        </span>
        {r.character_id && <span className="ml-2">ID: {r.character_id}</span>}
        {r.scene_id && <span className="ml-2">ID: {r.scene_id}</span>}
        {r.outfit_id && <span className="ml-2">装束 ID: {r.outfit_id}</span>}
        {r.variation_id && <span className="ml-2">变化 ID: {r.variation_id}</span>}
        {r.state_id && <span className="ml-2">状态 ID: {r.state_id}</span>}
      </div>
      {/* 0.7.0 轮 1：episode 子节点显示"继承自总美术"+ master 缩略图 */}
      {isEpisodeResult && r.parent_master && (
        <div className="flex items-center gap-2 p-2 rounded bg-octopus-bg/40 border border-dashed border-octopus-border">
          {r.parent_master.image_url && (
            <img
              src={r.parent_master.image_url}
              alt="master base"
              className="w-10 h-10 rounded object-cover border border-octopus-border flex-shrink-0"
              onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
            />
          )}
          <div className="flex-1 text-[10px] text-octopus-muted leading-tight">
            <div>📌 基于总美术「{r.parent_master.name || r.parent_master.location || r.parent_master.id}」细化</div>
            {r.outfit_when && <div className="text-octopus-text/70">出场时段：{r.outfit_when}</div>}
            {r.time_of_day && <div className="text-octopus-text/70">时间：{r.time_of_day}</div>}
            {r.lighting && <div className="text-octopus-text/70">光线：{r.lighting}</div>}
            {r.state_description && <div className="text-octopus-text/70">变化：{r.state_description}</div>}
          </div>
        </div>
      )}

      <Field label={isCharacter ? '角色名' : '场景地点'}>
        <TextInput value={name} onChange={(v) => { setName(v); setDirty(true) }} />
      </Field>
      <Field label="图像生成提示词" hint="修改后点重跑可重新出图">
        <TextArea value={prompt} rows={5} onChange={(v) => { setPrompt(v); setDirty(true) }} />
      </Field>

      {!readOnly && (
        <div className="flex gap-2">
          <button
            onClick={handleSave}
            disabled={!dirty || saving}
            className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded bg-green-600/80 hover:bg-green-500 disabled:opacity-40 text-white text-xs"
          >
            <Save className="w-3 h-3" />
            {saving ? '保存中...' : dirty ? '保存修改' : '已保存'}
          </button>
          {onRegenerate && (
            <button
              onClick={onRegenerate}
              className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded bg-octopus-bg hover:bg-white/5 text-octopus-text text-xs border border-octopus-border"
            >
              <RotateCcw className="w-3 h-3" />
              重新生成图片
            </button>
          )}
        </div>
      )}
    </div>
  )
}
