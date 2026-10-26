/* 策略分析面板：量化决策的完整依据与过程可视化（第三行剩余空间）。
   不显示"结果"（结果在左边流水里），这里展示"为什么这样决策"：

   1. 市场状态  —— 每个监控币的 regime / 波动档位 / 挤压状态（颜色标签）
   2. 技术指标  —— RSI 刻度条、Z-score 偏离度、布林带 %B、ADX 趋势强度、
                    ATR 历史分位、实测波动率
   3. 账户风控  —— 回撤进度条 + 熔断状态
   4. 过滤器    —— 最近被拦下的信号及具体原因
   5. 策略参数  —— 当前生效的全部阈值（可追溯到具体数值）

   数据全部来自后端 quant.snapshot().analysis，无前端臆造。 */
import { useMemo, useState } from "react"
import { Activity, Gauge, ShieldAlert, SlidersHorizontal, Filter } from "lucide-react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import styles from "./strategy-panel.module.css"

const REGIME_LABEL = {
  trend: { text: "趋势", tone: "trend" },
  range: { text: "震荡", tone: "range" },
  mixed: { text: "过渡", tone: "mixed" },
  unknown: { text: "数据不足", tone: "muted" },
}
const VOL_LABEL = {
  high: { text: "高波动", tone: "danger" },
  normal: { text: "常波", tone: "ok" },
  low: { text: "低波动", tone: "muted" },
  unknown: { text: "—", tone: "muted" },
}

const fmt = (v, d = 2) => {
  if (v == null || Number.isNaN(Number(v))) return "--"
  const n = Number(v)
  const a = Math.abs(n)
  if (a >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: d })
  return n.toFixed(d)
}

/** 水平刻度条：value 在 [min,max] 内的位置，可标出危险区间 */
function Meter({ value, min, max, tone = "neutral", label, marks = [] }) {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100))
  return (
    <div className={styles.meterRow}>
      <span className={styles.meterLabel}>{label}</span>
      <div className={styles.meterTrack}>
        {marks.map((m) => (
          <span key={m.at} className={styles.meterMark}
            style={{ left: `${((m.at - min) / (max - min)) * 100}%` }} title={m.label} />
        ))}
        <span className={styles.meterFill} data-tone={tone} style={{ width: `${pct}%` }} />
        <span className={styles.meterKnob} data-tone={tone} style={{ left: `${pct}%` }} />
      </div>
      <span className={styles.meterVal}>{value == null ? "--" : fmt(value, 1)}</span>
    </div>
  )
}

/** 单个币种的指标卡 */
function SymbolCard({ sym, st }) {
  const regime = REGIME_LABEL[st.regime] || REGIME_LABEL.unknown
  const vol = VOL_LABEL[st.volState] || VOL_LABEL.unknown
  const rsi = st.rsi
  const z = st.z
  const pctB = st.boll?.pctB
  const adx = st.adx
  const atrPct = st.atrPct

  const rsiTone = rsi == null ? "neutral" : rsi >= 70 ? "danger" : rsi <= 30 ? "warn" : "ok"
  const zTone = z == null ? "neutral" : Math.abs(z) >= 2 ? "danger" : Math.abs(z) >= 1.2 ? "warn" : "ok"
  const adxTone = adx == null ? "neutral" : adx >= 25 ? "ok" : adx <= 20 ? "muted" : "warn"

  return (
    <div className={styles.symCard}>
      <div className={styles.symHead}>
        <span className={styles.symName}>{sym.replace(/usdt$/i, "").toUpperCase()}</span>
        <span className={styles.tag} data-tone={regime.tone}>{regime.text}</span>
        <span className={styles.tag} data-tone={vol.tone}>{vol.text}</span>
        {st.squeeze && <span className={styles.tag} data-tone="warn">挤压</span>}
        <span className={styles.symPrice}>{fmt(st.price, st.price >= 1000 ? 1 : 4)}</span>
      </div>

      {/* RSI：30/70 两个阈值刻度 */}
      <Meter value={rsi} min={0} max={100} tone={rsiTone}
        label="RSI"
        marks={[{ at: 30, label: "超卖 30" }, { at: 50, label: "中轴 50" }, { at: 70, label: "超买 70" }]} />

      {/* Z-score：-2~+2 之外是极端偏离 */}
      <Meter value={z} min={-3} max={3} tone={zTone}
        label="Z值"
        marks={[{ at: -2, label: "-2σ" }, { at: 0, label: "均值" }, { at: 2, label: "+2σ" }]} />

      {/* 布林 %B：0=下轨 1=上轨 */}
      <Meter value={pctB} min={0} max={1.2} tone={pctB == null ? "neutral" : pctB > 1 ? "danger" : pctB < 0 ? "danger" : "ok"}
        label="%B"
        marks={[{ at: 0, label: "下轨" }, { at: 0.5, label: "中轨" }, { at: 1, label: "上轨" }]} />

      {/* ADX：20/25 分界 */}
      <Meter value={adx} min={0} max={60} tone={adxTone}
        label="ADX"
        marks={[{ at: 20, label: "震荡 20" }, { at: 25, label: "趋势 25" }]} />

      {/* ATR 历史分位 */}
      <Meter value={atrPct} min={0} max={100} tone={atrPct == null ? "neutral" : atrPct >= 85 ? "danger" : atrPct <= 15 ? "muted" : "ok"}
        label="ATR分位"
        marks={[{ at: 15, label: "低" }, { at: 85, label: "高" }]} />

      <div className={styles.symFoot}>
        <span>ATR {fmt(st.atr, st.atr >= 100 ? 2 : 6)}</span>
        <span>带宽 {st.boll?.bandwidth != null ? (st.boll.bandwidth * 100).toFixed(2) + "%" : "--"}</span>
        <span>实测波动 {st.realizedVol != null ? (st.realizedVol * 100).toFixed(3) + "%" : "--"}</span>
        <span>+DI {fmt(st.plusDI, 0)} / -DI {fmt(st.minusDI, 0)}</span>
      </div>
    </div>
  )
}

