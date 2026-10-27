/* 决策依据面板：不分页签，一屏呈现三层信息。数据全部来自
   quant.snapshot().analysis，前端只做呈现与趋势累积，不臆造任何数值。

   主视图 · 指标矩阵热力图
     行 = 币种，列 = 关键指标，色阶 = 该指标偏离"平静态"的程度
     （青绿＝温和 → 琥珀 → 红＝极端）。视觉参考 uiarc activity-terrain
     的 2D 热力视图：用同一把色阶把"哪里紧张"一眼标出来。

   辅助视图 · ATR 风险区间
     每个币一条 split-bar：左段＝止损距离，右段＝止盈距离，宽度按
     1.5×ATR / 2.5×ATR 夹紧后的实际百分比分配（拆条语义参考 uiarc
     split-bar）。这样能直接看出风控给这笔交易留了多大空间、盈亏比多少。

   底部 · 趋势条
     前端按快照累积的 ADX / ATR 分位 走势（streamgraph 观感），右侧接
     当前回撤与过滤器拦截计数。

   uiarc 的 customer-galaxy / slope-chart 未采用：前者需要上千个带分组
   的散点（本项目只有 ≤10 个币，散点退化成几个点，信息量不如热力矩阵），
   后者需要两个时点的同指标对比（后端只提供最新快照，历史指标未落库）。 */
import { useEffect, useId, useMemo, useRef, useState } from "react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import styles from "./strategy-panel.module.css"

const SYM = (s) => String(s || "").replace(/usdt$/i, "").toUpperCase()

const fmt = (v, d = 2) => {
  if (v == null || Number.isNaN(Number(v))) return "--"
  const n = Number(v)
  return Math.abs(n) >= 1000 ? n.toLocaleString(undefined, { maximumFractionDigits: d }) : n.toFixed(d)
}

/** 热度 0..1 → 底色：青绿（温和）→ 琥珀 → 红（极端） */
const heatBg = (t) => {
  const x = Math.max(0, Math.min(1, t))
  return `hsl(${(162 - 158 * x).toFixed(0)} ${(56 + 24 * x).toFixed(0)}% ${(93 - 47 * x).toFixed(0)}%)`
}
const heatFg = (t) => (t > 0.58 ? "#fff" : t > 0.32 ? "#2b3445" : "#66707f")

/** 指标列定义：heat 返回 0..1（null = 数据不足），text 是格内显示值 */
const COLS = [
  {
    key: "rsi", label: "RSI", hint: "相对强弱：越靠近 70/30 两端越热",
    heat: (s) => (s.rsi == null ? null : Math.min(1, (Math.abs(s.rsi - 50) / 50) * 1.12)),
    text: (s) => (s.rsi == null ? "--" : Math.round(s.rsi)),
  },
  {
    key: "z", label: "Z值", hint: "价格偏离均值的标准差倍数，|z|≥2 视为极端",
    heat: (s) => (s.z == null ? null : Math.min(1, Math.abs(s.z) / 3)),
    text: (s) => (s.z == null ? "--" : s.z.toFixed(1)),
  },
  {
    key: "boll", label: "%B", hint: "布林带位置：0=下轨、1=上轨，越出轨道会变热",
    heat: (s) => {
      const v = s.boll?.pctB
      if (v == null) return null
      if (v > 1) return Math.min(1, 0.72 + (v - 1) * 0.7)
      if (v < 0) return Math.min(1, 0.72 - v * 0.7)
      return Math.min(0.5, Math.abs(v - 0.5) * 1.0)
    },
    text: (s) => (s.boll?.pctB == null ? "--" : s.boll.pctB.toFixed(2)),
  },
  {
    key: "adx", label: "ADX", hint: "趋势强度：≥25 顺势，≤20 震荡",
    heat: (s) => (s.adx == null ? null : Math.min(1, s.adx / 60)),
    text: (s) => (s.adx == null ? "--" : Math.round(s.adx)),
  },
  {
    key: "atrPct", label: "ATR分位", hint: "当前 ATR 在近 100 根中的分位，越高波动越大",
    heat: (s) => (s.atrPct == null ? null : Math.min(1, s.atrPct / 100)),
    text: (s) => (s.atrPct == null ? "--" : Math.round(s.atrPct)),
  },
  {
    key: "bwPct", label: "带宽分位", hint: "布林带宽分位：越低越接近挤压",
    heat: (s) => (s.bwPct == null ? null : Math.min(1, s.bwPct / 100)),
    text: (s) => (s.bwPct == null ? "--" : Math.round(s.bwPct)),
  },
  {
    key: "realizedVol", label: "实测波动", hint: "近期收益率标准差（%），反映实际振幅",
    heat: (s) => (s.realizedVol == null ? null : Math.min(1, s.realizedVol / 0.02)),
    text: (s) => (s.realizedVol == null ? "--" : (s.realizedVol * 100).toFixed(2)),
  },
  {
    key: "roc", label: "ROC", hint: "区间涨跌幅（%），绝对值越大越热",
    heat: (s) => (s.roc == null ? null : Math.min(1, Math.abs(s.roc) / 0.02)),
    text: (s) => (s.roc == null ? "--" : (s.roc * 100).toFixed(2)),
  },
]

