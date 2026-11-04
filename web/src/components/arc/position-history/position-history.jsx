/* 仓位历史：已平仓记录（按账户、保留 7 天、服务端 MySQL）。
   与决策流水同一套视觉语言：无边框、虚拟滚动、逐条入场。
   每条 = 币种图标 + 时间 + 方向 + 数量 + 盈亏 + 平仓原因 + 持有分钟。 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { TrendingDown, TrendingUp } from "lucide-react"
import CoinAvatar from "@/components/ui/coin-avatar"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import JumpLatest from "@/components/arc/jump-latest/jump-latest"
import styles from "./position-history.module.css"

const ROW_H = 22
const OVERSCAN = 10
const NEW_MS = 900

const pad = (n) => String(n).padStart(2, "0")
const fmtClock = (ts) => {
  const d = new Date(ts + 8 * 3600 * 1000)
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`
}
const fmtDay = (ts) => {
  const d = new Date(ts + 8 * 3600 * 1000)
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`
}
/** 数量：与后端 fmt_qty 同规则（小数量不能显示成 0.00） */
const fmtQty = (q) => {
  const v = Math.abs(Number(q) || 0)
  let s
  if (v >= 1000) s = v.toLocaleString(undefined, { maximumFractionDigits: 2 })
  else if (v >= 0.01) s = v.toFixed(4)
  else s = v.toFixed(6)
  if (s.includes(".")) s = s.replace(/0+$/, "").replace(/\.$/, "")
  return s
}
/** 平仓原因压缩成 2~3 字，窄栏里才有意义（完整原因在 hover title） */
const shortReason = (r) => {
  const s = String(r || "")
  if (s.includes("移动止盈")) return "移盈"
  if (s.includes("止盈")) return "止盈"
  if (s.includes("止损")) return "止损"
  if (s.includes("超时")) return "超时"
  const i = s.indexOf("(")
  return (i > 0 ? s.slice(0, i) : s).slice(0, 4)
}

const fmtPnl = (v) => {
  const n = Number(v) || 0
  const a = Math.abs(n)
  const s = a >= 1000 ? a.toFixed(1) : a.toFixed(2)
  return (n > 0 ? "+" : n < 0 ? "-" : "") + s
}

function TradeRow({ t, chartColors, prevTs, isNew }) {
  const long = t.side === "BUY"
  const pnl = Number(t.pnl) || 0
  const color = pnl > 0 ? chartColors.up : pnl < 0 ? chartColors.down : "rgba(16,24,40,0.5)"
  const showDay = prevTs == null || fmtDay(prevTs) !== fmtDay(t.ts)
  const DirIcon = long ? TrendingUp : TrendingDown
  return (
    <div className={styles.row} data-new={isNew || undefined}
      title={`${t.symbol.toUpperCase()} ${long ? "多" : "空"} ${fmtQty(t.qty)} · `
        + `开 ${t.entry} → 平 ${t.exit} · ${t.reason} · 持有 ${t.holdMin} 分钟`}>
      <span className={styles.lead} data-side={long ? "long" : "short"}>
        <CoinAvatar symbol={t.symbol} size={13} />
      </span>
      <span className={styles.time}>
        {showDay ? `${fmtDay(t.ts)} ` : ""}{fmtClock(t.ts)}
      </span>
      <span className={styles.sym}>{t.symbol.replace(/usdt$/i, "").toUpperCase()}</span>
      <span className={styles.side} data-side={long ? "long" : "short"}>
        <DirIcon size={9} aria-hidden />{long ? "多" : "空"}
      </span>
      <span className={styles.qty}>{fmtQty(t.qty)}</span>
      <span className={styles.reason}>{shortReason(t.reason)}</span>
      <span className={styles.hold}>{Math.round(t.holdMin)}m</span>
      <span className={styles.pnl} style={{ color }}>{fmtPnl(pnl)}</span>
    </div>
  )
}

