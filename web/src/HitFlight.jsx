/* 命中/流转飞线：一个全屏 canvas，支持两种轨迹。
   mode = "arc"     : K线卡片 → 形态卡片（贝塞尔弧线，46% 概率绕一个弧）
   mode = "wobble"  : 形态卡片徽标 → 决策流水最新一行（不规则摆动曲线）

   时间线：波点呼吸 → 拉成曲线飞行 → 落点弹性 → 通知上层（展开背景）。
   整层 pointer-events:none，不挡交互。 */
import { useEffect, useRef } from "react"

const DOT_PHASE = 240       // 波点拉成曲线
const LAND_PHASE = 460      // 落点弹性

const easeOut = (t) => 1 - Math.pow(1 - t, 3)
const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)
const easeOutBack = (t) => {
  const c1 = 1.70158, c3 = c1 + 1
  return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2)
}

const DEFAULT_COLORS = {
  bullish: { core: "#0d9488", glow: "rgba(13,148,136,0.55)", tail: "#5eead4" },
  bearish: { core: "#e11d48", glow: "rgba(225,29,72,0.55)", tail: "#fda4af" },
  neutral: { core: "#64748b", glow: "rgba(100,116,139,0.5)", tail: "#cbd5e1" },
}

/** 摆动曲线：直线插值 + 垂直方向 sin 摆动。k 越大绕得越明显。 */
function wobblePos(from, to, k, amp) {
  const dx = to.x - from.x, dy = to.y - from.y
  const len = Math.hypot(dx, dy) || 1
  const nx = -dy / len, ny = dx / len
  return (t) => ({
    x: from.x + dx * t + nx * Math.sin(t * Math.PI * k) * amp,
    y: from.y + dy * t + ny * Math.sin(t * Math.PI * k) * amp,
  })
}

/** 三次贝塞尔取点（弧线模式） */
function bezierPos(p0, p1, p2, p3) {
  return (t) => {
    const mt = 1 - t
    return {
      x: mt * mt * mt * p0.x + 3 * mt * mt * t * p1.x + 3 * mt * t * t * p2.x + t * t * t * p3.x,
      y: mt * mt * mt * p0.y + 3 * mt * mt * t * p1.y + 3 * mt * t * t * p2.y + t * t * t * p3.y,
    }
  }
}

export default function HitFlight({ flight, onLand, onDone }) {
  const canvasRef = useRef(null)

  useEffect(() => {
    if (!flight) return
    const canvas = canvasRef.current
    if (!canvas) return
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const W = window.innerWidth
    const H = window.innerHeight
    canvas.width = Math.round(W * dpr)
    canvas.height = Math.round(H * dpr)
    canvas.style.width = W + "px"
    canvas.style.height = H + "px"
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    const c = flight.colors || DEFAULT_COLORS[flight.direction] || DEFAULT_COLORS.neutral
    const { from, to } = flight
    const dist = Math.hypot(to.x - from.x, to.y - from.y)
    const fly = flight.fly ?? (flight.mode === "wobble" ? 640 : 700)
    const dotBase = flight.dot ?? 3
    const TOTAL = DOT_PHASE + fly + LAND_PHASE

    // 轨迹
    let posAt
    if (flight.mode === "wobble") {
      const k = 1 + Math.random() * 1.4          // >1.6 时曲线会绕回来一段
      const amp = Math.min(110, 22 + dist * 0.16) * (Math.random() < 0.5 ? 1 : -1)
      posAt = wobblePos(from, to, k, amp)
    } else {
      const dx = to.x - from.x, dy = to.y - from.y
      const len = dist || 1
      const nx = -dy / len, ny = dx / len
      const bow = Math.min(190, 90 + dist * 0.18) * (Math.random() < 0.5 ? 1 : -1)
      const loop = Math.random() < 0.45
      const c1 = { x: from.x + dx * 0.26 + nx * bow, y: from.y + dy * 0.26 + ny * bow }
      const c2 = loop
        ? { x: from.x + dx * 0.72 - nx * bow * 1.5, y: from.y + dy * 0.72 - ny * bow * 1.5 }
        : { x: from.x + dx * 0.74 + nx * bow * 0.5, y: from.y + dy * 0.74 + ny * bow * 0.5 }
      posAt = bezierPos(from, c1, c2, to)
    }

    let raf = 0
    const t0 = performance.now()
    let landed = false

    const draw = (now) => {
      const el = now - t0
      ctx.clearRect(0, 0, W, H)

      if (el < DOT_PHASE) {
        const t = easeOut(el / DOT_PHASE)
        ctx.save()
        ctx.shadowColor = c.glow
        ctx.shadowBlur = 16 + 12 * t
        ctx.fillStyle = c.core
        ctx.beginPath()
        ctx.arc(from.x, from.y, Math.max(0.1, dotBase + dotBase * 1.5 * t), 0, Math.PI * 2)
        ctx.fill()
        ctx.restore()
      } else if (el < DOT_PHASE + fly) {
        const t = (el - DOT_PHASE) / fly
        const head = easeInOut(t)
        const tail = easeInOut(Math.max(0, t - 0.3))
        const SEG = 30
        const pts = []
        for (let i = 0; i <= SEG; i++) {
          const u = tail + (head - tail) * (i / SEG)
          pts.push(posAt(Math.min(1, Math.max(0, u))))
        }
        for (let i = 0; i < pts.length - 1; i++) {
          const k = i / (pts.length - 1)
          ctx.beginPath()
          ctx.moveTo(pts[i].x, pts[i].y)
          ctx.lineTo(pts[i + 1].x, pts[i + 1].y)
          ctx.strokeStyle = k > 0.72 ? c.core : c.tail
          ctx.globalAlpha = 0.15 + 0.85 * k
          ctx.lineWidth = 1 + 3.6 * k
          ctx.lineCap = "round"
          ctx.shadowColor = c.glow
          ctx.shadowBlur = 9 + 9 * k
          ctx.stroke()
        }
        const hp = pts[pts.length - 1]
        ctx.globalAlpha = 1
        ctx.shadowBlur = 0
        ctx.fillStyle = "#fff"
        ctx.beginPath()
        ctx.arc(hp.x, hp.y, 2.2, 0, Math.PI * 2)
        ctx.fill()
      } else if (el < TOTAL) {
        if (!landed) {
          landed = true
          onLand && onLand(to, flight.direction, flight.pattern, flight.mode)
        }
        const t = (el - DOT_PHASE - fly) / LAND_PHASE
        const pop = easeOutBack(Math.min(1, t * 1.6))
        const fade = 1 - Math.max(0, (t - 0.5) / 0.5)
        ctx.save()
        ctx.shadowColor = c.glow
        ctx.shadowBlur = 20 * fade
        ctx.globalAlpha = fade
        ctx.fillStyle = c.core
        ctx.beginPath()
        ctx.arc(to.x, to.y, Math.max(0.1, (dotBase * 0.7 + dotBase * 2.6 * pop) * fade), 0, Math.PI * 2)
        ctx.fill()
        ctx.restore()
      } else {
        ctx.clearRect(0, 0, W, H)
        onDone && onDone()
        return
      }
      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flight && flight.key])

  if (!flight) return null
  return <canvas ref={canvasRef} className="hit-flight" aria-hidden />
}
