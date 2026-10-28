/* Three.js 亮色科幻星图：铺满宿主，无独立底色依赖（与页面表面同色）。 */
import * as THREE from "three"

const NODE_COLORS = [
  0x0284c7, 0x7c3aed, 0x059669, 0xd97706,
  0xdb2777, 0x2563eb, 0xe11d48, 0x0d9488,
]

function clamp(value, low = 0, high = 1) {
  return Math.min(high, Math.max(low, value))
}

function hash(text) {
  let value = 0
  for (const char of String(text)) value = (value * 31 + char.charCodeAt(0)) >>> 0
  return value
}

function makeSpherePoints(count, radius, colorA, colorB, colorC, spread = 0.08) {
  const positions = new Float32Array(count * 3)
  const colors = new Float32Array(count * 3)
  const golden = Math.PI * (3 - Math.sqrt(5))
  const a = new THREE.Color(colorA)
  const b = new THREE.Color(colorB)
  const c = new THREE.Color(colorC)
  for (let i = 0; i < count; i += 1) {
    const y = 1 - (i / Math.max(1, count - 1)) * 2
    const r = Math.sqrt(Math.max(0, 1 - y * y))
    const theta = golden * i
    const jitter = 1 - spread / 2 + (hash(`j${i}`) % 100) / 100 * spread
    positions[i * 3] = Math.cos(theta) * r * radius * jitter
    positions[i * 3 + 1] = y * radius * jitter
    positions[i * 3 + 2] = Math.sin(theta) * r * radius * jitter
    const mix = (hash(`c${i}`) % 100) / 100
    const color = a.clone().lerp(b, mix * 0.75).lerp(c, mix > 0.72 ? 0.4 : 0.1)
    colors[i * 3] = color.r
    colors[i * 3 + 1] = color.g
    colors[i * 3 + 2] = color.b
  }
  return { positions, colors }
}

function makeVolumePoints(count, radius) {
  const positions = new Float32Array(count * 3)
  const colors = new Float32Array(count * 3)
  const a = new THREE.Color(0x93c5fd)
  const b = new THREE.Color(0xc4b5fd)
  for (let i = 0; i < count; i += 1) {
    // random in ball
    let x = 0
    let y = 0
    let z = 0
    for (;;) {
      x = Math.random() * 2 - 1
      y = Math.random() * 2 - 1
      z = Math.random() * 2 - 1
      if (x * x + y * y + z * z <= 1) break
    }
    const dist = 0.55 + Math.random() * 0.45
    positions[i * 3] = x * radius * dist
    positions[i * 3 + 1] = y * radius * dist
    positions[i * 3 + 2] = z * radius * dist
    const color = a.clone().lerp(b, Math.random())
    colors[i * 3] = color.r
    colors[i * 3 + 1] = color.g
    colors[i * 3 + 2] = color.b
  }
  return { positions, colors }
}

function ellipsePoints(rx, ry, segments = 160) {
  const pts = []
  for (let i = 0; i <= segments; i += 1) {
    const t = (i / segments) * Math.PI * 2
    pts.push(new THREE.Vector3(Math.cos(t) * rx, 0, Math.sin(t) * ry))
  }
  return pts
}

function makeRingTicks(rx, ry, count, color) {
  const positions = []
  const colors = []
  const col = new THREE.Color(color)
  for (let i = 0; i < count; i += 1) {
    const t = (i / count) * Math.PI * 2
    const x = Math.cos(t) * rx
    const z = Math.sin(t) * ry
    const inward = 0.92
    positions.push(x, 0, z, x * inward, 0, z * inward)
    colors.push(col.r, col.g, col.b, col.r, col.g, col.b)
  }
  const geo = new THREE.BufferGeometry()
  geo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3))
  geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3))
  return new THREE.LineSegments(geo, new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.35,
    depthWrite: false,
  }))
}

