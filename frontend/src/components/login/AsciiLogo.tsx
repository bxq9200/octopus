/**
 * AsciiLogo — 章鱼 logo 的 ASCII 终端版
 *
 * 视觉层次（全部在 .ascii-logo__inner 内部，渐变全局同步）：
 *   1. 主体 <pre>，4 个触手末端"球"区域被挖空
 *   2. 4 个独立 <pre>，每个对应一个触手末端，独立做轻微摆动
 *   3. 两只眼睛绝对定位单字符覆盖，纯白 + 强辉光，按时间表眨眼
 *
 * 4 个触手区域用不同周期/相位/方向摆动，模拟水流推动的不同步漂浮。
 * 主体保持静止（避免之前"上下半身切分"那种诡异感）。
 *
 * 行高严格 1.0、字体严格等宽，所以 (col * 1ch, row * 1em) 精准对位。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react'
import asciiArt from './octopus.txt?raw'

// 眼睛在 ASCII 网格里的精确格子（0-indexed）
const EYE_ROW = 13.5
const EYE_LEFT_COL = 38
const EYE_RIGHT_COL = 51

// 4 个触手末端"球"区域 — (row, col) 为左上角，含 row 行 col 列起的 height × width 矩阵
type Region = { row: number; col: number; height: number; width: number; cls: string }
const TENTACLE_REGIONS: Region[] = [
  { row: 3,  col: 15, height: 9, width: 17, cls: 'tip--tl' },  // 头顶左侧上挑触手
  { row: 3,  col: 57, height: 9, width: 17, cls: 'tip--tr' },  // 头顶右侧上挑触手
  { row: 23, col: 7,  height: 7, width: 19, cls: 'tip--bl' },  // 左下角侧伸触手
  { row: 23, col: 65, height: 7, width: 18, cls: 'tip--br' },  // 右下角侧伸触手
]

const GRID_COLS = 90

type EyeFrame = 'open' | 'closing' | 'closed'
const EYE_LEFT = { open: '●', closing: '>', closed: '-' }
const EYE_RIGHT = { open: '●', closing: '<', closed: '-' }

/** 把 4 个触手区域从主体里挖出来，得到 main + 4 个 overlay 文本 */
function spliceArt(art: string, regions: Region[]) {
  const lines: string[][] = art.split('\n').map((l) => {
    const arr = l.split('')
    while (arr.length < GRID_COLS) arr.push(' ')
    return arr
  })
  const overlays: string[] = []
  for (const r of regions) {
    const segment: string[] = []
    for (let y = 0; y < r.height; y++) {
      const rowIdx = r.row + y
      const line = lines[rowIdx]
      if (!line) {
        segment.push(' '.repeat(r.width))
        continue
      }
      const out: string[] = []
      for (let x = 0; x < r.width; x++) {
        const colIdx = r.col + x
        const ch = line[colIdx] ?? ' '
        out.push(ch)
        if (ch !== ' ') line[colIdx] = ' '
      }
      segment.push(out.join(''))
    }
    overlays.push(segment.join('\n'))
  }
  const main = lines.map((l) => l.join('').replace(/\s+$/, '')).join('\n')
  return { main, overlays }
}

export default function AsciiLogo() {
  const [eye, setEye] = useState<EyeFrame>('open')
  const timers = useRef<number[]>([])

  const { main, overlays } = useMemo(() => spliceArt(asciiArt, TENTACLE_REGIONS), [])

  useEffect(() => {
    const clearAll = () => {
      timers.current.forEach((t) => window.clearTimeout(t))
      timers.current = []
    }
    const blink = () => {
      setEye('closing')
      timers.current.push(window.setTimeout(() => setEye('closed'), 60))
      timers.current.push(window.setTimeout(() => setEye('closing'), 170))
      timers.current.push(
        window.setTimeout(() => {
          setEye('open')
          timers.current.push(window.setTimeout(blink, 3000 + Math.random() * 3000))
        }, 230),
      )
    }
    timers.current.push(window.setTimeout(blink, 1500))
    return clearAll
  }, [])

  return (
    <div className="ascii-logo">
      <div className="ascii-logo__inner">
        <pre className="ascii-logo__body">{main}</pre>
        {overlays.map((o, i) => (
          <pre
            key={i}
            className={`ascii-logo__body ascii-logo__tip ${TENTACLE_REGIONS[i].cls}`}
            style={{
              top: `${TENTACLE_REGIONS[i].row}em`,
              left: `${TENTACLE_REGIONS[i].col}ch`,
            }}
          >
            {o}
          </pre>
        ))}
        <span
          className="ascii-logo__eye"
          style={{ top: `${EYE_ROW}em`, left: `${EYE_LEFT_COL}ch` }}
        >
          {EYE_LEFT[eye]}
        </span>
        <span
          className="ascii-logo__eye"
          style={{ top: `${EYE_ROW}em`, left: `${EYE_RIGHT_COL}ch` }}
        >
          {EYE_RIGHT[eye]}
        </span>
      </div>
    </div>
  )
}
