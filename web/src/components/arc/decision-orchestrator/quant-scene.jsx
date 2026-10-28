import { useEffect, useRef, useState } from "react"
import * as THREE from "three"
import styles from "./decision-orchestrator.module.css"

const NODE_DEFINITIONS = [
  { id: "intake", position: [-3.35, 0.68, 2.05] },
  { id: "features", position: [-2.65, 1.0, -1.7] },
  { id: "decision", position: [0, 1.22, 0] },
  { id: "risk", position: [2.35, 0.96, -1.85] },
  { id: "execution", position: [3.2, 0.78, 1.0] },
  { id: "feedback", position: [0.3, 0.62, 2.9] },
]

const makeGlowTexture = () => {
  const canvas = document.createElement("canvas")
  canvas.width = 64
  canvas.height = 64
  const context = canvas.getContext("2d")
  const gradient = context.createRadialGradient(32, 32, 0, 32, 32, 32)
  gradient.addColorStop(0, "rgba(255,255,255,1)")
  gradient.addColorStop(0.18, "rgba(255,255,255,.82)")
  gradient.addColorStop(0.48, "rgba(255,255,255,.2)")
  gradient.addColorStop(1, "rgba(255,255,255,0)")
  context.fillStyle = gradient
  context.fillRect(0, 0, 64, 64)
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  return texture
}

const arrivalDelay = (pulse) =>
  (pulse.delay || 0) + (pulse.spawn ?? 170) + (pulse.duration || 1400)

