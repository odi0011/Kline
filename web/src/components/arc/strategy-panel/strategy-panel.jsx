/* 决策依据面板（只占第二行 40% 宽，约 290px）。
   窄容器不能用「币种 × 指标」的二维表格——8 列 × 28px 放不下四字列名和数值，
   必然文字重叠。改用横向热力条带（heat strip）：

     币种   ▮▮▮▮▮▮▮▮   ATR90·带宽83
            └8 段色块，每段 = 一个指标的热度，顺序固定
   颜色表意、数值靠 hover，一行一个币，窄宽度下也不会互压。

   底部 ATR 风险区间沿用 split-bar 的分裂条语义（红段止损 / 绿段止盈）。 */
import { useEffect, useId, useMemo, useRef, useState } from "react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import styles from "./strategy-panel.module.css"

const SYM = (s) => String(s || "").replace(/usdt$/i, "").toUpperCase()

const fmt = (v, d = 2) => {
  if (v == null || Number.isNaN(Number(v))) return "--"
  const n = Number(v)
  return Math.abs(n) >= 1000 ? n.toLocaleString(undefined, { maximumFractionDigits: d }) : n.toFixed(d)
}

/** 热度 0..1 → 底色：青绿（温和）→ 琥珀 → 红（极端）。
 *  中段用平方根压缩，避免 0.7 的"偏高"渲染成和 1.0 一样的刺眼红。 */
const heatBg = (t) => {
  const x = Math.max(0, Math.min(1, Math.sqrt(Math.max(0, t - 0.25) / 0.75)))
  return `hsl(${(162 - 158 * x).toFixed(0)} ${(52 + 26 * x).toFixed(0)}% ${(94 - 48 * x).toFixed(0)}%)`
}

/** 8 个指标：heat 返回 0..1 或 null（数据不足），hot 用于排序「最热」 */
const COLS = [
  {
    key: "rsi", short: "RSI", label: "相对强弱 RSI",
    heat: (s) => (s.rsi == null ? null : Math.min(1, (Math.abs(s.rsi - 50) / 50) * 1.12)),
    text: (s) => (s.rsi == null ? "--" : String(Math.round(s.rsi))),
  },
  {
    key: "z", short: "Z", label: "价格偏离度 Z值",
    heat: (s) => (s.z == null ? null : Math.min(1, Math.abs(s.z) / 3)),
    text: (s) => (s.z == null ? "--" : s.z.toFixed(1)),
  },
  {
    key: "pctB", short: "%B", label: "布林带位置 %B",
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
    key: "adx", short: "ADX", label: "趋势强度 ADX",
    heat: (s) => (s.adx == null ? null : Math.min(1, s.adx / 60)),
    text: (s) => (s.adx == null ? "--" : String(Math.round(s.adx))),
  },
  {
    key: "atrPct", short: "ATR", label: "ATR 历史分位",
    heat: (s) => (s.atrPct == null ? null : Math.min(1, s.atrPct / 100)),
    text: (s) => (s.atrPct == null ? "--" : String(Math.round(s.atrPct))),
  },
  {
    key: "bwPct", short: "BW", label: "布林带宽分位",
    heat: (s) => (s.bwPct == null ? null : Math.min(1, s.bwPct / 100)),
    text: (s) => (s.bwPct == null ? "--" : String(Math.round(s.bwPct))),
  },
  {
    key: "realizedVol", short: "RV", label: "实测波动率",
    heat: (s) => (s.realizedVol == null ? null : Math.min(1, s.realizedVol / 0.02)),
    text: (s) => (s.realizedVol == null ? "--" : (s.realizedVol * 100).toFixed(2) + "%"),
  },
  {
    key: "roc", short: "ROC", label: "区间涨跌幅 ROC",
    heat: (s) => (s.roc == null ? null : Math.min(1, Math.abs(s.roc) / 0.02)),
    text: (s) => (s.roc == null ? "--" : (s.roc * 100).toFixed(2) + "%"),
  },
]

/** 与后端 adaptive_sl_tp 同一套口径：ATR 倍数 → 夹紧到 min/max 百分比 */
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
  return { slPct: slPct * 100, tpPct: tpPct * 100 }
}