/** 风控区：回撤进度 + 熔断 */
function RiskBlock({ dd, chartColors }) {
  if (!dd) return null
  const cur = Number(dd.current) || 0
  const limit = Number(dd.limit) || -5
  // 用"回撤深度 / 熔断线"作为进度（0 → 未回撤，1 → 触及熔断）
  const ratio = limit !== 0 ? Math.min(1, Math.abs(cur) / Math.abs(limit)) : 0
  const tone = dd.paused ? "danger" : ratio > 0.6 ? "warn" : "ok"
  return (
    <div className={styles.block}>
      <div className={styles.blockHead}>
        <ShieldAlert size={11} />
        账户风控
        {dd.paused && <span className={styles.tag} data-tone="danger">熔断中</span>}
      </div>
      <div className={styles.ddRow}>
        <span className={styles.ddLabel}>当前回撤</span>
        <span className={styles.ddVal} style={{ color: cur < -0.01 ? chartColors.down : "inherit" }}>
          {cur.toFixed(2)}%
        </span>
        <span className={styles.ddSep}>/</span>
        <span className={styles.ddLabel}>熔断线</span>
        <span className={styles.ddVal}>{limit.toFixed(1)}%</span>
        <span className={styles.ddSpacer} />
        <span className={styles.ddLabel}>历史最大</span>
        <span className={styles.ddVal} style={{ color: (dd.max || 0) < -0.01 ? chartColors.down : "inherit" }}>
          {Number(dd.max || 0).toFixed(2)}%
        </span>
        <span className={styles.ddLabel}>峰值</span>
        <span className={styles.ddVal}>{fmt(dd.peak, 2)}</span>
      </div>
      <div className={styles.ddTrack}>
        <span className={styles.ddFill} data-tone={tone} style={{ width: `${ratio * 100}%` }} />
      </div>
    </div>
  )
}

