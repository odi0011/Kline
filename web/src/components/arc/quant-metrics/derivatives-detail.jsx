/* 衍生品指标详情：点列表某一行弹出的原生 <dialog>。
   沿用 article-modal 的开关/焦点管理，内容按"这个数字是什么意思"组织，
   而不是把字段名堆一遍——这些指标最终要参与策略判断。 */
import { useId, useLayoutEffect, useRef } from "react"
import { X } from "lucide-react"
import styles from "./derivatives-detail.module.css"

const num = (v) => {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

const fmtPrice = (v) => {
  const n = num(v)
  if (n == null || n <= 0) return "--"
  if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 1 })
  if (n >= 1) return n.toFixed(3)
  return n.toPrecision(4)
}
const fmtPct = (v, digits = 2) => {
  const n = num(v)
  return n == null ? "--" : `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`
}
const fmtVol = (v) => {
  const n = num(v)
  if (n == null) return "--"
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}K`
  return String(Math.round(n))
}

const fundingAside = (nextMs) => {
  const n = Number(nextMs)
  if (!Number.isFinite(n) || n <= 0) return ""
  const minutes = Math.round((n - Date.now()) / 60000)
  if (minutes < 0) return "即将结算"
  return minutes >= 60 ? `${Math.floor(minutes / 60)} 小时后结算` : `${minutes} 分钟后结算`
}

const sideOf = (ratio) => {
  const n = num(ratio)
  if (n == null) return null
  return n >= 1 ? "long" : "short"
}

/** 每个指标：数值 + 含义 + 怎么用。读的人要知道它进策略时的方向。 */
function metricRows(row, fmt) {
  const rows = []
  const accL = num(row.accountLongPct)
  const topL = num(row.topPositionLongPct)
  const takerL = num(row.takerBuyPct)

  rows.push({
    key: "accountLongShort",
    label: "全局多空账户比",
    value: fmt.fmtRatio(row.accountLongShort),
    aside: accL == null ? "" : `多头账户 ${accL.toFixed(1)}%`,
    side: sideOf(row.accountLongShort),
    meaning: "所有散户持仓账户里，做多的户数除以做空的户数。",
    usage: "极端值常被当作反向指标：多头账户占比过高时，上方容易出现清算挤压。",
  })
  rows.push({
    key: "topPositionLongShort",
    label: "大户持仓比",
    value: fmt.fmtRatio(row.topPositionLongShort),
    aside: topL == null ? "" : `多头持仓 ${topL.toFixed(1)}%`,
    side: sideOf(row.topPositionLongShort),
    meaning: "名义价值前 20% 的账户，按持仓量（不是户数）算出的多空比。",
    usage: "比账户比更接近真实资金方向，常与账户比反向使用：大户做多、散户做空时趋势更容易延续。",
  })
  rows.push({
    key: "takerBuySellRatio",
    label: "主动买卖量比",
    value: fmt.fmtRatio(row.takerBuySellRatio),
    aside: takerL == null ? "" : `主动买占 ${takerL.toFixed(1)}%`,
    side: sideOf(row.takerBuySellRatio),
    meaning: "最近 5 分钟吃单成交里，主动买入量与主动卖出量的比值。",
    usage: "反映当下的即时攻击方向；和价格同向时确认突破有效，背离时警惕假突破。",
  })
  rows.push({
    key: "oiChangePct",
    label: "持仓量变化",
    value: fmt.fmtPct(row.oiChangePct),
    aside: `当前持仓 ${fmt.fmtVol(row.openInterest)}`,
    side: num(row.oiChangePct) == null ? null : num(row.oiChangePct) > 0 ? "long" : "short",
    meaning: "最近 1 小时该合约未平仓合约总量的变化。",
    usage: "价格涨 + OI 涨 = 新多进场（趋势健康）；价格涨 + OI 跌 = 空头回补（涨幅难续）。",
  })
  rows.push({
    key: "fundingRate",
    label: "资金费率",
    value: fmt.fmtFunding(row.fundingRate),
    aside: fundingAside(row.nextFundingMs),
    side: num(row.fundingRate) == null ? null : num(row.fundingRate) > 0 ? "long" : "short",
    meaning: "多空双方每 8 小时互相支付的费率，正数表示多头付给空头。",
    usage: "费率持续为正说明多头拥挤，做多成本高；结算前 10 分钟内不利方向会被引擎拦下。",
  })
  return rows
}


export default function DerivativesDetail({ symbol, row, chartColors, onClose }) {
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const titleId = useId()

  useLayoutEffect(() => {
    const dialog = dialogRef.current
    const previous = document.activeElement
    dialog.showModal()
    closeRef.current?.focus({ preventScroll: true })
    return () => {
      dialog.close()
      if (previous?.isConnected) previous.focus({ preventScroll: true })
    }
  }, [])

  const rows = metricRows(row, {
    fmtRatio: (v) => (num(v) == null ? "--" : num(v).toFixed(3)),
    fmtPct: (v) => (num(v) == null ? "--" : `${num(v) > 0 ? "+" : ""}${num(v).toFixed(2)}%`),
    fmtFunding: (v) => (num(v) == null ? "--" : `${(num(v) * 100).toFixed(4)}%`),
    fmtVol: fmtVol,
  })
  const up = chartColors?.up || "#16a34a"
  const down = chartColors?.down || "#dc2626"
  const tone = (side) => (side === "long" ? up : side === "short" ? down : "rgba(16,24,40,0.55)")

  return (
    <dialog ref={dialogRef} className={styles.dialog} aria-labelledby={titleId}
      onCancel={(e) => { e.preventDefault(); onClose() }}
      onClose={onClose}>
      <header className={styles.header}>
        <div>
          <h2 id={titleId} className={styles.title}>{symbol.toUpperCase()}</h2>
          <p className={styles.sub}>
            {fmtPrice(row.price)} · {fmtPct(row.changePct || 0, 2)}
            <span className={styles.vol}>24h {fmtVol(row.quoteVolume)}</span>
          </p>
        </div>
        <button ref={closeRef} type="button" className={styles.close} onClick={onClose} aria-label="关闭">
          <X size={14} />
        </button>
      </header>

      <div className={styles.body}>
        {rows.map((item) => (
          <section key={item.key} className={styles.metric}>
            <div className={styles.metricHead}>
              <span className={styles.metricLabel}>{item.label}</span>
              <span className={styles.metricValue} style={{ color: tone(item.side) }}>{item.value}</span>
              {item.aside && <span className={styles.metricAside}>{item.aside}</span>}
            </div>
            <p className={styles.meaning}>{item.meaning}</p>
            <p className={styles.usage}>{item.usage}</p>
          </section>
        ))}
      </div>
    </dialog>
  )
}
