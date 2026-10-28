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

// Deform the existing path, not its seed/phase: departure stays fixed and t=1
// always lands on the receiver's CURRENT Three-camera projection.
export const pointFollowingReceiver = (path, t, receiver) => {
  const point = pointOnPath(path, t)
  const u = clamp01(t), weight = u * u * (3 - 2 * u)
  return { x: point.x + (receiver.x - path.p3.x) * weight, y: point.y + (receiver.y - path.p3.y) * weight }
}

export const pruneFlightMaps = (pulses, started, curves, done) => {
  const alive = new Set(pulses.slice(-48).map((p) => p.id))
  for (const collection of [started, curves, done]) {
    for (const id of collection.keys()) if (!alive.has(id)) collection.delete(id)
  }
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
    const loop = circleSegments(center, Math.hypot(from.x - center.x, from.y - center.y), startAngle, sweep)
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

export default function MetricFlights({ pulses = [], onDone }) {
  const canvasRef = useRef(null)
  const startedRef = useRef(new Map()), curvesRef = useRef(new Map()), doneRef = useRef(new Set())
  const onDoneRef = useRef(onDone), pulsesRef = useRef(pulses), syncRef = useRef(null)
  onDoneRef.current = onDone; pulsesRef.current = pulses.slice(-48)
  useEffect(() => { syncRef.current?.() }, [pulses])

  useEffect(() => {
    const canvas = canvasRef.current, ctx = canvas?.getContext("2d")
    if (!canvas || !ctx) return
    let W = 1, H = 1, raf = 0, clock = 0, lastFrame = 0, disposed = false
    let targetEl = null, intersecting = true, targetObserver = null, motionObserver = null
    const media = window.matchMedia("(prefers-reduced-motion: reduce)")
    const started = startedRef.current, curves = curvesRef.current, done = doneRef.current
    const canMove = () => targetEl?.isConnected && targetEl.dataset.ready === "true"
      && targetEl.dataset.motion === "running" && !document.hidden && !media.matches && intersecting
    const receiver = () => {
      const rect = targetEl?.getBoundingClientRect()
      return rect ? { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 } : null
    }
    const finish = (id) => {
      started.delete(id); curves.delete(id)
      if (!done.has(id)) { done.add(id); onDoneRef.current?.(id) }
    }
    const bindTarget = () => {
      const next = document.querySelector("[data-metric-intake]")
      if (next === targetEl) return
      targetObserver?.disconnect(); motionObserver?.disconnect()
      targetEl = next; intersecting = true
      if (!next) return
      targetObserver = new IntersectionObserver(([entry]) => { intersecting = entry.isIntersecting; wake() }, { threshold: [0, 0.01] })
      targetObserver.observe(next)
      motionObserver = new MutationObserver(wake)
      motionObserver.observe(next, { attributes: true, attributeFilter: ["data-ready", "data-motion"] })
    }
    const draw = (now, still = false) => {
      if (!still) raf = 0
      if (disposed || (!still && !canMove())) { lastFrame = 0; return }
      if (!still) {
        clock += lastFrame ? Math.min(80, Math.max(0, now - lastFrame)) : 0
        lastFrame = now
      }
      const target = receiver()
      if (!target) return
      ctx.clearRect(0, 0, W, H)
      let alive = false
      for (const pulse of pulsesRef.current) {
        if (done.has(pulse.id)) continue
        if (!started.has(pulse.id)) {
          if (still) continue
          if (!Number.isFinite(pulse.source?.x) || !Number.isFinite(pulse.source?.y)) { finish(pulse.id); continue }
          started.set(pulse.id, clock + Math.max(0, pulse.delay || 0))
          curves.set(pulse.id, makeCurve(pulse.source, target, pulse))
        }
        const curve = curves.get(pulse.id), elapsed = clock - started.get(pulse.id)
        if (!curve) continue
        if (elapsed < 0) { alive = true; continue }
        const spawn = Math.max(0, pulse.spawn ?? 170), duration = Math.max(1, pulse.duration || 1400)
        const travelElapsed = elapsed - spawn, color = pulse.color || "#24caba"
        if (travelElapsed < 0) {
          const p = Math.min(1, elapsed / Math.max(1, spawn))
          ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = 0.9; ctx.globalAlpha = 0.68 * (1 - p)
          ctx.beginPath(); ctx.arc(curve.p0.x, curve.p0.y, 2 + 9 * p, 0, Math.PI * 2); ctx.stroke()
          ctx.fillStyle = color; ctx.globalAlpha = 1
          ctx.beginPath(); ctx.arc(curve.p0.x, curve.p0.y, 1.5, 0, Math.PI * 2); ctx.fill(); ctx.restore()
          alive = true; continue
        }
        if (travelElapsed < duration) {
          const t = easeInOut(Math.min(1, travelElapsed / duration)), tail = 0.38, segments = 28
          ctx.save(); ctx.strokeStyle = color; ctx.lineCap = "round"
          for (let index = 0; index < segments; index++) {
            const a = Math.max(0, t - tail * (index + 1) / segments), b = Math.max(0, t - tail * index / segments)
            if (b <= a) continue
            const p1 = pointFollowingReceiver(curve, a, target), p2 = pointFollowingReceiver(curve, b, target), strength = 1 - index / segments
            ctx.globalAlpha = 0.07 + strength * 0.58; ctx.lineWidth = 0.46 + strength * 0.68
            ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y); ctx.stroke()
          }
          const head = pointFollowingReceiver(curve, t, target)
          ctx.globalAlpha = 0.28; ctx.fillStyle = color
          ctx.beginPath(); ctx.arc(head.x, head.y, 5.5, 0, Math.PI * 2); ctx.fill()
          ctx.globalAlpha = 1; ctx.beginPath(); ctx.arc(head.x, head.y, 1.35, 0, Math.PI * 2); ctx.fill(); ctx.restore()
          alive = true; continue
        }
        if (!curve.arrived && !still) {
          curve.arrived = true
          window.dispatchEvent(new CustomEvent("quant-metric-arrival", { detail: { id: pulse.id } }))
        }
        const fade = (travelElapsed - duration) / FADE_MS
        if (fade < 1) {
          ctx.save(); ctx.strokeStyle = color; ctx.lineCap = "round"; ctx.globalAlpha = 0.27 * (1 - fade); ctx.lineWidth = 0.62
          ctx.beginPath()
          for (let index = 0; index <= 56; index++) {
            const point = pointFollowingReceiver(curve, index / 56, target)
            if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y)
          }
          ctx.stroke(); ctx.globalAlpha = 0.58 * (1 - fade)
          ctx.beginPath(); ctx.arc(target.x, target.y, 3 + 18 * fade, 0, Math.PI * 2); ctx.stroke(); ctx.restore()
          alive = true; continue
        }
        finish(pulse.id)
      }
      if (alive && !still) raf = requestAnimationFrame(draw)
      else if (!alive) { lastFrame = 0; ctx.clearRect(0, 0, W, H) }
    }
    function wake() {
      if (disposed) return
      canvas.style.visibility = !document.hidden && intersecting && targetEl?.isConnected ? "visible" : "hidden"
      if (!canMove()) {
        cancelAnimationFrame(raf); raf = 0; lastFrame = 0
        if (targetEl?.isConnected) draw(performance.now(), true)
        else ctx.clearRect(0, 0, W, H)
        return
      }
      if (!raf && pulsesRef.current.some((p) => !done.has(p.id))) raf = requestAnimationFrame(draw)
    }
    const sync = () => {
      pruneFlightMaps(pulsesRef.current, started, curves, done)
      bindTarget()
      if (document.hidden || media.matches || !intersecting || targetEl?.dataset.motion === "frozen") {
        // Preserve already flying pulses, but do not queue a replay of samples received while suspended.
        for (const pulse of pulsesRef.current) if (!started.has(pulse.id) && !done.has(pulse.id)) finish(pulse.id)
      }
      if (!pulsesRef.current.length) {
        cancelAnimationFrame(raf); raf = 0; lastFrame = 0; ctx.clearRect(0, 0, W, H)
      } else wake()
    }
    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5)
      W = window.innerWidth; H = window.innerHeight
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr)
      canvas.style.width = `${W}px`; canvas.style.height = `${H}px`; ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      wake()
    }
    // Only listen for receiver replacement; do not recalculate curves on every React render.
    const mountObserver = new MutationObserver(() => { if (!targetEl?.isConnected) { bindTarget(); wake() } })
    mountObserver.observe(document.body, { childList: true, subtree: true })
    document.addEventListener("visibilitychange", wake); media.addEventListener("change", wake)
    window.addEventListener("resize", resize)
    syncRef.current = sync; resize(); sync()
    return () => {
      disposed = true; syncRef.current = null
      cancelAnimationFrame(raf); targetObserver?.disconnect(); motionObserver?.disconnect(); mountObserver.disconnect()
      document.removeEventListener("visibilitychange", wake); media.removeEventListener("change", wake); window.removeEventListener("resize", resize)
      started.clear(); curves.clear(); done.clear(); ctx.clearRect(0, 0, W, H)
      canvas.width = canvas.height = 1
    }
  }, [])

  return <canvas ref={canvasRef} className={styles.layer} aria-hidden />
}
