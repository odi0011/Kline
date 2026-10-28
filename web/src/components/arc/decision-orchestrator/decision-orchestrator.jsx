/* 中间 50% 槽位：亮色轨道星云。
   左上角小玻璃盒是策略概览（不再放总资金）。
   每个币只有一张玻璃胶囊：图标 + 代码 + 分数，方向写在同一行。
   飞线落点是星云上的随机小节点，不是画面正中。 */
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

function scoreText(node) {
  if (node.pct == null) return "—"
  const digits = node.score == null ? 2 : 0
  const sign = node.pct >= 0 && node.score == null ? "+" : ""
  const unit = node.score == null ? "%" : ""
  return `${sign}${Number(node.pct).toFixed(digits)}${unit}`
}

function directionLabel(direction) {
  if (direction === "bullish") return "看涨"
  if (direction === "bearish") return "看跌"
  return ""
}

const SUMMARY_CLEAR = { right: 180, bottom: 124 }

function placePill(x, y, bounds) {
  const halfW = 62
  const halfH = 22
  let nx = x
  let ny = y
  const overlaps = nx - halfW < SUMMARY_CLEAR.right && nx + halfW > 8
    && ny - halfH < SUMMARY_CLEAR.bottom && ny + halfH > 8
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

  const thirdLabel = winRateText ? "胜率" : "偏多占比"
  const thirdValue = winRateText || (summary.bias == null ? "—" : `${summary.bias}%`)
  const meanText = summary.mean == null ? "—" : String(Math.round(summary.mean))

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

      {/* 可见性闸门用星云软包围盒。真正落点是下面的 data-metric-node；还没有节点时才退回这个盒子的中心。 */}
      <div className={styles.intake} data-metric-intake="" aria-hidden />

      {intakeTargets.map((target) => (
        <span
          key={target.id}
          className={styles.metricNode}
          data-metric-node=""
          style={{ left: `${target.x}px`, top: `${target.y}px` }}
          aria-hidden
        />
      ))}

      <LiquidGlass
        radius={16}
        bevelDepth={12}
        scale={24}
        aberration={0}
        frost={2.4}
        tint={0.46}
        className={styles.summary}
        contentClassName={styles.summaryInner}
        aria-label="策略概览"
      >
        <span className={styles.summaryKicker}>策略概览</span>
        <span className={styles.summaryRow}>
          <em>平均评分</em>
          <strong>{meanText}</strong>
        </span>
        <span className={styles.summaryRow}>
          <em>已评分</em>
          <strong>{summary.scored}/{ordered.length}</strong>
        </span>
        <span
          className={styles.summaryRow}
          title={winRateText ? "分析数据中的胜率" : "评分不低于 55 的币种占比，不是胜率"}
        >
          <em>{thirdLabel}</em>
          <strong>{thirdValue}</strong>
        </span>
      </LiquidGlass>

      {overlayNodes.filter((node) => node.visible).map((node) => {
        const live = nodeById.get(node.id)
        const status = directionLabel(live?.direction)
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
              <span>
                {scoreText(node)}
                {status ? <i>{status}</i> : null}
              </span>
            </div>
          </LiquidGlass>
        )
      })}

      <span className={styles.srOnly} role="status" aria-live="polite">
        {`${ordered.length} 个币种在星图轨道上运行，已完成 ${summary.scored} 个评分，平均评分 ${meanText}，${thirdLabel} ${thirdValue}。`}
      </span>
    </section>
  )
}
