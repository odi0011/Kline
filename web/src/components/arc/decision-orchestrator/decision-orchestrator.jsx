/* 中间 50% 槽位：亮色轨道星云。固定不滚动，飞线落在核心。 */
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

const BUBBLE_LINES = [
  "structure still cooking",
  "dead cat bounce. literally",
  "momentum not chill rn",
  "watching the gate",
  "flow says wait",
  "resonance warming up",
  "risk first, ego later",
  "orbit sync in progress",
]

function hash(text) {
  let value = 0
  for (const char of String(text)) value = (value * 31 + char.charCodeAt(0)) >>> 0
  return value
}

function formatFunds(value) {
  const n = number(value)
  if (n == null) return "--"
  return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function shortSym(symbol) {
  return String(symbol || "").replace(/usdt$/i, "").toUpperCase() || "—"
}

function bubbleAnchor(pos, center, bounds) {
  const width = bounds?.w || 0
  const height = bounds?.h || 0
  const cx = typeof center?.x === "number" ? center.x : pos.x
  const cy = typeof center?.y === "number" ? center.y : pos.y
  const dx = pos.x - cx
  const dy = pos.y - cy
  const dist = Math.hypot(dx, dy) || 1
  const minR = 108
  let x = dist >= minR ? pos.x : cx + (dx / dist) * minR
  let y = dist >= minR ? pos.y : cy + (dy / dist) * minR
  let side = y >= cy ? "below" : "above"
  if (height) {
    if (side === "below" && y > height - 78) side = "above"
    if (side === "above" && y < 72) side = "below"
    if (side === "below") y = Math.min(y, height - 76)
    else y = Math.max(y, 68)
  }
  if (width) x = Math.min(width - 100, Math.max(100, x))
  return { x, y, side }
}

export default function DecisionOrchestrator({
  analysis, symbols, events, structures, metricPulses = [], account = null,
}) {
  const reduced = useReducedMotion()
  const hostRef = useRef(null)
  const sceneRef = useRef(null)
  const [overlayNodes, setOverlayNodes] = useState([])
  const [center, setCenter] = useState({ x: "50%", y: "50%" })
  const [bubble, setBubble] = useState(null)
  const [tick, setTick] = useState(0)

  const traces = analysis?.decisionTraces || []
  const latestBySymbol = analysis?.latestTraces || {}
  const liveFeatures = analysis?.liveFeatures || {}
  const liveEvaluations = analysis?.liveEvaluations || {}
  const symbolStates = analysis?.symbols || {}

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

  const funds = number(account?.balance)
  const scoredCount = nodes.filter((node) => node.score != null).length
  const signalAge = latestSignal ? Date.now() - (number(latestSignal.ts, 0) || 0) : Infinity
  const signalActive = signalAge < 5000 ? latestSignal : null

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
        if (frame.center) setCenter(frame.center)
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

  useEffect(() => {
    if (reduced) return undefined
    const timer = setInterval(() => setTick((value) => value + 1), 2600)
    return () => clearInterval(timer)
  }, [reduced])

  useEffect(() => {
    if (reduced || !nodes.length) return undefined
    const pick = nodes[tick % nodes.length]
    const line = signalActive?.name_zh
      || BUBBLE_LINES[(tick + hash(pick.id)) % BUBBLE_LINES.length]
    setBubble({
      id: `${pick.id}-${tick}`,
      text: typeof line === "string" ? line : BUBBLE_LINES[tick % BUBBLE_LINES.length],
      label: pick.label,
      color: overlayNodes.find((node) => node.id === pick.id)?.color || "#38bdf8",
    })
    const clear = setTimeout(() => setBubble(null), 2800)
    return () => clearTimeout(clear)
  }, [tick, nodes, reduced, signalActive]) // eslint-disable-line react-hooks/exhaustive-deps

  const bubblePos = useMemo(() => {
    if (!bubble) return null
    return overlayNodes.find((node) => node.label === bubble.label && node.visible)
      || overlayNodes.find((node) => node.visible)
      || null
  }, [bubble, overlayNodes])

  const bubblePlace = bubble && bubblePos
    ? bubbleAnchor(bubblePos, center, {
      w: hostRef.current?.clientWidth || 0,
      h: hostRef.current?.clientHeight || 0,
    })
    : null

  return (
    <section
      className={styles.root}
      aria-label="量化星图监控"
      data-ready={scoredCount > 0 || undefined}
      data-signal={signalActive ? "y" : undefined}
    >
      <div ref={hostRef} className={styles.viewport} />

      <div
        className={styles.coreHit}
        data-metric-intake=""
        data-active={metricPulses.length ? "y" : undefined}
        style={{
          left: typeof center.x === "number" ? `${center.x}px` : center.x,
          top: typeof center.y === "number" ? `${center.y}px` : center.y,
        }}
        role="img"
        aria-label="数据核心"
      />

      <LiquidGlass
        radius={20}
        bevelDepth={12}
        scale={22}
        aberration={0}
        frost={2.2}
        tint={0.58}
        className={styles.funds}
        contentClassName={styles.fundsIn}
        style={{
          position: "absolute",
          left: typeof center.x === "number" ? `${center.x}px` : center.x,
          top: typeof center.y === "number" ? `${center.y}px` : center.y,
        }}
      >
        <span>总资金</span>
        <strong>{formatFunds(funds)}</strong>
        <em>{scoredCount}/{Math.max(1, ordered.length)} 已评分</em>
      </LiquidGlass>

      {overlayNodes.filter((node) => node.visible).map((node) => (
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
            left: `${node.x}px`,
            top: `${node.y}px`,
            "--node-color": node.color,
            zIndex: 8 + Math.round((node.depth ?? 0.5) * 10),
          }}
        >
          <CoinAvatar symbol={node.id} size={18} className={styles.nodeIcon} />
          <div>
            <b>{node.label}</b>
            <span>
              {node.pct == null
                ? "—"
                : `${node.pct >= 0 && node.score == null ? "+" : ""}${Number(node.pct).toFixed(node.score == null ? 2 : 0)}${node.score == null ? "%" : ""}`}
            </span>
          </div>
        </LiquidGlass>
      ))}

      {bubble && bubblePlace && (
        <LiquidGlass
          key={bubble.id}
          radius={16}
          bevelDepth={10}
          scale={16}
          aberration={0}
          frost={1.6}
          tint={0.5}
          className={styles.bubble}
          contentClassName={styles.bubbleIn}
          data-side={bubblePlace.side}
          style={{
            position: "absolute",
            left: `${bubblePlace.x}px`,
            top: `${bubblePlace.y}px`,
            "--bubble-color": bubble.color,
          }}
        >
          <div className={styles.bubbleHead}>
            <CoinAvatar symbol={bubblePos.id || bubble.label} size={14} />
            <b>{bubble.label}</b>
          </div>
          <span>{bubble.text}</span>
        </LiquidGlass>
      )}

      {/* structures reserved for future depth cues; keep prop for parent API */}
      <span className={styles.srOnly} role="status" aria-live="polite">
        {`${ordered.length} 个币种在星图轨道上运行，已完成 ${scoredCount} 个评分。总资金 ${formatFunds(funds)}。${structures ? "" : ""}`}
      </span>
    </section>
  )
}
