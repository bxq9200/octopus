/**
 * MentionTextArea — 在 textarea 中输入 @ 时弹出候选列表，选中后插入 @<名字>
 */
import React, { useEffect, useRef, useState } from 'react'

export interface MentionItem {
  /** 真实写入文本的名字（@后这部分） */
  value: string
  /** 显示给用户的标签 */
  label: string
  /** 分组：character / scene / prop，仅用于显示分组头 */
  group?: 'character' | 'scene' | 'prop'
  /** 用于预览的辅助文本 */
  hint?: string
  /** 缩略图 */
  imageUrl?: string
}

// 0.7.1 ①：与后端 _build_video_payload._aliases 对齐 —— 剥离括号注释 + 拆分隔符，
// 让 @小峰 命中 "小峰（林悦老公）"、@麻糬 命中 "麻糬 / Mochi"
export function mentionAliases(name: string): string[] {
  if (!name) return []
  const out: string[] = []
  const push = (s: string) => { const t = s.trim(); if (t && !out.includes(t)) out.push(t) }
  const base = name.trim()
  for (const cand of [base, ...base.split(/[/／,，、·・•]/)]) {
    push(cand)
    push(cand.replace(/[（(][^）)]*[）)]/g, ''))
  }
  return out
}

interface Props {
  value: string
  onChange: (v: string) => void
  rows?: number
  placeholder?: string
  items: MentionItem[]
}

// 计算 textarea 中光标位置之前的 token：如果存在未闭合的 @xxx，返回 [起始位置, 查询词]
function getActiveMention(text: string, caret: number): { start: number; query: string } | null {
  // 从光标向前找到最近的 @
  let i = caret - 1
  while (i >= 0) {
    const ch = text[i]
    if (ch === '@') {
      const start = i
      const query = text.slice(start + 1, caret)
      // 不允许 query 包含空白或换行（说明 @ 已经"结束"了）
      if (/\s/.test(query)) return null
      return { start, query }
    }
    if (/\s/.test(ch)) return null
    i--
  }
  return null
}

