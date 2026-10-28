/* 中间 50% 槽位：亮色轨道星云。
   左上角策略牌贴齐槽位，液态玻璃里滚动指标，不再留大标题。
   每个币一张玻璃胶囊，数字带中文指标名，并在同一张牌里轮换。
   飞线落在该币自己的环节点上。 */
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

const EMPTY_LIST = []
const EMPTY_MAP = {}
const RATE_KEYS = new Set(["winrate", "hitrate", "win_rate", "hit_rate"])
const SUMMARY_CLEAR = { right: 176, bottom: 96 }
const GLASS_WASH = "color-mix(in srgb, var(--accent) 32%, transparent)"

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
  return ""
}

function formatPct(value) {
  const n = number(value)
  if (n == null) return null
  const pct = Math.abs(n) <= 1 ? n * 100 : n
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`
}

function coinFields(node, state) {
  const dir = directionLabel(node.direction)
  const fields = []
  if (node.score != null) fields.push({ k: "评分", v: String(Math.round(node.score)), dir })
  const change = formatPct(state?.changePct ?? state?.pct)
  if (change) fields.push({ k: "涨跌", v: change })
  if (state?.rsi != null && Number.isFinite(Number(state.rsi))) {
    fields.push({ k: "RSI", v: String(Math.round(Number(state.rsi))) })
  }
  if (state?.adx != null && Number.isFinite(Number(state.adx))) {
    fields.push({ k: "ADX", v: String(Math.round(Number(state.adx))) })
  }
  if (state?.z != null && Number.isFinite(Number(state.z))) {
    fields.push({ k: "Z值", v: Number(state.z).toFixed(1) })
  }
  if (state?.roc != null && Number.isFinite(Number(state.roc))) {
    fields.push({ k: "ROC", v: `${(Number(state.roc) * 100).toFixed(2)}%` })
  }
  if (state?.bwPct != null && Number.isFinite(Number(state.bwPct))) {
    fields.push({ k: "BW", v: String(Math.round(Number(state.bwPct))) })
  }
  const pctB = state?.boll?.pctB
  if (pctB != null && Number.isFinite(Number(pctB))) {
    fields.push({ k: "%B", v: Number(pctB).toFixed(2) })
  }
  if (!fields.length) {
    const fallback = node.pct == null
      ? "—"
      : `${node.pct >= 0 ? "+" : ""}${Number(node.pct).toFixed(2)}%`
    fields.push({ k: node.score == null && node.pct != null ? "涨跌" : "评分", v: fallback, dir })
  }
  return fields
}

function averageOf(states, pick) {
  const values = states.map(pick).map(Number).filter(Number.isFinite)
  if (!values.length) return null
  return values.reduce((sum, value) => sum + value, 0) / values.length
}

function placePill(x, y, bounds) {
  const halfW = 74
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
  const [metricTick, setMetricTick] = useState(0)

  const traces = analysis?.decisionTraces ?? EMPTY_LIST
  const latestBySymbol = analysis?.latestTraces ?? EMPTY_MAP
  const liveEvaluations = analysis?.liveEvaluations ?? EMPTY_MAP
  const symbolStates = analysis?.symbols ?? EMPTY_MAP

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

  const meanText = summary.mean == null ? "—" : String(Math.round(summary.mean))
  const summaryRows = useMemo(() => {
    const states = ordered.map((symbol) => symbolStates[symbol]).filter(Boolean)
    const rows = [
      { k: "均分", v: meanText },
      { k: "已评", v: `${summary.scored}/${ordered.length}` },
      { k: "偏多", v: summary.bias == null ? "—" : `${summary.bias}%` },
      { k: "看涨", v: String(nodes.filter((node) => node.direction === "bullish").length) },
      { k: "看跌", v: String(nodes.filter((node) => node.direction === "bearish").length) },
    ]
    if (winRateText) rows.push({ k: "胜率", v: winRateText })
    const rsi = averageOf(states, (state) => state?.rsi)
    const adx = averageOf(states, (state) => state?.adx)
    const roc = averageOf(states, (state) => state?.roc)
    if (rsi != null) rows.push({ k: "RSI", v: String(Math.round(rsi)) })
    if (adx != null) rows.push({ k: "ADX", v: String(Math.round(adx)) })
    if (roc != null) rows.push({ k: "ROC", v: `${(roc * 100).toFixed(2)}%` })
    for (const node of nodes) {
      const state = symbolStates[node.id] || {}
      const bits = []
      if (node.score != null) bits.push(String(Math.round(node.score)))
      const dir = directionLabel(node.direction)
      if (dir) bits.push(dir)
      if (state.rsi != null && Number.isFinite(Number(state.rsi))) bits.push(`R${Math.round(Number(state.rsi))}`)
      rows.push({ k: node.label, v: bits.join(" ") || "—" })
    }
    return rows
  }, [meanText, summary, ordered, nodes, symbolStates, winRateText])

  useEffect(() => {
    if (reduced) return undefined
    const timer = setInterval(() => setMetricTick((value) => value + 1), 1800)
    return () => clearInterval(timer)
  }, [reduced])

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
        bevelDepth={8}
        scale={14}
        aberration={0}
        frost={1.6}
        tint={0.2}
        className={styles.summary}
        contentClassName={styles.summaryInner}
        data-strategy-summary=""
        aria-label={`策略概览 均分 ${meanText} 已评 ${summary.scored}/${ordered.length} 偏多 ${summary.bias == null ? "—" : `${summary.bias}%`}${winRateText ? ` 胜率 ${winRateText}` : ""}`}
        style={{ position: "absolute", top: 0, left: 0, margin: 0, backgroundColor: GLASS_WASH }}
      >
        <div className={styles.summaryClip} data-scroll={!reduced && summaryRows.length > 4 ? "y" : undefined}>
          <div
            className={styles.summaryTrack}
            style={{ "--cycle": `${Math.max(9, summaryRows.length * 1.25)}s` }}
          >
            {(reduced || summaryRows.length <= 4 ? summaryRows : summaryRows.concat(summaryRows)).map((row, index) => (
              <span className={styles.summaryRow} key={`${row.k}-${index}`}>
                <em>{row.k}</em>
                <strong>{row.v}</strong>
              </span>
            ))}
          </div>
        </div>
      </LiquidGlass>

      {overlayNodes.filter((node) => node.visible).map((node) => {
        const live = nodeById.get(node.id) || node
        const fields = coinFields(live, symbolStates[node.id])
        const field = fields[reduced ? 0 : (metricTick + hash(node.id)) % fields.length]
        const placed = placePill(node.x, node.y, frameBox)
        const tone = live.score == null ? "idle" : live.score >= 55 ? "good" : live.score >= 45 ? "mid" : "weak"
        return (
          <LiquidGlass
            key={node.id}
            radius={999}
            bevelDepth={8}
            scale={14}
            aberration={0}
            frost={1.6}
            tint={0.2}
            className={styles.node}
            contentClassName={styles.nodeIn}
            data-symbol={node.id}
            data-tone={tone}
            aria-label={`${live.label} 评分 ${live.score == null ? "—" : Math.round(live.score)}${directionLabel(live.direction) ? ` ${directionLabel(live.direction)}` : ""}`}
            style={{
              position: "absolute",
              left: `${placed.x}px`,
              top: `${placed.y}px`,
              "--node-color": node.color,
              backgroundColor: GLASS_WASH,
              zIndex: 8 + Math.round((node.depth ?? 0.5) * 10),
            }}
          >
            <CoinAvatar symbol={node.id} size={18} className={styles.nodeIcon} />
            <div>
              <b>{live.label}</b>
              <span className={styles.metricLine} key={fields.length > 1 ? `${field.k}-${metricTick}` : field.k}>
                <em>{field.k}</em>
                <strong>{field.v}</strong>
                {field.dir ? <i>· {field.dir}</i> : null}
              </span>
            </div>
          </LiquidGlass>
        )
      })}

      <span className={styles.srOnly} role="status" aria-live="polite">
        {`${ordered.length} 个币种在星图轨道上运行，已完成 ${summary.scored} 个评分，平均评分 ${meanText}，偏多 ${summary.bias == null ? "—" : `${summary.bias}%`}${winRateText ? `，胜率 ${winRateText}` : ""}。`}
      </span>
    </section>
  )
}
