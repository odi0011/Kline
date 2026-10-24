/* 钱包堆叠：悬停展开、点击置前。
   卡片材质 = 账户色相衍生的水彩背景（gradient-engine paintRecipe 静态绘制）；
   内容 = 账户名 + 权益动画数字 + 未实现盈亏（跟随全局涨跌色）+ 权益迷你曲线
   （hover 联动上方两个动画数字）。 */
import { useEffect, useRef, useState } from "react"
import { motion } from "motion/react"
import { AnimatedCounter } from "@/components/arc/animated-counter/animated-counter"
import EquityCurve from "./equity-curve"
import { accountHue, accountRecipe } from "@/lib/account-theme"
import { paintRecipe } from "@/lib/gradient-engine.js"

const spring = { type: "spring", stiffness: 420, damping: 36, mass: 0.9 }
const CARD_H = 180
const STACK_GAP = 12
// 与 styles.css 的 .pattern-zone/.wallet-zone 高度(192)同步：
// 收起时最前一张卡底边落在 188（= 顶部留白2 + 形态卡186，同形态卡底线）
const STACK_ZONE_H = 192
const STACK_BOTTOM_LINE = 188

/* 账户色相衍生的水彩背景（动态）：逐帧驱动 paintRecipe 的 startT 时间轴。
   无 WebGL（引擎退化为 CPU 逐像素渲染）时只画静态一帧，避免逐帧卡顿。 */
function AccountBackdrop({ name }) {
  const ref = useRef(null)
  useEffect(() => {
    const c = ref.current
    if (!c) return
    const hue = accountHue(name)
    const recipe = accountRecipe(name)
    const glOk = !!document.createElement("canvas").getContext("webgl")
    let raf = 0
    let alive = true
    const t0 = performance.now()
    const paint = (elapsed) => {
      const w = c.clientWidth, h = c.clientHeight
      if (!w || !h) return
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5)
      const W = Math.round(w * dpr), H = Math.round(h * dpr)
      if (c.width !== W) c.width = W
      if (c.height !== H) c.height = H
      try {
        paintRecipe(c, { ...recipe, startT: elapsed })
      } catch { /* 保留渐变兜底 */ }
    }
    if (!glOk) { paint(0); return () => { alive = false } }
    const frame = () => {
      if (!alive) return
      paint((performance.now() - t0) * (24 / 50))
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)
    return () => { alive = false; cancelAnimationFrame(raf) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name])
  return <canvas ref={ref} className="wcard-bg" aria-hidden />
}

function rgba(hex, a) {
  const h = hex.replace("#", "")
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`
}

function WalletCard({ account, front, series, chartColors, onPick }) {
  const { name, icon, balance, unrealized } = account
  const hue = accountHue(name)
  const [hoverS, setHoverS] = useState(null)
  const pnl = hoverS ? hoverS.upnl : unrealized
  const up = (pnl ?? 0) > 0
  const down = (pnl ?? 0) < 0
  const pnlColor = up ? chartColors.up : down ? chartColors.down : "rgba(16,24,40,0.55)"
  const dispBal = hoverS ? hoverS.bal : balance

  return (
    <div className={"wcard" + (front ? " front" : "")} onClick={() => onPick(name)}>
      <AccountBackdrop name={name} />
      <div className="wcard-body">
        <div className="wcard-top">
          <span className="wcard-avatar"><img src={icon} alt="" /></span>
          <span className="wcard-issuer">{name}</span>
        </div>
        <div className="wcard-mid">
          <span className="wcard-price-label">账户权益 (USDT)</span>
          <div className="wcard-binline">
            {balance == null
              ? <span className="wcard-na">获取失败</span>
              : <span className="wcard-bal"><AnimatedCounter value={dispBal ?? 0} decimals={2} /></span>}
            {pnl != null && (
              <span className="wcard-pnl" style={{ color: pnlColor }}>
                {up ? "+" : down ? "-" : ""}
                <AnimatedCounter value={Math.abs(pnl)} decimals={2} />
              </span>
            )}
          </div>
        </div>
      </div>
      <EquityCurve samples={series || []} hue={hue} height={62}
        onHover={(s) => setHoverS(s)} />
    </div>
  )
}

export function WalletStack({ accounts = [], seriesMap = {}, chartColors, onFrontChange, className = "" }) {
  const [hover, setHover] = useState(false)
  const [front, setFront] = useState(null)   // 置前的账户名（渲染序最后 = 折叠态最前）
  const rootRef = useRef(null)
  const n = accounts.length

  useEffect(() => {
    if (front && !accounts.some((a) => a.name === front)) setFront(null)
  }, [accounts, front])

  // 点击置前：把该账户排到渲染序最后（折叠态显示在最前）
  const ordered = (() => {
    if (!front) return accounts
    const f = accounts.find((a) => a.name === front)
    if (!f) return accounts
    return [...accounts.filter((a) => a.name !== front), f]
  })()

  const pickFront = (name) => {
    setFront(name)
    onFrontChange && onFrontChange(name)
  }

  const expanded = hover

  return (
    <div
      ref={rootRef}
      className={"wstack" + (className ? " " + className : "")}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      aria-label="账户"
    >
      <motion.div
        className="wstack-cards"
        animate={{ height: expanded ? n * (CARD_H + 12) + 24 : STACK_ZONE_H }}
        transition={spring}
      >
        {ordered.map((a, i) => {
          // 收起位由 React 的 style.top 管理（motion 可能在挂载/动画结束时清掉自己写的内联
          // transform/height，若静态位置依赖 animate 值会回落错位）；motion 只动相对位移 y。
          // 背面卡间距压缩到前卡顶边与区域顶边之间的余量里（账户多间距自动变细），
          // 保证收起时整叠不超出整行高度，且最前一张卡底边始终对齐形态卡底线。
          const frontTop = STACK_BOTTOM_LINE - CARD_H
          const back = n - 1 - i
          const strip = n > 1 ? Math.min(STACK_GAP, frontTop / (n - 1)) : STACK_GAP
          const collapsedY = Math.max(0, frontTop - back * strip)
          const expandedY = i * (CARD_H + 12)
          const y = expanded ? expandedY - collapsedY : 0
          return (
            <motion.div
              key={a.name}
              className="wstack-slot"
              style={{ position: "absolute", left: 0, right: 0, top: collapsedY, zIndex: i + 1 }}
              animate={{ y }}
              transition={spring}
            >
              <WalletCard account={a} front={i === n - 1} series={seriesMap[a.name]}
                chartColors={chartColors} onPick={pickFront} />
            </motion.div>
          )
        })}
      </motion.div>
    </div>
  )
}
