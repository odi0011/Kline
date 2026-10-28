import { useMemo, useState } from "react"
import styles from "./account-card.module.css"

const DAY = 86400000
const iso = (ms) => new Date(ms).toISOString().slice(0, 10)
const money = (n) => n.toLocaleString("zh-CN", { maximumFractionDigits: 4 })
const tint = (level) => `hsl(var(--h) 58% ${94 - level * 48}%)`

// 输入为 storage.equity_heatmap 的真实日记录；不使用短期 series、不前向填充。
function dailyRecords(history, today) {
  const records = new Map()
  for (const row of Array.isArray(history) ? history : []) {
    if (!row || !/^\d{4}-\d{2}-\d{2}$/.test(row.date) ||
        typeof row.close !== "number" || !Number.isFinite(row.close)) continue
    const ms = Date.parse(`${row.date}T00:00:00Z`)
    if (!Number.isFinite(ms) || iso(ms) !== row.date || row.date > today) continue
    const prior = records.get(row.date)
    if (!prior || (row.lastTs ?? 0) >= (prior.lastTs ?? 0)) records.set(row.date, row)
  }
  return new Map([...records].sort(([a], [b]) => a.localeCompare(b)))
}

export default function EquityHeatmap({ history }) {
  const today = iso(Date.now())
  const currentYear = Number(today.slice(0, 4))
  const records = useMemo(() => dailyRecords(history, today), [history, today])
  const first = records.keys().next().value
  const [selectedYear, setSelectedYear] = useState(null)
  const [hovered, setHovered] = useState(null)
  const [focused, setFocused] = useState(null)
  const firstYear = first ? Number(first.slice(0, 4)) : currentYear
  const year = selectedYear >= firstYear && selectedYear <= currentYear ? selectedYear : currentYear
  const years = Array.from({ length: currentYear - firstYear + 1 }, (_, i) => currentYear - i)
  const values = [...records.values()].map((r) => r.close)
  // 所有年份共用绝对权益刻度；零值/负值有效，单值使用中间色阶。
  const min = values.length ? Math.min(...values) : 0
  const max = values.length ? Math.max(...values) : 0
  const level = (n) => max === min ? 0.55 : (n - min) / (max - min)
  const start = first && first > `${year}-01-01` ? first : `${year}-01-01`
  const end = year === currentYear ? today : `${year}-12-31`
  const startMs = Date.parse(`${start}T00:00:00Z`)
  const count = Math.max(0, Math.round((Date.parse(`${end}T00:00:00Z`) - startMs) / DAY) + 1)
  const offset = (new Date(startMs).getUTCDay() + 6) % 7
  const days = Array.from({ length: count }, (_, i) => iso(startMs + i * DAY))
  const weeks = Math.ceil((offset + count) / 7)
  const months = new Map()
  days.forEach((date, i) => {
    if (i === 0 || date.endsWith("-01")) {
      months.set(Math.floor((offset + i) / 7) + 1, Number(date.slice(5, 7)))
    }
  })
  const describe = (date) => {
    const row = records.get(date)
    return `${date} · ${row ? `${date === today ? "最新" : "日末"}总权益 ${money(row.close)} USDT` : "无数据"}`
  }
  const active = hovered || focused

  const moveFocus = (event, index) => {
    const delta = { ArrowLeft: -7, ArrowRight: 7, ArrowUp: -1, ArrowDown: 1 }[event.key]
    if (delta == null && event.key !== "Home" && event.key !== "End") return
    event.preventDefault()
    const next = event.key === "Home" ? 0 : event.key === "End" ? count - 1
      : Math.max(0, Math.min(count - 1, index + delta))
    const target = event.currentTarget.parentElement.querySelector(`[data-date="${days[next]}"]`)
    target?.focus()
    target?.scrollIntoView({ block: "nearest", inline: "nearest" })
  }

  return (
    <div className={styles.heatmap} role="group" aria-label="UTC 每日总权益热力图">
      <div className={styles.heatHead}>
        <b title="颜色表示日末总权益的绝对值，不代表收益；今日为最新采样">日末总权益</b>
        <span className={styles.heatMissing}><i />无数据</span>
        <select aria-label="权益年份" value={year} onChange={(e) => {
          setSelectedYear(Number(e.target.value)); setHovered(null); setFocused(null)
        }}>
          {years.map((y) => <option value={y} key={y}>{y} 年</option>)}
        </select>
      </div>
      <div className={styles.heatSince} title="首次记录是现存档案的起点，不一定是账户接入日；已删除历史无法恢复，缺失日不补值。">
        {first ? `首次记录 ${first} · UTC` : "暂无每日权益记录 · UTC"}
      </div>
      {first ? (
        <div className={styles.heatScroll}>
          <div className={styles.heatCalendar} style={{ "--weeks": weeks }}>
            <div className={styles.heatMonths} aria-hidden="true">
              {[...months].map(([column, month]) => (
                <span key={column} style={{ gridColumn: column }}>{month}月</span>
              ))}
            </div>
            <div className={styles.heatDays}>
              {Array.from({ length: offset }, (_, i) => <span key={`pad${i}`} aria-hidden="true" />)}
              {days.map((date, i) => {
                const row = records.get(date)
                return (
                  <button key={date} type="button" data-date={date} data-missing={!row || undefined}
                    className={styles.heatDay} aria-label={describe(date)} title={describe(date)}
                    style={row ? { backgroundColor: tint(level(row.close)) } : undefined}
                    onMouseEnter={() => setHovered(date)} onMouseLeave={() => setHovered(null)}
                    onFocus={() => setFocused(date)} onBlur={() => setFocused(null)}
                    onKeyDown={(e) => moveFocus(e, i)} />
                )
              })}
            </div>
          </div>
        </div>
      ) : <div className={styles.heatWaiting}>等待真实采样，不补造历史</div>}
      <div className={styles.heatDetail} aria-live="polite" aria-atomic="true" title={active ? describe(active) : "全历史统一色阶：绝对权益，非收益；单位 USDT"}>
        {active ? describe(active) : first ? <>
          <span>{money(min)}</span>
          <span className={styles.heatScale} aria-hidden="true">
            {[0, 0.25, 0.5, 0.75, 1].map((v) => <i key={v} style={{ backgroundColor: tint(v) }} />)}
          </span>
          <span>{money(max)} USDT · 绝对值</span>
        </> : "总权益 · USDT"}
      </div>
    </div>
  )
}
