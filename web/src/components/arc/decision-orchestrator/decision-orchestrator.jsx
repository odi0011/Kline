/* 量化计算编排水位：数据总线 → 两层交叉计算 → 路由 → 双侧专家并联 →
   汇合 → 风控/执行。只呈现计算过程，交易结果仍进入决策流水。 */
import { useEffect, useId, useMemo, useState } from "react"
import { motion, useReducedMotion } from "motion/react"
import {
  Activity,
  Database,
  Network,
  Newspaper,
  Scale,
  ShieldCheck,
  TrendingUp,
  Waves,
  Zap,
} from "lucide-react"
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
const fixed = (value, digits = 1) => number(value) == null ? "--" : number(value).toFixed(digits)
const signedPct = (value, digits = 2) => {
  const n = number(value)
  return n == null ? "--" : `${n >= 0 ? "+" : ""}${n.toFixed(digits)}%`
}
const compactPrice = (value) => {
  const n = number(value)
  if (n == null) return "--"
  const abs = Math.abs(n)
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}m`
  if (abs >= 1000) return `${(n / 1000).toFixed(1)}k`
  if (abs >= 1) return n.toFixed(2)
  return n.toPrecision(3)
}
const priceText = (value) => {
  const n = number(value)
  if (n == null) return "--"
  return n >= 1000
    ? n.toLocaleString("en-US", { maximumFractionDigits: 1 })
    : n >= 1 ? n.toFixed(3) : n.toPrecision(4)
}
const toneForScore = (score) => {
  if (score == null) return "idle"
  if (score >= 55) return "good"
  if (score >= 45) return "mid"
  return "weak"
}

const BUS = { x: 200, y: 30 }
const HIDDEN_A = [82, 160, 240, 318].map((x, index) => ({ x, y: index % 2 ? 69 : 61 }))
const HIDDEN_B = [66, 151, 249, 334].map((x, index) => ({ x, y: index % 2 ? 103 : 95 }))
const ROUTER = { left: 178, right: 222, top: 112, bottom: 274 }
const ROW_Y = [130, 157, 184, 211, 238, 265]
const EXPERT_X = [78, 78, 78, 322, 322, 322]
const MERGE = { x: 200, y: 294 }
const OUTPUT = {
  risk: { x: 162, y: 327 },
  execution: { x: 238, y: 327 },
}
const EXPERTS = [
  { id: "trend", label: "趋势", Icon: TrendingUp, tone: "var(--color-sky-500)" },
  { id: "momentum", label: "动量", Icon: Activity, tone: "var(--color-violet-500)" },
  { id: "flow", label: "量价", Icon: Waves, tone: "var(--color-teal-500)" },
  { id: "structure", label: "结构", Icon: Network, tone: "var(--color-amber-500)" },
  { id: "derivatives", label: "衍生", Icon: Scale, tone: "var(--color-blue-500)" },
  { id: "information", label: "信息", Icon: Newspaper, tone: "var(--color-pink-500)" },
]

const pos = (point) => ({
  left: `${(point.x / 400) * 100}%`,
  top: `${(point.y / 350) * 100}%`,
})

const edgePath = (from, to, bend = 0) => {
  const dx = to.x - from.x
  const dy = to.y - from.y
  return `M ${from.x} ${from.y} C ${from.x + dx * 0.35} ${from.y + bend} ` +
    `${to.x - dx * 0.28} ${to.y - bend * 0.6} ${to.x} ${to.y}`
}

function FlowParticle({ path, delay, duration, tone = "accent" }) {
  return (
    <g className={styles.particle} data-tone={tone} opacity="0">
      <circle r="2.6" className={styles.particleHalo} />
      <circle r="1" className={styles.particleCore} />
      <animateMotion path={path} dur={`${duration}s`} begin={`${delay}s`} repeatCount="indefinite" />
      <animate attributeName="opacity" values="0;0.94;0.94;0" keyTimes="0;0.12;0.82;1"
        dur={`${duration}s`} begin={`${delay}s`} repeatCount="indefinite" />
    </g>
  )
}

function DataPacket({ path, text, delay, duration, midpoint, reduced }) {
  const width = Math.max(24, 8 + [...text].reduce(
    (sum, char) => sum + (/[\u3400-\u9fff]/.test(char) ? 6.2 : 3.6),
    0,
  ))
  const body = (
    <>
      <rect x={-width / 2} y="-6" width={width} height="12" rx="6" className={styles.packetBg} />
      <text x="0" y="0.5" textAnchor="middle" dominantBaseline="middle"
        className={styles.packetText}>{text}</text>
    </>
  )
  if (reduced) {
    return <g className={styles.dataPacket}
      transform={`translate(${midpoint.x} ${midpoint.y})`}>{body}</g>
  }
  return (
    <g className={styles.dataPacket} opacity="0">
      {body}
      <animateMotion path={path} dur={`${duration}s`} begin={`${delay}s`} repeatCount="indefinite" />
      <animate attributeName="opacity" values="0;1;1;0" keyTimes="0;0.12;0.82;1"
        dur={`${duration}s`} begin={`${delay}s`} repeatCount="indefinite" />
    </g>
  )
}

export default function DecisionOrchestrator({ analysis, symbols, events, structures, metricPulses = [] }) {
  const traces = analysis?.decisionTraces || []
  const latestBySymbol = analysis?.latestTraces || {}
  const liveFeatures = analysis?.liveFeatures || {}
  const liveEvaluations = analysis?.liveEvaluations || {}
  const symbolStates = analysis?.symbols || {}
  const gateLog = analysis?.gateLog || []
  const plans = analysis?.positionPlans || []
  const reduced = useReducedMotion()
  const [scanIndex, setScanIndex] = useState(0)
  const rawId = useId()
  const uid = rawId.replace(/[^a-zA-Z0-9_-]/g, "")
  const ordered = useMemo(
    () => (Array.isArray(symbols) ? symbols.map((symbol) => String(symbol).toLowerCase()) : []),
    [symbols],
  )
  const latestSignal = useMemo(() => {
    if (!Array.isArray(events)) return null
    for (let index = events.length - 1; index >= 0; index -= 1) {
      if (events[index]?.kind === "signal") return events[index]
    }
    return null
  }, [events])

  useEffect(() => {
    if (ordered.length < 2) return
    const timer = setInterval(() => setScanIndex((value) => value + 1), 4200)
    return () => clearInterval(timer)
  }, [ordered.length])

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

  const signalAge = latestSignal ? Date.now() - (number(latestSignal.ts, 0) || 0) : Infinity
  const recentSignal = signalAge < 12000 ? latestSignal : null
  const signalActive = signalAge < 5000 ? latestSignal : null
  const focusSymbol = recentSignal && ordered.includes(recentSignal.symbol)
    ? recentSignal.symbol
    : ordered[scanIndex % Math.max(1, ordered.length)] || ""
  const focusLane = lanes.find((lane) => lane.symbol === focusSymbol) || lanes[0] || null
  const focusState = focusLane?.state || {}
  const focusFeatures = focusLane?.features || {}
  const focusBoll = focusState.boll || {}
  const focusOI = focusFeatures.oi || {}
  const focusFunding = focusFeatures.funding || {}
  const focusSentiment = focusFeatures.sentiment || {}
  const focusHMM = focusFeatures.hmm || {}
  const focusPrice = number(focusLane?.trace?.price) ?? number(focusState.price)
  const levels = (() => {
    const current = focusPrice
    let support = null
    let resistance = null
    for (const level of structures?.[focusSymbol]?.levels || []) {
      const value = number(level?.price)
      if (value == null) continue
      if (level.kind === "support" && (current == null || value <= current)) {
        if (support == null || value > support) support = value
      }
      if (level.kind === "resistance" && (current == null || value >= current)) {
        if (resistance == null || value < resistance) resistance = value
      }
    }
    return {
      support: support ?? number(focusBoll.lower),
      resistance: resistance ?? number(focusBoll.upper),
    }
  })()

  const scoreById = {
    trend: avg(lanes.map((lane) => lane.breakdown.trend)),
    momentum: avg(lanes.map((lane) => lane.breakdown.momentum)),
    flow: avg(lanes.map((lane) => lane.breakdown.volume)),
    structure: avg(lanes.map((lane) => lane.breakdown.structure)),
    derivatives: avg(lanes.map((lane) => lane.derivativeScore)),
    information: avg(lanes.map((lane) => lane.sentimentScore)),
  }
  const experts = EXPERTS.map((expert) => ({ ...expert, score: scoreById[expert.id] }))
  const scoredCount = lanes.filter((lane) => lane.score != null).length
  const riskCount = lanes.filter((lane) => lane.trace?.risk).length + plans.length
  const executionCount = lanes.filter((lane) => lane.trace?.status === "opened").length

  const busToA = HIDDEN_A.map((node, index) => ({
    id: `bus-a-${index}`,
    path: edgePath(BUS, node, (index - 1.5) * 4),
  }))
  const aToB = []
  for (const [fromIndex, from] of HIDDEN_A.entries()) {
    for (const [toIndex, to] of HIDDEN_B.entries()) {
      aToB.push({
        id: `a-b-${fromIndex}-${toIndex}`,
        path: edgePath(from, to, ((fromIndex + toIndex) % 5 - 2) * 5),
      })
    }
  }
  const routerFeeds = ROW_Y.map((y, index) => {
    const from = HIDDEN_B[index % HIDDEN_B.length]
    const to = { x: ROUTER.left, y }
    return {
      id: `router-${experts[index].id}`,
      from,
      to,
      y,
      path: edgePath(from, to, ((index % 3) - 1) * 7),
    }
  })
  const routerToExpert = ROW_Y.map((y, index) => ({
    id: `expert-${experts[index].id}`,
    path: index < 3
      ? `M ${ROUTER.left} ${y} C 155 ${y} 132 ${y} 122 ${y}`
      : `M ${ROUTER.right} ${y} C 245 ${y} 268 ${y} 278 ${y}`,
  }))
  const expertToMerge = ROW_Y.map((y, index) => ({
    id: `merge-${experts[index].id}`,
    path: index < 3
      ? `M 34 ${y + 10} C 34 ${y + 35} 70 ${MERGE.y + 28} 200 ${MERGE.y}`
      : `M 366 ${y + 10} C 366 ${y + 35} 330 ${MERGE.y + 28} 200 ${MERGE.y}`,
  }))
  const outputPaths = [
    { id: "risk", path: `M 200 294 C 188 304 174 309 162 327` },
    { id: "execution", path: `M 200 294 C 212 304 226 309 238 327` },
  ]
  const feedbackPath = `M 238 341 C 286 341 390 315 390 182 C 390 65 310 26 246 30`

  const metricChoices = {
    trend: [`ADX ${fixed(focusState.adx, 0)}`],
    momentum: [
      `RSI ${fixed(focusState.rsi, 0)}`,
      `ROC ${signedPct(number(focusState.roc) == null ? null : number(focusState.roc) * 100, 2)}`,
    ],
    flow: [`%B ${fixed(focusBoll.pctB, 2)}`, `Z ${fixed(focusState.z, 1)}`],
    structure: [`压 ${compactPrice(levels.resistance)}`, `撑 ${compactPrice(levels.support)}`],
    derivatives: [
      `OI ${signedPct(number(focusOI.changePct) == null ? null : number(focusOI.changePct) * 100, 2)}`,
      `费率 ${signedPct(number(focusFunding.rate) == null ? null : number(focusFunding.rate) * 100, 3)}`,
    ],
    information: [`情绪 ${fixed(focusSentiment.score, 2)}`, `HMM ${focusHMM.label || "--"}`],
  }
  const feedPackets = routerFeeds.map((edge, index) => {
    const choices = metricChoices[experts[index].id] || []
    return {
      ...edge,
      text: choices[(scanIndex + index) % Math.max(1, choices.length)] || "--",
      midpoint: {
        x: (edge.from.x + edge.to.x) / 2,
        y: (edge.from.y + edge.to.y) / 2,
      },
    }
  })

  const busArrivalDelay = (pulse) =>
    (pulse.delay || 0) + (pulse.spawn ?? 170) + (pulse.duration || 1400)

  return (
    <motion.section
      className={styles.root}
      aria-label="量化计算编排水位"
      data-ready={scoredCount > 0 || undefined}
      data-signal={signalActive ? "y" : undefined}
      initial={reduced ? false : { opacity: 0, scale: 0.985 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: reduced ? 0 : 0.35, ease: [0.22, 1, 0.36, 1] }}
    >
      <svg className={styles.network} viewBox="0 0 400 350" preserveAspectRatio="none" aria-hidden>
        <defs>
          <linearGradient id={`in-${uid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="var(--accent)" stopOpacity="0.72" />
            <stop offset="1" stopColor="var(--accent)" stopOpacity="0.12" />
          </linearGradient>
          <linearGradient id={`route-${uid}`} x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stopColor="var(--accent)" stopOpacity="0.15" />
            <stop offset="1" stopColor="var(--color-teal-500)" stopOpacity="0.56" />
          </linearGradient>
        </defs>

        <g className={styles.wires}>
          {busToA.map((edge) => (
            <path key={edge.id} className={styles.inputWire} d={edge.path}
              stroke={`url(#in-${uid})`} />
          ))}
          {aToB.map((edge, index) => (
            <path key={edge.id} className={styles.networkWire} d={edge.path}
              style={{ "--wire-delay": `${(index % 8) * -0.5}s` }} />
          ))}
          {routerFeeds.map((edge) => (
            <path key={edge.id} className={styles.routerWire} d={edge.path}
              stroke={`url(#route-${uid})`} />
          ))}
          {routerToExpert.map((edge) => (
            <path key={edge.id} className={styles.expertWire} d={edge.path} />
          ))}
          {expertToMerge.map((edge) => (
            <path key={edge.id} className={styles.mergeWire} d={edge.path} />
          ))}
          {outputPaths.map((edge) => (
            <path key={edge.id} className={styles.outputWire} d={edge.path} />
          ))}
          <path className={styles.feedbackWire} d={feedbackPath} />
        </g>

        {[...HIDDEN_A, ...HIDDEN_B].map((node, index) => (
          <g key={`node-${index}`} className={styles.microNode}
            style={{ "--micro-delay": `${(index % 8) * -0.36}s` }}>
            <circle cx={node.x} cy={node.y} r="5.4" className={styles.microHalo} />
            <circle cx={node.x} cy={node.y} r="2.6" className={styles.microCore} />
          </g>
        ))}

        {!reduced && (
          <g>
            {aToB.filter((_, index) => index % 2 === 0).map((edge, index) => (
              <FlowParticle key={`net-${edge.id}`} path={edge.path}
                delay={index * 0.18} duration={3.2 + (index % 4) * 0.34} tone="node" />
            ))}
            {routerFeeds.map((edge, index) => (
              <FlowParticle key={`route-${edge.id}`} path={edge.path}
                delay={0.45 + index * 0.2} duration={2.8 + (index % 3) * 0.26} tone="router" />
            ))}
            {expertToMerge.map((edge, index) => (
              <FlowParticle key={`merge-flow-${edge.id}`} path={edge.path}
                delay={0.3 + index * 0.15} duration={3.1 + (index % 2) * 0.35} tone="collect" />
            ))}
            {outputPaths.map((edge, index) => (
              <FlowParticle key={`out-${edge.id}`} path={edge.path}
                delay={0.2 + index * 0.6} duration={2.1} tone="output" />
            ))}
            <FlowParticle path={feedbackPath} delay={0.7} duration={7.2} tone="loop" />
          </g>
        )}

        {feedPackets.map((packet) => (
          <DataPacket key={`metric-${packet.id}-${packet.text}`}
            path={packet.path} text={packet.text} midpoint={packet.midpoint}
            delay={0.2 + (packet.y % 5) * 0.28} duration={6.8}
            reduced={reduced} />
        ))}
      </svg>

      <div className={styles.dataBus} style={pos(BUS)}
        data-metric-intake="" data-active={metricPulses.length ? "y" : undefined}
        role="img" aria-label="数据接收">
        <span className={styles.busIcon}><Database size={11} strokeWidth={1.8} /></span>
        <span className={styles.busPorts}>
          {[0, 1, 2, 3, 4, 5].map((item) => <i key={item} />)}
        </span>
        {metricPulses.slice(-10).map((pulse) => (
          <span key={pulse.id} className={styles.busImpact}
            style={{
              "--impact-color": pulse.color,
              animationDelay: `${busArrivalDelay(pulse)}ms`,
            }} />
        ))}
      </div>

      <div className={styles.routerPanel}
        style={{
          left: `${(ROUTER.left / 400) * 100}%`,
          top: `${(ROUTER.top / 350) * 100}%`,
          width: `${((ROUTER.right - ROUTER.left) / 400) * 100}%`,
          height: `${((ROUTER.bottom - ROUTER.top) / 350) * 100}%`,
        }}>
        {experts.map((expert, index) => (
          <span key={expert.id} className={styles.routerRow}
            style={{ top: `${((ROW_Y[index] - ROUTER.top) / (ROUTER.bottom - ROUTER.top)) * 100}%` }}>
            <i data-tone={toneForScore(expert.score)}
              style={{ width: `${expert.score == null ? 0 : clamp(expert.score)}%` }} />
          </span>
        ))}
      </div>

      {experts.map((expert, index) => (
        <div key={expert.id} className={styles.expertRow}
          data-tone={toneForScore(expert.score)}
          style={{
            left: `${(EXPERT_X[index] / 400) * 100}%`,
            top: `${(ROW_Y[index] / 350) * 100}%`,
            "--expert-tone": expert.tone,
          }}
          role="img" aria-label={`${expert.label} ${expert.score == null ? "计算中" : Math.round(expert.score)}`}>
          <span className={styles.expertIcon}><expert.Icon size={11} strokeWidth={1.8} /></span>
          <span className={styles.expertMeter}>
            <i style={{ width: `${expert.score == null ? 0 : clamp(expert.score)}%` }} />
          </span>
          <b>{expert.score == null ? "—" : Math.round(expert.score)}</b>
        </div>
      ))}

      <span className={styles.mergeNode} style={pos(MERGE)} aria-hidden><i /></span>

      <div className={styles.outputNode} data-active={riskCount > 0 || undefined}
        style={{ ...pos(OUTPUT.risk), "--output-tone": "var(--color-rose-500)" }}
        role="img" aria-label="统一风控">
        <span className={styles.outputHalo} />
        <span className={styles.outputDisc}><ShieldCheck size={11} strokeWidth={1.8} /></span>
      </div>
      <div className={styles.outputNode} data-active={executionCount > 0 || undefined}
        style={{ ...pos(OUTPUT.execution), "--output-tone": "var(--color-blue-500)" }}
        role="img" aria-label="执行">
        <span className={styles.outputHalo} />
        <span className={styles.outputDisc}><Zap size={11} strokeWidth={1.8} /></span>
      </div>

      {signalActive && (
        <motion.div key={signalActive.id || signalActive.ts} className={styles.signalPacket}
          initial={reduced ? { left: "50%", top: "8.6%", opacity: 0 } : { left: "-3%", top: "8.6%", opacity: 0, scale: 0.78 }}
          animate={{
            left: "50%", top: "8.6%",
            opacity: reduced ? 0 : [0, 1, 1, 0],
            scale: reduced ? 1 : [0.78, 1, 1, 1],
          }}
          transition={{ duration: reduced ? 0.35 : 1.35, ease: [0.22, 1, 0.36, 1] }}>
          <span>{priceText(number(signalActive.price) ?? focusPrice)}</span>
          {signalActive.name_zh && <em>{signalActive.name_zh}</em>}
        </motion.div>
      )}

      <span className={styles.srOnly} role="status" aria-live="polite">
        {`${ordered.length} 个币种进入同一套计算链，已完成 ${scoredCount} 个币种评分。`}
      </span>
    </motion.section>
  )
}
