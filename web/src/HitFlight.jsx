/* 命中飞线动画：K线卡片 → 形态卡片。
   时间线：
     0.           源点出现一个小波点（呼吸放大）
     1. ~ 260ms   波点拉长成一段曲线（尾部拖出，像丝带被拽动）
     2. ~ 900ms   曲线沿贝塞尔轨迹飞向目标，中途可能绕一圈（随机）
     3. 到达      缩回成一个波点，弹性回弹两下
     4. 之后      通知形态卡片：背景从该落点开始填充
   整层是 pointer-events:none 的全屏 canvas，不挡交互。 */
import { useEffect, useRef } from "react"

const COLORS = {
  bullish: { core: "#0d9488", glow: "rgba(13,148,136,0.55)", tail: "#5eead4" },
  bearish: { core: "#e11d48", glow: "rgba(225,29,72,0.55)", tail: "#fda4af" },
  neutral: { core: "#64748b", glow: "rgba(100,116,139,0.5)", tail: "#cbd5e1" },
}

const DOT_PHASE = 260       // 波点拉成曲线
const FLY_PHASE = 720       // 飞行
const LAND_PHASE = 520      // 落点弹性
const TOTAL = DOT_PHASE + FLY_PHASE + LAND_PHASE

const easeOut = (t) => 1 - Math.pow(1 - t, 3)
const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)
const easeOutBack = (t) => {
  const c1 = 1.70158, c3 = c1 + 1
  return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2)
}

/** 三次贝塞尔取点 */
function bezier(p0, p1, p2, p3, t) {
  const mt = 1 - t
  return {
    x: mt * mt * mt * p0.x + 3 * mt * mt * t * p1.x + 3 * mt * t * t * p2.x + t * t * t * p3.x,
    y: mt * mt * mt * p0.y + 3 * mt * mt * t * p1.y + 3 * mt * t * t * p2.y + t * t * t * p3.y,
  }
}

/**
 * @param flight {{from:{x,y}, to:{x,y}, direction:string, key:number}}
 * @param onLand  到达目标时的回调（坐标 + 方向），用于启动卡片背景填充
 * @param onDone  整段动画结束
 */
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

    const c = COLORS[flight.direction] || COLORS.neutral
    const { from, to } = flight
    const dx = to.x - from.x
    const dy = to.y - from.y
    const dist = Math.hypot(dx, dy)

    // 轨迹控制点：主弧向上拱起；有几率额外绕一圈（在垂直方向加一个凸起）
    const loop = Math.random() < 0.45
    const bow = Math.min(190, 90 + dist * 0.18) * (Math.random() < 0.5 ? 1 : -1)
    const nx = dist ? -dy / dist : 0
    const ny = dist ? dx / dist : 1
    const c1 = { x: from.x + dx * 0.26 + nx * bow, y: from.y + dy * 0.26 + ny * bow }
    const c2 = loop
      ? { x: from.x + dx * 0.72 - nx * bow * 1.5, y: from.y + dy * 0.72 - ny * bow * 1.5 }
      : { x: from.x + dx * 0.74 + nx * bow * 0.5, y: from.y + dy * 0.74 + ny * bow * 0.5 }

    let raf = 0
    const t0 = performance.now()
    let landed = false

    const draw = (now) => {
      const el = now - t0
      ctx.clearRect(0, 0, W, H)

      if (el < DOT_PHASE) {
        // 1) 波点呼吸
        const t = easeOut(el / DOT_PHASE)
        const r = 3 + 5 * t
        ctx.save()
        ctx.shadowColor = c.glow
        ctx.shadowBlur = 18 + 14 * t
        ctx.fillStyle = c.core
        ctx.beginPath()
        ctx.arc(from.x, from.y, r, 0, Math.PI * 2)
        ctx.fill()
        ctx.restore()
      } else if (el < DOT_PHASE + FLY_PHASE) {
        // 2+3) 拉成曲线并飞行：头部在前，尾部按进度滞后
        const t = (el - DOT_PHASE) / FLY_PHASE
        const head = easeInOut(t)
        const tail = easeInOut(Math.max(0, t - 0.28))
        const SEG = 26
        const pts = []
        for (let i = 0; i <= SEG; i++) {
          const u = tail + (head - tail) * (i / SEG)
          pts.push(bezier(from, c1, c2, to, Math.min(1, Math.max(0, u))))
        }
        // 曲线：由粗到细、由深到浅（头部实、尾部淡）
        for (let i = 0; i < pts.length - 1; i++) {
          const k = i / (pts.length - 1)
          ctx.beginPath()
          ctx.moveTo(pts[i].x, pts[i].y)
          ctx.lineTo(pts[i + 1].x, pts[i + 1].y)
          ctx.strokeStyle = k > 0.72 ? c.core : c.tail
          ctx.globalAlpha = 0.15 + 0.85 * k
          ctx.lineWidth = 1.2 + 4.2 * k
          ctx.lineCap = "round"
          ctx.shadowColor = c.glow
          ctx.shadowBlur = 10 + 10 * k
          ctx.stroke()
        }
        // 头部光点
        const hp = pts[pts.length - 1]
        ctx.globalAlpha = 1
        ctx.fillStyle = "#fff"
        ctx.beginPath()
        ctx.arc(hp.x, hp.y, 2.6, 0, Math.PI * 2)
        ctx.fill()
        ctx.globalAlpha = 1
        ctx.shadowBlur = 0
      } else if (el < TOTAL) {
        // 进入落点阶段：只触发一次背景填充（不依赖 t 恰好到 1，避免跳帧漏掉）
        if (!landed) {
          landed = true
          onLand && onLand(to, flight.direction, flight.pattern)
        }
        // 4) 落点：弹性回弹两下
        const t = (el - DOT_PHASE - FLY_PHASE) / LAND_PHASE
        const pop = easeOutBack(Math.min(1, t * 1.6))
        const fade = 1 - Math.max(0, (t - 0.55) / 0.45)
        const r = Math.max(0, (2 + 9 * pop) * fade)
        ctx.save()
        ctx.shadowColor = c.glow
        ctx.shadowBlur = 22 * fade
        ctx.globalAlpha = fade
        ctx.fillStyle = c.core
        ctx.beginPath()
        ctx.arc(to.x, to.y, r, 0, Math.PI * 2)
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
