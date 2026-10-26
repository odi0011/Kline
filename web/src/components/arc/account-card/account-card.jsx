/* 账户卡（单账户）：头像 / 名称 / 权益 / 未实现盈亏 / 权益曲线 / 持仓卡。
   全卡使用主题色（--accent）着色；单账户不再按账户名派生色相。
   持仓卡横向滚动，左右翻页用全局统一的玻璃箭头按钮（LiquidGlass）。 */
import { useMemo, useRef, useState } from "react"
import { ChevronLeft, ChevronRight } from "lucide-react"
import { AnimatedCounter } from "@/components/arc/animated-counter/animated-counter"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import { LiquidGlass } from "@/components/ui/liquid-glass"
import CoinAvatar from "@/components/ui/coin-avatar"
import { hexHue } from "@/lib/account-theme"
import EquityCurve from "./equity-curve"
import styles from "./account-card.module.css"

const fmtPrice = (v) => {
  const n = Number(v) || 0
  if (n >= 1000) return n.toFixed(1)
  if (n >= 1) return n.toFixed(3)
  return n.toPrecision(4)
}
const pctColor = (v, chartColors) =>
  v > 0 ? chartColors.up : v < 0 ? chartColors.down : "rgba(16,24,40,0.5)"

/* 持仓卡：头部（图标+币种+方向+杠杆）—— 盈亏 —— 分隔线 —— 明细四行 */
function PositionCard({ p, chartColors }) {
  const long = p.amt > 0
  const pnl = Number(p.unrealized) || 0
  const pct = Number(p.unrealized_pct) || 0
  const color = pctColor(pnl, chartColors)
  const base = p.symbol.replace(/usdt$/i, "").toUpperCase()
  const rows = [
    ["开仓", fmtPrice(p.entry)],
    ["强平", p.liquidation ? fmtPrice(p.liquidation) : "--"],
    ["保证金", p.margin != null ? Number(p.margin).toLocaleString(undefined, { maximumFractionDigits: 2 }) : "--"],
  ]
  const amt = Math.abs(p.amt).toLocaleString(undefined, { maximumFractionDigits: 4 })
  return (
    <div className={styles.pcard}
      title={`${base} ${long ? "做多" : "做空"} ${amt} · 开仓 ${p.entry} · 标记 ${p.mark} · 名义 ${p.notional}${p.isolated ? " · 逐仓" : " · 全仓"}`}>
      <div className={styles.phead}>
        <CoinAvatar symbol={p.symbol} size={13} />
        <b className={styles.psym}>{base}</b>
        <span className={styles.pside} data-side={long ? "long" : "short"}>{long ? "多" : "空"}</span>
        {p.leverage ? <span className={styles.plev}>{p.leverage}x</span> : null}
      </div>
      <div className={styles.ppnl} style={{ color }}>
        {(pnl > 0 ? "+" : "") + pnl.toFixed(2)}
        <span className={styles.ppct}>{(pct > 0 ? "+" : "") + pct.toFixed(2)}%</span>
      </div>
      <div className={styles.psep} />
      <div className={styles.prows}>
        <div className={styles.prow}>
          <span className={styles.pkey}>数量</span>
          <span className={styles.pval}>{amt}</span>
        </div>
        {rows.map(([k, v]) => (
          <div className={styles.prow} key={k}>
            <span className={styles.pkey}>{k}</span>
            <span className={styles.pval}>{v}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function AccountCard({ account, series, chartColors, accent = "#f0b90b" }) {
  const [hoverS, setHoverS] = useState(null)
  const [edges, setEdges] = useState({ left: false, right: false })
  const viewportRef = useRef(null)
  const hue = useMemo(() => hexHue(accent), [accent])

  if (!account) {
    return (
      <div className={styles.empty}>
        <div className={styles.emptyTitle}>未接入账户</div>
        <div className={styles.emptyTxt}>在「设置 → 账户」填入币安合约 API Key</div>
      </div>
    )
  }

  const positions = Array.isArray(account.positions) ? account.positions : []
  const balance = hoverS ? hoverS.bal : account.balance
  const pnl = hoverS ? hoverS.upnl : account.unrealized
  const up = (pnl ?? 0) > 0
  const down = (pnl ?? 0) < 0
  const lost = account.balance == null
  const scrollable = edges.left || edges.right

  const nudge = (dir) => {
    const vp = viewportRef.current
    if (!vp) return
    vp.scrollBy({ left: dir * Math.max(110, vp.clientWidth * 0.8), behavior: "smooth" })
  }

  return (
    <section className={styles.card} aria-label="账户">
      <header className={styles.head}>
        <span className={styles.avatar}>
          {account.icon
            ? <img src={account.icon} alt="" />
            : <CoinAvatar symbol={account.name} size={28} />}
        </span>
        <div className={styles.ident}>
          <span className={styles.name} title={account.name}>{account.name}</span>
          <span className={styles.sub}>USDT 本位合约</span>
        </div>
        <div className={styles.curve}>
          <EquityCurve samples={series || []} hue={hue} height={46} onHover={setHoverS} />
        </div>
      </header>

      <div className={styles.eqLine}>
        <span className={styles.eqNum}>
          {lost ? "--" : <AnimatedCounter value={balance ?? 0} decimals={2} />}
        </span>
        {pnl != null && (
          <span className={styles.eqPnl}
            style={{ color: up ? chartColors.up : down ? chartColors.down : "rgba(16,24,40,0.45)" }}>
            {up ? "+" : down ? "-" : ""}
            <AnimatedCounter value={Math.abs(pnl)} decimals={2} />
          </span>
        )}
      </div>

      <div className={styles.posArea}>
        {positions.length > 0 ? (
          <ScrollArea viewportRef={viewportRef} orientation="horizontal" fade={16} scrollbars="auto"
            className={styles.posScroll} viewportClassName={styles.posViewport} label="当前持仓"
            onEdgeChange={setEdges}>
            <div className={styles.posList}>
              {positions.map((p) => (
                <PositionCard key={p.symbol} p={p} chartColors={chartColors} />
              ))}
            </div>
          </ScrollArea>
        ) : (
          <span className={styles.posEmpty}>{account.error ? "数据获取失败" : "无持仓"}</span>
        )}
        {scrollable && (
          <div className={styles.posNav}>
            <LiquidGlass radius={999} scale={20} aberration={0} frost={2} tint={0.14} className={styles.navGlass}>
              <button type="button" className={styles.navBtn} aria-label="向左滚动" data-off={!edges.left || undefined}
                onClick={() => nudge(-1)}><ChevronLeft size={12} strokeWidth={2} aria-hidden /></button>
            </LiquidGlass>
            <LiquidGlass radius={999} scale={20} aberration={0} frost={2} tint={0.14} className={styles.navGlass}>
              <button type="button" className={styles.navBtn} aria-label="向右滚动" data-off={!edges.right || undefined}
                onClick={() => nudge(1)}><ChevronRight size={12} strokeWidth={2} aria-hidden /></button>
            </LiquidGlass>
          </div>
        )}
      </div>
    </section>
  )
}