export default function MentionTextArea({ value, onChange, rows = 3, placeholder, items }: Props) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [mentionStart, setMentionStart] = useState<number>(-1)
  const [activeIdx, setActiveIdx] = useState(0)

  const filtered = items.filter((it) => {
    if (!query) return true
    const q = query.toLowerCase()
    return it.value.toLowerCase().includes(q) || it.label.toLowerCase().includes(q)
  })

  // 光标位置变化时检测是否在 @ token 中
  const refresh = () => {
    const ta = textareaRef.current
    if (!ta) return
    const caret = ta.selectionStart ?? 0
    const m = getActiveMention(value, caret)
    if (m) {
      setOpen(true)
      setQuery(m.query)
      setMentionStart(m.start)
      setActiveIdx(0)
    } else {
      setOpen(false)
      setMentionStart(-1)
    }
  }

  useEffect(() => {
    // value 变化也可能是外部更新，重新检测
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])

  const insertMention = (item: MentionItem) => {
    if (mentionStart < 0) return
    const ta = textareaRef.current
    const caret = ta?.selectionStart ?? mentionStart + 1 + query.length
    const before = value.slice(0, mentionStart)
    const after = value.slice(caret)
    const insert = `@${item.value}`
    const next = before + insert + after
    onChange(next)
    setOpen(false)
    setMentionStart(-1)
    // 把光标移到插入文本之后
    requestAnimationFrame(() => {
      if (ta) {
        const pos = before.length + insert.length
        ta.focus()
        ta.setSelectionRange(pos, pos)
      }
    })
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (!open || filtered.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx((i) => (i + 1) % filtered.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx((i) => (i - 1 + filtered.length) % filtered.length)
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      insertMention(filtered[activeIdx])
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  // 简单分组（character 在前，scene 中，prop 后）
  const charItems = filtered.filter((it) => it.group === 'character')
  const sceneItems = filtered.filter((it) => it.group === 'scene')
  const propItems = filtered.filter((it) => it.group === 'prop')
  const others = filtered.filter((it) => !it.group || (it.group !== 'character' && it.group !== 'scene' && it.group !== 'prop'))

  // 在文本中识别 @xxx → 配色高亮
  // 注册名可能含空格（如 "The Jumper"）/ 斜杠（"Locker room / preparation zone"），
  // 单纯 \w+ 正则会切到第一个空格就停，导致只识别 "@The"。
  // 解决：把已注册名按长度倒序拼成 alternation 优先匹配，没匹配到的 fallback 单词正则。
  const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  // 0.7.1 ①：按别名（剥括号/拆分隔符）建 alias→item，@小峰 也能命中 "小峰（林悦老公）"
  const itemByValue = new Map<string, MentionItem>()
  for (const it of items) {
    for (const a of mentionAliases(it.value)) {
      if (!itemByValue.has(a)) itemByValue.set(a, it)
    }
  }
  const knownNames = Array.from(itemByValue.keys()).filter(Boolean).sort((a, b) => b.length - a.length)
  const knownPattern = knownNames.length
    ? new RegExp(`@(${knownNames.map(escapeRe).join('|')}|[\\w一-鿿]+)`, 'g')
    : new RegExp(`@([\\w一-鿿]+)`, 'g')
  const highlighted: React.ReactNode[] = []
  if (value) {
    const re = knownPattern
    re.lastIndex = 0
    let last = 0
    let m: RegExpExecArray | null
    let key = 0
    while ((m = re.exec(value)) !== null) {
      if (m.index > last) {
        highlighted.push(<span key={`p-${key++}`} className="text-octopus-muted/80">{value.slice(last, m.index)}</span>)
      }
      const name = m[1]
      const ref = itemByValue.get(name)
      const isChar = ref?.group === 'character'
      const isScene = ref?.group === 'scene'
      const isProp = ref?.group === 'prop'
      const cls = isChar
        ? 'bg-purple-500/30 text-purple-200 border border-purple-400/50'
        : isScene
        ? 'bg-emerald-500/30 text-emerald-200 border border-emerald-400/50'
        : isProp
        ? 'bg-amber-500/30 text-amber-200 border border-amber-400/50'
        : 'bg-red-500/20 text-red-300 border border-red-400/40 line-through'  // 未匹配
      highlighted.push(
        <span key={`m-${key++}`} className={`inline-block px-1 rounded ${cls}`} title={ref?.hint || (ref ? '' : '未匹配，请确认名字拼写')}>
          @{name}
        </span>,
      )
      last = m.index + m[0].length
    }
    if (last < value.length) {
      highlighted.push(<span key={`p-${key++}`} className="text-octopus-muted/80">{value.slice(last)}</span>)
    }
  }
  const hasMentions = /@[\w一-鿿]+/.test(value || '')

  return (
    <div className="relative">
      <textarea
        ref={textareaRef}
        value={value ?? ''}
        rows={rows}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        onClick={refresh}
        onKeyUp={refresh}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        className="bg-octopus-bg border border-octopus-border rounded px-2 py-1.5 text-sm text-octopus-text w-full focus:border-octopus-primary outline-none resize-y"
      />

      {hasMentions && (
        <div className="mt-1 px-2 py-1 rounded bg-octopus-bg/40 border border-octopus-border/50 text-[11px] leading-relaxed">
          <span className="text-[10px] text-octopus-muted mr-1">识别到的引用:</span>
          {highlighted}
        </div>
      )}
      {open && filtered.length > 0 && (
        <div
          className="absolute left-0 right-0 mt-1 z-30 bg-octopus-panel border border-octopus-border rounded-lg shadow-xl max-h-[260px] overflow-y-auto"
          onMouseDown={(e) => e.preventDefault() /* 阻止 textarea 失焦 */}
        >
          {[
            { title: '🎭 角色', list: charItems },
            { title: '🏞️ 场景', list: sceneItems },
            { title: '🎁 道具', list: propItems },
            { title: '其他', list: others },
          ].map(({ title, list }) =>
            list.length === 0 ? null : (
              <div key={title}>
                <div className="px-2 py-1 text-[10px] uppercase text-octopus-muted bg-octopus-bg/50">{title}</div>
                {list.map((item) => {
                  const idx = filtered.indexOf(item)
                  const active = idx === activeIdx
                  return (
                    <button
                      key={`${item.group ?? '_'}-${item.value}`}
                      type="button"
                      onClick={() => insertMention(item)}
                      onMouseEnter={() => setActiveIdx(idx)}
                      className={`w-full text-left flex items-center gap-2 px-2 py-1.5 text-sm ${
                        active ? 'bg-octopus-primary/30 text-octopus-text' : 'text-octopus-chat hover:bg-octopus-border'
                      }`}
                    >
                      {item.imageUrl && (
                        <img
                          src={item.imageUrl}
                          alt=""
                          className="w-7 h-7 object-cover rounded flex-shrink-0"
                          onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
                        />
                      )}
                      <div className="flex-1 min-w-0">
                        <div className="text-octopus-text truncate">@{item.value}</div>
                        {item.hint && <div className="text-[10px] text-octopus-muted truncate">{item.hint}</div>}
                      </div>
                    </button>
                  )
                })}
              </div>
            ),
          )}
        </div>
      )}
    </div>
  )
}
