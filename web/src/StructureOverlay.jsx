/* K线结构叠加层：在 lightweight-charts 上自动画支撑压力位、趋势线、
   FVG/订单块区间、结构形态轮廓（头肩/三重/双顶底/三角/楔形）+ 目标位。

   实现方式：一个绝对定位的透明 canvas 覆盖在图表上，每次可视范围变化
   （滚动/缩放/新K线）都用图表自带的 timeToCoordinate / priceToCoordinate
   重新换算坐标重绘——这样画出来的线和K线严格对齐。
   注意：lightweight-charts 只保证 timeScale 的坐标查询对已存在的K线有效，
   超出数据范围的时间点由我们自己按最后可见K线的间距外推。 */
import { useCallback, useEffect, useRef } from 'react'

const COLORS = {
  support: '#16a34a',
  resistance: '#dc2626',
  trendlineSupport: 'rgba(22,163,74,0.75)',
  trendlineResistance: 'rgba(220,38,38,0.75)',
  fvgBull: 'rgba(22,163,74,0.16)',
  fvgBear: 'rgba(220,38,38,0.16)',
  obBull: 'rgba(59,130,246,0.15)',
  obBear: 'rgba(245,158,11,0.16)',
  patternUp: 'rgba(22,163,74,0.9)',
  patternDown: 'rgba(220,38,38,0.9)',
  neckline: 'rgba(28,35,51,0.5)',
  target: 'rgba(28,35,51,0.32)',
  label: 'rgba(28,35,51,0.62)',
}

const FONT = '10px "Douyin Sans", "Microsoft YaHei", system-ui, sans-serif'

function measure(ctx, text) {
  const prev = ctx.font
  ctx.font = `600 10.5px "Douyin Sans", "Microsoft YaHei", system-ui, sans-serif`
  const w = ctx.measureText(text).width
  ctx.font = prev
  return w
}

