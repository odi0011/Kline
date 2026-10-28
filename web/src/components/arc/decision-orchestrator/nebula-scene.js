/* 亮色轨道星云：铺满宿主的交叉椭圆环 + 稠密粒子网。
   单位坐标 [-1,1] 经拉伸贴合视口（约 86% 宽高），环在拉伸之外旋转，避免非均匀缩放把圆剪成斜切。
   配色跟页面 --accent（默认币安金）以及青 / 品红 / 涨跌色，不走深色青绿网络那套。 */
import * as THREE from "three"

const FILL = 0.92
const FOV = 30
const BASE_Z = 7.6

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

const SLOT_KEYS = [
  "accent", "deep", "cyan", "cyanBright", "magenta", "magentaBright",
  "success", "successDeep", "sky", "blue", "danger",
]

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

function ellipseXY(rx, ry, segments) {
  const pts = []
  for (let i = 0; i < segments; i += 1) {
    const t = (i / segments) * Math.PI * 2
    pts.push(new THREE.Vector3(Math.cos(t) * rx, Math.sin(t) * ry, 0))
  }
  return pts
}

function superellipse(rng, r0, r1) {
  const power = 2.45
  for (let attempt = 0; attempt < 24; attempt += 1) {
    const x = rng() * 2 - 1
    const y = rng() * 2 - 1
    const rho = (Math.abs(x) ** power + Math.abs(y) ** power) ** (1 / power)
    if (rho < r0 || rho > r1) continue
    const z = (rng() - 0.5) * 0.42 * (1.08 - rho)
    return { x: x * 0.96, y: y * 0.96, z }
  }
  const angle = rng() * Math.PI * 2
  const radius = r0 + (r1 - r0) * rng()
  return {
    x: Math.cos(angle) * radius * 0.9,
    y: Math.sin(angle) * radius * 0.9,
    z: (rng() - 0.5) * 0.28,
  }
}

function gaussian(rng, sx, sy, sz) {
  const u = Math.max(1e-6, rng())
  const v = rng()
  const mag = Math.sqrt(-2 * Math.log(u))
  const x = mag * Math.cos(Math.PI * 2 * v) * sx
  const u2 = Math.max(1e-6, rng())
  const v2 = rng()
  const mag2 = Math.sqrt(-2 * Math.log(u2))
  const y = mag2 * Math.cos(Math.PI * 2 * v2) * sy
  return {
    x: clamp(x, -0.72, 0.72),
    y: clamp(y, -0.72, 0.72),
    z: (rng() - 0.5) * 2 * sz,
  }
}

function paintBuffer(attr, slots, mode, palette) {
  const array = attr.array
  const scratch = new THREE.Color()
  for (let i = 0; i < slots.length; i += 1) {
    scratch.copy(toneColor(palette, slots[i], mode))
    array[i * 3] = scratch.r
    array[i * 3 + 1] = scratch.g
    array[i * 3 + 2] = scratch.b
  }
  attr.needsUpdate = true
}

function makePoints(positions, slots, sizePx, opacity) {
  const geo = new THREE.BufferGeometry()
  geo.setAttribute("position", new THREE.BufferAttribute(positions, 3))
  const colors = new Float32Array(slots.length * 3)
  geo.setAttribute("color", new THREE.BufferAttribute(colors, 3))
  const material = new THREE.PointsMaterial({
    size: sizePx,
    vertexColors: true,
    transparent: true,
    opacity,
    depthWrite: false,
    sizeAttenuation: false,
  })
  const points = new THREE.Points(geo, material)
  points.frustumCulled = false
  return { points, geo, material, slots, colorAttr: geo.getAttribute("color") }
}

function glowTexture(accent) {
  const canvas = document.createElement("canvas")
  canvas.width = 256
  canvas.height = 256
  const ctx = canvas.getContext("2d")
  const hex = `#${accent.getHexString()}`
  const grd = ctx.createRadialGradient(128, 128, 8, 128, 128, 128)
  grd.addColorStop(0, hex)
  grd.addColorStop(0.22, "rgba(34, 211, 238, 0.55)")
  grd.addColorStop(0.5, "rgba(232, 121, 249, 0.28)")
  grd.addColorStop(1, "rgba(255, 255, 255, 0)")
  ctx.clearRect(0, 0, 256, 256)
  ctx.globalAlpha = 0.42
  ctx.fillStyle = grd
  ctx.fillRect(0, 0, 256, 256)
  const tex = new THREE.CanvasTexture(canvas)
  tex.colorSpace = THREE.SRGBColorSpace
  return tex
}

