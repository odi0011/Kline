/* 账户卡（单账户）：头像 / 名称 / 权益 / 未实现盈亏 / 权益曲线 / 持仓标签，
   全部信息集成在第一行 188px 区域内。
   材质与形态卡同一套浅色设计语言：账户色相派生淡雅渐变 + 发丝边框。 */
import { useMemo, useState } from "react"
import { AnimatedCounter } from "@/components/arc/animated-counter/animated-counter"
import CoinAvatar from "@/components/ui/coin-avatar"
import { accountHue } from "@/lib/account-theme"
import EquityCurve from "./equity-curve"
import styles from "./account-card.module.css"

/* 持仓标签：图标 + 币种 + 数量 + 盈亏，横向排列自动换行 */
function PositionTag({ p, chartColors }) {
  const long = p.amt > 0
  const pnl = Number(p.unrealized) || 0
  const color = pnl > 0 ? chartColors.up : pnl < 0 ? chartColors.down : "rgba(16,24,40,0.4)"
  const base = p.symbol.replace(/usdt$/i, "").toUpperCase()
  return (
    <span className={styles.tag} data-side={long ? "long" : "short"}
      title={`${base} ${long ? "做多" : "做空"} ${Math.abs(p.amt)} · 浮动 ${pnl.toFixed(2)}`}>
      <CoinAvatar symbol={p.symbol} size={13} />
      <b className={styles.tagSym}>{base}</b>
      <span className={styles.tagAmt}>{Math.abs(p.amt).toLocaleString()}</span>
      <span className={styles.tagPnl} style={{ color }}>{(pnl > 0 ? "+" : "") + pnl.toFixed(2)}</span>
    </span>
  )
}

export default function AccountCard({ account, series, chartColors }) {
  const [hoverS, setHoverS] = useState(null)
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
        <div className={styles.posList}>
          {positions.map((p) => <PositionTag key={p.symbol} p={p} chartColors={chartColors} />)}
          {positions.length === 0 && (
            <span className={styles.posEmpty}>{account.error ? "数据获取失败" : "无持仓"}</span>
          )}
        </div>
      </div>
    </section>
  )
}
