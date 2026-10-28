import { useMemo } from "react"
import { motion, useReducedMotion } from "motion/react"
import { Cpu } from "lucide-react"
import { LiquidGlass } from "@/components/ui/liquid-glass"
import QuantScene from "./quant-scene"
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
const isRecent = (event, windowMs) => Boolean(
  event && Date.now() - (number(event.ts) || 0) < windowMs,
)

const findLatest = (events, kind) => {
  if (!Array.isArray(events)) return null
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index]?.kind === kind) return events[index]
  }
  return null
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

  const latestSignal = useMemo(() => findLatest(events, "signal"), [events])
  const recentOpen = useMemo(() => findLatest(events, "open"), [events])
  const recentSkip = useMemo(() => findLatest(events, "skip"), [events])
  const signalActive = isRecent(latestSignal, 5000)
  const riskActive = Boolean(analysis?.drawdown?.paused) || isRecent(recentSkip, 5000)
  const executionActive = isRecent(recentOpen, 5000)
  const coreActive = metricPulses.length > 0 || signalActive
  const activeStages = {
    intake: metricPulses.length > 0,
    features: computedCount > 0,
    decision: coreActive,
    risk: riskActive,
    execution: executionActive,
    feedback: activePositions > 0,
  }

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
        activeStages={activeStages}
        signalActive={signalActive}
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
        <header className={styles.systemHead}>
          <span className={styles.systemTitle}><Cpu size={13} strokeWidth={1.8} />量化引擎</span>
          <span className={styles.runningState} data-state={enabled == null ? "pending" : enabled ? "running" : "paused"}>
            <i />{runningLabel}
          </span>
        </header>
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
