/**
 * 极简动画小章鱼 — 用作 chat agent 头像
 * 头部呼吸（轻微缩放 + 透明度），触须左右摇摆
 */
import React from 'react'

export default function OctopusIcon({
  size = 20,
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
      className={`octopus-icon ${className}`}
    >
      {/* 触须层（先画，被头部盖在上面） */}
      <g className="octopus-tentacles">
        {/* 左外 */}
        <path
          d="M 8 19 Q 5 23 7 28"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          fill="none"
          className="tentacle t1"
        />
        {/* 左内 */}
        <path
          d="M 12 21 Q 11 25 13 29"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          fill="none"
          className="tentacle t2"
        />
        {/* 中 */}
        <path
          d="M 16 22 Q 16 26 16 30"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          fill="none"
          className="tentacle t3"
        />
        {/* 右内 */}
        <path
          d="M 20 21 Q 21 25 19 29"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          fill="none"
          className="tentacle t4"
        />
        {/* 右外 */}
        <path
          d="M 24 19 Q 27 23 25 28"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          fill="none"
          className="tentacle t5"
        />
      </g>

      {/* 头部 */}
      <g className="octopus-head">
        <path
          d="M 6 16 Q 6 6 16 6 Q 26 6 26 16 Q 26 21 22 22 L 10 22 Q 6 21 6 16 Z"
          fill="currentColor"
          opacity="0.95"
        />
        {/* 眼白 */}
        <circle cx="12" cy="14" r="2.2" fill="#0a0a0f" />
        <circle cx="20" cy="14" r="2.2" fill="#0a0a0f" />
        {/* 高光眼珠 */}
        <circle cx="12.6" cy="13.6" r="0.9" fill="#fff" className="octopus-eye" />
        <circle cx="20.6" cy="13.6" r="0.9" fill="#fff" className="octopus-eye" />
      </g>
    </svg>
  )
}
