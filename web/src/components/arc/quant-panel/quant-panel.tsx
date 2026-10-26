/* 决策流水：只有记录列表，没有标题/状态/统计。
   每条 = 图标（有币种用币种图标，否则用事件类型图标）+ 时间 + 内容 + tag + 盈亏。
   内容单行显示，中间省略（保住括号尾注、价格等关键尾部信息）。
   列表采用虚拟滚动：只渲染可视窗口内的行，历史几千条也不卡。
   新到的行有入场动画（淡入 + 上滑 + 轻微缩放）。 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import {
  Activity, AlertTriangle, ArrowRightLeft, Cog, PlayCircle,
  Search, XCircle,
} from "lucide-react"
import CoinAvatar from "@/components/ui/coin-avatar"
import { LiquidGlass } from "@/components/ui/liquid-glass"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import styles from "./quant-panel.module.css"

const ROW_H = 22          // 固定行高（虚拟滚动依赖固定高度）
const OVERSCAN = 10       // 视口上下额外渲染行数
const NEW_MS = 900        // 新行高亮/动画时长
const ENTER_GAP = 150     // 逐条入场间隔（同批到多条时一条条冒出来）

const KIND_META = {
  signal: { Icon: Search, tone: "signal" },
  open: { Icon: PlayCircle, tone: "open" },
  close: { Icon: ArrowRightLeft, tone: "close" },
  skip: { Icon: XCircle, tone: "skip" },
  error: { Icon: AlertTriangle, tone: "error" },
  audit: { Icon: Activity, tone: "audit" },
  system: { Icon: Cog, tone: "system" },
}

const pad = (n) => String(n).padStart(2, "0")
/** UTC 毫秒 → 北京时间 HH:MM:SS */
const fmtClock = (ts) => {
  const d = new Date(ts + 8 * 3600 * 1000)
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`
}
const fmtDay = (ts) => {
  const d = new Date(ts + 8 * 3600 * 1000)
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`
}
const fmtMoney = (v) => {
  const n = Number(v) || 0
  const abs = Math.abs(n)
  const s = abs >= 1000 ? abs.toFixed(1) : abs >= 1 ? abs.toFixed(2) : abs.toPrecision(3)
  return (n > 0 ? "+" : n < 0 ? "-" : "") + s
}

/** 切分说明为「头 + 尾」，让中间省略时两端都保留有用信息。
    优先把【动作/主体】留在头部、【结果/数值】留在尾部：
      "做多 BTCUSDT 数量 0.619 @ 80262.2（保证金 2494.3）"
        → 头 "做多 BTCUSDT 数量 0.619" + 尾 "@ 80262.2（保证金 2494.3）"
      "移动止盈(峰值0.14%→0.06%)平仓 XRPUSDT，盈亏 1.63"
        → 头 "移动止盈(峰值0.14%→0.06%)" + 尾 "平仓 XRPUSDT，盈亏 1.63"
    括号注记不能当分隔点（否则会把"平仓 XXX"整段吞掉）。 */
function splitReason(reason, compact = false) {
  let s = String(reason || "")
  if (!s) return ["", ""]
  // 窄容器：只剥掉"长括号"（峰值/涨幅这类型细节描述）；
  // 短括号往往本身是关键数值（还需 32s、2/2、4/4），必须保留。
  if (compact) {
    s = s.replace(/[（(][^）)]{12,}[）)]/g, "").replace(/\s{2,}/g, " ").trim()
  }
  // 1) 价格分隔：@ 之后（含保证金尾注）
  const at = s.lastIndexOf(" @ ")
  if (at > 0) return [s.slice(0, at), s.slice(at + 1)]
  // 2) 结果分隔：平仓/，盈亏/，数量 这类"结果段"整体留尾部
  const marks = ["，盈亏 ", "，平仓 ", "平仓 ", "，数量 "]
  let best = -1
  for (const m of marks) {
    const i = s.lastIndexOf(m)
    if (i > 0 && (best < 0 || i < best)) best = i
  }
  if (best > 0) return [s.slice(0, best), s.slice(best)]
  // 3) 兜底：长文本切掉中间，留头 60% / 尾 24%
  if (s.length > 22) {
    const keepTail = Math.max(8, Math.round(s.length * 0.24))
    return [s.slice(0, s.length - keepTail), s.slice(-keepTail)]
  }
  return [s, ""]
}

