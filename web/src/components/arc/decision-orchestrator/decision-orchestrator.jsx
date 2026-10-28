/* 中间 50% 槽位：亮色轨道星云。
   左上角策略概览是液态玻璃数据牌，贴齐槽位左上角，多组指标交叉淡入。
   每个币只有一张玻璃胶囊：图标 + 代码 + 评分与方向。玻璃底不改。
   飞线落在该币自己的椭圆环节点上。 */
import { useEffect, useMemo, useRef, useState } from "react"
import { useReducedMotion } from "motion/react"
import CoinAvatar from "@/components/ui/coin-avatar"
import { LiquidGlass } from "@/components/ui/liquid-glass"
import { createNebulaScene } from "./nebula-scene"
import styles from "./decision-orchestrator.module.css"

const number = (value, fallback = null) => {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

const RATE_KEYS = new Set(["winrate", "hitrate", "win_rate", "hit_rate"])
const SUMMARY_CLEAR = { right: 252, bottom: 120 }

function hash(text) {
  let value = 0
  for (const char of String(text)) value = (value * 31 + char.charCodeAt(0)) >>> 0
  return value
}

function shortSym(symbol) {
  return String(symbol || "").replace(/usdt$/i, "").toUpperCase() || "—"
}

function rateKey(key) {
  return String(key).replace(/-/g, "_").toLowerCase()
}

function formatRate(value) {
  const n = number(value)
  if (n == null || n < 0) return null
  if (n <= 1) return `${Math.round(n * 100)}%`
  if (n <= 100) return `${Math.round(n)}%`
  return null
}

/** 只在分析快照里找真正的胜率字段，找不到就返回 null，绝不把评分当成胜率。 */
function findWinRate(value, depth = 0) {
  if (value == null || depth > 4) return null
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findWinRate(item, depth + 1)
      if (found) return found
    }
    return null
  }
  if (typeof value !== "object") return null
  for (const [key, child] of Object.entries(value)) {
    if (RATE_KEYS.has(rateKey(key))) {
      const text = formatRate(child)
      if (text) return text
    }
  }
  for (const child of Object.values(value)) {
    if (child && typeof child === "object") {
      const found = findWinRate(child, depth + 1)
      if (found) return found
    }
  }
  return null
}

function directionLabel(direction) {
  if (direction === "bullish") return "看涨"
  if (direction === "bearish") return "看跌"
  if (direction === "neutral") return "中性"
  return ""
}

function fmtInt(value) {
  const n = number(value)
  return n == null ? "—" : String(Math.round(n))
}

function fmtSigned(value) {
  const n = number(value)
  if (n == null) return "—"
  const rounded = Math.round(n)
  return rounded > 0 ? `+${rounded}` : String(rounded)
}

function meanOf(values) {
  const nums = values.map((value) => number(value)).filter((value) => value != null)
  if (!nums.length) return null
  return nums.reduce((sum, value) => sum + value, 0) / nums.length
}

function lookupSymbol(map, symbol) {
  if (!map || symbol == null) return null
  const key = String(symbol)
  return map[key] || map[key.toLowerCase()] || map[key.toUpperCase()] || null
}

function toneForScore(score) {
  if (score == null) return "idle"
  if (score >= 55) return "good"
  if (score >= 45) return "mid"
  return "weak"
}

/** 币种胶囊文案。只改文字，玻璃底仍由外层 LiquidGlass 负责。 */
function pillCaption(live, overlay) {
  const score = number(live?.score) ?? number(overlay?.score)
  if (score != null) {
    const dir = directionLabel(live?.direction)
    const text = `评分 ${Math.round(score)}`
    return dir ? `${text} · ${dir}` : text
  }
  const pct = number(live?.pct) ?? number(overlay?.pct)
  if (pct == null) return "评分 —"
  const sign = pct > 0 ? "+" : ""
  return `涨跌 ${sign}${pct.toFixed(2)}%`
}

const SUMMARY_PAGE_SLOTS = 8

