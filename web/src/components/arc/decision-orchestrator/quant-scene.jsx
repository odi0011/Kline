import { useEffect, useRef, useState } from "react"
import * as THREE from "three"
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js"
import styles from "./decision-orchestrator.module.css"

const TAU = Math.PI * 2
const RING_RADIUS = 3.2
const RING_Y = 0.62
const HISTORY_LIMIT = 48
const EVENT_TTL = 15000
export const numeric = (v) => v == null || v === "" || typeof v === "boolean" ? null : Number.isFinite(Number(v)) ? Number(v) : null
const clamp = (v, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, v))
const symKey = (v) => String(v || "").toLowerCase()
const stationAt = (deg) => [Math.cos(deg * Math.PI / 180) * RING_RADIUS, RING_Y, Math.sin(deg * Math.PI / 180) * RING_RADIUS]
const STATIONS = [
  { id: "intake", position: stationAt(170) },
  { id: "features", position: stationAt(242) },
  { id: "risk", position: stationAt(314) },
  { id: "execution", position: stationAt(26) },
  { id: "feedback", position: stationAt(98) },
  { id: "decision", position: [0, 1.3, 0] },
]

export const ringPoint = (progress, target) => {
  const angle = 170 * Math.PI / 180 + progress * TAU
  return target.set(Math.cos(angle) * RING_RADIUS, RING_Y, Math.sin(angle) * RING_RADIUS)
}

// quant._event has no id until storage attaches one. Keep its wire identity too.
export const eventKey = (e) => [e.ts, e.kind, symKey(e.symbol), e.account, e.orderId, e.reason].join("|")
const trimMap = (map, size) => { while (map.size > size) map.delete(map.keys().next().value) }
export const createEventGate = () => ({ ready: false, floor: 0, watermark: 0, snapshotTs: 0, seen: new Map(), levels: new Map(), positionKey: null, drawdown: null })
const fresh = (ts, now, floor) => numeric(ts) != null && ts >= floor && ts <= now + 1000 && now - ts <= EVENT_TTL

export function advanceEventGate(gate, { analysis = {}, snapshot = {}, symbols, events }, now = Date.now()) {
  analysis ||= {}; snapshot ||= {}
  const allowed = Array.isArray(symbols) ? new Set(symbols.map(symKey)) : null
  const source = [...(Array.isArray(events) ? events : []), ...(snapshot.events || [])]
    .filter((e) => e && (!e.symbol || !allowed || allowed.has(symKey(e.symbol))))
    .sort((a, b) => (numeric(a.ts) || 0) - (numeric(b.ts) || 0)).slice(-96)
  const initial = !gate.ready
  if (initial) gate.floor = now
  const emitted = []
  const oldWatermark = gate.watermark
  for (const event of source) {
    const key = eventKey(event)
    const id = event.id == null ? null : `id:${event.id}`
    const seen = gate.seen.has(key) || (id && gate.seen.has(id))
    gate.seen.set(key, true)
    if (id) gate.seen.set(id, true)
    if (numeric(event.ts) != null && event.ts <= now + 1000) gate.watermark = Math.max(gate.watermark, event.ts)
    if (!initial && !seen && fresh(event.ts, now, gate.floor) && event.ts >= oldWatermark
      && ["signal", "skip", "open", "close"].includes(event.kind)) {
      emitted.push({ ...event, key, mode: event.kind === "skip" ? "risk" : event.kind === "close" ? "feedback" : event.kind })
    }
  }
  trimMap(gate.seen, 256)
  const params = analysis.params || {}
  const liveKeys = new Set()
  for (const [symbol, evaluation] of Object.entries(analysis.liveEvaluations || {}).slice(0, 64)) {
    if (allowed && !allowed.has(symKey(symbol))) continue
    liveKeys.add(symbol)
    const state = analysis.symbols?.[symbol] || {}
    const score = numeric(evaluation.score), gap = numeric(evaluation.gap)
    const minScore = numeric(params.minScore), minGap = numeric(params.minScoreGap)
    const rsi = numeric(state.rsi), adx = numeric(state.adx)
    const low = numeric(params.rsiOversold), high = numeric(params.rsiOverbought), trend = numeric(params.adxTrend)
    const next = {
      ts: numeric(evaluation.ts),
      score: score == null || gap == null || minScore == null || minGap == null ? null : score >= minScore && gap >= minGap,
      rsi: rsi == null || low == null || high == null ? null : rsi <= low ? -1 : rsi >= high ? 1 : 0,
      trend: adx == null || trend == null ? null : adx >= trend,
      squeeze: typeof state.squeeze === "boolean" ? state.squeeze : null,
    }
    const prev = gate.levels.get(symbol)
    if (!initial && prev && next.ts > prev.ts && fresh(next.ts, now, gate.floor)) {
      const scoreCross = prev.score === false && next.score === true
      const factorCross = (prev.rsi != null && next.rsi != null && next.rsi !== 0 && prev.rsi !== next.rsi)
        || (prev.trend === false && next.trend === true) || (prev.squeeze === false && next.squeeze === true)
      if ((scoreCross || factorCross) && !emitted.some((e) => symKey(e.symbol) === symbol)) {
        emitted.push({ key: `threshold:${symbol}:${next.ts}`, ts: next.ts, symbol, mode: "signal", kind: "threshold", score: evaluation.score })
      }
    }
    // Late snapshots must not re-arm a threshold.
    if (!prev || next.ts >= prev.ts) gate.levels.set(symbol, next)
  }
  for (const key of gate.levels.keys()) if (!liveKeys.has(key)) gate.levels.delete(key)
  const dd = analysis.drawdown
  const snapshotCurrent = initial || (numeric(snapshot.ts) != null && snapshot.ts >= gate.snapshotTs && snapshot.ts <= now + 1000)
  const ddCurrent = numeric(dd?.current), ddLimit = numeric(dd?.limit)
  const blocked = dd ? Boolean(dd.paused) || (ddCurrent != null && ddLimit != null && ddLimit < 0 && ddCurrent <= ddLimit) : null
  if (snapshotCurrent && !initial && gate.drawdown === false && blocked === true && fresh(snapshot.ts, now, gate.floor)) {
    emitted.push({ key: `drawdown:${snapshot.ts}`, ts: snapshot.ts, mode: "risk", kind: "drawdown" })
  }
  if (snapshotCurrent && blocked != null) gate.drawdown = blocked
  if (snapshotCurrent && numeric(snapshot.ts) != null) gate.snapshotTs = Math.max(gate.snapshotTs, snapshot.ts)
  if (snapshotCurrent && Array.isArray(snapshot.positions)) {
    const positionKey = snapshot.positions.filter((p) => !allowed || allowed.has(symKey(p.symbol)))
      .map((p) => [p.account, p.symbol, p.entryTs, p.qty, p.side].join(":")).sort().join("|")
    if (!initial && gate.positionKey != null && gate.positionKey !== positionKey && fresh(snapshot.ts, now, gate.floor)
      && !emitted.some((e) => e.mode === "feedback" || e.mode === "open")) {
      emitted.push({ key: `positions:${snapshot.ts}`, ts: snapshot.ts, kind: "positions", mode: "feedback", symbol: snapshot.positions[0]?.symbol })
    }
    gate.positionKey = positionKey
  }
  gate.ready = true
  // This is a bounded live queue, never a historical event player.
  return emitted.slice(-6)
}

export function appendSample(history, ts, value, limit = HISTORY_LIMIT) {
  ts = numeric(ts); value = numeric(value)
  if (ts == null || value == null) return history
  const last = history[history.length - 1]
  if (last && ts < last.ts) return history
  if (last?.ts === ts) history[history.length - 1] = { ts, value }
  else history.push({ ts, value })
  if (history.length > limit) history.splice(0, history.length - limit)
  return history
}

