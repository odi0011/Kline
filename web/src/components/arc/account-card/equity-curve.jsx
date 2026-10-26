/* 权益曲线（SVG sparkline）。
   参考 uiarc Sparkline 的画法：先分桶均值降采样吃掉高频噪声，再用单调三次插值
   （Fritsch–Carlson，保证曲线不冲过数据点）算密集采样点，最后用 SVG 折线绘制。
   相比把上千个原始点直接丢给图表库，小尺寸下不会糊成锯齿。
   hover 按横坐标就近取点，回调上层联动权益数字。
   绘制使用实测像素尺寸（不用 viewBox 拉伸），保证描边和 hover 圆点不会被拉变形。 */
import { useLayoutEffect, useMemo, useRef, useState } from "react"

const INSET = 4          // 上下留白（px）
const MAX_POINTS = 48    // 降采样后参与插值的点数（更少 = 更平滑）
const SAMPLES_PER_SEG = 12

/** 分桶均值降采样：把 n 个点压到最多 max 个，消掉逐点噪声，同时保留时间顺序。 */
function decimate(samples, max = MAX_POINTS) {
  const n = samples.length
  if (!n) return []
  // 先做 3 点移动平均（首尾保持原值）：原始采样里 ±几 U 的抖动会在小尺寸曲线上
  // 表现为台阶/毛刺，先抹掉再降采样，曲线才顺。
  const softened = samples.map((s, i) => {
    if (i === 0 || i === n - 1) return { t: s.t, bal: s.bal, upnl: s.upnl ?? 0 }
    const a = samples[i - 1], b = samples[i + 1]
    return {
      t: s.t,
      bal: (a.bal + s.bal * 2 + b.bal) / 4,
      upnl: ((a.upnl ?? 0) + (s.upnl ?? 0) * 2 + (b.upnl ?? 0)) / 4,
    }
  })
  samples = softened
  if (n <= max) return samples.map((s) => ({ t: s.t, bal: s.bal, upnl: s.upnl }))
  const out = []
  for (let i = 0; i < max; i++) {
    const from = Math.floor((i * n) / max)
    const to = Math.max(from + 1, Math.floor(((i + 1) * n) / max))
    let bal = 0, upnl = 0, count = 0
    for (let j = from; j < to; j++) {
      bal += samples[j].bal
      upnl += samples[j].upnl ?? 0
      count++
    }
    const mid = samples[Math.min(n - 1, Math.floor((from + to) / 2))]
    out.push({ t: mid.t, bal: bal / count, upnl: upnl / count })
  }
  return out
}

/** 单调三次 Hermite 插值（Fritsch–Carlson）：相邻斜率异号时切线为 0，不会出现过冲假峰。 */
function smoothPath(pts) {
  const n = pts.length
  if (n === 0) return ""
  if (n === 1) return `M${pts[0][0].toFixed(2)},${pts[0][1].toFixed(2)}`
  if (n === 2) {
    return `M${pts[0][0].toFixed(2)},${pts[0][1].toFixed(2)}L${pts[1][0].toFixed(2)},${pts[1][1].toFixed(2)}`
  }
  const h = [], slope = []
  for (let i = 0; i < n - 1; i++) {
    h[i] = pts[i + 1][0] - pts[i][0]
    slope[i] = h[i] === 0 ? 0 : (pts[i + 1][1] - pts[i][1]) / h[i]
  }
  const m = new Array(n)
  m[0] = slope[0]
  m[n - 1] = slope[n - 2]
  for (let i = 1; i < n - 1; i++) {
    const a = slope[i - 1], b = slope[i]
    m[i] = a * b <= 0 ? 0 : (2 * a * b) / (a + b)
  }
  let d = `M${pts[0][0].toFixed(2)},${pts[0][1].toFixed(2)}`
  for (let i = 0; i < n - 1; i++) {
    const seg = h[i]
    const x0 = pts[i][0], y0 = pts[i][1]
    const x1 = pts[i + 1][0], y1 = pts[i + 1][1]
    for (let s = 1; s <= SAMPLES_PER_SEG; s++) {
      const t = s / SAMPLES_PER_SEG
      const t2 = t * t, t3 = t2 * t
      const y = (2 * t3 - 3 * t2 + 1) * y0
        + (t3 - 2 * t2 + t) * seg * m[i]
        + (3 * t2 - 2 * t3) * y1
        + (t3 - t2) * seg * m[i + 1]
      d += `L${(x0 + seg * t).toFixed(2)},${y.toFixed(2)}`
    }
  }
  return d
}

