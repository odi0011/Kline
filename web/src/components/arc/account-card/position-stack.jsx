/* 第一行右侧的仓位竖排栏：贴在账户卡右边，占满 wallet-zone 的剩余宽度。

## 为什么必须绝对定位

上一版写 `height: 100%`，而父容器 `.wallet-zone` 高度是 auto，
百分比在 auto 父级里等于"跟随内容"——4 张仓位卡把这一列撑到 412px，
再把整行（连同形态区）一起拉高，首行等高彻底崩掉（线上实测
row 197 → 412）。

`.pos-stack` 现在 `position: absolute; top/bottom: 0`：完全脱离文档流，
它的高度由 wallet-zone 决定，内容再多也只在自己内部滚动，
**永远不可能撑高首行**。

## 宽度

`clamp(128px, 38%, 168px)`：wallet 越窄这一列越窄，把宽度让给权益卡。
固定 168px 会把 361px 的 wallet 挤得只剩 193px，概览的四个指标
直接叠在一起（线上截图里 `+74.83` 和 `+91.27` 重叠成一串）。

## 滚动条

用页面统一的 ScrollArea 组件，而不是裸 `overflow-y:auto`——
滚动条样式、边缘渐隐、键盘滚动行为全站一致（用户要求"滚动盒子也要统一"）。
*/
import { useMemo } from "react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import CoinAvatar from "@/components/ui/coin-avatar"
import "./position-stack.css"

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
  // 已平仓显示 平仓价/持有时间，持仓显示 强平价/保证金
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
  const closed = useMemo(() => {
    // 持仓优先占位；剩余名额补最近已平仓
    const room = Math.max(0, 4 - open.length)
    return (Array.isArray(trades) ? trades : []).slice(0, room)
  }, [trades, open.length])

  if (!open.length && !closed.length) return null

  return (
    <ScrollArea className="pos-stack" viewportClassName="pos-stack-viewport"
      orientation="vertical" fade={10} scrollbars="auto" label="仓位">
      <div className="pos-stack-list">
        {open.map((p) => (
          <Row key={`o-${p.symbol}`} p={p} chartColors={chartColors} />
        ))}
        {closed.map((t, i) => (
          <Row key={`c-${t.id ?? i}`} p={t} closed chartColors={chartColors} />
        ))}
      </div>
    </ScrollArea>
  )
}
