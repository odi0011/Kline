/* 悬浮聊天窗（ODDD）。
   能力：拖动移动、折叠到左下角设置按钮上方（弹性粘性动画）、玻璃材质。
   只显示气泡对话——思考过程与工具调用不展示给用户。
   消息/发送走 App 注入的 onSend（WS agent_action），本组件不碰网络。 */
import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { motion, useReducedMotion } from "motion/react"
import { ChevronLeft, FileText, Loader2, MoreHorizontal } from "lucide-react"
import ChatComposer from "./chat-composer"
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

/** 附件引用归一：新版是对象（图片带 url），旧版存的是纯字符串名字 */
const asRef = (raw) => (typeof raw === "string"
  ? { kind: "text", name: raw }
  : (raw && typeof raw === "object" ? raw : null))

const fmtBytes = (bytes) => {
  const n = Number(bytes)
  if (!Number.isFinite(n) || n <= 0) return ""
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`
  if (n >= 1024) return `${Math.round(n / 1024)}KB`
  return `${n}B`
}

/** 图片气泡：多张图按 3 列宫格排列，超出自动换行 */
function ImageGrid({ items }) {
  return (
    <div className={styles.imageGrid} data-count={items.length}>
      {items.map((item, index) => (
        <a key={`${item.url}-${index}`} className={styles.thumb} href={item.url}
          target="_blank" rel="noreferrer" title={item.name}>
          {/* 不用 loading="lazy"：气泡本身就在滚动区内、数量很少，
              懒加载在一些环境下不触发，图会一直是空白占位 */}
          <img src={item.url} alt={item.name} decoding="async" />
        </a>
      ))}
    </div>
  )
}

/** 单个文件气泡：一个文件一枚，图标 + 名字 + 大小 */
function FileBubble({ item, mine }) {
  return (
    <div className={styles.bubbleRow} data-role={mine ? "user" : "bot"}>
      <div className={`${styles.bubble} ${styles.fileBubble}`} data-role={mine ? "user" : "bot"}>
        <span className={styles.fileIcon} aria-hidden>
          <FileText size={14} />
        </span>
        <span className={styles.fileMeta}>
          <b className={styles.fileName}>{item.name}</b>
          {item.size ? <span className={styles.fileSize}>{fmtBytes(item.size)}</span> : null}
        </span>
      </div>
    </div>
  )
}

// 消息气泡按内容记忆：只要这条消息没变就不重渲染，长对话不会随每次输入卡顿
const MessageBubble = memo(function MessageBubble({ message }) {
  const mine = message.role === "user"
  const text = typeof message.content === "string" ? message.content : ""
  const busy = message.status === "running" || message.status === "queued"
  // 还在等回复且没出字：整个气泡不渲染，不要三点也不要"正在处理"
  if (busy && !text) return null
  const refs = (Array.isArray(message.attachments) ? message.attachments : [])
    .map(asRef).filter(Boolean)
  const images = refs.filter((r) => r.kind === "image" && r.url)
  const files = refs.filter((r) => r.kind !== "image" || !r.url)
  return (
    <>
      {/* 文字气泡：附件不再混在文字里 */}
      {(text || message.status === "failed" || message.status === "cancelled") && (
        <div className={styles.bubbleRow} data-role={mine ? "user" : "bot"}>
          <div className={styles.bubble} data-role={mine ? "user" : "bot"}>
            {text ? <p className={styles.text}>{text}</p> : null}
            {message.status === "failed" && (
              <p className={styles.error} role="alert">{message.error || STATUS_TEXT.failed}</p>
            )}
            {message.status === "cancelled" && <span className={styles.muted}>已停止</span>}
          </div>
        </div>
      )}
      {/* 图片：单独的宫格气泡 */}
      {images.length > 0 && (
        <div className={styles.bubbleRow} data-role={mine ? "user" : "bot"}>
          <div className={`${styles.bubble} ${styles.imageBubble}`} data-role={mine ? "user" : "bot"}>
            <ImageGrid items={images} />
          </div>
        </div>
      )}
      {/* 其它文件：每个文件一枚气泡 */}
      {files.map((item, index) => (
        <FileBubble key={`${item.name}-${index}`} item={item} mine={mine} />
      ))}
    </>
  )
})

/** 时间分隔：两条消息相隔超过 5 分钟才插一行，和微信/Telegram 一致 */
const TIME_GAP_MS = 5 * 60 * 1000
const dayLabel = (ts) => {
  const d = new Date(ts)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const y = new Date(today.getTime() - 86400000).toDateString() === d.toDateString()
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`
  if (sameDay) return hm
  if (y) return `昨天 ${hm}`
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`
}

export default function ChatDock({ messages = [], onSend, connected = true, onCancel, busy = false, onClear }) {
  const reduced = useReducedMotion()
  const [open, setOpen] = useState(false)
  const [menu, setMenu] = useState(false)
  const [pos, setPos] = useState(() => loadPos() || defaultPos())
  const scrollRef = useRef(null)
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

  // 新消息贴底（输入框自己管理焦点，这里只管滚动）
  useLayoutEffect(() => {
    const node = scrollRef.current
    if (!node || !open) return
    node.scrollTop = node.scrollHeight
  }, [messages, open])

  /** Composer 只上交「文字 + 附件」，请求构造在这里统一做。 */
  const send = async (prompt, attachments = []) => {
    if (!connected) throw new Error("尚未连接服务端")
    const payload = { prompt: prompt || "（见附件）" }
    if (attachments.length) {
      payload.attachments = attachments.map((file) => ({
        kind: file.kind, name: file.name, mime: file.mime, data: file.data,
      }))
    }
    const result = await onSend?.({ type: "agent_action", action: "chat", payload })
    if (result?.ok === false) throw new Error(textOfError(result.error))
  }

  const textOfError = (value) => {
    if (typeof value === "string" && value.trim()) return value.trim()
    return "发送失败，请重试"
  }

  // 折叠：先同步算好落点再卸载窗口，否则按钮会在"上一帧的落点"起步
  const collapse = () => {
    const spot = collapsedTarget()
    if (spot) setLauncherSpot(spot)
    setMenu(false)
    setOpen(false)
  }

  const resetPosition = () => {
    const spot = defaultPos()
    commitPos(spot.x, spot.y)
  }

  // 清空只清本地的显示列表，服务端会话由 onClear 处理（没有就只关面板不谎称已清）
  const clearChat = () => {
    onClear?.()
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
      // 展开：只做缩放弹出，**不动 opacity**。
      // 之前用 opacity:0 起始，遇上动画时间轴被冻结（后台标签页）就永远
      // 停在初始帧、窗口等于消失。可见性必须由默认样式保证。
      initial={reduced ? false : { scale: 0.9 }}
      animate={{ scale: 1 }}
      transition={reduced ? { duration: 0 }
        : { type: "spring", stiffness: 460, damping: 34, mass: 0.8 }}
      aria-label="ODDD 对话"
      data-dragging={draggingUi || undefined}>
      <div className={styles.shell}>
        {/* 标题栏按截图布局：左侧圆形返回、中间胶囊（头像+绿点+名字）、右侧圆形菜单。
           整条都可以拖动窗口；两个按钮自身不触发拖拽。 */}
        <header className={styles.header} title="拖动可移动窗口"
          onPointerDown={startDrag}>
          <button type="button" className={styles.roundBtn} aria-label="收起对话"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={collapse}>
            <ChevronLeft size={17} strokeWidth={2.4} />
          </button>

          <span className={styles.titlePill}>
            <span className={styles.avatarWrap}>
              <img src="/icon-32.png" alt="" width={20} height={20} />
              <i className={styles.onlineDot} data-on={connected || undefined} />
            </span>
            <b>ODDD</b>
          </span>

          <button type="button" className={styles.roundBtn} aria-label="更多"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={() => setMenu((open) => !open)}>
            <MoreHorizontal size={17} strokeWidth={2.4} />
          </button>
        </header>

        {menu && (
          <div className={styles.menu} role="menu">
            <button type="button" role="menuitem" onClick={() => { setMenu(false); clearChat() }}>
              清空对话
            </button>
            <button type="button" role="menuitem" onClick={() => { setMenu(false); resetPosition() }}>
              复位窗口位置
            </button>
          </div>
        )}

        <div className={styles.thread} ref={scrollRef}>
          {messages.length === 0 && <p className={styles.empty}>问市场、持仓或系统。</p>}
          {messages.map((message, index) => {
            const previous = messages[index - 1]
            const gap = !previous || (Number(message.ts) || 0) - (Number(previous.ts) || 0) > TIME_GAP_MS
            return (
              <Fragment key={message.id}>
                {gap && message.ts ? <p className={styles.timeDivider}>{dayLabel(Number(message.ts))}</p> : null}
                <MessageBubble message={message} />
              </Fragment>
            )
          })}
        </div>

        <div className={styles.composer}>
          <ChatComposer onSend={send} busy={busy} disabled={!connected}
            onCancel={onCancel} />
        </div>
      </div>
    </motion.section>,
    document.body,
  )
}
