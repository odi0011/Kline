/* 同一套量化引擎的全局计算流：币种汇入核心，六个因子节点协同计算，
   最后进入统一风控与执行，再回流下一轮巡检。画面不承载交易结果。 */
import { useId, useMemo } from "react"
import { motion, useReducedMotion } from "motion/react"
import {
  Activity,
  Network,
  Newspaper,
  Scale,
  ShieldCheck,
  TrendingUp,
  Waves,
  Zap,
} from "lucide-react"
import CoinAvatar from "@/components/ui/coin-avatar"
import styles from "./decision-orchestrator.module.css"

const clamp = (value, low = 0, high = 100) => Math.min(high, Math.max(low, value))
const number = (value, fallback = null) => {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}
const avg = (values) => {
  const valid = values.map((value) => number(value)).filter((value) => value != null)
  return valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : null
}
const toneForScore = (score) => {
  if (score == null) return "idle"
  if (score >= 55) return "good"
  if (score >= 45) return "mid"
  return "weak"
}

const CORE = { x: 185, y: 175 }
const COLLECTOR = { x: 329, y: 175 }
const OUTPUT = {
  risk: { x: 356, y: 137 },
  execution: { x: 356, y: 213 },
}
const EXPERT_LAYOUT = [
  { id: "trend", label: "趋势", x: 88, y: 119, Icon: TrendingUp, tone: "var(--color-sky-500)" },
  { id: "momentum", label: "动量", x: 185, y: 58, Icon: Activity, tone: "var(--color-violet-500)" },
  { id: "flow", label: "量价", x: 282, y: 119, Icon: Waves, tone: "var(--color-teal-500)" },
  { id: "structure", label: "结构", x: 282, y: 231, Icon: Network, tone: "var(--color-amber-500)" },
  { id: "derivatives", label: "衍生", x: 185, y: 292, Icon: Scale, tone: "var(--color-blue-500)" },
  { id: "information", label: "信息", x: 88, y: 231, Icon: Newspaper, tone: "var(--color-pink-500)" },
]

const pos = (point) => ({
  left: `${(point.x / 400) * 100}%`,
  top: `${(point.y / 350) * 100}%`,
})

const computePath = (target) =>
  `M ${CORE.x} ${CORE.y} C ${CORE.x + (target.x - CORE.x) * 0.34} ${CORE.y} ` +
  `${CORE.x + (target.x - CORE.x) * 0.72} ${target.y} ${target.x} ${target.y}`

const collectPath = (source) =>
  `M ${source.x} ${source.y} C ${source.x + (COLLECTOR.x - source.x) * 0.36} ${source.y} ` +
  `${COLLECTOR.x} ${COLLECTOR.y + (source.y - COLLECTOR.y) * 0.24} ${COLLECTOR.x} ${COLLECTOR.y}`

function FlowParticle({ path, delay, duration, tone = "accent" }) {
  return (
    <g className={styles.particle} data-tone={tone}>
      <circle r="3.2" className={styles.particleHalo} />
      <circle r="1.25" className={styles.particleCore} />
      <animateMotion path={path} dur={`${duration}s`} begin={`${delay}s`} repeatCount="indefinite" />
    </g>
  )
}

