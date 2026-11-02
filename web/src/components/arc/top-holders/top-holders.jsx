/* 大户持仓（右侧板块）：横向卡片流，与「K线形态图鉴」完全同一套卡片语言。
   数据来自币安 /futures/data/topLongShortPositionRatio（名义价值前 20% 的
   账户按持仓量统计），含近 24 小时走势。

   布局刻意和形态卡片对齐：
     · 用同一个 Card 组件（同圆角、同描边、同 media 高度、同标题/描述排版）
     · 卡宽 176px、间距 12px，与 .cards-grid 一致
     · 横向滚动沿用同一个 ScrollArea（scrollButtons + fade） */
import { useMemo } from "react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import { Card } from "@/components/arc/card/card"
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
    const H = 30
    return values.map((v, i) => {
      const x = (i / (values.length - 1)) * W
      const y = H - ((v - min) / span) * H
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`
    }).join(" ")
  }, [series])
  if (!path) return <div className={styles.sparkEmpty} aria-hidden />
  return (
    <svg className={styles.spark} viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden>
      <path d={path} fill="none" stroke={color} strokeWidth="1.4"
        strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

/** media 区：多空占比条（主角）+ 24h 走势。
    形态卡片的 media 放的是 K 线图形；这里放这张卡的"图形"，
    所以占据同样的高度位置，视觉节奏一致。 */
function HolderMedia({ longPct, series, color }) {
  return (
    <div className={styles.media}>
      <div className={styles.track} aria-hidden>
        <i style={{ width: `${longPct ?? 0}%`, background: color }} />
      </div>
      <Sparkline series={series} color={color} />
    </div>
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

  const tone = change == null ? "flat" : change > 0 ? "up" : change < 0 ? "down" : "flat"
  const changeText = change == null ? "--" : `${change > 0 ? "+" : ""}${change.toFixed(2)}%`

  return (
    <div className={styles.cell}>
      <Card
        title={<span className={styles.title}>{symbol.replace(/usdt$/i, "").toUpperCase()}</span>}
        media={<HolderMedia longPct={longPct} series={row.topLongSeries} color={color} />}
        // 描述只放"这是什么"，不重复页脚已经给出的数字
        description={`多头账户占比 · 多空比 ${ratio == null ? "--" : ratio.toFixed(2)}`}
        meta={<span className={styles.meta} style={{ color }}>{longPct == null ? "--" : `${longPct.toFixed(1)}%`}</span>}
        action={<span className={styles.change} data-tone={tone}>24h {changeText}</span>}
      />
    </div>
  )
}

export default function TopHolders({ rows, symbols, chartColors }) {
  const list = useMemo(() => {
    const order = Array.isArray(symbols) && symbols.length ? symbols : Object.keys(rows || {})
    return order.map((s) => [s, rows?.[s]]).filter(([, r]) => r?.topPositionLongPct != null)
  }, [rows, symbols])

  // 没有数据就整块不渲染：空占位文字比留白更 distracting
  if (!list.length) {
    return <section className={styles.root} aria-label="大户持仓" />
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
