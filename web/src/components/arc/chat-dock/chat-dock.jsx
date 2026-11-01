/* 悬浮聊天窗（ODDD）。
   能力：拖动移动、折叠到左下角设置按钮上方（弹性粘性动画）、玻璃材质。
   只显示气泡对话——思考过程与工具调用不展示给用户。
   消息/发送走 App 注入的 onSend（WS agent_action），本组件不碰网络。 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { motion, useReducedMotion } from "motion/react"
import {
  ArrowUp, ChevronDown, Paperclip, Globe, Image as ImageIcon, Loader2, Square,
} from "lucide-react"
import styles from "./chat-dock.module.css"

const WIDTH = 320
const HEIGHT = 468
const MARGIN = 14
const STORAGE_KEY = "oddd.chat.dock.v1"

const STATUS_TEXT = { queued: "排队中", running: "正在处理", failed: "回复失败", cancelled: "已停止" }

const loadPos = () => {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}")
    if (Number.isFinite(saved.x) && Number.isFinite(saved.y)) return { x: saved.x, y: saved.y }
  } catch { /* 忽略损坏的本地设置 */ }
  return null
}

/** 默认停在右下角（面板右下、不压住第三行列表） */
const defaultPos = () => {
  if (typeof window === "undefined") return { x: 0, y: 0 }
  return {
    x: window.innerWidth - WIDTH - MARGIN,
    y: window.innerHeight - HEIGHT - MARGIN,
  }
}

const LAUNCHER = 46

/** 折叠后的落点：左下角设置圆按钮的正上方。
    必须等布局完成才有真实 rect——首次渲染时 .glass-wrap 还没排版，
    getBoundingClientRect 全是 0，退回兜底值会和设置按钮重叠。 */
const collapsedTarget = () => {
  const anchor = document.querySelector(".glass-wrap")
  const box = anchor?.getBoundingClientRect()
  const hasLayout = box && box.height > 0 && box.top > 0
  if (!hasLayout) return null
  return {
    x: Math.round(box.left + box.width / 2 - LAUNCHER / 2),
    y: Math.round(box.top - LAUNCHER - 10),
  }
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value))
}

function MessageBubble({ message }) {
  const mine = message.role === "user"
  const text = typeof message.content === "string" ? message.content : ""
  const busy = message.status === "running" || message.status === "queued"
  return (
    <div className={styles.bubbleRow} data-role={mine ? "user" : "bot"}>
      <div className={styles.bubble} data-role={mine ? "user" : "bot"}>
        {text ? <p className={styles.text}>{text}</p> : null}
        {busy && (
          <span className={styles.pending}>
            <Loader2 size={11} className={styles.spin} aria-hidden />
            {STATUS_TEXT[message.status] || "正在处理"}
          </span>
        )}
        {message.status === "failed" && (
          <p className={styles.error} role="alert">{message.error || STATUS_TEXT.failed}</p>
        )}
        {message.status === "cancelled" && <span className={styles.muted}>已停止</span>}
      </div>
    </div>
  )
}