function buildMist(rng, palette) {
  const layers = [
    { count: 900, sx: 0.16, sy: 0.14, sz: 0.1, size: 3.1, opacity: 0.92, bias: [SLOT.accent, SLOT.deep, SLOT.accent, SLOT.magentaBright] },
    { count: 1100, sx: 0.34, sy: 0.3, sz: 0.16, size: 2.3, opacity: 0.62, bias: [SLOT.accent, SLOT.cyanBright, SLOT.magentaBright, SLOT.sky, SLOT.deep] },
    { count: 720, sx: 0.5, sy: 0.46, sz: 0.2, size: 1.7, opacity: 0.34, bias: [SLOT.cyan, SLOT.magenta, SLOT.blue, SLOT.success, SLOT.accent] },
  ]
  return layers.map((layer) => {
    const positions = new Float32Array(layer.count * 3)
    const slots = new Uint8Array(layer.count)
    for (let i = 0; i < layer.count; i += 1) {
      const lobe = i % 7 === 0
      const p = gaussian(rng, layer.sx, layer.sy, layer.sz)
      if (lobe) {
        p.x = clamp(p.x * 0.65 - 0.3, -0.7, 0.15)
        p.y = clamp(p.y * 0.7 - 0.06, -0.55, 0.4)
      }
      positions[i * 3] = p.x
      positions[i * 3 + 1] = p.y
      positions[i * 3 + 2] = p.z
      slots[i] = layer.bias[i % layer.bias.length]
    }
    const made = makePoints(positions, slots, layer.size, layer.opacity)
    paintBuffer(made.colorAttr, slots, "particle", palette)
    made.points.renderOrder = 3
    return made
  })
}

