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
  // 信号年龄：基于哪根已收盘K线（basisBarTime）+ 该周期长度。
  // 图上价格每秒都在跳，而结论是一根K线算一次的——不把"这个结论是几点的"说出来，
  // 用户会拿跳动的K线去处对静止的止损价，觉得是数据坏了（线上原话："这都是啥价"）。
  const barMs = Number(advice.barMs) || 0
  const basisTs = Number(advice.basisBarTime) || 0
  const ageBars = barMs && basisTs
    ? Math.max(0, Math.floor((Date.now() - basisTs - barMs) / barMs))
    : 0
  // 超过 2 根K线没更新 = 这个币已经不在量化范围内（引擎不刷它了），
  // 明确降级成"旧信号"，不要装作是当前的。
  const stale = trade && ageBars >= 2
  // 入场时机：now = 现价可入；wait = 等回到区间
  const zone = Array.isArray(advice.entryZone) ? advice.entryZone : null
  const entryText = !trade ? ""
    : advice.timing === "wait" && zone
      ? `等 ${fmt(zone[0])}-${fmt(zone[1])}`
      : "可入"

  const label = trade
    ? `建议${meta.label}：${advice.timing === "wait" ? "等回落到入场区" : "现价可入"}，`
      + `${advice.holdMin ? `持有约 ${Math.round(advice.holdMin)} 分钟，` : ""}`
      + `杠杆 ${advice.leverage ?? "--"} 倍，止损 ${fmt(advice.sl)}，止盈 ${fmt(advice.tp)}`
    : `建议${meta.label}${advice.reason ? `：${advice.reason}` : ""}`

  // 止损价的来源：图上哪条线、距现价多少。用户问"这都是啥价"时，
  // 答案必须就在 tooltip 里，而不是让他自己去猜。
  const slTitle = advice.sl == null ? "止损"
    : `止损 ${fmt(advice.sl)}（${advice.slPct ?? "--"}%）`
      + (advice.slBasis && advice.slBasis !== "ATR"
        ? `\n依据：${advice.slBasis}${advice.slLevel ? ` ${fmt(advice.slLevel)}` : ""}外侧`
        : "\n依据：ATR 波动幅度（图上没有可用的结构位）")
  const tpTitle = advice.tp == null ? "止盈"
    : `止盈 ${fmt(advice.tp)}（${advice.tpPct ?? "--"}%）`
      + (advice.tpBasis ? `\n依据：${advice.tpBasis}` : "")

  return (
    <div className={styles.pill} data-tone={meta.tone}
      data-stale={stale || undefined}
      title={(advice.reason || `参考价 ${fmt(price)}`)
        + (stale ? `\n\n这个结论来自 ${ageBars} 根K线前，已经过期——`
                   + `该币不在量化交易范围里，引擎不再更新它。` : "")}
      aria-label={label}>
      <span className={styles.side} style={tone ? { color: tone } : undefined}>
        <i style={tone ? { background: tone } : undefined} aria-hidden />
        {meta.label}
        {/* 方向被锁住（分差不够没翻）：标一个小锁，说明"上一轮就是这个方向" */}
        {trade && advice.directionLatched && (
          <span className={styles.latch} title="信号优势不明显，维持上一轮方向">锁</span>
        )}
        {/* 信号过期：这个币已经不在量化范围，结论不是现在的。
            宁可显式标出来，也不能让用户拿旧价去下单。 */}
        {stale && (
          <span className={styles.stale} title={`结论来自 ${ageBars} 根K线前`}>
            旧 {ageBars}根
          </span>
        )}
      </span>
      {!trade && advice.reason && (
        <span className={styles.reason}>{advice.reason}</span>
      )}
      {trade && <>
        <span className={styles.sep} aria-hidden />
        {/* 入场时机：这是最关键的一条——什么时候进比进不进方向更重要 */}
        <span className={styles.item} data-tone={advice.timing === "wait" ? "wait" : "entry"}
          title={advice.entryNote || "入场时机"}>
          {entryText}
        </span>
        <span className={styles.sep} aria-hidden />
        <span className={styles.item} title={`交易所该合约最高 ${advice.maxLeverage ?? "?"}x`}>
          <b>{advice.leverage ?? "--"}</b>x
        </span>
        <span className={styles.item} title="建议持有时长（超时未达目标就平掉）">
          {advice.holdMin != null ? `${Math.round(advice.holdMin)}m` : "--"}
        </span>
        <span className={styles.sep} aria-hidden />
        <span className={styles.item} data-tone="sl" title={slTitle}>
          损 {advice.sl != null ? fmt(advice.sl) : "--"}
        </span>
        <span className={styles.item} data-tone="tp" title={tpTitle}>
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
