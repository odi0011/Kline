/* DecisionOrchestrator 三维星图：同一 WebGL 场景里的中心卫星体、
   硬币轨道环、体积粒子网，以及世界空间三次飞线。
   环是水平面里的圆环（Torus），只绕轨道法线自旋；冲动只改这一轴的角速度，
   轨道平面保持不动，避免扁椭圆在屏幕上翻滚。 */
import * as THREE from "three"

const FOV = 42
const FILL = 0.74

const SLOT = {
  accent: 0,
  deep: 1,
  cyan: 2,
  cyanBright: 3,
  magenta: 4,
  magentaBright: 5,
  success: 6,
  successDeep: 7,
  sky: 8,
  blue: 9,
  danger: 10,
}

const COIN_SLOTS = [SLOT.accent, SLOT.cyan, SLOT.magenta, SLOT.success, SLOT.sky, SLOT.deep]
const COIN_BASES = ["btc", "eth", "bnb", "sol"]
const IMPULSE_PEAK = { boost: 3.2, reverse: -3.4 }
const FLIGHT_FADE_MS = 680
const ANCHORS = 8
const liveScenes = new Set()

const SLOT_KEYS = [
  "accent", "deep", "cyan", "cyanBright", "magenta", "magentaBright",
  "success", "successDeep", "sky", "blue", "danger",
]

export function baseSymbol(symbol) {
  return String(symbol || "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "")
    .replace(/usdt$/, "")
}

function assignRingSymbols(rings, nodes) {
  const list = Array.isArray(nodes) ? nodes : []
  const ringOf = new Map()
  const used = new Set()
  for (const node of list) {
    const preferred = COIN_BASES.indexOf(baseSymbol(node?.id))
    if (preferred >= 0 && preferred < rings.length && !used.has(preferred)) {
      used.add(preferred)
      ringOf.set(node.id, preferred)
    }
  }
  for (const node of list) {
    if (ringOf.has(node.id)) continue
    let index = rings.findIndex((_, i) => !used.has(i))
    if (index < 0) index = ringOf.size % Math.max(1, rings.length)
    used.add(index)
    ringOf.set(node.id, index)
  }
  rings.forEach((ring, index) => {
    const owner = list.find((node) => ringOf.get(node.id) === index)
    ring.base = owner ? baseSymbol(owner.id) : (COIN_BASES[index] || null)
    ring.symbol = owner ? String(owner.id).toLowerCase() : null
  })
  return ringOf
}

function impulseOffset(impulse, now) {
  if (!impulse) return 0
  const duration = impulse.duration || 1000
  const elapsed = now - impulse.start
  if (elapsed < 0 || elapsed >= duration) return 0
  const env = Math.sin(Math.PI * (elapsed / duration))
  const peak = impulse.mode === "reverse" ? IMPULSE_PEAK.reverse : IMPULSE_PEAK.boost
  return peak * env
}

function hashString(text) {
  let value = 0
  for (const char of String(text)) value = (value * 31 + char.charCodeAt(0)) >>> 0
  return value
}

function easeInOut(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2
}

export function syncMetricFlights(pulses, onDone) {
  let handled = false
  for (const scene of liveScenes) {
    if (typeof scene.syncFlights === "function") {
      scene.syncFlights(pulses || [], onDone)
      handled = true
    }
  }
  return handled
}

export function nudgeRing(symbol, options = {}) {
  let nudged = false
  for (const scene of liveScenes) {
    if (typeof scene.nudgeRing === "function" && scene.nudgeRing(symbol, options)) nudged = true
  }
  return nudged
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value))
}