function buildNetwork(rng, palette) {
  const inner = []
  const outer = []
  for (let i = 0; i < 260; i += 1) {
    const p = gaussian(rng, 0.2, 0.17, 0.12)
    inner.push(p)
  }
  for (let i = 0; i < 120; i += 1) inner.push(superellipse(rng, 0.18, 0.48))
  for (let i = 0; i < 190; i += 1) outer.push(superellipse(rng, 0.5, 0.98))

  const nodes = inner.concat(outer)
  const innerCount = inner.length
  const positions = new Float32Array(nodes.length * 3)
  const slots = new Uint8Array(nodes.length)
  nodes.forEach((p, i) => {
    positions[i * 3] = p.x
    positions[i * 3 + 1] = p.y
    positions[i * 3 + 2] = p.z
    slots[i] = i < innerCount
      ? [SLOT.accent, SLOT.deep, SLOT.magentaBright, SLOT.cyanBright][i % 4]
      : [SLOT.cyan, SLOT.magenta, SLOT.sky, SLOT.success, SLOT.accent, SLOT.blue][i % 6]
  })

  const cell = 0.09
  const buckets = new Map()
  const keyOf = (x, y) => `${Math.floor(x / cell)}:${Math.floor(y / cell)}`
  inner.forEach((p, i) => {
    const key = keyOf(p.x, p.y)
    const list = buckets.get(key)
    if (list) list.push(i)
    else buckets.set(key, [i])
  })

  const edges = []
  const pushEdge = (a, b) => {
    if (a === b || a < 0 || b < 0) return
    const id = a < b ? a * 10000 + b : b * 10000 + a
    edges.push(id)
  }

  inner.forEach((p, i) => {
    const cx = Math.floor(p.x / cell)
    const cy = Math.floor(p.y / cell)
    let best = -1
    let bestD = 0.2
    let second = -1
    let secondD = 0.2
    for (let oy = -2; oy <= 2; oy += 1) {
      for (let ox = -2; ox <= 2; ox += 1) {
        const list = buckets.get(`${cx + ox}:${cy + oy}`)
        if (!list) continue
        for (const j of list) {
          if (j === i) continue
          const q = inner[j]
          const d = (p.x - q.x) ** 2 + (p.y - q.y) ** 2 + (p.z - q.z) ** 2
          if (d < 0.004) continue
          if (d < bestD) {
            second = best
            secondD = bestD
            best = j
            bestD = d
          } else if (d < secondD) {
            second = j
            secondD = d
          }
        }
      }
    }
    if (best >= 0) pushEdge(i, best)
    if (second >= 0 && secondD < 0.045) pushEdge(i, second)
  })

  outer.forEach((p, i) => {
    let best = 0
    let bestD = Infinity
    const stride = 3
    for (let j = 0; j < inner.length; j += stride) {
      const q = inner[j]
      const d = (p.x - q.x) ** 2 + (p.y - q.y) ** 2
      if (d < bestD) {
        bestD = d
        best = j
      }
    }
    pushEdge(innerCount + i, best)
    if (i % 3 === 0) pushEdge(innerCount + i, (best * 7 + 13) % inner.length)
  })

  const unique = [...new Set(edges)]
  const linePositions = new Float32Array(unique.length * 2 * 3)
  const lineSlots = new Uint8Array(unique.length * 2)
  unique.forEach((id, e) => {
    const a = Math.floor(id / 10000)
    const b = id % 10000
    const pa = nodes[a]
    const pb = nodes[b]
    const o = e * 6
    linePositions[o] = pa.x
    linePositions[o + 1] = pa.y
    linePositions[o + 2] = pa.z
    linePositions[o + 3] = pb.x
    linePositions[o + 4] = pb.y
    linePositions[o + 5] = pb.z
    lineSlots[e * 2] = slots[a]
    lineSlots[e * 2 + 1] = slots[b]
  })

  const lineGeo = new THREE.BufferGeometry()
  lineGeo.setAttribute("position", new THREE.BufferAttribute(linePositions, 3))
  const lineColors = new Float32Array(lineSlots.length * 3)
  lineGeo.setAttribute("color", new THREE.BufferAttribute(lineColors, 3))
  const lineMat = new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.78,
    depthWrite: false,
  })
  const lines = new THREE.LineSegments(lineGeo, lineMat)
  lines.frustumCulled = false
  lines.renderOrder = 4
  const lineColorAttr = lineGeo.getAttribute("color")
  paintBuffer(lineColorAttr, lineSlots, "line", palette)

  const dots = makePoints(positions, slots, 3.3, 0.9)
  paintBuffer(dots.colorAttr, slots, "particle", palette)
  dots.points.renderOrder = 5

  return {
    lines,
    lineSlots,
    lineColorAttr,
    dots,
    edgeCount: unique.length,
  }
}

function buildDust(rng) {
  const count = 520
  const positions = new Float32Array(count * 3)
  const slots = new Uint8Array(count)
  for (let i = 0; i < count; i += 1) {
    const p = superellipse(rng, 0.05, 1)
    positions[i * 3] = p.x
    positions[i * 3 + 1] = p.y
    positions[i * 3 + 2] = p.z * 0.4 - 0.15
    slots[i] = i % 5 === 0 ? SLOT.accent : SLOT.blue
  }
  const made = makePoints(positions, slots, 1.45, 0.4)
  made.points.renderOrder = 1
  return made
}

function buildRadar() {
  const group = new THREE.Group()
  const material = new THREE.LineBasicMaterial({
    color: 0xb7c2d0,
    transparent: true,
    opacity: 0.55,
    depthWrite: false,
  })
  for (const radius of [0.28, 0.46, 0.64, 0.8, 0.94]) {
    group.add(new THREE.LineLoop(
      new THREE.BufferGeometry().setFromPoints(ellipseXY(radius, radius * 0.96, 96)),
      material,
    ))
  }
  for (let i = 0; i < 12; i += 1) {
    const a = (i / 12) * Math.PI * 2
    group.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(Math.cos(a) * 0.16, Math.sin(a) * 0.15, 0),
        new THREE.Vector3(Math.cos(a) * 0.96, Math.sin(a) * 0.92, 0),
      ]),
      material,
    ))
  }
  group.position.z = -0.22
  group.renderOrder = 0
  return group
}

