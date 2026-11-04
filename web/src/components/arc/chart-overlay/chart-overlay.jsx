/* K线卡片左侧玻璃面板：悬浮覆盖在图表左半侧（45% 宽）。
   三个真实数据图同屏纵向排列，不再用 tab 切换：
     1. 买卖力量：每根K线的主动买量 vs 主动卖量（aggTrade 的 isBuyerMaker）
     2. 订单簿深度：真实挂单累积量（/public depth 流，20 档）
     3. 风险画像：CVaR / 已实现波动率 / 平仓样本数
   全部数据来自服务端，没有随机数。
   **账户权益不在这里**：那是账户级数据，权威位置是账户卡（那里有曲线和日热力图）。
   之前把它画进每张 K 线卡，4 个币就出现 4 份一模一样的图。 */
import { useEffect, useMemo, useRef, useState } from "react"
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

/* ---------- 3. 风险：尾部画像 + 回撤 ----------
   原来这一格画的是"权益与回撤"，也就是把同一个账户权益在**每张K线卡**
   里各画一遍（4 个币 = 4 份完全相同的图），而且第三个数字标错了：
   渲染的是 maxDD（最大回撤）却写成了"峰值"，跟账户卡里真正的
   最高权益 7366.08 对不上——同一个账户两个"峰值"，一个 0.00%、
   一个 7366.08 USDT。

   权益曲线已经由账户卡负责（那一处才是权威），这里改画**这张卡自己的
   风险画像**：CVaR / 波动率 / 样本数。这是每张 K 线卡唯一的、不重复的信息。 */
function RiskView({ risk, chartColors }) {
  const r = risk || {}
  const samples = Number(r.samples) || 0
  const cvar95 = Number.isFinite(Number(r.cvar95)) ? Number(r.cvar95) : null
  const vol = Number.isFinite(Number(r.volatility)) ? Number(r.volatility) : null
  const worst = Number.isFinite(Number(r.worst)) ? Number(r.worst) : null
  // 样本不足时明确说"样本不足"，绝不拿几笔样本算一个看起来像风险的数
  if (!samples) return <div className={styles.hint}>还没有平仓样本</div>
  const pct = (v, d = 2) => (v == null ? "--" : `${(v * 100).toFixed(d)}%`)
  return (
    <div className={styles.view}>
      <div className={styles.metricRow}>
        <span style={{ color: cvar95 != null && cvar95 < 0 ? chartColors.down : "inherit" }}
          title="CVaR95：最差 5% 样本的平均收益率（历史样本口径，不是拟合）">
          CVaR {r.enoughSamples ? pct(cvar95) : "样本不足"}
        </span>
        <span title="已实现波动率：收益率样本标准差，不年化">波动 {pct(vol)}</span>
        <span className={styles.metricHint}>{samples} 笔样本</span>
      </div>
      <div className={styles.kvRow}>
        <span>最差一笔 {pct(worst)}</span>
        <span>均值 {pct(Number.isFinite(Number(r.mean)) ? Number(r.mean) : null, 3)}</span>
      </div>    </div>
  )
}

/* 图表时间轴（X 轴标签行）的高度。lightweight-charts 没暴露稳定 API，
   按容器高度比例估：时间轴约占底部 26px（字体 11px + padding）。
   面板在做垂直居中时要把这部分排除。 */
const TIME_AXIS_H = 26

export default function ChartOverlay({ symbol, bars, depth, account, series, chartColors, risk }) {
  const boxRef = useRef(null)
  const [geo, setGeo] = useState({ h: 0, top: 0 })
  // 面板在"蜡烛区"（不含时间轴）内垂直居中：等分上下留白
  useEffect(() => {
    const host = boxRef.current?.parentElement
    if (!host) return
    const measure = () => {
      const total = host.clientHeight
      const plot = Math.max(1, total - TIME_AXIS_H)
      // 面板高度取蜡烛区的 82%（比之前略矮，留出居中呼吸感）
      const panelH = Math.round(plot * 0.82)
      setGeo({ h: panelH, top: Math.round((plot - panelH) / 2) })
    }
    measure()
    if (typeof ResizeObserver === "undefined") return
    const ro = new ResizeObserver(measure)
    ro.observe(host)
    return () => ro.disconnect()
  }, [])

  return (
    <div ref={boxRef} className={styles.host}>
    <LiquidGlass
      radius={10}
      bevelDepth={18}
      scale={36}
      aberration={0}
      frost={3.2}
      tint={0.34}
      className={styles.overlay}
      contentClassName={styles.overlayInner}
      style={geo.h ? { top: geo.top, height: geo.h, bottom: "auto" } : undefined}
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
        <span className={styles.paneTitle}>风险画像</span>
        <RiskView risk={risk} chartColors={chartColors} />
      </div>
    </LiquidGlass>
    </div>
  )
}
