/* 决策依据：币种宫格（一行 3 个，滚动）。
   宽度只有第二行的 40%（约 290px），任何"矩阵/条带/热力图"都会挤成一团。
   直接按币种分卡片——左侧有几个币，这里就有几张卡，每张卡里放该币的全部
   决策数据，一眼一目了然，不用横向对齐列宽。

   卡片内容（全部来自 snapshot.analysis.symbols，前端不做任何推断）：
     · 币种 + 持仓方向 + 价格
     · 6 个核心指标（数值 + 热力底色）
     · 当前 ATR 口径的止损/止盈距离

   色阶：青绿=温和 → 琥珀 → 红=极端。中段平方根压缩，避免"偏高"看着像"极端"。 */
import { useMemo } from "react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import styles from "./strategy-panel.module.css"

const SYM = (s) => String(s || "").replace(/usdt$/i, "").toUpperCase()

const fmtPrice = (v) => {
  if (v == null || Number.isNaN(Number(v))) return "--"
  const n = Number(v)
  if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 1 })
  if (n >= 1) return n.toFixed(3)
  return n.toFixed(5)
}

const heatBg = (t) => {
  const x = Math.max(0, Math.min(1, Math.sqrt(Math.max(0, t - 0.25) / 0.75)))
  return `hsl(${(162 - 158 * x).toFixed(0)} ${(52 + 26 * x).toFixed(0)}% ${(94 - 48 * x).toFixed(0)}%)`
}
const heatInk = (t) => (t > 0.78 ? "#fff" : t > 0.5 ? "#2b3445" : "#5f6a78")

/** 6 个核心指标：id / 短名 / 全称 / 热度 / 显示值 */
const METRICS = [
  {
    id: "rsi", short: "RSI", name: "相对强弱",
    heat: (s) => (s.rsi == null ? null : Math.min(1, (Math.abs(s.rsi - 50) / 50) * 1.12)),
    text: (s) => (s.rsi == null ? "--" : String(Math.round(s.rsi))),
  },
  {
    id: "z", short: "Z", name: "价格偏离度",
    heat: (s) => (s.z == null ? null : Math.min(1, Math.abs(s.z) / 3)),
    text: (s) => (s.z == null ? "--" : s.z.toFixed(1)),
  },
  {
    id: "pctB", short: "%B", name: "布林带位置",
    heat: (s) => {
      const v = s.boll?.pctB
      if (v == null) return null
      if (v > 1) return Math.min(1, 0.72 + (v - 1) * 0.7)
      if (v < 0) return Math.min(1, 0.72 - v * 0.7)
      return Math.min(0.5, Math.abs(v - 0.5))
    },
    text: (s) => (s.boll?.pctB == null ? "--" : s.boll.pctB.toFixed(2)),
  },
  {
    id: "adx", short: "ADX", name: "趋势强度",
    heat: (s) => (s.adx == null ? null : Math.min(1, s.adx / 60)),
    text: (s) => (s.adx == null ? "--" : String(Math.round(s.adx))),
  },
  {
    id: "atrPct", short: "ATR", name: "ATR 历史分位",
    heat: (s) => (s.atrPct == null ? null : Math.min(1, s.atrPct / 100)),
    text: (s) => (s.atrPct == null ? "--" : String(Math.round(s.atrPct))),
  },
  {
    id: "bwPct", short: "BW", name: "布林带宽分位",
    heat: (s) => (s.bwPct == null ? null : Math.min(1, s.bwPct / 100)),
    text: (s) => (s.bwPct == null ? "--" : String(Math.round(s.bwPct))),
  },
]

/** 与后端 adaptive_sl_tp 同一口径：ATR 倍数 → 夹紧到 min/max 百分比 */
function riskBand(st, p) {
  const price = Number(st?.price) || 0
  const atr = Number(st?.atr) || 0
  const slMin = (p?.slMinPct ?? 0.4) / 100
  const slMax = (p?.slMaxPct ?? 2.0) / 100
  const tpMin = (p?.tpMinPct ?? 0.18) / 100
  const tpMax = (p?.tpMaxPct ?? 3.0) / 100
  let slPct = slMin, tpPct = tpMin
  if (price > 0 && atr > 0) {
    slPct = Math.min(slMax, Math.max(slMin, (atr * (p?.atrSlMult ?? 1.5)) / price))
    tpPct = Math.min(tpMax, Math.max(tpMin, (atr * (p?.atrTpMult ?? 2.5)) / price))
  }
  return { sl: slPct * 100, tp: tpPct * 100, live: price > 0 && atr > 0 }
}