function buildRings(palette) {
  const specs = [
    { rx: 0.72, ry: 0.56, tiltX: 1.08, tiltZ: 0.18, speed: 0.11, hold: 0.4, slot: SLOT.accent, opacity: 0.95, tube: 0.02 },
    { rx: 0.86, ry: 0.62, tiltX: 0.78, tiltZ: -0.48, speed: -0.08, hold: 1.1, slot: SLOT.cyan, opacity: 0.9, tube: 0.016 },
    { rx: 0.64, ry: 0.84, tiltX: 0.42, tiltZ: 0.96, speed: 0.07, hold: 2.2, slot: SLOT.magenta, opacity: 0.88, tube: 0.015 },
    { rx: 0.94, ry: 0.7, tiltX: 1.24, tiltZ: 0.28, speed: -0.05, hold: 0.8, slot: SLOT.successDeep, opacity: 0.82, tube: 0.013 },
    { rx: 0.52, ry: 0.44, tiltX: 1.32, tiltZ: -0.22, speed: 0.14, hold: 1.7, slot: SLOT.deep, opacity: 0.9, tube: 0.018 },
  ]
  return specs.map((spec) => {
    const tilt = new THREE.Group()
    tilt.rotation.x = spec.tiltX
    tilt.rotation.z = spec.tiltZ
    const spin = new THREE.Group()
    const stretch = new THREE.Group()
    tilt.add(spin)
    spin.add(stretch)

    const curve = new THREE.CatmullRomCurve3(ellipseXY(spec.rx, spec.ry, 140), true, "catmullrom", 0.05)
    const color = toneColor(palette, spec.slot, "particle")
    const tube = new THREE.Mesh(
      new THREE.TubeGeometry(curve, 140, spec.tube, 6, true),
      new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity: spec.opacity,
        depthWrite: false,
      }),
    )
    tube.renderOrder = 6
    const rail = new THREE.Mesh(
      new THREE.TubeGeometry(
        new THREE.CatmullRomCurve3(ellipseXY(spec.rx * 0.985, spec.ry * 0.985, 100), true),
        100, spec.tube * 0.38, 4, true,
      ),
      new THREE.MeshBasicMaterial({
        color: 0xffffff,
        transparent: true,
        opacity: 0.55,
        depthWrite: false,
      }),
    )
    rail.renderOrder = 7
    stretch.add(tube)
    stretch.add(rail)

    const tickPositions = []
    const tickSlots = []
    const ticks = 42
    for (let i = 0; i < ticks; i += 1) {
      const a = (i / ticks) * Math.PI * 2
      const x = Math.cos(a) * spec.rx
      const y = Math.sin(a) * spec.ry
      tickPositions.push(x, y, 0, x * 0.93, y * 0.93, 0)
      tickSlots.push(spec.slot, spec.slot)
    }
    const tickGeo = new THREE.BufferGeometry()
    tickGeo.setAttribute("position", new THREE.Float32BufferAttribute(tickPositions, 3))
    const tickColors = new Float32Array(tickSlots.length * 3)
    tickGeo.setAttribute("color", new THREE.BufferAttribute(tickColors, 3))
    const tickColorAttr = tickGeo.getAttribute("color")
    paintBuffer(tickColorAttr, tickSlots, "line", palette)
    const tickLines = new THREE.LineSegments(tickGeo, new THREE.LineBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.45,
      depthWrite: false,
    }))
    tickLines.renderOrder = 6
    stretch.add(tickLines)

    const dustCount = 70
    const dustPos = new Float32Array(dustCount * 3)
    const dustSlots = new Uint8Array(dustCount)
    for (let i = 0; i < dustCount; i += 1) {
      const a = (i / dustCount) * Math.PI * 2 + (i % 5) * 0.02
      const j = 1 + ((i * 17) % 9) * 0.012
      dustPos[i * 3] = Math.cos(a) * spec.rx * j
      dustPos[i * 3 + 1] = Math.sin(a) * spec.ry * j
      dustPos[i * 3 + 2] = ((i % 7) - 3) * 0.01
      dustSlots[i] = spec.slot
    }
    const dust = makePoints(dustPos, dustSlots, 2.4, 0.75)
    paintBuffer(dust.colorAttr, dustSlots, "particle", palette)
    dust.points.renderOrder = 6
    stretch.add(dust.points)

    const sparkGeo = new THREE.SphereGeometry(0.032, 12, 12)
    const haloGeo = new THREE.SphereGeometry(0.058, 10, 10)
    const sparks = [0, 1, 2, 3].map((index) => {
      const spark = new THREE.Mesh(sparkGeo, new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity: 0.98,
        depthWrite: false,
      }))
      const halo = new THREE.Mesh(haloGeo, new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity: 0.22,
        depthWrite: false,
      }))
      spark.add(halo)
      spark.userData.offset = index * (Math.PI / 2)
      spark.renderOrder = 8
      stretch.add(spark)
      return spark
    })

    return {
      spec, tilt, spin, stretch, tube, sparks, dust, tickSlots, tickColorAttr,
    }
  })
}

