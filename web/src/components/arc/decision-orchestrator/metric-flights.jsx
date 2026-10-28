/* 高频指标飞线：左侧指标条 → 星云里随机的小节点。
   独立 canvas，细线加长拖尾。路径是带种子噪声的不规则三次曲线
   （偶尔两段），横向要弯得出来，不再整圈打转后拉一条直线。 */
import { useEffect, useRef } from "react"
import styles from "./metric-flights.module.css"

const FADE_MS = 680

const hash = (text) => {
  let value = 0
  for (const char of String(text)) value = (value * 31 + char.charCodeAt(0)) >>> 0
  return value
}

const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)
const clamp01 = (value) => Math.min(1, Math.max(0, value))
const clamp = (value, low, high) => Math.min(high, Math.max(low, value))
const cubicSegment = (p0, c1, c2, p3) => ({ p0, c1, c2, p3 })
const noise01 = (seed, shift) => ((seed >>> shift) & 1023) / 1023

const pointInSegment = (segment, t) => {
  const mt = 1 - t
  return {
    x: mt * mt * mt * segment.p0.x + 3 * mt * mt * t * segment.c1.x
      + 3 * mt * t * t * segment.c2.x + t * t * t * segment.p3.x,
    y: mt * mt * mt * segment.p0.y + 3 * mt * mt * t * segment.c1.y
      + 3 * mt * t * t * segment.c2.y + t * t * t * segment.p3.y,
  }
}

const pointInSegments = (segments, t) => {
  if (!segments.length) return { x: 0, y: 0 }
  const scaled = clamp01(t) * segments.length
  const index = Math.min(segments.length - 1, Math.floor(scaled))
  const local = index === segments.length - 1 && t >= 1 ? 1 : scaled - index
  return pointInSegment(segments[index], clamp01(local))
}

const pointOnPath = (path, t) => {
  const total = path.parts.reduce((sum, part) => sum + part.weight, 0)
  let remain = clamp01(t) * total
  for (let index = 0; index < path.parts.length; index += 1) {
    const part = path.parts[index]
    if (remain <= part.weight || index === path.parts.length - 1) {
      return pointInSegments(part.segments, remain / part.weight)
    }
    remain -= part.weight
  }
  return path.p3
}

function makeCurve(from, to, pulse) {
  const dx = to.x - from.x
  const dy = to.y - from.y
  const distance = Math.hypot(dx, dy) || 1
  const ux = dx / distance
  const uy = dy / distance
  const nx = -uy
  const ny = ux
  const seed = hash(String(pulse?.id ?? "pulse"))
  const sign = (seed & 1) ? 1 : -1
  const n = (shift) => noise01(seed, shift)
  // 弯曲跟距离走，长途不会被收成一条近直线，短途也不会甩成圆。
  const minBend = distance * 0.28
  const maxBend = distance * 0.46
  const mag = (shift, floor) => minBend * floor + n(shift) * (maxBend - minBend * floor)
  const at = (along, side) => ({
    x: from.x + ux * along + nx * side,
    y: from.y + uy * along + ny * side,
  })

  // 约两成拆成两段，其余一段。控制点沿飞行方向往前排，并待在弦线同一侧，
  // 收尾的控制点还要再往外偏，避免弯完之后剩一截直线。
  const split = (seed % 5) === 0
  if (split) {
    const side = sign * mag(3, 0.86)
    const midAlong = distance * (0.38 + n(6) * 0.12)
    const endSpan = distance - midAlong
    const s = Math.abs(side)
    const c1Along = distance * (0.1 + n(8) * 0.08)
    const c2Along = clamp(midAlong - distance * (0.08 + n(10) * 0.05), c1Along + distance * 0.06, midAlong - distance * 0.04)
    const f3 = 0.22 + n(12) * 0.12
    const f4 = 0.58 + n(14) * 0.16
    const c3Along = midAlong + endSpan * f3
    const c4Along = midAlong + endSpan * Math.max(f4, f3 + 0.18)
    const bulge = s * (0.22 + n(20) * 0.16)
    const w1 = clamp(midAlong / distance, 0.36, 0.56)
    return {
      p0: from,
      p3: to,
      parts: [
        {
          segments: [cubicSegment(
            from,
            at(c1Along, sign * s * (0.48 + n(16) * 0.24)),
            at(c2Along, sign * s * (0.8 + n(18) * 0.18)),
            at(midAlong, side),
          )],
          weight: w1,
        },
        {
          segments: [cubicSegment(
            at(midAlong, side),
            at(c3Along, side * (1 - f3) + sign * bulge),
            at(c4Along, side * (1 - Math.max(f4, f3 + 0.18)) + sign * bulge * (0.65 + n(22) * 0.4)),
            to,
          )],
          weight: 1 - w1,
        },
      ],
    }
  }

  const bow1 = sign * mag(3, 0.94)
  const bow2 = sign * Math.abs(bow1) * (0.58 + n(15) * 0.38)
  const t1 = 0.18 + n(5) * 0.16
  const t2 = 0.56 + n(7) * 0.22
  const shear = (n(19) - 0.5) * distance * 0.05
  return {
    p0: from,
    p3: to,
    parts: [{
      segments: [cubicSegment(
        from,
        at(clamp(distance * t1 + shear, distance * 0.08, distance * 0.42), bow1),
        at(clamp(distance * t2 - shear * 0.35, distance * 0.5, distance * 0.86), bow2),
        to,
      )],
      weight: 1,
    }],
  }
}

