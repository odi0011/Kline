/* 模拟仓：每个监控币一个持续自检的纸上交易，用来验证策略而不是真下单。
   每 3 分钟按方向性涨跌幅自检：跌 0.15% 判亏、涨 0.20% 判赢、
   中间地带默认继续持有，只有信号强烈反向才反手。
   （旧的"15 分钟到期平仓"已废弃；expires_ts 现在是 4 小时保险丝。）

   卡片布局（自上而下）：
     ┌ 10 根K线（带止损/止盈/强平/入场四条参考线）　右上角胜率徽标 ┐
     │ 币种图标 + 方向 + 刷新钮                                      │
     │ 入场 / 保证金 / 数量 / 强平 / 杠杆                            │
     │ 止盈价 · 目标盈　止损价 · 止损亏                              │
     │ 实时浮盈（大字）+ 剩余时间 + 理由                              │
     └                                                              ┘

   卡片只是展示，所有计算在后端 decision/paper.py——前端不参与任何
   交易判断，避免出现"两套算法"。 */
import { memo } from "react"
import { RefreshCw } from "lucide-react"
import CoinAvatar from "@/components/ui/coin-avatar"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import styles from "./paper-trade.module.css"

const fmt = (v, digits = 2) => {
  const n = Number(v)
  if (!Number.isFinite(n)) return "--"
  const a = Math.abs(n)
  if (a >= 10000) return n.toLocaleString(undefined, { maximumFractionDigits: 0 })
  if (a >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 1 })
  if (a >= 1) return n.toFixed(digits)
  if (a >= 0.01) return n.toFixed(4)
  return n.toPrecision(3)
}

/** 10 根K线的小图：只画实体和影线，不引图表库——这里只要形状，不要交互。
    四条参考线分别是 止盈 / 入场 / 止损 / 强平。 */
function MiniCandles({ candles, pos }) {
  const list = Array.isArray(candles) ? candles : []
  if (list.length < 2) return <div className={styles.chartEmpty} />
  const W = 168
  const H = 62
  const pad = 2

  // 纵轴**只按K线的最高最低**算，不把价位线算进去。
  // 之前把强平价也纳入范围：BTC 多 20x 的强平在入场下方 4.7%，
  // 而 10 根K线的波动只有 0.2%，结果K线被压成一条细线（截图里那个样子）。
  const highs = [], lows = []
  for (const c of list) {
    const h = Number(c?.h), l = Number(c?.l)
    if (Number.isFinite(h)) highs.push(h)
    if (Number.isFinite(l)) lows.push(l)
  }
  if (!highs.length || !lows.length) return <div className={styles.chartEmpty} />
  let lo = Math.min(...lows)
  let hi = Math.max(...highs)
  // 留一点上下留白，同时保证范围不为 0
  const rawSpan = hi - lo
  const breathe = rawSpan > 0 ? rawSpan * 0.18 : Math.max(1e-9, hi * 0.001)
  lo -= breathe
  hi += breathe
  const span = hi - lo || 1
  const y = (v) => pad + (H - pad * 2) * (1 - (Number(v) - lo) / span)
  const step = W / list.length
  const bw = Math.max(2.5, step * 0.58)

  const lines = [
    { key: "tp", color: "#16a34a", label: "盈" },
    { key: "entry", color: "#1c2333", label: "入" },
    { key: "sl", color: "#dc2626", label: "损" },
    { key: "liq", color: "#7c3aed", label: "强" },
  ].filter((l) => Number.isFinite(Number(pos?.[l.key])))

  return (
    <svg className={styles.chart} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
      {lines.map((l) => {
        const v = Number(pos[l.key])
        // 价位在可视区外时贴边画一条短标记，而不是把整个纵轴撑开
        const above = v > hi
        const below = v < lo
        const yy = above ? pad + 3 : below ? H - pad - 3 : y(v)
        if (above || below) {
          const ty = above ? yy + 6.5 : yy - 1.5
          return (
            <g key={l.key}>
              <line x1={0} x2={W} y1={yy} y2={yy} stroke={l.color}
                strokeWidth="0.8" strokeDasharray="2 2" opacity="0.45" />
              <text x={W - 2} y={ty} fill={l.color} fontSize="5" textAnchor="end" opacity="0.8">
                {l.label}{above ? "↑" : "↓"}
              </text>
            </g>
          )
        }
        return (
          <g key={l.key}>
            <line x1={0} x2={W} y1={yy} y2={yy} stroke={l.color}
              strokeWidth={l.key === "entry" ? 1 : 0.8} strokeDasharray={l.key === "entry" ? "" : "3 2"}
              opacity={l.key === "entry" ? 0.5 : 0.75} />
            <text x={W - 2} y={yy - 1.5} fill={l.color} fontSize="5" textAnchor="end" opacity="0.9">
              {l.label}
            </text>
          </g>
        )
      })}
      {list.map((c, i) => {
        const cx = step * i + step / 2
        const up = Number(c.c) >= Number(c.o)
        const color = up ? "#16a34a" : "#dc2626"
        const yO = y(c.o)
        const yC = y(c.c)
        const top = Math.min(yO, yC)
        const hBody = Math.max(1, Math.abs(yC - yO))
        return (
          <g key={c.t || i}>
            <line x1={cx} x2={cx} y1={y(c.h)} y2={y(c.l)} stroke={color} strokeWidth="0.7" opacity="0.7" />
            <rect x={cx - bw / 2} y={top} width={bw} height={hBody} fill={color} opacity="0.85" />
          </g>
        )
      })}
    </svg>
  )
}

