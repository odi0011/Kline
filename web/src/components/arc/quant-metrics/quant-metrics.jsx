/* 币安衍生品指标（右上板块上半）。
   列表行沿用项目统一语言：22px 行高、圆角浅底 hover、ScrollArea 滚动容器。
   这一列只有约 190px 宽，所以每行只回答一个问题「现在谁占上风」：
   一个结论胶囊 + 一个 OI 变化；完整指标点开弹窗看。
   数据逐项来自币安公开接口 /futures/data 与 /fapi，每 60 秒刷新。 */
import { useMemo, useState } from "react"
import CoinAvatar from "@/components/ui/coin-avatar"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import DerivativesDetail from "./derivatives-detail"
import styles from "./quant-metrics.module.css"

const num = (v) => {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

const biasKey = (ratio) => {
  const n = num(ratio)
  if (n == null) return null
  if (n >= 1.3) return "long"
  if (n <= 0.77) return "short"
  return "flat"
}

/** 主力方向：大户持仓与主动买卖同向才算数，背离时明确说背离 */
const flowOf = (row) => {
  const top = biasKey(row.topPositionLongShort)
  const taker = biasKey(row.takerBuySellRatio)
  if (!top || !taker) return null
  if (top === "flat" || taker === "flat") return { key: "flat", text: "结构中性" }
  if (top === taker) return { key: top, text: top === "long" ? "主力偏多" : "主力偏空" }
  return { key: "flat", text: "多空背离" }
}

/** 汇总成一句话：优先说主力的方向，资金面背离时以背离为准 */
const verdictOf = (row) => {
  const flow = flowOf(row)
  if (!flow) return null
  if (flow.key !== "flat") return flow
  const taker = biasKey(row.takerBuySellRatio)
  if (flow.text === "多空背离") {
    return { key: taker, text: taker === "long" ? "背离·买盘强" : "背离·卖盘强" }
  }
  return flow
}

const fmtPct = (v, digits = 1) => {
  const n = num(v)
  return n == null ? "--" : `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`
}

function MetricRow({ symbol, row, chartColors, selected, onOpen }) {
  const up = chartColors?.up || "#16a34a"
  const down = chartColors?.down || "#dc2626"
  const verdict = verdictOf(row)
  const color = verdict?.key === "long" ? up : verdict?.key === "short" ? down : "rgba(16,24,40,0.42)"
  const oi = num(row.oiChangePct)

  return (
    <button type="button" className={styles.row} data-selected={selected || undefined}
      onClick={() => onOpen(symbol)} aria-haspopup="dialog"
      title={`${symbol.toUpperCase()} · 点开看完整指标`}>
      <span className={styles.symCell}>
        <CoinAvatar symbol={symbol} size={13} />
        <b>{symbol.replace(/usdt$/i, "").toUpperCase()}</b>
      </span>
      <span className={styles.verdict} style={{ color }}>{verdict?.text || "--"}</span>
      <span className={styles.oi} style={{ color: oi > 0 ? up : oi < 0 ? down : undefined }}>
        {fmtPct(row.oiChangePct)}
      </span>
    </button>
  )
}

export default function QuantMetrics({ rows, symbols, chartColors, selected = [] }) {
  const [open, setOpen] = useState(null)
  const list = useMemo(() => {
    const order = Array.isArray(symbols) && symbols.length ? symbols : Object.keys(rows || {})
    return order.map((s) => [s, rows?.[s]]).filter(([, r]) => r)
  }, [rows, symbols])

  const detail = open ? rows?.[open] : null

  return (
    <section className={styles.root} aria-label="币安衍生品量化指标">
      <ScrollArea orientation="vertical" fade={14} scrollbars="auto" label="币安衍生品量化指标"
        className={styles.scroll} viewportClassName={styles.viewport}>
        {list.map(([sym, row]) => (
          <MetricRow key={sym} symbol={sym} row={row} chartColors={chartColors}
            selected={selected.includes(sym)} onOpen={setOpen} />
        ))}
      </ScrollArea>
      {detail && <DerivativesDetail symbol={open} row={detail} chartColors={chartColors}
        onClose={() => setOpen(null)} />}
    </section>
  )
}