function mulberry32(seed) {
  let a = seed >>> 0
  return () => {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function readCssColor(name, fallback) {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  const color = new THREE.Color(fallback)
  if (!raw) return color
  try {
    color.setStyle(raw)
  } catch {
    color.set(fallback)
  }
  return color
}

function pageBackground() {
  const raw = getComputedStyle(document.body).backgroundColor
  const color = new THREE.Color(0xffffff)
  if (!raw || raw === "transparent" || raw === "rgba(0, 0, 0, 0)") return color
  try {
    color.setStyle(raw)
  } catch {
    /* keep white */
  }
  return color
}

function makePalette() {
  const accent = readCssColor("--accent", "#f0b90b")
  const deep = accent.clone().lerp(new THREE.Color("#6a4a00"), 0.58)
  return {
    accent,
    deep,
    cyan: new THREE.Color("#0891b2"),
    cyanBright: new THREE.Color("#22d3ee"),
    magenta: new THREE.Color("#c026d3"),
    magentaBright: new THREE.Color("#e879f9"),
    success: new THREE.Color("#10b981"),
    successDeep: new THREE.Color("#16a34a"),
    sky: new THREE.Color("#38bdf8"),
    blue: new THREE.Color("#64748b"),
    danger: new THREE.Color("#dc2626"),
    slate: new THREE.Color("#94a3b8"),
  }
}

function toneColor(palette, slot, mode) {
  const base = palette[SLOT_KEYS[slot] || "accent"] || palette.accent
  const hsl = { h: 0, s: 0, l: 0 }
  base.getHSL(hsl)
  const out = new THREE.Color()
  if (mode === "line") {
    out.setHSL(
      hsl.h,
      clamp(hsl.s * 0.92 + 0.08, 0, 1),
      clamp(Math.min(0.42, Math.max(0.30, hsl.l * 0.78)), 0, 1),
    )
    return out
  }
  out.setHSL(
    hsl.h,
    clamp(hsl.s * 0.96 + 0.04, 0, 1),
    clamp(Math.max(0.42, Math.min(0.58, hsl.l)), 0, 1),
  )
  return out
}

function solidColor(palette, slot) {
  const base = palette[SLOT_KEYS[slot] || "accent"] || palette.accent
  return base.clone()
}

function applyCssColor(material, value, fallback) {
  try {
    material.color.set(value || fallback)
  } catch {
    material.color.set(fallback)
  }
}

function ringSignature(nodes) {
  if (!nodes.length) return "default"
  return nodes.map((node) => baseSymbol(node.id)).sort().join("|")
}

function makeSpecs(count) {
  const total = Math.max(1, count)
  const gap = total > 5 ? 0.28 : 0.36
  const slots = [SLOT.accent, SLOT.cyan, SLOT.magenta, SLOT.success, SLOT.deep, SLOT.sky]
  const tilts = [
    [1.28, 0.22],
    [0.52, -0.42],
    [1.02, 0.88],
    [0.78, -0.18],
    [1.16, 0.48],
    [0.64, -0.72],
  ]
  return Array.from({ length: total }, (_, index) => {
    const tilt = tilts[index % tilts.length]
    const sign = index % 2 === 0 ? 1 : -1
    return {
      radius: 1.02 + index * gap,
      tiltX: tilt[0],
      tiltZ: tilt[1],
      speed: sign * (0.18 + (index % 3) * 0.05),
      hold: 0.4 * index,
      slot: slots[index % slots.length],
      tube: 0.04 + (index % 2) * 0.008,
    }
  })
}

function disposeTree(root) {
  if (!root) return
  root.traverse((obj) => {
    if (obj.geometry) obj.geometry.dispose()
    if (obj.material) {
      const list = Array.isArray(obj.material) ? obj.material : [obj.material]
      list.forEach((material) => {
        if (material.map) material.map.dispose()
        material.dispose()
      })
    }
  })
}

function makePlanetTexture(accent) {
  const canvas = document.createElement("canvas")
  canvas.width = 512
  canvas.height = 256
  const ctx = canvas.getContext("2d")
  const base = accent.clone().lerp(new THREE.Color("#5c3d00"), 0.42)
  const band = accent.clone().lerp(new THREE.Color("#fff6d2"), 0.28)
  const panel = accent.clone().lerp(new THREE.Color("#ffffff"), 0.62)
  ctx.fillStyle = `#${base.getHexString()}`
  ctx.fillRect(0, 0, 512, 256)
  ctx.fillStyle = `#${band.getHexString()}`
  for (let i = 0; i < 8; i += 1) {
    ctx.globalAlpha = i % 2 ? 0.45 : 0.92
    ctx.fillRect(0, 14 + i * 30, 512, 8 + (i % 3) * 3)
  }
  ctx.globalAlpha = 0.9
  ctx.fillStyle = `#${panel.getHexString()}`
  ctx.beginPath()
  ctx.arc(128, 74, 20, 0, Math.PI * 2)
  ctx.fill()
  ctx.fillRect(286, 146, 78, 24)
  ctx.fillRect(48, 176, 52, 16)
  ctx.fillRect(360, 64, 28, 40)
  ctx.globalAlpha = 1
  const tex = new THREE.CanvasTexture(canvas)
  tex.colorSpace = THREE.SRGBColorSpace
  tex.wrapS = THREE.RepeatWrapping
  tex.anisotropy = 4
  return tex
}

function buildCore(palette) {
  const group = new THREE.Group()
  const color = palette.accent.clone()
  const map = makePlanetTexture(color)
  const body = new THREE.Mesh(
    new THREE.SphereGeometry(0.46, 48, 32),
    new THREE.MeshStandardMaterial({
      color,
      map,
      emissive: color,
      emissiveIntensity: 0.18,
      metalness: 0.58,
      roughness: 0.36,
    }),
  )
  body.name = "core-body"
  body.renderOrder = 3
  group.add(body)

  const atmosphere = new THREE.Mesh(
    new THREE.SphereGeometry(0.6, 32, 24),
    new THREE.MeshBasicMaterial({
      color,
      transparent: true,
      opacity: 0.14,
      depthWrite: false,
    }),
  )
  atmosphere.renderOrder = 4
  group.add(atmosphere)

  const equator = new THREE.Mesh(
    new THREE.TorusGeometry(0.78, 0.016, 8, 80),
    new THREE.MeshStandardMaterial({
      color,
      emissive: color,
      emissiveIntensity: 0.35,
      metalness: 0.4,
      roughness: 0.35,
    }),
  )
  equator.rotation.x = 1.18
  equator.renderOrder = 3
  group.add(equator)

  return { group, body, atmosphere, equator, map }
}

function buildOrbitRings(palette, count) {
  return makeSpecs(count).map((spec) => {
    const tilt = new THREE.Group()
    tilt.rotation.order = "XYZ"
    tilt.rotation.x = spec.tiltX
    tilt.rotation.y = 0
    tilt.rotation.z = spec.tiltZ

    const spin = new THREE.Group()
    spin.rotation.order = "XYZ"
    tilt.add(spin)

    const color = solidColor(palette, spec.slot)
    const tubeMat = new THREE.MeshStandardMaterial({
      color,
      emissive: color,
      emissiveIntensity: 0.22,
      metalness: 0.48,
      roughness: 0.34,
    })
    const torus = new THREE.Mesh(
      new THREE.TorusGeometry(spec.radius, spec.tube, 14, 120),
      tubeMat,
    )
    torus.renderOrder = 4
    tilt.add(torus)

    const beadMat = new THREE.MeshStandardMaterial({
      color,
      emissive: color,
      emissiveIntensity: 0.7,
      metalness: 0.3,
      roughness: 0.28,
    })
    const anchors = []
    for (let i = 0; i < ANCHORS; i += 1) {
      const angle = ((i + 0.17) / ANCHORS) * Math.PI * 2
      const bead = new THREE.Mesh(
        new THREE.SphereGeometry(i === 0 ? 0.09 : 0.058, 14, 12),
        beadMat,
      )
      bead.position.set(Math.cos(angle) * spec.radius, Math.sin(angle) * spec.radius, 0)
      bead.userData.index = i
      bead.renderOrder = 5
      spin.add(bead)
      anchors.push(bead)
    }

    return {
      spec,
      tilt,
      spin,
      torus,
      tubeMat,
      beadMat,
      anchors,
      phase: 0,
      impulse: null,
      base: null,
      symbol: null,
    }
  })
}

function buildNetwork(rng, palette) {
  const count = 680
  const positions = new Float32Array(count * 3)
  const slots = new Uint8Array(count)
  const points = []
  for (let i = 0; i < count; i += 1) {
    const radius = 0.22 + Math.pow(rng(), 0.72) * 0.86
    const theta = Math.acos(clamp(2 * rng() - 1, -1, 1))
    const phi = rng() * Math.PI * 2
    const x = Math.sin(theta) * Math.cos(phi) * radius
    const y = Math.cos(theta) * radius
    const z = Math.sin(theta) * Math.sin(phi) * radius
    positions[i * 3] = x
    positions[i * 3 + 1] = y
    positions[i * 3 + 2] = z
    points.push({ x, y, z })
    slots[i] = [SLOT.accent, SLOT.cyan, SLOT.magenta, SLOT.sky, SLOT.deep, SLOT.success][i % 6]
  }

  const edges = []
  const seen = new Set()
  for (let i = 0; i < count; i += 1) {
    let best = -1
    let bestD = 0.08
    const a = points[i]
    for (let j = 0; j < count; j += 1) {
      if (i === j) continue
      const b = points[j]
      const d = (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2
      if (d < bestD) {
        bestD = d
        best = j
      }
    }
    if (best < 0) continue
    const id = i < best ? i * 1000 + best : best * 1000 + i
    if (seen.has(id)) continue
    seen.add(id)
    edges.push([i, best])
  }

  const linePositions = new Float32Array(edges.length * 2 * 3)
  const lineSlots = new Uint8Array(edges.length * 2)
  edges.forEach(([a, b], index) => {
    const o = index * 6
    linePositions[o] = points[a].x
    linePositions[o + 1] = points[a].y
    linePositions[o + 2] = points[a].z
    linePositions[o + 3] = points[b].x
    linePositions[o + 4] = points[b].y
    linePositions[o + 5] = points[b].z
    lineSlots[index * 2] = slots[a]
    lineSlots[index * 2 + 1] = slots[b]
  })

  const lineGeo = new THREE.BufferGeometry()
  lineGeo.setAttribute("position", new THREE.BufferAttribute(linePositions, 3))
  const lineColors = new Float32Array(lineSlots.length * 3)
  lineGeo.setAttribute("color", new THREE.BufferAttribute(lineColors, 3))
  const lines = new THREE.LineSegments(lineGeo, new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.55,
    depthWrite: false,
  }))
  lines.frustumCulled = false
  lines.renderOrder = 1

  const dotGeo = new THREE.BufferGeometry()
  dotGeo.setAttribute("position", new THREE.BufferAttribute(positions, 3))
  const dotColors = new Float32Array(slots.length * 3)
  dotGeo.setAttribute("color", new THREE.BufferAttribute(dotColors, 3))
  const dots = new THREE.Points(dotGeo, new THREE.PointsMaterial({
    size: 0.042,
    vertexColors: true,
    transparent: true,
    opacity: 0.9,
    depthWrite: false,
    sizeAttenuation: true,
  }))
  dots.frustumCulled = false
  dots.renderOrder = 1

  const paint = (nextPalette) => {
    const lineAttr = lineGeo.getAttribute("color")
    const dotAttr = dotGeo.getAttribute("color")
    const scratch = new THREE.Color()
    for (let i = 0; i < lineSlots.length; i += 1) {
      scratch.copy(toneColor(nextPalette, lineSlots[i], "line"))
      lineAttr.array[i * 3] = scratch.r
      lineAttr.array[i * 3 + 1] = scratch.g
      lineAttr.array[i * 3 + 2] = scratch.b
    }
    for (let i = 0; i < slots.length; i += 1) {
      scratch.copy(toneColor(nextPalette, slots[i], "particle"))
      dotAttr.array[i * 3] = scratch.r
      dotAttr.array[i * 3 + 1] = scratch.g
      dotAttr.array[i * 3 + 2] = scratch.b
    }
    lineAttr.needsUpdate = true
    dotAttr.needsUpdate = true
  }
  paint(palette)

  const root = new THREE.Group()
  root.add(lines)
  root.add(dots)
  return { root, paint }
}

function bendCurve(from, to, seed, viewDir) {
  const sign = (seed & 1) ? 1 : -1
  const span = Math.max(0.45, from.distanceTo(to))
  const mag = span * (0.38 + ((seed >>> 4) % 30) / 100)
  const mid = from.clone().lerp(to, 0.5)
  const away = mid.lengthSq() > 1e-4 ? mid.clone().normalize() : new THREE.Vector3(0, 1, 0)
  const side = new THREE.Vector3().crossVectors(away, new THREE.Vector3(0, 1, 0))
  if (side.lengthSq() < 1e-6) side.set(1, 0, 0)
  side.normalize()
  const toward = (viewDir || new THREE.Vector3(0, 0, 1)).clone().multiplyScalar(-sign)
  const c1 = from.clone().lerp(to, 0.32)
    .addScaledVector(side, sign * mag)
    .addScaledVector(toward, span * 0.34)
    .add(new THREE.Vector3(0, sign * span * 0.22, 0))
  const c2 = from.clone().lerp(to, 0.7)
    .addScaledVector(side, sign * mag * 0.45)
    .addScaledVector(toward, span * 0.12)
  for (const point of [c1, c2]) {
    if (point.length() < 0.78) point.setLength(0.78)
  }
  return new THREE.CubicBezierCurve3(from.clone(), c1, c2, to.clone())
}

function failureStub() {
  const stub = {
    setNodes() {},
    setReduced() {},
    setOnFrame() {},
    getIntakeTargets() { return [] },
    nudgeRing() { return false },
    syncFlights(pulses, onDone) {
      for (const pulse of pulses || []) onDone?.(pulse.id)
    },
    sampleOrbit() { return { hasCore: false, rings: [] } },
    resize() {},
    dispose() { liveScenes.delete(stub) },
  }
  liveScenes.add(stub)
  return stub
}

export function createNebulaScene(host, options = {}) {
  const state = {
    nodes: options.nodes || [],
    reduced: Boolean(options.reduced),
    onFrame: typeof options.onFrame === "function" ? options.onFrame : null,
    onFlightDone: null,
    intakeTargets: [],
    disposed: false,
    width: 1,
    height: 1,
    accentHex: "",
    outer: 2.2,
    camDist: 12,
    ringSig: "",
  }

  let palette = makePalette()
  state.accentHex = palette.accent.getHexString()
  const bg = pageBackground()

  const scene = new THREE.Scene()
  scene.background = bg

  const camera = new THREE.PerspectiveCamera(FOV, 1, 0.08, 80)
  camera.position.set(0, 4, 10)
  camera.lookAt(0, 0, 0)

  let renderer
  try {
    renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: false,
      powerPreference: "high-performance",
      preserveDrawingBuffer: true,
    })
  } catch {
    return failureStub()
  }
  renderer.setClearColor(bg, 1)
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
  renderer.outputColorSpace = THREE.SRGBColorSpace
  renderer.domElement.style.display = "block"
  renderer.domElement.style.width = "100%"
  renderer.domElement.style.height = "100%"
  renderer.domElement.style.pointerEvents = "none"
  host.appendChild(renderer.domElement)

  scene.add(new THREE.AmbientLight(0xfff8ee, 0.22))
  scene.add(new THREE.HemisphereLight(0xfff4dc, 0xd5deea, 0.2))
  const key = new THREE.DirectionalLight(0xffffff, 3.3)
  key.position.set(4.2, 6.4, 3.6)
  scene.add(key)
  const rim = new THREE.DirectionalLight(0xb7d7ff, 0.9)
  rim.position.set(-5.2, 1.4, -3.8)
  scene.add(rim)
  const coreLight = new THREE.PointLight(0xf0b90b, 8, 16, 2)
  scene.add(coreLight)

  let core = buildCore(palette)
  scene.add(core.group)

  const network = buildNetwork(mulberry32(0xa11ce), palette)
  scene.add(network.root)

  let rings = []
  const coinRoot = new THREE.Group()
  scene.add(coinRoot)
  const coinGeo = new THREE.SphereGeometry(0.09, 18, 14)
  const nodeMeshes = []

  const flightRoot = new THREE.Group()
  scene.add(flightRoot)
  const flights = new Map()
  const headGeo = new THREE.SphereGeometry(0.05, 12, 10)

  const linkMax = 32
  const linkPos = new Float32Array(linkMax * 2 * 3)
  const linkCol = new Float32Array(linkMax * 2 * 3)
  const linkGeo = new THREE.BufferGeometry()
  linkGeo.setAttribute("position", new THREE.BufferAttribute(linkPos, 3))
  linkGeo.setAttribute("color", new THREE.BufferAttribute(linkCol, 3))
  linkGeo.setDrawRange(0, 0)
  const linkLine = new THREE.LineSegments(linkGeo, new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.65,
    depthWrite: false,
  }))
  linkLine.frustumCulled = false
  linkLine.renderOrder = 2
  scene.add(linkLine)

  const clock = new THREE.Clock()
  let lastTime = null
  const projected = new THREE.Vector3()
  const worldPoint = new THREE.Vector3()
  const centerPoint = new THREE.Vector3()
  const targetPoint = new THREE.Vector3()
  let raf = 0

  const layoutNetwork = () => {
    network.root.scale.setScalar(Math.max(1.15, state.outer))
  }

  const clearRings = () => {
    rings.forEach((ring) => {
      scene.remove(ring.tilt)
      disposeTree(ring.tilt)
    })
    rings = []
  }

  const ensureRings = (nodes) => {
    const sig = ringSignature(nodes)
    if (sig === state.ringSig && rings.length) return
    state.ringSig = sig
    clearRings()
    rings = buildOrbitRings(palette, nodes.length || 4)
    rings.forEach((ring) => scene.add(ring.tilt))
    state.outer = rings.reduce((max, ring) => Math.max(max, ring.spec.radius), 1.2)
    layoutNetwork()
  }

  const rebuildCoins = (nodes, ringOf) => {
    while (coinRoot.children.length) {
      const child = coinRoot.children.pop()
      child.traverse((obj) => {
        if (obj.material && obj.geometry !== coinGeo) {
          const list = Array.isArray(obj.material) ? obj.material : [obj.material]
          list.forEach((material) => material.dispose())
        } else if (obj.material) {
          const list = Array.isArray(obj.material) ? obj.material : [obj.material]
          list.forEach((material) => material.dispose())
        }
      })
    }
    nodeMeshes.length = 0
    nodes.forEach((node, index) => {
      const ringIndex = ringOf?.get(node.id) ?? (index % Math.max(1, rings.length))
      const ring = rings[ringIndex] || rings[0]
      const slot = ring?.spec.slot ?? COIN_SLOTS[index % COIN_SLOTS.length]
      const color = toneColor(palette, slot, "particle")
      const mesh = new THREE.Mesh(coinGeo, new THREE.MeshStandardMaterial({
        color,
        emissive: color,
        emissiveIntensity: 0.5,
        metalness: 0.32,
        roughness: 0.36,
      }))
      mesh.renderOrder = 6
      mesh.userData = {
        id: node.id,
        label: node.label,
        color: `#${color.getHexString()}`,
        pct: node.pct,
        score: node.score,
        ring: ringIndex,
        baseAngle: node.baseAngle || 0,
        drift: node.drift || 0,
        slot,
      }
      coinRoot.add(mesh)
      nodeMeshes.push(mesh)
    })
  }

  const syncNodes = (nodes) => {
    state.nodes = nodes || []
    ensureRings(state.nodes)
    const ringOf = assignRingSymbols(rings, state.nodes)
    rebuildCoins(state.nodes, ringOf)
    fit()
  }

  const repaint = () => {
    palette = makePalette()
    state.accentHex = palette.accent.getHexString()
    const accent = palette.accent.clone()
    if (core.map) core.map.dispose()
    core.map = makePlanetTexture(accent)
    core.body.material.color.copy(accent)
    core.body.material.emissive.copy(accent)
    core.body.material.map = core.map
    core.body.material.needsUpdate = true
    core.atmosphere.material.color.copy(accent)
    core.equator.material.color.copy(accent)
    core.equator.material.emissive.copy(accent)
    coreLight.color.copy(accent)
    network.paint(palette)
    rings.forEach((ring) => {
      const color = solidColor(palette, ring.spec.slot)
      ring.tubeMat.color.copy(color)
      ring.tubeMat.emissive.copy(color)
      ring.beadMat.color.copy(color)
      ring.beadMat.emissive.copy(color)
    })
    nodeMeshes.forEach((mesh) => {
      const color = toneColor(palette, mesh.userData.slot, "particle")
      mesh.material.color.copy(color)
      mesh.material.emissive.copy(color)
      mesh.userData.color = `#${color.getHexString()}`
    })
  }

  const fit = () => {
    const aspect = state.width / Math.max(1, state.height)
    camera.aspect = aspect
    camera.fov = FOV
    const outer = Math.max(1.35, state.outer + 0.42)
    const half = THREE.MathUtils.degToRad(FOV) / 2
    const limit = Math.min(Math.max(aspect, 0.35), 1)
    const visH = (2 * outer / FILL) / limit
    state.camDist = visH / (2 * Math.tan(half))
    camera.near = 0.08
    camera.far = Math.max(80, state.camDist * 4)
    camera.updateProjectionMatrix()
  }

  const resize = () => {
    if (state.disposed) return
    const rect = host.getBoundingClientRect()
    state.width = Math.max(1, Math.floor(rect.width))
    state.height = Math.max(1, Math.floor(rect.height))
    renderer.setSize(state.width, state.height, false)
    fit()
    if (state.reduced) renderFrame(1.15)
  }

  const placeCamera = (time) => {
    const yaw = state.reduced ? 0.62 : 0.42 + Math.sin(time * 0.16) * 0.38
    const elev = state.reduced ? 0.34 : 0.32 + Math.sin(time * 0.08) * 0.035
    const dist = state.camDist
    camera.position.set(
      Math.sin(yaw) * Math.cos(elev) * dist,
      Math.sin(elev) * dist,
      Math.cos(yaw) * Math.cos(elev) * dist,
    )
    camera.lookAt(0, 0, 0)
  }

  const resolveAnchor = (symbol, index) => {
    const want = baseSymbol(symbol)
    if (!want) return null
    const ring = rings.find((item) => item.symbol && baseSymbol(item.symbol) === want)
    if (!ring || !ring.anchors.length) return null
    return ring.anchors[index % ring.anchors.length]
  }

  const raycaster = new THREE.Raycaster()
  const rayNdc = new THREE.Vector2()
  const screenToWorld = (clientX, clientY) => {
    const rect = renderer.domElement.getBoundingClientRect()
    const width = Math.max(1, rect.width)
    const height = Math.max(1, rect.height)
    let nx = ((Number(clientX) - rect.left) / width) * 2 - 1
    let ny = -((Number(clientY) - rect.top) / height) * 2 + 1
    if (!Number.isFinite(nx) || !Number.isFinite(ny)) {
      nx = -0.9
      ny = 0.12
    }
    rayNdc.set(clamp(nx, -1.35, 1.35), clamp(ny, -1.15, 1.15))
    raycaster.setFromCamera(rayNdc, camera)
    const hit = new THREE.Vector3()
    const shellRadius = Math.max(1.35, state.outer * 1.05)
    const shell = new THREE.Sphere(new THREE.Vector3(0, 0, 0), shellRadius)
    if (raycaster.ray.intersectSphere(shell, hit)) return hit
    const closest = raycaster.ray.closestPointToPoint(new THREE.Vector3(), new THREE.Vector3())
    if (closest.lengthSq() < 1e-4) closest.set(-shellRadius, 0.25, 0.35)
    return closest.setLength(shellRadius)
  }

  const hideFlightMesh = (rec) => {
    if (rec.tube) rec.tube.visible = false
    if (rec.head) rec.head.visible = false
  }

  const finishFlight = (rec) => {
    if (rec.done) return
    rec.done = true
    hideFlightMesh(rec)
    state.onFlightDone?.(rec.pulse.id)
  }

  const showHead = (rec, position, color, alpha) => {
    if (!rec.head) {
      rec.head = new THREE.Mesh(headGeo, new THREE.MeshBasicMaterial({
        color: 0xf0b90b,
        transparent: true,
        depthWrite: false,
      }))
      rec.head.renderOrder = 9
      rec.head.frustumCulled = false
      flightRoot.add(rec.head)
    }
    rec.head.visible = true
    rec.head.position.copy(position)
    rec.head.material.opacity = alpha
    applyCssColor(rec.head.material, color, "#f0b90b")
  }

  const showTrail = (rec, curve, t0, t1, color, alpha) => {
    const steps = 18
    const pts = []
    for (let i = 0; i <= steps; i += 1) {
      pts.push(curve.getPoint(t0 + (t1 - t0) * (i / steps)))
    }
    const tooClose = pts.some((point) => camera.position.distanceTo(point) < 1.4)
    const tooFar = pts.some((point) => point.length() > Math.max(4, state.outer * 2.4))
    if (tooClose || tooFar || pts[0].distanceTo(pts[pts.length - 1]) < 0.04) {
      if (rec.tube) rec.tube.visible = false
      return
    }
    const trail = new THREE.CatmullRomCurve3(pts)
    const geometry = new THREE.TubeGeometry(trail, 22, 0.03, 6, false)
    if (!rec.tube) {
      rec.tube = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
        color: 0xf0b90b,
        transparent: true,
        depthWrite: false,
      }))
      rec.tube.renderOrder = 8
      rec.tube.frustumCulled = false
      flightRoot.add(rec.tube)
    } else {
      rec.tube.geometry.dispose()
      rec.tube.geometry = geometry
    }
    rec.tube.visible = true
    rec.tube.material.opacity = 0.92 * alpha
    applyCssColor(rec.tube.material, color, "#f0b90b")
  }

  const updateFlights = (now) => {
    for (const rec of flights.values()) {
      if (rec.done) continue
      const pulse = rec.pulse
      if (rec.startedAt == null) rec.startedAt = now + (Number(pulse.delay) || 0)
      const elapsed = now - rec.startedAt
      if (elapsed < 0) continue
      const spawn = pulse.spawn ?? 170
      const duration = Math.max(1, Number(pulse.duration) || 1400)
      const travel = elapsed - spawn
      const anchor = resolveAnchor(pulse.symbol, rec.anchorIndex)
      if (!anchor) {
        const age = Date.now() - (pulse.ts || Date.now())
        if (age > 4000) finishFlight(rec)
        continue
      }
      anchor.getWorldPosition(targetPoint)
      const source = pulse.source || {}
      const from = screenToWorld(source.x, source.y)
      if (travel < 0) {
        showHead(rec, from, pulse.color, 0.85)
        if (rec.tube) rec.tube.visible = false
        continue
      }
      if (travel >= duration && !rec.arrived) {
        rec.arrived = true
        const reverse = (hashString(`${pulse.id}:ring`) & 1) === 1
        nudgeLocal(pulse.symbol, reverse
          ? { reverse: true, durationMs: 1000 }
          : { boost: true, durationMs: 1000 })
      }
      const fade = travel < duration ? 1 : 1 - (travel - duration) / FLIGHT_FADE_MS
      if (fade <= 0) {
        finishFlight(rec)
        continue
      }
      const t = travel < duration ? easeInOut(Math.min(1, travel / duration)) : 1
      const viewDir = new THREE.Vector3()
      camera.getWorldDirection(viewDir)
      const curve = bendCurve(from, targetPoint, hashString(String(pulse.id)), viewDir)
      showTrail(rec, curve, Math.max(0, t - 0.4), t, pulse.color, fade)
      showHead(rec, curve.getPoint(t), pulse.color, fade)
    }
  }

  function nudgeLocal(symbol, options = {}) {
    const want = baseSymbol(symbol)
    if (!want || state.reduced) return false
    const ring = rings.find((item) => item.symbol && baseSymbol(item.symbol) === want)
    if (!ring) return false
    const durationMs = clamp(Number(options.durationMs) || 1000, 600, 1400)
    let mode = "boost"
    if (options.reverse || options.mode === "reverse") mode = "reverse"
    else if (options.boost || options.mode === "boost") mode = "boost"
    else mode = Math.random() < 0.5 ? "reverse" : "boost"
    ring.impulse = { start: performance.now(), duration: durationMs, mode }
    return true
  }

  const renderFrame = (time) => {
    const reduced = state.reduced
    const t = reduced ? 1.15 : time
    const dt = lastTime == null ? 0 : clamp(time - lastTime, 0, 0.05)
    lastTime = time
    const nowMs = performance.now()

    core.body.rotation.y = reduced ? 0.4 : t * 0.32
    network.root.rotation.y = reduced ? 0.2 : t * 0.07

    rings.forEach((ring) => {
      if (ring.impulse && nowMs >= ring.impulse.start + ring.impulse.duration) ring.impulse = null
      const burst = reduced ? 0 : impulseOffset(ring.impulse, nowMs)
      if (!reduced) ring.phase += dt * burst
      ring.spin.rotation.x = 0
      ring.spin.rotation.y = 0
      ring.spin.rotation.z = reduced ? ring.spec.hold : t * ring.spec.speed + ring.phase
      ring.tilt.rotation.x = ring.spec.tiltX
      ring.tilt.rotation.y = 0
      ring.tilt.rotation.z = ring.spec.tiltZ
    })

    placeCamera(t)
    camera.updateMatrixWorld(true)
    scene.updateMatrixWorld(true)

    const positions = linkGeo.getAttribute("position")
    const colors = linkGeo.getAttribute("color")
    let vert = 0
    const overlay = []
    const coinWorld = []

    nodeMeshes.forEach((mesh) => {
      const data = mesh.userData
      const ring = rings[data.ring] || rings[0]
      if (!ring) return
      const drift = reduced ? 0 : t * data.drift
      const angle = ring.spin.rotation.z + data.baseAngle + drift
      worldPoint.set(
        Math.cos(angle) * ring.spec.radius,
        Math.sin(angle) * ring.spec.radius,
        0,
      )
      ring.tilt.localToWorld(worldPoint)
      mesh.position.copy(worldPoint)
      coinWorld.push(worldPoint.clone())

      projected.copy(worldPoint).project(camera)
      const padX = clamp(state.width * 0.07, 52, 78)
      const padY = clamp(state.height * 0.08, 42, 68)
      const rawX = (projected.x * 0.5 + 0.5) * state.width
      const rawY = (-projected.y * 0.5 + 0.5) * state.height
      const closeness = clamp((1 - projected.z) * 0.5, 0, 1)
      overlay.push({
        id: data.id,
        label: data.label,
        color: data.color,
        pct: data.pct,
        score: data.score,
        x: clamp(rawX, padX, state.width - padX),
        y: clamp(rawY, padY, state.height - padY),
        z: projected.z,
        visible: projected.z > -1 && projected.z < 1,
        depth: closeness,
      })
    })

    const pushLink = (a, b, ca, cb) => {
      if (vert + 2 > linkPos.length / 3) return
      linkPos[vert * 3] = a.x
      linkPos[vert * 3 + 1] = a.y
      linkPos[vert * 3 + 2] = a.z
      linkCol[vert * 3] = ca.r
      linkCol[vert * 3 + 1] = ca.g
      linkCol[vert * 3 + 2] = ca.b
      vert += 1
      linkPos[vert * 3] = b.x
      linkPos[vert * 3 + 1] = b.y
      linkPos[vert * 3 + 2] = b.z
      linkCol[vert * 3] = cb.r
      linkCol[vert * 3 + 1] = cb.g
      linkCol[vert * 3 + 2] = cb.b
      vert += 1
    }

    const origin = new THREE.Vector3(0, 0, 0)
    for (let i = 0; i < coinWorld.length; i += 1) {
      const ca = toneColor(palette, nodeMeshes[i].userData.slot, "line")
      const coreTone = toneColor(palette, SLOT.accent, "line")
      pushLink(coinWorld[i], origin, ca, coreTone)
    }
    positions.needsUpdate = true
    colors.needsUpdate = true
    linkGeo.setDrawRange(0, vert)
    linkLine.visible = vert > 0

    centerPoint.set(0, 0, 0).project(camera)
    const center = {
      x: (centerPoint.x * 0.5 + 0.5) * state.width,
      y: (-centerPoint.y * 0.5 + 0.5) * state.height,
    }

    const intakeTargets = []
    rings.forEach((ring) => {
      if (!ring.symbol) return
      ring.anchors.forEach((anchor, index) => {
        anchor.getWorldPosition(worldPoint)
        projected.copy(worldPoint).project(camera)
        if (projected.z <= -1 || projected.z >= 1) return
        const x = (projected.x * 0.5 + 0.5) * state.width
        const y = (-projected.y * 0.5 + 0.5) * state.height
        if (x < 8 || y < 8 || x > state.width - 8 || y > state.height - 8) return
        intakeTargets.push({
          id: `${ring.symbol}:${index}`,
          symbol: ring.symbol,
          base: ring.base,
          x,
          y,
        })
      })
    })
    state.intakeTargets = intakeTargets

    updateFlights(nowMs)

    renderer.setClearColor(bg, 1)
    renderer.render(scene, camera)

    if (state.onFrame) {
      state.onFrame({
        nodes: overlay,
        intakeTargets,
        center,
        width: state.width,
        height: state.height,
      })
    }
  }

  const loop = () => {
    if (state.disposed || state.reduced) return
    raf = requestAnimationFrame(loop)
    if ((Math.floor(clock.getElapsedTime() * 2) % 30) === 0) {
      const next = readCssColor("--accent", "#f0b90b").getHexString()
      if (next !== state.accentHex) repaint()
    }
    renderFrame(clock.getElapsedTime())
  }

  syncNodes(state.nodes)

  const observer = new ResizeObserver(resize)
  observer.observe(host)
  resize()
  renderFrame(state.reduced ? 1.15 : 0)
  if (!state.reduced) raf = requestAnimationFrame(loop)

  const accentObserver = new MutationObserver(() => {
    const next = readCssColor("--accent", "#f0b90b").getHexString()
    if (next !== state.accentHex) {
      repaint()
      if (state.reduced) renderFrame(1.15)
    }
  })
  accentObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["style"] })

  const api = {
    setNodes(nodes) {
      syncNodes(nodes || [])
      if (state.reduced) renderFrame(1.15)
    },
    setReduced(value) {
      const next = Boolean(value)
      if (next === state.reduced) return
      state.reduced = next
      if (next) {
        cancelAnimationFrame(raf)
        renderFrame(1.15)
      } else {
        clock.start()
        lastTime = null
        raf = requestAnimationFrame(loop)
      }
    },
    setOnFrame(fn) {
      state.onFrame = typeof fn === "function" ? fn : null
    },
    getIntakeTargets(symbol) {
      const want = baseSymbol(symbol)
      const list = state.intakeTargets || []
      const matched = want ? list.filter((item) => item.base === want) : list
      return matched.map((item) => ({ ...item }))
    },
    nudgeRing(symbol, options) {
      return nudgeLocal(symbol, options)
    },
    syncFlights(pulses, onDone) {
      state.onFlightDone = typeof onDone === "function" ? onDone : state.onFlightDone
      const incoming = new Set()
      for (const pulse of pulses || []) {
        if (!pulse?.id) continue
        incoming.add(pulse.id)
        const existing = flights.get(pulse.id)
        if (existing) {
          existing.pulse = pulse
          continue
        }
        flights.set(pulse.id, {
          pulse,
          anchorIndex: hashString(String(pulse.id)) % ANCHORS,
          startedAt: null,
          arrived: false,
          done: false,
          tube: null,
          head: null,
        })
      }
      for (const [id, rec] of flights) {
        if (incoming.has(id)) continue
        if (rec.tube) {
          flightRoot.remove(rec.tube)
          rec.tube.geometry.dispose()
          rec.tube.material.dispose()
        }
        if (rec.head) {
          flightRoot.remove(rec.head)
          rec.head.material.dispose()
        }
        flights.delete(id)
      }
    },
    sampleOrbit() {
      const bead = new THREE.Vector3()
      return {
        hasCore: Boolean(core?.body),
        coreRadius: core.body.geometry.parameters.radius,
        camera: camera.position.toArray(),
        rings: rings.map((ring) => {
          ring.anchors[0]?.getWorldPosition(bead)
          return {
            symbol: ring.symbol,
            radius: ring.spec.radius,
            spinX: ring.spin.rotation.x,
            spinY: ring.spin.rotation.y,
            spinZ: ring.spin.rotation.z,
            tiltX: ring.tilt.rotation.x,
            tiltY: ring.tilt.rotation.y,
            tiltZ: ring.tilt.rotation.z,
            bead: bead.toArray(),
          }
        }),
      }
    },
    resize,
    dispose() {
      state.disposed = true
      liveScenes.delete(api)
      cancelAnimationFrame(raf)
      observer.disconnect()
      accentObserver.disconnect()
      for (const rec of flights.values()) {
        if (rec.tube) {
          rec.tube.geometry.dispose()
          rec.tube.material.dispose()
        }
        if (rec.head) rec.head.material.dispose()
      }
      flights.clear()
      renderer.dispose()
      disposeTree(scene)
      if (renderer.domElement.parentNode === host) host.removeChild(renderer.domElement)
    },
  }
  renderer.domElement.__orbitScene = api
  liveScenes.add(api)
  return api
}
