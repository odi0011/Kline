/* K线卡片左侧玻璃面板：悬浮覆盖在图表左半侧（45% 宽）。
   三个真实数据图同屏纵向排列，不再用 tab 切换：
     1. 买卖力量：每根K线的主动买量 vs 主动卖量（aggTrade 的 isBuyerMaker）
     2. 订单簿深度：真实挂单累积量（/public depth 流，20 档）
     3. 权益与回撤：账户权益采样 + 从峰值回撤（MySQL equity 表）
   全部数据来自服务端，没有随机数。风险敞口移至别处。 */
import { useMemo } from "react"
import { LiquidGlass } from "@/components/ui/liquid-glass"
import styles from "./chart-overlay.module.css"


const fmtNum = (v, d = 2) => {
  const n = Number(v) || 0
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(d) + "B"
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(d) + "M"
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(d) + "K"
  return n.toFixed(d)
}

/* ---------- 1. 买卖力量：最近 N 根的买卖量对比 ---------- */
function BuySellView({ bars, chartColors }) {
  const rows = useMemo(() => (bars || []).slice(-26), [bars])
  const max = useMemo(
    () => Math.max(1e-9, ...rows.map((b) => Math.max(b.buyVol || 0, b.sellVol || 0))),
    [rows]
  )
  // 全窗口买卖占比（真实成交分类汇总）
  const { buy, sell } = useMemo(() => {
    let buy = 0, sell = 0
    for (const b of rows) { buy += b.buyVol || 0; sell += b.sellVol || 0 }
    return { buy, sell }
  }, [rows])
  const total = buy + sell
  const buyPct = total > 0 ? (buy / total) * 100 : 50
  const hasData = total > 0

  return (
    <div className={styles.view}>
      <div className={styles.metricRow}>
        <span style={{ color: chartColors.up }}>买 {buyPct.toFixed(0)}%</span>
        <span style={{ color: chartColors.down }}>卖 {(100 - buyPct).toFixed(0)}%</span>
        <span className={styles.metricHint}>{rows.length} 根</span>
      </div>
      {!hasData && <div className={styles.hint}>等待成交…</div>}
      <div className={styles.bars}>
        {rows.map((b, i) => {
          const bv = b.buyVol || 0, sv = b.sellVol || 0
          return (
            <div className={styles.barCol} key={b.time ?? i}
              title={`买 ${fmtNum(bv, 3)} / 卖 ${fmtNum(sv, 3)}`}>
              <span className={styles.barBuy}
                style={{ height: `${(bv / max) * 100}%`, background: chartColors.up }} />
              <span className={styles.barSell}
                style={{ height: `${(sv / max) * 100}%`, background: chartColors.down }} />
            </div>
          )
        })}
      </div>
    </div>
  )
}

/* ---------- 2. 深度图：真实挂单累积 ---------- */
function DepthView({ depth, chartColors }) {
  const geo = useMemo(() => {
    if (!depth || (!depth.bids?.length && !depth.asks?.length)) return null
    const bids = (depth.bids || []).slice(0, 14)
    const asks = (depth.asks || []).slice(0, 14)
    const mid = asks.length && bids.length
      ? (bids[0][0] + asks[0][0]) / 2
      : (asks[0]?.[0] ?? bids[0]?.[0] ?? 0)
    // 累积量（从中间向外累加）
    let run = 0
    const bidPts = bids.map(([p, q]) => { run += q; return { price: p, cum: run } })
    run = 0
    const askPts = asks.map(([p, q]) => { run += q; return { price: p, cum: run } })
    const maxCum = Math.max(1e-9, ...bidPts.map(p => p.cum), ...askPts.map(p => p.cum))
    const prices = [...bids.map(b => b[0]), ...asks.map(a => a[0])]
    const pMin = Math.min(...prices), pMax = Math.max(...prices)
    const span = pMax - pMin || 1
    const x = (p) => ((p - pMin) / span) * 100
    const y = (c) => 100 - (c / maxCum) * 92
    const path = (pts, dir) => {
      if (!pts.length) return ""
      const sorted = [...pts].sort((a, b) => dir === "bid" ? b.price - a.price : a.price - b.price)
      let d = `M${x(mid).toFixed(2)},100`
      for (const p of sorted) d += `L${x(p.price).toFixed(2)},${y(p.cum).toFixed(2)}`
      d += `L${x(sorted[sorted.length - 1].price).toFixed(2)},100Z`
      return d
    }
    return {
      bidPath: path(bidPts, "bid"),
      askPath: path(askPts, "ask"),
      midX: x(mid),
      mid,
      bidSum: bidPts.length ? bidPts[bidPts.length - 1].cum : 0,
      askSum: askPts.length ? askPts[askPts.length - 1].cum : 0,
      levels: { bids: bids.length, asks: asks.length },
    }
  }, [depth])

  if (!geo) return <div className={styles.hint}>等待订单簿…</div>
  const ratio = geo.bidSum + geo.askSum > 0 ? (geo.bidSum / (geo.bidSum + geo.askSum)) * 100 : 50

  return (
    <div className={styles.view}>
      <div className={styles.metricRow}>
        <span style={{ color: chartColors.up }}>买 {ratio.toFixed(0)}%</span>
        <span style={{ color: chartColors.down }}>卖 {(100 - ratio).toFixed(0)}%</span>
        <span className={styles.metricHint}>{geo.mid.toFixed(geo.mid >= 1000 ? 0 : 4)}</span>
      </div>
      <div className={styles.chartEnv}>
      <svg className={styles.depthSvg} viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden>
        <path d={geo.bidPath} fill={chartColors.up} opacity="0.3" />
        <path d={geo.bidPath} fill="none" stroke={chartColors.up} strokeWidth="0.8" vectorEffect="non-scaling-stroke" />
        <path d={geo.askPath} fill={chartColors.down} opacity="0.26" />
        <path d={geo.askPath} fill="none" stroke={chartColors.down} strokeWidth="0.8" vectorEffect="non-scaling-stroke" />
        <line x1={geo.midX} y1="0" x2={geo.midX} y2="100"
          stroke="rgba(28,35,51,0.3)" strokeWidth="0.8" strokeDasharray="2 2" vectorEffect="non-scaling-stroke" />
      </svg>
      </div>
    </div>
  )
}