export default function PositionHistory({ trades: pushed, chartColors, streamRef }) {
  const [trades, setTrades] = useState(() => pushed || [])
  const [scrollTop, setScrollTop] = useState(0)
  const [viewH, setViewH] = useState(0)
  // 和其它流式板块（计算流水、决策流水、新闻）统一：正序、最新在底部，
  // 默认贴底跟随；用户往上翻就不再拽他，并露出「最新」按钮。
  // 早先是倒序 + 默认停在新上面，和别处不一致。
  const [atLatest, setAtLatest] = useState(true)
  const viewportRef = useRef(null)
  const seenRef = useRef(new Set())
  const followRef = useRef(true)
  const autoScrollAt = useRef(0)
  const userScrollAt = useRef(0)

  useLayoutEffect(() => {
    const el = viewportRef.current
    if (!el) return
    const measure = () => setViewH(el.clientHeight)
    measure()
    if (typeof ResizeObserver === "undefined") return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // 服务端下发的历史：统一成正序（最早 → 最新），最新在底部。
  // 后端按 closed_ts DESC 返回，这里反转一次；排序按 ts 兜底，
  // 保证实时插入的记录不会因为时间戳抖动而错位。
  useEffect(() => {
    if (!pushed) return
    const sorted = pushed.slice().sort((a, b) => (a.ts || 0) - (b.ts || 0))
    setTrades(sorted)
    seenRef.current = new Set(sorted.map((t) => t.id ?? `${t.ts}-${t.symbol}`))
  }, [pushed])

  // 实时新增：追加到末尾（列表是正序）
  useEffect(() => {
    if (!pushed) return
    setTrades((cur) => {
      const list = cur.slice()
      let added = false
      for (const t of pushed) {
        const key = t.id ?? `${t.ts}-${t.symbol}`
        if (seenRef.current.has(key)) continue
        seenRef.current.add(key)
        list.push(t)
        added = true
      }
      // 只留最近 500 条（内存保护；服务端保留 365 天，翻历史靠查询）
      return added ? list.slice(-500) : cur
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pushed])

  /** 新交易单条推入（由上层调用） */
  useEffect(() => {
    if (!streamRef) return
    streamRef.current = { pushTrade: (t) => {
      const key = t.id ?? `${t.ts}-${t.symbol}`
      if (seenRef.current.has(key)) return
      seenRef.current.add(key)
      // 正序列表：新记录追加到末尾
      setTrades((cur) => [...cur, t].slice(-500))
    } }
  }, [streamRef])

  /** 贴底跟随：新增记录或首次渲染时，只有在"本来就在底部"才跟。
      写 scrollTop 用 rAF 合并，避免连续插入时一格一格地抖。 */
  useLayoutEffect(() => {
    const el = viewportRef.current
    if (!el || !followRef.current) return
    // 用户刚动过（1 秒内）：这一轮不贴底，尊重他的位置
    if (Date.now() - userScrollAt.current < 1000) return
    const target = el.scrollHeight - el.clientHeight
    if (Math.abs(el.scrollTop - target) < 2) return
    autoScrollAt.current = Date.now()
    el.scrollTop = target
    setScrollTop(target)
  }, [trades])

  const markUserScroll = () => { userScrollAt.current = Date.now() }

  const onScroll = () => {
    const el = viewportRef.current
    if (!el) return
    setScrollTop(el.scrollTop)
    // 程序滚动的回响：忽略，但**不改 followRef**
    if (Date.now() - autoScrollAt.current < 80) return
    // 容差 40px：行高 22px，两行以内都算"还在底部"，
    // 轻微滚轮抖动不该把跟随关掉（与计算流水一致）
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    followRef.current = near
    setAtLatest(near)
  }

  const jump = () => {
    const el = viewportRef.current
    if (!el) return
    followRef.current = true
    setAtLatest(true)
    userScrollAt.current = 0
    autoScrollAt.current = Date.now()
    el.scrollTop = el.scrollHeight
    setScrollTop(el.scrollTop)
  }

  const total = trades.length
  const winH = Math.max(viewH, 160)
  const start = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN)
  const end = Math.min(total, Math.ceil((scrollTop + winH) / ROW_H) + OVERSCAN)
  const slice = useMemo(() => trades.slice(start, end), [trades, start, end])

  const now = Date.now()
  const isNew = (t) => now - (t.ts || 0) < NEW_MS

  return (
    <section className={styles.wrap} aria-label="仓位历史">
      <ScrollArea viewportRef={viewportRef} orientation="vertical" fade={18} scrollbars="auto"
        className={styles.stream} viewportClassName={styles.streamViewport}
        label="仓位历史" onScroll={onScroll}
        onWheel={markUserScroll} onTouchMove={markUserScroll}>
        {total === 0 ? (
          <div className={styles.empty}>暂无平仓记录</div>
        ) : (
          <div className={styles.spacer} style={{ height: total * ROW_H }}>
            {slice.map((t, i) => {
              const idx = start + i
              return (
                <div key={t.id ?? `${t.ts}-${idx}`} className={styles.rowSlot}
                  style={{ transform: `translateY(${idx * ROW_H}px)` }}>
                  <TradeRow t={t} chartColors={chartColors}
                    prevTs={trades[idx - 1]?.ts} isNew={isNew(t)} />
                </div>
              )
            })}
          </div>
        )}
      </ScrollArea>
      {!atLatest && <JumpLatest onClick={jump} />}
    </section>
  )
}
