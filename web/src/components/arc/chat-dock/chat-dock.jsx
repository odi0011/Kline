/* 悬浮聊天窗（ODDD）。

   折叠态 = 顶部居中的一枚玻璃胶囊（类似灵动岛）：
     · 平时是窄胶囊，带头像和在线点
     · 有未读/正在回复时，胶囊横向生长并显示状态（"在判断方向"）
     · 点一下从胶囊位置展开成完整对话窗（spring + squircle 圆角变化）

   展开态 = 可拖动的对话窗。
   折叠/展开动画都由 motion 的 layout 完成——形状在胶囊和卡片之间连续插值，
   圆角从"半个高度"收到卡片半径，和灵动岛的做法一致。 */
import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { motion, useReducedMotion } from "motion/react"
import { Archive, FileText, MoreHorizontal, RotateCcw, Trash2, X } from "lucide-react"
import ChatComposer from "./chat-composer"
import ChatStats from "./chat-stats"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import { GlassMenu } from "@/components/arc/glass-menu/glass-menu"
import { Sticker, splitSticker } from "./stickers"
import styles from "./chat-dock.module.css"

const WIDTH = 320
const HEIGHT = 468
const MARGIN = 14
/** 折叠胶囊：贴顶部居中，距顶 10px */
const ISLAND_TOP = 10
const ISLAND_H = 34
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

/** 一轮消息的渲染单元。
    后端会把过长的回复拆成多条（id 带 `-2`/`-3` 后缀），那是同一次回答的
    分段，视觉上必须是一枚气泡里的几段话；只有附件按类型各自独立成泡。 */