/* ---------- 3. 资金曲线 + 回撤 ---------- */
function EquityView({ series, chartColors }) {
  const geo = useMemo(() => {
    const ss = (series || []).slice(-160)
    if (ss.length < 2) return null
    const vals = ss.map(s => s.bal)
    const min = Math.min(...vals), max = Math.max(...vals)
    const span = max - min || 1
    // 回撤：从历史峰值往下的百分比
    let peak = -Infinity
    const dd = ss.map((s) => {
      if (s.bal > peak) peak = s.bal
      return peak > 0 ? ((s.bal - peak) / peak) * 100 : 0
    })
    const maxDD = Math.min(...dd)
    const curDD = dd[dd.length - 1]
    const W = 100, H = 100
    const pts = ss.map((s, i) => [
      (i / (ss.length - 1)) * W,
      H - ((s.bal - min) / span) * (H - 8) - 4,
    ])
    const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`).join("")
    return {
      line, area: `${line}L${W},${H}L0,${H}Z`,
      min, max, cur: vals[vals.length - 1],
      maxDD, curDD, n: ss.length,
    }
  }, [series])

  if (!geo) return <div className={styles.hint}>权益采样中…</div>
  return (
    <div className={styles.view}>
      <div className={styles.metricRow}>
        <span>{geo.cur.toFixed(1)}</span>
        <span style={{ color: geo.curDD < -0.01 ? chartColors.down : "inherit" }}>
          回撤 {geo.curDD.toFixed(2)}%
        </span>
        <span className={styles.metricHint}>峰值 {geo.maxDD.toFixed(2)}%</span>
      </div>
      <div className={styles.chartEnv}>
      <svg className={styles.depthSvg} viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden>
        <path d={geo.area} fill={chartColors.up} opacity="0.18" />
        <path d={geo.line} fill="none" stroke={chartColors.up} strokeWidth="1.1"
          vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
      </svg>
      </div>
    </div>
  )
}

export default function ChartOverlay({ symbol, bars, depth, account, series, chartColors }) {
  return (
    <LiquidGlass
      radius={10}
      bevelDepth={18}
      scale={36}
      aberration={0}
      frost={4.5}
      tint={0.9}
      className={styles.overlay}
      contentClassName={styles.overlayInner}
      aria-label={`${symbol} 辅助面板`}
    >
      <div className={styles.pane}>
        <span className={styles.paneTitle}>买卖力量</span>
        <BuySellView bars={bars} chartColors={chartColors} />
      </div>
      <div className={styles.hsep} />
      <div className={styles.pane}>
        <span className={styles.paneTitle}>订单簿深度</span>
        <DepthView depth={depth} chartColors={chartColors} />
      </div>
      <div className={styles.hsep} />
      <div className={styles.pane}>
        <span className={styles.paneTitle}>权益与回撤</span>
        <EquityView series={series} chartColors={chartColors} />
      </div>
    </LiquidGlass>
  )
}
