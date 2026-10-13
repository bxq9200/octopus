/**
 * LoginPage — 终端风格登录页
 *
 * 视觉层次：
 *   1. 黑色全屏背景 + 极轻 CRT 扫描线 + 中心 vignette
 *   2. 中央"终端窗口"卡片：交通灯按钮 + 标题栏
 *   3. 卡片正中：动画 ASCII 章鱼 logo（AsciiLogo）
 *   4. 下方品牌字"OCTOPUS · AI VIDEO CREATION OS"
 *   5. 启动日志打字机：依次浮现 booting / loading / ready
 *   6. 启动日志结束后，账号密码 prompt 出现，光标闪
 *
 * 0.8 缝5：登录改走后端 /api/auth/login。
 *   local 模式语义与之前一致——任意非空账号密码通过；差别是判断在后端、会话有真实落点，
 *   token 由后端签发（不再是前端自己拼的 mock 串），并被 api.ts 拦截器统一带上。
 *   cloud_client 的真实云端登录在 0.9；后端会回 501 + 可读话术，这里直接显示。
 */
import React, { useEffect, useRef, useState } from 'react'
import AsciiLogo from './AsciiLogo'
import asciiTitle from './octopus_title.txt?raw'
import { authAPI, TOKEN_KEY } from '../../services/api'

const BOOT_LINES = [
  { delay: 200,  text: '> octopus init...',                        suffix: '[ ok ]' },
  { delay: 350,  text: '> loading agents (director, screenwriter, art_director, storyboard)...', suffix: '[ ok ]' },
  { delay: 320,  text: '> mounting modules (text_to_image, image_to_video, video_merge)...', suffix: '[ ok ]' },
  { delay: 280,  text: '> connecting kimi / 智谱 / seedance...',   suffix: '[ ok ]' },
  { delay: 260,  text: '> ready.',                                  suffix: '' },
]

type BootEntry = { text: string; suffix: string }

type Mode = 'local' | 'cloud_client'

