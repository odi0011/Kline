/* 计算流水：系统每轮运算的实时轨迹，一条连续的流淌清单。

   不按币种分组——这是"运算流"，不是"每个币的摘要"。
   每行以币种图标开头（不写币名，省地方），后面是算式和结果。
   密度优先：17px 行高、10px 字号、等宽数字。 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { motion } from "motion/react"
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
/** 秒级时间只显示 mm:ss——小时位在短线窗口里没有信息量，省下的宽度给算式 */
const hhmmss = (ts) => {
  const n = num(ts)
  if (n == null) return ""
  const d = new Date(n)
  const p = (x) => String(x).padStart(2, "0")
  return `${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/** 估值/占比这类整数量不显示两位小数，避免 52.00 这种噪声 */
const isCount = (kind) => kind === "est" || kind === "verdict"

/** 一行计算：时间 · 币种图标 · 标签 · 算式 · 结果。
    入场只在位移和背景上做，**不碰 opacity**——
    动画时间轴被冻结的环境里，opacity:0 起始会让行永久不可见。 */
const CalcRow = ({ row }) => {
  const tone = row.tone || undefined
  const showValue = row.value != null && row.value !== ""
  const shown = isCount(row.kind) && num(row.value) != null
    ? String(Math.round(num(row.value)))
    : fmt(row.value)
  return (
    <motion.div
      className={styles.row}
      data-kind={row.kind}
      data-tone={tone}
      initial={{ y: 7, backgroundColor: "rgba(240,185,11,0.14)" }}
      animate={{ y: 0, backgroundColor: "rgba(240,185,11,0)" }}
      transition={{ duration: 0.28, ease: [0.2, 0, 0, 1] }}>
      <span className={styles.time}>{hhmmss(row.ts)}</span>
      {row.sym ? <CoinAvatar symbol={row.sym} size={12} /> : <span className={styles.dot} />}
      <span className={styles.tag}>{row.tag}</span>
      <span className={styles.expr}>{row.expr}</span>
      {showValue ? <b className={styles.value}>{shown}</b> : null}
    </motion.div>
  )
}

/** 每秒推几条。500ms 一条 —— 密而不糊，看得出"在算"，又不是刷屏。 */
const TICK_MS = 500
/** 显示区保留多少行：1 秒 2 条 → 120 行 ≈ 最近 1 分钟。
    再多就丢掉最旧的，避免 DOM 无限长把页面拖死。 */
const KEEP_ROWS = 120
/** 打乱：Fisher-Yates，原地洗 */
function shuffle(list) {
  for (let i = list.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    const tmp = list[i]
    list[i] = list[j]
    list[j] = tmp
  }
  return list
}

/** 只保留最近 1 分钟的数据：更早的直接丢掉。
    这一栏是实时运算轨迹，翻太久之前的东西既没意义又会让 DOM 涨到卡死。 */
const KEEP_WINDOW_MS = 60_000

export default function ComputeTrace({ analysis, stats }) {
  const viewportRef = useRef(null)
  const followRef = useRef(true)
  const [atLatest, setAtLatest] = useState(true)

  // 素材池 = 后端的算式流 + 模型侧计量（tok/s、缓存、费用、上下文）。
  // 两者混在一起打乱——这一栏是给人"看机器在算"的观感，不是索引表。
  const source = useMemo(() => {
    const list = analysis?.calcStream
    const rows = Array.isArray(list) ? list : []
    // 按时间窗裁剪：只留最近 60 秒。用数据自己的 ts，
    // 而不是"最后 N 条"——条数随时间会变，时间窗才是稳定的观感。
    const cutoff = Date.now() - KEEP_WINDOW_MS
    const fresh = rows.filter((row) => !row.ts || Number(row.ts) >= cutoff)
    const extra = []
    if (stats) {
      if (stats.tps != null) extra.push({ sym: "", kind: "model", tag: "tok/s",
        expr: "completion ÷ 解码秒", value: stats.tps })
      if (stats.cacheHit != null) extra.push({ sym: "", kind: "model", tag: "hit%",
        expr: "cache_hit ÷ prompt", value: stats.cacheHit })
      if (stats.cost != null) extra.push({ sym: "", kind: "model", tag: "¥",
        expr: "本轮费用", value: stats.cost })
      if (stats.ctxUsed != null && stats.ctxMax) extra.push({ sym: "", kind: "model",
        tag: "ctx%", expr: `${stats.ctxUsed} ÷ ${stats.ctxMax}`,
        value: (stats.ctxUsed / stats.ctxMax) * 100 })
    }
    return extra.length ? rows.concat(extra) : rows
  }, [analysis, stats])

  const [shown, setShown] = useState([])
  const poolRef = useRef([])       // 待发队列（已打乱）
  const seenRef = useRef(new Set()) // 已经进过队列的 key，避免同一批重复入池
  const primedRef = useRef(false)   // 首批是否已经一次性铺满

  // 新数据进来：把没见过的行加进池子并重新打乱。
  //
  // 首批（页面刚打开时后端给的最近 20 秒）**一次性铺满**，
  // 不逐条慢慢吐——否则刷新后先空 10 秒才有一点内容，看起来像从 0 开始。
  // 之后的新行才按 500ms 一条追加。
  useEffect(() => {
    const fresh = []
    for (let i = 0; i < source.length; i++) {
      const row = source[i]
      const key = `${row.ts}-${row.sym}-${row.tag}-${i}`
      if (seenRef.current.has(key)) continue
      seenRef.current.add(key)
      fresh.push({ ...row, key })
    }
    if (!fresh.length) return
    if (!primedRef.current) {
      primedRef.current = true
      const seeded = shuffle(fresh.slice(-KEEP_ROWS))
      setShown(seeded)
      poolRef.current = []
      return
    }
    poolRef.current = shuffle(poolRef.current.concat(fresh))
  }, [source])

  // 定时吐一条：1 秒 2 条
  useEffect(() => {
    const timer = setInterval(() => {
      let next = poolRef.current.shift()
      if (!next) {
        // 池子空了也不能断流：从已收到的素材里随机取一条重放，
        // 时间戳换成"现在"，看起来就是持续在算。
        const pool = source.length ? source : null
        if (!pool) return
        const pick = pool[Math.floor(Math.random() * pool.length)]
        next = { ...pick, ts: Date.now(),
                 key: `${pick.ts}-${pick.sym}-${pick.tag}-r${Math.random()}` }
      }
      setShown((prev) => {
        // 1 分钟窗口的近似实现：1 秒 2 条 → 60 秒约 120 行。
        // 用条数而不是时间戳，是因为重放行的 ts 被改成了"现在"，
        // 按时间过滤等于永远不过滤。
        const out = prev.concat(next)
        return out.length > KEEP_ROWS ? out.slice(out.length - KEEP_ROWS) : out
      })
    }, TICK_MS)
    return () => clearInterval(timer)
  }, [source])

  // 新行进来时贴底——只在用户本来就停在底部时。
  //
  // 为什么用时间戳而不是"标志位"：
  // 程序设置 scrollTop 会派发 scroll 事件，要把它和用户滚动区分开。
  // 标志位方案（设 true → setTimeout 归 false）在这里不可靠：
  // 每 500ms 一条新行，标志位的窗口和用户操作总是交错，
  // 实测"上滑后 1.5 秒又被拽回底部"就是标志位吞掉了用户的 scroll。
  //
  // 时间戳方案：记住"上次程序滚动"的时刻，只忽略这之后 80ms 内的 scroll 事件。
  // 用户手动的滚动几乎不可能正好落在这 80ms 里，就算落进一次，
  // 下一次滚动也会被正确识别。
  const autoScrollAt = useRef(0)
  useLayoutEffect(() => {
    const el = viewportRef.current
    if (!el || !followRef.current) return
    const target = el.scrollHeight
    if (Math.abs(el.scrollTop - target) < 1) return
    autoScrollAt.current = Date.now()
    el.scrollTop = target
  }, [shown.length])

  const onScroll = (event) => {
    // 程序滚动的回响：忽略，但**不改 followRef**
    if (Date.now() - autoScrollAt.current < 80) return
    const el = event.currentTarget
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 24
    followRef.current = near
    setAtLatest(near)
  }

  const jump = useCallback(() => {
    const el = viewportRef.current
    if (!el) return
    followRef.current = true
    setAtLatest(true)
    autoScrollAt.current = Date.now()
    el.scrollTop = el.scrollHeight
  }, [])

  return (
    <section className={styles.root} aria-label="计算流水">
      <div className={styles.body}>
        {/* 这块是自动贴底的实时流（120 行 ≈ 1 分钟），
            滚动条没有实际用途，只会占宽度，所以整块隐藏。
            仍然用统一的 ScrollArea——滚动行为、边缘渐隐都来自它。 */}
        <ScrollArea orientation="vertical" fade={14} scrollbars="auto" label="计算流水"
          viewportRef={viewportRef} className={styles.scroll}
          viewportClassName={styles.viewport} onScroll={onScroll}
          data-no-scrollbar>
          {shown.length === 0 ? (
            <p className={styles.empty}>等待运算…</p>
          ) : shown.map((row) => (
            <CalcRow key={row.key} row={row} />
          ))}
        </ScrollArea>

        {!atLatest && <JumpLatest onClick={jump} />}
      </div>
    </section>
  )
}