export function deriveEquity(drawdown) {
  const peak = numeric(drawdown?.peak), current = numeric(drawdown?.current)
  // quant.py rounds peak/current; this is explicitly approximate, not an account balance feed.
  return peak != null && peak > 0 && current != null && current >= -100 && current <= 0 ? peak * (1 + current / 100) : null
}

export function telemetryFor(analysis = {}, snapshot = {}, symbols, focus, event, events = []) {
  analysis ||= {}; snapshot ||= {}
  const keys = [...new Set((Array.isArray(symbols) ? symbols : Object.keys(analysis.liveEvaluations || analysis.symbols || {})).map(symKey))].slice(0, 64)
  const symbol = keys.includes(symKey(focus)) ? symKey(focus) : keys[0] || ""
  const evaluation = analysis.liveEvaluations?.[symbol] || {}
  const market = analysis.symbols?.[symbol] || {}
  const positions = Array.isArray(snapshot.positions) ? snapshot.positions : []
  const held = positions.filter((p) => symKey(p.symbol) === symbol)
  const plan = analysis.positionPlans?.find((p) => symKey(p.symbol) === symbol) || {}
  const fills = []
  const seen = new Set()
  const addFill = (ts, price, key) => {
    if (seen.has(key) || numeric(ts) == null || numeric(price) == null) return
    seen.add(key); fills.push({ ts: Number(ts), value: Number(price) })
  }
  const relevant = [...(events || []), ...(snapshot.events || []), ...(event ? [event] : [])].filter((e) => symKey(e.symbol) === symbol)
  for (const e of relevant) if (e.kind === "open") addFill(e.ts, e.entry, `open:${e.account}:${e.orderId ?? e.ts}`)
  for (const p of held) addFill(p.entryTs, p.entry, `entry:${p.account}:${p.entryTs}`)
  for (const t of snapshot.trades || []) if (symKey(t.symbol) === symbol) {
    addFill(t.entryTs, t.entry, `entry:${t.account}:${t.entryTs}`)
    addFill(t.ts, t.exit, `exit:${t.account}:${t.closeId ?? t.ts}`)
  }
  fills.sort((a, b) => a.ts - b.ts)
  const uniqueFills = fills.filter((p, i) => !i || p.ts !== fills[i - 1].ts || p.value !== fills[i - 1].value).slice(-HISTORY_LIMIT)
  const latestOpen = relevant.filter((e) => e.kind === "open").sort((a, b) => b.ts - a.ts)[0]
  const execution = event?.kind === "open" && symKey(event.symbol) === symbol ? event : latestOpen || held[0] || {}
  const scores = []
  for (const point of (analysis.liveScoreHistory?.[symbol] || []).slice(-24)) appendSample(scores, point.ts, point.score, 24)
  const upnl = held.length && held.every((p) => numeric(p.upnl) != null) ? held.reduce((sum, p) => sum + Number(p.upnl), 0) : null
  return { symbol, keys, evaluation, market, plan, held, positions, execution, fills: uniqueFills, scores, upnl,
    equity: deriveEquity(analysis.drawdown), drawdown: analysis.drawdown || {}, params: analysis.params || {}, ts: numeric(snapshot.ts) }
}

// Time/value points remain piecewise linear. No procedural market curves or synthetic history.
export function normalizedSeries(samples) {
  const valid = samples.filter((p) => numeric(p.ts) != null && numeric(p.value) != null).slice(-HISTORY_LIMIT)
  if (!valid.length) return []
  const values = valid.map((p) => p.value), min = Math.min(...values), max = Math.max(...values)
  const start = valid[0].ts, span = valid[valid.length - 1].ts - start
  return valid.map((p) => ({ x: span > 0 ? (p.ts - start) / span : 0.5, y: max > min ? (p.value - min) / (max - min) : 0.5 }))
}

// ---- Three machinery: one owned resource registry and rigid instancing batches. ----
const makeQuartzTexture = () => {
  const canvas = document.createElement("canvas")
  canvas.width = canvas.height = 512
  const ctx = canvas.getContext("2d")
  ctx.fillStyle = "#faf9f5"; ctx.fillRect(0, 0, 512, 512)
  for (let i = 0; i < 24; i++) {
    ctx.strokeStyle = `rgba(145,156,165,${0.035 + i % 3 * 0.018})`; ctx.lineWidth = 0.5 + i % 2
    ctx.beginPath(); ctx.moveTo(-10, i * 24)
    for (let j = 0; j < 16; j++) ctx.lineTo(j * 36, i * 24 + Math.sin(j * 0.64 + i) * 18 + j * 2)
    ctx.stroke()
  }
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  return texture
}
const format = (v, digits = 2) => numeric(v) == null ? "—" : Math.abs(Number(v)) > 0 && Math.abs(Number(v)) < 0.01 ? Number(v).toPrecision(3) : Number(v).toLocaleString("en-US", { maximumFractionDigits: digits })
const pct = (v) => numeric(v) == null ? "—" : `${format(v)}%`
const shortSym = (symbol) => symbol ? symbol.toUpperCase() : "—"

