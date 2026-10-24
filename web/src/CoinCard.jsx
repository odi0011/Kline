import { useEffect, useRef, useState } from 'react'
import { createChart, CandlestickSeries, HistogramSeries, ColorType } from 'lightweight-charts'
import { AnimatedCounter } from '@/components/arc/animated-counter/animated-counter'
import CoinAvatar from '@/components/ui/coin-avatar'
import { PATTERN_META } from './patterns_meta.js'

const rgba = (hex, a) => {
  const h = hex.replace('#', '')
  const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`
}

const pad = (n) => String(n).padStart(2, '0')
// ts 为 UTC 秒：+8h 后用 UTC 取值才是北京时间（本地时区取值会叠加成 +16h）
const bj = (t) => new Date(t * 1000 + 8 * 3600 * 1000)
const fmtTick = (t) => { const d = bj(t); return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}` }
const fmtCross = (t) => {
  const d = bj(t)
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
}

export default function CoinCard({ symbol, intervals, settings, live, chartColors, activeHit, onHoverHits }) {
  const hitActive = !!activeHit
  const boxRef = useRef(null)
  const chgRef = useRef(null)
  const chartRef = useRef(null)
  const candleRef = useRef(null)
  const volRef = useRef(null)
  const prevCloseRef = useRef(null)
  const lastDirRef = useRef('')
  const intervalRef = useRef(settings.interval)
  const settingsRef = useRef(settings)
  const hitUntilRef = useRef(0)
  const hitBarsRef = useRef(1)
  const hitDirRef = useRef('neutral')
  const onHoverHitsRef = useRef(onHoverHits)
  const ccRef = useRef(chartColors)
  ccRef.current = chartColors
  const [tick, setTick] = useState(null)   // { price, dir }
  const [hover, setHover] = useState(null) // { x, y, timeMs } 十字悬停
  const [barDetail, setBarDetail] = useState(null)
  const base = symbol.replace(/usdt$/, '')

  intervalRef.current = settings.interval
  settingsRef.current = settings
  onHoverHitsRef.current = onHoverHits

  const updateHeader = () => {
    const bars = live.store[symbol]?.[intervalRef.current]?.bars || []
    const last = bars[bars.length - 1]
    if (!last || !chgRef.current) return
    const first = bars[Math.max(0, bars.length - 40)]
    const pct = ((last.close - first.open) / first.open) * 100
    chgRef.current.textContent = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`
    chgRef.current.className = `chg ${pct >= 0 ? 'up' : 'down'}`
  }

  // 水墨映射：方向定墨色（青碧墨/赤墨/灰墨），参与形态的 N 根一起染色，
  // 逐根错相呼吸 → 水墨扩散层次。lightweight-charts 的 update() 只允许最新一根，
  // 所以多根染色必须走 setData 全量重绘。
  const INK = {
    bullish: { hue: 168, sat: 60 },
    bearish: { hue: 348, sat: 62 },
    neutral: { hue: 218, sat: 36 },
  }
  const inkOf = (barsAgo, count, t) => {
    const ink = INK[hitDirRef.current] || INK.neutral
    const hue = (ink.hue + (barsAgo - (count - 1) / 2) * 16 + 360) % 360
    const breath = 0.5 + 0.5 * Math.sin(t / 260 + barsAgo * 1.15)
    const light = 30 + breath * 17
    const sat = ink.sat + breath * 9
    return {
      color: `hsl(${hue.toFixed(0)} ${sat.toFixed(0)}% ${light.toFixed(0)}%)`,
      vol: `hsl(${hue.toFixed(0)} ${sat.toFixed(0)}% ${light.toFixed(0)}% / 0.35)`,
    }
  }

  const paintHit = () => {
    const bars = live.store[symbol]?.[intervalRef.current]?.bars || []
    if (!bars.length || !candleRef.current || !chartRef.current) return
    const count = Math.min(hitBarsRef.current, bars.length)
    const t = Date.now()
    const candles = []
    const vols = []
    for (let i = 0; i < bars.length; i++) {
      const b = bars[i]
      const barsAgo = bars.length - 1 - i
      const base = { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close }
      if (barsAgo < count) {
        const { color, vol } = inkOf(barsAgo, count, t)
        candles.push({ ...base, color, borderColor: color, wickColor: color })
        vols.push({ time: b.time, value: b.volume, color: vol })
      } else {
        const up = b.close >= b.open
        candles.push(base)
        vols.push({ time: b.time, value: b.volume, color: up ? rgba(ccRef.current.up, 0.3) : rgba(ccRef.current.down, 0.3) })
      }
    }
    candleRef.current.setData(candles)
    volRef.current.setData(vols)
    // setData 会重置可视范围：按设置的显示根数恢复
    chartRef.current.timeScale().setVisibleLogicalRange({
      from: Math.max(0, bars.length - (settingsRef.current.visibleBars || 70)),
      to: bars.length + 2,
    })
  }

  const applyLiveBar = () => {
    const bars = live.store[symbol]?.[intervalRef.current]?.bars || []
    const last = bars[bars.length - 1]
    if (!last || !candleRef.current) return
    const baseBar = { time: last.time, open: last.open, high: last.high, low: last.low, close: last.close }
    const cc = ccRef.current
    if (Date.now() < hitUntilRef.current) return   // 命中期间由 90ms 定时器统一水墨重绘
    candleRef.current.update(baseBar)
    volRef.current?.update({ time: last.time, value: last.volume, color: last.close >= last.open ? rgba(cc.up, 0.3) : rgba(cc.down, 0.3) })
  }

  const drawBar = (b) => {
    if (Date.now() < hitUntilRef.current) { applyLiveBar(); return }
    const cc = ccRef.current
    candleRef.current?.update({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })
    volRef.current?.update({
      time: b.time, value: b.volume, color: b.close >= b.open ? rgba(cc.up, 0.3) : rgba(cc.down, 0.3),
    })
  }

  const setData = () => {
    const bars = live.store[symbol]?.[intervalRef.current]?.bars || []
    candleRef.current?.setData(bars.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })))
    volRef.current?.setData(bars.map((b) => ({ time: b.time, value: b.volume, color: b.close >= b.open ? rgba(ccRef.current.up, 0.3) : rgba(ccRef.current.down, 0.3) })))
    if (bars.length) {
      const vb = settingsRef.current.visibleBars
      chartRef.current?.timeScale().setVisibleLogicalRange({
        from: Math.max(0, bars.length - vb), to: bars.length + 2,
      })
    }
  }

  // 图表创建 + 订阅全部周期（按当前设置路由）
  useEffect(() => {
    const s = settingsRef.current
    const chart = createChart(boxRef.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#7a8499', fontSize: 11, attributionLogo: false,
      },
      grid: { vertLines: { visible: s.showGrid, color: '#eef1f6' }, horzLines: { visible: s.showGrid, color: '#eef1f6' } },
      rightPriceScale: { borderVisible: false },
      timeScale: {
        borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 2,
        tickMarkFormatter: fmtTick,
      },
      localization: { timeFormatter: fmtCross },
      crosshair: { mode: 1 },
    })
    const cc = ccRef.current
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: cc.up, downColor: cc.down, wickUpColor: cc.up, wickDownColor: cc.down, borderVisible: false,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    })
    const vol = chart.addSeries(HistogramSeries, {
      visible: s.showVolume,
      priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false,
      priceLineVisible: false,
    })
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
    chartRef.current = chart
    candleRef.current = candle
    volRef.current = vol

    setData()
    const offs = intervals.map((iv) =>
      live.on(`candle|${symbol}|${iv}`, (b) => {
        if (iv !== intervalRef.current) return
        drawBar(b)
        setTick((prev) => {
          let dir = lastDirRef.current
          if (prev == null || b.close > prev.price) dir = 'up'
          else if (b.close < prev.price) dir = 'down'
          lastDirRef.current = dir
          return { price: b.close, dir }
        })
        updateHeader()
      }))
    // WS 重连后的新 init 快照：仓库被重建，图表按快照重置
    offs.push(live.on('init', () => { setData(); prevCloseRef.current = null; updateHeader() }))
    offs.push(live.on(`bar_detail|${symbol}`, (m) => {
      setBarDetail(m)
      // 悬停到有形态记录的K线 → 右侧图鉴滚动到对应形态并触发渐变效果
      if (m.hits && m.hits.length) onHoverHitsRef.current?.(m.hits, symbol)
      else onHoverHitsRef.current?.([], symbol)
    }))

    // 悬停K线：记录位置与时间，去抖后向服务端查询该根的存储记录
    let lastT = 0, lastQ = 0
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.point) { setHover(null); return }
      setHover({ x: param.point.x, y: param.point.y, timeMs: param.time * 1000 })
      if (param.time !== lastT) {
        lastT = param.time
        const now = Date.now()
        if (now - lastQ > 120) {
          lastQ = now
          setBarDetail(null)
          live.queryBar(symbol, intervalRef.current, param.time * 1000)
        }
      }
    })
    updateHeader()

    return () => { offs.forEach((f) => f()); chart.remove() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol])

  // 周期切换：重画 + 视图
  useEffect(() => {
    if (!chartRef.current) return
    setData()
    prevCloseRef.current = null
    updateHeader()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.interval])

  // 显示根数变化：重新套视图
  useEffect(() => {
    if (!chartRef.current) return
    const bars = live.store[symbol]?.[intervalRef.current]?.bars || []
    if (bars.length) {
      chartRef.current.timeScale().setVisibleLogicalRange({
        from: Math.max(0, bars.length - settings.visibleBars), to: bars.length + 2,
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.visibleBars])

  // 成交量 / 网格线开关
  useEffect(() => {
    volRef.current?.applyOptions({ visible: settings.showVolume })
    if (settings.showVolume) applyLiveBar()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.showVolume])

  useEffect(() => {
    chartRef.current?.applyOptions({
      grid: {
        vertLines: { visible: settings.showGrid, color: '#eef1f6' },
        horzLines: { visible: settings.showGrid, color: '#eef1f6' },
      },
    })
  }, [settings.showGrid])

  // 涨跌颜色变化：热更新系列配色并重绘最新K线
  useEffect(() => {
    if (!candleRef.current) return
    candleRef.current.applyOptions({
      upColor: chartColors.up, downColor: chartColors.down,
      wickUpColor: chartColors.up, wickDownColor: chartColors.down,
    })
    applyLiveBar()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartColors.up, chartColors.down])

  // 命中特效：参与形态的 N 根K线一起触发水墨映射（逐根错相呼吸）；
  // 到期恢复普通配色；hover 记录仍只落在最后一根K线上
  useEffect(() => {
    if (activeHit) {
      hitBarsRef.current = PATTERN_META[activeHit.pattern]?.bars || 1
      hitDirRef.current = activeHit.direction || 'neutral'
      hitUntilRef.current = Date.now() + 9500
      paintHit()
      const timer = setInterval(() => {
        if (Date.now() >= hitUntilRef.current) {
          clearInterval(timer)
          hitUntilRef.current = 0
          setData()               // 恢复普通配色 + 可视范围
        } else {
          paintHit()
        }
      }, 90)
      return () => clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeHit])

  const decimals = tick ? (tick.price >= 1000 ? 1 : tick.price >= 1 ? 3 : 4) : 2

  return (
    <section className="card">
      <div className="card-head">
        <CoinAvatar symbol={symbol} size={22} className={`coin-icon ${hitActive ? 'hit' : ''}`} />
        <span className="sym">{symbol.toUpperCase()}</span>
        <span className={`price ${tick ? tick.dir : ''}`}>
          <AnimatedCounter value={tick ? tick.price : 0} decimals={decimals} />
        </span>
        <span ref={chgRef} className="chg">--</span>
        <span className="iv-label">{intervalRef.current}</span>
      </div>
      <div ref={boxRef} className="chart-box">
        {hover && (
          <div className="hover-tip"
            style={{
              left: Math.min(hover.x + 14, (boxRef.current?.clientWidth || 300) - 200),
              top: Math.min(hover.y + 14, (boxRef.current?.clientHeight || 200) - 110),
            }}>
            <div className="ht-time">{fmtCross(hover.timeMs / 1000)}</div>
            {barDetail && barDetail.candle ? (
              <div className="ht-ohlc">
                O {barDetail.candle.open} H {barDetail.candle.high} L {barDetail.candle.low} C {barDetail.candle.close}
                <span className="ht-vol"> 量 {barDetail.candle.volume}</span>
              </div>
            ) : <div className="ht-ohlc dim">查询中…</div>}
            {barDetail && barDetail.hits && barDetail.hits.length > 0 ? (
              barDetail.hits.map((h2, i2) => (
                <div key={i2} className={`ht-hit ${h2.direction}`}>
                  {h2.name_zh} {h2.direction === 'bullish' ? '↑' : h2.direction === 'bearish' ? '↓' : '◆'} · {h2.mode === 'live' ? '盘中' : '收线'} @ {h2.price}
                  {h2.outcome === 'win' ? ' · ✓胜' : h2.outcome === 'loss' ? ' · ✗负' : h2.outcome === 'pending' ? ' · 待验证' : ''}
                </div>
              ))
            ) : barDetail && barDetail.candle ? <div className="ht-none">该K线无形态记录</div> : null}
          </div>
        )}
      </div>
    </section>
  )
}
