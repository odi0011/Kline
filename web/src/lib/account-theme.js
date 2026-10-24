/* 账户主题：由账户名生成确定性色相，并基于渐变引擎的水彩预设衍生配色。
   模板预设取自 gradient-engine 内置的 "Mint Splash"（wc-botanical，水彩 wash），
   把它的色相整体旋转到账户色相，即得到每账户专属的同风格配色。
   哈希用 131 进制 + 997/47 扩散：名字只差最后一个字（如 测试盘1/2/3）也会得到
   相差约 47° 的明显不同色相。 */

export function accountHue(name) {
  let h = 0
  const s = name || ""
  for (let i = 0; i < s.length; i++) h = (h * 131 + s.charCodeAt(i)) % 4294967296
  return ((h % 997) * 47) % 360
}

function hexToHsl(hex) {
  let h = hex.replace("#", "")
  if (h.length === 3) h = h.split("").map((c) => c + c).join("")
  const n = parseInt(h, 16)
  const r = ((n >> 16) & 255) / 255, g = ((n >> 8) & 255) / 255, b = (n & 255) / 255
  const max = Math.max(r, g, b), min = Math.min(r, g, b)
  let hh = 0
  const l = (max + min) / 2
  const d = max - min
  const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1))
  if (d !== 0) {
    if (max === r) hh = 60 * (((g - b) / d) % 6)
    else if (max === g) hh = 60 * ((b - r) / d + 2)
    else hh = 60 * ((r - g) / d + 4)
  }
  if (hh < 0) hh += 360
  return { h: hh, s: s * 100, l: l * 100 }
}

function hslToHex(h, s, l) {
  h = ((h % 360) + 360) % 360
  s = Math.max(0, Math.min(100, s)) / 100
  l = Math.max(0, Math.min(100, l)) / 100
  const c = (1 - Math.abs(2 * l - 1)) * s
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1))
  const m = l - c / 2
  let r = 0, g = 0, b = 0
  if (h < 60) { r = c; g = x }
  else if (h < 120) { r = x; g = c }
  else if (h < 180) { g = c; b = x }
  else if (h < 240) { g = x; b = c }
  else if (h < 300) { r = x; b = c }
  else { r = c; b = x }
  const to = (v) => Math.round((v + m) * 255).toString(16).padStart(2, "0")
  return `#${to(r)}${to(g)}${to(b)}`
}

export function hexHue(hex) {
  return hexToHsl(hex).h
}

export function shiftHue(hex, delta) {
  const { h, s, l } = hexToHsl(hex)
  return hslToHex(h + delta, s, l)
}

/** 模板水彩预设（gradient-engine 的 wc-botanical "Mint Splash"） */
export const TEMPLATE_PRESET = {
  stops: ["#18BDA7", "#41D6B3", "#72EAC1", "#B1F0CC"],
  params: {
    technique: "wash", seed: 419, angle: 274, layers: 6, scale: 58, coverage: 94,
    wetness: 74, spread: 76, pigment: 50, blooms: 32, granulation: 30, edges: 34,
    texture: 20, paperWarmth: 5,
  },
}

/** 把模板预设的整体色相旋转到目标色相，返回衍生后的 stops */
export function deriveStops(hue) {
  const dom = hexHue(TEMPLATE_PRESET.stops[0])
  const delta = hue - dom
  return TEMPLATE_PRESET.stops.map((s) => shiftHue(s, delta))
}

/** 账户卡片的水彩配方（供 gradient-engine paintRecipe 使用） */
export function accountRecipe(name) {
  const hue = accountHue(name)
  return {
    type: "WATERCOLOR",
    stops: deriveStops(hue),
    params: { ...TEMPLATE_PRESET.params, seed: (TEMPLATE_PRESET.params.seed + hue) % 9999 },
    cover: true,
    seed: hue % 9999,
  }
}