function EventRow({ ev, chartColors, prevTs, isNew, compact, fill }) {
  const meta = KIND_META[ev.kind] || KIND_META.system
  const { Icon } = meta
  const [head, tail] = splitReason(ev.reason, compact)
  const showDay = prevTs == null || fmtDay(prevTs) !== fmtDay(ev.ts)
  const tags = []
  // 形态名只加一次：说明文本里已经带了就不重复放 tag
  if (ev.name_zh && ev.kind === "signal" && !String(ev.reason || "").includes(ev.name_zh)) {
    tags.push(ev.name_zh)
  }
  if (typeof ev.winrate === "number") tags.push(`胜率${ev.winrate}%`)
  if (ev.qty != null && ev.kind === "open") tags.push(`量${ev.qty}`)

  // fill: {tone, fx, fy, grow} —— 飞线落点扩散（fx/fy 是行内百分比）
  return (
    <div className={styles.row} data-tone={meta.tone} data-new={isNew || undefined}
      data-plain={isNew && !fill ? "" : undefined}
      data-row-id={ev.id ?? undefined}
      title={ev.reason}>
      {fill && (
        <span className={styles.fillLayer} data-tone={fill.tone || "neutral"}
          data-fade={fill.fade || undefined} aria-hidden
          style={{ clipPath: `circle(${fill.grow ? 150 : 0}% at ${fill.fx}% ${fill.fy}%)` }} />
      )}
      <span className={styles.lead}>
        {ev.symbol
          ? <CoinAvatar symbol={ev.symbol} size={13} />
          : <Icon size={12} aria-hidden />}
      </span>
      <span className={styles.time}>
        {showDay ? `${fmtDay(ev.ts)} ` : ""}{fmtClock(ev.ts)}
      </span>
      <span className={styles.text}>
        <span className={styles.head}>{head}</span>
        {tail && <span className={styles.tail}>{tail}</span>}
      </span>
      {!compact && tags.slice(0, 2).map((t) => (
        <span key={t} className={styles.tag}>{t}</span>
      ))}
      {typeof ev.pnl === "number" && (
        <span className={styles.pnl}
          style={{ color: ev.pnl >= 0 ? chartColors.up : chartColors.down }}>
          {fmtMoney(ev.pnl)}
        </span>
      )}
    </div>
  )
}