/** 底部趋势：ADX 与 ATR 分位的全市场均值（面积叠加，streamgraph 观感） */
function TrendStrip({ data, chartColors }) {
  const gid = useId().replace(/:/g, "")
  const W = 100, H = 22
  if (data.length < 2) return <div className={styles.trendEmpty}>趋势积累中…</div>
  const n = data.length
  const path = (key) => {
    const pts = data.map((d, i) => [
      (i / (n - 1)) * W,
      H - Math.max(0, Math.min(1, d[key] || 0)) * (H - 2) - 1,
    ])
    const line = `M${pts.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join("L")}`
    return { line, area: `${line}L${W},${H}L0,${H}Z` }
  }
  const adx = path("adx"), atr = path("atr")
  return (
    <svg className={styles.trendSvg} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
      <defs>
        <linearGradient id={`${gid}a`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="hsl(215 70% 55% / 0.3)" />
          <stop offset="100%" stopColor="hsl(215 70% 55% / 0.02)" />
        </linearGradient>
        <linearGradient id={`${gid}b`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="hsl(20 85% 55% / 0.26)" />
          <stop offset="100%" stopColor="hsl(20 85% 55% / 0.02)" />
        </linearGradient>
      </defs>
      <path d={atr.area} fill={`url(#${gid}b)`} />
      <path d={adx.area} fill={`url(#${gid}a)`} />
      <path d={adx.line} fill="none" stroke={chartColors?.up || "#2563eb"} strokeWidth="0.7"
        vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
      <path d={atr.line} fill="none" stroke="#ea580c" strokeWidth="0.7"
        vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
    </svg>
  )
}

export default function StrategyPanel({ analysis, chartColors }) {
  const syms = analysis?.symbols || {}
  const params = analysis?.params
  const dd = analysis?.drawdown
  const gateLog = analysis?.gateLog || []
  const plans = analysis?.positionPlans || []
  const rows = useMemo(() => Object.entries(syms), [syms])

  // 趋势历史：每个快照把「全市场均值」压成一点（约 5s 一点，保留 ~7 分钟）
  const histRef = useRef([])
  const [, bumpHist] = useState(0)
  useEffect(() => {
    if (!rows.length) return
    const avg = (fn) => {
      let sum = 0, n = 0
      for (const [, s] of rows) {
        const v = Number(fn(s))
        if (Number.isFinite(v)) { sum += v; n++ }
      }
      return n ? sum / n : 0
    }
    const h = histRef.current
    h.push({ t: Date.now(), adx: avg((s) => (s.adx ?? 0) / 60), atr: avg((s) => (s.atrPct ?? 0) / 100) })
    if (h.length > 90) h.shift()
    bumpHist((v) => v + 1)
  }, [rows])

  const planBySym = useMemo(() => {
    const m = new Map()
    for (const p of plans) if (p?.symbol) m.set(String(p.symbol).toLowerCase(), p)
    return m
  }, [plans])

  // 全市场「最热」指标：右上角汇总，替代原来空着的空间
  const hottest = useMemo(() => {
    let best = null
    for (const [sym, st] of rows) {
      for (const c of COLS) {
        const t = c.heat(st || {})
        if (t == null) continue
        if (!best || t > best.t) best = { sym, col: c, t, val: c.text(st || {}) }
      }
    }
    return best
  }, [rows])

  const ddCur = Number(dd?.current) || 0
  const ddLimit = Number(dd?.limit) || -5
  const ddRatio = ddLimit !== 0 ? Math.min(1, Math.abs(ddCur) / Math.abs(ddLimit)) : 0

  return (
    <section className={styles.wrap} aria-label="决策依据">
      {/* 顶栏：左＝标题+色阶，右＝当前最热指标（利用原先空着的右上角） */}
      <header className={styles.head}>
        <span className={styles.headTitle}>指标热力</span>
        <span className={styles.scale}>
          <i />低
          <span className={styles.scaleBar} />
          高
        </span>
        {hottest && (
          <span className={styles.hottest} title={`${SYM(hottest.sym)} ${hottest.col.label} = ${hottest.val}`}>
            <em>{SYM(hottest.sym)}</em>
            {hottest.col.short}
            <b>{hottest.val}</b>
          </span>
        )}
      </header>

      {/* 指标热力条带：每币一行 */}
      <div className={styles.strips}>
        <ScrollArea orientation="vertical" fade={8} scrollbars="auto"
          className={styles.stripScroll} label="指标热力条带">
          <div className={styles.stripList}>
            {rows.map(([sym, st]) => {
              const plan = planBySym.get(String(sym).toLowerCase())
              const side = plan ? (plan.side === "BUY" ? "多" : "空") : ""
              // 取热度最高的两项，写在条带右侧
              const ranked = COLS
                .map((c) => ({ c, t: c.heat(st || {}) }))
                .filter((x) => x.t != null)
                .sort((a, b) => b.t - a.t)
                .slice(0, 2)
              return (
                <div key={sym} className={styles.stripRow}>
                  <span className={styles.stripSym}>
                    <b>{SYM(sym)}</b>
                    {side && <i data-side={side === "多" ? "long" : "short"}>{side}</i>}
                    {st?.regime === "range" && <u title="震荡市">震</u>}
                  </span>
                  <span className={styles.stripBar}>
                    {COLS.map((c) => {
                      const t = c.heat(st || {})
                      const val = c.text(st || {})
                      return (
                        <span
                          key={c.key}
                          className={styles.seg}
                          data-na={t == null || undefined}
                          style={t == null ? undefined : { background: heatBg(t) }}
                          title={`${c.label}：${val}`}
                        />
                      )
                    })}
                  </span>
                  <span className={styles.stripTags}>
                    {ranked.map(({ c, t }) => (
                      <em key={c.key} data-hot={t > 0.75 || undefined} title={c.label}>
                        {c.short} {c.text(st || {})}
                      </em>
                    ))}
                  </span>
                </div>
              )
            })}
            {rows.length === 0 && <div className={styles.empty}>等待指标计算（需至少 30 根K线）…</div>}
          </div>
        </ScrollArea>
      </div>

      {/* ATR 风险区间 */}
      <div className={styles.riskBlock}>
        <div className={styles.riskHead}>
          <span className={styles.riskTitle}>ATR 风险区间</span>
          <span className={styles.riskLegend}>
            <i data-tone="sl" />止损<i data-tone="tp" />止盈
          </span>
        </div>
        <div className={styles.riskList}>
          {rows.map(([sym, st]) => {
            const b = riskBand(st, params)
            const total = b.slPct + b.tpPct
            const slW = total > 0 ? (b.slPct / total) * 100 : 50
            const rr = b.slPct > 0 ? b.tpPct / b.slPct : 0
            return (
              <div key={sym} className={styles.riskRow}>
                <span className={styles.riskSym}>{SYM(sym)}</span>
                <span className={styles.riskBar}
                  title={`止损 ${b.slPct.toFixed(2)}% / 止盈 ${b.tpPct.toFixed(2)}%（ATR ${fmt(st?.atr, st?.atr >= 100 ? 2 : 6)}）`}>
                  <span className={styles.riskSl} style={{ width: `${slW}%` }} />
                  <span className={styles.riskTp} style={{ width: `${100 - slW}%` }} />
                </span>
                <span className={styles.riskNum}>
                  <i data-tone="sl">-{b.slPct.toFixed(2)}</i>
                  <i data-tone="tp">+{b.tpPct.toFixed(2)}</i>
                </span>
                <span className={styles.riskRr} title="盈亏比">{rr.toFixed(2)}</span>
              </div>
            )
          })}
          {rows.length === 0 && <div className={styles.empty}>—</div>}
        </div>
      </div>

      {/* 底栏：趋势 + 回撤 + 拦截 */}
      <footer className={styles.bottom}>
        <span className={styles.trendLegend} title="全市场 ADX / ATR 分位均值走势">
          <i data-tone="adx" />ADX<i data-tone="atr" />ATR
        </span>
        <div className={styles.trendBox}>
          <TrendStrip data={histRef.current} chartColors={chartColors} />
        </div>
        <span className={styles.ddGroup} title="账户回撤 / 熔断线">
          <em>回撤</em>
          <b style={{ color: ddCur < -0.01 ? (chartColors?.down || "#e11d48") : "inherit" }}>
            {ddCur.toFixed(2)}%
          </b>
          <span className={styles.ddBar}>
            <span className={styles.ddFill} data-tone={dd?.paused ? "danger" : ddRatio > 0.6 ? "warn" : "ok"}
              style={{ width: `${ddRatio * 100}%` }} />
          </span>
          {dd?.paused && <b className={styles.pausedTag}>熔断</b>}
        </span>
        <span className={styles.gateGroup} title={gateLog.length ? "最近被过滤器拦下的信号数" : "暂无拦截"}>
          <b>{gateLog.length}</b>
          <em>拦截</em>
        </span>
      </footer>
    </section>
  )
}
