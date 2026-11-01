import { useEffect, useState } from "react"

/* 币种头像四级回退（每级失败才走下一级，全部失败退到确定性色相字母牌）：
   ① public/icons/{base}.svg   本地矢量（项目自带的少数主流币）
   ② public/icons/{base}.png   本地位图（黄金/白银/特斯拉等 TradFi 标的）
   ③ CoinCap 图标 CDN          全市场 U 本位合约的绝大多数币种
   ④ 字母牌                     色相由币种名逐字符编码，稳定不变
   本地优先只针对确实存在的文件：币种菜单一次列几百个币，盲目探测本地
   会产生成百上千个 404。新增本地图标时同步这份清单。 */
const LOCAL = new Set([
  "ada", "bnb", "btc", "doge", "eth", "sol", "xrp",   // *.svg
  "tsla", "xau", "xag",                                // *.png
])
const hasLocal = (base) => LOCAL.has(base.toLowerCase())

/** CoinCap 的 slug 去掉了币安的价格倍率前缀：1000PEPE→pepe、1000000MOG→mog */
const iconSlug = (base) => {
  const lower = String(base || "").toLowerCase()
  const stripped = lower.replace(/^(?:1000000|100000|10000|1000|1m|10m|1b)/, "")
  return stripped || lower
}
const REMOTE = (base) => `https://assets.coincap.io/assets/icons/${iconSlug(base)}@2x.png`

export default function CoinAvatar({ symbol, size = 22, className = "" }) {
  const base = symbol.replace(/usdt$/i, "")          // 文件路径保持小写原样（icons/btc.svg …）
  const display = base.toUpperCase()                  // 仅字母兜底牌用大写
  const [stage, setStage] = useState(() => (hasLocal(base) ? 0 : 2))  // 0=svg 1=png 2=CDN 3=字母兜底
  useEffect(() => { setStage(hasLocal(base) ? 0 : 2) }, [base])
  const imgStyle = { width: size, height: size, borderRadius: "50%", objectFit: "cover", display: "block" }
  const imgProps = {
    className, style: imgStyle, alt: base, width: size, height: size,
    loading: "lazy", decoding: "async", referrerPolicy: "no-referrer",
  }
  if (stage === 0) {
    return <img {...imgProps} src={`icons/${base}.svg`} onError={() => setStage(1)} />
  }
  if (stage === 1) {
    return <img {...imgProps} src={`icons/${base}.png`} onError={() => setStage(2)} />
  }
  if (stage === 2) {
    return <img {...imgProps} src={REMOTE(base)} onError={() => setStage(3)} />
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
      {display.slice(0, 2)}
    </span>
  )
}
