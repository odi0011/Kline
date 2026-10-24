/* 账户权益迷你曲线：直接用项目已有的 lightweight-charts AreaSeries（真实组件）。
   单点也铺平线可见；hover 十字由库提供，联动上方余额/盈亏动画数字。 */
import { useEffect, useRef } from "react"
import { createChart, AreaSeries, ColorType } from "lightweight-charts"

export default function EquityCurve({ samples, hue, height = 62, onHover }) {
  const wrapRef = useRef(null)
  const chartRef = useRef(null)
  const areaRef = useRef(null)
  const samplesRef = useRef(samples)
  samplesRef.current = samples
  const onHoverRef = useRef(onHover)
  onHoverRef.current = onHover

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "transparent",
        attributionLogo: false,
      },
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
      rightPriceScale: { visible: false },
      timeScale: { visible: false, borderVisible: false },
      handleScroll: false,
      handleScale: false,
      crosshair: {
        mode: 0,
        vertLine: { color: "rgba(28,35,51,0.3)", labelBackgroundColor: "rgba(28,35,51,0.6)" },
        horzLine: { color: "rgba(28,35,51,0.3)", labelBackgroundColor: "rgba(28,35,51,0.6)" },
      },
    })
    const area = chart.addSeries(AreaSeries, {
      lineColor: `hsl(${hue} 70% 46%)`,
      topColor: `hsl(${hue} 70% 52% / 0.5)`,
      bottomColor: `hsl(${hue} 70% 52% / 0.03)`,
      lineWidth: 2,
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerRadius: 4,
      crosshairMarkerBorderColor: "rgba(255,255,255,0.95)",
      crosshairMarkerBackgroundColor: `hsl(${hue} 70% 46%)`,
    })
    chartRef.current = chart
    areaRef.current = area
    applyData()

    // hover 十字 → 就近采样回调（余额/盈亏数字联动）
    chart.subscribeCrosshairMove((param) => {
      const d = param.seriesData.get(area)
      if (!d) { onHoverRef.current && onHoverRef.current(null); return }
      const tSec = param.time
      const ss = samplesRef.current || []
      let nearest = null
      for (const s of ss) {
        const st = Math.round(s.t / 1000)
        if (nearest === null || Math.abs(st - tSec) < Math.abs(Math.round(nearest.t / 1000) - tSec)) nearest = s
      }
      onHoverRef.current && onHoverRef.current({ bal: d.value, upnl: nearest ? nearest.upnl : null })
    })

    return () => chart.remove()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function applyData() {
    const area = areaRef.current, chart = chartRef.current
    if (!area || !chart) return
    const ss = samplesRef.current || []
    let data = ss.map((s) => ({ time: Math.round(s.t / 1000), value: s.bal }))
    if (data.length === 1) {
      // 单点铺成平线，保证曲线始终可见
      data = [{ time: data[0].time - 120, value: data[0].value }, data[0]]
    }
    area.setData(data)
    chart.timeScale().fitContent()
  }

  useEffect(() => { applyData() }, [samples])

  // 色相变化：热更新面积曲线配色
  useEffect(() => {
    areaRef.current?.applyOptions({
      lineColor: `hsl(${hue} 70% 46%)`,
      topColor: `hsl(${hue} 70% 52% / 0.5)`,
      bottomColor: `hsl(${hue} 70% 52% / 0.03)`,
      crosshairMarkerBackgroundColor: `hsl(${hue} 70% 46%)`,
    })
  }, [hue])

  return <div ref={wrapRef} className="ecurve" style={{ height }} />
}
