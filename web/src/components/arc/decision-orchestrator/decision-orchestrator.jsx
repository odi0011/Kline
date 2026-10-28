import { useEffect, useMemo, useRef, useState } from "react"
import { motion, useReducedMotion } from "motion/react"
import { LiquidGlass } from "@/components/ui/liquid-glass"
import QuantScene, { advanceEventGate, createEventGate } from "./quant-scene"
import styles from "./decision-orchestrator.module.css"

const number = (value) => {
  if (value == null || value === "") return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}
const fixed = (value, digits = 1) => {
  const parsed = number(value)
  return parsed == null ? "--" : parsed.toFixed(digits)
}
const percent = (value, digits = 2) => {
  const parsed = number(value)
  return parsed == null ? "--" : `${parsed.toFixed(digits)}%`
}
export default function DecisionOrchestrator({
  analysis,
  snapshot,
  symbols,
  events,
  metricPulses = [],
}) {
  const reduced = useReducedMotion()
  const params = analysis?.params || {}
  const enabled = snapshot?.enabled
  const evaluations = analysis?.liveEvaluations || {}
  const activePositions = analysis?.positionPlans?.length || 0
  const computedCount = useMemo(() => {
    const requested = Array.isArray(symbols)
      ? symbols.map((symbol) => String(symbol).toLowerCase())
      : Object.keys(evaluations)
    return requested.filter((symbol) => number(evaluations[symbol]?.score) != null).length
  }, [evaluations, symbols])

  const gateRef = useRef(null)
  if (!gateRef.current) gateRef.current = createEventGate()
  const [newEvents, setNewEvents] = useState([])
  useEffect(() => {
    if (!snapshot?.ts && !events?.length && !Object.keys(analysis?.liveEvaluations || {}).length) return
    const batch = advanceEventGate(gateRef.current, { analysis, snapshot, symbols, events })
    if (batch.length) setNewEvents(batch)
  }, [analysis, snapshot, symbols, events])

  const runningLabel = enabled == null ? "待接入" : enabled ? "运行中" : "已暂停"
  const scoreGate = number(params.minScore) == null
    ? "--"
    : `≥${fixed(params.minScore, 0)} / Δ${fixed(params.minScoreGap, 0)}`
  const positionGate = number(params.maxPositions) == null
    ? "--"
    : `${fixed(params.maxPositions, 0)} / 同向 ${fixed(params.maxSameDir, 0)}`
  const drawdownLimit = number(params.ddLimitPct)
  const systemParams = [
    { label: "计算周期", value: number(params.calcIntervalSec) == null ? "--" : `${fixed(params.calcIntervalSec, 0)}s` },
    { label: "评分门槛", value: scoreGate },
    { label: "单笔风险", value: percent(params.riskPerTradePct) },
    { label: "ATR SL / TP", value: `${fixed(params.atrSlMult, 1)}× / ${fixed(params.atrTpMult, 1)}×` },
    { label: "持仓上限", value: positionGate },
    { label: "回撤熔断", value: percent(drawdownLimit == null ? null : Math.abs(drawdownLimit), 1) },
  ]

  return (
    <motion.section
      className={styles.root}
      aria-label="自动量化交易三维闭环"
      data-ready={computedCount > 0 || undefined}
      data-enabled={enabled == null ? "unknown" : String(enabled)}
      initial={reduced ? false : { opacity: 0, scale: 0.985 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: reduced ? 0 : 0.35, ease: [0.22, 1, 0.36, 1] }}
    >
      <QuantScene
        analysis={analysis}
        snapshot={snapshot}
        symbols={symbols}
        events={events}
        newEvents={newEvents}
        enabled={enabled}
        reduced={reduced}
        metricPulses={metricPulses}
      />

      <LiquidGlass
        radius={12}
        bevelDepth={18}
        scale={36}
        aberration={0}
        frost={3.2}
        tint={0.34}
        className={styles.systemGlass}
        contentClassName={styles.systemGlassInner}
        aria-label="量化机制与系统参数"
      >
        <div className={styles.paramsGrid}>
          {systemParams.map((item) => (
            <div className={styles.param} key={item.label}>
              <span>{item.label}</span>
              <b>{item.value}</b>
            </div>
          ))}
        </div>
      </LiquidGlass>

      <span className={styles.srOnly} role="status" aria-live="polite">
        {`量化引擎${runningLabel}，${computedCount} 组实时评分已计算，${activePositions} 个持仓进入反馈回路。`}
      </span>
    </motion.section>
  )
}
