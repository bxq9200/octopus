import React from 'react'
import { ExternalLink } from 'lucide-react'

const URL_RE = /(https?:\/\/[^\s，。；、）)】]+)/g

/** 把一段说明文字里的 URL 自动渲染成可点链接。
 *  设置页/向导的 hint 都用它 —— 这样文案可以写成
 *  「去 https://xxx 注册 → 控制台 → API Keys → 新建」而不只是光秃秃一个链接。 */
export default function HintText({ text, className = '' }: { text: string; className?: string }) {
  if (!text) return null
  // split 带捕获组 → 结果交替为 [文本, URL, 文本, URL…]，直接判前缀即可
  // （不能用 URL_RE.test：全局正则有 lastIndex 状态，会隔一次漏判）
  const parts = text.split(URL_RE)
  return (
    <span className={className}>
      {parts.map((p, i) =>
        p.startsWith('http') ? (
          <a
            key={i}
            href={p}
            target="_blank"
            rel="noreferrer"
            className="text-cyan-400/90 hover:text-cyan-300 underline underline-offset-2 break-all inline-flex items-baseline gap-0.5"
          >
            {p}
            <ExternalLink className="w-2.5 h-2.5 shrink-0 self-center" />
          </a>
        ) : (
          <React.Fragment key={i}>{p}</React.Fragment>
        ),
      )}
    </span>
  )
}