export default function ChatDock({ messages = [], onSend, connected = true, onCancel, busy = false }) {
  const reduced = useReducedMotion()
  const [open, setOpen] = useState(false)
  const [prompt, setPrompt] = useState("")
  const [pos, setPos] = useState(() => loadPos() || defaultPos())
  const [sending, setSending] = useState(false)
  const scrollRef = useRef(null)
  const inputRef = useRef(null)
  const [launcherSpot, setLauncherSpot] = useState(() => ({ x: pos.x, y: pos.y }))
  const dragging = useRef(false)
  const [draggingUi, setDraggingUi] = useState(false)

  // 位置持久化：拖动结束才写，避免拖拽过程中每帧写 localStorage
  const commitPos = useCallback((nx, ny) => {
    const next = {
      x: clamp(nx, 0, Math.max(0, window.innerWidth - WIDTH)),
      y: clamp(ny, 0, Math.max(0, window.innerHeight - 56)),
    }
    setPos(next)
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(next)) } catch { /* 隐私模式忽略 */ }
  }, [])

  // 折叠落点：布局就绪后测量，并在窗口尺寸变化时重测
  useEffect(() => {
    if (open) return
    const measure = () => {
      const spot = collapsedTarget()
      if (spot) setLauncherSpot(spot)
    }
    measure()
    const raf = requestAnimationFrame(measure)
    window.addEventListener("resize", measure)
    return () => { cancelAnimationFrame(raf); window.removeEventListener("resize", measure) }
  }, [open])

  /** 手写拖拽：pointer 事件绑在 window 上，松手才落盘位置。
      不用 motion 的 drag —— 它在本页（大量 canvas / 浮层）里事件接管不稳定。 */
  const startDrag = useCallback((event) => {
    if (event.button !== 0) return
    if (event.target.closest("button")) return      // 收起按钮不触发拖拽
    event.preventDefault()
    const startX = event.clientX
    const startY = event.clientY
    const originX = pos.x
    const originY = pos.y
    dragging.current = true
    setDraggingUi(true)
    document.body.style.userSelect = "none"

    // 拖拽过程中的实时位置只进 ref，松手才落一次 state + localStorage
    let latest = { x: originX, y: originY }
    const onMove = (moveEvent) => {
      latest = { x: originX + (moveEvent.clientX - startX), y: originY + (moveEvent.clientY - startY) }
      setPos(latest)
    }
    const onUp = () => {
      dragging.current = false
      setDraggingUi(false)
      document.body.style.userSelect = ""
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onUp)
      window.removeEventListener("pointercancel", onUp)
      commitPos(latest.x, latest.y)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onUp)
    window.addEventListener("pointercancel", onUp)
  }, [commitPos, pos.x, pos.y])

  // 窗口尺寸变化时把窗口收回可视区
  useEffect(() => {
    const onResize = () => commitPos(pos.x, pos.y)
    window.addEventListener("resize", onResize)
    return () => window.removeEventListener("resize", onResize)
  }, [commitPos, pos.x, pos.y])

  // 展开后聚焦输入框；新消息贴底
  useLayoutEffect(() => {
    if (!open) return
    inputRef.current?.focus({ preventScroll: true })
  }, [open])

  useLayoutEffect(() => {
    const node = scrollRef.current
    if (!node || !open) return
    node.scrollTop = node.scrollHeight
  }, [messages, open])

  const send = async () => {
    const text = prompt.trim()
    if (!text || sending || !connected) return
    setSending(true)
    try {
      await onSend?.({ type: "agent_action", action: "chat", payload: { prompt: text } })
      setPrompt("")
    } finally {
      setSending(false)
    }
  }

  const onKeyDown = (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      send()
    }
  }

  // 折叠：先同步算好落点再卸载窗口，否则按钮会在"上一帧的落点"起步
  const collapse = () => {
    const spot = collapsedTarget()
    if (spot) setLauncherSpot(spot)
    setOpen(false)
  }

  // 折叠态：一个圆形玻璃按钮，落在设置按钮正上方
  if (!open) {
    const spot = launcherSpot
    return createPortal(
      <button key={`launcher-${pos.x}-${pos.y}`} type="button" className={styles.launcher}
        style={{
          left: spot.x,
          top: spot.y,
          "--from-x": `${pos.x}px`,
          "--from-y": `${pos.y}px`,
          "--spot-x": `${spot.x}px`,
          "--spot-y": `${spot.y}px`,
        }}
        onClick={() => setOpen(true)}
        aria-label="打开 ODDD 对话"
        title="ODDD 对话">
        <img src="/icon-32.png" alt="" width={22} height={22} />
        {busy && <span className={styles.launcherDot} aria-hidden />}
      </button>,
      document.body,
    )
  }

  return createPortal(
    <motion.section
      className={styles.dock}
      style={{ position: "fixed", left: pos.x, top: pos.y, width: WIDTH, height: HEIGHT }}
      aria-label="ODDD 对话"
      data-dragging={draggingUi || undefined}>
      <div className={styles.shell}>
        <header className={styles.header} title="拖动可移动窗口"
          onPointerDown={startDrag}>
          <span className={styles.identity}>
            <img src="/icon-32.png" alt="" width={18} height={18} />
            <b>ODDD</b>
            {busy && <span className={styles.busyDot} aria-label="正在处理" />}
          </span>
          <button type="button" className={styles.collapse} aria-label="收起对话"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={collapse}>
            <ChevronDown size={15} />
          </button>
        </header>

        <div className={styles.thread} ref={scrollRef}>
          {messages.length === 0 && <p className={styles.empty}>问市场、持仓或系统。</p>}
          {messages.map((message) => <MessageBubble key={message.id} message={message} />)}
        </div>

        <div className={styles.composer}>
          <div className={styles.composerInner}>
            <textarea
              ref={inputRef}
              className={styles.input}
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder="向 ODDD 提问…"
              rows={1}
              maxLength={6000}
              aria-label="输入消息"
            />
            <div className={styles.options}>
              <div className={styles.btnsAdd}>
                <button type="button" aria-label="附加文件" title="附加文件（未接入）" disabled>
                  <Paperclip size={17} />
                </button>
                <button type="button" aria-label="添加图片" title="添加图片（未接入）" disabled>
                  <ImageIcon size={17} />
                </button>
                <button type="button" aria-label="联网搜索" title="联网搜索（未接入）" disabled>
                  <Globe size={17} />
                </button>
              </div>
              {busy
                ? <button type="button" className={styles.stop} onClick={() => onCancel?.()}
                    aria-label="停止回复" title="停止回复"><Square size={12} fill="currentColor" /></button>
                : <button type="button" className={styles.submit} onClick={send}
                    disabled={!prompt.trim() || sending || !connected}
                    aria-label="发送" title={connected ? "发送" : "未连接服务端"}>
                    {sending ? <Loader2 size={15} className={styles.spin} /> : <ArrowUp size={15} strokeWidth={2.6} />}
                  </button>}
            </div>
          </div>
        </div>
      </div>
    </motion.section>,
    document.body,
  )
}
