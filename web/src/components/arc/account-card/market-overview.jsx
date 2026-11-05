/* 账户卡下半区：市场情绪 + 分窗口战绩。
   替代原来的权益热力图——热力图占了一整块高度，但信息量只有"哪天赚了多少"，
   而这个位置真正该回答的是"现在市场什么状态、我这套策略在最近这些窗口里
   打得怎么样"。

   三件事：
     1. 情绪   当前监控币种的方向分布（偏多/偏空/分歧）+ 平均共鸣分
     2. 战绩   30 分钟 / 今日 的胜率、盈亏、最大回撤、夏普
     3. 无仓位时显示这套数据；有仓位时仓位卡优先（仓位是更紧急的信息）

   所有数字来自服务端实时计算（每 5 秒刷新），前端只做格式化。 */
import { useMemo } from "react"
import styles from "./market-overview.module.css"

const num = (v) => {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

const fmtPnl = (v) => {
  const n = num(v)
  if (n == null) return "--"
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}`
}

/** 情绪色：偏多用涨色、偏空用跌色、分歧用中性灰 */
function toneOf(label) {
  if (label === "偏多") return "up"
  if (label === "偏空") return "down"
  return "flat"
}

function Stat({ label, value, hint, tone, title }) {
  return (
    <div className={styles.stat} title={title}>
      <span className={styles.statLabel}>{label}</span>
      <b className={styles.statValue} data-tone={tone}>{value}</b>
      {hint ? <span className={styles.statHint}>{hint}</span> : null}
    </div>
  )
}

function WindowBlock({ title, stats }) {
  const s = stats || {}
  const trades = num(s.trades) ?? 0
  const wins = num(s.wins) ?? 0
  const losses = num(s.losses) ?? 0
  const draws = num(s.draws) ?? 0
  const empty = trades === 0
  const winrate = num(s.winrate)
  const pnl = num(s.pnl)
  const dd = num(s.maxDrawdown)
  const sharpe = num(s.sharpe)
  // 有成交但全平局：胜率算不出来（分母是 0），但说"无交易"是错的
  const onlyDraws = !empty && wins + losses === 0
  const real = num(s.realTrades) ?? 0
  const paper = num(s.paperTrades) ?? 0
  // 构成说明：实盘成交很稀疏（线上实测 30 分钟常常 0 笔），
  // 数字主要来自模拟仓。不标出来用户会以为是实盘战绩。
  const mix = empty ? "" : [
    real ? `实盘${real}` : "", paper ? `模拟${paper}` : "",
  ].filter(Boolean).join(" · ")
  return (
    <div className={styles.block}>
      <span className={styles.blockTitle}>
        {title}
        {mix ? <em className={styles.mix}>{mix}</em> : null}
      </span>
      {empty ? (
        <span className={styles.blockEmpty}>无交易</span>
      ) : (
        // 指标横向排（胜/盈亏/回撤/夏普），一律单行，不再 2x2 堆叠——
        // 之前 2 列堆叠在窄卡里会超出高度被裁掉。
        <div className={styles.blockStats}>
          <Stat label="胜" value={winrate == null ? "--" : `${Math.round(winrate)}%`}
            hint={onlyDraws ? `${draws}平` : `${wins}胜${losses}负`}
            tone={winrate == null ? "flat" : winrate >= 50 ? "up" : "down"}
            title={`${wins} 胜 / ${losses} 负${draws ? ` / ${draws} 平` : ""}；平局不计入胜率分母`} />
          <Stat label="盈亏" value={fmtPnl(pnl)}
            tone={pnl == null ? "flat" : pnl > 0 ? "up" : pnl < 0 ? "down" : "flat"} />
          <Stat label="回撤" value={dd == null ? "--" : dd.toFixed(2)}
            tone={dd != null && dd < 0 ? "down" : "flat"}
            title="窗口内累计盈亏从峰值的最大回吐（与盈亏同单位）" />
          <Stat label="夏普" value={sharpe == null ? "--" : sharpe.toFixed(2)}
            tone={sharpe == null ? "flat" : sharpe > 0 ? "up" : "down"}
            title="每笔收益率的均值 ÷ 标准差，不年化——3 分钟周期的年化是噪声" />
        </div>
      )}
    </div>
  )
}

export default function MarketOverview({ overview, positions = [] }) {
  const ov = overview || {}
  const sentiment = ov.sentiment || {}
  const tone = toneOf(sentiment.label)
  const breadth = num(sentiment.breadth)
  const avgScore = num(sentiment.avgScore)

  // 情绪条：多头占比从左往右填充
  const fillPct = breadth == null ? 50 : Math.max(0, Math.min(100, breadth))
  const barColor = useMemo(() => {
    if (tone === "up") return "var(--up)"
    if (tone === "down") return "var(--down)"
    return "rgba(16,24,40,0.28)"
  }, [tone])

  const hasPosition = Array.isArray(positions) && positions.length > 0
  if (!ov.sentiment && !ov.m30 && !ov.today) {
    return <div className={styles.empty}>等待行情评估…</div>
  }

  return (
    <div className={styles.wrap} data-has-position={hasPosition || undefined}>
      <div className={styles.sentiment}>
        <div className={styles.sentRow}>
          <span className={styles.sentLabel}>情绪</span>
          <b className={styles.sentValue} data-tone={tone}>{sentiment.label || "--"}</b>
          {breadth != null && (
            <span className={styles.sentDetail}>
              {sentiment.bullish ?? 0} 多 / {sentiment.bearish ?? 0} 空
            </span>
          )}
          {avgScore != null && (
            <span className={styles.sentScore}>均分 {avgScore}</span>
          )}
        </div>
        <div className={styles.sentBar} aria-hidden
          title="偏多币种占比：≥65% 偏多、≤35% 偏空、中间为分歧">
          <i style={{ width: `${fillPct}%`, background: barColor }} />
        </div>
      </div>

      <div className={styles.windows}>
        <WindowBlock title="30 分钟" stats={ov.m30} />
        <WindowBlock title="今日" stats={ov.today} />
        {/* 短窗口没成交时前两格是空的，累计列保证这一块永远有真实数字可看。
            它也是"策略长期到底行不行"的唯一参照。 */}
        <WindowBlock title="累计" stats={ov.all} />
      </div>
    </div>
  )
}
