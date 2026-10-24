/* 组合图表卡（柱 + 线双轴）——按 BoardUI ComboChartCard 的 API 自实现（官方为 Pro 组件）。
   API 对齐：title / data / bar{key,label,format} / line{key,label,format} / delta / ranges / tiles */
import { useState } from "react"

const ACCENT = "#f0b90b"

export function ComboChartCard({
  title, data, bar, line, delta, ranges, tiles, height = 160, className = "",
}) {
  const [rangeId, setRangeId] = useState(ranges?.[0]?.id)
  const active = ranges?.find((r) => r.id === rangeId)
  const d = active?.data || data || []
  const dd = active?.delta ?? delta

  const W = 340, H = height, padL = 10, padR = 40, padT = 14, padB = 24
  const n = d.length
  const bw = n ? (W - padL - padR) / n : 0
  const bmax = Math.max(...d.map((x) => Math.abs(x[bar.key])), 1e-9)
  const lvals = d.map((x) => x[line.key])
  const lmin = Math.min(...lvals, 0)
  const lmax = Math.max(...lvals, 1e-9)
  const plotH = H - padT - padB
  const yBar = (v) => H - padB - (Math.abs(v) / bmax) * plotH
  const yLine = (v) => H - padB - ((v - lmin) / (lmax - lmin || 1)) * plotH
  const cx = (i) => padL + bw * i + bw / 2
  const linePts = d.map((x, i) => `${cx(i)},${yLine(x[line.key])}`).join(" ")
  const barTotal = d.reduce((s, x) => s + x[bar.key], 0)
  const lineAvg = n ? d.reduce((s, x) => s + x[line.key], 0) / n : 0

  return (
    <div className={"combo-card" + (className ? " " + className : "")}>
      <div className="combo-head">
        <div>
          <div className="combo-title">{title}</div>
          {dd != null && (
            <span className={`combo-delta ${dd >= 0 ? "up" : "down"}`}>
              {dd >= 0 ? "+" : ""}{(dd * 100).toFixed(1)}%
            </span>
          )}
        </div>
        {ranges && (
          <div className="combo-ranges">
            {ranges.map((r) => (
              <button key={r.id} className={r.id === rangeId ? "on" : ""} onClick={() => setRangeId(r.id)}>
                {r.label}
              </button>
            ))}
          </div>
        )}
      </div>

      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}>
        <line x1={padL} x2={W - padR} y1={H - padB} y2={H - padB} stroke="#e5e9f0" />
        {d.map((x, i) => {
          const v = Math.abs(x[bar.key])
          const top = yBar(v)
          return (
            <g key={i}>
              <rect x={cx(i) - bw * 0.28} y={top} width={bw * 0.56}
                height={Math.max(1.5, H - padB - top)} fill={ACCENT} opacity="0.75" rx="2" />
              {x.label && (
                <text x={cx(i)} y={H - 8} textAnchor="middle" fontSize="9" fill="#8b949e">{x.label}</text>
              )}
            </g>
          )
        })}
        <polyline points={linePts} fill="none" stroke="#5b7cfa" strokeWidth="2" strokeLinejoin="round" />
        {d.map((x, i) => (
          <circle key={`p${i}`} cx={cx(i)} cy={yLine(x[line.key])} r="2.6" fill="#5b7cfa" />
        ))}
        <text x={W - padR + 4} y={padT + 4} fontSize="9" fill="#8b949e">
          {line.format ? line.format(lmax) : lmax.toFixed(1)}
        </text>
        <text x={W - padR + 4} y={H - padB} fontSize="9" fill="#8b949e">
          {line.format ? line.format(lmin) : lmin.toFixed(1)}
        </text>
      </svg>

      {tiles && (
        <div className="combo-tiles">
          <div className="combo-tile">
            <span className="t-label">{bar.label}合计</span>
            <b>{bar.format ? bar.format(barTotal) : barTotal}</b>
          </div>
          <div className="combo-tile">
            <span className="t-label">{line.label}均值</span>
            <b>{line.format ? line.format(lineAvg) : lineAvg.toFixed(2)}</b>
          </div>
        </div>
      )}
    </div>
  )
}
