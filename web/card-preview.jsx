/* 账户卡布局预览：单独挂载 AccountCard，注入真实形状的持仓数据。

为什么需要它：账户卡在"无仓位 / 有仓位"下是两套完全不同的高度预算
（有仓位时持仓卡 + 市场概览叠在一起）。线上要复现"有仓位"必须真的开个仓，
而这里直接给组件喂数据，量出来的就是真实渲染结果。

只用于开发/回归，不进生产包（vite 只在 dev 下提供这个入口）。
*/
import { createRoot } from "react-dom/client"
import AccountCard from "@/components/arc/account-card/account-card"
import "./src/styles.css"

const chartColors = { up: "#16a34a", down: "#dc2626" }

const position = (sym, amt, entry, mark, upnl, pct, lev, liq, margin, notional) => ({
  symbol: sym, amt, entry, mark, unrealized: upnl, unrealized_pct: pct,
  leverage: lev, liquidation: liq, margin, notional, isolated: false,
})

const account = (positions) => ({
  name: "OODDDDOOO",
  id: "preview",
  balance: 7337.12,
  unrealized: positions.reduce((s, p) => s + p.unrealized, 0),
  available: 7300,
  positions,
  series: Array.from({ length: 60 }, (_, i) => ({
    t: Date.now() - (60 - i) * 30000,
    bal: 7300 + Math.sin(i / 5) * 18 + i,
    upnl: 0,
  })),
})

/* 与线上截图同形：两个 3 倍仓位，盈亏微小 */
const twoPositions = [
  position("dogeusdt", 2034, 0.07210, 0.07239, 0.02, 0.04, 3, 0.0975, 14.2, 147.2),
  position("xrpusdt", 119.8, 1.217, 1.223, 0.07, 0.15, 3, 1.64, 12.8, 146.5),
]

const overview = {
  sentiment: { label: "偏空", bullish: 1, bearish: 8, breadth: 11, avgScore: 60.9 },
  m30: { trades: 186, wins: 62, losses: 74, draws: 50, winrate: 46, pnl: -2.03,
         maxDrawdown: -8.26, sharpe: 0.0, realTrades: 1, paperTrades: 185 },
  today: { trades: 194, wins: 64, losses: 76, draws: 54, winrate: 46, pnl: -1.90,
           maxDrawdown: -31.07, sharpe: 0.0, realTrades: 1, paperTrades: 193 },
  all: { trades: 2000, wins: 700, losses: 760, draws: 540, winrate: 48, pnl: -2.06,
         maxDrawdown: -45.65, sharpe: -0.01, realTrades: 58, paperTrades: 1942 },
}

const cases = [
  ["无仓位", [], 230],
  ["两个仓位（线上同形）", twoPositions, 230],
  ["四个仓位", [...twoPositions,
    position("btcusdt", 0.012, 80300, 80410, 1.2, 0.14, 5, 66000, 190, 964),
    position("solusdt", 8.4, 96.7, 96.4, -2.1, -0.28, 3, 68, 270, 810)], 230],
]

function App() {
  return (
    <div style={{ padding: 16, background: "#eef1f6", display: "flex",
                  alignItems: "flex-start", gap: 16 }}>
      {cases.map(([label, positions, width]) => (
        <div key={label} style={{ width, flex: "none" }}>
          <div style={{ font: "600 12px system-ui", marginBottom: 6 }}>{label}（{width}px）</div>
          <AccountCard account={account(positions)} series={account(positions).series}
            chartColors={chartColors} accent="#f0b90b" overview={overview} />
        </div>
      ))}
    </div>
  )
}

createRoot(document.getElementById("root")).render(<App />)
