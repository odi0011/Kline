/* 全局决策编排：同一套量化引擎常驻运行，所有币种共享同一条决策链路。
   这里只做实时观察，不写流水；最终交易结果仍由既有决策流水承担。 */
import { useMemo } from "react"
import { motion, useReducedMotion } from "motion/react"
import {
  Activity,
  Check,
  Clock3,
  Database,
  GitBranch,
  Layers3,
  Network,
  Newspaper,
  Radar,
  RefreshCw,
  Scale,
  ShieldCheck,
  TrendingUp,
  Waves,
  X,
  Zap,
} from "lucide-react"
import CoinAvatar from "@/components/ui/coin-avatar"
import styles from "./decision-orchestrator.module.css"

const SYM = (symbol) => String(symbol || "").replace(/usdt$/i, "").toUpperCase()
const clamp = (value, low = 0, high = 100) => Math.min(high, Math.max(low, value))
const number = (value, fallback = null) => {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}
const pct = (value, digits = 1) => {
  const n = number(value)
  return n == null ? "--" : `${n >= 0 ? "+" : ""}${n.toFixed(digits)}%`
}
const round = (value) => {
  const n = number(value)
  return n == null ? "--" : String(Math.round(n))
}
const avg = (values) => {
  const valid = values.map((value) => number(value)).filter((value) => value != null)
  return valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : null
}
const timeText = (ts) => {
  if (!ts) return "--:--"
  const date = new Date(ts + 8 * 3600_000)
  return `${String(date.getUTCHours()).padStart(2, "0")}:${String(date.getUTCMinutes()).padStart(2, "0")}:${String(date.getUTCSeconds()).padStart(2, "0")}`
}

const STAGES = [
  { id: "intake", label: "信号", Icon: Radar, tone: "var(--color-sky-500)" },
  { id: "features", label: "数据", Icon: Database, tone: "var(--color-emerald-500)" },
  { id: "score", label: "路由", Icon: GitBranch, tone: "var(--color-violet-500)" },
  { id: "watch", label: "周期", Icon: Layers3, tone: "var(--color-amber-500)" },
  { id: "risk", label: "风控", Icon: ShieldCheck, tone: "var(--color-rose-500)" },
  { id: "execution", label: "执行", Icon: Zap, tone: "var(--color-blue-500)" },
]

const REGIME_LABEL = {
  trend: "趋势",
  range: "震荡",
  mixed: "过渡",
  unknown: "未知",
}
const DIRECTION_LABEL = {
  bullish: "多",
  bearish: "空",
}
const HMM_LABEL = {
  bull: "趋势偏多",
  bear: "趋势偏空",
  range: "震荡",
}

function toneForScore(score) {
  if (score == null) return "idle"
  if (score >= 55) return "good"
  if (score >= 45) return "mid"
  return "weak"
}

function StatusMark({ status }) {
  if (status === "done") return <Check size={9} strokeWidth={2.4} />
  if (status === "blocked") return <X size={9} strokeWidth={2.4} />
  if (status === "running" || status === "live") return <span className={styles.liveDot} />
  return null
}

function ExpertCard({ expert, index, reduced }) {
  const score = expert.score
  const available = score != null
  return (
    <motion.div
      className={styles.expert}
      data-state={available ? toneForScore(score) : "idle"}
      style={{ "--expert-tone": expert.tone }}
      initial={reduced ? false : { opacity: 0, y: 5 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reduced ? 0 : 0.2, delay: reduced ? 0 : index * 0.025 }}
      title={expert.detail}
    >
      <span className={styles.expertIcon}><expert.Icon size={13} strokeWidth={1.8} /></span>
      <div className={styles.expertBody}>
        <div className={styles.expertTop}>
          <span>{expert.label}</span>
          <b>{available ? Math.round(score) : "—"}</b>
        </div>
        <div className={styles.expertBar} aria-hidden>
          <i style={{ width: `${available ? score : 0}%` }} />
          <u />
        </div>
        <small>{expert.detail}</small>
      </div>
    </motion.div>
  )
}