function pickTarget(pulse) {
  const nodes = document.querySelectorAll("[data-metric-node]")
  const visible = []
  for (const el of nodes) {
    const rect = el.getBoundingClientRect()
    const x = rect.left + rect.width / 2
    const y = rect.top + rect.height / 2
    if (rect.width < 2 || rect.height < 2) continue
    if (x < 4 || y < 4 || x > window.innerWidth - 4 || y > window.innerHeight - 4) continue
    visible.push({ x, y })
  }
  if (visible.length) {
    return visible[hash(String(pulse?.id ?? "x")) % visible.length]
  }
  const intake = document.querySelector("[data-metric-intake]")
  if (!intake) return null
  const rect = intake.getBoundingClientRect()
  if (rect.width < 2 || rect.height < 2) return null
  return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
}

export default function MetricFlights({ pulses, onDone }) {
  const canvasRef = useRef(null)
  const startedRef = useRef(new Map())
  const curvesRef = useRef(new Map())
  const doneRef = useRef(new Set())
  const onDoneRef = useRef(onDone)
  onDoneRef.current = onDone

  useEffect(() => {
    if (!pulses.length) return
    const canvas = canvasRef.current
    if (!canvas) return
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const W = window.innerWidth
    const H = window.innerHeight
    canvas.width = Math.round(W * dpr)
    canvas.height = Math.round(H * dpr)
    canvas.style.width = `${W}px`
    canvas.style.height = `${H}px`
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    for (const pulse of pulses) {
      if (doneRef.current.has(pulse.id)) continue
      if (!startedRef.current.has(pulse.id)) {
        const target = pickTarget(pulse)
        if (!target) continue
        startedRef.current.set(pulse.id, performance.now() + (pulse.delay || 0))
        curvesRef.current.set(pulse.id, makeCurve(pulse.source, target, pulse))
      }
    }

    let raf = 0
    const draw = (now) => {
      ctx.clearRect(0, 0, W, H)
      let alive = false
      for (const pulse of pulses) {
        const curve = curvesRef.current.get(pulse.id)
        const start = startedRef.current.get(pulse.id)
        if (!curve || start == null) continue
        const elapsed = now - start
        if (elapsed < 0) {
          alive = true
          continue
        }
        const spawn = pulse.spawn ?? 170
        const duration = Math.max(1, pulse.duration || 1400)
        const travelElapsed = elapsed - spawn

        if (travelElapsed < 0) {
          const p = Math.min(1, elapsed / Math.max(1, spawn))
          ctx.save()
          ctx.strokeStyle = pulse.color
          ctx.lineWidth = 0.9
          ctx.globalAlpha = 0.68 * (1 - p)
          ctx.beginPath()
          ctx.arc(curve.p0.x, curve.p0.y, 2 + 9 * p, 0, Math.PI * 2)
          ctx.stroke()
          ctx.fillStyle = pulse.color
          ctx.globalAlpha = 1
          ctx.beginPath()
          ctx.arc(curve.p0.x, curve.p0.y, 1.5, 0, Math.PI * 2)
          ctx.fill()
          ctx.restore()
          alive = true
          continue
        }

        if (travelElapsed < duration) {
          const t = easeInOut(Math.min(1, travelElapsed / duration))
          const tail = 0.38
          const segments = 28
          ctx.save()
          ctx.strokeStyle = pulse.color
          ctx.lineCap = "round"
          for (let index = 0; index < segments; index += 1) {
            const a = Math.max(0, t - tail * (index + 1) / segments)
            const b = Math.max(0, t - tail * index / segments)
            if (b <= a) continue
            const p1 = pointOnPath(curve, a)
            const p2 = pointOnPath(curve, b)
            const strength = 1 - index / segments
            ctx.globalAlpha = 0.07 + strength * 0.58
            ctx.lineWidth = 0.46 + strength * 0.68
            ctx.beginPath()
            ctx.moveTo(p1.x, p1.y)
            ctx.lineTo(p2.x, p2.y)
            ctx.stroke()
          }
          const head = pointOnPath(curve, t)
          ctx.globalAlpha = 0.28
          ctx.fillStyle = pulse.color
          ctx.beginPath()
          ctx.arc(head.x, head.y, 5.5, 0, Math.PI * 2)
          ctx.fill()
          ctx.globalAlpha = 1
          ctx.beginPath()
          ctx.arc(head.x, head.y, 1.35, 0, Math.PI * 2)
          ctx.fill()
          ctx.restore()
          alive = true
          continue
        }

        const fade = (travelElapsed - duration) / FADE_MS
        if (fade < 1) {
          ctx.save()
          ctx.strokeStyle = pulse.color
          ctx.lineCap = "round"
          ctx.globalAlpha = 0.27 * (1 - fade)
          ctx.lineWidth = 0.62
          ctx.beginPath()
          for (let index = 0; index <= 56; index += 1) {
            const point = pointOnPath(curve, index / 56)
            if (index === 0) ctx.moveTo(point.x, point.y)
            else ctx.lineTo(point.x, point.y)
          }
          ctx.stroke()
          ctx.globalAlpha = 0.58 * (1 - fade)
          ctx.beginPath()
          ctx.arc(curve.p3.x, curve.p3.y, 3 + 18 * fade, 0, Math.PI * 2)
          ctx.stroke()
          ctx.restore()
          alive = true
          continue
        }

        if (!doneRef.current.has(pulse.id)) {
          doneRef.current.add(pulse.id)
          onDoneRef.current?.(pulse.id)
        }
        startedRef.current.delete(pulse.id)
        curvesRef.current.delete(pulse.id)
      }
      if (alive) raf = requestAnimationFrame(draw)
      else ctx.clearRect(0, 0, W, H)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [pulses])

  if (!pulses.length) return null
  return <canvas ref={canvasRef} className={styles.layer} aria-hidden />
}