/** 过滤器拦截记录 */
function GateBlock({ log }) {
  return (
    <div className={styles.block}>
      <div className={styles.blockHead}>
        <Filter size={11} />
        信号过滤器
        <span className={styles.blockHint}>最近 {log.length} 条拦截</span>
      </div>
      <div className={styles.gateList}>
        {log.length === 0 && <span className={styles.muted}>暂无拦截记录</span>}
        {log.slice().reverse().map((g, i) => (
          <div key={`${g.ts}-${i}`} className={styles.gateRow}>
            <span className={styles.gateTime}>
              {new Date(g.ts + 8 * 3600e3).toISOString().slice(11, 19)}
            </span>
            <span className={styles.gateSym}>{String(g.symbol || "").replace(/usdt$/i, "").toUpperCase()}</span>
            <span className={styles.gateDir} data-dir={g.direction}>
              {g.direction === "bullish" ? "多" : "空"}
            </span>
            <span className={styles.gateWhy} title={g.reason}>{g.reason}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** 策略参数表 */
function ParamsBlock({ p }) {
  if (!p) return null
  const rows = [
    ["止损 = ATR ×", `${p.atrSlMult}（夹在 ${p.slMinPct}% ~ ${p.slMaxPct}%）`],
    ["止盈 = ATR ×", `${p.atrTpMult}（夹在 ${p.tpMinPct}% ~ ${p.tpMaxPct}%）`],
    ["回撤熔断", `${p.ddLimitPct}% / 暂停 ${p.ddPauseMin} 分钟`],
    ["持仓上限", `${p.maxPositions} 仓（同向 ${p.maxSameDir}）`],
    ["冷却 / 超时", `${p.cooldownSec}s / ${p.maxHoldMin}min`],
    ["移动止盈", `浮盈 ≥ ${p.trailArm}% 后回撤 ${p.trailGap}% 平`],
    ["单仓保证金", `可用余额 ${p.marginPct}%`],
    ["RSI 阈值", `超买 ${p.rsiOverbought} / 超卖 ${p.rsiOversold}`],
    ["Z 值极端", `|z| ≥ ${p.zExtreme} 不顺势`],
    ["ADX 分界", `趋势 ≥ ${p.adxTrend} / 震荡 ≤ ${p.adxRange}`],
    ["布林挤压", `带宽分位 ≤ ${p.squeezePct}%`],
  ]
  return (
    <div className={styles.block}>
      <div className={styles.blockHead}>
        <SlidersHorizontal size={11} />
        当前策略参数
      </div>
      <div className={styles.paramGrid}>
        {rows.map(([k, v]) => (
          <div key={k} className={styles.paramRow}>
            <span className={styles.paramKey}>{k}</span>
            <span className={styles.paramVal}>{v}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function StrategyPanel({ analysis, chartColors }) {
  const [tab, setTab] = useState("symbols")
  const syms = analysis?.symbols || {}
  const ents = useMemo(() => Object.entries(syms), [syms])
  const dd = analysis?.drawdown
  const gateLog = analysis?.gateLog || []
  const params = analysis?.params
  const plans = analysis?.positionPlans || []

  return (
    <section className={styles.wrap} aria-label="策略分析">
      <header className={styles.head}>
        <span className={styles.title}>
          <Activity size={11} /> 策略分析
        </span>
        <div className={styles.tabs}>
          <button type="button" className={styles.tab} data-on={tab === "symbols" || undefined}
            onClick={() => setTab("symbols")}>
            <Gauge size={10} /> 指标 {ents.length}
          </button>
          <button type="button" className={styles.tab} data-on={tab === "risk" || undefined}
            onClick={() => setTab("risk")}>
            <ShieldAlert size={10} /> 风控
          </button>
          <button type="button" className={styles.tab} data-on={tab === "params" || undefined}
            onClick={() => setTab("params")}>
            <SlidersHorizontal size={10} /> 参数
          </button>
        </div>
      </header>

      <ScrollArea orientation="vertical" fade={16} scrollbars="auto"
        className={styles.body} label="策略分析内容">
        {tab === "symbols" && (
          <>
            {plans.length > 0 && (
              <div className={styles.block}>
                <div className={styles.blockHead}>
                  <Activity size={11} />
                  当前持仓的开仓依据
                </div>
                <div className={styles.planList}>
                  {plans.map((p, i) => (
                    <div key={`${p.symbol}-${i}`} className={styles.planRow}>
                      <span className={styles.planSym}>
                        {String(p.symbol || "").replace(/usdt$/i, "").toUpperCase()}
                      </span>
                      <span className={styles.planSide} data-side={p.side === "BUY" ? "long" : "short"}>
                        {p.side === "BUY" ? "多" : "空"}
                      </span>
                      <span className={styles.planKV}>入场 {fmt(p.entry, p.entry >= 1000 ? 1 : 4)}</span>
                      <span className={styles.planKV} data-tone="down">止损 {p.slPct}%</span>
                      <span className={styles.planKV} data-tone="up">止盈 {p.tpPct}%</span>
                      <span className={styles.planKV}>
                        ATR {fmt(p.atrAtEntry, p.atrAtEntry >= 100 ? 2 : 6)}
                      </span>
                      <span className={styles.planKV}>
                        开仓时 {REGIME_LABEL[p.regimeAtEntry]?.text || "—"}
                        {p.rsiAtEntry != null ? ` / RSI ${Math.round(p.rsiAtEntry)}` : ""}
                      </span>
                      <span className={styles.planPattern}>{p.name_zh || p.pattern || ""}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {ents.length === 0 && (
              <div className={styles.muted}>等待指标计算（需至少 30 根K线）…</div>
            )}
            <div className={styles.symGrid}>
              {ents.map(([sym, st]) => (
                <SymbolCard key={sym} sym={sym} st={st} />
              ))}
            </div>
          </>
        )}

        {tab === "risk" && (
          <>
            <RiskBlock dd={dd} chartColors={chartColors} />
            <GateBlock log={gateLog} />
          </>
        )}

        {tab === "params" && <ParamsBlock p={params} />}
      </ScrollArea>
    </section>
  )
}