function Sparkline({ values }) {
  const width = 116
  const height = 28
  const points = values.length
    ? values.map((value, index) => {
        const x = values.length === 1 ? width / 2 : (index / (values.length - 1)) * width
        const y = height - 3 - (clamp(value) / 100) * (height - 6)
        return [x, y]
      })
    : []
  const path = points.map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ")
  return (
    <svg className={styles.spark} viewBox={`0 0 ${width} ${height}`} aria-hidden>
      <line x1="0" y1={height - 3 - 0.55 * (height - 6)}
        x2={width} y2={height - 3 - 0.55 * (height - 6)}
        className={styles.sparkThreshold} />
      {path && <path d={path} className={styles.sparkPath} />}
      {points.map(([x, y], index) => (
        <circle key={index} cx={x} cy={y} r={index === points.length - 1 ? 2.2 : 1.4}
          className={styles.sparkDot} />
      ))}
    </svg>
  )
}

export default function DecisionOrchestrator({ analysis, symbols, patternStats }) {
  const traces = analysis?.decisionTraces || []
  const latestBySymbol = analysis?.latestTraces || {}
  const liveFeatures = analysis?.liveFeatures || {}
  const liveEvaluations = analysis?.liveEvaluations || {}
  const liveScoreHistory = analysis?.liveScoreHistory || {}
  const symbolStates = analysis?.symbols || {}
  const gateLog = analysis?.gateLog || []
  const plans = analysis?.positionPlans || []
  const reduced = useReducedMotion()
  const ordered = useMemo(
    () => (Array.isArray(symbols) ? symbols.map((symbol) => String(symbol).toLowerCase()) : []),
    [symbols],
  )

  /* 所有币种进入同一张巡检表。这里按币种读取数据，只是为了汇总展示；
     决策仍由后端同一套引擎、同一组门控和同一份账户风控运行。 */
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
      const state = symbolStates[symbol] || null
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
        gate,
        liveEval,
        state,
        features,
        breakdown,
        direction,
        score: number(scoreData?.score),
        gap: number(scoreData?.gap),
        derivativeScore,
        sentimentScore,
      }
    })
  }, [ordered, traces, gateLog, latestBySymbol, liveEvaluations, symbolStates, liveFeatures])

  const total = ordered.length
  const scored = lanes.filter((lane) => lane.score != null)
  const featureCount = lanes.filter((lane) => Object.keys(lane.features || {}).length > 0).length
  const htfCount = lanes.filter((lane) => lane.features?.htf?.direction).length
  const planSymbols = new Set(plans.map((plan) => String(plan.symbol || "").toLowerCase()))
  const riskCount = lanes.filter((lane) => lane.trace?.risk || planSymbols.has(lane.symbol)).length
  const executionCount = lanes.filter((lane) => lane.trace?.status === "opened").length
  const errorCount = lanes.filter((lane) => lane.trace?.status === "error").length
  const avgScore = avg(scored.map((lane) => lane.score))
  const avgGap = avg(scored.map((lane) => lane.gap))
  const breakdown = {
    trend: avg(lanes.map((lane) => lane.breakdown.trend)),
    momentum: avg(lanes.map((lane) => lane.breakdown.momentum)),
    volume: avg(lanes.map((lane) => lane.breakdown.volume)),
    structure: avg(lanes.map((lane) => lane.breakdown.structure)),
  }
  const avgAdx = avg(lanes.map((lane) => lane.state?.adx))
  const avgRsi = avg(lanes.map((lane) => lane.state?.rsi))
  const avgRoc = avg(lanes.map((lane) => number(lane.state?.roc) == null ? null : number(lane.state.roc) * 100))
  const avgObi = avg(lanes.map((lane) => number(lane.features?.obi?.bias)))
  const avgOiChange = avg(lanes.map((lane) => number(lane.features?.oi?.changePct) == null
    ? null : number(lane.features.oi.changePct) * 100))
  const avgFunding = avg(lanes.map((lane) => number(lane.features?.funding?.rate) == null
    ? null : number(lane.features.funding.rate) * 100))
  const avgSentiment = avg(lanes.map((lane) => number(lane.features?.sentiment?.score)))
  const cvdBull = lanes.filter((lane) => lane.features?.cvd?.signal === "bullish").length
  const cvdBear = lanes.filter((lane) => lane.features?.cvd?.signal === "bearish").length
  const squeezeCount = lanes.filter((lane) => lane.features?.squeeze?.on).length
  const oiAligned = lanes.filter((lane) => lane.features?.oi?.bias === lane.direction).length
  const hmmBull = lanes.filter((lane) => lane.features?.hmm?.label === "bull").length
  const hmmBear = lanes.filter((lane) => lane.features?.hmm?.label === "bear").length
  const rsiValue = round(avgRsi)
  const winStats = lanes
    .map((lane) => patternStats?.[lane.trace?.pattern])
    .filter(Boolean)
  const wins = winStats.reduce((sum, item) => sum + Number(item.wins || 0), 0)
  const losses = winStats.reduce((sum, item) => sum + Number(item.losses || 0), 0)
  const winrate = wins + losses > 0 ? (wins / (wins + losses)) * 100 : null
  const derivativeScore = avg(lanes.map((lane) => lane.derivativeScore))
  const sentimentScore = avg(lanes.map((lane) => lane.sentimentScore))

  const experiments = [
    {
      id: "trend",
      label: "趋势面",
      Icon: TrendingUp,
      tone: "var(--color-sky-500)",
      score: breakdown.trend,
      detail: `${htfCount}/${total} 币同向 · ADX ${round(avgAdx)} · ${REGIME_LABEL[lanes[0]?.state?.regime] || "扫描中"}`,
    },
    {
      id: "momentum",
      label: "动量面",
      Icon: Activity,
      tone: "var(--color-violet-500)",
      score: breakdown.momentum,
      detail: `RSI ${rsiValue} · ROC ${pct(avgRoc, 2)} · ${scored.length}/${total} 币评分`,
    },
    {
      id: "flow",
      label: "量价面",
      Icon: Waves,
      tone: "var(--color-teal-500)",
      score: breakdown.volume,
      detail: `CVD 多${cvdBull}/空${cvdBear} · OBI ${pct(avgObi == null ? null : avgObi * 100, 1)} · 挤压 ${squeezeCount}`,
    },
    {
      id: "structure",
      label: "结构面",
      Icon: Network,
      tone: "var(--color-amber-500)",
      score: breakdown.structure,
      detail: `近期信号 ${traces.length} · 计划 ${plans.length} · ${winrate == null ? "胜率累计中" : `胜率 ${Math.round(winrate)}%`}`,
    },
    {
      id: "derivatives",
      label: "衍生面",
      Icon: Scale,
      tone: "var(--color-blue-500)",
      score: derivativeScore,
      detail: `OI ${pct(avgOiChange, 2)} · 费率 ${pct(avgFunding, 3)} · ${oiAligned}/${total} 同向`,
    },
    {
      id: "information",
      label: "信息面",
      Icon: Newspaper,
      tone: "var(--color-pink-500)",
      score: sentimentScore,
      detail: `情绪 ${avgSentiment == null ? "--" : avgSentiment.toFixed(2)} · HMM 多${hmmBull}/空${hmmBear}`,
    },
  ]

  const stageData = {
    intake: {
      count: total, status: total ? "live" : "idle",
      summary: `共享引擎常驻运行，正在循环巡检 ${total} 个币种`,
    },
    features: {
      count: featureCount, status: featureCount ? "done" : "live",
      summary: `${featureCount} 个币种的技术、量价、衍生品和信息数据已归并`,
    },
    score: {
      count: scored.length, status: scored.length ? "done" : "live",
      summary: `${scored.length} 个币种完成多空评分`,
    },
    watch: {
      count: htfCount, status: htfCount ? "done" : "live",
      summary: `${htfCount} 个币种具备多周期方向`,
    },
    risk: {
      count: riskCount, status: riskCount ? "done" : "live",
      summary: `${riskCount} 个币种进入统一风控或已有仓位计划`,
    },
    execution: {
      count: executionCount, status: executionCount ? "done" : "live",
      summary: executionCount ? `${executionCount} 个币种有开仓记录` : "等待满足全部条件的信号",
    },
  }
  const stages = STAGES.map((stage) => ({ ...stage, ...stageData[stage.id] }))

  const scorePoints = []
  for (const history of Object.values(liveScoreHistory)) {
    for (const item of history || []) {
      if (number(item?.score) != null) scorePoints.push({ ts: number(item.ts, 0), score: number(item.score) })
    }
  }
  for (const trace of traces) {
    if (number(trace?.score?.score) != null) {
      scorePoints.push({ ts: number(trace.updatedAt || trace.ts, 0), score: number(trace.score.score) })
    }
  }
  const recentScores = scorePoints
    .sort((a, b) => a.ts - b.ts)
    .slice(-18)
    .map((point) => point.score)
  const lastUpdated = Math.max(
    0,
    ...traces.map((trace) => number(trace.updatedAt || trace.ts, 0)),
    ...Object.values(liveEvaluations).map((item) => number(item?.ts, 0)),
    ...gateLog.map((item) => number(item?.ts, 0)),
  )
  const resultText = errorCount
    ? `巡检中 · ${errorCount} 异常`
    : executionCount ? `巡检中 · ${executionCount} 开仓` : `全线巡检 · ${total} 币`
  const resultTone = errorCount ? "error" : executionCount ? "good" : "idle"
  const summary = `覆盖 ${featureCount}/${total} · 评分 ${scored.length} · 拦截 ${gateLog.length}`

  return (
    <section className={styles.root} aria-label="全局决策编排">
      <header className={styles.head}>
        <div className={styles.title}>
          <span className={styles.titleIcon}><GitBranch size={14} /></span>
          <b>决策编排</b>
          <span className={styles.liveLabel}><i />常驻</span>
        </div>
        <div className={styles.headMeta}>
          <Clock3 size={11} />
          <span>{timeText(lastUpdated)}</span>
        </div>
      </header>

      <div className={styles.loopStrip} aria-label={`共享巡检 ${total} 个币种`}>
        <span className={styles.loopLead}>
          <RefreshCw size={10} strokeWidth={1.9} />
          <span>共享巡检</span>
        </span>
        <div className={styles.coinRail} aria-hidden>
          {lanes.map((lane, index) => (
            <span key={lane.symbol} className={styles.coinPulse}
              style={{ "--coin-i": index }}
              title={`${SYM(lane.symbol)} · ${lane.score == null ? "等待评分" : `${Math.round(lane.score)} 分`}`}>
              <CoinAvatar symbol={lane.symbol} size={14} />
              <i data-tone={toneForScore(lane.score)} />
            </span>
          ))}
        </div>
        <b>{total} 币循环</b>
      </div>

      <div className={styles.pipeline}>
        <div className={styles.rail} aria-hidden>
          <motion.span className={styles.railFill}
            initial={reduced ? { width: "100%" } : { width: 0 }}
            animate={{ width: "100%" }}
            transition={{ duration: reduced ? 0 : 0.6, ease: [0.22, 1, 0.36, 1] }} />
          {!reduced && [0, 1].map((index) => (
            <motion.span key={index} className={styles.scan}
              initial={{ left: "-8%", opacity: 0 }}
              animate={{ left: "104%", opacity: [0, 0.9, 0] }}
              transition={{
                duration: 3.8, repeat: Infinity, repeatDelay: 1.4,
                delay: index * 2.6, ease: "linear",
              }} />
          ))}
        </div>
        {stages.map((stage) => (
          <div key={stage.id} className={styles.stage} data-status={stage.status}
            style={{ "--stage-tone": stage.tone }} title={stage.summary}>
            <span className={styles.stageOrb}>
              <stage.Icon size={10} strokeWidth={1.9} />
              <span className={styles.stageMark}><StatusMark status={stage.status} /></span>
            </span>
            <span className={styles.stageLabel}>
              <span>{stage.label}</span>
              <em>{stage.count}</em>
            </span>
            <span className={styles.srOnly}>
              {stage.status === "done" ? "已处理" : stage.status === "blocked" ? "未通过" : "运行中"}
            </span>
          </div>
        ))}
      </div>

      <div className={styles.branch} aria-hidden>
        <span />
      </div>

      <div className={styles.experts}>
        {experiments.map((expert, index) => (
          <ExpertCard key={expert.id} expert={expert} index={index} reduced={reduced} />
        ))}
      </div>

      <footer className={styles.footer}>
        <div className={styles.scoreBlock} data-tone={toneForScore(avgScore)}>
          <span>全线分</span>
          <b>{avgScore == null ? "—" : Math.round(avgScore)}</b>
          <em>{avgGap == null ? "等待评分" : `均Gap ${avgGap >= 0 ? "+" : ""}${Math.round(avgGap)}`}</em>
        </div>
        <div className={styles.trendBlock}>
          <span>跨币势能</span>
          <Sparkline values={recentScores} />
        </div>
        <div className={styles.riskBlock}>
          <span className={styles.resultChip} data-tone={resultTone}>{resultText}</span>
          <small>{summary}</small>
        </div>
      </footer>
      <span className={styles.srOnly} role="status" aria-live="polite">
        {`${total} 个币种共享一套决策引擎，持续循环巡检。${summary}`}
      </span>
    </section>
  )
}