function packSummaryPages(cells) {
  const pages = []
  let page = []
  let slots = 0
  const flush = () => {
    if (!page.length) return
    pages.push(page)
    page = []
    slots = 0
  }
  for (const cell of cells) {
    const span = cell.wide ? 2 : 1
    if (slots + span > SUMMARY_PAGE_SLOTS) flush()
    page.push(cell)
    slots += span
  }
  flush()
  return pages
}

function buildStrategyCells({ nodes, ordered, symbolStates, liveEvaluations, analysis, winRateText }) {
  const scored = nodes.filter((node) => node.score != null)
  const meanScore = meanOf(scored.map((node) => node.score))
  const hot = scored.filter((node) => node.score >= 55).length
  const bias = scored.length ? Math.round((hot / scored.length) * 100) : null
  const bulls = nodes.filter((node) => node.direction === "bullish").length
  const bears = nodes.filter((node) => node.direction === "bearish").length
  const ranked = [...scored].sort((a, b) => b.score - a.score)
  const lead = ranked[0] || null
  const lag = ranked.length > 1 ? ranked[ranked.length - 1] : null

  const evals = ordered.map((symbol) => lookupSymbol(liveEvaluations, symbol)).filter(Boolean)
  const confidence = meanOf(evals.map((item) => item.confidence))
  const gap = meanOf(evals.map((item) => item.gap))
  const bullScore = meanOf(evals.map((item) => item.bull))
  const bearScore = meanOf(evals.map((item) => item.bear))
  const grades = { strong: 0, weak: 0 }
  let vetoes = 0
  for (const item of evals) {
    if (item.grade === "strong") grades.strong += 1
    if (item.grade === "weak" || item.grade === "none") grades.weak += 1
    if (Array.isArray(item.vetoes) && item.vetoes.length) vetoes += 1
  }

  const regimes = { trend: 0, range: 0, mixed: 0 }
  let squeeze = 0
  let highVol = 0
  for (const symbol of ordered) {
    const state = lookupSymbol(symbolStates, symbol)
    if (!state) continue
    if (regimes[state.regime] != null) regimes[state.regime] += 1
    if (state.squeeze || (number(state.bwPct) != null && state.bwPct < 15)) squeeze += 1
    if (state.volState === "high") highVol += 1
  }

  const drawdown = analysis?.drawdown || null
  const drift = analysis?.drift || null
  const plans = Array.isArray(analysis?.positionPlans) ? analysis.positionPlans : []
  const longs = plans.filter((plan) => plan?.side === "BUY").length
  const shorts = plans.filter((plan) => plan?.side === "SELL").length
  const blocked = Array.isArray(analysis?.gateLog) ? analysis.gateLog.length : 0
  const confidenceText = confidence == null
    ? "—"
    : `${Math.round(confidence <= 1 ? confidence * 100 : confidence)}%`

  const cells = [
    { id: "mean", label: "平均评分", value: fmtInt(meanScore), tone: toneForScore(meanScore) },
    { id: "scored", label: "已评分", value: `${scored.length}/${Math.max(ordered.length, 0)}` },
    {
      id: "bias",
      label: "偏多占比",
      value: bias == null ? "—" : `${bias}%`,
      tone: bias == null ? "idle" : bias >= 60 ? "good" : bias <= 40 ? "weak" : "mid",
    },
    { id: "bulls", label: "看涨", value: String(bulls), tone: bulls ? "good" : "idle" },
    { id: "bears", label: "看跌", value: String(bears), tone: bears ? "weak" : "idle" },
    { id: "lead", label: "领先", value: lead ? `${lead.label} ${Math.round(lead.score)}` : "—" },
    { id: "lag", label: "落后", value: lag ? `${lag.label} ${Math.round(lag.score)}` : "—" },
    { id: "gap-count", label: "多空差", value: fmtSigned(bulls - bears), tone: bulls === bears ? "idle" : bulls > bears ? "good" : "weak" },
    ...(winRateText ? [{ id: "win", label: "胜率", value: winRateText, tone: "good" }] : []),
    { id: "conf", label: "置信", value: confidenceText, tone: toneForScore(confidence == null ? null : (confidence <= 1 ? confidence * 100 : confidence)) },
    { id: "sep", label: "分离度", value: fmtSigned(gap), tone: gap == null ? "idle" : gap >= 8 ? "good" : gap <= 0 ? "weak" : "mid" },
    { id: "bull-score", label: "多方", value: fmtInt(bullScore), tone: toneForScore(bullScore) },
    { id: "bear-score", label: "空方", value: fmtInt(bearScore), tone: bearScore == null ? "idle" : bearScore >= 55 ? "weak" : "mid" },
    { id: "strong", label: "强评级", value: String(grades.strong), tone: grades.strong ? "good" : "idle" },
    { id: "weak", label: "弱评级", value: String(grades.weak), tone: grades.weak ? "weak" : "idle" },
    { id: "veto", label: "否决", value: String(vetoes), tone: vetoes ? "weak" : "idle" },
    { id: "blocked", label: "拦截", value: String(blocked), tone: blocked ? "weak" : "idle" },
    {
      id: "dd",
      label: "回撤",
      value: number(drawdown?.current) == null ? "—" : `${Number(drawdown.current).toFixed(2)}%`,
      tone: drawdown?.paused ? "weak" : "idle",
    },
    { id: "halt", label: "熔断", value: drawdown?.paused ? "暂停" : "正常", tone: drawdown?.paused ? "weak" : "good" },
    {
      id: "drift",
      label: "漂移",
      value: drift?.shadow ? "影子" : drift?.caution ? "谨慎" : "正常",
      tone: drift?.shadow || drift?.caution ? "mid" : "good",
    },
    { id: "trend", label: "趋势", value: String(regimes.trend) },
    { id: "range", label: "震荡", value: String(regimes.range) },
    { id: "squeeze", label: "挤压", value: String(squeeze), tone: squeeze ? "mid" : "idle" },
    { id: "vol", label: "高波", value: String(highVol), tone: highVol ? "weak" : "idle" },
    { id: "pos", label: "持仓", value: `多${longs} 空${shorts}`, tone: longs || shorts ? "mid" : "idle" },
  ]

  for (const node of ranked) {
    const dir = directionLabel(node.direction)
    cells.push({
      id: `sym:${node.id}`,
      label: node.label,
      value: dir ? `评分 ${Math.round(node.score)} · ${dir}` : `评分 ${Math.round(node.score)}`,
      tone: toneForScore(node.score),
      wide: true,
    })
  }

  return cells
}