export default function QuantScene({
  activeStages,
  signalActive,
  enabled,
  reduced,
  metricPulses,
}) {
  const hostRef = useRef(null)
  const canvasRef = useRef(null)
  const targetRef = useRef(null)
  const activeRef = useRef(activeStages)
  const signalRef = useRef(signalActive)
  const enabledRef = useRef(enabled)
  const pulseScheduleRef = useRef(new Map())
  const [failed, setFailed] = useState(false)
  activeRef.current = activeStages
  signalRef.current = signalActive
  enabledRef.current = enabled

  useEffect(() => {
    const now = performance.now()
    for (const pulse of metricPulses) {
      if (!pulseScheduleRef.current.has(pulse.id)) {
        pulseScheduleRef.current.set(pulse.id, {
          at: now + arrivalDelay(pulse),
          color: pulse.color || "#f0b90b",
          injected: false,
        })
      }
    }
  }, [metricPulses])

  useEffect(() => {
    const host = hostRef.current
    const canvas = canvasRef.current
    const target = targetRef.current
    if (!host || !canvas || !target) return

    let renderer
    try {
      renderer = new THREE.WebGLRenderer({
        canvas,
        alpha: true,
        antialias: true,
        powerPreference: "high-performance",
      })
    } catch {
      setFailed(true)
      return
    }
    setFailed(false)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    renderer.setClearColor(0x000000, 0)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.toneMappingExposure = 0.98
    renderer.shadowMap.enabled = true
    renderer.shadowMap.type = THREE.PCFSoftShadowMap

    const scene = new THREE.Scene()
    const camera = new THREE.OrthographicCamera(-6, 6, 4, -4, 0.1, 80)
    camera.position.set(7.2, 6.1, 8.2)
    camera.lookAt(0, 0.65, 0)

    const style = getComputedStyle(document.documentElement)
    const accent = new THREE.Color(style.getPropertyValue("--accent").trim() || "#f0b90b")
    const teal = new THREE.Color("#24b8aa")
    const amber = new THREE.Color("#e5a43b")
    const pale = new THREE.Color("#1c2735")

    scene.add(new THREE.HemisphereLight(0xffffff, 0xdbe4ee, 1.65))
    const keyLight = new THREE.DirectionalLight(0xffffff, 2.25)
    keyLight.position.set(-3.5, 8, 5)
    keyLight.castShadow = true
    keyLight.shadow.mapSize.set(1024, 1024)
    keyLight.shadow.camera.left = -7
    keyLight.shadow.camera.right = 7
    keyLight.shadow.camera.top = 7
    keyLight.shadow.camera.bottom = -7
    scene.add(keyLight)
    const rimLight = new THREE.DirectionalLight(accent, 1.15)
    rimLight.position.set(5, 4, -5)
    scene.add(rimLight)

    const world = new THREE.Group()
    scene.add(world)

    const platform = new THREE.Mesh(
      new THREE.CylinderGeometry(4.25, 4.34, 0.11, 80),
      new THREE.MeshStandardMaterial({
        color: 0x101823,
        roughness: 0.32,
        metalness: 0.82,
        transparent: true,
        opacity: 0.96,
      }),
    )
    platform.position.y = -0.12
    platform.receiveShadow = true
    world.add(platform)

    const ground = new THREE.Mesh(
      new THREE.CircleGeometry(4.23, 96),
      new THREE.MeshStandardMaterial({
        color: 0x131e2b,
        roughness: 0.5,
        metalness: 0.58,
        transparent: true,
        opacity: 0.98,
      }),
    )
    ground.rotation.x = -Math.PI / 2
    ground.position.y = -0.058
    ground.receiveShadow = true
    world.add(ground)

    const grid = new THREE.GridHelper(6.0, 24, 0x2f8f8b, 0x314152)
    grid.position.y = -0.043
    grid.material.transparent = true
    grid.material.opacity = 0.32
    world.add(grid)

    for (const radius of [2.9, 4.06]) {
      const ring = new THREE.Mesh(
        new THREE.RingGeometry(radius, radius + 0.014, 128),
        new THREE.MeshBasicMaterial({
          color: radius > 4 ? teal : accent,
          transparent: true,
          opacity: radius > 4 ? 0.5 : 0.32,
          side: THREE.DoubleSide,
          depthWrite: false,
        }),
      )
      ring.rotation.x = -Math.PI / 2
      ring.position.y = -0.028
      world.add(ring)
    }

    const circuits = new THREE.Group()
    for (let index = 0; index < 18; index += 1) {
      const angle = (index / 18) * Math.PI * 2
      const inner = 1.05 + (index % 3) * 0.18
      const middle = 2.3 + (index % 4) * 0.18
      const outer = 3.82 - (index % 2) * 0.25
      const bend = angle + (index % 2 ? 0.09 : -0.09)
      const points = [
        new THREE.Vector3(Math.cos(angle) * inner, -0.022, Math.sin(angle) * inner),
        new THREE.Vector3(Math.cos(angle) * middle, -0.022, Math.sin(angle) * middle),
        new THREE.Vector3(Math.cos(bend) * middle, -0.022, Math.sin(bend) * middle),
        new THREE.Vector3(Math.cos(bend) * outer, -0.022, Math.sin(bend) * outer),
      ]
      circuits.add(new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(points),
        new THREE.LineBasicMaterial({
          color: index % 3 === 0 ? teal : accent,
          transparent: true,
          opacity: 0.24,
          depthWrite: false,
        }),
      ))
    }
    world.add(circuits)

    const scan = new THREE.Mesh(
      new THREE.CircleGeometry(4.02, 48, 0, Math.PI / 7),
      new THREE.MeshBasicMaterial({
        color: teal,
        transparent: true,
        opacity: 0.075,
        side: THREE.DoubleSide,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }),
    )
    scan.rotation.x = -Math.PI / 2
    scan.position.y = -0.016
    world.add(scan)

    const points = NODE_DEFINITIONS.map((node) => new THREE.Vector3(...node.position))
    const curve = new THREE.CatmullRomCurve3(points, true, "catmullrom", 0.32)
    const track = new THREE.Mesh(
      new THREE.TubeGeometry(curve, 360, 0.014, 6, true),
      new THREE.MeshBasicMaterial({
        color: accent,
        transparent: true,
        opacity: 0.58,
        depthWrite: false,
      }),
    )
    track.renderOrder = 2
    world.add(track)

    const trackLine = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(curve.getPoints(360)),
      new THREE.LineDashedMaterial({
        color: teal,
        transparent: true,
        opacity: 0.5,
        dashSize: 0.08,
        gapSize: 0.12,
        depthWrite: false,
      }),
    )
    trackLine.computeLineDistances()
    trackLine.renderOrder = 3
    world.add(trackLine)

    const nodes = new Map()
    const neutralMaterial = new THREE.MeshStandardMaterial({
      color: pale,
      roughness: 0.38,
      metalness: 0.46,
    })

    for (const definition of NODE_DEFINITIONS) {
      const [x, y, z] = definition.position
      const nodeColor = definition.id === "risk"
        ? amber
        : definition.id === "execution" || definition.id === "feedback" ? teal : accent
      const group = new THREE.Group()
      group.position.set(x, y, z)
      world.add(group)

      const base = new THREE.Mesh(
        new THREE.CylinderGeometry(0.43, 0.52, 0.09, 6),
        neutralMaterial.clone(),
      )
      base.position.y = -y + 0.06
      base.castShadow = true
      base.receiveShadow = true
      group.add(base)

      const baseRing = new THREE.Mesh(
        new THREE.TorusGeometry(0.43, 0.014, 6, 48),
        new THREE.MeshBasicMaterial({ color: nodeColor, transparent: true, opacity: 0.72 }),
      )
      baseRing.rotation.x = Math.PI / 2
      baseRing.position.y = -y + 0.13
      group.add(baseRing)

      const stemHeight = Math.max(0.12, y - 0.38)
      const stem = new THREE.Mesh(
        new THREE.CylinderGeometry(0.012, 0.025, stemHeight, 12),
        new THREE.MeshBasicMaterial({
          color: nodeColor,
          transparent: true,
          opacity: 0.38,
          blending: THREE.AdditiveBlending,
        }),
      )
      stem.position.y = 0.16 + stemHeight / 2 - y
      group.add(stem)

      const visual = new THREE.Group()
      group.add(visual)
      const glowMaterials = []
      const makeGlowMaterial = (options = {}) => {
        const material = new THREE.MeshStandardMaterial({
          color: options.color || 0x263445,
          roughness: options.roughness ?? 0.25,
          metalness: options.metalness ?? 0.5,
          transparent: options.transparent || false,
          opacity: options.opacity ?? 1,
          emissive: nodeColor,
          emissiveIntensity: options.emissiveIntensity ?? 0.1,
          wireframe: options.wireframe || false,
        })
        glowMaterials.push(material)
        return material
      }

      let spinner = null
      let core = null
      let coreWire = null
      const layers = []
      let satellite = null
      let computeCloud = null
      const rings = []

      if (definition.id === "intake") {
        spinner = new THREE.Group()
        const receiver = new THREE.Mesh(
          new THREE.TorusGeometry(0.4, 0.052, 12, 56),
          makeGlowMaterial({ color: nodeColor, emissiveIntensity: 0.18 }),
        )
        receiver.rotation.x = Math.PI / 2.35
        spinner.add(receiver)
        const bowl = new THREE.Mesh(
          new THREE.CylinderGeometry(0.23, 0.34, 0.22, 32, 1, true),
          makeGlowMaterial({ transparent: true, opacity: 0.88 }),
        )
        bowl.position.y = -0.08
        spinner.add(bowl)
        const center = new THREE.Mesh(
          new THREE.SphereGeometry(0.095, 20, 16),
          makeGlowMaterial({ color: nodeColor, emissiveIntensity: 0.65 }),
        )
        spinner.add(center)
        visual.add(spinner)
      }

      if (definition.id === "features") {
        for (let index = 0; index < 3; index += 1) {
          const layer = new THREE.Group()
          const box = new THREE.Mesh(
            new THREE.BoxGeometry(0.58, 0.1, 0.58),
            makeGlowMaterial({ transparent: true, opacity: 0.9, emissiveIntensity: 0.12 }),
          )
          box.castShadow = true
          const edges = new THREE.LineSegments(
            new THREE.EdgesGeometry(box.geometry),
            new THREE.LineBasicMaterial({ color: nodeColor, transparent: true, opacity: 0.52 }),
          )
          layer.add(box, edges)
          layer.position.y = (index - 1) * 0.23
          layer.rotation.y = Math.PI / 4 + index * 0.12
          visual.add(layer)
          layers.push(layer)
        }
        core = new THREE.Mesh(
          new THREE.OctahedronGeometry(0.18, 0),
          makeGlowMaterial({ color: nodeColor, wireframe: true, emissiveIntensity: 0.45 }),
        )
        visual.add(core)
      }

      if (definition.id === "decision") {
        core = new THREE.Mesh(
          new THREE.IcosahedronGeometry(0.43, 2),
          makeGlowMaterial({ roughness: 0.14, metalness: 0.28, emissiveIntensity: 0.2 }),
        )
        core.castShadow = true
        visual.add(core)
        coreWire = new THREE.Mesh(
          new THREE.IcosahedronGeometry(0.48, 1),
          new THREE.MeshBasicMaterial({
            color: nodeColor,
            wireframe: true,
            transparent: true,
            opacity: 0.38,
          }),
        )
        visual.add(coreWire)
        for (let index = 0; index < 3; index += 1) {
          const pivot = new THREE.Group()
          const ring = new THREE.Mesh(
            new THREE.TorusGeometry(0.67 + index * 0.055, 0.017, 8, 96),
            new THREE.MeshBasicMaterial({
              color: index === 1 ? teal : nodeColor,
              transparent: true,
              opacity: 0.62 - index * 0.1,
            }),
          )
          ring.rotation.set(index * 0.82 + 0.25, index * 0.65, index * 0.38)
          pivot.add(ring)
          visual.add(pivot)
          rings.push(pivot)
        }
        for (let index = 0; index < 6; index += 1) {
          const angle = (index / 6) * Math.PI * 2
          const port = new THREE.Mesh(
            new THREE.SphereGeometry(0.035, 12, 8),
            makeGlowMaterial({ color: nodeColor, emissiveIntensity: 0.48 }),
          )
          port.position.set(Math.cos(angle) * 0.67, Math.sin(angle) * 0.67, 0)
          rings[0].add(port)
        }
        const cloudPositions = new Float32Array(42 * 3)
        for (let index = 0; index < 42; index += 1) {
          const angle = index * 2.399963
          const radius = 0.55 + ((index * 17) % 23) / 100
          const vertical = -0.48 + ((index * 13) % 97) / 100
          cloudPositions[index * 3] = Math.cos(angle) * radius
          cloudPositions[index * 3 + 1] = vertical
          cloudPositions[index * 3 + 2] = Math.sin(angle) * radius
        }
        const cloudGeometry = new THREE.BufferGeometry()
        cloudGeometry.setAttribute("position", new THREE.BufferAttribute(cloudPositions, 3))
        computeCloud = new THREE.Points(
          cloudGeometry,
          new THREE.PointsMaterial({
            color: nodeColor,
            size: 0.026,
            transparent: true,
            opacity: 0.78,
            depthWrite: false,
            blending: THREE.AdditiveBlending,
          }),
        )
        visual.add(computeCloud)
        const coreLight = new THREE.PointLight(nodeColor, 1.4, 3.5, 2)
        visual.add(coreLight)
      }

      if (definition.id === "risk") {
        const shield = new THREE.Mesh(
          new THREE.CylinderGeometry(0.4, 0.4, 0.18, 6),
          makeGlowMaterial({ emissiveIntensity: 0.14 }),
        )
        shield.rotation.y = Math.PI / 6
        shield.castShadow = true
        visual.add(shield)
        const ring = new THREE.Mesh(
          new THREE.TorusGeometry(0.47, 0.025, 8, 56),
          makeGlowMaterial({ color: nodeColor, emissiveIntensity: 0.3 }),
        )
        ring.rotation.x = Math.PI / 2
        visual.add(ring)
        core = new THREE.Mesh(
          new THREE.OctahedronGeometry(0.19, 0),
          makeGlowMaterial({ color: nodeColor, emissiveIntensity: 0.36 }),
        )
        core.position.y = 0.18
        visual.add(core)
        spinner = ring
      }

      if (definition.id === "execution") {
        core = new THREE.Mesh(
          new THREE.ConeGeometry(0.3, 0.55, 4),
          makeGlowMaterial({ color: nodeColor, emissiveIntensity: 0.32 }),
        )
        core.rotation.y = Math.PI / 4
        core.castShadow = true
        visual.add(core)
        for (let index = 0; index < 3; index += 1) {
          const packet = new THREE.Mesh(
            new THREE.BoxGeometry(0.16, 0.1, 0.16),
            makeGlowMaterial({ transparent: true, opacity: 0.82 - index * 0.16 }),
          )
          packet.position.set(0, -0.12, 0.42 + index * 0.2)
          visual.add(packet)
          layers.push(packet)
        }
        const ring = new THREE.Mesh(
          new THREE.TorusGeometry(0.45, 0.018, 8, 56),
          new THREE.MeshBasicMaterial({ color: nodeColor, transparent: true, opacity: 0.46 }),
        )
        ring.rotation.x = Math.PI / 2
        ring.position.y = -0.28
        visual.add(ring)
      }

      if (definition.id === "feedback") {
        spinner = new THREE.Group()
        const ring = new THREE.Mesh(
          new THREE.TorusGeometry(0.42, 0.04, 10, 64),
          makeGlowMaterial({ color: nodeColor, emissiveIntensity: 0.24 }),
        )
        ring.rotation.x = Math.PI / 2
        spinner.add(ring)
        const inner = new THREE.Mesh(
          new THREE.TorusGeometry(0.25, 0.012, 6, 48),
          new THREE.MeshBasicMaterial({ color: nodeColor, transparent: true, opacity: 0.42 }),
        )
        inner.rotation.x = Math.PI / 2
        spinner.add(inner)
        satellite = new THREE.Mesh(
          new THREE.SphereGeometry(0.075, 16, 12),
          makeGlowMaterial({ color: nodeColor, emissiveIntensity: 0.62 }),
        )
        satellite.position.x = 0.42
        spinner.add(satellite)
        visual.add(spinner)
      }

      nodes.set(definition.id, {
        definition,
        group,
        visual,
        glowMaterials,
        spinner,
        core,
        coreWire,
        layers,
        satellite,
        computeCloud,
        rings,
        color: nodeColor,
      })
    }

    const glowTexture = makeGlowTexture()
    const packetGeometry = new THREE.SphereGeometry(0.052, 12, 10)
    const trailGeometry = new THREE.SphereGeometry(0.03, 10, 8)
    const makePacket = (color, injected = false) => {
      const material = new THREE.MeshBasicMaterial({ color })
      const head = new THREE.Mesh(packetGeometry, material)
      const glow = new THREE.Sprite(new THREE.SpriteMaterial({
        map: glowTexture,
        color,
        transparent: true,
        opacity: injected ? 0.85 : 0.52,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }))
      glow.scale.setScalar(injected ? 0.46 : 0.32)
      head.add(glow)
      world.add(head)
      const trails = []
      for (let index = 0; index < 5; index += 1) {
        const trail = new THREE.Mesh(
          trailGeometry,
          new THREE.MeshBasicMaterial({
            color,
            transparent: true,
            opacity: 0.32 * (1 - index / 5),
            depthWrite: false,
          }),
        )
        trail.scale.setScalar(1 - index * 0.12)
        world.add(trail)
        trails.push(trail)
      }
      return { head, trails, injected }
    }

    const packets = Array.from({ length: 10 }, (_, index) => ({
      ...makePacket(index % 3 === 2 ? teal : accent),
      offset: index / 10,
      speed: 0.052,
    }))
    const injections = []
    const projected = new THREE.Vector3()
    const scaleTarget = new THREE.Vector3()
    const fitPoint = new THREE.Vector3()
    const fitPoints = []
    for (let index = 0; index < 64; index += 1) {
      const angle = (index / 64) * Math.PI * 2
      for (const y of [-0.18, -0.03]) {
        fitPoints.push(new THREE.Vector3(Math.cos(angle) * 4.36, y, Math.sin(angle) * 4.36))
      }
    }
    for (const definition of NODE_DEFINITIONS) {
      const [x, y, z] = definition.position
      const radius = definition.id === "decision" ? 0.88 : 0.64
      for (const dx of [-radius, radius]) {
        for (const dy of [-0.68, 0.88]) {
          for (const dz of [-radius, radius]) {
            fitPoints.push(new THREE.Vector3(x + dx, y + dy, z + dz))
          }
        }
      }
    }
    let width = 1
    let height = 1
    let frame = 0

    const resize = () => {
      width = Math.max(1, host.clientWidth)
      height = Math.max(1, host.clientHeight)
      const aspect = width / height
      camera.updateMatrixWorld(true)
      let minX = Infinity
      let maxX = -Infinity
      let minY = Infinity
      let maxY = -Infinity
      for (const point of fitPoints) {
        fitPoint.copy(point).applyMatrix4(camera.matrixWorldInverse)
        minX = Math.min(minX, fitPoint.x)
        maxX = Math.max(maxX, fitPoint.x)
        minY = Math.min(minY, fitPoint.y)
        maxY = Math.max(maxY, fitPoint.y)
      }
      const crowded = width < 480 && height >= 300
      const offsetX = crowded ? -0.45 : 0
      const offsetY = crowded ? 0.55 : 0
      const halfWidth = (maxX - minX) / 2 + 0.26 + Math.abs(offsetX)
      const halfHeight = (maxY - minY) / 2 + 0.26 + Math.abs(offsetY)
      const viewHeight = Math.max(halfHeight * 2, (halfWidth * 2) / aspect)
      const centerX = (minX + maxX) / 2 + offsetX
      const centerY = (minY + maxY) / 2 + offsetY
      camera.left = centerX - (viewHeight * aspect) / 2
      camera.right = centerX + (viewHeight * aspect) / 2
      camera.top = centerY + viewHeight / 2
      camera.bottom = centerY - viewHeight / 2
      camera.updateProjectionMatrix()
      renderer.setSize(width, height, false)
    }
    const observer = new ResizeObserver(resize)
    observer.observe(host)
    resize()

    const removePacket = (packet) => {
      world.remove(packet.head)
      packet.head.material.dispose()
      packet.head.children.forEach((child) => child.material?.dispose())
      for (const trail of packet.trails) {
        world.remove(trail)
        trail.material.dispose()
      }
    }

    const animate = (now) => {
      const time = reduced || enabledRef.current === false ? 0 : now / 1000
      const motionTime = time
      scan.rotation.z = -motionTime * 0.22
      circuits.children.forEach((line, index) => {
        line.material.opacity = 0.16 + (Math.sin(motionTime * 1.4 + index * 0.7) + 1) * 0.07
      })

      for (const [id, node] of nodes) {
        const active = Boolean(activeRef.current?.[id])
        const targetScale = active ? 1.065 + Math.sin(motionTime * 4) * 0.018 : 1
        node.visual.scale.lerp(scaleTarget.setScalar(targetScale), 0.1)
        for (const material of node.glowMaterials) {
          const targetIntensity = active ? 0.42 : 0.1
          material.emissiveIntensity += (targetIntensity - material.emissiveIntensity) * 0.08
        }
        if (id === "intake" && node.spinner) {
          node.spinner.rotation.y = motionTime * 0.42
          node.spinner.rotation.z = Math.sin(motionTime * 0.7) * 0.12
        }
        if (id === "features") {
          node.layers.forEach((layer, index) => {
            layer.position.y = (index - 1) * 0.23 + Math.sin(motionTime * 1.8 + index) * 0.035
            layer.rotation.y = Math.PI / 4 + index * 0.12 + motionTime * (index % 2 ? -0.16 : 0.13)
          })
          if (node.core) node.core.rotation.y = motionTime * 0.8
        }
        if (id === "decision") {
          if (node.core) {
            node.core.rotation.x = motionTime * 0.18
            node.core.rotation.y = motionTime * 0.32
          }
          if (node.coreWire) {
            node.coreWire.rotation.x = -motionTime * 0.16
            node.coreWire.rotation.y = motionTime * 0.24
          }
          node.rings.forEach((ring, index) => {
            ring.rotation.y = motionTime * (0.18 + index * 0.08) * (index % 2 ? -1 : 1)
            ring.rotation.z = motionTime * 0.08 * (index + 1)
          })
          if (node.computeCloud) {
            node.computeCloud.rotation.y = -motionTime * 0.3
            node.computeCloud.rotation.x = motionTime * 0.08
          }
          if (signalRef.current && node.core) {
            node.core.material.emissiveIntensity = 0.42 + Math.sin(motionTime * 7) * 0.12
          }
        }
        if (id === "risk") {
          if (node.spinner) node.spinner.rotation.z = motionTime * 0.35
          if (node.core) node.core.rotation.y = motionTime * 0.7
        }
        if (id === "execution") {
          node.layers.forEach((packet, index) => {
            packet.position.z = 0.35 + ((motionTime * 0.32 + index * 0.22) % 0.72)
            packet.material.opacity = 0.85 - ((motionTime * 0.32 + index * 0.22) % 0.72) * 0.65
          })
          if (node.core) node.core.position.y = Math.sin(motionTime * 2.2) * 0.055
        }
        if (id === "feedback" && node.spinner) {
          node.spinner.rotation.y = motionTime * 0.55
          node.spinner.rotation.z = Math.sin(motionTime * 0.5) * 0.18
        }
      }

      for (const packet of packets) {
        const t = (packet.offset + motionTime * packet.speed) % 1
        curve.getPointAt(t, packet.head.position)
        packet.trails.forEach((trail, index) => {
          const trailT = (t - (index + 1) * 0.0035 + 1) % 1
          curve.getPointAt(trailT, trail.position)
        })
      }

      for (const [id, schedule] of pulseScheduleRef.current) {
        if (!schedule.injected && now >= schedule.at && !reduced && enabledRef.current !== false) {
          schedule.injected = true
          const intake = nodes.get("intake")
          intake.visual.scale.setScalar(1.16)
          const color = new THREE.Color(schedule.color)
          injections.push({
            ...makePacket(color, true),
            start: motionTime,
            speed: 0.12,
          })
        }
        if (schedule.injected && now >= schedule.at + 11000) {
          pulseScheduleRef.current.delete(id)
        }
      }

      for (let index = injections.length - 1; index >= 0; index -= 1) {
        const packet = injections[index]
        const progress = (motionTime - packet.start) * packet.speed
        if (progress >= 1 || progress < 0) {
          if (progress >= 1) {
            removePacket(packet)
            injections.splice(index, 1)
          }
          continue
        }
        curve.getPointAt(progress, packet.head.position)
        packet.trails.forEach((trail, trailIndex) => {
          const trailT = (progress - (trailIndex + 1) * 0.005 + 1) % 1
          curve.getPointAt(trailT, trail.position)
        })
      }

      const intake = nodes.get("intake")
      intake.group.getWorldPosition(projected)
      projected.project(camera)
      target.style.left = `${(projected.x * 0.5 + 0.5) * width}px`
      target.style.top = `${(-projected.y * 0.5 + 0.5) * height}px`
      renderer.render(scene, camera)
      frame = requestAnimationFrame(animate)
    }
    frame = requestAnimationFrame(animate)

    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
      pulseScheduleRef.current.clear()
      const geometries = new Set()
      const materials = new Set()
      const textures = new Set()
      scene.traverse((object) => {
        if (object.geometry) geometries.add(object.geometry)
        if (object.material) {
          const list = Array.isArray(object.material) ? object.material : [object.material]
          for (const material of list) {
            materials.add(material)
            if (material.map) textures.add(material.map)
          }
        }
      })
      geometries.forEach((geometry) => geometry.dispose())
      materials.forEach((material) => material.dispose())
      textures.forEach((texture) => texture.dispose())
      glowTexture.dispose()
      renderer.dispose()
    }
  }, [reduced])

  return (
    <div ref={hostRef} className={styles.scene} aria-hidden={failed || undefined}>
      <canvas ref={canvasRef} className={styles.sceneCanvas} aria-label="Three.js 自动量化交易三维闭环场景" />
      {failed && <span className={styles.sceneFallback}>当前浏览器无法初始化 3D 场景</span>}
      <span
        ref={targetRef}
        className={styles.intakeTarget}
        data-metric-intake=""
        data-active={metricPulses.length ? "y" : undefined}
        aria-label="3D 实时指标飞线接收入口"
      >
        {metricPulses.slice(-10).map((pulse) => (
          <i
            key={pulse.id}
            className={styles.intakeImpact}
            style={{
              "--impact-color": pulse.color,
              animationDelay: `${arrivalDelay(pulse)}ms`,
            }}
          />
        ))}
      </span>
    </div>
  )
}