export default function DecisionOrchestrator({ analysis, symbols }) {
  const traces = analysis?.decisionTraces || []
  const latestBySymbol = analysis?.latestTraces || {}
  const liveFeatures = analysis?.liveFeatures || {}
  const liveEvaluations = analysis?.liveEvaluations || {}
  const symbolStates = analysis?.symbols || {}
  const gateLog = analysis?.gateLog || []
  const plans = analysis?.positionPlans || []
  const reduced = useReducedMotion()
  const rawId = useId()
  const uid = rawId.replace(/[^a-zA-Z0-9_-]/g, "")
  const ordered = useMemo(
    () => (Array.isArray(symbols) ? symbols.map((symbol) => String(symbol).toLowerCase()) : []),
    [symbols],
  )

  const lanes = useMemo(() => {
    const lastTrace = new Map()
    const lastGate = new Map()
    for (const item of traces) {
      if (item?.symbol) lastTrace.set(item.symbol, item)
    }
    for (const item of gateLog) {
      if (item?.symbol) lastGate.set(item.symbol, item)
    }
    return ordered.map((symbol) => {
      const trace = latestBySymbol[symbol] || lastTrace.get(symbol) || null
      const gate = lastGate.get(symbol) || null
      const liveEval = liveEvaluations[symbol] || null
      const features = trace?.factors || liveFeatures[symbol] || {}
      const scoreData = trace?.score || gate || liveEval || {}
      const direction = trace?.direction || gate?.direction || liveEval?.direction || null
      const sideKey = direction === "bearish" ? "bear" : "bull"
      const breakdown = scoreData?.breakdown?.[sideKey] || {}
      const oi = features.oi || {}
      const oiBias = oi.bias || null
      const derivativeScore = oiBias == null
        ? null
        : oiBias === "neutral"
          ? 50
          : clamp(50 + (oiBias === direction ? 1 : -1) * number(oi.strength, 0) * 44)
      const sentiment = features.sentiment || {}
      const sentimentScore = number(sentiment.score) == null
        ? null
        : clamp(50 + number(sentiment.score) * number(sentiment.confidence, 0) * 45)
      return {
        symbol,
        trace,
        features,
        breakdown,
        score: number(scoreData?.score),
        state: symbolStates[symbol] || null,
        derivativeScore,
        sentimentScore,
      }
    })
  }, [ordered, traces, gateLog, latestBySymbol, liveEvaluations, liveFeatures, symbolStates])

  const nodeScores = {
    trend: avg(lanes.map((lane) => lane.breakdown.trend)),
    momentum: avg(lanes.map((lane) => lane.breakdown.momentum)),
    flow: avg(lanes.map((lane) => lane.breakdown.volume)),
    structure: avg(lanes.map((lane) => lane.breakdown.structure)),
    derivatives: avg(lanes.map((lane) => lane.derivativeScore)),
    information: avg(lanes.map((lane) => lane.sentimentScore)),
  }
  const expertNodes = EXPERT_LAYOUT.map((node) => ({ ...node, score: nodeScores[node.id] }))
  const overallScore = avg(lanes.map((lane) => lane.score))
  const scoredCount = lanes.filter((lane) => lane.score != null).length
  const riskCount = lanes.filter((lane) => lane.trace?.risk).length + plans.length
  const executionCount = lanes.filter((lane) => lane.trace?.status === "opened").length

  const coinPoints = ordered.map((symbol, index, all) => {
    const y = all.length <= 1 ? CORE.y : 62 + (index / (all.length - 1)) * 226
    return {
      symbol,
      y,
      score: lanes.find((lane) => lane.symbol === symbol)?.score,
      path: `M 18 ${y} C 58 ${y} 116 ${CORE.y + (y - CORE.y) * 0.18} 162 ${CORE.y}`,
    }
  })
  const expertPaths = expertNodes.map((node) => ({ node, path: computePath(node) }))
  const collectorPaths = expertNodes.map((node) => ({ node, path: collectPath(node) }))
  const outputPaths = [
    { key: "risk", path: `M ${COLLECTOR.x} ${COLLECTOR.y} C 338 ${COLLECTOR.y} 342 156 ${OUTPUT.risk.x} ${OUTPUT.risk.y}` },
    { key: "execution", path: `M ${COLLECTOR.x} ${COLLECTOR.y} C 338 ${COLLECTOR.y} 342 194 ${OUTPUT.execution.x} ${OUTPUT.execution.y}` },
  ]
  const loopPath = "M 356 230 C 356 296 312 330 185 330 C 58 330 14 294 14 253 C 14 211 15 180 18 150"

  return (
    <motion.section
      className={styles.root}
      aria-label="量化计算流动"
      data-ready={scoredCount > 0 || undefined}
      initial={reduced ? false : { opacity: 0, scale: 0.985 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: reduced ? 0 : 0.35, ease: [0.22, 1, 0.36, 1] }}
    >
      <svg className={styles.network} viewBox="0 0 400 350" preserveAspectRatio="none" aria-hidden>
        <defs>
          <linearGradient id={`source-${uid}`} x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stopColor="var(--accent)" stopOpacity="0.04" />
            <stop offset="0.75" stopColor="var(--accent)" stopOpacity="0.3" />
            <stop offset="1" stopColor="var(--accent)" stopOpacity="0.7" />
          </linearGradient>
          <linearGradient id={`collect-${uid}`} x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stopColor="var(--color-emerald-500)" stopOpacity="0.08" />
            <stop offset="1" stopColor="var(--color-emerald-500)" stopOpacity="0.55" />
          </linearGradient>
        </defs>

        <path className={styles.outerOrbit}
          d="M 356 230 C 356 296 312 330 185 330 C 58 330 14 294 14 253 C 14 211 15 180 18 150" />
        <path className={styles.hexOrbit}
          d="M 88 119 L 185 58 L 282 119 L 282 231 L 185 292 L 88 231 Z" />

        <g className={styles.beams}>
          {coinPoints.map((coin) => (
            <path key={`source-line-${coin.symbol}`} className={styles.sourceBeam}
              d={coin.path} stroke={`url(#source-${uid})`} />
          ))}
          {expertPaths.map(({ node, path }) => (
            <path key={`compute-line-${node.id}`} className={styles.computeBeam} d={path}
              style={{ "--beam-tone": node.tone }} />
          ))}
          {collectorPaths.map(({ node, path }) => (
            <path key={`collect-line-${node.id}`} className={styles.collectBeam} d={path}
              stroke={`url(#collect-${uid})`} />
          ))}
          {outputPaths.map(({ key, path }) => (
            <path key={`output-line-${key}`} className={styles.outputBeam} d={path} />
          ))}
          <path className={styles.loopBeam} d={loopPath} />
        </g>

        {!reduced && (
          <g>
            {coinPoints.map((coin, index) => (
              <FlowParticle key={`source-flow-${coin.symbol}`} path={coin.path}
                delay={0.1 + index * 0.21} duration={2.8 + (index % 3) * 0.34} />
            ))}
            {expertPaths.map(({ node, path }, index) => (
              <FlowParticle key={`compute-flow-${node.id}`} path={path}
                delay={index * 0.34} duration={3.1 + (index % 2) * 0.4} tone="node" />
            ))}
            {collectorPaths.map(({ node, path }, index) => (
              <FlowParticle key={`collect-flow-${node.id}`} path={path}
                delay={0.45 + index * 0.29} duration={2.6 + (index % 3) * 0.28} tone="collect" />
            ))}
            {outputPaths.map(({ key, path }, index) => (
              <FlowParticle key={`output-flow-${key}`} path={path}
                delay={0.2 + index * 0.7} duration={2.2} tone="output" />
            ))}
            <FlowParticle path={loopPath} delay={0.35} duration={6.4} tone="loop" />
            <FlowParticle path={loopPath} delay={3.55} duration={6.4} tone="loop" />
          </g>
        )}
      </svg>

      <div className={styles.coinRail} aria-hidden>
        {coinPoints.map((coin, index) => (
          <span key={coin.symbol} className={styles.coinToken}
            style={{ top: `${(coin.y / 350) * 100}%`, "--coin-delay": `${index * -0.37}s` }}
            aria-label={coin.symbol}>
            <CoinAvatar symbol={coin.symbol} size={15} />
            <i data-tone={toneForScore(coin.score)} />
          </span>
        ))}
      </div>

      <div className={styles.core} style={pos(CORE)} role="img"
        aria-label={overallScore == null ? "综合计算中" : `综合 ${Math.round(overallScore)}`}>
        <span className={styles.coreGlow} />
        <span className={styles.coreOrbit} />
        <span className={styles.coreOrbitInner} />
        <span className={styles.corePulse} />
        <span className={styles.corePulseLate} />
        <span className={styles.coreDisc}>
          <b>{overallScore == null ? "·" : Math.round(overallScore)}</b>
        </span>
      </div>

      {expertNodes.map((node, index) => (
        <div key={node.id} className={styles.expertNode} style={{
          ...pos(node),
          "--node-tone": node.tone,
          "--node-ring": node.score == null ? "var(--border-strong)" : "var(--accent)",
          "--node-score": `${node.score == null ? 0 : clamp(node.score)}%`,
          "--node-delay": `${index * -0.52}s`,
        }} data-tone={toneForScore(node.score)} role="img"
        aria-label={`${node.label} ${node.score == null ? "计算中" : Math.round(node.score)}`}>
          <span className={styles.nodeHalo} />
          <span className={styles.nodeRing}>
            <span className={styles.nodeDisc}><node.Icon size={13} strokeWidth={1.8} /></span>
          </span>
          {node.score != null && <b className={styles.nodeScore}>{Math.round(node.score)}</b>}
        </div>
      ))}

      <span className={styles.collector} style={pos(COLLECTOR)} aria-hidden>
        <i />
      </span>

      <div className={styles.outputNode} data-active={riskCount > 0 || undefined}
        style={{ ...pos(OUTPUT.risk), "--output-tone": "var(--color-rose-500)" }}
        role="img" aria-label="统一风控">
        <span className={styles.outputHalo} />
        <span className={styles.outputDisc}><ShieldCheck size={13} strokeWidth={1.8} /></span>
      </div>
      <div className={styles.outputNode} data-active={executionCount > 0 || undefined}
        style={{ ...pos(OUTPUT.execution), "--output-tone": "var(--color-blue-500)" }}
        role="img" aria-label="执行">
        <span className={styles.outputHalo} />
        <span className={styles.outputDisc}><Zap size={13} strokeWidth={1.8} /></span>
      </div>

      <span className={styles.srOnly} role="status" aria-live="polite">
        {`${ordered.length} 个币种共享一套计算链，已完成 ${scoredCount} 个币种评分。`}
      </span>
    </motion.section>
  )
}