export default function QuantPanel({ snapshot, events: pushedEvents, chartColors, reservedRows, streamRef }) {
  const snap = snapshot || { enabled: false, stats: {} }
  const [events, setEvents] = useState(() => pushedEvents || [])
  const [scrollTop, setScrollTop] = useState(0)
  const [viewH, setViewH] = useState(0)
  const [compact, setCompact] = useState(false)   // 窄容器：省略次要信息
  const reserved = reservedRows || []            // 末尾占位行（飞线落点）
  const [follow, setFollow] = useState(true)
  const [, setTick] = useState(0)
  const wrapRef = useRef(null)
  const viewportRef = useRef(null)
  const seenRef = useRef(new Set())
  const addedAtRef = useRef(new Map())
  const pendingRef = useRef([])          // 待入场的事件队列
  const drainTimerRef = useRef(null)

  // 视口高度（虚拟窗口大小）+ 容器宽度（决定是否精简内容）
  useLayoutEffect(() => {
    const el = viewportRef.current
    const wrap = wrapRef.current
    if (!el) return
    const measure = () => {
      setViewH(el.clientHeight)
      if (wrap) setCompact(wrap.clientWidth < 340)
    }
    measure()
    if (typeof ResizeObserver === "undefined") return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    if (wrap) ro.observe(wrap)
    return () => ro.disconnect()
  }, [])

  // 服务端下发的持久化历史
  useEffect(() => {
    if (!pushedEvents) return
    setEvents(pushedEvents)
    seenRef.current = new Set(pushedEvents.map((e) => e.id ?? `${e.ts}-${e.reason}`))
    pendingRef.current = []          // 历史一次性铺好，不做逐条动画
  }, [pushedEvents])

  // 新事件先入队；由下面的定时器逐条展示（避免同批多条一起冒出来）
  useEffect(() => {
    if (pushedEvents) return
    for (const ev of (snap.events || [])) {
      const key = ev.id ?? `${ev.ts}-${ev.kind}-${ev.reason}`
      if (seenRef.current.has(key)) continue
      seenRef.current.add(key)
      pendingRef.current.push({ ...ev, id: key })
    }
  }, [snap.events, pushedEvents])

  // 逐条入场：每 ENTER_GAP 毫秒放一条，每条各自触发入场动画
  useEffect(() => {
    if (drainTimerRef.current) return
    const drain = () => {
      const next = pendingRef.current.shift()
      if (!next) { drainTimerRef.current = null; return }
      addedAtRef.current.set(next.id, Date.now())
      setEvents((cur) => {
        const merged = cur.concat(next)
        merged.sort((a, b) => (a.ts || 0) - (b.ts || 0))
        return merged.slice(-3000)
      })
      drainTimerRef.current = setTimeout(drain, ENTER_GAP)
    }
    if (pendingRef.current.length) drain()
    return () => {
      if (drainTimerRef.current) { clearTimeout(drainTimerRef.current); drainTimerRef.current = null }
    }
  }, [snap.events, pushedEvents])

  // 动画到期后刷新一次，让 data-new 失效（避免一直高亮）
  useEffect(() => {
    if (!addedAtRef.current.size) return
    const id = setTimeout(() => setTick((t) => t + 1), NEW_MS + 60)
    return () => clearTimeout(id)
  }, [events])

  // 贴底跟随（初次瞬间、后续平滑）
  useEffect(() => {
    if (!follow) return
    const el = viewportRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: scrollTop === 0 ? "auto" : "smooth" })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events.length, reserved.length, follow])

  const onScroll = useCallback(() => {
    const el = viewportRef.current
    if (!el) return
    setScrollTop(el.scrollTop)
    setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < ROW_H * 1.5)
  }, [])

  // 虚拟窗口：真实事件行走虚拟滚动，末尾占位行数量很少直接渲染
  const evTotal = events.length
  const total = evTotal + reserved.length
  const winH = Math.max(viewH, 200)
  const start = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN)
  const end = Math.min(total, Math.ceil((scrollTop + winH) / ROW_H) + OVERSCAN)
  const slice = useMemo(
    () => events.slice(Math.min(start, evTotal), Math.min(end, evTotal)),
    [events, start, end, evTotal]
  )

  const now = Date.now()
  const isNew = (id) => now - (addedAtRef.current.get(id) || 0) < NEW_MS

  return (
    <section ref={wrapRef} className={styles.wrap} aria-label="决策流水">
      <ScrollArea viewportRef={(el) => {
        viewportRef.current = el
        if (streamRef) streamRef.current = el
      }} orientation="vertical" fade={18} scrollbars="auto"
        className={styles.stream} viewportClassName={styles.streamViewport}
        label="量化决策流水" onScroll={onScroll}>
        {total === 0 ? (
          <div className={styles.empty}>暂无决策记录</div>
        ) : (
          <div className={styles.spacer} style={{ height: total * ROW_H }}>
            {slice.map((ev, i) => {
              const idx = start + i
              return (
                <div key={ev.id ?? idx} className={styles.rowSlot}
                  style={{ transform: `translateY(${idx * ROW_H}px)` }}>
                  <EventRow ev={ev} chartColors={chartColors} compact={compact}
                    prevTs={events[idx - 1]?.ts} isNew={isNew(ev.id)} />
                </div>
              )
            })}
            {reserved.map((r, i) => {
              const idx = evTotal + i
              if (idx < start || idx >= end) return null
              const prev = evTotal > 0 ? events[evTotal - 1]?.ts : reserved[i - 1]?.ev?.ts
              return (
                <div key={r.id} className={styles.rowSlot} data-row-id={r.id}
                  style={{ transform: `translateY(${idx * ROW_H}px)` }}>
                  {r.revealed ? (
                    <EventRow ev={r.ev} chartColors={chartColors} compact={compact}
                      prevTs={prev} isNew
                      fill={{ tone: r.tone, fx: r.fx, fy: r.fy, grow: r.grow, fade: r.fade }} />
                  ) : (
                    <div className={styles.row} data-pending="" />
                  )}
                </div>
              )
            })}
          </div>
        )}
      </ScrollArea>

      {!follow && total > 0 && (
        <div className={styles.jumpWrap}>
          <LiquidGlass radius={999} scale={22} aberration={0} frost={2} tint={0.14}
            className={styles.jumpGlass}>
            <button type="button" className={styles.jumpBtn}
              onClick={() => {
                setFollow(true)
                const el = viewportRef.current
                if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" })
              }}>
              最新
            </button>
          </LiquidGlass>
        </div>
      )}
    </section>
  )
}
