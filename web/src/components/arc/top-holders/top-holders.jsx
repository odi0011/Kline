/* 大户持仓（右下板块上半）：横向卡片，每张 = 一个币的大户多空结构。
   数据来自币安 /futures/data/topLongShortPositionRatio（名义价值前 20% 的
   账户按持仓量统计），含近 24 小时走势。
   横向滚动沿用「K线形态图鉴」那套 ScrollArea（scrollButtons + fade）。 */
import { useMemo } from "react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import CoinAvatar from "@/components/ui/coin-avatar"
import styles from "./top-holders.module.css"

const num = (v) => {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

/** 走势迷你折线：只画 longPct，纵轴按自身幅度铺满，看方向不看绝对值 */
function Sparkline({ series, color }) {
  const path = useMemo(() => {
    const values = (series || []).map((p) => num(p.longPct)).filter((v) => v != null)
    if (values.length < 2) return ""
    const min = Math.min(...values)
    const max = Math.max(...values)
    const span = max - min || 1
    const W = 100
    const H = 24
    return values.map((v, i) => {
      const x = (i / (values.length - 1)) * W
      const y = H - ((v - min) / span) * H
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`
    }).join(" ")
  }, [series])
  if (!path) return <div className={styles.sparkEmpty} aria-hidden />
  return (
    <svg className={styles.spark} viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden>
      <path d={path} fill="none" stroke={color} strokeWidth="1.4"
        strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

function HolderCard({ symbol, row, chartColors }) {
  const up = chartColors?.up || "#16a34a"
  const down = chartColors?.down || "#dc2626"
  const longPct = num(row.topPositionLongPct)
  const change = num(row.topLongChange24h)
  const ratio = num(row.topPositionLongShort)
  // 大户多头占比是这张卡的唯一主角：柱长 = 占比，颜色 = 相对 50% 的偏向
  const color = longPct == null ? "rgba(16,24,40,0.4)" : longPct >= 50 ? up : down

  return (
    <article className={styles.card} title={`${symbol.toUpperCase()} · 大户多头 ${longPct ?? "--"}%`
      + ` · 多空比 ${ratio ?? "--"} · 24h ${change == null ? "--" : `${change > 0 ? "+" : ""}${change}%`}`}>
      <header className={styles.cardHead}>
        <CoinAvatar symbol={symbol} size={15} />
        <b>{symbol.replace(/usdt$/i, "").toUpperCase()}</b>
        <span className={styles.pct} style={{ color }}>{longPct == null ? "--" : `${longPct.toFixed(1)}%`}</span>
      </header>

      <div className={styles.track} aria-hidden>
        <i style={{ width: `${longPct ?? 0}%`, background: color }} />
      </div>

      <Sparkline series={row.topLongSeries} color={color} />

      <footer className={styles.cardFoot}>
        <span>多空 {ratio == null ? "--" : ratio.toFixed(2)}</span>
        <span style={{ color: change > 0 ? up : change < 0 ? down : "rgba(16,24,40,0.4)" }}>
          {change == null ? "--" : `${change > 0 ? "+" : ""}${change.toFixed(2)}%`}
        </span>
      </footer>
    </article>
  )
}

export default function TopHolders({ rows, symbols, chartColors, error = "" }) {
  const list = useMemo(() => {
    const order = Array.isArray(symbols) && symbols.length ? symbols : Object.keys(rows || {})
    return order.map((s) => [s, rows?.[s]]).filter(([, r]) => r?.topPositionLongPct != null)
  }, [rows, symbols])

  if (!list.length) {
    return <section className={styles.root} aria-label="大户持仓">
      <p className={styles.empty} title={error || undefined}>
        {error ? `大户持仓取数失败：${error}` : "等待币安大户持仓数据…"}
      </p>
    </section>
  }

  return (
    <section className={styles.root} aria-label="大户持仓">
      <ScrollArea orientation="horizontal" scrollbars="auto" scrollButtons fade={16}
        label="大户持仓" className={styles.scroll} viewportClassName={styles.viewport}>
        <div className={styles.cards}>
          {list.map(([sym, row]) => (
            <HolderCard key={sym} symbol={sym} row={row} chartColors={chartColors} />
          ))}
        </div>
      </ScrollArea>
    </section>
  )
}
