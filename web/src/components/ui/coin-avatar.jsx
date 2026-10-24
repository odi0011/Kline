import { useEffect, useState } from "react"

/* 币种头像：优先用 public/icons/{base}.svg，没有就用确定性色相的字母圆牌兜底
   （TSLA/XAU/XAG 这类新上币种没有现成 icon）。色相由币种名逐字符编码，稳定不变。 */
export default function CoinAvatar({ symbol, size = 22, className = "" }) {
  const base = symbol.replace(/usdt$/i, "").toUpperCase()
  const [ok, setOk] = useState(true)
  useEffect(() => { setOk(true) }, [symbol])
  if (ok) {
    return (
      <img className={className} src={`icons/${base}.svg`} alt={base} width={size} height={size}
        onError={() => setOk(false)} />
    )
  }
  let hue = 0
  for (const ch of base) hue = (hue * 33 + ch.charCodeAt(0)) % 360
  return (
    <span className={className} aria-label={base}
      style={{
        width: size, height: size, borderRadius: "50%", display: "inline-grid", placeItems: "center",
        background: `linear-gradient(135deg, hsl(${hue} 72% 55%), hsl(${(hue + 40) % 360} 72% 44%))`,
        color: "#fff", fontSize: Math.round(size * 0.4), fontWeight: 700, lineHeight: 1,
      }}>
      {base.slice(0, 2)}
    </span>
  )
}
