/* 对话面板（ODDD）—— 页面第二行的固定板块，不是悬浮窗。

   历史：这里原本是"顶部玻璃胶囊 + 可拖动悬浮窗"。改成固定面板是因为
   第二行的 3D 编排场景实测占掉近四分之一 CPU（帧间隔中位数 296ms），
   删掉后那个 50% 的位置正好给对话用，比悬浮窗更省事也更好找。

   保留的：消息气泡、表情包、附件、性能条、输入框、清空/压缩菜单。
   去掉的：拖拽定位、折叠胶囊、展开/收起动画（固定面板不需要）。 */
import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { motion, useReducedMotion } from "motion/react"
import { Archive, FileText, MoreHorizontal, Trash2 } from "lucide-react"
import ChatComposer from "./chat-composer"
import ChatStats from "./chat-stats"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import { GlassMenu } from "@/components/arc/glass-menu/glass-menu"
import { RemoteSticker, Sticker, StickerCredit, splitSticker } from "./stickers"
import styles from "./chat-dock.module.css"

/** 自动跟随阈值：离底部不超过这个距离就算"正在看最新消息" */
const FOLLOW_SLACK = 32

const STATUS_TEXT = { queued: "排队中", running: "正在处理", failed: "回复失败", cancelled: "已停止" }

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

/** 一组消息的渲染签名：只有 id / 状态 / 内容 / 错误 / 附件变化时才需要重渲染。
    后端每次 agent_state 广播都会整体深拷贝 chat 数组（引用必变），没有这个
    比较的话，窗口里几十条消息每 250ms 都要重渲染一次。 */
const groupSignature = (items) => items.map((item) => [
  item?.id, item?.role, item?.status, item?.content || "", item?.error || "",
  Array.isArray(item?.attachments) ? JSON.stringify(item.attachments) : "",
].join("\u0001")).join("\u0002")

/** 一轮消息的渲染单元。
    后端会把过长的回复拆成多条（id 带 `-2`/`-3` 后缀），那是同一次回答的
    分段，视觉上必须是一枚气泡里的几段话；只有附件按类型各自独立成泡。 */
