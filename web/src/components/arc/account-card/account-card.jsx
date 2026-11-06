/* 账户卡（单账户）：头像 / 名称 / 权益 / 未实现盈亏 / 权益曲线 / 市场概览。
   全卡使用主题色（--accent）着色；单账户不再按账户名派生色相。

   **这张卡只放权益和统计**。持仓挪到了右侧独立的竖排栏
   （position-stack.jsx）：卡片只有 236px 宽，横排两张仓位必须裁字段，
   而用户明确不要裁（"仓位还给我简化了，需要你简化吗？竖着排绝对够了"）。
   卡宽也保持原样——把它拉宽去塞仓位，右侧就没有空间了。 */
import { useMemo, useState } from "react"
import { AnimatedCounter } from "@/components/arc/animated-counter/animated-counter"
import CoinAvatar from "@/components/ui/coin-avatar"
import { hexHue } from "@/lib/account-theme"
import EquityCurve from "./equity-curve"
import MarketOverview from "./market-overview"
import styles from "./account-card.module.css"

export default function AccountCard({ account, series, chartColors, accent = "#f0b90b", overview }) {
  const [hoverS, setHoverS] = useState(null)
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
              （首行是等高栅格，卡高一变整行就参差），所以这里统一 28。 */}
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

      <div className={styles.posArea}>
        {account.error ? (
          <span className={styles.posEmpty} role="alert">数据获取失败</span>
        ) : null}
        {/* 卡内只有市场概览。原来的权益热力图已去掉：它只回答
            "哪天赚了多少"，而这块空间该回答"现在市场什么状态、
            我的策略在这些窗口里打得怎么样"。 */}
        <div className={styles.overviewArea}>
          <MarketOverview overview={overview} positions={positions} />
        </div>
      </div>
    </section>
  )
}