export default function StructureOverlay({ chartRef, seriesRef, structure, interval, visibleBars }) {
  const canvasRef = useRef(null)
  const wrapRef = useRef(null)
  const stRef = useRef(structure)
  const ivRef = useRef(interval)
  stRef.current = structure
  ivRef.current = interval

  // 时间戳 → x 坐标。优先用图表自身映射；超界时按最后一根的像素间距外推。
  const makeX = useCallback((chart) => {
    const ts = chart.timeScale()
    const bars = stRef.current?.__bars || []
    const lastBar = bars[bars.length - 1]
    let lastX = null
    let stepPx = 6
    if (lastBar) {
      const x1 = ts.timeToCoordinate(lastBar.time)
      if (x1 != null && bars.length > 1) {
        const prev = bars[bars.length - 2]
        const x0 = ts.timeToCoordinate(prev.time)
        if (x0 != null && x1 !== x0) stepPx = x1 - x0
        lastX = x1
      }
    }
    return (tMs) => {
      const sec = Math.round(tMs / 1000)
      const x = ts.timeToCoordinate(sec)
      if (x != null) return x
      if (lastX != null && lastBar) {
        const steps = (sec - lastBar.time) / (ivSec(ivRef.current) || 60)
        return lastX + steps * stepPx
      }
      return null
    }
  }, [])

  const draw = useCallback(() => {
    const chart = chartRef.current
    const series = seriesRef?.current
    const canvas = canvasRef.current
    const wrap = wrapRef.current
    const st = stRef.current
    if (!chart || !series || !canvas || !wrap) return
    const w = wrap.clientWidth
    const h = wrap.clientHeight
    if (!w || !h) return
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr)
      canvas.height = Math.round(h * dpr)
      canvas.style.width = w + 'px'
      canvas.style.height = h + 'px'
    }
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, w, h)
    if (!st) return

    const cs = chart.timeScale()
    // lightweight-charts v5：priceToCoordinate 在 series 上（不在 chart 上）
    const yOf = (price) => {
      const c = series.priceToCoordinate(price)
      return c == null ? null : c
    }
    const xOf = makeX(chart)
    const plotW = cs.width ? cs.width() : w
    const plotH = cs.height ? cs.height() : h

    /* ---- 1. FVG / 订单块区间（先画，压在K线下面一层视觉） ---- */
    for (const z of st.zones || []) {
      const y1 = yOf(z.top), y2 = yOf(z.bottom)
      if (y1 == null || y2 == null) continue
      const x1 = xOf(z.from)
      if (x1 == null) continue
      const x2 = Math.min(plotW, Math.max(x1 + 20, plotW))
      const top = Math.min(y1, y2), bot = Math.max(y1, y2)
      ctx.fillStyle = z.kind === 'fvg'
        ? (z.dir === 'bull' ? COLORS.fvgBull : COLORS.fvgBear)
        : (z.dir === 'bull' ? COLORS.obBull : COLORS.obBear)
      ctx.fillRect(Math.max(0, x1), top, x2 - Math.max(0, x1), Math.max(2, bot - top))
      ctx.strokeStyle = z.kind === 'fvg'
        ? (z.dir === 'bull' ? 'rgba(22,163,74,0.45)' : 'rgba(220,38,38,0.45)')
        : (z.dir === 'bull' ? 'rgba(59,130,246,0.5)' : 'rgba(245,158,11,0.5)')
      ctx.lineWidth = 1
      ctx.strokeRect(Math.max(0, x1) + 0.5, top + 0.5, Math.max(4, x2 - Math.max(0, x1) - 1), Math.max(1, bot - top - 1))
      ctx.font = FONT
      ctx.fillStyle = COLORS.label
      ctx.textBaseline = 'middle'
      ctx.fillText(z.label, Math.max(0, x1) + 4, top + Math.min(9, (bot - top) / 2))
    }

    /* ---- 2. 支撑压力位（水平虚线 + 触碰次数） ---- */
    const usedLabelY = []
    for (const lv of st.levels || []) {
      const y = yOf(lv.price)
      if (y == null || y < 0 || y > plotH) continue
      ctx.save()
      ctx.setLineDash([5, 4])
      ctx.strokeStyle = lv.kind === 'support' ? COLORS.support : COLORS.resistance
      ctx.globalAlpha = 0.35 + Math.min(0.45, lv.strength * 0.45)
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(0, y + 0.5)
      ctx.lineTo(plotW, y + 0.5)
      ctx.stroke()
      ctx.restore()
      ctx.font = FONT
      ctx.fillStyle = lv.kind === 'support' ? COLORS.support : COLORS.resistance
      const label = `${lv.kind === 'support' ? '支撑' : '压力'} ${lv.price} ×${lv.touches}`
      ctx.textBaseline = 'bottom'
      // 优先放线的上方；太靠顶（会被裁）或撞到已有标签时依次下移
      let ly = y - 3
      if (ly < 12) ly = y + 12
      let guard = 0
      while (usedLabelY.some((v) => Math.abs(v - ly) < 11) && guard++ < 8) ly += 11
      if (ly > plotH - 4) ly = y - 3
      usedLabelY.push(ly)
      ctx.textBaseline = ly > y ? 'top' : 'bottom'
      ctx.fillText(label, 34, ly)
    }

    /* ---- 3. 趋势线 ---- */
    for (const tl of st.trendlines || []) {
      const x1 = xOf(tl.p1[0]), x2 = xOf(tl.p2[0])
      const y1 = yOf(tl.p1[1]), y2 = yOf(tl.p2[1])
      if (x1 == null || x2 == null || y1 == null || y2 == null) continue
      if (x1 > x2) continue
      // 延长到右边界
      let ex = plotW, ey = y2
      if (x2 !== x1) ey = y2 + (y2 - y1) * (plotW - x2) / (x2 - x1)
      ctx.save()
      ctx.strokeStyle = tl.kind === 'support' ? COLORS.trendlineSupport : COLORS.trendlineResistance
      ctx.lineWidth = 1.6
      ctx.beginPath()
      ctx.moveTo(x1, y1)
      ctx.lineTo(x2, y2)
      if (ey > 0 && ey < plotH) ctx.lineTo(ex, ey)
      ctx.stroke()
      ctx.restore()
      ctx.font = FONT
      ctx.fillStyle = COLORS.label
      ctx.textBaseline = 'middle'
      const lmx = (x1 + x2) / 2
      ctx.fillText('趋势线', Math.max(2, lmx + 4), (y1 + y2) / 2 - 7)
    }

    /* ---- 4. 结构形态轮廓 + 颈线 + 目标位 ---- */
    const p = st.pattern
    if (p && p.points?.length >= 2) {
      const col = p.direction === 'bearish' ? COLORS.patternDown : COLORS.patternUp
      const pts = p.points
        .map(([t, price]) => [xOf(t), yOf(price)])
        .filter(([x, y]) => x != null && y != null)

      if (pts.length >= 2) {
        // 轮廓：穿过各个肩/头/底的折线
        ctx.save()
        ctx.strokeStyle = col
        ctx.lineWidth = 1.8
        ctx.globalAlpha = 0.85
        ctx.beginPath()
        pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)))
        ctx.stroke()
        // 摆动点小圆点
        ctx.fillStyle = col
        pts.forEach(([x, y]) => {
          ctx.beginPath()
          ctx.arc(x, y, 2.6, 0, Math.PI * 2)
          ctx.fill()
        })
        ctx.restore()

        // 颈线：从第二个点延伸到右边界
        if (p.neckline) {
          const [a, b] = p.neckline
          const x1 = xOf(a[0]), y1 = yOf(a[1])
          const x2 = xOf(b[0]), y2 = yOf(b[1])
          if (x1 != null && x2 != null && y1 != null && y2 != null) {
            ctx.save()
            ctx.setLineDash([6, 4])
            ctx.strokeStyle = COLORS.neckline
            ctx.lineWidth = 1.2
            ctx.beginPath()
            ctx.moveTo(x1, y1)
            ctx.lineTo(x2, y2)
            let ex = plotW
            let ey = y2
            if (x2 !== x1) ey = y2 + (y2 - y1) * (plotW - x2) / (x2 - x1)
            if (ey > 0 && ey < plotH) ctx.lineTo(ex, ey)
            ctx.stroke()
            ctx.restore()
            ctx.font = FONT
            ctx.fillStyle = COLORS.label
            ctx.textBaseline = 'middle'
            ctx.fillText('颈线', Math.max(0, x2) + 4, y2)
          }
        }

        // 目标位（量度涨跌幅）
        if (p.target) {
          const ty = yOf(p.target)
          if (ty != null && ty > 0 && ty < plotH) {
            ctx.save()
            ctx.setLineDash([2, 4])
            ctx.strokeStyle = COLORS.target
            ctx.lineWidth = 1
            ctx.beginPath()
            ctx.moveTo(Math.max(0, pts[pts.length - 1][0]), ty)
            ctx.lineTo(plotW, ty)
            ctx.stroke()
            ctx.restore()
            ctx.font = FONT
            ctx.fillStyle = COLORS.label
            ctx.textBaseline = 'middle'
            ctx.fillText(`目标 ${p.target}`, Math.max(0, pts[pts.length - 1][0]) + 4, ty)
          }
        }

        // 形态名标签：放在轮廓最右端上方（左边界留给坐标轴，避免压住K线）
        const lastPt = pts[pts.length - 1]
        const highPt = pts.reduce((m, q, i) => (q[1] < pts[m][1] ? i : m), 0)
        const labelX = Math.min(plotW - 8 - measure(ctx, `${p.name_zh} · 构筑中`), Math.max(4, lastPt[0] + 6))
        const labelY = Math.max(14, Math.min(lastPt[1], pts[highPt][1]) - 8)
        ctx.font = `600 10.5px "Douyin Sans", "Microsoft YaHei", system-ui, sans-serif`
        ctx.fillStyle = col
        ctx.textBaseline = 'bottom'
        const state = p.status === 'confirmed' ? '确认' : '构筑中'
        ctx.fillText(`${p.name_zh} · ${state}`, labelX, labelY)
      }
    }

    /* ---- 5. 右上角图例（说明画面里的线是什么） ---- */
    const legend = []
    if ((st.levels || []).length) legend.push(['支撑/压力', COLORS.resistance])
    if ((st.trendlines || []).length) legend.push(['趋势线', COLORS.trendlineSupport])
    if ((st.zones || []).length) legend.push(['FVG/OB', 'rgba(59,130,246,0.7)'])
    if (p) legend.push([p.name_zh, p.direction === 'bearish' ? COLORS.patternDown : COLORS.patternUp])
    if (legend.length) {
      ctx.font = FONT
      ctx.textBaseline = 'middle'
      let lx = plotW - 6
      for (let i = legend.length - 1; i >= 0; i--) {
        const [text, color] = legend[i]
        const tw = ctx.measureText(text).width
        lx -= tw + 12
        ctx.fillStyle = color
        ctx.fillRect(lx, 7, 8, 2)
        ctx.fillStyle = 'rgba(28,35,51,0.55)'
        ctx.fillText(text, lx + 11, 8)
      }
    }
  }, [chartRef, seriesRef, makeX])

  // 可视范围/尺寸变化 → 重画
  useEffect(() => {
    const chart = chartRef.current
    const wrap = wrapRef.current
    if (!chart || !wrap) return
    let raf = 0
    const schedule = () => {
      if (raf) return
      raf = requestAnimationFrame(() => { raf = 0; draw() })
    }
    schedule()
    const ts = chart.timeScale()
    const unsub = ts.subscribeVisibleLogicalRangeChange ? ts.subscribeVisibleLogicalRangeChange(schedule) : null
    const ro = new ResizeObserver(schedule)
    ro.observe(wrap)
    return () => {
      if (raf) cancelAnimationFrame(raf)
      if (unsub) ts.unsubscribeVisibleLogicalRangeChange(schedule)
      ro.disconnect()
    }
  }, [chartRef, seriesRef, draw])

  // 结构更新（新收线）→ 重画
  useEffect(() => {
    const id = requestAnimationFrame(() => draw())
    return () => cancelAnimationFrame(id)
  }, [structure, draw])

  return (
    <div ref={wrapRef} className="struct-overlay" aria-hidden>
      <canvas ref={canvasRef} />
    </div>
  )
}

function ivSec(iv) {
  const m = { '1s': 1, '5s': 5, '15s': 15, '30s': 30, '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '2h': 7200, '4h': 14400, '1d': 86400 }
  return m[iv] || 60
}