const MessageGroup = memo(function MessageGroup({ items }) {
  const mine = items[0]?.role === "user"
  // 每段先拆出 [sticker:...]：表情包要单独成泡，不能混在文字里
  const parsed = items.map((item) => splitSticker(
    typeof item.content === "string" ? item.content : ""))
  const texts = parsed.map((p) => p.text).filter(Boolean)
  const stickers = parsed.map((p) => p.sticker).filter(Boolean)
  const remotes = parsed.map((p) => p.remote).filter(Boolean)
  const failed = items.find((item) => item.status === "failed")
  const cancelled = items.some((item) => item.status === "cancelled")
  const busy = items.some((item) => item.status === "running" || item.status === "queued")
  // 还在等回复且没出字：整个气泡不渲染，不要三点也不要"正在处理"
  if (busy && !texts.length && !stickers.length && !remotes.length) return null
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
      {/* 文字：每段一个独立气泡（像连发几条微信），不是一个气泡里塞多段。
          后端把模型用 ||| 分的段拆成多条消息（id 带 -2/-3），
          这里按段逐个渲染；只有"失败/停止"提示才并入最后一个气泡。 */}
      {texts.map((text, index) => {
        const isLast = index === texts.length - 1
        return (
          <div key={`t-${index}`} className={styles.bubbleRow} data-role={mine ? "user" : "bot"}>
            <div className={styles.bubble} data-role={mine ? "user" : "bot"}>
              <p className={styles.text}>{text}</p>
              {isLast && failed && (
                <p className={styles.error} role="alert">{failed.error || STATUS_TEXT.failed}</p>
              )}
              {isLast && cancelled && !failed && <span className={styles.muted}>已停止</span>}
            </div>
          </div>
        )
      })}
      {/* 只有失败/停止、没有任何正文：单独给一个提示气泡（否则什么都不显示） */}
      {!texts.length && (failed || cancelled) && (
        <div className={styles.bubbleRow} data-role={mine ? "user" : "bot"}>
          <div className={styles.bubble} data-role={mine ? "user" : "bot"}>
            {failed
              ? <p className={styles.error} role="alert">{failed.error || STATUS_TEXT.failed}</p>
              : <span className={styles.muted}>已停止</span>}
          </div>
        </div>
      )}
      {/* 表情包：独立一枚，无气泡底色——贴纸本身就是内容 */}
      {stickers.map((name, index) => (
        <div key={`${name}-${index}`} className={styles.stickerRow} data-role={mine ? "user" : "bot"}>
          <Sticker name={name} max={132} />
        </div>
      ))}
      {/* 在线贴纸：服务器已落盘的本地文件，尺寸已知所以加载前就占好位置。
          署名只在 GIPHY 来源时显示——条款只管它，本地图不用挂。 */}
      {remotes.map((remote, index) => (
        <div key={`${remote.url}-${index}`} className={styles.stickerRow} data-role={mine ? "user" : "bot"}>
          <RemoteSticker remote={remote} max={132} />
          {remote.credit ? <StickerCredit /> : null}
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
}, (prev, next) => prev.sig === next.sig)

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
  const [confirm, setConfirm] = useState(null)   // null | "clear" | "compact"
  const scrollRef = useRef(null)

  // 新消息/流式增长贴底：只有用户本来就在底部才跟随，往回翻历史时不能被拽下去。
  // 写 scrollTop 必须合并到 requestAnimationFrame：agent_state 每 250ms 广播一次，
  // 每次广播都直接改 scrollTop 会让流式增长的气泡一格一格地抖。
  const followRef = useRef(true)
  const pinRafRef = useRef(0)
  const pinToBottom = useCallback(() => {
    if (pinRafRef.current) window.cancelAnimationFrame(pinRafRef.current)
    pinRafRef.current = window.requestAnimationFrame(() => {
      pinRafRef.current = 0
      const node = scrollRef.current
      if (!node || !followRef.current) return
      node.scrollTop = node.scrollHeight
    })
  }, [])

  const onThreadScroll = (event) => {
    const el = event.currentTarget
    // 程序化贴底也会触发 scroll，因此这里每次滚动都重新判断，阈值给足余量
    followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_SLACK
  }

  // 面板挂载后直接贴底（布局阶段执行，避免先看到顶部再跳）
  useLayoutEffect(() => {
    followRef.current = true
    pinToBottom()
  }, [pinToBottom])

  // 只有"新消息插入"（条数/最后一条 id 变化）才单独贴底一次；
  // 流式改 content 不触发这条，交给下面的 ResizeObserver。
  const lastMessageId = messages.length
    ? String(messages[messages.length - 1]?.id || "") : ""
  useLayoutEffect(() => {
    pinToBottom()
  }, [messages.length, lastMessageId, pinToBottom])

  // 内容区真实高度变化（流式文字、图片/表情包加载完成）时保持贴底。
  // 只观察消息子节点；一帧最多写一次 scrollTop，且用户往上翻时自动放弃跟随。
  useEffect(() => {
    const node = scrollRef.current
    if (!node || typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(() => pinToBottom())
    const observe = () => {
      observer.disconnect()
      observer.observe(node)
      Array.from(node.children).forEach((child) => observer.observe(child))
    }
    observe()
    // 新分组插入时子节点集合会变，重新观察一遍
    const mutations = new MutationObserver(() => { observe(); pinToBottom() })
    mutations.observe(node, { childList: true })
    return () => {
      observer.disconnect()
      mutations.disconnect()
      if (pinRafRef.current) window.cancelAnimationFrame(pinRafRef.current)
      pinRafRef.current = 0
    }
  }, [pinToBottom])

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
    // 自己发消息时无视当前滚动位置，直接回到最新（符合聊天软件习惯）
    followRef.current = true
    pinToBottom()
  }

  const textOfError = (value) => {
    if (typeof value === "string" && value.trim()) return value.trim()
    return "发送失败，请重试"
  }

  const clearChat = () => onClear?.()

  /** 「更多」菜单：走统一 GlassMenu。
      面板固定在页面里，"复位窗口位置"已经没有意义，不再提供。 */
  const menuGroups = [{
    id: "g-dock",
    items: [
      { id: "compact", label: "压缩对话", icon: <Archive size={12} /> },
      { id: "clear", label: "清空全部", icon: <Trash2 size={12} />, danger: true },
    ],
  }]

  const onMenuSelect = (id) => {
    if (id === "compact") setConfirm("compact")
    else if (id === "clear") setConfirm("clear")
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
  // 消息分组：渲染与"是否首次出现"记录共用；group key 是组内首条消息 id，
  // 后端每次广播虽然整体替换数组，但 id 稳定，所以 key 也稳定（不会 remount）。
  const groups = useMemo(() => groupMessages(messages), [messages])
  const seenGroupsRef = useRef(new Set())
  const groupKeySignature = groups.map((group) => group.key).join("|")
  useEffect(() => {
    // 提交后再登记：本帧新出现的分组仍算"新"，已有分组永远不会重放动画
    for (const group of groups) seenGroupsRef.current.add(group.key)
  }, [groupKeySignature])   // eslint-disable-line react-hooks/exhaustive-deps

  // ---------- 固定面板（页面第二行，替代原 3D 编排场景的位置） ----------
  return (
    <section className={styles.dock} aria-label="ODDD 对话">
      <div className={styles.shell}>
        {/* 标题栏：头像 + 名称/状态 + 菜单（不再可拖动、也没有收起钮） */}
        <header className={styles.header}>
          <span className={styles.titlePill} data-busy={busy || undefined}>
            <span className={styles.avatarWrap}>
              <img src="/icon-32.png" alt="" width={20} height={20} />
              <i className={styles.onlineDot} data-on={connected || undefined} />
            </span>
            <motion.b
              // 只在忙碌/空闲切换时重挂载做一次小位移动画；状态词每 250ms 变一次
              // 不能跟着换 key，否则胶囊文字会一直重放入场动画（标题栏抖动）。
              key={busy ? "busy" : "idle"}
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
          {groups.map((group) => {
            const gap = !group.at || !group.prevTs || group.at - group.prevTs > TIME_GAP_MS
            // 入场动画只给"本帧第一次出现"的 assistant 分组；已有分组更新
            // （流式增长/状态变化）不重放 y/scale。用户气泡也绝不做 y 位移：
            // 发送瞬间它和助手占位同时插入，再滑一下就是明显的整段抖动。
            const isNew = !seenGroupsRef.current.has(group.key)
            const animateIn = !reduced && isNew && group.role !== "user"
            return (
              <Fragment key={group.key}>
                {gap ? <p className={styles.timeDivider}>{dayLabel(group.at)}</p> : null}
                <motion.div className={styles.groupEnter}
                  initial={animateIn ? { y: 6, scale: 0.985 } : false}
                  animate={{ y: 0, scale: 1 }}
                  transition={reduced ? { duration: 0 }
                    : { type: "spring", stiffness: 480, damping: 34, mass: 0.7 }}>
                  <MessageGroup items={group.items} sig={groupSignature(group.items)} />
                </motion.div>
              </Fragment>
            )
          })}
        </ScrollArea>

        <div className={styles.composer}>
          <div className={styles.composerShell}>
            <ChatStats stats={stats} busy={busy} />
            {/* 不传 onCancel：这个输入框支持连续发多条，发送键不做"停止" */}
            <ChatComposer onSend={send} disabled={!connected} />
          </div>
        </div>
      </div>
    </section>
  )
}