// 3 分钟自检的判定阈值，与后端 decision/paper.py 的 CHECK_LOSS_RATIO /
// CHECK_WIN_RATIO 保持同一口径（小数比率）。只用于 tooltip 文案。
const CHECK_LOSS_DISPLAY = 0.15
const CHECK_WIN_DISPLAY = 0.20

const PaperCard = memo(function PaperCard({ pos, onRefresh, busy, price, total }) {
  if (!pos) return null
  const long = pos.direction === "long"
  // 实时价：订单簿中间价每 200ms 一推，比 5 秒一轮的 quant 快照新得多。
  // 拿不到时退回快照里的 mark，再退入场价。
  const mark = Number.isFinite(Number(price)) && Number(price) > 0
    ? Number(price)
    : (Number(pos.mark) || Number(pos.entry))
  // 浮盈用实时价在本地重算——不然数字要等 5 秒才跳一次
  const pnl = (mark - Number(pos.entry)) * Number(pos.qty) * (long ? 1 : -1)
  const margin = Number(pos.margin) || 0
  const pnlPct = margin ? (pnl / margin) * 100 : 0
  const tone = pnl > 0 ? "up" : pnl < 0 ? "down" : "flat"
  // 距下一次 3 分钟自检还有多久（仓位不再按固定到期时间平仓，
  // expires_ts 已改成 4 小时保险丝；这里显示的是"下次检查"的倒计时）
  const nextCheckIn = (() => {
    const interval = 3 * 60 * 1000
    const anchor = Number(pos.last_check_ts) || Number(pos.entry_ts) || Date.now()
    return Math.max(0, anchor + interval - Date.now())
  })()
  const checkMin = Math.floor(nextCheckIn / 60000)
  const checkSec = Math.floor((nextCheckIn % 60000) / 1000)
  // 上一次自检的结论：hold/win/loss/flip + 当时的方向性涨跌幅
  const lastVerdict = String(pos.last_verdict || "")
  const lastMove = Number.isFinite(Number(pos.last_move_pct)) ? Number(pos.last_move_pct) : null
  const VERDICT_LABEL = { hold: "持有", win: "判赢", loss: "判亏", flip: "反手" }
  // 目标盈 / 止损亏：按名义价值算的绝对值
  const target = pos.notional * (Number(pos.tp_pct) || 0) / 100
  const risk = pos.notional * (Number(pos.sl_pct) || 0) / 100
  // 两个胜率是不同口径，必须分开显示：
  //   winPct   = 这一仓的**估算**胜率（按 SL/TP 距离 + 信号分算的前瞻概率）
  //   totalPct = 这个币**累计**已平仓的**实际**胜率（后验，来自 paper_trades）
  // 摆在一起才能看出"信号说 65%、实际只赢 40%"这种偏差。
  const winPct = Number.isFinite(Number(pos.winrate)) ? Number(pos.winrate) : null
  // 分档与 K 线形态卡片的「胜XX%」完全一致（>=50 hi / >=10 mid / 其余 low）
  const tierOf = (pct) => (pct == null ? "" : pct >= 50 ? "hi" : pct >= 10 ? "mid" : "low")
  const winTier = tierOf(winPct)
  const hist = total?.[pos.symbol] || null
  const totalPct = hist && Number(hist.closed) > 0 && Number.isFinite(Number(hist.winrate))
    ? Math.round(Number(hist.winrate))
    : null
  const totalTier = tierOf(totalPct)

  return (
    <article className={styles.card} data-tone={long ? "long" : "short"}>
      <div className={styles.chartWrap}>
        <MiniCandles candles={pos.candles} pos={pos} />
        {/* 左上角：刷新（平掉当前仓、按最新数据重开）。
            原来这里是个"模拟"字标——卡片标题已经写了模拟仓，重复标注没意义，
            这个位置留给操作。 */}
        <button type="button" className={styles.refresh} aria-label="按当前数据刷新仓位"
          title="平掉当前模拟仓，立刻按最新数据重开" disabled={busy}
          onClick={() => onRefresh?.(pos.symbol)}>
          <RefreshCw size={11} strokeWidth={2.4} data-spin={busy || undefined} />
        </button>
        {/* 右上角两枚胶囊徽标，样式对齐 K 线形态卡片：
            左边「估XX%」= 这一仓的估算胜率（前瞻）
            右边「胜XX%」= 这个币累计已平仓的实际胜率（后验） */}
        <span className={styles.badges}>
          <span className={`${styles.winrate} ${styles[winTier] || ""}`}
            title={`这一仓的估算胜率 ${pos.winrate ?? "--"}%（按 SL/TP 距离、信号分、形态历史推算，是前瞻概率）`}>
            估{winPct == null ? "--" : winPct}%
          </span>
          <span className={`${styles.winrate} ${styles.actual} ${styles[totalTier] || ""}`}
            data-muted={totalPct == null || undefined}
            title={totalPct == null
              ? "这个币还没有平过仓，暂时没有实际胜率"
              : `这个币累计 ${hist.closed} 笔已平仓：赢 ${hist.wins}、输 ${hist.losses} = ${totalPct}%`}>
            胜{totalPct == null ? "--" : totalPct}%
          </span>
        </span>
      </div>

      <header className={styles.head}>
        <CoinAvatar symbol={pos.symbol} size={14} />
        <b className={styles.sym}>{pos.symbol.replace(/usdt$/i, "").toUpperCase()}</b>
        <span className={styles.dir} data-tone={long ? "long" : "short"}>
          {long ? "多" : "空"}
        </span>
        <span className={styles.lev}>{pos.leverage}x</span>
      </header>

      <div className={styles.grid}>
        <span><i>入</i>{fmt(pos.entry)}</span>
        <span><i>量</i>{fmt(pos.qty, 4)}</span>
        <span><i>保证金</i>{fmt(pos.margin)}</span>
        <span><i>强平</i>{fmt(pos.liq)}</span>
      </div>

      <div className={styles.levels}>
        <span data-k="tp" title={`止盈 ${fmt(pos.tp)}（+${pos.tp_pct ?? "--"}%）`}>
          盈 {fmt(pos.tp)} <em>+{fmt(target)}</em>
        </span>
        <span data-k="sl" title={`止损 ${fmt(pos.sl)}（−${pos.sl_pct ?? "--"}%）${pos.trailing ? " · 已上移" : ""}`}>
          损 {fmt(pos.sl)} <em>−{fmt(risk)}</em>{pos.trailing ? " ·" : ""}
        </span>
      </div>

      <footer className={styles.foot}>
        <span className={styles.pnl} data-tone={tone}>
          {pnl >= 0 ? "+" : ""}{fmt(pnl)}
          <em>{pnlPct >= 0 ? "+" : ""}{fmt(pnlPct, 1)}%</em>
        </span>
        {/* 每 3 分钟自检一次：倒计时到下次检查，并显示上次判了什么 */}
        <span className={styles.left}
          title={`每 3 分钟自检：跌 ${Math.abs(CHECK_LOSS_DISPLAY)}% 判亏、涨 ${CHECK_WIN_DISPLAY}% 判赢、中间地带看信号决定持有或反手`}>
          {lastVerdict && lastVerdict !== "hold"
            ? `${VERDICT_LABEL[lastVerdict] || lastVerdict}${lastMove == null ? "" : ` ${lastMove > 0 ? "+" : ""}${lastMove.toFixed(2)}%`}`
            : `检 ${checkMin}:${String(checkSec).padStart(2, "0")}`}
        </span>
        {pos.reason && <span className={styles.reason} title={pos.reason}>{pos.reason}</span>}
      </footer>
    </article>
  )
})