export default function StrategyPanel({ analysis, chartColors }) {
  const syms = analysis?.symbols || {}
  const params = analysis?.params
  const dd = analysis?.drawdown
  const gateLog = analysis?.gateLog || []
  const plans = analysis?.positionPlans || []

  const cards = useMemo(() => Object.entries(syms), [syms])
  const planBySym = useMemo(() => {
    const m = new Map()
    for (const p of plans) if (p?.symbol) m.set(String(p.symbol).toLowerCase(), p)
    return m
  }, [plans])

  const ddCur = Number(dd?.current) || 0
  const ddLimit = Number(dd?.limit) || -5

  return (
    <section className={styles.wrap} aria-label="决策依据">
      {cards.length === 0 ? (
        <div className={styles.empty}>等待指标计算（需至少 30 根K线）…</div>
      ) : (
        <ScrollArea orientation="vertical" fade={10} scrollbars="auto"
          className={styles.gridScroll} label="币种决策宫格">
          <div className={styles.grid}>
            {cards.map(([sym, st]) => {
              const plan = planBySym.get(String(sym).toLowerCase())
              const side = plan ? (plan.side === "BUY" ? "多" : "空") : null
              const band = riskBand(st, params)
              return (
                <div key={sym} className={styles.cell} data-held={side ? "y" : undefined}>
                  <div className={styles.cellHead}>
                    <span className={styles.sym}>
                      {SYM(sym)}
                      {side && <i data-side={side === "多" ? "long" : "short"}>{side}</i>}
                    </span>
                    <span className={styles.price}>{fmtPrice(st?.price)}</span>
                  </div>

                  <div className={styles.tags}>
                    {st?.regime && (
                      <em data-tone={st.regime}>
                        {st.regime === "trend" ? "趋势" : st.regime === "range" ? "震荡" : st.regime === "mixed" ? "过渡" : "—"}
                      </em>
                    )}
                    {st?.volState && (
                      <em data-tone={st.volState === "high" ? "high" : st.volState === "low" ? "low" : "normal"}>
                        {st.volState === "high" ? "高波" : st.volState === "low" ? "低波" : "常波"}
                      </em>
                    )}
                    {st?.squeeze && <em data-tone="squeeze">挤压</em>}
                  </div>

                  <div className={styles.metrics}>
                    {METRICS.map((m) => {
                      const t = m.heat(st || {})
                      const na = t == null
                      return (
                        <span
                          key={m.id}
                          className={styles.metric}
                          data-na={na || undefined}
                          style={na ? undefined : { background: heatBg(t), color: heatInk(t) }}
                          title={`${m.name}：${m.text(st || {})}`}
                        >
                          <u>{m.short}</u>
                          <b>{m.text(st || {})}</b>
                        </span>
                      )
                    })}
                  </div>

                  <div className={styles.band}>
                    <span data-tone="sl" title="止损距离（ATR 口径，已夹紧）">-{band.sl.toFixed(2)}%</span>
                    <span data-tone="tp" title="止盈距离（ATR 口径，已夹紧）">+{band.tp.toFixed(2)}%</span>
                  </div>
                </div>
              )
            })}
          </div>
        </ScrollArea>
      )}

      <footer className={styles.foot}>
        <span className={styles.footDd} title="账户当前回撤 / 熔断线">
          <u>回撤</u>
          <b style={{ color: ddCur < -0.01 ? (chartColors?.down || "#e11d48") : "inherit" }}>
            {ddCur.toFixed(2)}%
          </b>
          <em>/{ddLimit.toFixed(1)}%</em>
          {dd?.paused && <s>熔断中</s>}
        </span>
        <span className={styles.footGate} title="最近被过滤器拦下的信号数">
          <b>{gateLog.length}</b>
          <u>拦截</u>
        </span>
      </footer>
    </section>
  )
}
