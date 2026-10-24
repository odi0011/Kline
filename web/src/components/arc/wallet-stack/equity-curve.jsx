/* 账户权益迷你曲线：平滑曲线 + 渐变面积填充，hover 竖线与圆点并回调采样值。
   samples: [{ t, bal, upnl }]（时间升序，最少 1 个）。
   hover 用原生事件监听（挂载在元素上），不依赖合成事件委托。 */
import { useEffect, useMemo, useRef, useState } from "react"

function smoothPath(pts) {
  if (pts.length < 2) return ""
  let d = `M ${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)]
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6]
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6]
    d += ` C ${c1[0].toFixed(1)} ${c1[1].toFixed(1)}, ${c2[0].toFixed(1)} ${c2[1].toFixed(1)}, ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`
  }
  return d
}

export default function EquityCurve({ samples, hue, height = 62, onHover }) {
  const wrapRef = useRef(null)
  const [hoverIdx, setHoverIdx] = useState(null)
  const W = 100, H = 100   // viewBox 归一化坐标，按容器比例拉伸

  const geom = useMemo(() => {
    if (!samples || !samples.length) return null
    const padY = 14
    const vals = samples.map((s) => s.bal)
    let min = Math.min(...vals), max = Math.max(...vals)
    if (max - min < 1e-9) { min -= 1; max += 1 }
    const n = samples.length
    const xs = samples.map((_, i) => (n === 1 ? W / 2 : (i / (n - 1)) * W))
    const ys = vals.map((v) => H - padY - ((v - min) / (max - min)) * (H - padY * 2))
    const pts = xs.map((x, i) => [x, ys[i]])
    const line = smoothPath(pts)
    const area = pts.length > 1 ? `${line} L ${W} ${H} L 0 ${H} Z` : ""
    return { pts, line, area }
  }, [samples])

  // 最新值走 ref，监听器不必随数据重建
  const geomRef = useRef(geom)
  geomRef.current = geom
  const samplesRef = useRef(samples)
  samplesRef.current = samples
  const onHoverRef = useRef(onHover)
  onHoverRef.current = onHover

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const pick = (clientX) => {
      const g = geomRef.current, ss = samplesRef.current
      if (!el || !g || !ss || !ss.length) return
      const r = el.getBoundingClientRect()
      const fx = Math.max(0, Math.min(1, (clientX - r.left) / r.width))
      let idx = Math.round(fx * (ss.length - 1))
      if (ss.length === 1) idx = 0
      setHoverIdx(idx)
      onHoverRef.current && onHoverRef.current(ss[idx])
    }
    const move = (e) => pick(e.clientX)
    const leave = () => { setHoverIdx(null); onHoverRef.current && onHoverRef.current(null) }
    el.addEventListener("pointermove", move)
    el.addEventListener("pointerleave", leave)
    return () => {
      el.removeEventListener("pointermove", move)
      el.removeEventListener("pointerleave", leave)
    }
  }, [])

  const hoverPt = hoverIdx != null && geom ? geom.pts[hoverIdx] : null
  const c1 = `hsl(${hue} 70% 46%)`
  const c2 = `hsl(${(hue + 30) % 360} 65% 60%)`

  return (
    <div ref={wrapRef} className="ecurve" style={{ height }}>
      {geom && (
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ width: "100%", height: "100%", display: "block" }}>
          <defs>
            <linearGradient id={`ecg-${hue}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={c1} stopOpacity="0.34" />
              <stop offset="100%" stopColor={c2} stopOpacity="0.04" />
            </linearGradient>
            <linearGradient id={`ecl-${hue}`} x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor={c2} />
              <stop offset="100%" stopColor={c1} />
            </linearGradient>
          </defs>
          {geom.area && <path d={geom.area} fill={`url(#ecg-${hue})`} />}
          <path d={geom.line} fill="none" stroke={`url(#ecl-${hue})`} strokeWidth="2.4"
            vectorEffect="non-scaling-stroke" strokeLinecap="round" />
        </svg>
      )}
      {hoverPt && (
        <svg className="ecurve-hover" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
          style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }}>
          <line x1={hoverPt[0]} y1="0" x2={hoverPt[0]} y2={H} stroke="rgba(28,35,51,0.28)" strokeWidth="1" vectorEffect="non-scaling-stroke" />
          <circle cx={hoverPt[0]} cy={hoverPt[1]} r="3.4" fill={c1} stroke="#fff" strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
        </svg>
      )}
    </div>
  )
}