export default function PaperTrade({ rows, depths, onRefresh, busySymbol, bySymbol, checks }) {
  const list = Array.isArray(rows) ? rows : []
  const stats = checks || {}
  const hasStats = Number(stats.checks) > 0
  // 订单簿中间价：bids/asks 都是真实挂单，取最优买一卖一的中点
  const midOf = (sym) => {
    const d = depths?.[sym]
    if (!d) return null
    const bid = Number(d.bids?.[0]?.[0])
    const ask = Number(d.asks?.[0]?.[0])
    if (Number.isFinite(bid) && Number.isFinite(ask) && bid > 0 && ask > 0) return (bid + ask) / 2
    if (Number.isFinite(bid) && bid > 0) return bid
    if (Number.isFinite(ask) && ask > 0) return ask
    return null
  }
  return (
    <section className={styles.root} aria-label="模拟仓">
      {/* 3 分钟自检的汇总：判赢/判亏/反手次数 + 累计盈亏回撤。
          放在卡片上方一行，因为"这套自检判定准不准"是全局问题。 */}
      {hasStats && (
        <div className={styles.checkBar} role="group" aria-label="自检统计">
          <span title="自检判赢 / 判亏次数（按 3 分钟窗口的价格涨跌幅判定）">
            判赢 {stats.winChecks} · 判亏 {stats.lossChecks}
          </span>
          {Number(stats.flipChecks) > 0 && (
            <span title="中间地带因信号强烈反向而反手的次数">反手 {stats.flipChecks}</span>
          )}
          <span title="按自检结论算的胜率（判赢 ÷（判赢+判亏））">
            {stats.checkWinrate == null ? "--" : `${stats.checkWinrate}%`}
          </span>
          <span title="累计盈亏从峰值的最大回吐（金额）"
            style={{ color: Number(stats.maxDrawdown) < 0 ? "var(--down)" : undefined }}>
            回撤 {Number(stats.maxDrawdown || 0).toFixed(2)}
          </span>
          <span title="累计已实现盈亏（所有平仓之和）">
            累计 {Number(stats.netPnl || 0) >= 0 ? "+" : ""}{Number(stats.netPnl || 0).toFixed(2)}
          </span>
        </div>
      )}
      {/* 横向滚动沿用统一的 ScrollArea（右上角玻璃箭头翻页 + 边缘渐隐），
          和 K线形态图鉴、大户持仓是同一套，不自己写 overflow。 */}
      <ScrollArea orientation="horizontal" scrollbars="auto" scrollButtons fade={14}
        label="模拟仓" className={styles.scroll} viewportClassName={styles.cards}>
        {list.length === 0
          ? <p className={styles.empty}>等待评估数据…</p>
          : list.map((pos) => (
            <PaperCard key={pos.symbol} pos={pos} price={midOf(pos.symbol)}
              total={bySymbol} onRefresh={onRefresh} busy={busySymbol === pos.symbol} />
          ))}
      </ScrollArea>
    </section>
  )
}
