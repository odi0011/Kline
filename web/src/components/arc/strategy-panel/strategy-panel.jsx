/* 决策依据：币种宫格（一行 2 个，滚动）。
   面板只占第二行 30%（约 220~300px），所以按币种分卡片，左侧有几个币就几张卡。

   卡片布局：
     [icon] BTC 多                    [震荡][常波][挤压]   ← 图标+名称在左，状态标签在右
     ─────────────────────────────────────────────
     RSI 47    Z 0.4     %B 0.61     ADX 18      ← 主指标（热力底色）
     ATR 69    BW 61     RV 0.08%    ROC 0.10%   ← 次指标（热力底色）
     ─────────────────────────────────────────────
     止损 -0.40%   止盈 +0.67%   盈亏比 1.67
     ATR 100.4    带宽 0.47%    +DI 26/-DI 35

   价格不在此展示——左侧 K 线大图已有；这块空间让给状态标签。
   全部数值来自 snapshot.analysis.symbols，前端不做推断。 */
import { useMemo } from "react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import CoinAvatar from "@/components/ui/coin-avatar"
import styles from "./strategy-panel.module.css"

const SYM = (s) => String(s || "").replace(/usdt$/i, "").toUpperCase()

const num = (v, d = 2) => (v == null || Number.isNaN(Number(v)) ? "--" : Number(v).toFixed(d))

/** 热度 0..1 → 底色：青绿（温和）→ 琥珀 → 红（极端）。
 *  中段平方根压缩，避免 0.7 的"偏高"渲染成和 1.0 一样的刺眼红。 */
const heatBg = (t) => {
  const x = Math.max(0, Math.min(1, Math.sqrt(Math.max(0, t - 0.25) / 0.75)))
  return `hsl(${(162 - 158 * x).toFixed(0)} ${(52 + 26 * x).toFixed(0)}% ${(94 - 48 * x).toFixed(0)}%)`
}
const heatInk = (t) => (t > 0.78 ? "#fff" : t > 0.5 ? "#2b3445" : "#5f6a78")

/** 8 个指标（4 列 × 2 行）：id / 缩写 / 全名 / 热度 / 显示值 */
const METRICS = [
  {
    id: "rsi", short: "RSI", name: "相对强弱",
    heat: (s) => (s.rsi == null ? null : Math.min(1, (Math.abs(s.rsi - 50) / 50) * 1.12)),
    text: (s) => (s.rsi == null ? "--" : String(Math.round(s.rsi))),
  },
  {
    id: "z", short: "Z", name: "价格偏离度（标准差）",
    heat: (s) => (s.z == null ? null : Math.min(1, Math.abs(s.z) / 3)),
    text: (s) => (s.z == null ? "--" : s.z.toFixed(1)),
  },
  {
    id: "pctB", short: "%B", name: "布林带位置（0下轨/1上轨）",
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
    id: "adx", short: "ADX", name: "趋势强度（≥25 趋势）",
    heat: (s) => (s.adx == null ? null : Math.min(1, s.adx / 60)),
    text: (s) => (s.adx == null ? "--" : String(Math.round(s.adx))),
  },
  {
    id: "atrPct", short: "ATR", name: "ATR 历史分位",
    heat: (s) => (s.atrPct == null ? null : Math.min(1, s.atrPct / 100)),
    text: (s) => (s.atrPct == null ? "--" : String(Math.round(s.atrPct))),
  },
  {
    id: "bwPct", short: "BW", name: "布林带宽分位（低=挤压）",
    heat: (s) => (s.bwPct == null ? null : Math.min(1, s.bwPct / 100)),
    text: (s) => (s.bwPct == null ? "--" : String(Math.round(s.bwPct))),
  },
  {
    id: "realizedVol", short: "RV", name: "实测波动率",
    heat: (s) => (s.realizedVol == null ? null : Math.min(1, s.realizedVol / 0.02)),
    text: (s) => (s.realizedVol == null ? "--" : (s.realizedVol * 100).toFixed(2)),
  },
  {
    id: "roc", short: "ROC", name: "区间涨跌幅",
    heat: (s) => (s.roc == null ? null : Math.min(1, Math.abs(s.roc) / 0.02)),
    text: (s) => (s.roc == null ? "--" : (s.roc * 100).toFixed(2)),
  },
]

