/* 第二行：当前选中账户的数据面板。
   概览统计（权益/未实现盈亏/可用）+ 权益走势（lightweight-charts AreaSeries）
   + 当前持仓列表。数据全部来自账户广播与本地权益采样，随置前卡片切换。 */
import { useEffect, useMemo, useRef, useState } from "react"
import { createChart, AreaSeries, ColorType } from "lightweight-charts"
import { AnimatedCounter } from "@/components/arc/animated-counter/animated-counter"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import CoinAvatar from "@/components/ui/coin-avatar"
import styles from "./account-panel.module.css"

function rgba(hex, a) {
  const h = hex.replace("#", "")
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`
}

/** 权益走势（AreaSeries，首尾贴边，无坐标轴，hover 十字带读数） */
function EquityChart({ samples, hue, height = 150 }) {
  const wrapRef = useRef(null)
  const chartRef = useRef(null)
  const areaRef = useRef(null)
  const samplesRef = useRef(samples)
  samplesRef.current = samples
  const [hoverVal, setHoverVal] = useState(null)

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#7a8499", fontSize: 10, attributionLogo: false,
      },
      grid: { vertLines: { visible: false }, horzLines: { visible: true, color: "rgba(28,35,51,0.05)" } },
      rightPriceScale: { visible: false },
      timeScale: { visible: false, borderVisible: false },
      handleScroll: false, handleScale: false,
      crosshair: { mode: 0 },
    })
    const area = chart.addSeries(AreaSeries, {
      lineColor: `hsl(${hue} 50% 34%)`,
      topColor: `hsl(${hue} 60% 45% / 0.4)`,
      bottomColor: `hsl(${hue} 60% 45% / 0.02)`,
      lineWidth: 2,
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerRadius: 4,
      crosshairMarkerBorderColor: "rgba(255,255,255,0.95)",
      crosshairMarkerBackgroundColor: `hsl(${hue} 50% 34%)`,
    })
    chartRef.current = chart
    areaRef.current = area
    chart.subscribeCrosshairMove((param) => {
      const d = param.seriesData.get(area)
      setHoverVal(d ? d.value : null)
    })
    return () => chart.remove()
  }, [hue])

  useEffect(() => {
    const area = areaRef.current, chart = chartRef.current
    if (!area || !chart) return
    const ss = samplesRef.current || []
    let data = ss.map((s) => ({ time: Math.round(s.t / 1000), value: s.bal }))
    if (!data.length) return
    if (data.length === 1) data = [{ time: data[0].time - 120, value: data[0].value }, data[0]]
    area.setData(data)
    chart.timeScale().setVisibleLogicalRange({ from: -0.5, to: Math.max(1, data.length - 0.5) })
  }, [samples && samples.length, samples && samples.length && samples[samples.length - 1]?.bal])

  return (
    <div className={styles.chartBox}>
      <div className={styles.chartHead}>
        <span className={styles.chartTitle}>权益走势</span>
        {hoverVal != null && (
          <span className={styles.chartVal}>{Number(hoverVal).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
        )}
      </div>
      <div ref={wrapRef} style={{ height }} />
    </div>
  )
}

/** 持仓行 */
function PositionRow({ p, chartColors }) {
  const base = p.symbol.replace(/usdt$/i, "")
  const long = p.amt > 0
  return (
    <div className={styles.posRow}>
      <CoinAvatar symbol={p.symbol} size={22} />
      <span className={styles.posSym}>{p.symbol.toUpperCase()}</span>
      <span className={styles.posSide} data-side={long ? "long" : "short"}>{long ? "多" : "空"}</span>
      <span className={styles.posAmt}>{Math.abs(p.amt).toLocaleString()}</span>
      <span className={styles.posPnl} style={{ color: p.unrealized > 0 ? chartColors.up : p.unrealized < 0 ? chartColors.down : "inherit" }}>
        {(p.unrealized > 0 ? "+" : "") + Number(p.unrealized).toFixed(2)}
      </span>
    </div>
  )
}

export default function AccountPanel({ account, series, chartColors }) {
  const hue = useMemo(() => {
    if (!account) return 45
    let h = 0
    for (const ch of account.name) h = (h * 131 + ch.charCodeAt(0)) % 4294967296
    return ((h % 997) * 47) % 360
  }, [account && account.name])

  const seriesKey = series ? `${series.length}:${series.length ? series[series.length - 1].bal : 0}` : "0"
  const hasSeries = !!series && series.length > 0
  const balance = account && account.balance
  const pnl = account && account.unrealized
  const up = (pnl ?? 0) > 0
  const down = (pnl ?? 0) < 0
  const positions = (account && account.positions) || []

  if (!account) {
    return (
      <div className={styles.wrap}>
        <div className={styles.empty}>未接入账户——在「设置 → 账户」添加后，这里展示对应账户的数据</div>
      </div>
    )
  }

  return (
    <div className={styles.wrap} style={{ borderTopColor: `hsl(${hue} 60% 55% / 0.5)` }}>
      <header className={styles.head}>
        {account.icon ? <img className={styles.acctIcon} src={account.icon} alt="" /> : <CoinAvatar symbol={account.name} size={34} />}
        <h3 className={styles.name}>{account.name}</h3>
        <span className={styles.liveTag}>实时数据</span>
      </header>
      <div className={styles.stats}>
        <div className={styles.stat}>
          <span className={styles.statLabel}>账户权益 (USDT)</span>
          <span className={styles.statValue}>
            {balance == null ? "--" : <AnimatedCounter value={balance} decimals={2} />}
          </span>
        </div>
        <div className={styles.stat}>
          <span className={styles.statLabel}>未实现盈亏 (USDT)</span>
          <span className={styles.statValue} style={{ color: up ? chartColors.up : down ? chartColors.down : "#1c2333" }}>
            {pnl == null ? "--" : (
              <>
                {up ? "+" : down ? "-" : ""}
                <AnimatedCounter value={Math.abs(pnl)} decimals={2} />
              </>
            )}
          </span>
        </div>
        <div className={styles.stat}>
          <span className={styles.statLabel}>可用余额 (USDT)</span>
          <span className={styles.statValue}>
            {account.available == null ? "--" : <AnimatedCounter value={account.available} decimals={2} />}
          </span>
        </div>
      </div>
      <div className={styles.grid2}>
        <div className={hasSeries ? "" : styles.placeholder}>
          {hasSeries ? (
            <EquityChart samples={series} hue={hue} height={140} />
          ) : (
            <div className={styles.emptySmall}>
              <span className={styles.chartTitle}>权益走势</span>
              <div className={styles.emptyTxt}>权益采样积累中，稍后自动生成曲线</div>
            </div>
          )}
        </div>
        <div className={styles.posBox}>
          <span className={styles.chartTitle}>当前持仓</span>
          <ScrollArea className={styles.posScroll}>
            <div className={styles.posList}>
              {positions.map((p) => (
                <PositionRow key={p.symbol} p={p} chartColors={chartColors} />
              ))}
              {positions.length === 0 && (
                <div className={styles.emptyTxt}>该账户当前没有持仓</div>
              )}
            </div>
          </ScrollArea>
        </div>
      </div>
    </div>
  )
}
