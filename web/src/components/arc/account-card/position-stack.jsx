/* 第一行右侧的仓位竖排栏。

为什么独立成一列：账户卡本身只有 ~236px 宽，两个仓位横排时每张只剩
113px，必须裁掉开仓/强平/保证金才塞得下——而用户明确不要裁
（"仓位还给我简化了，需要你简化吗？竖着排绝对够了啊"）。
竖排后每张卡占满整列宽度，字段一个不省；高度上限由 wallet-zone 决定，
超出滚动，不会把整行撑高。

同一个列表里也放最近已平仓位（状态标签写「已平」）——
用户原话："历史仓位可以放，多空那里就写已平不就可以"。
已平仓没有方向，所以标签用中性灰，不用多/空的涨跌色。
*/
import { useMemo } from "react"
import CoinAvatar from "@/components/ui/coin-avatar"

const fmtPrice = (v) => {
  const n = Number(v) || 0
  if (n >= 1000) return n.toFixed(1)
  if (n >= 1) return n.toFixed(3)
  return n.toPrecision(4)
}
const fmtNum = (v) => {
  const n = Number(v)
  if (!Number.isFinite(n)) return "--"
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

/** 一个持仓/已平仓的完整卡片：币种 · 方向 · 杠杆 / 盈亏 / 数量·开仓·强平·保证金 */
function Row({ p, closed, chartColors }) {
  const isClosed = Boolean(closed)
  const long = Number(p.amt) > 0 || String(p.side) === "BUY"
  const pnl = Number(isClosed ? p.pnl : p.unrealized) || 0
  const pct = Number(p.unrealized_pct)
  const base = String(p.symbol || "").replace(/usdt$/i, "").toUpperCase()
  const tone = pnl > 0 ? chartColors.up : pnl < 0 ? chartColors.down : "rgba(16,24,40,0.5)"
  // 已平仓显示 平仓价，持仓显示 强平价/保证金
  const details = isClosed
    ? [
        ["数量", fmtNum(Math.abs(Number(p.qty) || 0))],
        ["开仓", fmtPrice(p.entry)],
        ["平仓", fmtPrice(p.exit)],
        ["持有", `${Math.round(Number(p.holdMin) || 0)}m`],
      ]
    : [
        ["数量", fmtNum(Math.abs(Number(p.amt) || 0))],
        ["开仓", fmtPrice(p.entry)],
        ["强平", p.liquidation ? fmtPrice(p.liquidation) : "--"],
        ["保证金", p.margin != null ? fmtNum(p.margin) : "--"],
      ]
  return (
    <div className="stack-card"
      data-closed={isClosed || undefined}
      title={isClosed
        ? `${base} 已平 · 开 ${p.entry} → 平 ${p.exit} · ${p.reason || ""}`
        : `${base} ${long ? "做多" : "做空"} · 开仓 ${p.entry} · 标记 ${p.mark} · 名义 ${p.notional}`}>
      <div className="stack-head">
        <CoinAvatar symbol={p.symbol} size={12} />
        <b className="stack-sym">{base}</b>
        {isClosed
          ? <span className="stack-tag stack-tag-closed">已平</span>
          : <span className="stack-tag" data-side={long ? "long" : "short"}>{long ? "多" : "空"}</span>}
        <span className="stack-lev">{isClosed ? `${Math.round(Number(p.holdMin) || 0)}m` : `${p.leverage || "--"}x`}</span>
      </div>
      <div className="stack-pnl" style={{ color: tone }}>
        {(pnl > 0 ? "+" : "") + pnl.toFixed(2)}
        {Number.isFinite(pct) && !isClosed && (
          <span className="stack-pct">{(pct > 0 ? "+" : "") + pct.toFixed(2)}%</span>
        )}
      </div>
      <div className="stack-sep" />
      <div className="stack-rows">
        {details.map(([k, v]) => (
          <div className="stack-row" key={k}>
            <span className="stack-key">{k}</span>
            <span className="stack-val">{v}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function PositionStack({ positions = [], trades = [], chartColors }) {
  const open = Array.isArray(positions) ? positions : []
  // 已平仓只补到"总数不超过 4 条"：这块高度有限，先在屏的仓优先。
  const closed = useMemo(() => {
    const room = Math.max(0, 4 - open.length)
    return (Array.isArray(trades) ? trades : []).slice(0, room)
  }, [trades, open.length])

  if (!open.length && !closed.length) return null

  return (
    <div className="pos-stack" role="region" aria-label="仓位">
      {open.map((p) => (
        <Row key={`o-${p.symbol}`} p={p} chartColors={chartColors} />
      ))}
      {closed.map((t, i) => (
        <Row key={`c-${t.id ?? i}`} p={t} closed chartColors={chartColors} />
      ))}
    </div>
  )
}
