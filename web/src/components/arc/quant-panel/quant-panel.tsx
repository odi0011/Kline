/* 第三行：自动量化交易——算法判断流程可视化 + 实盘持仓/成交记录。
   左侧：决策流水（信号 → 风控 → 下单 → 平仓，逐步带时间与原因）
   中间：当前持仓（实时盈亏）
   右侧：最近平仓记录（盈亏/持有分钟/触发原因）+ 统计 */
import { useMemo } from "react"
import { Activity, ArrowRight, CircleDot, XCircle, CheckCircle2, Zap, AlertTriangle } from "lucide-react"
import CoinAvatar from "@/components/ui/coin-avatar"
import { AnimatedCounter } from "@/components/arc/animated-counter/animated-counter"
import styles from "./quant-panel.module.css"

const KIND_META = {
  open: { label: "开仓", Icon: CheckCircle2, cls: "open" },
  close: { label: "平仓", Icon: Zap, cls: "close" },
  skip: { label: "跳过", Icon: CircleDot, cls: "skip" },
  error: { label: "错误", Icon: AlertTriangle, cls: "error" },
  audit: { label: "审计", Icon: Activity, cls: "audit" },
  system: { label: "系统", Icon: CircleDot, cls: "system" },
}

const fmtClock = (ts) => {
  const d = new Date(ts + 8 * 3600 * 1000)
  const p = (n) => String(n).padStart(2, "0")
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`
}
const fmtPct = (n) => `${n > 0 ? "+" : ""}${Number(n).toFixed(2)}%`

/** 一段可视化流程：信号 → 风控 → 下单 */
function FlowStrip({ stats }) {
  const steps = [
    { label: "信号", v: stats.signals ?? 0 },
    { label: "决策", v: stats.decisions ?? 0 },
    { label: "下单", v: stats.orders ?? 0 },
    { label: "平仓", v: stats.closes ?? 0 },
  ]
  return (
    <div className={styles.flow}>
      {steps.map((s, i) => (
        <div key={s.label} className={styles.flowStep}>
          <span className={styles.flowLabel}>{s.label}</span>
          <span className={styles.flowVal}>{s.v}</span>
          {i < steps.length - 1 && <ArrowRight size={12} className={styles.flowArrow} aria-hidden />}
        </div>
      ))}
      <div className={styles.flowStep}>
        <span className={styles.flowLabel}>胜/负</span>
        <span className={styles.flowVal}>
          <b className={styles.win}>{stats.wins ?? 0}</b>/<b className={styles.loss}>{stats.losses ?? 0}</b>
        </span>
      </div>
    </div>
  )
}

function EventRow({ ev, chartColors }) {
  const meta = KIND_META[ev.kind] || KIND_META.system
  const { Icon } = meta
  return (
    <div className={styles.evRow} data-kind={meta.cls}>
      <Icon size={12} className={styles.evIcon} aria-hidden />
      <span className={styles.evTime}>{fmtClock(ev.ts)}</span>
      <span className={styles.evLabel}>{meta.label}</span>
      {ev.symbol && <CoinAvatar symbol={ev.symbol} size={14} />}
      <span className={styles.evText} title={ev.reason}>{ev.reason || ""}</span>
      {ev.account && <span className={styles.evAcct}>{ev.account.replace(/^ODDD/, "")}</span>}
      {typeof ev.pnl === "number" && (
        <span className={styles.evPnl} style={{ color: ev.pnl >= 0 ? chartColors.up : chartColors.down }}>
          {ev.pnl > 0 ? "+" : ""}{ev.pnl}
        </span>
      )}
    </div>
  )
}

export default function QuantPanel({ snapshot, chartColors, onToggle }) {
  const snap = snapshot || { enabled: false, stats: {}, events: [], positions: [], trades: [] }
  const stats = snap.stats || {}
  const events = useMemo(() => (snap.events || []).slice().reverse(), [snap.events])
  const positions = snap.positions || []
  const trades = (snap.trades || []).slice().reverse()
  const winRate = (stats.wins || 0) + (stats.losses || 0) > 0
    ? Math.round((stats.wins / (stats.wins + stats.losses)) * 100) : null

  return (
    <div className={styles.wrap}>
      <header className={styles.head}>
        <h3 className={styles.title}>自动量化交易</h3>
        <span className={styles.state} data-on={snap.enabled || undefined}>
          {snap.enabled ? "运行中" : "已停止"}
        </span>
        <span className={styles.meta}>形态收线确认 → 风控 → 市价开仓 → 止损/止盈 → 记录</span>
        <button type="button" className={styles.toggle} data-on={snap.enabled || undefined}
          onClick={() => onToggle && onToggle(!snap.enabled)}>
          {snap.enabled ? "停止" : "启动"}
        </button>
      </header>

      <FlowStrip stats={stats} />

      <div className={styles.grid3}>
        {/* 决策流水 */}
        <section className={styles.col}>
          <div className={styles.colHead}>
            <span className={styles.colTitle}>决策流水</span>
            <span className={styles.colHint}>每一步都记录</span>
          </div>
          <div className={styles.scroll}>
            {events.map((ev, i) => <EventRow key={`${ev.ts}-${i}`} ev={ev} chartColors={chartColors} />)}
            {events.length === 0 && <div className={styles.empty}>等待首个信号…</div>}
          </div>
        </section>

        {/* 当前持仓 */}
        <section className={styles.col}>
          <div className={styles.colHead}>
            <span className={styles.colTitle}>当前持仓</span>
            <span className={styles.colHint}>{positions.length} 仓</span>
          </div>
          <div className={styles.scroll}>
            {positions.map((p) => (
              <div key={`${p.account}-${p.symbol}`} className={styles.posRow}>
                <CoinAvatar symbol={p.symbol} size={18} />
                <span className={styles.posSym}>{p.symbol.toUpperCase()}</span>
                <span className={styles.posSide} data-side={p.side === "BUY" ? "long" : "short"}>
                  {p.side === "BUY" ? "多" : "空"}
                </span>
                <span className={styles.posQty}>{Number(p.qty).toLocaleString()}</span>
                <span className={styles.posPnl} style={{ color: p.upnl >= 0 ? chartColors.up : chartColors.down }}>
                  {p.upnl >= 0 ? "+" : "-"}
                  <AnimatedCounter value={Math.abs(p.upnl)} decimals={2} />
                  <em>{fmtPct(p.movePct)}</em>
                </span>
              </div>
            ))}
            {positions.length === 0 && <div className={styles.empty}>暂未持仓</div>}
          </div>
        </section>

        {/* 平仓记录 */}
        <section className={styles.col}>
          <div className={styles.colHead}>
            <span className={styles.colTitle}>平仓记录</span>
            {winRate != null && <span className={styles.winRate}>胜率 {winRate}%</span>}
          </div>
          <div className={styles.scroll}>
            {trades.map((t, i) => (
              <div key={`${t.ts}-${i}`} className={styles.tradeRow}>
                <span className={styles.tTime}>{fmtClock(t.ts)}</span>
                <span className={styles.posSym}>{t.symbol.replace(/usdt$/i, "").toUpperCase()}</span>
                <span className={styles.posSide} data-side={t.side === "BUY" ? "long" : "short"}>
                  {t.side === "BUY" ? "多" : "空"}
                </span>
                <span className={styles.tReason}>{t.reason}</span>
                <span className={styles.tHold}>{t.holdMin}m</span>
                <span className={styles.posPnl} style={{ color: t.pnl >= 0 ? chartColors.up : chartColors.down }}>
                  {t.pnl > 0 ? "+" : ""}{t.pnl}
                </span>
              </div>
            ))}
            {trades.length === 0 && <div className={styles.empty}>暂无平仓记录</div>}
          </div>
        </section>
      </div>
    </div>
  )
}
