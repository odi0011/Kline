import { useEffect, useState } from "react"

/* 币种头像：依次尝试 public/icons/{base}.svg → {base}.png → 确定性色相字母圆牌。
   色相由币种名逐字符编码，稳定不变（仅当两种格式的 icon 文件都不存在时才会走到）。 */
export default function CoinAvatar({ symbol, size = 22, className = "" }) {
  const base = symbol.replace(/usdt$/i, "").toUpperCase()
  const [stage, setStage] = useState(0)   // 0=svg 1=png 2=字母兜底
  useEffect(() => { setStage(0) }, [symbol])
  if (stage === 0) {
    return (
      <img className={className} src={`icons/${base}.svg`} alt={base} width={size} height={size}
        onError={() => setStage(1)} />
    )
  }
  if (stage === 1) {
    return (
      <img className={className} src={`icons/${base}.png`} alt={base} width={size} height={size}
        onError={() => setStage(2)} />
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