export default function QuantScene({ analysis, snapshot, symbols, events, newEvents = [], enabled, reduced, metricPulses = [] }) {
  const hostRef = useRef(null), targetRef = useRef(null)
  const inputs = useRef({})
  const syncRef = useRef(null)
  const [failed, setFailed] = useState(false)
  inputs.current = { analysis, snapshot, symbols, events, newEvents, enabled, reduced, metricPulses }
  useEffect(() => { syncRef.current?.() }, [analysis, snapshot, symbols, events, newEvents, enabled, reduced, metricPulses])

  useEffect(() => {
    const host = hostRef.current, target = targetRef.current
    if (!host || !target) return
    // A remount must never reuse a canvas whose context was force-lost in cleanup.
    const canvas = document.createElement("canvas")
    canvas.className = styles.sceneCanvas
    canvas.setAttribute("aria-label", "实时评分、风控、成交与权益回流三维量化电路")
    host.insertBefore(canvas, target)
    let renderer
    try {
      renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "low-power" })
    } catch { canvas.remove(); setFailed(true); return }
    setFailed(false)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5))
    renderer.setClearColor(0x000000, 0)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.toneMappingExposure = 1.12
    renderer.shadowMap.enabled = true
    renderer.shadowMap.type = THREE.PCFShadowMap
    renderer.shadowMap.autoUpdate = false

    const geometries = new Set(), materials = new Set(), textures = new Set(), instanceMeshes = new Set()
    const geo = (g) => { geometries.add(g); return g }
    const mat = (m) => { materials.add(m); return m }
    const tex = (t) => { textures.add(t); return t }
    const scene = new THREE.Scene(), world = new THREE.Group()
    scene.add(world)
    const room = new RoomEnvironment()
    const pmrem = new THREE.PMREMGenerator(renderer)
    const environment = pmrem.fromScene(room, 0.04)
    room.dispose(); pmrem.dispose()
    scene.environment = environment.texture; scene.environmentIntensity = 0.55
    const camera = new THREE.OrthographicCamera(-6, 6, 4, -4, 0.1, 60)
    const hemisphere = new THREE.HemisphereLight(0xffffff, 0xcbd6de, 2.1)
    const key = new THREE.DirectionalLight(0xfffbef, 2.5)
    key.position.set(-4, 8, 5); key.castShadow = true
    key.shadow.mapSize.set(1024, 1024); key.shadow.bias = -0.0004
    Object.assign(key.shadow.camera, { left: -5, right: 5, top: 5, bottom: -5, near: 1, far: 22 })
    const rim = new THREE.DirectionalLight(0xb8e8ee, 1.6); rim.position.set(5, 4, -5)
    scene.add(hemisphere, key, rim)
    const standard = (color, roughness = 0.35, metalness = 0.65) => mat(new THREE.MeshStandardMaterial({ color, roughness, metalness }))
    const luminous = (color, opacity = 1) => mat(new THREE.MeshBasicMaterial({ color, transparent: opacity < 1, opacity, depthWrite: opacity === 1 }))
    const silver = standard(0xcbd4dc), dark = standard(0x25323d, 0.43, 0.48)
    const pcb = standard(0x124c4a, 0.5, 0.25), gold = standard(0xc5a361, 0.3, 0.85)
    const ceramic = standard(0xf4f4ed, 0.34, 0.2), black = standard(0x12232a, 0.55, 0.15)
    const teal = luminous(0x24caba), amber = luminous(0xf0b847), red = luminous(0xed7561)
    const dim = luminous(0x52696d), faint = luminous(0x58a99e, 0.25)
    const quartz = standard(0xffffff, 0.53, 0.05); quartz.map = tex(makeQuartzTexture())
    const glass = mat(new THREE.MeshStandardMaterial({ color: 0xb5e3dc, roughness: 0.16, metalness: 0.2, transparent: true, opacity: 0.22, depthWrite: false }))
    const box = geo(new THREE.BoxGeometry(1, 1, 1))
    const cylinder = geo(new THREE.CylinderGeometry(1, 1, 1, 32))
    const hex = geo(new THREE.CylinderGeometry(1, 1, 1, 6))
    const sphere = geo(new THREE.IcosahedronGeometry(1, 2))
    const torusCache = new Map()
    const torus = (radius, tube = 0.012, arc = TAU) => {
      const key = `${radius}:${tube}:${arc}`
      if (!torusCache.has(key)) torusCache.set(key, geo(new THREE.TorusGeometry(radius, tube, 6, 64, arc)))
      return torusCache.get(key)
    }
    const moving = []
    const group = (parent, position = [0, 0, 0], movable = false) => {
      const g = new THREE.Group(); g.position.set(...position); parent.add(g)
      if (movable) { g.userData.movable = true; moving.push(g) }
      return g
    }
    const mesh = (parent, geometry, material, position = [0, 0, 0], scale = [1, 1, 1], rotation) => {
      const m = new THREE.Mesh(geometry, material); m.position.set(...position); m.scale.set(...scale)
      if (rotation) m.rotation.set(...rotation)
      m.castShadow = !material.isMeshBasicMaterial && !material.transparent; m.receiveShadow = m.castShadow
      parent.add(m); return m
    }
    const block = (p, m, pos, scale, rot) => mesh(p, box, m, pos, scale, rot)
    const disc = (p, m, pos, r, h, sides = cylinder) => mesh(p, sides, m, pos, [r, h, r])
    const ring = (p, m, pos, r, tube = 0.012, rotation = [Math.PI / 2, 0, 0]) => mesh(p, torus(r, tube), m, pos, [1, 1, 1], rotation)
    const rod = (p, a, b, material = gold, radius = 0.014) => {
      const from = new THREE.Vector3(...a), to = new THREE.Vector3(...b), delta = to.sub(from)
      const m = mesh(p, cylinder, material, from.addScaledVector(delta, 0.5).toArray(), [radius, delta.length(), radius])
      m.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), delta.normalize()); return m
    }
    const radial = (p, n, radius, y, material, size, offset = 0) => {
      for (let i = 0; i < n; i++) {
        const a = i / n * TAU + offset
        block(p, material, [Math.cos(a) * radius, y, Math.sin(a) * radius], size, [0, -a, 0])
      }
    }
    const board = (p, width, depth, y = 0) => {
      block(p, pcb, [0, y, 0], [width, 0.055, depth])
      block(p, gold, [0, y - 0.035, 0], [width + 0.025, 0.012, depth + 0.025])
      // Address/data buses, gold edge fingers, decoupling capacitors and solder vias.
      for (let i = 0; i < 12; i++) {
        const x = (i / 11 - 0.5) * width * 0.85
        block(p, gold, [x, y + 0.03, depth * 0.43], [0.018, 0.006, 0.16])
        block(p, gold, [x, y + 0.03, -depth * 0.43], [0.018, 0.006, 0.16])
        block(p, i % 3 ? ceramic : black, [x, y + 0.062, depth * 0.26], [0.04, 0.045, 0.055])
        block(p, gold, [x, y + 0.033, 0], [0.006, 0.005, depth * 0.7])
      }
      for (const x of [-1, 1]) for (const z of [-1, 1]) disc(p, silver, [x * width * 0.43, y + 0.035, z * depth * 0.36], 0.025, 0.016, hex)
    }
    const fan = (parent, position, radius = 0.13) => {
      const rotor = group(parent, position, true)
      disc(rotor, dark, [0, 0, 0], radius, 0.03)
      radial(rotor, 9, radius * 0.62, 0.025, silver, [radius * 0.7, 0.012, 0.038], 0.25)
      disc(rotor, gold, [0, 0.04, 0], radius * 0.22, 0.04)
      ring(parent, dark, position, radius + 0.015, 0.016)
      return rotor
    }

    // Continuous white quartz chassis, radial bridge backplanes and engraved contacts.
    disc(world, silver, [0, -0.13, 0], 4.34, 0.16)
    disc(world, quartz, [0, -0.035, 0], 4.29, 0.07)
    ring(world, gold, [0, -0.02, 0], 4.3, 0.024)
    ring(world, silver, [0, 0.012, 0], 4.07, 0.009)
    radial(world, 120, 4.14, 0.014, gold, [0.055, 0.007, 0.009])
    ring(world, dark, [0, 0.095, 0], 2.91, 0.03)
    ring(world, faint, [0, RING_Y, 0], RING_RADIUS, 0.017)
    ring(world, gold, [0, 0.1, 0], 1.08, 0.012)
    for (let i = 0; i < 40; i++) {
      const a = i / 40 * TAU, r = i % 2 ? 2.25 : 1.75
      rod(world, [Math.cos(a) * 1.1, 0.013, Math.sin(a) * 1.1], [Math.cos(a) * r, 0.013, Math.sin(a) * r], i % 3 ? gold : silver, 0.005)
    }
    const bridges = new Map()
    for (const def of STATIONS.filter((d) => d.id !== "decision")) {
      const [x, , z] = def.position, angle = Math.atan2(z, x)
      const bridge = group(world, [Math.cos(angle) * 1.97, 0.12, Math.sin(angle) * 1.97])
      bridge.rotation.y = -angle
      block(bridge, ceramic, [0, 0, 0], [1.7, 0.09, 0.27])
      block(bridge, pcb, [0, 0.055, 0], [1.65, 0.02, 0.22])
      for (const side of [-1, 1]) block(bridge, gold, [0, 0.072, side * 0.075], [1.66, 0.012, 0.015])
      for (let i = 0; i < 16; i++) block(bridge, silver, [-0.76 + i * 0.1, 0.09, 0], [0.034, 0.025, 0.08])
      bridges.set(def.id, { angle })
    }

    const nodes = new Map(), fans = [], decks = [], petals = [], nozzlePetals = []
    const plaques = []
    const atlas = document.createElement("canvas"); atlas.width = 1024; atlas.height = 1024
    const atlasCtx = atlas.getContext("2d")
    const atlasTexture = tex(new THREE.CanvasTexture(atlas)); atlasTexture.colorSpace = THREE.SRGBColorSpace
    atlasTexture.minFilter = THREE.LinearFilter; atlasTexture.generateMipmaps = false
    const plaqueMaterial = mat(new THREE.MeshBasicMaterial({ map: atlasTexture, transparent: true, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }))
    const plaque = (parent, tile, position, width = 1.43) => {
      const g = geo(new THREE.PlaneGeometry(width, width * 0.5))
      const uv = g.attributes.uv
      for (let i = 0; i < uv.count; i++) uv.setXY(i, (tile % 2 + uv.getX(i)) / 2, (3 - Math.floor(tile / 2) + uv.getY(i)) / 4)
      const holder = group(parent, position, true)
      const plane = mesh(holder, g, plaqueMaterial); plane.renderOrder = 8
      plaques.push(holder); return holder
    }
    let receiver, decisionCore, decisionCage, nozzle, jet, feedbackRotor, intakeRotor
    const gimbals = []
    for (const [index, def] of STATIONS.entries()) {
      const node = group(world, def.position), visual = group(node)
      nodes.set(def.id, { node, visual })
      disc(node, dark, [0, -def.position[1] + 0.085, 0], def.id === "decision" ? 0.66 : 0.58, 0.12, hex)
      disc(node, silver, [0, -def.position[1] + 0.16, 0], 0.46, 0.045)
      ring(node, def.id === "risk" ? amber : teal, [0, -def.position[1] + 0.2, 0], 0.43, 0.013)
      for (let i = 0; i < 4; i++) {
        const a = i * Math.PI / 2 + Math.PI / 4
        rod(node, [Math.cos(a) * 0.32, -def.position[1] + 0.18, Math.sin(a) * 0.32], [Math.cos(a) * 0.32, -0.13, Math.sin(a) * 0.32], silver, 0.022)
      }
      plaque(node, index, [0, def.id === "decision" ? 1.18 : 1.2, 0], def.id === "decision" ? 1.6 : 1.43)
      if (def.id === "intake") {
        board(visual, 0.82, 0.64, -0.22)
        for (let i = 0; i < 3; i++) {
          block(visual, silver, [-0.25 + i * 0.25, -0.1, 0.18], [0.19, 0.16, 0.22])
          for (let j = 0; j < 5; j++) block(visual, dark, [-0.32 + i * 0.25 + j * 0.035, -0.01, 0.18], [0.012, 0.07, 0.2])
        }
        intakeRotor = group(visual, [0, 0.12, 0], true)
        intakeRotor.rotation.x = 0.35
        const profile = Array.from({ length: 19 }, (_, i) => { const r = i / 18 * 0.46; return new THREE.Vector2(r, r * r * 1.3) })
        const dishMat = standard(0xe5e9e9, 0.26, 0.85); dishMat.side = THREE.DoubleSide
        mesh(intakeRotor, geo(new THREE.LatheGeometry(profile, 48)), dishMat)
        ring(intakeRotor, gold, [0, 0.275, 0], 0.46, 0.017)
        radial(intakeRotor, 24, 0.43, 0.253, silver, [0.026, 0.026, 0.045])
        for (let i = 0; i < 3; i++) {
          const a = i / 3 * TAU
          rod(intakeRotor, [Math.cos(a) * 0.4, 0.22, Math.sin(a) * 0.4], [0, 0.43, 0], gold, 0.009)
        }
        disc(intakeRotor, dark, [0, 0.36, 0], 0.05, 0.12)
        receiver = mesh(intakeRotor, sphere, teal, [0, 0.44, 0], [0.046, 0.046, 0.046])
        fans.push(fan(visual, [-0.32, -0.11, -0.22], 0.09))
      }
      if (def.id === "features") {
        for (let i = 0; i < 3; i++) {
          const deck = group(visual, [0, -0.17 + i * 0.24, 0], true)
          board(deck, 0.85, 0.7)
          block(deck, dark, [0, 0.075, -0.055], [0.36, 0.08, 0.32])
          block(deck, i === 1 ? gold : silver, [0, 0.13, -0.055], [0.25, 0.025, 0.23])
          for (let p = 0; p < 8; p++) for (const side of [-1, 1]) block(deck, silver, [side * 0.21, 0.07, -0.2 + p * 0.044], [0.08, 0.016, 0.02])
          for (let f = 0; f < 7; f++) block(deck, silver, [-0.31 + f * 0.1, 0.09, -0.3], [0.02, 0.11, 0.085])
          decks.push(deck)
          if (i === 2) fans.push(fan(deck, [0.29, 0.1, -0.05], 0.1))
        }
        for (const x of [-0.47, 0.47]) for (const z of [-0.36, 0.36]) rod(visual, [x, -0.3, z], [x, 0.7, z], silver, 0.014)
      }
      if (def.id === "decision") {
        disc(visual, dark, [0, -0.74, 0], 0.71, 0.15)
        radial(visual, 36, 0.64, -0.65, silver, [0.14, 0.15, 0.024])
        ring(visual, gold, [0, -0.56, 0], 0.64, 0.025)
        for (let i = 0; i < 8; i++) {
          const a = i / 8 * TAU
          rod(visual, [Math.cos(a) * 0.6, -0.56, Math.sin(a) * 0.6], [Math.cos(a) * 0.37, -0.25, Math.sin(a) * 0.37], silver, 0.025)
        }
        const coreMat = mat(new THREE.MeshStandardMaterial({ color: 0xf2efe0, roughness: 0.22, metalness: 0.4, emissive: 0x4da596, emissiveIntensity: 0.12 }))
        const spin = group(visual, [0, 0, 0], true)
        decisionCore = mesh(spin, sphere, coreMat, [0, 0, 0], [0.39, 0.39, 0.39])
        decisionCage = mesh(spin, geo(new THREE.IcosahedronGeometry(0.46, 1)), mat(new THREE.MeshBasicMaterial({ color: 0x977d46, wireframe: true, transparent: true, opacity: 0.55 })))
        for (let i = 0; i < 3; i++) {
          const pivot = group(visual, [0, 0, 0], true)
          const assembly = group(pivot); assembly.rotation.set(0.35 + i * 0.67, 0.2, i * 0.6)
          ring(assembly, i === 1 ? silver : gold, [0, 0, 0], 0.64 + i * 0.095, 0.018, [0, 0, 0])
          for (let j = 0; j < 28; j++) {
            const a = j / 28 * TAU, r = 0.64 + i * 0.095
            block(assembly, dark, [Math.cos(a) * r, Math.sin(a) * r, 0], [0.035, 0.05, 0.035], [0, 0, a])
          }
          gimbals.push(pivot)
        }
        // Two independently valued score arcs, not a decorative random progress gauge.
        for (const [side, material] of [[-1, teal], [1, amber]]) {
          const arc = mesh(visual, geo(new THREE.TorusGeometry(0.54, 0.013, 6, 80, Math.PI)), material, [0, -0.02, 0], [1, 1, 1], [Math.PI / 2, 0, side < 0 ? 0 : Math.PI])
          arc.userData.scoreSide = side; nodes.get(def.id)[side < 0 ? "bullArc" : "bearArc"] = arc
          arc.userData.movable = true
        }
      }
      if (def.id === "risk") {
        const gate = group(visual, [0, 0.2, 0]); gate.rotation.y = 0.72
        ring(gate, silver, [0, 0, 0], 0.61, 0.037, [0, 0, 0])
        ring(gate, amber, [0, 0, 0.055], 0.55, 0.012, [0, 0, 0])
        for (let i = 0; i < 6; i++) {
          const a = i / 6 * TAU
          const section = group(gate, [0, 0, 0], true)
          const shape = new THREE.Shape()
          shape.moveTo(0.025 * Math.cos(a + Math.PI / 6), 0.025 * Math.sin(a + Math.PI / 6))
          shape.lineTo(0.48 * Math.cos(a + 0.035), 0.48 * Math.sin(a + 0.035))
          shape.lineTo(0.48 * Math.cos(a + Math.PI / 3 - 0.035), 0.48 * Math.sin(a + Math.PI / 3 - 0.035)); shape.closePath()
          const plate = geo(new THREE.ExtrudeGeometry(shape, { depth: 0.095, bevelEnabled: true, bevelSize: 0.009, bevelThickness: 0.008, bevelSegments: 1, steps: 1 }))
          mesh(section, plate, ceramic)
          const middle = a + Math.PI / 6
          block(section, gold, [Math.cos(middle) * 0.3, Math.sin(middle) * 0.3, 0.115], [0.14, 0.026, 0.018], [0, 0, middle])
          block(section, dark, [Math.cos(middle) * 0.4, Math.sin(middle) * 0.4, 0.116], [0.06, 0.036, 0.016], [0, 0, middle])
          petals.push({ section, angle: middle })
          rod(gate, [Math.cos(middle) * 0.52, Math.sin(middle) * 0.52, -0.1], [Math.cos(middle) * 0.74, Math.sin(middle) * 0.74, -0.1], silver, 0.018)
        }
        board(visual, 0.86, 0.63, -0.25)
        for (const x of [-0.5, 0.5]) block(visual, dark, [x, 0, 0], [0.07, 0.6, 0.19])
      }
      if (def.id === "execution") {
        board(visual, 0.8, 0.73, -0.21)
        nozzle = group(visual, [0, 0.16, 0], true); nozzle.rotation.z = -0.23
        const profile = [[0.12, 0.34], [0.17, 0.18], [0.13, 0.02], [0.2, -0.12], [0.34, -0.24]].map(([x, y]) => new THREE.Vector2(x, y))
        const nozzleMat = standard(0xaabac2, 0.22, 0.88); nozzleMat.side = THREE.DoubleSide
        mesh(nozzle, geo(new THREE.LatheGeometry(profile, 48)), nozzleMat)
        for (let i = 0; i < 4; i++) ring(nozzle, gold, [0, 0.12 + i * 0.055, 0], 0.155 - i * 0.007, 0.012)
        for (let i = 0; i < 6; i++) {
          const a = i / 6 * TAU, petal = group(nozzle, [0, 0, 0], true)
          block(petal, dark, [Math.cos(a) * 0.27, -0.08, Math.sin(a) * 0.27], [0.09, 0.27, 0.085], [0, -a, -0.18])
          nozzlePetals.push({ section: petal, angle: a })
        }
        for (const x of [-0.4, 0.4]) {
          disc(visual, silver, [x, 0.02, 0.04], 0.11, 0.42)
          ring(visual, gold, [x, 0.15, 0.04], 0.115, 0.014)
          rod(visual, [x, 0.25, 0.04], [0, 0.34, 0], gold, 0.026)
        }
        const jetRoot = group(nozzle, [0, -0.28, 0], true)
        jet = mesh(jetRoot, geo(new THREE.ConeGeometry(0.21, 0.44, 24, 1, true)), mat(new THREE.MeshBasicMaterial({ color: 0x24d6bd, transparent: true, opacity: 0, depthWrite: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending })), [0, -0.12, 0])
        fans.push(fan(visual, [0, -0.08, -0.29], 0.1))
      }
      if (def.id === "feedback") {
        board(visual, 0.84, 0.75, -0.22)
        feedbackRotor = group(visual, [0, 0.12, 0], true)
        for (let i = 0; i < 3; i++) {
          ring(feedbackRotor, silver, [0, i * 0.12, 0], 0.4, 0.026)
          radial(feedbackRotor, 24, 0.36, i * 0.12, i === 1 ? gold : dark, [0.08, 0.055, 0.045])
        }
        disc(feedbackRotor, dark, [0, 0.1, 0], 0.17, 0.43)
        ring(feedbackRotor, teal, [0, 0.34, 0], 0.18, 0.018)
        for (const x of [-0.49, 0.49]) {
          block(visual, silver, [x, 0.19, 0], [0.08, 0.5, 0.23])
          block(visual, black, [x, 0.22, 0.15], [0.12, 0.33, 0.045])
          for (let j = 0; j < 5; j++) block(visual, gold, [x, 0.09 + j * 0.06, 0.178], [0.055, 0.012, 0.012])
        }
      }
    }

    // All repeated connectors, traces, fins and chassis parts share geometry/material batches.
    const batchRigid = (root) => {
      root.updateWorldMatrix(true, true)
      const inverse = root.matrixWorld.clone().invert(), batches = new Map()
      const collect = (object) => {
        if (object !== root && object.userData.movable) return
        if (object.isMesh && !object.isInstancedMesh) {
          const id = `${object.geometry.uuid}:${object.material.uuid}`
          if (!batches.has(id)) batches.set(id, [])
          batches.get(id).push(object)
        }
        for (const child of object.children) collect(child)
      }
      collect(root)
      for (const list of batches.values()) {
        if (list.length < 2) continue
        const inst = new THREE.InstancedMesh(list[0].geometry, list[0].material, list.length)
        instanceMeshes.add(inst)
        list.forEach((m, i) => { inst.setMatrixAt(i, new THREE.Matrix4().multiplyMatrices(inverse, m.matrixWorld)); m.removeFromParent() })
        inst.castShadow = list[0].castShadow; inst.receiveShadow = list[0].receiveShadow
        inst.computeBoundingSphere(); root.add(inst)
      }
    }
    for (const assembly of moving) batchRigid(assembly)
    batchRigid(world)

    const leds = new THREE.InstancedMesh(box, mat(new THREE.MeshBasicMaterial({ color: 0xffffff })), 6 * 16)
    instanceMeshes.add(leds); world.add(leds)
    const dummy = new THREE.Object3D(), ledColor = new THREE.Color()
    STATIONS.forEach((def, station) => {
      for (let i = 0; i < 16; i++) {
        dummy.position.set(def.position[0] - 0.34 + (i % 8) * 0.097, def.position[1] - 0.16 + Math.floor(i / 8) * 0.07, def.position[2] + 0.38)
        dummy.scale.set(0.055, 0.025, 0.025); dummy.updateMatrix(); leds.setMatrixAt(station * 16 + i, dummy.matrix); leds.setColorAt(station * 16 + i, ledColor.set(0x3a4b51))
      }
    })
    leds.instanceMatrix.needsUpdate = true

    const makeDataLine = (material, parent = world) => {
      const geometry = geo(new THREE.BufferGeometry())
      geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(HISTORY_LIMIT * 3), 3).setUsage(THREE.DynamicDrawUsage))
      geometry.setDrawRange(0, 0)
      const line = new THREE.Line(geometry, material); line.frustumCulled = false; line.renderOrder = 4; parent.add(line)
      const dot = mesh(parent, sphere, material, [0, 0, 0], [0.032, 0.032, 0.032]); dot.visible = false
      return { line, dot, count: 0, revealed: 1 }
    }
    const scoreLine = makeDataLine(teal, nodes.get("features").visual)
    const fillLine = makeDataLine(amber, nodes.get("execution").visual)
    const equityLine = makeDataLine(teal), positionLine = makeDataLine(amber)
    const updateLine = (plot, history, mapper) => {
      const normalized = normalizedSeries(history), attr = plot.line.geometry.attributes.position
      normalized.forEach((p, i) => { const xyz = mapper(p); attr.setXYZ(i, ...xyz) })
      attr.needsUpdate = true; plot.count = normalized.length
      const count = Math.min(plot.count, Math.max(1, Math.ceil(plot.count * plot.revealed)))
      plot.line.geometry.setDrawRange(0, count)
      plot.dot.visible = plot.count > 0
      if (count) plot.dot.position.fromBufferAttribute(attr, count - 1)
    }
    const particles = new THREE.InstancedMesh(sphere, mat(new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.8, depthWrite: false })), 32 * 5)
    instanceMeshes.add(particles); particles.instanceMatrix.setUsage(THREE.DynamicDrawUsage); particles.count = 0; particles.frustumCulled = false; world.add(particles)
    const injected = [], pendingPulses = new Map(), handledEvents = new Map(), queue = []
    const histories = new Map(), equityHistory = []
    let focus = "", activeEvent = null, episode = null, data = null, lastBatch = null
    let time = 0, lastFrame = 0, lastPaint = -Infinity, frame = 0, timer = 0, disposed = false
    let width = 1, height = 1, visible = true, running = false, lost = false, dirty = true, lastTexture = -Infinity, lastSignature = ""
    let lastShadow = -Infinity, lastTelemetry = null
    let azimuth = 0.72, elevation = 0.62, cameraStrength = 0
    const projection = new THREE.Vector3(), cameraTarget = new THREE.Vector3(), scratch = new THREE.Vector3()
    const cameraGoal = { signal: [-0.24, 0.06], risk: [0.26, 0.035], open: [0.16, -0.055], feedback: [-0.16, 0.025] }
    const setCamera = (az, el) => {
      camera.position.set(Math.sin(az) * Math.cos(el) * 12, 0.7 + Math.sin(el) * 12, Math.cos(az) * Math.cos(el) * 12)
      camera.lookAt(cameraTarget.set(0, 0.7, 0)); camera.updateMatrixWorld(true)
    }
    setCamera(azimuth, elevation)

    // Fit the whole chassis AND all expanded hardware at every allowed orbit angle.
    // Frustum remains constant during events: the camera only orbits, never zooms/crops.
    const fit = () => {
      width = Math.max(1, host.clientWidth); height = Math.max(1, host.clientHeight)
      const points = []
      for (let i = 0; i < 64; i++) points.push(new THREE.Vector3(Math.cos(i / 64 * TAU) * 4.43, -0.23, Math.sin(i / 64 * TAU) * 4.43))
      for (const def of STATIONS) for (let i = 0; i < 24; i++) for (const y of [0.02, def.id === "decision" ? 2.16 : 1.62]) {
        points.push(new THREE.Vector3(def.position[0] + Math.cos(i / 24 * TAU) * 0.9, y, def.position[2] + Math.sin(i / 24 * TAU) * 0.9))
      }
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
      const include = (p, dx = 0, dy = 0) => {
        minX = Math.min(minX, p.x - dx); maxX = Math.max(maxX, p.x + dx)
        minY = Math.min(minY, p.y - dy); maxY = Math.max(maxY, p.y + dy)
      }
      for (const az of [0.46, 0.59, 0.72, 0.85, 0.98]) for (const el of [0.565, 0.62, 0.68]) {
        setCamera(az, el)
        for (const point of points) include(scratch.copy(point).applyMatrix4(camera.matrixWorldInverse))
        for (const def of STATIONS) {
          const halfWidth = def.id === "decision" ? 0.8 : 0.715
          scratch.set(def.position[0], def.id === "decision" ? 2.48 : 1.82, def.position[2]).applyMatrix4(camera.matrixWorldInverse)
          include(scratch, halfWidth, halfWidth / 2)
        }
      }
      const aspect = width / height, halfH = Math.max((maxY - minY) / 2 + 0.12, ((maxX - minX) / 2 + 0.12) / aspect)
      const centerX = (minX + maxX) / 2, centerY = (minY + maxY) / 2
      camera.left = centerX - halfH * aspect; camera.right = centerX + halfH * aspect
      camera.top = centerY + halfH; camera.bottom = centerY - halfH
      camera.updateProjectionMatrix(); setCamera(azimuth, elevation)
      renderer.setSize(width, height, false); renderer.shadowMap.needsUpdate = true
    }
    const canMove = () => !lost && visible && !document.hidden && !inputs.current.reduced && inputs.current.enabled !== false
    const historyFor = (symbol) => {
      if (!histories.has(symbol)) histories.set(symbol, { marks: [] })
      trimMap(histories, 64); return histories.get(symbol)
    }
    const refreshData = () => {
      const value = inputs.current
      data = telemetryFor(value.analysis, value.snapshot, value.symbols, focus, activeEvent, value.events)
      focus = data.symbol
      const live = new Set(data.keys)
      for (const key of histories.keys()) if (!live.has(key)) histories.delete(key)
      if (lastTelemetry !== value.snapshot) {
        appendSample(equityHistory, data.ts, data.equity)
        for (const symbol of data.keys) {
          const held = value.snapshot?.positions?.filter((p) => symKey(p.symbol) === symbol) || []
          const h = historyFor(symbol)
          const identity = held.map((p) => `${p.account}:${p.entryTs}`).sort().join("|")
          if (h.identity !== identity) { h.marks.length = 0; h.identity = identity }
          const mark = held.find((p) => numeric(p.mark) != null)?.mark
          appendSample(h.marks, data.ts, mark)
        }
        lastTelemetry = value.snapshot
      }
      updateLine(scoreLine, data.scores, (p) => [-0.5 + p.x, 0.42 + p.y * 0.32, 0.34])
      updateLine(fillLine, data.fills, (p) => [-0.57 + p.x * 1.14, -0.02 + p.y * 0.48, 0.49])
      const feedback = STATIONS.find((s) => s.id === "feedback").position
      const returnPath = (offset) => (p) => {
        const t = p.x, r = 3.0 - t * 2.12, a = 98 * Math.PI / 180 + Math.sin(t * Math.PI) * 0.16 + offset
        return [Math.cos(a) * r, 0.29 + p.y * 0.28, Math.sin(a) * r]
      }
      updateLine(equityLine, equityHistory, returnPath(-0.045))
      updateLine(positionLine, historyFor(focus).marks, returnPath(0.045))
      // The origin is mechanically connected to feedback's dual-channel recorder.
      equityLine.line.userData.origin = feedback
      const strengths = [numeric(data.market.rsi), numeric(data.evaluation.score),
        numeric(data.drawdown.current) != null && numeric(data.drawdown.limit) < 0 ? Math.abs(data.drawdown.current / data.drawdown.limit) * 100 : null,
        numeric(data.execution.qty) != null ? 100 : null,
        numeric(data.params.maxPositions) > 0 ? data.positions.length / data.params.maxPositions * 100 : null,
        numeric(data.evaluation.bull)]
      strengths.forEach((value, station) => {
        for (let i = 0; i < 16; i++) {
          const lit = value != null && i < Math.round(clamp(value / 100) * 16)
          leds.setColorAt(station * 16 + i, ledColor.set(lit ? station === 2 ? 0xf0b847 : 0x24caba : 0x344a50))
        }
      })
      leds.instanceColor.needsUpdate = true
      const decision = nodes.get("decision")
      for (const [name, value] of [["bullArc", data.evaluation.bull], ["bearArc", data.evaluation.bear]]) {
        const arc = decision[name], count = arc.geometry.index.count
        arc.geometry.setDrawRange(0, numeric(value) == null ? 0 : Math.floor(count * clamp(Number(value) / 100) / 6) * 6)
      }
      dirty = true
    }
    const chart = (samples, x, y, w, h, color) => {
      const points = normalizedSeries(samples)
      if (!points.length) return
      atlasCtx.strokeStyle = "#304b50"; atlasCtx.lineWidth = 1
      atlasCtx.beginPath(); atlasCtx.moveTo(x, y + h); atlasCtx.lineTo(x + w, y + h); atlasCtx.stroke()
      atlasCtx.strokeStyle = color; atlasCtx.fillStyle = color; atlasCtx.lineWidth = 3
      atlasCtx.beginPath()
      points.forEach((p, i) => { const px = x + p.x * w, py = y + (1 - p.y) * h; if (i) atlasCtx.lineTo(px, py); else atlasCtx.moveTo(px, py) })
      atlasCtx.stroke()
      for (const p of points) { atlasCtx.beginPath(); atlasCtx.arc(x + p.x * w, y + (1 - p.y) * h, 2.5, 0, TAU); atlasCtx.fill() }
    }
    const paintTelemetry = (now) => {
      if (!dirty || now - lastTexture < 300 || !data) return
      const e = data.evaluation, m = data.market, dd = data.drawdown, ex = data.execution, symbol = shortSym(data.symbol)
      const mode = episode?.event.mode
      const rows = [
        [symbol, `PX ${format(m.price, 5)}`, `RSI ${format(m.rsi, 1)}  ·  ADX ${format(m.adx, 1)}`, [], "#5ce0c5"],
        [symbol, `SCORE ${format(e.score, 1)}`, `B ${format(e.bull, 1)} / S ${format(e.bear, 1)}  ·  Δ ${format(e.gap, 1)}`, data.scores, "#5ce0c5"],
        [dd.paused ? "DD / PAUSED" : "DD / GATE", `${pct(dd.current)} / ${pct(dd.limit)}`, `SL ${format(data.plan.sl, 5)}  ·  TP ${format(data.plan.tp, 5)}`, [], "#f4c470"],
        [symbol, `${ex.side || "FILL"} ${format(ex.entry, 5)}`, `QTY ${format(ex.qty, 8)}  ·  SLIP ${format(ex.slippageAtr, 3)} ATR`, data.fills, "#f4c470"],
        ["EQ≈ / MARK", `≈${format(data.equity, 2)}`, `${symbol}  ${format(data.held[0]?.mark, 5)}  ·  PNL ${format(data.upnl, 2)}`, equityHistory, "#5ce0c5"],
        [`${symbol} / ${e.direction === "bullish" ? "LONG" : e.direction === "bearish" ? "SHORT" : "—"}`, `${format(e.score, 1)}  ${e.grade || ""}`, `GAP ${format(e.gap, 1)} / ${format(data.params.minScoreGap, 1)}  ·  ${mode ? mode.toUpperCase() : "LIVE"}`, data.scores, "#5ce0c5"],
      ]
      const signature = JSON.stringify(rows)
      dirty = false; lastTexture = now
      if (signature === lastSignature) return
      lastSignature = signature
      atlasCtx.clearRect(0, 0, 1024, 1024)
      rows.forEach(([title, value, detail, samples, color], i) => {
        const x = i % 2 * 512, y = Math.floor(i / 2) * 256
        atlasCtx.fillStyle = "rgba(15,33,40,.94)"; atlasCtx.fillRect(x + 4, y + 5, 504, 246)
        atlasCtx.fillStyle = color; atlasCtx.fillRect(x + 4, y + 5, 4, 246)
        atlasCtx.font = "600 26px ui-monospace, Consolas, monospace"; atlasCtx.fillStyle = "#a8c3c6"
        atlasCtx.fillText(title, x + 23, y + 42, 465)
        atlasCtx.font = "600 44px ui-monospace, Consolas, monospace"; atlasCtx.fillStyle = "#f3f3e9"
        atlasCtx.fillText(value, x + 22, y + 99, 468)
        atlasCtx.font = "23px ui-monospace, Consolas, monospace"; atlasCtx.fillStyle = color
        atlasCtx.fillText(detail, x + 23, y + 139, 464)
        chart(samples, x + 25, y + 160, 460, 55, color)
        if (samples.length) {
          const values = samples.map((p) => p.value)
          atlasCtx.fillStyle = "#91adaf"; atlasCtx.font = "18px ui-monospace, Consolas, monospace"
          atlasCtx.fillText(`${format(Math.min(...values), 5)}–${format(Math.max(...values), 5)}`, x + 23, y + 241, 300)
          const stamp = new Date(samples[samples.length - 1].ts).toLocaleTimeString("en-GB", { hour12: false })
          atlasCtx.textAlign = "right"; atlasCtx.fillText(stamp, x + 485, y + 241, 145); atlasCtx.textAlign = "left"
        }
        if (!samples.length) {
          atlasCtx.fillStyle = "#a8c3c6"; atlasCtx.font = "24px ui-monospace, Consolas, monospace"
          const footer = i === 0 ? `ATR ${format(m.atr, 5)}  ·  Z ${format(m.z, 2)}` : i === 2 ? `POS ${Array.isArray(inputs.current.snapshot?.positions) ? data.positions.length : "—"} / ${format(data.params.maxPositions, 0)}` : "—"
          atlasCtx.fillText(footer, x + 23, y + 198, 464)
        }
      })
      atlasTexture.needsUpdate = true
    }
    const startEpisode = (event) => {
      activeEvent = event
      if (event.symbol) focus = symKey(event.symbol)
      episode = { event, start: time, duration: event.mode === "signal" ? 3.6 : event.mode === "risk" ? 4.0 : 4.5 }
      refreshData()
    }
    const advance = (dt) => {
      time += dt
      while (queue.length && Date.now() - queue[0].ts > EVENT_TTL) queue.shift()
      if (episode && time - episode.start >= episode.duration) {
        const completed = episode.event
        episode = null; activeEvent = null; dirty = true
        if (completed.mode === "open" && fresh(completed.ts, Date.now(), 0)) {
          queue.unshift({ ...completed, mode: "feedback", key: `${completed.key}:return` })
          if (queue.length > 6) queue.length = 6
        }
      }
      if (!episode && queue.length) startEpisode(queue.shift())
      const progress = episode ? clamp((time - episode.start) / episode.duration) : 0
      const envelope = episode ? Math.sin(Math.PI * progress) ** 0.75 : 0
      const mode = episode?.event.mode
      const goal = cameraGoal[mode] || [0, 0]
      const smoothing = 1 - Math.exp(-dt * 3.5)
      cameraStrength += (envelope - cameraStrength) * smoothing
      azimuth += (0.72 + goal[0] * cameraStrength - azimuth) * smoothing
      elevation += (0.62 + goal[1] * cameraStrength - elevation) * smoothing
      setCamera(azimuth, elevation)
      const score = numeric(data?.evaluation.score), strength = score == null ? 0 : clamp(score / 100)
      const side = data?.evaluation.direction === "bearish" ? -1 : 1
      intakeRotor.rotation.y += dt * (0.09 + strength * 0.1)
      fans.forEach((rotor) => { rotor.rotation.y += dt * (1.4 + strength * 5) })
      decks.forEach((deck, i) => {
        const expansion = mode === "signal" ? envelope : 0
        deck.position.y = -0.17 + i * (0.24 + expansion * 0.16)
        deck.position.x = side * (i - 1) * expansion * 0.13
        deck.rotation.y = side * expansion * (i - 1) * 0.11
      })
      decisionCore.parent.rotation.y += dt * (0.1 + strength * 0.24)
      decisionCore.material.emissiveIntensity = 0.12 + (mode === "signal" ? envelope * 0.65 : strength * 0.08)
      gimbals.forEach((g, i) => { g.rotation.y += dt * (0.07 + i * 0.03) * (i % 2 ? -side : side); g.rotation.z = (mode === "signal" ? envelope : 0) * (i - 1) * 0.23 })
      petals.forEach(({ section, angle }, i) => {
        const expansion = mode === "risk" ? envelope : 0
        section.position.set(Math.cos(angle) * expansion * 0.23, Math.sin(angle) * expansion * 0.23, expansion * (i % 2 ? 0.16 : -0.1))
        section.rotation.z = expansion * (i % 2 ? 0.07 : -0.07)
      })
      const thrust = mode === "open" ? envelope : 0
      nozzle.position.y = 0.16 + thrust * 0.09
      nozzlePetals.forEach(({ section, angle }) => { section.position.set(Math.cos(angle) * thrust * 0.08, thrust * 0.05, Math.sin(angle) * thrust * 0.08) })
      jet.material.opacity = thrust * 0.46; jet.scale.y = 0.5 + thrust * 0.5
      feedbackRotor.rotation.y += dt * (0.13 + (mode === "feedback" || mode === "open" ? envelope * 1.2 : 0))
      for (const [plot, highlighted] of [[scoreLine, mode === "signal"], [fillLine, mode === "open"], [equityLine, mode === "feedback"], [positionLine, mode === "feedback"]]) {
        const reveal = highlighted ? clamp(progress * 1.9) : 1
        plot.revealed = reveal
        const count = Math.min(plot.count, Math.max(1, Math.ceil(plot.count * reveal)))
        plot.line.geometry.setDrawRange(0, count)
        if (count && plot.count) plot.dot.position.fromBufferAttribute(plot.line.geometry.attributes.position, count - 1)
      }
      for (let i = injected.length - 1; i >= 0; i--) if (time - injected[i].start > 8.4) injected.splice(i, 1)
      let count = 0
      for (const packet of injected) for (let trail = 0; trail < 5; trail++) {
        const progress = (time - packet.start) / 8.4 - trail * 0.006
        if (progress < 0) continue
        ringPoint(progress, dummy.position); dummy.scale.setScalar(0.04 - trail * 0.005)
        dummy.rotation.set(0, 0, 0); dummy.updateMatrix(); particles.setMatrixAt(count, dummy.matrix); particles.setColorAt(count, ledColor.set(packet.color)); count++
      }
      if (episode && count < 150) {
        const from = nodes.get(mode === "open" ? "execution" : mode === "feedback" ? "feedback" : mode === "risk" ? "risk" : "features").node.position
        const to = nodes.get("decision").node.position
        for (let i = 0; i < 7; i++) {
          const t = (progress * 2 + i / 7) % 1
          dummy.position.lerpVectors(from, to, t); dummy.position.y += Math.sin(t * Math.PI) * 0.2
          dummy.scale.setScalar(0.025 + envelope * 0.01); dummy.updateMatrix(); particles.setMatrixAt(count, dummy.matrix); particles.setColorAt(count, ledColor.set(mode === "risk" ? 0xf0b847 : 0x24caba)); count++
        }
      }
      particles.count = count; particles.instanceMatrix.needsUpdate = true
      if (particles.instanceColor) particles.instanceColor.needsUpdate = true
    }
    const render = (now) => {
      if (disposed || lost || !visible || document.hidden) return
      paintTelemetry(now)
      plaques.forEach((p) => { p.quaternion.copy(camera.quaternion) })
      world.updateMatrixWorld(true)
      receiver.getWorldPosition(projection); projection.project(camera)
      target.style.left = `${(projection.x * 0.5 + 0.5) * width}px`
      target.style.top = `${(-projection.y * 0.5 + 0.5) * height}px`
      if (target.dataset.ready !== "true") target.dataset.ready = "true"
      if (now - lastShadow > 160) { renderer.shadowMap.needsUpdate = true; lastShadow = now }
      renderer.render(scene, camera)
      host.dataset.drawCalls = String(renderer.info.render.calls)
    }
    const tick = (now) => {
      frame = 0
      if (!running || disposed) return
      if (now - lastPaint >= 1000 / 30) {
        const dt = lastFrame ? Math.min(0.08, (now - lastFrame) / 1000) : 0
        lastFrame = now; lastPaint = now; advance(dt); render(now)
      }
      frame = requestAnimationFrame(tick)
    }
    const updateMotion = () => {
      const next = canMove()
      host.dataset.motion = next ? "running" : "frozen"
      target.dataset.motion = next ? "running" : "frozen"
      if (next !== running) {
        running = next; lastFrame = 0
        cancelAnimationFrame(frame); frame = 0
        if (!next) queue.length = 0
      }
      if (running && !frame) frame = requestAnimationFrame(tick)
      if (!running) render(performance.now())
    }
    const sync = () => {
      if (disposed) return
      const value = inputs.current, alive = new Set(value.metricPulses.slice(-48).map((p) => p.id))
      for (const id of pendingPulses.keys()) if (!alive.has(id)) pendingPulses.delete(id)
      for (const pulse of value.metricPulses.slice(-48)) if (!pendingPulses.has(pulse.id)) pendingPulses.set(pulse.id, { color: pulse.color || "#24caba", arrived: false })
      trimMap(pendingPulses, 48)
      if (lastBatch !== value.newEvents) {
        lastBatch = value.newEvents
        for (const event of value.newEvents) {
          if (handledEvents.has(event.key)) continue
          handledEvents.set(event.key, true)
          if (canMove() && fresh(event.ts, Date.now(), 0)) {
            const duplicate = queue.findIndex((e) => e.mode === event.mode && e.symbol === event.symbol)
            if (duplicate >= 0) queue.splice(duplicate, 1)
            queue.push(event)
          }
        }
        trimMap(handledEvents, 128)
        if (queue.length > 6) queue.splice(0, queue.length - 6)
      }
      if (!episode && value.metricPulses.length) {
        const pulse = value.metricPulses[value.metricPulses.length - 1]
        if (pulse.symbol) focus = symKey(pulse.symbol)
      }
      refreshData(); updateMotion()
      clearTimeout(timer)
      // A reduced-motion scene still gets current numbers, at most one atlas upload / 300 ms.
      if (!running && visible && !document.hidden) timer = setTimeout(() => render(performance.now()), 310)
    }
    const arrival = (event) => {
      const pending = pendingPulses.get(event.detail?.id)
      if (!pending || pending.arrived || !canMove()) return
      pending.arrived = true
      injected.push({ start: time, color: pending.color }); if (injected.length > 28) injected.shift()
    }
    const resize = () => { fit(); render(performance.now()) }
    const resizeObserver = new ResizeObserver(resize); resizeObserver.observe(host)
    const intersection = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting && entry.intersectionRatio > 0; updateMotion() }, { threshold: [0, 0.01] })
    intersection.observe(host)
    document.addEventListener("visibilitychange", updateMotion)
    window.addEventListener("quant-metric-arrival", arrival)
    const contextLost = (event) => { event.preventDefault(); lost = true; target.dataset.ready = "false"; updateMotion(); setFailed(true) }
    const contextRestored = () => { lost = false; setFailed(false); dirty = true; lastSignature = ""; renderer.shadowMap.needsUpdate = true; updateMotion() }
    canvas.addEventListener("webglcontextlost", contextLost); canvas.addEventListener("webglcontextrestored", contextRestored)
    syncRef.current = sync
    fit(); sync(); render(performance.now())
    return () => {
      disposed = true; syncRef.current = null
      cancelAnimationFrame(frame); clearTimeout(timer)
      resizeObserver.disconnect(); intersection.disconnect()
      document.removeEventListener("visibilitychange", updateMotion); window.removeEventListener("quant-metric-arrival", arrival)
      canvas.removeEventListener("webglcontextlost", contextLost); canvas.removeEventListener("webglcontextrestored", contextRestored)
      pendingPulses.clear(); handledEvents.clear(); histories.clear(); queue.length = 0; injected.length = 0; equityHistory.length = 0
      instanceMeshes.forEach((m) => m.dispose())
      geometries.forEach((g) => g.dispose()); materials.forEach((m) => m.dispose()); textures.forEach((t) => t.dispose())
      key.shadow.dispose(); environment.dispose(); scene.clear(); renderer.renderLists.dispose(); renderer.dispose(); renderer.forceContextLoss()
      canvas.remove()
      delete target.dataset.ready; delete target.dataset.motion
    }
  }, [])

  return (
    <div ref={hostRef} className={styles.scene}>
      {failed && <span className={styles.sceneFallback}>当前浏览器无法初始化 3D 场景</span>}
      <span ref={targetRef} className={styles.intakeTarget} data-metric-intake="" aria-hidden="true" />
    </div>
  )
}
