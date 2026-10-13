/**
 * 创作者动画头像 — 戴贝雷帽的"小导演"
 * 头部轻微点头（思考姿态）+ 眼睛偶尔眨 + 帽尖轻摆
 */
import React from 'react'

export default function DirectorIcon({
  size = 22,
  className = '',
}: {
  size?: number
  className?: string
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={`director-icon ${className}`}
    >
      {/* 整体随节奏轻微点头 */}
      <g className="director-body">
        {/* 肩膀 / 衣领 */}
        <path
          d="M 4 30 Q 4 22 16 22 Q 28 22 28 30 Z"
          fill="currentColor"
          opacity="0.85"
        />
        {/* 衣领 V 字 */}
        <path
          d="M 13 22 L 16 26 L 19 22"
          stroke="#0a0a0f"
          strokeWidth="1"
          fill="none"
          opacity="0.6"
        />

        {/* 脸 */}
        <circle cx="16" cy="15" r="6.2" fill="currentColor" />

        {/* 眼睛 */}
        <ellipse cx="13.5" cy="15" rx="0.9" ry="1.4" fill="#0a0a0f" className="director-eye" />
        <ellipse cx="18.5" cy="15" rx="0.9" ry="1.4" fill="#0a0a0f" className="director-eye" />

        {/* 嘴 — 微笑 */}
        <path
          d="M 14 18 Q 16 19.2 18 18"
          stroke="#0a0a0f"
          strokeWidth="0.9"
          strokeLinecap="round"
          fill="none"
        />

        {/* 贝雷帽 */}
        <g className="director-beret">
          {/* 帽顶 */}
          <ellipse cx="16" cy="9" rx="7" ry="3.2" fill="currentColor" opacity="0.95" />
          {/* 帽檐边线 */}
          <path
            d="M 9 10 Q 16 13 23 10"
            stroke="#0a0a0f"
            strokeWidth="0.6"
            fill="none"
            opacity="0.4"
          />
          {/* 帽尖小揪揪（轻摆） */}
          <circle cx="11.5" cy="6.5" r="1" fill="currentColor" className="director-pom" />
        </g>
      </g>
    </svg>
  )
}