export default function LoginPage({ onLogin }: { onLogin: (user: string) => void }) {
  const [bootLines, setBootLines] = useState<BootEntry[]>([])
  const [bootDone, setBootDone] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [focused, setFocused] = useState<'user' | 'pass' | null>('user')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const usernameRef = useRef<HTMLInputElement>(null)
  const passwordRef = useRef<HTMLInputElement>(null)

  // 0.9：部署模式决定登录方式。local=账号密码（本机不鉴权）；cloud_client=邮箱验证码
  const [mode, setMode] = useState<Mode>('local')
  const [codeSent, setCodeSent] = useState(false)
  const [cooldown, setCooldown] = useState(0)
  const codeRef = useRef<HTMLInputElement>(null)
  const isCloud = mode === 'cloud_client'

  useEffect(() => {
    let cancelled = false
    authAPI.status()
      .then(({ data }) => { if (!cancelled && data?.mode) setMode(data.mode as Mode) })
      .catch(() => { /* 后端没起也别卡住登录页，按 local 渲染 */ })
    return () => { cancelled = true }
  }, [])

  // 重发验证码倒计时
  useEffect(() => {
    if (cooldown <= 0) return
    const t = window.setTimeout(() => setCooldown((c) => c - 1), 1000)
    return () => window.clearTimeout(t)
  }, [cooldown])

  // 启动日志依次浮现
  useEffect(() => {
    let cancelled = false
    let acc = 600
    const timers: number[] = []
    BOOT_LINES.forEach((line) => {
      acc += line.delay
      timers.push(
        window.setTimeout(() => {
          if (cancelled) return
          setBootLines((prev) => [...prev, { text: line.text, suffix: line.suffix }])
        }, acc),
      )
    })
    timers.push(
      window.setTimeout(() => {
        if (cancelled) return
        setBootDone(true)
        // 自动 focus 到账号输入
        window.setTimeout(() => usernameRef.current?.focus(), 50)
      }, acc + 220),
    )
    return () => {
      cancelled = true
      timers.forEach((t) => window.clearTimeout(t))
    }
  }, [])

  const errText = (err: any, fallback: string) =>
    err?.response?.data?.detail || (err?.response ? fallback : 'backend unreachable — 后端未启动？')

  // cloud_client 第一步：请云端发验证码到邮箱
  const handleSendCode = async () => {
    if (submitting || cooldown > 0) return
    const email = username.trim()
    if (!email) { setError('email required'); return }
    setError(null)
    setSubmitting(true)
    try {
      await authAPI.sendCode(email)
      setCodeSent(true)
      setCooldown(60)                       // 与云端 60s 冷却对齐，避免用户白点
      window.setTimeout(() => codeRef.current?.focus(), 50)
    } catch (err: any) {
      setError(errText(err, 'send code failed'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleSubmit = async (e?: React.FormEvent) => {
    e?.preventDefault()
    if (submitting) return

    // ── cloud_client：邮箱 + 验证码 ──
    if (isCloud) {
      const email = username.trim()
      const code = password.trim()
      if (!email) { setError('email required'); return }
      if (!codeSent) { await handleSendCode(); return }   // 还没发码 → 先发
      if (!code) { setError('verification code required'); return }
      setError(null)
      setSubmitting(true)
      try {
        const { data } = await authAPI.verify(email, code)
        localStorage.setItem(TOKEN_KEY, data.token)
        localStorage.setItem('octopus_user', data.user?.email || email)
        onLogin(data.user?.email || email)
      } catch (err: any) {
        setError(errText(err, 'verify failed'))
        setSubmitting(false)
      }
      return
    }

    // ── local：账号密码（本机不鉴权，任意非空即可）──
    if (!username.trim() || !password.trim()) {
      setError('username / password required')
      return
    }
    setError(null)
    setSubmitting(true)
    // 假装"鉴权中"延迟，更有终端感
    await new Promise((r) => setTimeout(r, 400))
    try {
      const { data } = await authAPI.login(username.trim(), password.trim())
      localStorage.setItem(TOKEN_KEY, data.token)
      localStorage.setItem('octopus_user', data.user || username.trim())
      onLogin(data.user || username.trim())
    } catch (err: any) {
      setError(errText(err, 'login failed'))
      setSubmitting(false)
    }
  }

  return (
    <div className="relative w-screen h-screen overflow-hidden bg-black text-octopus-text">
      {/* 终端窗口 */}
      <div className="relative z-10 w-full h-full flex flex-col items-center justify-center px-4 py-6">
        <div className="w-full max-w-[920px] bg-[#0a0a0f]/90 border border-white/10 rounded-xl shadow-[0_0_60px_rgba(99,102,241,0.18)] overflow-hidden backdrop-blur-sm">
          {/* 标题栏 */}
          <div className="flex items-center gap-2 px-4 py-2.5 border-b border-white/10 bg-black/40">
            <span className="w-3 h-3 rounded-full bg-red-500/80" />
            <span className="w-3 h-3 rounded-full bg-yellow-500/80" />
            <span className="w-3 h-3 rounded-full bg-green-500/80" />
            <div className="flex-1 text-center text-xs text-white/50 font-mono select-none">
              octopus@local — /usr/local/bin/octopus-login
            </div>
            <div className="w-12" />
          </div>

          {/* 终端正文 */}
          <div className="px-6 py-6 font-mono text-sm flex flex-col items-center gap-4">
            {/* ASCII Logo */}
            <AsciiLogo />

            {/* 品牌字（ASCII 渲染版，跟章鱼用同一套渐变） */}
            <pre className="title-ascii mt-1 mb-2">{asciiTitle}</pre>

            {/* 启动日志 + login prompt */}
            <div className="w-full max-w-[640px] mt-2 space-y-1 text-[13px]">
              {bootLines.map((line, i) => (
                <div key={i} className="flex items-center text-white/70">
                  <span className="flex-1 truncate">{line.text}</span>
                  {line.suffix && (
                    <span className="ml-3 flex-shrink-0 text-green-400/90">{line.suffix}</span>
                  )}
                </div>
              ))}

              {/* 还没 boot 完时，最后一行带光标 */}
              {!bootDone && (
                <div className="flex items-center text-white/70">
                  <span className="terminal-cursor text-cyan-400">&nbsp;</span>
                </div>
              )}

              {/* boot 完毕：登录 prompt */}
              {bootDone && (
                <form onSubmit={handleSubmit} className="pt-3 space-y-1.5">
                  <PromptLine
                    label={isCloud ? 'email' : 'username'}
                    value={username}
                    setValue={setUsername}
                    focused={focused === 'user'}
                    inputRef={usernameRef}
                    onFocus={() => setFocused('user')}
                    onBlur={() => setFocused((f) => (f === 'user' ? null : f))}
                    onEnter={() => (isCloud && !codeSent ? handleSendCode() : passwordRef.current?.focus())}
                    type="text"
                  />
                  {/* cloud 模式下，验证码框在发码之后才出现 —— 免得用户对着空框发呆 */}
                  {(!isCloud || codeSent) && (
                    <PromptLine
                      label={isCloud ? 'code' : 'password'}
                      value={password}
                      setValue={setPassword}
                      focused={focused === 'pass'}
                      inputRef={isCloud ? codeRef : passwordRef}
                      onFocus={() => setFocused('pass')}
                      onBlur={() => setFocused((f) => (f === 'pass' ? null : f))}
                      onEnter={() => handleSubmit()}
                      type={isCloud ? 'text' : 'password'}
                    />
                  )}
                  {error && (
                    <div className="text-red-400/90 text-xs pt-1">! {error}</div>
                  )}
                  {isCloud && codeSent && !error && (
                    <div className="text-green-400/80 text-xs pt-1">
                      验证码已发至 {username.trim()}，10 分钟内有效
                    </div>
                  )}
                  <div className="flex items-center gap-3 pt-2">
                    <button
                      type="submit"
                      disabled={submitting}
                      className="px-4 py-1.5 rounded border border-cyan-400/40 bg-cyan-400/10 hover:bg-cyan-400/20 hover:border-cyan-400/70 text-cyan-300 text-xs tracking-widest transition-colors disabled:opacity-40"
                    >
                      {submitting
                        ? (isCloud && !codeSent ? '> sending...' : '> authenticating...')
                        : isCloud
                          ? (codeSent ? '> verify [ enter ]' : '> send code [ enter ]')
                          : '> login [ enter ]'}
                    </button>
                    {isCloud && codeSent && (
                      <button
                        type="button"
                        onClick={handleSendCode}
                        disabled={submitting || cooldown > 0}
                        className="px-3 py-1.5 rounded border border-white/15 text-white/50 text-xs tracking-wider hover:border-white/30 hover:text-white/70 transition-colors disabled:opacity-30"
                      >
                        {cooldown > 0 ? `resend ${cooldown}s` : 'resend'}
                      </button>
                    )}
                    <span className="text-[10px] text-white/30">
                      {isCloud
                        ? '云端模式：用邮箱收验证码登录'
                        : '本地模式：本机不鉴权，任意账号密码均可进入'}
                    </span>
                  </div>
                </form>
              )}
            </div>
          </div>
        </div>

        <div className="mt-4 text-[10px] text-white/30 font-mono tracking-wider select-none">
          v1.0.0 · © octopus
        </div>
      </div>

      {/* CRT 装饰层 */}
      <div className="crt-scanlines" />
      <div className="crt-vignette" />
    </div>
  )
}

function PromptLine({
  label,
  value,
  setValue,
  focused,
  inputRef,
  onFocus,
  onBlur,
  onEnter,
  type,
}: {
  label: string
  value: string
  setValue: (v: string) => void
  focused: boolean
  inputRef: React.RefObject<HTMLInputElement>
  onFocus: () => void
  onBlur: () => void
  onEnter: () => void
  type: 'text' | 'password'
}) {
  return (
    <label className="flex items-center gap-2 cursor-text" onClick={() => inputRef.current?.focus()}>
      <span className="text-cyan-400/90 select-none">$</span>
      <span className="text-white/60 select-none">{label}:</span>
      <span className="relative flex-1 min-w-0">
        <input
          ref={inputRef}
          type={type}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onFocus={onFocus}
          onBlur={onBlur}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              onEnter()
            }
          }}
          spellCheck={false}
          autoComplete="off"
          className="w-full bg-transparent border-0 outline-none text-white/90 font-mono caret-transparent"
          // caret-transparent 让浏览器原生光标隐藏，下面那个手绘块状光标接管
        />
        {focused && (
          <span
            className="terminal-cursor absolute pointer-events-none text-cyan-300"
            style={{
              left: `${Math.min(value.length, 32)}ch`,
              top: 0,
              bottom: 0,
              height: '1em',
            }}
          >
            &nbsp;
          </span>
        )}
      </span>
    </label>
  )
}
