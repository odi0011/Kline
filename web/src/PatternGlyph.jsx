// 迷你蜡烛示意图：按 patterns_meta 的 candles 数据画 2~3 根K线小图。
const UP = "#16a34a"
const DOWN = "#dc2626"

export default function PatternGlyph({ candles }) {
  const W = 120
  const H = 64
  const n = candles.length
  const slot = W / (n + 1)
  const bw = Math.min(14, slot * 0.52)
  const y = (v) => H - 6 - (v / 100) * (H - 12)

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="glyph" role="img" aria-hidden>
      {candles.map(([o, c, h, l], i) => {
        const hi = h ?? Math.max(o, c)
        const lo = l ?? Math.min(o, c)
        const cx = slot * (i + 1)
        const color = c >= o ? UP : DOWN
        const top = y(Math.max(o, c))
        const bot = y(Math.min(o, c))
        return (
          <g key={i}>
            <line x1={cx} x2={cx} y1={y(hi)} y2={y(lo)} stroke={color} strokeWidth="1.6" strokeLinecap="round" />
            <rect x={cx - bw / 2} y={top} width={bw} height={Math.max(1.6, bot - top)}
              fill={color} rx="1" />
          </g>
        )
      })}
    </svg>
  )
}
