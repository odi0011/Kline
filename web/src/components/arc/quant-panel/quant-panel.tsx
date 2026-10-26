/* 底部决策流水：单个无边框盒子，实时滚动显示引擎的全部决策与思考过程。
   数据来自服务端 MySQL（按账户持久化，重启/刷新都不清空），
   新事件到达时增量合并，滚动到底部时自动跟随。 */
import { useEffect, useMemo, useRef, useState } from "react"
import {
  Activity, AlertTriangle, ArrowRightLeft, CircleDot, Cog, PlayCircle,
  Search, TrendingDown, TrendingUp, XCircle,
} from "lucide-react"
import CoinAvatar from "@/components/ui/coin-avatar"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import styles from "./quant-panel.module.css"

const KIND_META = {
  signal: { label: "信号", Icon: Search, tone: "signal" },
  open: { label: "开仓", Icon: PlayCircle, tone: "open" },
  close: { label: "平仓", Icon: ArrowRightLeft, tone: "close" },
  skip: { label: "跳过", Icon: XCircle, tone: "skip" },
  error: { label: "错误", Icon: AlertTriangle, tone: "error" },
  audit: { label: "审计", Icon: Activity, tone: "audit" },
  system: { label: "系统", Icon: Cog, tone: "system" },
}

const pad = (n) => String(n).padStart(2, "0")

/** 时间戳 → 北京时间 HH:MM:SS（ts 为 UTC 毫秒，+8h 后用 UTC 取值） */
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

/** 一条流水：时间 · 类型 · 币种 · 说明（含思考细节）· 盈亏 */
function EventRow({ ev, chartColors, prevTs }) {
  const meta = KIND_META[ev.kind] || KIND_META.system
  const { Icon } = meta
  const DirIcon = ev.direction === "bearish" ? TrendingDown
    : ev.direction === "bullish" ? TrendingUp : null
  // 跨天时显示日期，方便回看历史
  const showDay = prevTs == null || fmtDay(prevTs) !== fmtDay(ev.ts)
  return (
    <div className={styles.row} data-tone={meta.tone}>
      <span className={styles.time}>
        {showDay && <em className={styles.day}>{fmtDay(ev.ts)}</em>}
        {fmtClock(ev.ts)}
      </span>
      <span className={styles.kind}>
        <Icon size={11} aria-hidden />
        {meta.label}
      </span>
      <span className={styles.sym}>
        {ev.symbol && <CoinAvatar symbol={ev.symbol} size={13} />}
        {ev.symbol ? ev.symbol.replace(/usdt$/i, "").toUpperCase() : ""}
      </span>
      <span className={styles.text} title={ev.reason}>
        {DirIcon && <DirIcon size={11} className={styles.dir} aria-hidden />}
        <span className={styles.reason}>{ev.reason}</span>
        {ev.name_zh && ev.kind === "signal" ? <em className={styles.meta}>{ev.name_zh}</em> : null}
        {typeof ev.winrate === "number" ? <em className={styles.meta}>胜率 {ev.winrate}%</em> : null}
        {ev.qty != null && ev.kind === "open" ? <em className={styles.meta}>量 {ev.qty}</em> : null}
      </span>
      {typeof ev.pnl === "number" && (
        <span className={styles.pnl}
          style={{ color: ev.pnl >= 0 ? chartColors.up : chartColors.down }}>
          {fmtMoney(ev.pnl)}
        </span>
      )}
    </div>
  )
}

export default function QuantPanel({ snapshot, events: pushedEvents, chartColors, onToggle }) {
  const snap = snapshot || { enabled: false, stats: {} }
  const stats = snap.stats || {}
  const [events, setEvents] = useState(() => pushedEvents || [])
  const [follow, setFollow] = useState(true)      // 自动滚到底；用户上翻时不打断
  const viewportRef = useRef(null)
  const seenRef = useRef(new Set())

  // 服务端下发的持久化历史（连接/重连时）
  useEffect(() => {
    if (!pushedEvents) return
    setEvents(pushedEvents)
    seenRef.current = new Set(pushedEvents.map((e) => e.id ?? `${e.ts}-${e.reason}`))
  }, [pushedEvents])

  // 实时增量：合并快照里的事件（按 id 去重）
  useEffect(() => {
    const live = snap.events || []
    if (!live.length) return
    setEvents((cur) => {
      const merged = cur.slice()
      let added = false
      for (const ev of live) {
        const key = ev.id ?? `${ev.ts}-${ev.kind}-${ev.reason}`
        if (seenRef.current.has(key)) continue
        seenRef.current.add(key)
        merged.push({ ...ev, id: key })
        added = true
      }
      if (!added) return cur
      merged.sort((a, b) => (a.ts || 0) - (b.ts || 0))
      return merged.slice(-800)
    })
  }, [snap.events])

  // 自动跟随到底
  useEffect(() => {
    if (!follow) return
    const vp = viewportRef.current
    if (vp) vp.scrollTop = vp.scrollHeight
  }, [events, follow])

  const onScroll = () => {
    const vp = viewportRef.current
    if (!vp) return
    setFollow(vp.scrollHeight - vp.scrollTop - vp.clientHeight < 24)
  }

  const flow = useMemo(() => ([
    ["信号", stats.signals ?? 0],
    ["决策", stats.decisions ?? 0],
    ["下单", stats.orders ?? 0],
    ["平仓", stats.closes ?? 0],
  ]), [stats])

  return (
    <section className={styles.wrap} aria-label="决策流水">
      <header className={styles.head}>
        <h3 className={styles.title}>决策流水</h3>
        <span className={styles.state} data-on={snap.enabled || undefined}>
          {snap.enabled ? "运行中" : "已停止"}
        </span>
        <div className={styles.flow}>
          {flow.map(([label, v]) => (
            <span key={label} className={styles.flowItem}><em>{label}</em>{v}</span>
          ))}
          <span className={styles.flowItem}>
            <em>胜/负</em>
            <b className={styles.win}>{stats.wins ?? 0}</b>/<b className={styles.loss}>{stats.losses ?? 0}</b>
          </span>
        </div>
        <button type="button" className={styles.toggle} data-on={snap.enabled || undefined}
          onClick={() => onToggle && onToggle(!snap.enabled)}>
          {snap.enabled ? "停止" : "启动"}
        </button>
      </header>

      <ScrollArea viewportRef={viewportRef} orientation="vertical" fade={22} scrollbars="auto"
        className={styles.stream} viewportClassName={styles.streamViewport}
        label="量化决策流水" onScroll={onScroll}>
        {events.length === 0 ? (
          <div className={styles.empty}>暂无决策记录</div>
        ) : (
          events.map((ev, i) => (
            <EventRow key={ev.id ?? `${ev.ts}-${i}`} ev={ev}
              chartColors={chartColors} prevTs={events[i - 1]?.ts} />
          ))
        )}
      </ScrollArea>

      {!follow && events.length > 0 && (
        <button type="button" className={styles.jump}
          onClick={() => {
            setFollow(true)
            const vp = viewportRef.current
            if (vp) vp.scrollTop = vp.scrollHeight
          }}>
          回到最新
        </button>
      )}
    </section>
  )
}