export default function EquityCurve({ samples, hue, height = 46, onHover }) {
  const wrapRef = useRef(null)
  const [box, setBox] = useState({ w: 0, h: height })
  const [hoverIdx, setHoverIdx] = useState(null)
  const pts = useMemo(() => decimate(samples || []), [samples])

  // 实测容器尺寸：SVG 按真实像素绘制，描边/圆点都不会被拉伸变形
  useLayoutEffect(() => {
    const node = wrapRef.current
    if (!node) return
    const measure = () => {
      const w = node.clientWidth
      if (w) setBox((cur) => (Math.abs(cur.w - w) < 0.5 && cur.h === height ? cur : { w, h: height }))
    }
    measure()
    if (typeof ResizeObserver === "undefined") return
    const ro = new ResizeObserver(measure)
    ro.observe(node)
    return () => ro.disconnect()
  }, [height])

  const geo = useMemo(() => {
    if (!pts.length || !box.w) return null
    const vals = pts.map((s) => s.bal)
    const min = Math.min(...vals)
    const max = Math.max(...vals)
    const span = max - min
    const innerH = Math.max(1, box.h - INSET * 2)
    const toY = (v) => INSET + (span > 0 ? (1 - (v - min) / span) * innerH : innerH / 2)
    const toX = (i) => (pts.length === 1 ? box.w : (i / (pts.length - 1)) * box.w)
    const coords = pts.map((s, i) => [toX(i), toY(s.bal)])
    const line = smoothPath(coords)
    return { coords, line, area: `${line}L${box.w},${box.h}L0,${box.h}Z` }
  }, [pts, box.w, box.h])

  const stroke = `hsl(${hue} 52% 30%)`
  const fillTop = `hsl(${hue} 62% 46% / 0.28)`
  const gradId = `eqg${Math.round(hue)}${Math.round(box.w)}`

  const handleMove = (e) => {
    if (!pts.length || !box.w) return
    const rect = e.currentTarget.getBoundingClientRect()
    if (!rect.width) return
    const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width))
    const idx = pts.length === 1 ? 0 : Math.round(ratio * (pts.length - 1))
    if (idx !== hoverIdx) {
      setHoverIdx(idx)
      onHover && onHover({ bal: pts[idx].bal, upnl: pts[idx].upnl })
    }
  }
  const handleLeave = () => {
    setHoverIdx(null)
    onHover && onHover(null)
  }

  const hoverPt = hoverIdx != null && geo ? geo.coords[hoverIdx] : null

  return (
    <div
      ref={wrapRef}
      style={{ height, width: "100%", position: "relative", cursor: "crosshair" }}
      onPointerMove={handleMove}
      onPointerLeave={handleLeave}
    >
      {geo && (
        <svg width={box.w} height={box.h} style={{ display: "block", overflow: "visible" }} aria-hidden>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={fillTop} />
              <stop offset="100%" stopColor={`hsl(${hue} 62% 46% / 0.02)`} />
            </linearGradient>
          </defs>
          <path d={geo.area} fill={`url(#${gradId})`} stroke="none" />
          <path d={geo.line} fill="none" stroke={stroke} strokeWidth={1.8}
            strokeLinecap="round" strokeLinejoin="round" />
          {hoverPt && (
            <>
              <line x1={hoverPt[0]} y1={0} x2={hoverPt[0]} y2={box.h}
                stroke={`hsl(${hue} 40% 40% / 0.26)`} strokeWidth={1} strokeDasharray="3 3" />
              <circle cx={hoverPt[0]} cy={hoverPt[1]} r={2.6} fill={stroke}
                stroke="rgba(255,255,255,0.95)" strokeWidth={1.2} />
            </>
          )}
        </svg>
      )}
    </div>
  )
}