const REGIME = {
  trend: { text: "趋势", tone: "trend" },
  range: { text: "震荡", tone: "range" },
  mixed: { text: "过渡", tone: "mixed" },
  unknown: { text: "—", tone: "muted" },
}
const VOL = {
  high: { text: "高波", tone: "high" },
  normal: { text: "常波", tone: "normal" },
  low: { text: "低波", tone: "low" },
}

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
  return { sl: slPct * 100, tp: tpPct * 100 }
}

export default function StrategyPanel({ analysis, symbols }) {
  const syms = analysis?.symbols || {}
  const params = analysis?.params
  const plans = analysis?.positionPlans || []

  // 严格跟随左侧 K 线展示的币种（App 传入的 symbols 已按"持仓优先"排序）：
  // 只显示这些币，且顺序与左侧一致。池里其它币即使算出了指标也不显示，
  // 保证「左侧看哪些币，这里就分析哪些币」这条规则唯一。
  const cards = useMemo(() => {
    const list = Array.isArray(symbols) ? symbols : []
    return list
      .map((s) => String(s).toLowerCase())
      .filter((s) => syms[s])
      .map((s) => [s, syms[s]])
  }, [symbols, syms])
  const planBySym = useMemo(() => {
    const m = new Map()
    for (const p of plans) if (p?.symbol) m.set(String(p.symbol).toLowerCase(), p)
    return m
  }, [plans])

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
              const regime = REGIME[st?.regime] || REGIME.unknown
              const vol = VOL[st?.volState]
              const rr = band.sl > 0 ? band.tp / band.sl : 0
              return (
                <div key={sym} className={styles.cell} data-held={side ? "y" : undefined}>
                  {/* 头部：图标 + 币种 + 方向（左）／状态标签（右，原价格位） */}
                  <div className={styles.cellHead}>
                    <CoinAvatar symbol={sym} size={16} />
                    <span className={styles.sym}>
                      {SYM(sym)}
                      {side && <i data-side={side === "多" ? "long" : "short"}>{side}</i>}
                    </span>
                    <span className={styles.tags}>
                      {st?.regime && <em data-tone={regime.tone}>{regime.text}</em>}
                      {vol && <em data-tone={vol.tone}>{vol.text}</em>}
                      {st?.squeeze && <em data-tone="squeeze">挤压</em>}
                    </span>
                  </div>

                  {/* 8 个指标：4 列 × 2 行 */}
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

                  {/* 风控区间 + ATR 明细 */}
                  <div className={styles.band}>
                    <span data-tone="sl" title="止损距离（1.5×ATR，夹紧到 0.4%~2%）">
                      -{band.sl.toFixed(2)}%
                    </span>
                    <span data-tone="tp" title="止盈距离（2.5×ATR，夹紧到 0.18%~3%）">
                      +{band.tp.toFixed(2)}%
                    </span>
                    <span data-tone="rr" title="盈亏比 = 止盈距离 ÷ 止损距离">
                      {rr.toFixed(2)}
                    </span>
                  </div>
                  <div className={styles.detail}>
                    <span title="平均真实波幅">
                      ATR {st?.atr == null ? "--" : st.atr >= 100 ? st.atr.toFixed(1) : st.atr >= 1 ? st.atr.toFixed(2) : st.atr.toPrecision(3)}
                    </span>
                    <span title="布林带宽">
                      BW {st?.boll?.bandwidth != null ? (st.boll.bandwidth * 100).toFixed(2) + "%" : "--"}
                    </span>
                    <span title="方向性指标 +DI / -DI">
                      DI {num(st?.plusDI, 0)}/{num(st?.minusDI, 0)}
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
        </ScrollArea>
      )}
    </section>
  )
}
