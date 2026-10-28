/* 高频指标飞线：左侧指标条 → 右侧数据接收节点。
   独立 canvas 与 K 线命中飞线分离，使用细线、长拖尾、多曲率和随机回环。 */
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
const lineSegment = (p0, p3) => ({ p0, c1: p0, c2: p3, p3 })
const cubicSegment = (p0, c1, c2, p3) => ({ p0, c1, c2, p3 })

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

const circleSegments = (center, radius, startAngle, sweep, steps = 22) => {
  const points = []
  for (let index = 0; index <= steps; index += 1) {
    const angle = startAngle + sweep * index / steps
    points.push({
      x: center.x + Math.cos(angle) * radius,
      y: center.y + Math.sin(angle) * radius,
    })
  }
  return points.slice(1).map((point, index) => lineSegment(points[index], point))
}

const roundedPolyline = (points, radius = 18) => {
  if (points.length < 2) return []
  const segments = []
  let cursor = points[0]
  for (let index = 1; index < points.length - 1; index += 1) {
    const corner = points[index]
    const previous = points[index - 1]
    const next = points[index + 1]
    const inLength = Math.hypot(corner.x - previous.x, corner.y - previous.y) || 1
    const outLength = Math.hypot(next.x - corner.x, next.y - corner.y) || 1
    const inUnit = { x: (corner.x - previous.x) / inLength, y: (corner.y - previous.y) / inLength }
    const outUnit = { x: (next.x - corner.x) / outLength, y: (next.y - corner.y) / outLength }
    const inRadius = Math.min(radius, inLength * 0.32)
    const outRadius = Math.min(radius, outLength * 0.32)
    const start = { x: corner.x - inUnit.x * inRadius, y: corner.y - inUnit.y * inRadius }
    const end = { x: corner.x + outUnit.x * outRadius, y: corner.y + outUnit.y * outRadius }
    if (Math.hypot(start.x - cursor.x, start.y - cursor.y) > 0.1) {
      segments.push(lineSegment(cursor, start))
    }
    segments.push(cubicSegment(
      start,
      { x: start.x + (corner.x - start.x) * 0.67, y: start.y + (corner.y - start.y) * 0.67 },
      { x: end.x + (corner.x - end.x) * 0.67, y: end.y + (corner.y - end.y) * 0.67 },
      end,
    ))
    cursor = end
  }
  const last = points[points.length - 1]
  if (Math.hypot(last.x - cursor.x, last.y - cursor.y) > 0.1) {
    segments.push(lineSegment(cursor, last))
  }
  return segments
}

function makeCurve(from, to, pulse) {
  const dx = to.x - from.x
  const dy = to.y - from.y
  const distance = Math.hypot(dx, dy) || 1
  const ux = dx / distance
  const uy = dy / distance
  const nx = -dy / distance
  const ny = dx / distance
  const seed = hash(pulse.id)
  const sign = seed % 2 ? 1 : -1
  const bend = Math.min(210, 72 + distance * 0.19) * sign
  const variant = seed % 6

  if (variant === 2 || variant === 5) {
    // 出发后绕圈，再拐向接收节点。
    const radius = variant === 2
      ? Math.min(70, 32 + distance * 0.055)
      : Math.min(42, 21 + distance * 0.025)
    const center = {
      x: from.x + ux * radius * (variant === 2 ? 0.22 : 0.16) + nx * radius,
      y: from.y + uy * radius * (variant === 2 ? 0.22 : 0.16) + ny * radius,
    }
    const startAngle = Math.atan2(from.y - center.y, from.x - center.x)
    const sweep = sign * Math.PI * (variant === 2 ? 2.08 : 2.65)
    const loop = circleSegments(center, radius, startAngle, sweep)
    const loopEnd = loop[loop.length - 1].p3
    return {
      p0: from,
      p3: to,
      parts: [
        { segments: loop, weight: variant === 2 ? 0.4 : 0.29 },
        {
          segments: [cubicSegment(
            loopEnd,
            { x: loopEnd.x + ux * distance * 0.2 + nx * bend * 0.2, y: loopEnd.y + uy * distance * 0.2 + ny * bend * 0.2 },
            { x: to.x - ux * distance * 0.12 + nx * bend * 0.08, y: to.y - uy * distance * 0.12 + ny * bend * 0.08 },
            to,
          )],
          weight: variant === 2 ? 0.6 : 0.71,
        },
      ],
    }
  }

  if (variant === 3) {
    // 故意拐错的折线路线，所有转角都做圆滑过渡。
    const gateX = from.x + dx * (0.3 + ((seed >>> 8) % 16) / 100)
    const dropY = to.y + dy * (((seed >>> 14) % 18) - 4) / 100
    const points = [
      from,
      { x: gateX, y: from.y },
      { x: gateX, y: dropY },
      { x: to.x, y: dropY },
      to,
    ]
    return {
      p0: from,
      p3: to,
      parts: [{ segments: roundedPolyline(points, 20), weight: 1 }],
    }
  }

  if (variant === 4) {
    // 先飞过节点，再掉头回来。
    const overshoot = {
      x: to.x + ux * (34 + seed % 36) + nx * (((seed >>> 9) % 17) - 8),
      y: to.y + uy * (34 + seed % 36) + ny * (((seed >>> 9) % 17) - 8),
    }
    return {
      p0: from,
      p3: to,
      parts: [
        {
          segments: [cubicSegment(
            from,
            { x: from.x + dx * 0.32 + nx * bend * 0.45, y: from.y + dy * 0.32 + ny * bend * 0.45 },
            { x: overshoot.x - dx * 0.2 + nx * bend * 0.18, y: overshoot.y - dy * 0.2 + ny * bend * 0.18 },
            overshoot,
          )],
          weight: 0.76,
        },
        {
          segments: [cubicSegment(
            overshoot,
            { x: overshoot.x - ux * 10 + nx * 8, y: overshoot.y - uy * 10 + ny * 8 },
            { x: to.x + ux * 10 - nx * 8, y: to.y + uy * 10 - ny * 8 },
            to,
          )],
          weight: 0.24,
        },
      ],
    }
  }

  return {
    p0: from,
    p3: to,
    parts: [{
      segments: [cubicSegment(
        from,
        { x: from.x + dx * (0.18 + ((seed >>> 3) % 15) / 100) + nx * bend, y: from.y + dy * (0.18 + ((seed >>> 7) % 15) / 100) + ny * bend },
        {
          x: from.x + dx * (0.68 + ((seed >>> 11) % 12) / 100)
            + nx * bend * (variant === 1 ? -0.85 : 0.28),
          y: from.y + dy * (0.68 + ((seed >>> 11) % 12) / 100)
            + ny * bend * (variant === 1 ? -0.85 : 0.28),
        },
        to,
      )],
      weight: 1,
    }],
  }
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
    // doneRef 只用于区分同一批脉冲是否播过；脉冲被移除后其 id 永久失效，
    // 定期清掉不在当前批次里的 id，避免长开页面集合无限增长。
    const alive = new Set(pulses.map((pulse) => pulse.id))
    for (const id of doneRef.current) {
      if (!alive.has(id)) doneRef.current.delete(id)
    }
    const canvas = canvasRef.current
    if (!canvas) return
    const targetEl = document.querySelector("[data-metric-intake]")
    const targetRect = targetEl?.getBoundingClientRect()
    const target = targetRect
      ? { x: targetRect.left + targetRect.width / 2, y: targetRect.top + targetRect.height / 2 }
      : null
    if (!target) return

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