export function createNebulaScene(host, options = {}) {
  const state = {
    nodes: options.nodes || [],
    reduced: Boolean(options.reduced),
    onFrame: typeof options.onFrame === "function" ? options.onFrame : null,
    disposed: false,
    width: 1,
    height: 1,
    accentHex: "",
  }

  let palette = makePalette()
  state.accentHex = palette.accent.getHexString()
  const bg = pageBackground()

  const scene = new THREE.Scene()
  scene.background = bg

  const camera = new THREE.PerspectiveCamera(FOV, 1, 0.1, 80)
  camera.position.set(0, 0.04, BASE_Z)
  camera.lookAt(0, 0, 0)

  let renderer
  try {
    renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: false,
      powerPreference: "high-performance",
    })
  } catch {
    return {
      setNodes() {},
      setReduced() {},
      setOnFrame() {},
      resize() {},
      dispose() {},
    }
  }
  renderer.setClearColor(bg, 1)
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
  renderer.outputColorSpace = THREE.SRGBColorSpace
  renderer.domElement.style.display = "block"
  renderer.domElement.style.width = "100%"
  renderer.domElement.style.height = "100%"
  renderer.domElement.style.pointerEvents = "none"
  host.appendChild(renderer.domElement)

  const cloudTilt = new THREE.Group()
  cloudTilt.rotation.x = 0.06
  const cloudStretch = new THREE.Group()
  cloudTilt.add(cloudStretch)
  scene.add(cloudTilt)

  const rng = mulberry32(0xa11ce)

  cloudStretch.add(buildRadar())
  const dust = buildDust(rng)
  paintBuffer(dust.colorAttr, dust.slots, "particle", palette)
  cloudStretch.add(dust.points)
  const mist = buildMist(rng, palette)
  mist.forEach((layer) => cloudStretch.add(layer.points))
  const network = buildNetwork(rng, palette)
  cloudStretch.add(network.lines)
  cloudStretch.add(network.dots.points)

  let glowMap = glowTexture(palette.accent)
  const glow = new THREE.Sprite(new THREE.SpriteMaterial({
    map: glowMap,
    transparent: true,
    opacity: 0.55,
    depthWrite: false,
    blending: THREE.NormalBlending,
  }))
  glow.scale.set(1.15, 1.15, 1)
  glow.position.z = -0.05
  glow.renderOrder = 2
  cloudStretch.add(glow)

  const rings = buildRings(palette)
  rings.forEach((ring) => scene.add(ring.tilt))

  const coinRoot = new THREE.Group()
  scene.add(coinRoot)
  const coinGeo = new THREE.SphereGeometry(1, 16, 16)
  const coinHaloGeo = new THREE.SphereGeometry(1, 12, 12)
  const nodeMeshes = []

  const linkPos = new Float32Array(64 * 2 * 3)
  const linkCol = new Float32Array(64 * 2 * 3)
  const linkGeo = new THREE.BufferGeometry()
  linkGeo.setAttribute("position", new THREE.BufferAttribute(linkPos, 3))
  linkGeo.setAttribute("color", new THREE.BufferAttribute(linkCol, 3))
  linkGeo.setDrawRange(0, 0)
  const linkLine = new THREE.LineSegments(linkGeo, new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.7,
    depthWrite: false,
  }))
  linkLine.frustumCulled = false
  linkLine.renderOrder = 7
  scene.add(linkLine)

  const paintCoins = () => {
    nodeMeshes.forEach((mesh, index) => {
      const slot = COIN_SLOTS[index % COIN_SLOTS.length]
      const color = toneColor(palette, slot, "particle")
      mesh.userData.color = `#${color.getHexString()}`
      mesh.userData.slot = slot
      mesh.children.forEach((child) => child.material.color.copy(color))
    })
  }

  const rebuildNodes = (nodes) => {
    while (coinRoot.children.length) {
      const child = coinRoot.children.pop()
      child.traverse((obj) => {
        if (obj.material) {
          const list = Array.isArray(obj.material) ? obj.material : [obj.material]
          list.forEach((m) => m.dispose())
        }
      })
    }
    nodeMeshes.length = 0
    nodes.forEach((node, index) => {
      const slot = COIN_SLOTS[index % COIN_SLOTS.length]
      const color = toneColor(palette, slot, "particle")
      const pivot = new THREE.Group()
      const glowMesh = new THREE.Mesh(coinGeo, new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity: 0.95,
        depthWrite: false,
      }))
      const halo = new THREE.Mesh(coinHaloGeo, new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity: 0.18,
        depthWrite: false,
      }))
      glowMesh.scale.setScalar(0.045)
      halo.scale.setScalar(0.09)
      glowMesh.renderOrder = 9
      halo.renderOrder = 8
      pivot.add(halo)
      pivot.add(glowMesh)
      pivot.userData = {
        id: node.id,
        label: node.label,
        color: `#${color.getHexString()}`,
        pct: node.pct,
        score: node.score,
        ring: (node.ring ?? index) % rings.length,
        baseAngle: node.baseAngle || 0,
        drift: node.drift || 0,
        bob: node.bob || 0,
        slot,
      }
      coinRoot.add(pivot)
      nodeMeshes.push(pivot)
    })
  }
  rebuildNodes(state.nodes)

  const repaint = () => {
    palette = makePalette()
    state.accentHex = palette.accent.getHexString()
    mist.forEach((layer) => paintBuffer(layer.colorAttr, layer.slots, "particle", palette))
    paintBuffer(network.lineColorAttr, network.lineSlots, "line", palette)
    paintBuffer(network.dots.colorAttr, network.dots.slots, "particle", palette)
    paintBuffer(dust.colorAttr, dust.slots, "particle", palette)
    rings.forEach((ring) => {
      const color = toneColor(palette, ring.spec.slot, "particle")
      ring.tube.material.color.copy(color)
      ring.sparks.forEach((spark) => {
        spark.material.color.copy(color)
        spark.children.forEach((child) => child.material.color.copy(color))
      })
      paintBuffer(ring.dust.colorAttr, ring.dust.slots, "particle", palette)
      paintBuffer(ring.tickColorAttr, ring.tickSlots, "line", palette)
    })
    if (glow.material.map) glow.material.map.dispose()
    glowMap = glowTexture(palette.accent)
    glow.material.map = glowMap
    paintCoins()
  }

  const clock = new THREE.Clock()
  const projected = new THREE.Vector3()
  const worldPoint = new THREE.Vector3()
  const centerPoint = new THREE.Vector3()
  let raf = 0
  let stretchX = 1
  let stretchY = 1
  let stretchZ = 1

  const fit = () => {
    const aspect = state.width / Math.max(1, state.height)
    camera.aspect = aspect
    camera.updateProjectionMatrix()
    const vFov = THREE.MathUtils.degToRad(camera.fov)
    const visH = 2 * Math.tan(vFov / 2) * BASE_Z
    const visW = visH * aspect
    stretchX = (visW * FILL) / 2
    stretchY = (visH * FILL) / 2
    stretchZ = (Math.min(visW, visH) * FILL) / 2 * 0.7
    cloudStretch.scale.set(stretchX, stretchY, stretchZ)
    rings.forEach((ring) => ring.stretch.scale.set(stretchX, stretchY, stretchZ))
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

  const renderFrame = (time) => {
    const reduced = state.reduced
    const t = reduced ? 1.15 : time

    cloudTilt.rotation.z = reduced ? 0.03 : Math.sin(t * 0.17) * 0.04
    cloudTilt.rotation.x = reduced ? 0.08 : 0.06 + Math.sin(t * 0.23) * 0.025

    rings.forEach((ring) => {
      ring.spin.rotation.z = reduced ? ring.spec.hold : t * ring.spec.speed
      ring.sparks.forEach((spark) => {
        const angle = (reduced ? ring.spec.hold : t * 0.35) + spark.userData.offset
        spark.position.set(
          Math.cos(angle) * ring.spec.rx,
          Math.sin(angle) * ring.spec.ry,
          0,
        )
      })
    })

    if (!reduced) {
      camera.position.x = Math.sin(t * 0.15) * 0.12
      camera.position.y = 0.04 + Math.sin(t * 0.11) * 0.03
      camera.position.z = BASE_Z
    } else {
      camera.position.set(0, 0.04, BASE_Z)
    }
    camera.lookAt(0, 0, 0)
    camera.updateMatrixWorld(true)
    cloudStretch.updateWorldMatrix(true, true)
    rings.forEach((ring) => ring.stretch.updateWorldMatrix(true, true))

    const positions = linkGeo.getAttribute("position")
    const colors = linkGeo.getAttribute("color")
    let vert = 0
    const overlay = []
    const coinWorld = []

    nodeMeshes.forEach((mesh, index) => {
      const data = mesh.userData
      const ring = rings[data.ring] || rings[0]
      const angle = data.baseAngle + (reduced ? 0 : t * data.drift) + (reduced ? ring.spec.hold * 0.15 : 0)
      worldPoint.set(
        Math.cos(angle) * ring.spec.rx,
        Math.sin(angle) * ring.spec.ry,
        Math.sin(t * 1.3 + index) * 0.02,
      )
      ring.stretch.localToWorld(worldPoint)
      mesh.position.copy(worldPoint)
      const pixel = (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) * BASE_Z) / Math.max(1, state.height)
      mesh.children[0].scale.setScalar(pixel * 11)
      mesh.children[1].scale.setScalar(pixel * 6.5)
      coinWorld.push(worldPoint.clone())

      projected.copy(worldPoint).project(camera)
      const padX = clamp(state.width * 0.07, 52, 78)
      const padY = clamp(state.height * 0.08, 42, 68)
      let rawX = (projected.x * 0.5 + 0.5) * state.width
      let rawY = (-projected.y * 0.5 + 0.5) * state.height
      // 玻璃资金牌占住中心，评分牌沿径向让开，避免盖住总资金数字
      const clearR = Math.min(state.width, state.height) * 0.3
      const rdx = rawX - state.width / 2
      const rdy = rawY - state.height / 2
      const rdist = Math.hypot(rdx, rdy)
      if (rdist < clearR) {
        const push = clearR / Math.max(rdist, 8)
        rawX = state.width / 2 + rdx * push
        rawY = state.height / 2 + rdy * push
      }
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
    cloudStretch.localToWorld(origin)
    for (let i = 0; i < coinWorld.length; i += 1) {
      const a = coinWorld[i]
      const ca = toneColor(palette, nodeMeshes[i].userData.slot, "line")
      const core = toneColor(palette, SLOT.accent, "line")
      pushLink(a, origin, ca, core)
      for (let j = i + 1; j < coinWorld.length; j += 1) {
        const b = coinWorld[j]
        if (a.distanceTo(b) > Math.max(stretchX, stretchY) * 1.15) continue
        const cb = toneColor(palette, nodeMeshes[j].userData.slot, "line")
        pushLink(a, b, ca, cb)
      }
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

    renderer.setClearColor(bg, 1)
    renderer.render(scene, camera)

    if (state.onFrame) {
      state.onFrame({
        nodes: overlay,
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

  return {
    setNodes(nodes) {
      state.nodes = nodes || []
      rebuildNodes(state.nodes)
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
        raf = requestAnimationFrame(loop)
      }
    },
    setOnFrame(fn) {
      state.onFrame = typeof fn === "function" ? fn : null
    },
    resize,
    dispose() {
      state.disposed = true
      cancelAnimationFrame(raf)
      observer.disconnect()
      accentObserver.disconnect()
      renderer.dispose()
      const geometries = new Set()
      const materials = new Set()
      scene.traverse((obj) => {
        if (obj.geometry) geometries.add(obj.geometry)
        if (obj.material) {
          const list = Array.isArray(obj.material) ? obj.material : [obj.material]
          list.forEach((m) => materials.add(m))
        }
      })
      geometries.forEach((geo) => geo.dispose())
      materials.forEach((m) => {
        if (m.map) m.map.dispose()
        m.dispose()
      })
      if (renderer.domElement.parentNode === host) host.removeChild(renderer.domElement)
    },
  }
}
