/* 账户卡（单账户）：头像 / 名称 / 权益 / 未实现盈亏 / 权益曲线 / 持仓卡，
   全部信息集成在第一行 188px 区域内。
   持仓用竖排小卡横向滚动（与K线形态图鉴同一种浏览方式），每张卡展示
   币种、方向、杠杆、浮动盈亏与收益率、数量、开仓价。 */
import { useMemo, useRef, useState } from "react"
import { ChevronLeft, ChevronRight } from "lucide-react"
import { AnimatedCounter } from "@/components/arc/animated-counter/animated-counter"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import CoinAvatar from "@/components/ui/coin-avatar"
import { accountHue } from "@/lib/account-theme"
import EquityCurve from "./equity-curve"
import styles from "./account-card.module.css"

const fmtPrice = (v) => {
  const n = Number(v) || 0
  if (n >= 1000) return n.toFixed(1)
  if (n >= 1) return n.toFixed(3)
  return n.toPrecision(4)
}

/* 持仓小卡：竖排，横向滚动 */
function PositionCard({ p, chartColors }) {
  const long = p.amt > 0
  const pnl = Number(p.unrealized) || 0
  const pct = Number(p.unrealized_pct) || 0
  const color = pnl > 0 ? chartColors.up : pnl < 0 ? chartColors.down : "rgba(16,24,40,0.45)"
  const base = p.symbol.replace(/usdt$/i, "").toUpperCase()
  const tip = [
    `${base} ${long ? "做多" : "做空"}`,
    `数量 ${Math.abs(p.amt)}`,
    `开仓 ${fmtPrice(p.entry)}`,
    p.mark ? `标记 ${fmtPrice(p.mark)}` : null,
    p.leverage ? `杠杆 ${p.leverage}x` : null,
    p.liquidation ? `强平 ${fmtPrice(p.liquidation)}` : null,
    p.margin ? `保证金 ${p.margin}` : null,
    `浮动 ${pnl.toFixed(2)} (${pct.toFixed(2)}%)`,
  ].filter(Boolean).join(" · ")

  return (
    <div className={styles.pcard} data-side={long ? "long" : "short"} title={tip}>
      <div className={styles.pcardTop}>
        <CoinAvatar symbol={p.symbol} size={14} />
        <b className={styles.pcardSym}>{base}</b>
        {p.leverage ? <span className={styles.pcardLev}>{p.leverage}x</span> : null}
      </div>
      <div className={styles.pcardPnl} style={{ color }}>
        {(pnl > 0 ? "+" : "") + pnl.toFixed(2)}
      </div>
      <div className={styles.pcardPct} style={{ color }}>
        {(pct > 0 ? "+" : "") + pct.toFixed(2)}%
      </div>
      <div className={styles.pcardRow}>
        <span className={styles.pcardKey}>量</span>{Math.abs(p.amt).toLocaleString()}
      </div>
      <div className={styles.pcardRow}>
        <span className={styles.pcardKey}>开</span>{fmtPrice(p.entry)}
      </div>
    </div>
  )
}

export default function AccountCard({ account, series, chartColors }) {
  const [hoverS, setHoverS] = useState(null)
  const [edges, setEdges] = useState({ left: false, right: false })
  const viewportRef = useRef(null)
  const hue = useMemo(() => (account ? accountHue(account.name) : 45), [account && account.name])

  if (!account) {
    return (
      <div className={styles.empty} style={{ "--acct-hue": hue }}>
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

  // 左右箭头：按一屏滚动（持仓少于一屏时按钮自动禁用）
  const scrollByDir = (dir) => {
    const vp = viewportRef.current
    if (!vp) return
    vp.scrollBy({ left: dir * Math.max(120, vp.clientWidth * 0.8), behavior: "smooth" })
  }

  return (
    <section className={styles.card} style={{ "--acct-hue": hue }} aria-label="账户">
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
          <div className={styles.posWrap}>
            <ScrollArea viewportRef={viewportRef} orientation="horizontal" fade={14} scrollbars="auto"
              className={styles.posScroll} viewportClassName={styles.posViewport} label="当前持仓"
              onEdgeChange={setEdges}>
              <div className={styles.posList}>
                {positions.map((p) => (
                  <PositionCard key={p.symbol} p={p} chartColors={chartColors} />
                ))}
              </div>
            </ScrollArea>
            {(edges.left || edges.right) && (
              <div className={styles.posNav}>
                <button type="button" aria-label="左移持仓" disabled={!edges.left}
                  onClick={() => scrollByDir(-1)}><ChevronLeft size={12} /></button>
                <button type="button" aria-label="右移持仓" disabled={!edges.right}
                  onClick={() => scrollByDir(1)}><ChevronRight size={12} /></button>
              </div>
            )}
          </div>
        ) : (
          <span className={styles.posEmpty}>{account.error ? "数据获取失败" : "无持仓"}</span>
        )}
      </div>
    </section>
  )
}
