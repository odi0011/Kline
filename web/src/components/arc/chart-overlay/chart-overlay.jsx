/* K线卡片左侧玻璃面板：悬浮覆盖在图表左半侧（默认 45% 宽）。
   四个真实数据视图轮播/切换：
     1. 买卖力量：每根K线的主动买量 vs 主动卖量（来自 aggTrade 的 isBuyerMaker）
     2. 深度图：订单簿真实挂单累积量（/public depth 流，20 档）
     3. 资金曲线：账户权益采样 + 从峰值回撤（MySQL equity 表）
     4. 风险敞口：持仓保证金占用 / 名义价值 / 距爆仓距离
   全部数据来自服务端，没有随机数。 */
import { useMemo, useState } from "react"
import { useId } from "react"
import { Activity, BarChart3, Layers, ShieldAlert } from "lucide-react"
import styles from "./chart-overlay.module.css"

const TABS = [
  { id: "flow", label: "买卖", Icon: BarChart3, hint: "主动买卖量" },
  { id: "depth", label: "深度", Icon: Layers, hint: "订单簿挂单" },
  { id: "equity", label: "权益", Icon: Activity, hint: "资金曲线与回撤" },
  { id: "risk", label: "风险", Icon: ShieldAlert, hint: "敞口与爆仓距离" },
]

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
      <div className={styles.ratioRow}>
        <span className={styles.ratioLabel}>主动买</span>
        <span className={styles.ratioVal} style={{ color: chartColors.up }}>{buyPct.toFixed(1)}%</span>
        <span className={styles.ratioLabel}>主动卖</span>
        <span className={styles.ratioVal} style={{ color: chartColors.down }}>{(100 - buyPct).toFixed(1)}%</span>
      </div>
      <div className={styles.ratioBar}>
        <span style={{ width: `${buyPct}%`, background: chartColors.up }} />
        <span style={{ width: `${100 - buyPct}%`, background: chartColors.down }} />
      </div>
      {!hasData && <div className={styles.hint}>等待成交数据…</div>}
      <div className={styles.bars}>
        {rows.map((b, i) => {
          const bv = b.buyVol || 0, sv = b.sellVol || 0
          return (
            <div className={styles.barCol} key={b.time ?? i}
              title={`${new Date((b.time || 0) * 1000).toLocaleTimeString("zh-CN", { hour12: false })}  买 ${fmtNum(bv, 3)} / 卖 ${fmtNum(sv, 3)}`}>
              <span className={styles.barBuy}
                style={{ height: `${(bv / max) * 100}%`, background: chartColors.up }} />
              <span className={styles.barSell}
                style={{ height: `${(sv / max) * 100}%`, background: chartColors.down }} />
            </div>
          )
        })}
      </div>
      <div className={styles.axisRow}>
        <span>{rows.length ? String(rows[0].time ? new Date(rows[0].time * 1000).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" }) : "") : ""}</span>
        <span>最近 {rows.length} 根</span>
        <span>{rows.length ? String(rows[rows.length - 1].time ? new Date(rows[rows.length - 1].time * 1000).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" }) : "") : ""}</span>
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

  if (!geo) return <div className={styles.hint}>等待订单簿数据…</div>
  const ratio = geo.bidSum + geo.askSum > 0 ? (geo.bidSum / (geo.bidSum + geo.askSum)) * 100 : 50

  return (
    <div className={styles.view}>
      <div className={styles.ratioRow}>
        <span className={styles.ratioLabel}>买盘</span>
        <span className={styles.ratioVal} style={{ color: chartColors.up }}>{ratio.toFixed(1)}%</span>
        <span className={styles.ratioLabel}>卖盘</span>
        <span className={styles.ratioVal} style={{ color: chartColors.down }}>{(100 - ratio).toFixed(1)}%</span>
        <span className={styles.midPrice}>{geo.mid.toFixed(geo.mid >= 1000 ? 1 : 4)}</span>
      </div>
      <svg className={styles.depthSvg} viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden>
        <path d={geo.bidPath} fill={chartColors.up} opacity="0.28" />
        <path d={geo.bidPath} fill="none" stroke={chartColors.up} strokeWidth="0.7" vectorEffect="non-scaling-stroke" />
        <path d={geo.askPath} fill={chartColors.down} opacity="0.24" />
        <path d={geo.askPath} fill="none" stroke={chartColors.down} strokeWidth="0.7" vectorEffect="non-scaling-stroke" />
        <line x1={geo.midX} y1="0" x2={geo.midX} y2="100"
          stroke="rgba(28,35,51,0.35)" strokeWidth="0.8" strokeDasharray="2 2" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className={styles.axisRow}>
        <span style={{ color: chartColors.up }}>买 {fmtNum(geo.bidSum, 1)}</span>
        <span>{geo.levels.bids}/{geo.levels.asks} 档</span>
        <span style={{ color: chartColors.down }}>卖 {fmtNum(geo.askSum, 1)}</span>
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

  if (!geo) return <div className={styles.hint}>权益采样积累中…</div>
  return (
    <div className={styles.view}>
      <div className={styles.ratioRow}>
        <span className={styles.ratioLabel}>当前</span>
        <span className={styles.ratioVal}>{geo.cur.toFixed(2)}</span>
        <span className={styles.ratioLabel}>回撤</span>
        <span className={styles.ratioVal} style={{ color: geo.curDD < -0.01 ? chartColors.down : "inherit" }}>
          {geo.curDD.toFixed(2)}%
        </span>
        <span className={styles.midPrice}>峰值 {geo.maxDD.toFixed(2)}%</span>
      </div>
      <svg className={styles.depthSvg} viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden>
        <path d={geo.area} fill={chartColors.up} opacity="0.16" />
        <path d={geo.line} fill="none" stroke={chartColors.up} strokeWidth="1.1"
          vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
      </svg>
      <div className={styles.axisRow}>
        <span>{geo.min.toFixed(0)}</span>
        <span>{geo.n} 个采样</span>
        <span>{geo.max.toFixed(0)}</span>
      </div>
    </div>
  )
}

/* ---------- 4. 风险敞口 ---------- */
function RiskView({ account, chartColors }) {
  const risk = useMemo(() => {
    if (!account) return null
    const pos = account.positions || []
    let margin = 0, notional = 0, upnl = 0
    let worst = null   // 距爆仓最近的仓位
    for (const p of pos) {
      margin += Number(p.margin) || 0
      notional += Number(p.notional) || 0
      upnl += Number(p.unrealized) || 0
      const mark = Number(p.mark) || 0
      const liq = Number(p.liquidation) || 0
      if (mark > 0 && liq > 0) {
        const dist = Math.abs((mark - liq) / mark) * 100
        if (!worst || dist < worst.dist) worst = { symbol: p.symbol, dist, liq, mark }
      }
    }
    const equity = Number(account.balance) || 0
    return {
      margin, notional, upnl,
      marginRatio: equity > 0 ? (margin / equity) * 100 : 0,
      leverage: equity > 0 ? notional / equity : 0,
      worst,
      count: pos.length,
    }
  }, [account])

  if (!risk) return <div className={styles.hint}>未接入账户</div>
  if (!risk.count) return <div className={styles.hint}>当前没有持仓，无风险敞口</div>

  return (
    <div className={styles.view}>
      <div className={styles.kvGrid}>
        <div className={styles.kv}><em>持仓</em><b>{risk.count}</b></div>
        <div className={styles.kv}><em>名义价值</em><b>{fmtNum(risk.notional)}</b></div>
        <div className={styles.kv}><em>占用保证金</em><b>{fmtNum(risk.margin)}</b></div>
        <div className={styles.kv}>
          <em>保证金占用率</em>
          <b style={{ color: risk.marginRatio > 60 ? chartColors.down : "inherit" }}>
            {risk.marginRatio >= 1 ? risk.marginRatio.toFixed(1) : risk.marginRatio.toFixed(2)}%
          </b>
        </div>
        <div className={styles.kv}>
          <em>账户杠杆</em>
          <b style={{ color: risk.leverage > 10 ? chartColors.down : "inherit" }}>
            {risk.leverage >= 1 ? risk.leverage.toFixed(1) : risk.leverage.toFixed(2)}x
          </b>
        </div>
        <div className={styles.kv}>
          <em>浮动盈亏</em>
          <b style={{ color: risk.upnl >= 0 ? chartColors.up : chartColors.down }}>
            {risk.upnl >= 0 ? "+" : ""}{risk.upnl.toFixed(2)}
          </b>
        </div>
      </div>
      {risk.worst && (
        <div className={styles.riskRow}>
          <span>最近爆仓距离</span>
          <b style={{ color: risk.worst.dist < 15 ? chartColors.down : "inherit" }}>
            {risk.worst.symbol.replace(/usdt$/i, "").toUpperCase()} {risk.worst.dist.toFixed(2)}%
          </b>
        </div>
      )}
      <div className={styles.meter}>
        <span style={{ width: `${Math.min(100, risk.marginRatio)}%`, background: risk.marginRatio > 60 ? chartColors.down : chartColors.up }} />
      </div>
    </div>
  )
}

export default function ChartOverlay({ symbol, bars, depth, account, series, chartColors }) {
  const [tab, setTab] = useState("flow")
  const gradId = useId().replace(/[^a-zA-Z0-9_-]/g, "")

  const body = (() => {
    switch (tab) {
      case "depth": return <DepthView depth={depth} chartColors={chartColors} />
      case "equity": return <EquityView series={series} chartColors={chartColors} />
      case "risk": return <RiskView account={account} chartColors={chartColors} />
      default: return <BuySellView bars={bars} chartColors={chartColors} />
    }
  })()

  return (
    <div className={styles.overlay} aria-label={`${symbol} 辅助面板`}>
      <div className={styles.tabs}>
        {TABS.map(({ id, label, Icon, hint }) => (
          <button key={id} type="button" className={styles.tab}
            data-on={tab === id || undefined} title={hint}
            onClick={(e) => { e.stopPropagation(); setTab(id) }}>
            <Icon size={10} aria-hidden />
            {label}
          </button>
        ))}
      </div>
      <div className={styles.body}>{body}</div>
    </div>
  )
}