const MessageGroup = memo(function MessageGroup({ items }) {
  const mine = items[0]?.role === "user"
  // 每段先拆出 [sticker:名字]：表情包要单独成泡，不能混在文字里
  const parsed = items.map((item) => splitSticker(
    typeof item.content === "string" ? item.content : ""))
  const texts = parsed.map((p) => p.text).filter(Boolean)
  const stickers = parsed.map((p) => p.sticker).filter(Boolean)
  const failed = items.find((item) => item.status === "failed")
  const cancelled = items.some((item) => item.status === "cancelled")
  const busy = items.some((item) => item.status === "running" || item.status === "queued")
  // 还在等回复且没出字：整个气泡不渲染，不要三点也不要"正在处理"
  if (busy && !texts.length && !stickers.length) return null
  const refs = items
    .flatMap((item) => (Array.isArray(item.attachments) ? item.attachments : []))
    .map(asRef).filter(Boolean)
  // 同一附件可能被重复带上（合并回复时），按 url/名字去重
  const seen = new Set()
  const unique = refs.filter((ref) => {
    const key = ref.url || ref.name
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
  const images = unique.filter((r) => r.kind === "image" && r.url)
  const files = unique.filter((r) => r.kind !== "image" || !r.url)
  return (
    <>
      {/* 文字气泡：一轮一个，分段之间用段落间距区分；附件不再混在文字里 */}
      {(texts.length > 0 || failed || cancelled) && (
        <div className={styles.bubbleRow} data-role={mine ? "user" : "bot"}>
          <div className={styles.bubble} data-role={mine ? "user" : "bot"}>
            {texts.map((text, index) => (
              <p key={index} className={styles.text}>{text}</p>
            ))}
            {failed && (
              <p className={styles.error} role="alert">{failed.error || STATUS_TEXT.failed}</p>
            )}
            {cancelled && !failed && <span className={styles.muted}>已停止</span>}
          </div>
        </div>
      )}
      {/* 表情包：独立一枚，无气泡底色——贴纸本身就是内容 */}
      {stickers.map((name, index) => (
        <div key={`${name}-${index}`} className={styles.stickerRow} data-role={mine ? "user" : "bot"}>
          <Sticker name={name} max={132} />
        </div>
      ))}
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

/** 长回复被后端拆成多条 assistant 消息（id 形如 `原id-2`），
    视觉上应算同一轮的一组：同组内不重复插时间分隔。 */
const groupMessages = (messages) => {
  const groups = []
  for (const message of messages) {
    const id = String(message.id || "")
    const base = id.replace(/-\d+$/, "")
    const last = groups[groups.length - 1]
    // 同角色、且 id 属于同一轮（共享 base）→ 并进当前组
    if (last && last.role === message.role && last.baseId === base) {
      last.items.push(message)
      continue
    }
    groups.push({
      key: id, baseId: base, role: message.role,
      at: Number(message.ts) || 0, prevTs: last?.at ?? 0, items: [message],
    })
  }
  return groups
}

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

export default function ChatDock({ messages = [], onSend, connected = true, stats = null,
                                  onCancel, busy = false, onClear, onCompact }) {
  const reduced = useReducedMotion()
  const [open, setOpen] = useState(false)
  const [confirm, setConfirm] = useState(null)   // null | "clear" | "compact"
  const [pos, setPos] = useState(() => loadPos() || defaultPos())
  const scrollRef = useRef(null)
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

  /** 手写拖拽：pointer 事件绑在 window 上，松手才落盘位置。
      不用 motion 的 drag —— 它在本页（大量 canvas / 浮层）里事件接管不稳定。 */
  const startDrag = useCallback((event) => {
    if (event.button !== 0) return
    if (event.target.closest("button")) return      // 圆钮不触发拖拽
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

  // 新消息贴底：只有原本就在底部时才自动跟随，往回翻看历史时不能把用户拽下去
  const followRef = useRef(true)
  useLayoutEffect(() => {
    const node = scrollRef.current
    if (!node || !open) return
    if (followRef.current) node.scrollTop = node.scrollHeight
  }, [messages, open])

  const onThreadScroll = (event) => {
    const el = event.currentTarget
    followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
  }

  /** Composer 只上交「文字 + 附件」，请求构造在这里统一做。
      只发附件时 prompt 留空：不能塞一句"（见附件）"充数，
      那会变成一条真的用户消息，气泡里就多出一行没意义的文字。 */
  const send = async (prompt, attachments = []) => {
    if (!connected) throw new Error("尚未连接服务端")
    const payload = { prompt: prompt || "" }
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

  const resetPosition = () => {
    const spot = defaultPos()
    commitPos(spot.x, spot.y)
  }

  const clearChat = () => onClear?.()

  /** 「更多」菜单：走统一 GlassMenu。
      收起按钮不再放这里——它是标题栏上独立的圆形按钮。 */
  const menuGroups = [{
    id: "g-dock",
    items: [
      { id: "compact", label: "压缩对话", icon: <Archive size={12} /> },
      { id: "clear", label: "清空全部", icon: <Trash2 size={12} />, danger: true },
      { id: "reset", label: "复位窗口位置", icon: <RotateCcw size={12} /> },
    ],
  }]

  const onMenuSelect = (id) => {
    if (id === "compact") setConfirm("compact")
    else if (id === "clear") setConfirm("clear")
    else if (id === "reset") resetPosition()
  }

  /** 忙碌时标题栏显示的那句状态：取最后一条进行中消息的 progress.status。
      后端在每轮模型请求和每次工具调用时都会写一句（"在判断方向"）。 */
  const statusLine = useMemo(() => {
    if (!busy) return ""
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]
      if (m.role === "user") continue
      if (m.status === "running" || m.status === "queued") {
        return m.progress?.status || (m.status === "queued" ? "排队中" : "在想")
      }
    }
    return "在想"
  }, [busy, messages])

  /** 未读：折叠态时如果有新完成的回复，胶囊上出一个小圆点。
      基准取"上一次折叠那一刻的已完成条数"——不是打开时的条数，
      否则折叠后收到的回复永远不会算作未读。 */
  const doneCount = (list) => list.filter((m) => m.role !== "user" && m.content
    && (m.status === "succeeded" || m.status === "failed")).length
  const seenRef = useRef(doneCount(messages))
  const [unread, setUnread] = useState(0)
  useEffect(() => {
    if (open) {
      // 打开着的时候不算未读：用户正在看
      seenRef.current = doneCount(messages)
      setUnread(0)
      return
    }
    setUnread(Math.max(0, doneCount(messages) - seenRef.current))
  }, [messages, open])
  // 折叠那一刻把基准对齐到当前，之后新增的才算未读
  const collapse = useCallback(() => {
    seenRef.current = doneCount(messages)
    setUnread(0)
    setOpen(false)
  }, [messages])

  // ---------- 折叠态：顶部居中的玻璃圆环 ----------
  if (!open) {
    return createPortal(
      // 外层只负责"贴顶 + 水平居中"（flex），圆钮自己负责形状与动画。
      // 不让按钮用 transform 居中：motion 的 layout 动画会接管 transform，
      // translateX(-50%) 会被覆盖，实测偏右半个身位。
      <div className={styles.islandWrap} style={{ top: ISLAND_TOP }}>
        <motion.button
          type="button"
          className={styles.island}
          data-busy={busy || undefined}
          data-unread={unread > 0 || undefined}
          onClick={() => setOpen(true)}
          aria-label="打开 ODDD 对话"
          title={busy ? (statusLine || "在想") : unread > 0 ? "有新回复" : "ODDD 对话"}
          initial={reduced ? false : { scale: 0.86 }}
          animate={{ scale: 1 }}
          whileTap={reduced ? undefined : { scale: 0.94 }}
          transition={reduced ? { duration: 0 }
            : { type: "spring", stiffness: 420, damping: 30, mass: 0.7 }}>
          <span className={styles.islandAvatar}>
            <img src="/icon-32.png" alt="" width={30} height={30} />
            <i className={styles.onlineDot} data-on={connected || undefined} />
          </span>
          {/* 忙碌：右下角转一圈的小点；未读：黄点。都不占圆外空间 */}
          {busy && <span className={styles.islandBusy} aria-hidden />}
          <span className={styles.islandBadge} aria-hidden />
        </motion.button>
      </div>,
      document.body,
    )
  }

  // ---------- 展开态：完整对话窗 ----------
  return createPortal(
    <motion.section
      className={styles.dock}
      style={{ position: "fixed", left: pos.x, top: pos.y, width: WIDTH, height: HEIGHT }}
      // 展开：从顶部胶囊的位置放大出来。只做 scale + y，**不动 opacity**——
      // 动画时间轴被冻结的环境里，opacity:0 起始会让窗口永久不可见。
      initial={reduced ? false : { scale: 0.72, y: -Math.max(0, pos.y - ISLAND_TOP), borderRadius: 40 }}
      animate={{ scale: 1, y: 0, borderRadius: 20 }}
      exit={reduced ? undefined : { scale: 0.8, y: -Math.max(0, pos.y - ISLAND_TOP), borderRadius: 40 }}
      transition={reduced ? { duration: 0 }
        : { type: "spring", stiffness: 380, damping: 32, mass: 0.85 }}
      aria-label="ODDD 对话"
      data-dragging={draggingUi || undefined}>
      <div className={styles.shell}>
        {/* 悬浮标题栏：胶囊 + 收起钮 + 圆形菜单，绝对定位压在消息滚动区之上。
            整个标题栏可拖动窗口。 */}
        <header className={styles.header} title="拖动可移动窗口"
          onPointerDown={startDrag}>
          <span className={styles.titlePill} data-busy={busy || undefined}>
            <span className={styles.avatarWrap}>
              <img src="/icon-32.png" alt="" width={20} height={20} />
              <i className={styles.onlineDot} data-on={connected || undefined} />
            </span>
            <motion.b
              key={busy ? (statusLine || "working") : "idle"}
              className={styles.pillText}
              data-busy={busy || undefined}
              initial={reduced ? false : { y: 3 }}
              animate={{ y: 0 }}
              transition={reduced ? { duration: 0 }
                : { type: "spring", stiffness: 520, damping: 36, mass: 0.6 }}>
              {busy ? (statusLine || "在想") : "ODDD"}
            </motion.b>
            {busy && <span className={styles.pillDots} aria-hidden><i /><i /><i /></span>}
          </span>

          <div className={styles.headerBtns}>
            {/* 收起钮：独立圆形按钮，放在菜单钮左边。
                图标是"向内收拢"的双箭头，和展开方向对应。 */}
            <button type="button" className={styles.roundBtn} aria-label="收起窗口"
              title="收起为顶部胶囊"
              onPointerDown={(event) => event.stopPropagation()}
              onClick={collapse}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
                aria-hidden>
                <path d="M9 4v5H4" /><path d="M9 9L4 4" />
                <path d="M15 20v-5h5" /><path d="M15 15l5 5" />
              </svg>
            </button>

            <GlassMenu label="对话菜单" triggerLabel="更多" trigger="custom" side="bottom"
              align="end" width={132} groups={menuGroups}
              onSelect={onMenuSelect}>
              <button type="button" className={styles.roundBtn} aria-label="更多"
                onPointerDown={(event) => event.stopPropagation()}>
                <MoreHorizontal size={17} strokeWidth={2.4} />
              </button>
            </GlassMenu>
          </div>
        </header>

        {/* 确认层：清空是不可逆操作，压缩会改写历史，两者都要二次确认。
            用内联的小卡片而不是浏览器 confirm()——后者会打断整页、
            样式也无法统一。 */}
        {confirm && (
          <div className={styles.confirm} role="alertdialog" aria-modal="true"
            aria-labelledby="oddd-confirm-title">
            <p className={styles.confirmTitle} id="oddd-confirm-title">
              {confirm === "clear" ? "清空全部？" : "压缩对话？"}
            </p>
            <p className={styles.confirmBody}>
              {confirm === "clear"
                ? "会删除所有对话、记忆和后台任务，无法恢复。"
                : "会把较早的对话压成摘要，只保留最近几条原文。"}
            </p>
            <div className={styles.confirmActions}>
              <button type="button" className={styles.confirmCancel}
                onClick={() => setConfirm(null)}>取消</button>
              <button type="button" className={styles.confirmOk}
                data-danger={confirm === "clear" || undefined}
                onClick={() => {
                  const kind = confirm
                  setConfirm(null)
                  if (kind === "clear") clearChat()
                  else onCompact?.()
                }}>
                {confirm === "clear" ? "清空" : "压缩"}
              </button>
            </div>
          </div>
        )}

        {/* 消息区用项目统一的 ScrollArea（细滚动条 + 边缘渐隐） */}
        <ScrollArea orientation="vertical" fade={16} scrollbars="auto" label="ODDD 对话记录"
          viewportRef={scrollRef} className={styles.thread}
          viewportClassName={styles.threadViewport} onScroll={onThreadScroll}>
          {messages.length === 0 && <p className={styles.empty}>问市场、持仓或系统。</p>}
          {groupMessages(messages).map((group) => {
            const gap = !group.at || !group.prevTs || group.at - group.prevTs > TIME_GAP_MS
            return (
              <Fragment key={group.key}>
                {gap ? <p className={styles.timeDivider}>{dayLabel(group.at)}</p> : null}
                {/* 气泡入场：只做位移+缩放，不动 opacity */}
                <motion.div className={styles.groupEnter}
                  initial={reduced ? false : { y: 6, scale: 0.985 }}
                  animate={{ y: 0, scale: 1 }}
                  transition={reduced ? { duration: 0 }
                    : { type: "spring", stiffness: 480, damping: 34, mass: 0.7 }}>
                  <MessageGroup items={group.items} />
                </motion.div>
              </Fragment>
            )
          })}
        </ScrollArea>

        <div className={styles.composer}>
          <div className={styles.composerShell}>
            <ChatStats stats={stats} busy={busy} />
            <ChatComposer onSend={send} busy={busy} disabled={!connected}
              onCancel={onCancel} />
          </div>
        </div>
      </div>
    </motion.section>,
    document.body,
  )
}
