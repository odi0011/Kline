/* 新闻时间升序、贴底跟随、内置阅读与后台 AI 任务。 */
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react"
import { ArrowDown, BookOpen } from "lucide-react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import ArticleModal from "./article-modal"
import { ROW_H, createAnalysisTracker, newsKey, scrollAnchor, sortNews, syncViewport } from "./news-state"
import styles from "./news-feed.module.css"

const OVERSCAN = 10
const EMPTY = []

const pad = (n) => String(n).padStart(2, "0")
const fmtClock = (ts) => {
  if (!ts) return "--"
  const d = new Date(ts + 8 * 3600 * 1000)
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
}

/* 来源徽标：交易所用品牌色、加密媒体用中性深色、综合财经用蓝灰 —— 一眼区分消息性质 */
const SRC_STYLE = {
  "币安": { kind: "exchange", label: "币安" },
  "欧易": { kind: "exchange", label: "欧易" },
  "Cointelegraph": { kind: "media", label: "CT" },
  "CoinDesk": { kind: "media", label: "CD" },
  "The Block": { kind: "media", label: "BLK" },
  "Decrypt": { kind: "media", label: "DEC" },
  "CryptoSlate": { kind: "media", label: "CS" },
  "英为财情·加密": { kind: "finance", label: "财经" },
  "英为财情·外汇": { kind: "finance", label: "外汇" },
  "英为财情·商品": { kind: "finance", label: "商品" },
  "WSJ·市场": { kind: "finance", label: "WSJ" },
  "ForexLive": { kind: "finance", label: "FL" },
}
const srcMeta = (s) => SRC_STYLE[s] || { kind: "media", label: String(s || "?").slice(0, 4) }

/** 标题切片：中文按字切，过长时中间省略（保住尾部日期/关键词） */
function splitTitle(title) {
  const s = String(title || "")
  if (s.length <= 30) return [s, ""]
  const keepTail = 10
  let cut = s.length - keepTail
  // 数字不切碎
  while (cut > 4 && /[0-9.]/.test(s[cut]) && /[0-9.]/.test(s[cut - 1])) cut--
  return [s.slice(0, cut), s.slice(cut)]
}

function NewsRow({ it, onOpen }) {
  const [head, tail] = splitTitle(it.title)
  const meta = srcMeta(it.source)
  return (
    <button type="button" className={styles.row} onClick={() => onOpen(it)} aria-haspopup="dialog"
      title={`${it.source}${it.cat ? ` · ${it.cat}` : ""}\n${it.title}`}>
      <span className={styles.src} data-kind={meta.kind}>{meta.label}</span>
      <span className={styles.time}>{fmtClock(Number(it.ts))}</span>
      <span className={styles.text}>
        <span className={styles.head}>{head}</span>
        {tail && <span className={styles.tail}>{tail}</span>}
      </span>
      <BookOpen size={10} className={styles.ext} aria-hidden />
    </button>
  )
}

/**
 * onRead(item) -> Promise<{ok, content, partial, error}>
 * onAnalyze(item, mode: interpret|impact|risk, content) -> Promise<{ok, id, error}>
 * tasks: server snapshots, matched strictly by the returned task.id.
 */
