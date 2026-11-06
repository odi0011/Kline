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
import MarketOverview from "./market-overview"
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

/* 最近平仓：已平掉的仓位，占着原来空着的那条右侧空间。
   和持仓卡同一套视觉语言（图标 + 币种 + 状态标签 + 盈亏），
   区别只是状态写「已平」而不是「多/空」——用户原话就是这个意思：
   "多空那里就写已平不就可以"。

   为什么不做成可滚动列表：这块高度只有 ~96px（要和左侧概览齐平），
   塞 4 行刚好；再多就要抢概览的位置。完整历史有专门的仓位历史面板。 */
function ClosedList({ trades = [], chartColors }) {
  const recent = (Array.isArray(trades) ? trades : []).slice(0, 4)
  if (!recent.length) {
    return (
      <div className={styles.closedArea}>
        <span className={styles.closedTitle}>最近平仓</span>
        <span className={styles.closedEmpty}>暂无</span>
      </div>
    )
  }
  return (
    <div className={styles.closedArea}>
      <span className={styles.closedTitle}
        title={`最近 ${recent.length} 笔已平仓（完整历史见「仓位历史」）`}>
        最近平仓
      </span>
      <div className={styles.closedList}>
        {recent.map((t) => {
          const pnl = Number(t.pnl) || 0
          const base = String(t.symbol || "").replace(/usdt$/i, "").toUpperCase()
          return (
            <div key={t.id ?? `${t.symbol}-${t.ts}`} className={styles.closedRow}
              title={`${base} ${t.side === "BUY" ? "多" : "空"} ${t.qty} · `
                + `开 ${t.entry} → 平 ${t.exit} · ${t.reason || ""} · `
                + `持有 ${Math.round(Number(t.holdMin) || 0)} 分钟`}>
              <CoinAvatar symbol={t.symbol} size={11} />
              <b className={styles.closedSym}>{base}</b>
              <span className={styles.closedTag}>已平</span>
              <span className={styles.closedPnl}
                style={{ color: pnl > 0 ? chartColors.up : pnl < 0 ? chartColors.down : "rgba(16,24,40,0.45)" }}>
                {(pnl > 0 ? "+" : "") + pnl.toFixed(2)}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default function AccountCard({ account, series, chartColors, accent = "#f0b90b", overview, trades }) {
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
  const showHistory = positions.length === 0 && !account.error
  const scrollable = positions.length > 0 && (edges.left || edges.right)

  const nudge = (dir) => {
    const vp = viewportRef.current
    if (!vp) return
    vp.scrollBy({ left: dir * Math.max(110, vp.clientWidth * 0.8), behavior: "smooth" })
  }

  return (
    <section className={`${styles.card}${showHistory ? ` ${styles.historyCard}` : ""}`}
      data-has-position={positions.length > 0 || undefined}
      aria-label="账户">
      <header className={styles.head}>
        <span className={styles.avatar}>
          {account.icon
            ? <img src={account.icon} alt="" />
            : <CoinAvatar symbol={account.name} size={28} />}
        </span>
        <div className={styles.ident}>
          <span className={styles.name} title={account.name}>{account.name}</span>
          {/* 标「实盘」是为了和模拟仓卡片区分：账户里的权益/持仓是真实委托，
              模拟仓只是纸上记账，两者不能互相解释。 */}
          <span className={styles.sub}>USDT 本位合约 · 实盘</span>
        </div>
        <div className={styles.curve}>
          {/* 曲线高度跟着布局走：有仓位时整卡要压回"无仓位"的高度
              （首行是等高栅格，卡高一变整行就参差），所以这里统一 28。
              只有账户出错、什么都没有时才给 46 占位。 */}
          <EquityCurve samples={series || []} hue={hue}
            height={showHistory || positions.length > 0 ? 28 : 46} onHover={setHoverS} />
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

      <div className={`${styles.posArea}${positions.length > 0 ? ` ${styles.split}` : ""}`}>
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
        ) : account.error ? (
          <span className={styles.posEmpty} role="alert">数据获取失败</span>
        ) : null}
        {/* 下半区左右分栏：左边市场概览（情绪 + 三个时间窗口），
            右边最近平仓。卡片撑满 wallet 宽度后右边多出来的空间
            原本是空的（线上实测 125px 全白），正好放已平的仓位。 */}
        <div className={styles.lowerRow}>
          <div className={styles.overviewArea}>
            <MarketOverview overview={overview} positions={positions} />
          </div>
          <ClosedList trades={trades} chartColors={chartColors} />
        </div>
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