function placePill(x, y, bounds) {
  const halfW = 72
  const halfH = 22
  let nx = x
  let ny = y
  const overlaps = nx - halfW < SUMMARY_CLEAR.right && nx + halfW > 0
    && ny - halfH < SUMMARY_CLEAR.bottom && ny + halfH > 0
  if (overlaps) {
    const right = { x: SUMMARY_CLEAR.right + halfW + 8, y }
    const below = { x, y: SUMMARY_CLEAR.bottom + halfH + 8 }
    const fits = (point) => {
      if (!(bounds.width > 0 && bounds.height > 0)) return true
      return point.x >= halfW && point.x <= bounds.width - halfW
        && point.y >= halfH && point.y <= bounds.height - halfH
    }
    const choice = fits(right) ? right : below
    nx = choice.x
    ny = choice.y
  }
  if (bounds.width > 0 && bounds.height > 0) {
    nx = Math.min(Math.max(nx, halfW), bounds.width - halfW)
    ny = Math.min(Math.max(ny, halfH), bounds.height - halfH)
  }
  return { x: nx, y: ny }
}

export default function DecisionOrchestrator({
  analysis, symbols,
}) {
  const reduced = useReducedMotion()
  const hostRef = useRef(null)
  const sceneRef = useRef(null)
  const [overlayNodes, setOverlayNodes] = useState([])
  const [intakeTargets, setIntakeTargets] = useState([])
  const [frameBox, setFrameBox] = useState({ width: 0, height: 0 })

  const traces = analysis?.decisionTraces || []
  const latestBySymbol = analysis?.latestTraces || {}
  const liveEvaluations = analysis?.liveEvaluations || {}
  const symbolStates = analysis?.symbols || {}

  const ordered = useMemo(
    () => (Array.isArray(symbols) ? symbols.map((symbol) => String(symbol).toLowerCase()) : []),
    [symbols],
  )

  const nodes = useMemo(() => {
    const lastTrace = new Map()
    for (const item of traces) {
      if (item?.symbol) lastTrace.set(item.symbol, item)
    }
    return ordered.map((symbol, index) => {
      const trace = latestBySymbol[symbol] || lastTrace.get(symbol) || null
      const liveEval = liveEvaluations[symbol] || null
      // 痕迹被挡时 score 常为空；实时共鸣分仍应用来点亮星图
      const scoreData = (trace?.score && number(trace.score?.score) != null)
        ? trace.score
        : (liveEval || trace?.score || {})
      const direction = liveEval?.direction || trace?.direction || null
      const state = symbolStates[symbol] || {}
      const score = number(scoreData?.score)
      const changePct = number(state.changePct) ?? number(state.pct) ?? number(state.roc)
      const pct = score != null
        ? score
        : changePct == null
          ? null
          : Math.abs(changePct) <= 1
            ? changePct * 100
            : changePct
      return {
        id: symbol,
        label: shortSym(symbol),
        score,
        pct,
        direction,
        ring: index % 3,
        baseAngle: (index / Math.max(1, ordered.length)) * Math.PI * 2
          + (hash(symbol) % 40) / 60,
        drift: ((hash(symbol) % 17) - 8) / 220,
        bob: ((hash(`b${symbol}`) % 13) - 6) / 180,
      }
    })
  }, [ordered, traces, latestBySymbol, liveEvaluations, symbolStates])

  const nodeById = useMemo(() => {
    const map = new Map()
    for (const node of nodes) map.set(node.id, node)
    return map
  }, [nodes])

  const winRateText = useMemo(() => findWinRate({
    liveEvaluations: analysis?.liveEvaluations || null,
    decisionTraces: analysis?.decisionTraces || null,
    symbols: analysis?.symbols || null,
    latestTraces: analysis?.latestTraces || null,
  }), [analysis])

  const summary = useMemo(() => {
    const scored = nodes.filter((node) => node.score != null)
    const mean = scored.length
      ? scored.reduce((sum, node) => sum + node.score, 0) / scored.length
      : null
    const hot = scored.filter((node) => node.score >= 55).length
    const bias = scored.length ? Math.round((hot / scored.length) * 100) : null
    return { mean, bias, scored: scored.length }
  }, [nodes])

  const summaryCells = useMemo(() => buildStrategyCells({
    nodes,
    ordered,
    symbolStates,
    liveEvaluations,
    analysis,
    winRateText,
  }), [nodes, ordered, symbolStates, liveEvaluations, analysis, winRateText])

  const summaryPages = useMemo(() => packSummaryPages(summaryCells), [summaryCells])
  const [summaryPage, setSummaryPage] = useState(0)
  const summaryFade = reduced !== true && summaryPages.length > 1
  const summaryIndex = summaryPages.length ? summaryPage % summaryPages.length : 0

  useEffect(() => {
    if (!summaryFade) return undefined
    const timer = setInterval(() => {
      setSummaryPage((value) => value + 1)
    }, 3400)
    return () => clearInterval(timer)
  }, [summaryFade, summaryPages.length])

  useEffect(() => {
    const host = hostRef.current
    if (!host) return undefined
    let lastUi = 0
    const scene = createNebulaScene(host, {
      nodes,
      reduced,
      onFrame: (frame) => {
        const now = performance.now()
        if (now - lastUi < 48) return
        lastUi = now
        setOverlayNodes(frame.nodes || [])
        setIntakeTargets(frame.intakeTargets || [])
        if (frame.width && frame.height) {
          setFrameBox({ width: frame.width, height: frame.height })
        }
      },
    })
    sceneRef.current = scene
    return () => {
      scene.dispose()
      sceneRef.current = null
    }
    // mount once; nodes/reduced updated below
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    sceneRef.current?.setNodes(nodes)
  }, [nodes])

  useEffect(() => {
    sceneRef.current?.setReduced(reduced)
  }, [reduced])

  return (
    <section
      className={styles.root}
      aria-label="量化星图监控"
      data-ready={summary.scored > 0 || undefined}
    >
      <div ref={hostRef} className={styles.viewport} />

      {/* 可见性闸门用星云软包围盒。真正落点是下面带 data-symbol 的环节点。 */}
      <div className={styles.intake} data-metric-intake="" aria-hidden />

      {intakeTargets.map((target) => (
        <span
          key={target.id}
          className={styles.metricNode}
          data-metric-node=""
          data-symbol={target.symbol}
          data-node-id={target.id}
          style={{ left: `${target.x}px`, top: `${target.y}px` }}
          aria-hidden
        />
      ))}

      <LiquidGlass
        radius={16}
        bevelDepth={10}
        scale={16}
        aberration={0}
        frost={1.6}
        tint={0.34}
        className={styles.summary}
        contentClassName={styles.summaryIn}
        role="region"
        aria-label="策略概览"
        style={{ position: "absolute", top: 0, left: 0, margin: 0 }}
      >
        <div className={styles.summaryStage} data-fade={summaryFade || undefined} data-page={summaryIndex}>
          {summaryPages.map((page, index) => (
            <div
              key={page.map((cell) => cell.id).join("|")}
              className={styles.summaryPage}
              data-on={!summaryFade || index === summaryIndex || undefined}
              aria-hidden={summaryFade && index !== summaryIndex ? true : undefined}
            >
              {page.map((cell) => (
                <span
                  key={cell.id}
                  className={styles.summaryRow}
                  data-tone={cell.tone || undefined}
                  data-wide={cell.wide || undefined}
                >
                  <em>{cell.label}</em>
                  <strong>{cell.value}</strong>
                </span>
              ))}
            </div>
          ))}
        </div>
        <span className={styles.summaryFoot}>
          <em>策略概览</em>
          {summaryFade && (
            <span className={styles.summaryDots} aria-hidden>
              {summaryPages.map((page, index) => (
                <i key={page[0]?.id || index} data-on={index === summaryIndex || undefined} />
              ))}
            </span>
          )}
        </span>
      </LiquidGlass>

      {overlayNodes.filter((node) => node.visible).map((node) => {
        const live = nodeById.get(node.id)
        const placed = placePill(node.x, node.y, frameBox)
        return (
          <LiquidGlass
            key={node.id}
            radius={999}
            bevelDepth={8}
            scale={14}
            aberration={0}
            frost={1.4}
            tint={0.46}
            className={styles.node}
            contentClassName={styles.nodeIn}
            data-symbol={node.id}
            data-tone={node.score == null ? "idle" : node.score >= 55 ? "good" : node.score >= 45 ? "mid" : "weak"}
            style={{
              position: "absolute",
              left: `${placed.x}px`,
              top: `${placed.y}px`,
              "--node-color": node.color,
              zIndex: 8 + Math.round((node.depth ?? 0.5) * 10),
            }}
          >
            <CoinAvatar symbol={node.id} size={18} className={styles.nodeIcon} />
            <div>
              <b>{node.label}</b>
              <span>{pillCaption(live, node)}</span>
            </div>
          </LiquidGlass>
        )
      })}

      <span className={styles.srOnly} role="status" aria-live="polite">
        {`${ordered.length} 个币种在星图轨道上运行，已完成 ${summary.scored} 个评分。平均评分 ${summaryCells.find((cell) => cell.id === "mean")?.value || "—"}，偏多占比 ${summaryCells.find((cell) => cell.id === "bias")?.value || "—"}（评分不低于 55 的币种）。`}
      </span>
    </section>
  )
}
