/* K线图右上角的「当前该干嘛」胶囊：方向 / 杠杆 / 持有时长 / 止损止盈。
   只展示后端算好的建议，不在前端做任何交易判断。 */
import { useMemo } from "react"
import styles from "./trade-advice.module.css"

const fmt = (v, digits = 2) => {
  const n = Number(v)
  if (!Number.isFinite(n)) return "--"
  const a = Math.abs(n)
  if (a >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 1 })
  if (a >= 1) return n.toFixed(digits > 2 ? 2 : digits)
  if (a >= 0.01) return n.toFixed(4)
  return n.toPrecision(4)
}

const ACTION = {
  long: { label: "做多", tone: "long" },
  short: { label: "做空", tone: "short" },
  wait: { label: "观望", tone: "wait" },
}

export default function TradeAdvice({ advice, chartColors }) {
  const meta = ACTION[advice?.action] || ACTION.wait
  const tone = useMemo(() => {
    if (meta.tone === "long") return chartColors?.up || "#16a34a"
    if (meta.tone === "short") return chartColors?.down || "#dc2626"
    return null
  }, [meta.tone, chartColors?.up, chartColors?.down])

  if (!advice) return null
  const trade = advice.action === "long" || advice.action === "short"
  const price = advice.price

  const label = trade
    ? `建议${meta.label}：杠杆 ${advice.leverage ?? "--"} 倍，止损 ${fmt(advice.sl)}，止盈 ${fmt(advice.tp)}`
    : `建议${meta.label}${advice.reason ? `：${advice.reason}` : ""}`

  return (
    <div className={styles.pill} data-tone={meta.tone}
      title={advice.reason || `参考价 ${fmt(price)}`}
      aria-label={label}>
      <span className={styles.side} style={tone ? { color: tone } : undefined}>
        <i style={tone ? { background: tone } : undefined} aria-hidden />
        {meta.label}
      </span>
      {!trade && advice.reason && (
        <span className={styles.reason}>{advice.reason}</span>
      )}
      {trade && <>
        <span className={styles.sep} aria-hidden />
        <span className={styles.item} title={`交易所该合约最高 ${advice.maxLeverage ?? "?"}x`}>
          <b>{advice.leverage ?? "--"}</b>x
        </span>
        <span className={styles.item} title="建议持有时长">
          {advice.holdMin != null ? `${Math.round(advice.holdMin)}m` : "--"}
        </span>
        <span className={styles.sep} aria-hidden />
        <span className={styles.item} data-tone="sl" title={`止损 ${fmt(advice.sl)}（${advice.slPct ?? "--"}%）`}>
          损 {advice.sl != null ? fmt(advice.sl) : "--"}
        </span>
        <span className={styles.item} data-tone="tp" title={`止盈 ${fmt(advice.tp)}（${advice.tpPct ?? "--"}%）`}>
          盈 {advice.tp != null ? fmt(advice.tp) : "--"}
        </span>
        {advice.rr != null && (
          <span className={styles.item} data-tone="rr" title="盈亏比">1:{fmt(advice.rr, 1)}</span>
        )}
      </>}
      {trade && advice.score != null && (
        <span className={styles.score} title="信号共鸣分">{Math.round(advice.score)}</span>
      )}
    </div>
  )
}
