/* 模拟仓：每个监控币一个 15 分钟的纸上交易，用来验证策略而不是真下单。

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

const PaperCard = memo(function PaperCard({ pos, onRefresh, busy, price }) {
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
  // 剩余时间
  const leftMs = Math.max(0, (Number(pos.expires_ts) || 0) - Date.now())
  const leftMin = Math.floor(leftMs / 60000)
  const leftSec = Math.floor((leftMs % 60000) / 1000)
  // 目标盈 / 止损亏：按名义价值算的绝对值
  const target = pos.notional * (Number(pos.tp_pct) || 0) / 100
  const risk = pos.notional * (Number(pos.sl_pct) || 0) / 100
  // 胜率徽标档位：与 K 线形态卡片的「胜XX%」同一套分档（>=50 hi / >=10 mid / 其余 low），
  // 颜色也从 --accent 派生，两边一眼能对上。
  const winPct = Number.isFinite(Number(pos.winrate)) ? Number(pos.winrate) : null
  const winTier = winPct == null ? "" : winPct >= 50 ? "hi" : winPct >= 10 ? "mid" : "low"

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
        {/* 右上角：这张仓位（该币全部模拟仓）的总预估胜率。
            样式对齐 K 线形态卡片的「胜XX%」徽标——按档位取色，
            不是自己画一块玻璃。 */}
        <span className={`${styles.winrate} ${styles[winTier] || ""}`}
          data-hi={winTier === "hi" || undefined}
          title={`这张模拟仓的估算胜率 ${pos.winrate ?? "--"}%（含 SL/TP 距离、信号分、形态历史），不是已平仓的历史胜率`}>
          胜{pos.winrate == null ? "--" : Math.round(pos.winrate)}%
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
        <span className={styles.left} title="模拟仓到期后自动平仓并重开">
          {leftMin}:{String(leftSec).padStart(2, "0")}
        </span>
        {pos.reason && <span className={styles.reason} title={pos.reason}>{pos.reason}</span>}
      </footer>
    </article>
  )
})

export default function PaperTrade({ rows, depths, onRefresh, busySymbol }) {
  const list = Array.isArray(rows) ? rows : []
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
      {/* 横向滚动沿用统一的 ScrollArea（右上角玻璃箭头翻页 + 边缘渐隐），
          和 K线形态图鉴、大户持仓是同一套，不自己写 overflow。 */}
      <ScrollArea orientation="horizontal" scrollbars="auto" scrollButtons fade={14}
        label="模拟仓" className={styles.scroll} viewportClassName={styles.cards}>
        {list.length === 0
          ? <p className={styles.empty}>等待评估数据…</p>
          : list.map((pos) => (
            <PaperCard key={pos.symbol} pos={pos} price={midOf(pos.symbol)}
              onRefresh={onRefresh} busy={busySymbol === pos.symbol} />
          ))}
      </ScrollArea>
    </section>
  )
}
