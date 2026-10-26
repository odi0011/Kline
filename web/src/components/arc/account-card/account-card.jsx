/* 账户卡（单账户）：头像 / 名称 / 权益 / 未实现盈亏 / 可用余额 + 当前持仓
   + 权益迷你曲线，全部信息集成在第一行 188px 区域内。
   材质与形态卡同一套浅色设计语言：账户色相派生淡雅渐变 + 发丝边框。 */
import { useMemo, useState } from "react"
import { Settings2, Wallet } from "lucide-react"
import { AnimatedCounter } from "@/components/arc/animated-counter/animated-counter"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import CoinAvatar from "@/components/ui/coin-avatar"
import { accountHue } from "@/lib/account-theme"
import EquityCurve from "./equity-curve"
import styles from "./account-card.module.css"

function PositionRow({ p, chartColors }) {
  const long = p.amt > 0
  const pnl = Number(p.unrealized) || 0
  return (
    <div className={styles.posRow}>
      <CoinAvatar symbol={p.symbol} size={16} />
      <span className={styles.posSym}>{p.symbol.replace(/usdt$/i, "").toUpperCase()}</span>
      <span className={styles.posSide} data-side={long ? "long" : "short"}>{long ? "多" : "空"}</span>
      <span className={styles.posAmt}>{Math.abs(p.amt).toLocaleString()}</span>
      <span className={styles.posPnl}
        style={{ color: pnl > 0 ? chartColors.up : pnl < 0 ? chartColors.down : "rgba(16,24,40,0.45)" }}>
        {(pnl > 0 ? "+" : "") + pnl.toFixed(2)}
      </span>
    </div>
  )
}

export default function AccountCard({ account, series, chartColors, onOpenSettings }) {
  const [hoverS, setHoverS] = useState(null)
  const hue = useMemo(() => (account ? accountHue(account.name) : 45), [account && account.name])

  if (!account) {
    return (
      <div className={styles.empty} style={{ "--acct-hue": hue }}>
        <Wallet size={22} className={styles.emptyIcon} aria-hidden />
        <div className={styles.emptyTitle}>未接入账户</div>
        <div className={styles.emptyTxt}>在设置里填入币安合约 API Key 后，这里展示权益与持仓</div>
        <button type="button" className={styles.emptyBtn} onClick={onOpenSettings}>前往设置</button>
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
            : <CoinAvatar symbol={account.name} size={30} />}
        </span>
        <div className={styles.ident}>
          <span className={styles.name} title={account.name}>{account.name}</span>
          <span className={styles.sub}>USDT 本位合约</span>
        </div>
        <span className={styles.posCount} title={`当前持仓 ${positions.length} 个`}>{positions.length} 仓</span>
        <button type="button" className={styles.gear} onClick={onOpenSettings} aria-label="账户设置">
          <Settings2 size={14} />
        </button>
      </header>

      <div className={styles.equity}>
        <div className={styles.eqTop}>
          <span className={styles.eqLabel}>账户权益 (USDT)</span>
          <span className={styles.eqAvail}>
            可用 {account.available == null ? "--" : Number(account.available).toLocaleString(undefined, { maximumFractionDigits: 2 })}
          </span>
        </div>
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
      </div>

      <div className={styles.posArea}>
        <ScrollArea orientation="vertical" fade={18} className={styles.posScroll}>
          <div className={styles.posList}>
            {positions.map((p) => <PositionRow key={p.symbol} p={p} chartColors={chartColors} />)}
            {positions.length === 0 && (
              <div className={styles.posEmpty}>{account.error ? `数据获取失败：${account.error}` : "当前没有持仓"}</div>
            )}
          </div>
        </ScrollArea>
      </div>

      <div className={styles.curve}>
        <EquityCurve samples={series || []} hue={hue} height={22} onHover={setHoverS} />
      </div>
    </section>
  )
}
