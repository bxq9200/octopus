/**
 * UploadImageOverrideButton — 上传图片直接替换节点 result 里的某个 image 字段
 *
 * 用于：用户对 AI 生成的角色立绘 / 三视图 / 场景图 / 关键帧不满意，想直接传一张自己的图
 * 流程：选图 → uploadAPI 拿到 URL → workflowAPI.replaceNodeImage 写回节点 result → 触发刷新
 */
import React, { useRef, useState } from 'react'
import { Upload, Loader2 } from 'lucide-react'
import { uploadAPI, workflowAPI } from '@/services/api'
import { useStore } from '@/store/useStore'

const ALLOWED = ['image/png', 'image/jpeg', 'image/jpg', 'image/webp', 'image/gif']
const MAX_SIZE = 8 * 1024 * 1024

export default function UploadImageOverrideButton({
  nodeId,
  field = 'image_url',
  label = '上传替换',
  onReplaced,
}: {
  nodeId: string
  field?: 'image_url' | 'three_view_image_url'
  label?: string
  onReplaced?: (imageUrl: string) => void
}) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [busy, setBusy] = useState(false)
  const currentProject = useStore((s) => s.currentProject)

  const handlePick = () => {
    if (busy || !currentProject?.id) return
    inputRef.current?.click()
  }

  const handleChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file || !currentProject?.id) return

    if (!ALLOWED.includes(file.type)) {
      alert(`只支持图片 (png/jpg/webp/gif)：${file.name}`)
      return
    }
    if (file.size > MAX_SIZE) {
      alert(`文件超过 8MB：${file.name}`)
      return
    }

    setBusy(true)
    try {
      // 1. 上传
      const upResp = await uploadAPI.uploadImages(currentProject.id, [file])
      const uploaded = (upResp.data?.attachments || [])[0]
      if (!uploaded?.url) {
        alert('上传失败：未拿到 URL')
        return
      }
      // 2. 写回节点 result
      await workflowAPI.replaceNodeImage(currentProject.id, nodeId, uploaded.url, field)
      onReplaced?.(uploaded.url)
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || String(err)
      alert(`替换失败：${detail}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept="image/png,image/jpeg,image/jpg,image/webp,image/gif"
        onChange={handleChange}
        className="hidden"
      />
      <button
        onClick={handlePick}
        disabled={busy || !currentProject?.id}
        title="上传一张自己的图，直接替换 AI 生成的结果（不消耗模型调用）"
        className="flex items-center gap-1 px-2 py-1 rounded border border-octopus-border bg-octopus-bg/40 hover:bg-octopus-bg text-octopus-text text-[10px] disabled:opacity-40"
      >
        {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Upload className="w-3 h-3" />}
        {busy ? '上传中...' : label}
      </button>
    </>
  )
}
