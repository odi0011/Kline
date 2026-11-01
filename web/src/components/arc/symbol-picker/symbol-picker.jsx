/* 快捷选标（右上板块下半）：直接勾选要看的币，不用翻图表设置菜单。
   与「选择币种」菜单同一份池和同一套最少/最多限制，勾选即生效。
   池有几百个币，列表按固定行高做窗口化渲染，避免一次挂满几百个节点。 */
import { useLayoutEffect, useMemo, useRef, useState } from "react"
import { Search, Check } from "lucide-react"
import CoinAvatar from "@/components/ui/coin-avatar"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import styles from "./symbol-picker.module.css"

const ROW_H = 22
const OVERSCAN = 6

const fmtPrice = (v) => {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return "--"
  if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 1 })
  if (n >= 1) return n.toFixed(3)
  return n.toPrecision(4)
}

export default function SymbolPicker({ pool = [], selected = [], maxSymbols = 24, minSymbols = 4,
  metrics = {}, chartColors, onToggle }) {
  const [query, setQuery] = useState("")
  const [scrollTop, setScrollTop] = useState(0)
  const [viewH, setViewH] = useState(0)
  const viewportRef = useRef(null)
  const up = chartColors?.up || "#16a34a"
  const down = chartColors?.down || "#dc2626"

  // 已选置顶；其余按成交量从高到低，方便一眼看到活跃标的
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    const list = (pool || []).filter((s) => !q || s.includes(q))
    const rank = (s) => (selected.includes(s) ? 1 : 0)
    const vol = (s) => Number(metrics?.[s]?.quoteVolume) || 0
    return list.slice().sort((a, b) => rank(b) - rank(a) || vol(b) - vol(a))
  }, [pool, query, selected, metrics])

  useLayoutEffect(() => {
    const el = viewportRef.current
    if (!el) return
    const measure = () => setViewH(el.clientHeight)
    measure()
    if (typeof ResizeObserver === "undefined") return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // 搜索词变化后回到顶部，否则会停在一个超出新列表长度的位置
  const onQuery = (value) => {
    setQuery(value)
    setScrollTop(0)
    viewportRef.current?.scrollTo({ top: 0 })
  }

  const total = rows.length
  const start = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN)
  const end = Math.min(total, Math.ceil((scrollTop + Math.max(viewH, ROW_H * 6)) / ROW_H) + OVERSCAN)
  const atMax = selected.length >= maxSymbols

  return (
    <section className={styles.root} aria-label="快捷选标">
      <label className={styles.search}>
        <Search size={11} aria-hidden />
        <input value={query} onChange={(e) => onQuery(e.target.value)}
          placeholder="搜索币种" aria-label="搜索币种" spellCheck={false} autoComplete="off" />
      </label>

      <ScrollArea orientation="vertical" fade={14} scrollbars="auto" label="快捷选标"
        viewportRef={viewportRef} className={styles.scroll} viewportClassName={styles.viewport}
        onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}>
        {total === 0 && <p className={styles.empty}>没有匹配的币种</p>}
        <div className={styles.spacer} style={{ height: total * ROW_H }}>
          {rows.slice(start, end).map((sym, i) => {
            const on = selected.includes(sym)
            const disabled = on ? selected.length <= minSymbols : atMax
            const m = metrics?.[sym] || {}
            const change = Number(m.changePct)
            return (
              <div key={sym} className={styles.rowSlot}
                style={{ transform: `translateY(${(start + i) * ROW_H}px)` }}>
                <button type="button" className={styles.row}
                  data-on={on || undefined} disabled={disabled}
                  aria-pressed={on}
                  onClick={() => onToggle?.(sym)}
                  title={disabled
                    ? (on ? `至少保留 ${minSymbols} 个币` : `最多同时看 ${maxSymbols} 个币`)
                    : `${on ? "移出" : "加入"} ${sym.toUpperCase()} · ${fmtPrice(m.price)}`}>
                  <span className={styles.check} aria-hidden>{on && <Check size={9} strokeWidth={3.2} />}</span>
                  <CoinAvatar symbol={sym} size={13} />
                  <b className={styles.sym}>{sym.replace(/usdt$/i, "").toUpperCase()}</b>
                  <span className={styles.chg}
                    style={{ color: change > 0 ? up : change < 0 ? down : undefined }}>
                    {Number.isFinite(change) ? `${change > 0 ? "+" : ""}${change.toFixed(2)}%` : "--"}
                  </span>
                </button>
              </div>
            )
          })}
        </div>
      </ScrollArea>
    </section>
  )
}