/** 按 adaptive_sl_tp 的同一套口径算止损/止盈距离（夹紧后），供风险区间条使用 */
function riskBand(sym, st, p) {
  const price = Number(st?.price) || 0
  const atr = Number(st?.atr) || 0
  const slMult = p?.atrSlMult ?? 1.5
  const tpMult = p?.atrTpMult ?? 2.5
  const slMin = (p?.slMinPct ?? 0.4) / 100
  const slMax = (p?.slMaxPct ?? 2.0) / 100
  const tpMin = (p?.tpMinPct ?? 0.18) / 100
  const tpMax = (p?.tpMaxPct ?? 3.0) / 100
  let slPct = slMin
  let tpPct = tpMin
  if (price > 0 && atr > 0) {
    slPct = Math.min(slMax, Math.max(slMin, (atr * slMult) / price))
    tpPct = Math.min(tpMax, Math.max(tpMin, (atr * tpMult) / price))
  }
  return { sym, slPct: slPct * 100, tpPct: tpPct * 100, valid: price > 0 && atr > 0 }
}

/** 底部趋势：ADX 与 ATR 分位两条归一化面积（streamgraph 观感） */
function TrendStrip({ data, chartColors }) {
  const gid = useId().replace(/:/g, "")
  const W = 100
  const H = 26
  if (data.length < 2) {
    return <div className={styles.trendEmpty}>趋势积累中…</div>
  }
  const n = data.length
  const line = (key, base) => {
    const pts = data.map((d, i) => {
      const x = (i / (n - 1)) * W
      const v = Math.max(0, Math.min(1, (d[key] || 0) / base))
      return [x, H - v * (H - 2) - 1]
    })
    const d0 = `M${pts.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join("L")}`
    return { line: d0, area: `${d0}L${W},${H}L0,${H}Z` }
  }
  const adx = line("adx", 1)      // 已归一化
  const atr = line("atr", 1)
  return (
    <svg className={styles.trendSvg} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden>
      <defs>
        <linearGradient id={`${gid}a`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="hsl(215 70% 55% / 0.34)" />
          <stop offset="100%" stopColor="hsl(215 70% 55% / 0.02)" />
        </linearGradient>
        <linearGradient id={`${gid}b`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="hsl(20 85% 55% / 0.30)" />
          <stop offset="100%" stopColor="hsl(20 85% 55% / 0.02)" />
        </linearGradient>
      </defs>
      <path d={atr.area} fill={`url(#${gid}b)`} />
      <path d={adx.area} fill={`url(#${gid}a)`} />
      <path d={adx.line} fill="none" stroke={chartColors?.up || "#2563eb"} strokeWidth="0.7"
        strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      <path d={atr.line} fill="none" stroke="#ea580c" strokeWidth="0.7"
        strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
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

  // 趋势历史：每次快照到达时把「全市场均值」压成一个点（约 5s 一点，保留 7 分钟）
  const histRef = useRef([])
  const [, bumpHist] = useState(0)
  useEffect(() => {
    if (!rows.length) return
    const avg = (fn) => {
      let sum = 0, n = 0
      for (const [, s] of rows) { const v = Number(fn(s)); if (Number.isFinite(v)) { sum += v; n++ } }
      return n ? sum / n : 0
    }
    const h = histRef.current
    h.push({
      t: Date.now(),
      adx: avg((s) => (s.adx ?? 0) / 60),
      atr: avg((s) => (s.atrPct ?? 0) / 100),
    })
    if (h.length > 90) h.shift()
    bumpHist((v) => v + 1)
  }, [rows])

  const planBySym = useMemo(() => {
    const m = new Map()
    for (const p of plans) if (p?.symbol) m.set(String(p.symbol).toLowerCase(), p)
    return m
  }, [plans])

  const ddCur = Number(dd?.current) || 0
  const ddLimit = Number(dd?.limit) || -5
  const ddRatio = ddLimit !== 0 ? Math.min(1, Math.abs(ddCur) / Math.abs(ddLimit)) : 0

  const gridCols = `48px repeat(${COLS.length}, minmax(0, 1fr))`

  return (
    <section className={styles.wrap} aria-label="决策依据">
      <div className={styles.main}>
        {/* ---------- 主视图：指标矩阵热力图 ---------- */}
        <div className={styles.matrixWrap}>
          <div className={styles.legendRow}>
            <span className={styles.legendLabel}>指标矩阵</span>
            <span className={styles.legend}>
              温和
              <span className={styles.legendBar} />
              极端
            </span>
            <span className={styles.legendHint}>颜色 = 该指标偏离平静态的程度</span>
          </div>
          <div className={styles.colHead} style={{ gridTemplateColumns: gridCols }}>
            <span />
            {COLS.map((c) => (
              <span key={c.key} className={styles.colLabel} title={c.hint}>{c.label}</span>
            ))}
          </div>
          <ScrollArea orientation="vertical" fade={10} scrollbars="auto"
            className={styles.matrixScroll} label="指标矩阵">
            <div className={styles.matrixBody} style={{ gridTemplateColumns: gridCols }}>
              {rows.map(([sym, st]) => {
                const plan = planBySym.get(String(sym).toLowerCase())
                const side = plan ? (plan.side === "BUY" ? "多" : "空") : ""
                return (
                  <FragmentRow key={sym} sym={sym} st={st} side={side} />
                )
              })}
              {rows.length === 0 && (
                <div className={styles.empty}>等待指标计算（需至少 30 根K线）…</div>
              )}
            </div>
          </ScrollArea>
        </div>

        {/* ---------- 辅助视图：ATR 风险区间 ---------- */}
        <div className={styles.riskWrap}>
          <div className={styles.legendRow}>
            <span className={styles.legendLabel}>ATR 风险区间</span>
            <span className={styles.riskLegend}>
              <i data-tone="sl" />止损
              <i data-tone="tp" />止盈
            </span>
          </div>
          <div className={styles.riskList}>
            {rows.map(([sym, st]) => {
              const b = riskBand(sym, st, params)
              const total = b.slPct + b.tpPct
              const slW = total > 0 ? (b.slPct / total) * 100 : 50
              const rr = b.slPct > 0 ? b.tpPct / b.slPct : 0
              const plan = planBySym.get(String(sym).toLowerCase())
              return (
                <div key={sym} className={styles.riskRow}>
                  <span className={styles.riskSym}>{SYM(sym)}</span>
                  <span className={styles.riskBar} title={`止损 ${b.slPct.toFixed(2)}% / 止盈 ${b.tpPct.toFixed(2)}%（ATR ${fmt(st?.atr, st?.atr >= 100 ? 2 : 6)}）`}>
                    <span className={styles.riskSl} style={{ width: `${slW}%` }} />
                    <span className={styles.riskTp} style={{ width: `${100 - slW}%` }} />
                    {plan && <span className={styles.riskEntry} />}
                  </span>
                  <span className={styles.riskPct}>
                    <i data-tone="sl">-{b.slPct.toFixed(2)}</i>
                    <i data-tone="tp">+{b.tpPct.toFixed(2)}</i>
                  </span>
                  <span className={styles.riskRr} title="盈亏比（止盈距离 ÷ 止损距离）">{rr.toFixed(2)}</span>
                </div>
              )
            })}
            {rows.length === 0 && <div className={styles.empty}>—</div>}
          </div>
        </div>
      </div>

      {/* ---------- 底部：趋势 + 回撤 + 拦截计数 ---------- */}
      <div className={styles.bottom}>
        <div className={styles.trendBox}>
          <span className={styles.trendLegend}>
            <i data-tone="adx" />ADX
            <i data-tone="atr" />ATR分位
          </span>
          <TrendStrip data={histRef.current} chartColors={chartColors} />
        </div>
        <div className={styles.ddBox}>
          <span className={styles.ddLabel}>回撤</span>
          <span className={styles.ddVal} style={{ color: ddCur < -0.01 ? (chartColors?.down || "#e11d48") : "inherit" }}>
            {ddCur.toFixed(2)}%
          </span>
          <span className={styles.ddBar}>
            <span className={styles.ddFill} data-tone={dd?.paused ? "danger" : ddRatio > 0.6 ? "warn" : "ok"}
              style={{ width: `${ddRatio * 100}%` }} />
          </span>
          <span className={styles.ddLimit}>{ddLimit.toFixed(1)}%</span>
          {dd?.paused && <span className={styles.badgeDanger}>熔断中</span>}
        </div>
        <div className={styles.gateBox} title={gateLog.length ? "最近被过滤器拦下的信号" : "暂无拦截"}>
          <span className={styles.gateNum}>{gateLog.length}</span>
          <span className={styles.gateLabel}>拦截</span>
        </div>
      </div>
    </section>
  )
}

/** 热力图的一行：币种名 + 各指标格 */
function FragmentRow({ sym, st, side }) {
  const regime = st?.regime
  return (
    <>
      <span className={styles.symCell} title={`${SYM(sym)} ${st?.price != null ? fmt(st.price, st.price >= 1000 ? 1 : 4) : ""}`}>
        <b>{SYM(sym)}</b>
        {side && <i data-side={side === "多" ? "long" : "short"}>{side}</i>}
        {regime === "range" && <u title="震荡市">震</u>}
        {st?.squeeze && <u data-tone="squeeze" title="布林挤压">挤</u>}
      </span>
      {COLS.map((c) => {
        const t = c.heat(st || {})
        const txt = c.text(st || {})
        const isNA = t == null
        return (
          <span
            key={c.key}
            className={styles.cell}
            style={isNA ? undefined : { background: heatBg(t), color: heatFg(t) }}
            data-na={isNA || undefined}
            title={`${c.label}：${txt}（${c.hint}）`}
          >
            {txt}
          </span>
        )
      })}
    </>
  )
}