export function createNebulaScene(host, options = {}) {
  const state = {
    nodes: options.nodes || [],
    reduced: Boolean(options.reduced),
    onFrame: typeof options.onFrame === "function" ? options.onFrame : null,
    disposed: false,
    width: 1,
    height: 1,
  }

  const scene = new THREE.Scene()
  // 与整页表面同色，不另开一块底
  const bg = new THREE.Color(0xffffff)
  scene.background = bg
  scene.fog = new THREE.FogExp2(0xffffff, 0.012)

  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 200)
  // 拉远并收窄视角，给轨道/节点留出边距，避免槽位边角被裁
  camera.position.set(0, 0.65, 10.4)
  camera.lookAt(0, 0, 0)

  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: false,
    powerPreference: "high-performance",
  })
  renderer.setClearColor(bg, 1)
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
  renderer.outputColorSpace = THREE.SRGBColorSpace
  renderer.autoClear = true
  renderer.domElement.style.display = "block"
  renderer.domElement.style.width = "100%"
  renderer.domElement.style.height = "100%"
  renderer.domElement.style.pointerEvents = "none"
  host.appendChild(renderer.domElement)

  // dense field dust filling the frame
  {
    const count = 700
    const positions = new Float32Array(count * 3)
    for (let i = 0; i < count; i += 1) {
      positions[i * 3] = (Math.random() - 0.5) * 14
      positions[i * 3 + 1] = (Math.random() - 0.5) * 9
      positions[i * 3 + 2] = (Math.random() - 0.5) * 11 - 1
    }
    const geo = new THREE.BufferGeometry()
    geo.setAttribute("position", new THREE.BufferAttribute(positions, 3))
    scene.add(new THREE.Points(geo, new THREE.PointsMaterial({
      color: 0x94a3b8,
      size: 0.026,
      transparent: true,
      opacity: 0.45,
      depthWrite: false,
      sizeAttenuation: true,
    })))
  }

  // HUD disc grid under the system
  {
    const grid = new THREE.Group()
    grid.rotation.x = Math.PI / 2
    grid.position.y = -1.15
    const disc = new THREE.Mesh(
      new THREE.RingGeometry(0.45, 3.15, 96),
      new THREE.MeshBasicMaterial({
        color: 0xcbd5e1,
        transparent: true,
        opacity: 0.14,
        side: THREE.DoubleSide,
        depthWrite: false,
      }),
    )
    grid.add(disc)
    for (let r = 0.9; r <= 2.95; r += 0.55) {
      const pts = ellipsePoints(r, r, 96)
      grid.add(new THREE.LineLoop(
        new THREE.BufferGeometry().setFromPoints(pts),
        new THREE.LineBasicMaterial({ color: 0x94a3b8, transparent: true, opacity: 0.18 }),
      ))
    }
    for (let i = 0; i < 16; i += 1) {
      const a = (i / 16) * Math.PI * 2
      grid.add(new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(Math.cos(a) * 0.55, 0, Math.sin(a) * 0.55),
          new THREE.Vector3(Math.cos(a) * 3.0, 0, Math.sin(a) * 3.0),
        ]),
        new THREE.LineBasicMaterial({ color: 0xcbd5e1, transparent: true, opacity: 0.14 }),
      ))
    }
    scene.add(grid)
  }

  const coreGroup = new THREE.Group()
  scene.add(coreGroup)

  // layered energy shells
  ;[
    { r: 1.28, color: 0xbae6fd, opacity: 0.12 },
    { r: 1.0, color: 0x7dd3fc, opacity: 0.16 },
    { r: 0.74, color: 0x38bdf8, opacity: 0.22 },
    { r: 0.4, color: 0xffffff, opacity: 0.9 },
  ].forEach((shell) => {
    coreGroup.add(new THREE.Mesh(
      new THREE.SphereGeometry(shell.r, 48, 48),
      new THREE.MeshBasicMaterial({
        color: shell.color,
        transparent: true,
        opacity: shell.opacity,
        depthWrite: false,
      }),
    ))
  })

  // dense particle core + halo volume
  const sphereData = makeSpherePoints(1400, 1.4, 0x0ea5e9, 0x8b5cf6, 0x22d3ee, 0.12)
  const sphereGeo = new THREE.BufferGeometry()
  sphereGeo.setAttribute("position", new THREE.BufferAttribute(sphereData.positions, 3))
  sphereGeo.setAttribute("color", new THREE.BufferAttribute(sphereData.colors, 3))
  const spherePoints = new THREE.Points(sphereGeo, new THREE.PointsMaterial({
    size: 0.048,
    vertexColors: true,
    transparent: true,
    opacity: 0.92,
    depthWrite: false,
    sizeAttenuation: true,
  }))
  coreGroup.add(spherePoints)

  const volumeData = makeVolumePoints(900, 1.9)
  const volumeGeo = new THREE.BufferGeometry()
  volumeGeo.setAttribute("position", new THREE.BufferAttribute(volumeData.positions, 3))
  volumeGeo.setAttribute("color", new THREE.BufferAttribute(volumeData.colors, 3))
  const volumePoints = new THREE.Points(volumeGeo, new THREE.PointsMaterial({
    size: 0.034,
    vertexColors: true,
    transparent: true,
    opacity: 0.42,
    depthWrite: false,
    sizeAttenuation: true,
  }))
  coreGroup.add(volumePoints)

  const outerData = makeSpherePoints(600, 2.2, 0x67e8f9, 0xa78bfa, 0x5eead4, 0.2)
  const outerGeo = new THREE.BufferGeometry()
  outerGeo.setAttribute("position", new THREE.BufferAttribute(outerData.positions, 3))
  outerGeo.setAttribute("color", new THREE.BufferAttribute(outerData.colors, 3))
  const outerPoints = new THREE.Points(outerGeo, new THREE.PointsMaterial({
    size: 0.03,
    vertexColors: true,
    transparent: true,
    opacity: 0.38,
    depthWrite: false,
  }))
  coreGroup.add(outerPoints)

  // tech orbits — fill more of the frame
  const ringSpecs = [
    { rx: 1.55, ry: 1.32, tiltX: 0.58, tiltZ: 0.1, speed: 0.22, color: 0x0284c7, opacity: 0.72 },
    { rx: 1.95, ry: 1.6, tiltX: -0.4, tiltZ: -0.2, speed: -0.16, color: 0x7c3aed, opacity: 0.64 },
    { rx: 2.35, ry: 1.9, tiltX: 0.26, tiltZ: 0.34, speed: 0.11, color: 0x0d9488, opacity: 0.56 },
    { rx: 2.75, ry: 2.18, tiltX: -0.16, tiltZ: 0.12, speed: -0.08, color: 0x2563eb, opacity: 0.44 },
    { rx: 3.05, ry: 2.42, tiltX: 0.42, tiltZ: -0.2, speed: 0.06, color: 0xdb2777, opacity: 0.34 },
  ]

  const ringGroups = ringSpecs.map((spec) => {
    const group = new THREE.Group()
    group.rotation.x = spec.tiltX
    group.rotation.z = spec.tiltZ
    scene.add(group)

    const curve = new THREE.CatmullRomCurve3(ellipsePoints(spec.rx, spec.ry, 180))
    group.add(new THREE.Mesh(
      new THREE.TubeGeometry(curve, 180, 0.022, 8, true),
      new THREE.MeshBasicMaterial({
        color: spec.color,
        transparent: true,
        opacity: spec.opacity,
        depthWrite: false,
      }),
    ))
    // secondary thin rail
    group.add(new THREE.Mesh(
      new THREE.TubeGeometry(
        new THREE.CatmullRomCurve3(ellipsePoints(spec.rx * 0.985, spec.ry * 0.985, 120)),
        120, 0.008, 5, true,
      ),
      new THREE.MeshBasicMaterial({
        color: 0xffffff,
        transparent: true,
        opacity: 0.35,
        depthWrite: false,
      }),
    ))
    group.add(makeRingTicks(spec.rx, spec.ry, 36, spec.color))

    const sparkGeo = new THREE.SphereGeometry(0.055, 12, 12)
    const sparks = [0, 1, 2, 3].map((index) => {
      const spark = new THREE.Mesh(
        sparkGeo,
        new THREE.MeshBasicMaterial({ color: spec.color, transparent: true, opacity: 0.95 }),
      )
      spark.userData.offset = index * ((Math.PI * 2) / 4)
      group.add(spark)
      return spark
    })
    return { group, spec, sparks }
  })

  // energy arcs connecting layers
  const arcGroup = new THREE.Group()
  scene.add(arcGroup)
  for (let i = 0; i < 8; i += 1) {
    const a0 = (i / 8) * Math.PI * 2
    const a1 = a0 + 0.9
    const r0 = 1.4
    const r1 = 2.75
    const p0 = new THREE.Vector3(Math.cos(a0) * r0, Math.sin(a0) * 0.24, Math.sin(a0) * r0)
    const p3 = new THREE.Vector3(Math.cos(a1) * r1, Math.cos(a1) * 0.2, Math.sin(a1) * r1)
    const mid = p0.clone().lerp(p3, 0.5).add(new THREE.Vector3(0, 0.85 + (i % 3) * 0.16, 0))
    const curve = new THREE.QuadraticBezierCurve3(p0, mid, p3)
    arcGroup.add(new THREE.Mesh(
      new THREE.TubeGeometry(curve, 40, 0.01, 5, false),
      new THREE.MeshBasicMaterial({
        color: i % 2 ? 0x38bdf8 : 0xa78bfa,
        transparent: true,
        opacity: 0.28,
        depthWrite: false,
      }),
    ))
  }

  const nodesGroup = new THREE.Group()
  scene.add(nodesGroup)
  const nodeMeshes = []
  const linkLine = new THREE.LineSegments(
    new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.78,
      depthWrite: false,
    }),
  )
  scene.add(linkLine)

  const rebuildNodes = (nodes) => {
    while (nodesGroup.children.length) {
      const child = nodesGroup.children.pop()
      child.geometry?.dispose?.()
      if (child.material) {
        if (Array.isArray(child.material)) child.material.forEach((m) => m.dispose())
        else child.material.dispose()
      }
    }
    nodeMeshes.length = 0
    nodes.forEach((node, index) => {
      const color = new THREE.Color(NODE_COLORS[index % NODE_COLORS.length])
      const glow = new THREE.Mesh(
        new THREE.SphereGeometry(0.05, 14, 14),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.95 }),
      )
      const halo = new THREE.Mesh(
        new THREE.SphereGeometry(0.14, 14, 14),
        new THREE.MeshBasicMaterial({
          color,
          transparent: true,
          opacity: 0.16,
          depthWrite: false,
        }),
      )
      const pivot = new THREE.Group()
      pivot.add(glow)
      pivot.add(halo)
      pivot.userData = {
        id: node.id,
        label: node.label,
        color: `#${color.getHexString()}`,
        pct: node.pct,
        score: node.score,
        ring: node.ring % ringSpecs.length,
        baseAngle: node.baseAngle,
        drift: node.drift,
        bob: node.bob,
      }
      nodesGroup.add(pivot)
      nodeMeshes.push(pivot)
    })
  }
  rebuildNodes(state.nodes)

  const clock = new THREE.Clock()
  const projected = new THREE.Vector3()
  let raf = 0

  const resize = () => {
    if (state.disposed) return
    const rect = host.getBoundingClientRect()
    const width = Math.max(1, Math.floor(rect.width))
    const height = Math.max(1, Math.floor(rect.height))
    state.width = width
    state.height = height
    camera.aspect = width / height
    camera.updateProjectionMatrix()
    renderer.setSize(width, height, false)
  }

  const observer = new ResizeObserver(resize)
  observer.observe(host)
  resize()

  const animate = () => {
    if (state.disposed) return
    raf = requestAnimationFrame(animate)
    const t = clock.getElapsedTime()
    const reduced = state.reduced

    const scoredN = nodeMeshes.filter((m) => m.userData.score != null).length
    const energy = 0.85 + scoredN * 0.08
    coreGroup.rotation.y = reduced ? 0.35 : t * 0.28
    coreGroup.rotation.x = reduced ? 0.12 : Math.sin(t * 0.38) * 0.16
    coreGroup.scale.setScalar(0.98 + Math.sin(t * 1.6) * 0.012 * energy)
    spherePoints.material.opacity = Math.min(0.98, 0.78 + scoredN * 0.045)
    spherePoints.rotation.y = -t * 0.14
    volumePoints.rotation.y = t * 0.1
    volumePoints.rotation.z = Math.sin(t * 0.35) * 0.12
    outerPoints.rotation.y = -t * 0.07
    arcGroup.rotation.y = t * 0.05
    arcGroup.visible = scoredN > 0 || nodeMeshes.length > 0

    ringGroups.forEach((ring) => {
      ring.group.rotation.y = t * ring.spec.speed
      ring.sparks.forEach((spark) => {
        const angle = t * ring.spec.speed * 2.4 + spark.userData.offset
        spark.position.set(
          Math.cos(angle) * ring.spec.rx,
          Math.sin(t * 1.7 + spark.userData.offset) * 0.06,
          Math.sin(angle) * ring.spec.ry,
        )
      })
    })

    const positions = []
    const colors = []
    const overlay = []

    nodeMeshes.forEach((mesh, index) => {
      const data = mesh.userData
      const ring = ringSpecs[data.ring]
      const angle = data.baseAngle + t * ring.speed * Math.PI * 2 + data.drift * t
      const local = new THREE.Vector3(
        Math.cos(angle) * ring.rx,
        Math.sin(t * 1.35 + index) * 0.22 * (0.5 + Math.abs(data.bob) * 8),
        Math.sin(angle) * ring.ry,
      )
      local.applyEuler(new THREE.Euler(ring.tiltX, 0, ring.tiltZ))
      mesh.position.copy(local)
      mesh.scale.setScalar(0.95 + Math.sin(t * 2.6 + index) * 0.1)

      projected.copy(mesh.position).project(camera)
      const padX = Math.min(84, state.width * 0.16)
      const padY = Math.min(64, state.height * 0.16)
      const rawX = (projected.x * 0.5 + 0.5) * state.width
      const rawY = (-projected.y * 0.5 + 0.5) * state.height
      overlay.push({
        id: data.id,
        label: data.label,
        color: data.color,
        pct: data.pct,
        score: data.score,
        x: clamp(rawX, padX, state.width - padX),
        y: clamp(rawY, padY, state.height - padY),
        z: projected.z,
        visible: projected.z < 1,
      })
    })

    for (let i = 0; i < nodeMeshes.length; i += 1) {
      for (let j = i + 1; j < nodeMeshes.length; j += 1) {
        const a = nodeMeshes[i]
        const b = nodeMeshes[j]
        const dist = a.position.distanceTo(b.position)
        if (dist > 4.6) continue
        const scored = a.userData.score != null && b.userData.score != null
        const pulse = 0.5 + 0.5 * Math.sin(t * 2.4 + i * 1.7 + j)
        // 未评分时仍保留淡连线；已评分时更亮、更稳
        if (!scored && pulse < 0.22) continue
        const ca = new THREE.Color(a.userData.color)
        const cb = new THREE.Color(b.userData.color)
        const boost = clamp(1.12 - dist / 4.6) * (scored ? 1.35 : 0.7) * (0.65 + pulse * 0.45)
        positions.push(a.position.x, a.position.y, a.position.z, b.position.x, b.position.y, b.position.z)
        colors.push(ca.r * boost, ca.g * boost, ca.b * boost, cb.r * boost, cb.g * boost, cb.b * boost)
      }
    }
    linkLine.geometry.dispose()
    if (positions.length) {
      const geo = new THREE.BufferGeometry()
      geo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3))
      geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3))
      linkLine.geometry = geo
      linkLine.visible = true
    } else {
      linkLine.geometry = new THREE.BufferGeometry()
      linkLine.visible = false
    }

    if (!reduced) {
      camera.position.x = Math.sin(t * 0.14) * 0.08
      camera.position.y = 0.65 + Math.sin(t * 0.18) * 0.04
      camera.position.z = 10.4
      camera.lookAt(0, 0.02, 0)
    }

    renderer.setClearColor(bg, 1)
    renderer.clear()
    renderer.render(scene, camera)

    if (state.onFrame) {
      state.onFrame({
        nodes: overlay,
        center: { x: state.width / 2, y: state.height * 0.48 },
        width: state.width,
        height: state.height,
      })
    }
  }

  raf = requestAnimationFrame(animate)

  return {
    setNodes(nodes) {
      state.nodes = nodes || []
      rebuildNodes(state.nodes)
    },
    setReduced(value) {
      state.reduced = Boolean(value)
    },
    setOnFrame(fn) {
      state.onFrame = typeof fn === "function" ? fn : null
    },
    resize,
    dispose() {
      state.disposed = true
      cancelAnimationFrame(raf)
      observer.disconnect()
      renderer.dispose()
      scene.traverse((obj) => {
        if (obj.geometry) obj.geometry.dispose()
        if (obj.material) {
          if (Array.isArray(obj.material)) obj.material.forEach((m) => m.dispose())
          else obj.material.dispose()
        }
      })
      if (renderer.domElement.parentNode === host) host.removeChild(renderer.domElement)
    },
  }
}