export default function NewsFeed({ items = EMPTY, onRead, onAnalyze, tasks = EMPTY }) {
  const [scrollTop, setScrollTop] = useState(0)
  const [viewH, setViewH] = useState(0)
  const [follow, setFollow] = useState(true)
  const [selected, setSelected] = useState(null)
  const [entries, setEntries] = useState(() => new Map())
  const trackerRef = useRef(null)
  if (!trackerRef.current) trackerRef.current = createAnalysisTracker(setEntries)
  const viewportRef = useRef(null)
  const followRef = useRef(true)
  const anchorRef = useRef(null)
  const list = useMemo(() => sortNews(items || EMPTY), [items])
  const listRef = useRef(list)
  const programmaticTop = useRef(null)
  const touchY = useRef(null)

  const sync = useCallback(() => {
    const el = viewportRef.current
    if (!el) return
    const position = syncViewport(el, listRef.current, followRef.current, anchorRef.current)
    programmaticTop.current = position.top
    anchorRef.current = position.anchor
    setViewH(position.height)
    setScrollTop(position.top)
  }, [])

  // Before first paint, including an initially empty feed becoming populated.
  // Depend on the snapshot, not just length: rolling windows also replace rows.
  useLayoutEffect(() => {
    listRef.current = list
    sync()
  }, [list, sync])

  useLayoutEffect(() => {
    const el = viewportRef.current
    if (!el) return
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(sync) : null
    observer?.observe(el)
    window.addEventListener("resize", sync)
    return () => { observer?.disconnect(); window.removeEventListener("resize", sync) }
  }, [sync])

  const handleScroll = useCallback(() => {
    const el = viewportRef.current
    if (!el) return
    setScrollTop(el.scrollTop)
    anchorRef.current = scrollAnchor(listRef.current, el.scrollTop)
    // Resize/anchor adjustments must not overwrite the reader's follow choice.
    if (programmaticTop.current != null && Math.abs(el.scrollTop - programmaticTop.current) < 1) {
      programmaticTop.current = null
      return
    }
    programmaticTop.current = null
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= 2
    followRef.current = atBottom
    setFollow(atBottom)
  }, [])
  const jumpLatest = () => {
    followRef.current = true
    setFollow(true)
    sync()
  }
  // Capture reading intent before a snapshot can race the native scroll event.
  const pauseFollow = () => {
    const el = viewportRef.current
    if (!el || el.scrollHeight <= el.clientHeight + 2) return
    followRef.current = false
    programmaticTop.current = null
    anchorRef.current = scrollAnchor(listRef.current, el.scrollTop)
    setFollow(false)
  }
  const close = useCallback(() => setSelected(null), [])
  const analyze = typeof onAnalyze === "function" ? (item, mode, content) =>
    trackerRef.current.submit(item, mode, content, onAnalyze, tasks || EMPTY) : undefined
  const total = list.length
  const winH = Math.max(viewH, 160)
  const start = Math.min(total, Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN))
  const end = Math.min(total, Math.ceil((scrollTop + winH) / ROW_H) + OVERSCAN)
  const slice = list.slice(start, end)

  return (
    <section className={styles.wrap} aria-label="新闻与交易所公告">
      <ScrollArea viewportRef={viewportRef} orientation="vertical" fade={18} scrollbars="auto"
        className={styles.stream} viewportClassName={styles.streamViewport}
        onWheelCapture={(event) => { if (event.deltaY < 0 && !event.ctrlKey) pauseFollow() }}
        onKeyDownCapture={(event) => { if (["ArrowUp", "PageUp", "Home"].includes(event.key) || (event.key === " " && event.shiftKey)) pauseFollow() }}
        onTouchStartCapture={(event) => { touchY.current = event.touches[0]?.clientY ?? null }}
        onTouchMoveCapture={(event) => {
          const y = event.touches[0]?.clientY
          if (touchY.current != null && y > touchY.current) pauseFollow()
          touchY.current = y ?? null
        }}
        label="新闻与交易所公告" onScroll={handleScroll}>
        {total === 0 ? <div className={styles.empty}>暂无新闻（等待首次拉取）</div> : (
          <div className={styles.spacer} style={{ height: total * ROW_H }}>
            {slice.map((it, i) => (
              <div key={newsKey(it)} className={styles.rowSlot} style={{ transform: `translateY(${(start + i) * ROW_H}px)` }}>
                <NewsRow it={it} onOpen={setSelected} />
              </div>
            ))}
          </div>
        )}
      </ScrollArea>
      {!follow && total > 0 && <button type="button" className={styles.latest} onClick={jumpLatest}>
        <ArrowDown size={12} aria-hidden /> 最新
      </button>}
      {selected && <ArticleModal key={newsKey(selected)} item={selected} onRead={onRead} onAnalyze={analyze}
        entries={entries} tasks={tasks || EMPTY} onClose={close} fallbackFocus={viewportRef} />}
    </section>
  )
}
