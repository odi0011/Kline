/* 量化计算网络：指标流进入数据总线，经过多层协同计算、路由和专家分流，
   最终汇入风控与执行并回流。画面只表达计算过程，不承载交易结果。 */
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
const priceText = (value) => {
  const n = number(value)
  if (n == null) return "--"
  return n >= 1000
    ? n.toLocaleString("en-US", { maximumFractionDigits: 1 })
    : n >= 1 ? n.toFixed(3) : n.toPrecision(4)
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
const toneForScore = (score) => {
  if (score == null) return "idle"
  if (score >= 55) return "good"
  if (score >= 45) return "mid"
  return "weak"
}

const BUS = { x: 27, y: 175 }
const LAYERS = [
  { id: "l1", x: 73, ys: [92, 147, 202, 257] },
  { id: "l2", x: 118, ys: [69, 116, 163, 210, 257] },
  { id: "l3", x: 163, ys: [92, 147, 202, 257] },
]
const ROW_Y = [92, 126, 160, 194, 228, 262]
const ROUTER = { left: 202, right: 254, top: 64, bottom: 296 }
const EXPERT_X = 316
const COLLECTOR = { x: 366, y: 175 }
const OUTPUT = {
  risk: { x: 381, y: 137 },
  execution: { x: 381, y: 213 },
}
const EXPERT_LAYOUT = [
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

function nearestLevels(structure, price, boll) {
  const current = number(price)
  let support = null
  let resistance = null
  for (const level of structure?.levels || []) {
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
    support: support ?? number(boll?.lower),
    resistance: resistance ?? number(boll?.upper),
  }
}

const edgePath = (from, to, bend = 0) => {
  const dx = to.x - from.x
  return `M ${from.x} ${from.y} C ${from.x + dx * 0.34} ${from.y + bend} ` +
    `${to.x - dx * 0.27} ${to.y - bend * 0.45} ${to.x} ${to.y}`
}

function FlowParticle({ path, delay, duration, tone = "accent" }) {
  return (
    <g className={styles.particle} data-tone={tone} opacity="0">
      <circle r="2.6" className={styles.particleHalo} />
      <circle r="1" className={styles.particleCore} />
      <animateMotion path={path} dur={`${duration}s`} begin={`${delay}s`} repeatCount="indefinite" />
      <animate attributeName="opacity" values="0;0.96;0.96;0" keyTimes="0;0.12;0.82;1"
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
  const levels = nearestLevels(structures?.[focusSymbol], focusPrice, focusBoll)

  const scoreById = {
    trend: avg(lanes.map((lane) => lane.breakdown.trend)),
    momentum: avg(lanes.map((lane) => lane.breakdown.momentum)),
    flow: avg(lanes.map((lane) => lane.breakdown.volume)),
    structure: avg(lanes.map((lane) => lane.breakdown.structure)),
    derivatives: avg(lanes.map((lane) => lane.derivativeScore)),
    information: avg(lanes.map((lane) => lane.sentimentScore)),
  }
  const experts = EXPERT_LAYOUT.map((expert) => ({ ...expert, score: scoreById[expert.id] }))
  const scoredCount = lanes.filter((lane) => lane.score != null).length
  const riskCount = lanes.filter((lane) => lane.trace?.risk).length + plans.length
  const executionCount = lanes.filter((lane) => lane.trace?.status === "opened").length

  const layerNodes = LAYERS.map((layer) => layer.ys.map((y, index) => ({
    id: `${layer.id}-${index}`,
    x: layer.x,
    y,
  })))
  const backendEdges = []
  for (let layerIndex = 0; layerIndex < layerNodes.length - 1; layerIndex += 1) {
    for (const [fromIndex, from] of layerNodes[layerIndex].entries()) {
      for (const [toIndex, to] of layerNodes[layerIndex + 1].entries()) {
        const bend = ((fromIndex + toIndex + layerIndex) % 5 - 2) * 4
        backendEdges.push({
          id: `${from.id}-${to.id}`,
          path: edgePath(from, to, bend),
          layer: layerIndex,
        })
      }
    }
  }
  const inputEdges = layerNodes[0].map((node, index) => ({
    id: `bus-${node.id}`,
    path: edgePath(BUS, node, (index - 1.5) * 3),
  }))
  const routerFeeds = ROW_Y.map((y, index) => {
    const from = layerNodes[2][index % layerNodes[2].length]
    const to = { x: ROUTER.left + 2, y }
    return {
      id: `router-${experts[index].id}`,
      from,
      to,
      y,
      path: edgePath(from, to, ((index % 3) - 1) * 8),
    }
  })
  const expertDrops = ROW_Y.map((y, index) => ({
    id: `drop-${experts[index].id}`,
    path: `M ${ROUTER.right + 2} ${y} C ${ROUTER.right + 8} ${y} ${EXPERT_X - 49} ${y} ${EXPERT_X - 46} ${y}`,
  }))
  const collectorPaths = ROW_Y.map((y, index) => ({
    id: `collect-${experts[index].id}`,
    path: `M ${EXPERT_X + 47} ${y} C ${EXPERT_X + 56} ${y} ${COLLECTOR.x - 5} ${COLLECTOR.y + (y - COLLECTOR.y) * 0.18} ${COLLECTOR.x} ${COLLECTOR.y}`,
  }))
  const outputPaths = [
    { id: "risk", path: `M ${COLLECTOR.x} ${COLLECTOR.y} C 374 ${COLLECTOR.y} 377 154 ${OUTPUT.risk.x} ${OUTPUT.risk.y}` },
    { id: "execution", path: `M ${COLLECTOR.x} ${COLLECTOR.y} C 374 ${COLLECTOR.y} 377 196 ${OUTPUT.execution.x} ${OUTPUT.execution.y}` },
  ]
  const feedbackPath = `M ${OUTPUT.execution.x} 229 C ${OUTPUT.execution.x} 309 318 330 203 330 C 80 330 18 309 18 280`

  const metricChoices = {
    trend: [`ADX ${fixed(focusState.adx, 0)}`, `ATR ${fixed(focusState.atrPct, 0)}`],
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
    const metric = choices[(scanIndex + index) % Math.max(1, choices.length)] || "--"
    return {
      ...edge,
      text: metric,
      midpoint: {
        x: (edge.from.x + edge.to.x) / 2,
        y: (edge.from.y + edge.to.y) / 2,
      },
    }
  })

  const animatedBackendEdges = backendEdges.filter((_, index) => index % 3 === 0)
  const busArrivalDelay = (pulse) =>
    (pulse.delay || 0) + (pulse.spawn ?? 170) + (pulse.duration || 1400)

  return (
    <motion.section
      className={styles.root}
      aria-label="量化计算网络"
      data-ready={scoredCount > 0 || undefined}
      data-signal={signalActive ? "y" : undefined}
      initial={reduced ? false : { opacity: 0, scale: 0.985 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: reduced ? 0 : 0.35, ease: [0.22, 1, 0.36, 1] }}
    >
      <svg className={styles.network} viewBox="0 0 400 350" preserveAspectRatio="none" aria-hidden>
        <defs>
          <linearGradient id={`bus-${uid}`} x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stopColor="var(--accent)" stopOpacity="0.46" />
            <stop offset="1" stopColor="var(--color-teal-500)" stopOpacity="0.18" />
          </linearGradient>
        </defs>

        <rect className={styles.layerBand} x="60" y="55" width="26" height="240" rx="13" />
        <rect className={styles.layerBand} x="105" y="55" width="26" height="240" rx="13" />
        <rect className={styles.layerBand} x="150" y="55" width="26" height="240" rx="13" />

        <g className={styles.wires}>
          {inputEdges.map((edge) => (
            <path key={edge.id} className={styles.inputWire} d={edge.path}
              stroke={`url(#bus-${uid})`} />
          ))}
          {backendEdges.map((edge) => (
            <path key={edge.id} className={styles.backendWire} d={edge.path}
              style={{ "--edge-layer": edge.layer }} />
          ))}
          {routerFeeds.map((edge) => (
            <path key={edge.id} className={styles.routerWire} d={edge.path} />
          ))}
          {expertDrops.map((edge) => (
            <path key={edge.id} className={styles.expertWire} d={edge.path} />
          ))}
          {collectorPaths.map((edge) => (
            <path key={edge.id} className={styles.collectWire} d={edge.path} />
          ))}
          {outputPaths.map((edge) => (
            <path key={edge.id} className={styles.outputWire} d={edge.path} />
          ))}
          <path className={styles.feedbackWire} d={feedbackPath} />
        </g>

        {layerNodes.flat().map((node, index) => (
          <circle key={node.id} className={styles.microNode} cx={node.x} cy={node.y} r="3.2"
            style={{ "--node-delay": `${(index % 7) * -0.31}s` }} />
        ))}

        {!reduced && (
          <g>
            {animatedBackendEdges.map((edge, index) => (
              <FlowParticle key={`flow-${edge.id}`} path={edge.path}
                delay={index * 0.17} duration={3.8 + (index % 4) * 0.42} tone="node" />
            ))}
            {routerFeeds.map((edge, index) => (
              <FlowParticle key={`router-flow-${edge.id}`} path={edge.path}
                delay={0.4 + index * 0.22} duration={3.1 + (index % 3) * 0.3} tone="router" />
            ))}
            {collectorPaths.map((edge, index) => (
              <FlowParticle key={`collect-flow-${edge.id}`} path={edge.path}
                delay={0.25 + index * 0.18} duration={2.7 + (index % 2) * 0.4} tone="collect" />
            ))}
            {outputPaths.map((edge, index) => (
              <FlowParticle key={`output-flow-${edge.id}`} path={edge.path}
                delay={0.3 + index * 0.7} duration={2.2} tone="output" />
            ))}
            <FlowParticle path={feedbackPath} delay={0.8} duration={7.2} tone="loop" />
          </g>
        )}

        {feedPackets.map((packet) => (
          <DataPacket key={`metric-${packet.id}-${packet.text}`}
            path={packet.path} text={packet.text} midpoint={packet.midpoint}
            delay={0.22 + (packet.y % 4) * 0.33} duration={7.2}
            reduced={reduced} />
        ))}
      </svg>

      <div className={styles.dataBus} style={pos(BUS)}
        data-metric-intake="" data-active={metricPulses.length ? "y" : undefined}
        role="img" aria-label="数据接收">
        <span className={styles.busHead}><Database size={10} strokeWidth={1.8} /></span>
        <span className={styles.busTrack}>
          {[0, 1, 2, 3, 4, 5].map((item) => <i key={item} data-tone={item % 2 ? "warm" : "cool"} />)}
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
            left: `${(EXPERT_X / 400) * 100}%`,
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

      <span className={styles.collector} style={pos(COLLECTOR)} aria-hidden><i /></span>

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
          initial={reduced ? { left: "6.75%", top: "50%", opacity: 0 } : { left: "-2%", top: "50%", opacity: 0, scale: 0.78 }}
          animate={{
            left: "6.75%", top: "50%",
            opacity: reduced ? 0 : [0, 1, 1, 0],
            scale: reduced ? 1 : [0.78, 1, 1, 1],
          }}
          transition={{ duration: reduced ? 0.35 : 1.35, ease: [0.22, 1, 0.36, 1] }}>
          <span>{priceText(number(signalActive.price) ?? focusPrice)}</span>
          {signalActive.name_zh && <em>{signalActive.name_zh}</em>}
        </motion.div>
      )}

      <span className={styles.srOnly} role="status" aria-live="polite">
        {`${ordered.length} 个币种进入同一套计算网络，已完成 ${scoredCount} 个币种评分。`}
      </span>
    </motion.section>
  )
}
