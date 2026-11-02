/* 计算流水：系统每轮运算的实时轨迹，一条连续的流淌清单。

   不按币种分组——这是"运算流"，不是"每个币的摘要"。
   每行以币种图标开头（不写币名，省地方），后面是算式和结果。
   密度优先：17px 行高、10px 字号、等宽数字。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import JumpLatest from "@/components/arc/jump-latest/jump-latest"
import CoinAvatar from "@/components/ui/coin-avatar"
import styles from "./compute-trace.module.css"

const num = (v) => {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}
const fmt = (v) => {
  const n = num(v)
  if (n == null) return ""
  return Math.abs(n) >= 1000 ? n.toFixed(0) : n.toFixed(2)
}
const hhmmss = (ts) => {
  const n = num(ts)
  if (n == null) return ""
  const d = new Date(n)
  const p = (x) => String(x).padStart(2, "0")
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/** 一行计算：时间 · 币种图标 · 标签 · 算式 · 结果 */
const CalcRow = ({ row }) => {
  const tone = row.tone || undefined
  const showValue = row.value != null && row.value !== ""
  return (
    <div className={styles.row} data-kind={row.kind} data-tone={tone}>
      <span className={styles.time}>{hhmmss(row.ts)}</span>
      {row.sym ? <CoinAvatar symbol={row.sym} size={12} /> : <span className={styles.dot} />}
      <span className={styles.tag}>{row.tag}</span>
      <span className={styles.expr}>{row.expr}</span>
      {showValue ? <b className={styles.value}>{fmt(row.value)}</b> : null}
    </div>
  )
}

export default function ComputeTrace({ analysis, stats }) {
  const viewportRef = useRef(null)
  const followRef = useRef(true)
  const [atLatest, setAtLatest] = useState(true)

  // 后端给的是按时间排好的平铺数组，直接用；不再按币归类。
  const rows = useMemo(() => {
    const list = analysis?.calcStream
    return Array.isArray(list) ? list : []
  }, [analysis])

  // 新行进来时贴底——但只在用户本来就停在底部时
  useEffect(() => {
    const el = viewportRef.current
    if (!el || !followRef.current) return
    el.scrollTop = el.scrollHeight
  }, [rows.length])

  const onScroll = (event) => {
    const el = event.currentTarget
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 24
    followRef.current = near
    setAtLatest(near)
  }

  const jump = useCallback(() => {
    const el = viewportRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    followRef.current = true
    setAtLatest(true)
  }, [])

  // 模型侧计量（tok/s、缓存、费用、上下文）作为流的开头几行，
  // 它也是"这次运算"的一部分，不单独占一个标题块。
  const modelRows = useMemo(() => {
    if (!stats) return []
    const out = []
    if (stats.tps != null) out.push({ key: "tps", tag: "tok/s", expr: "completion ÷ 解码秒", value: stats.tps })
    if (stats.cacheHit != null) out.push({ key: "hit", tag: "hit%", expr: "cache_hit ÷ prompt", value: stats.cacheHit })
    if (stats.cost != null) out.push({ key: "cost", tag: "¥", expr: stats.costNote || "本轮费用", value: stats.cost })
    if (stats.ctxUsed != null && stats.ctxMax) {
      out.push({ key: "ctx", tag: "ctx%", expr: `${stats.ctxUsed} ÷ ${stats.ctxMax}`,
                 value: (stats.ctxUsed / stats.ctxMax) * 100 })
    }
    return out
  }, [stats])

  return (
    <section className={styles.root} aria-label="计算流水">
      <div className={styles.body}>
        <ScrollArea orientation="vertical" fade={14} scrollbars="auto" label="计算流水"
          viewportRef={viewportRef} className={styles.scroll}
          viewportClassName={styles.viewport} onScroll={onScroll}>
          {modelRows.map((r) => (
            <div className={styles.row} data-kind="model" key={r.key}>
              <span className={styles.time} />
              <span className={styles.dot} />
              <span className={styles.tag}>{r.tag}</span>
              <span className={styles.expr}>{r.expr}</span>
              <b className={styles.value}>{fmt(r.value)}</b>
            </div>
          ))}

          {rows.length === 0 && modelRows.length === 0 ? (
            <p className={styles.empty}>等待运算…</p>
          ) : rows.map((row, i) => (
            <CalcRow key={`${row.ts}-${row.sym}-${row.tag}-${i}`} row={row} />
          ))}
        </ScrollArea>

        {!atLatest && <JumpLatest onClick={jump} />}
      </div>
    </section>
  )
}
