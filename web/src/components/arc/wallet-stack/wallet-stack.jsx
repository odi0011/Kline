/* 钱包堆叠（按 uiarc WalletStack 交互文档自实现，官方源码为 Pro 专属）：
   堆叠的"账户"卡（每张 = 一个币安测试盘账户），悬停向上展开，
   账户权益数字滚动，点击卡片展开明细。 */
import { useEffect, useRef, useState } from "react"
import { motion } from "motion/react"
import { AnimatedCounter } from "@/components/arc/animated-counter/animated-counter"

const spring = { type: "spring", stiffness: 420, damping: 36, mass: 0.9 }
const CARD_H = 196
const STACK_GAP = 12

function WalletCard({ account, front, expanded }) {
  const { name, icon, balance, unrealized, available } = account
  const up = (unrealized ?? 0) > 0
  const down = (unrealized ?? 0) < 0
  return (
    <div
      className={"wcard" + (front ? " front" : "")}
      style={{ background: "linear-gradient(135deg, rgba(240,185,11,0.3), rgba(255,255,255,0.88) 55%, rgba(255,255,255,0.82))" }}
    >
      <div className="wcard-top">
        <span className="wcard-avatar"><img src={icon} alt="" /></span>
        <span className="wcard-issuer">{name}</span>
        <span className="wcard-live"><i className="wcard-dot" />测试盘</span>
      </div>
      <div className="wcard-mid">
        <div className="wcard-price">
          <span className="wcard-price-label">账户权益 (USDT)</span>
          {balance == null
            ? <span className="wcard-na">获取失败</span>
            : <AnimatedCounter value={balance} decimals={2} />}
        </div>
        <span className="wcard-chip">◈</span>
      </div>
      <div className="wcard-bottom">
        <span>未实现盈亏 <b className={up ? "up" : down ? "down" : ""}>{unrealized ?? "--"}</b></span>
        <span>可用 <b>{available ?? "--"}</b></span>
      </div>
      {expanded && (
        <div className="wcard-recent">
          <div className="wcard-recent-title">账户状态</div>
          <div className="wcard-recent-item" style={{ color: "rgba(16,24,40,.6)" }}>
            币安合约测试盘 · 只读监控 · 数据 30 秒刷新
          </div>
        </div>
      )}
      <span className="wcard-num">···· {name.slice(-2)}</span>
    </div>
  )
}

export function WalletStack({ accounts = [], label = "账户", onSelectedChange, className = "" }) {
  const [hover, setHover] = useState(false)
  const [selected, setSelected] = useState(null)
  const rootRef = useRef(null)
  const n = accounts.length

  useEffect(() => {
    if (!selected) return
    const onDoc = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) setSelected(null) }
    const onKey = (e) => { if (e.key === "Escape") setSelected(null) }
    document.addEventListener("pointerdown", onDoc)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("pointerdown", onDoc)
      document.removeEventListener("keydown", onKey)
    }
  }, [selected])

  const expanded = hover || selected != null
  const selIdx = accounts.findIndex((a) => a.name === selected)

  return (
    <div
      ref={rootRef}
      className={"wstack" + (className ? " " + className : "")}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => { setHover(false); if (selected) { setSelected(null); onSelectedChange?.(null) } }}
      aria-label={label}
    >
      <motion.div
        className="wstack-cards"
        animate={{ height: expanded ? n * (CARD_H + 12) + 24 : STACK_GAP * (n - 1) + CARD_H + 18 }}
        transition={spring}
      >
        {accounts.map((a, i) => {
          const isSel = selected === a.name
          const y = expanded
            ? i * (CARD_H + 12) + (isSel && i > selIdx ? 96 : 0)
            : i * STACK_GAP
          return (
            <motion.div
              key={a.name}
              className="wstack-slot"
              style={{ position: "absolute", left: 0, right: 0, zIndex: i + 1 }}
              animate={{ y }}
              transition={spring}
              onClick={() => {
                const next = selected === a.name ? null : a.name
                setSelected(next)
                onSelectedChange?.(next ? { name: a.name } : null)
              }}
            >
              <WalletCard account={a} front={i === n - 1} expanded={isSel} />
            </motion.div>
          )
        })}
      </motion.div>
    </div>
  )
}
