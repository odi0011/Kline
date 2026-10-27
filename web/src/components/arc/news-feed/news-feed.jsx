/* 新闻流：交易所公告 + 加密媒体 + 综合财经（真实数据，后台 10 分钟刷新）。
   与另外两个板块同一套视觉：无边框、虚拟滚动、单行 + 中间省略。
   点条目在浏览器新标签打开原文。 */
import { useMemo, useRef, useState } from "react"
import { ExternalLink } from "lucide-react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import styles from "./news-feed.module.css"

const ROW_H = 22
const OVERSCAN = 10

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

function NewsRow({ it }) {
  const [head, tail] = splitTitle(it.title)
  const meta = srcMeta(it.source)
  return (
    <a className={styles.row} href={it.url || "#"} target="_blank" rel="noopener noreferrer"
      title={`${it.source}${it.cat ? ` · ${it.cat}` : ""}\n${it.title}`}>
      <span className={styles.src} data-kind={meta.kind}>
        {meta.label}
      </span>
      <span className={styles.time}>{fmtClock(it.ts)}</span>
      <span className={styles.text}>
        <span className={styles.head}>{head}</span>
        {tail && <span className={styles.tail}>{tail}</span>}
      </span>
      <ExternalLink size={9} className={styles.ext} aria-hidden />
    </a>
  )
}

export default function NewsFeed({ items }) {
  const [scrollTop, setScrollTop] = useState(0)
  const [viewH, setViewH] = useState(0)
  const viewportRef = useRef(null)

  // 视口高度（虚拟窗口）
  useMemo(() => {
    // 用 rAF 保证 DOM 挂载后再测
    if (typeof requestAnimationFrame !== "undefined") {
      requestAnimationFrame(() => {
        const el = viewportRef.current
        if (el && el.clientHeight && el.clientHeight !== viewH) setViewH(el.clientHeight)
      })
    }
  }, [items && items.length])

  const list = items || []
  const total = list.length
  const winH = Math.max(viewH, 160)
  const start = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN)
  const end = Math.min(total, Math.ceil((scrollTop + winH) / ROW_H) + OVERSCAN)
  const slice = useMemo(() => list.slice(start, end), [list, start, end])

  return (
    <section className={styles.wrap} aria-label="交易所公告">
      <ScrollArea viewportRef={viewportRef} orientation="vertical" fade={18} scrollbars="auto"
        className={styles.stream} viewportClassName={styles.streamViewport}
        label="交易所公告" onScroll={() => {
          const el = viewportRef.current
          if (el) setScrollTop(el.scrollTop)
        }}>
        {total === 0 ? (
          <div className={styles.empty}>暂无公告（等待首次拉取）</div>
        ) : (
          <div className={styles.spacer} style={{ height: total * ROW_H }}>
            {slice.map((it, i) => {
              const idx = start + i
              return (
                <div key={`${it.source}-${it.ts}-${idx}`} className={styles.rowSlot}
                  style={{ transform: `translateY(${idx * ROW_H}px)` }}>
                  <NewsRow it={it} />
                </div>
              )
            })}
          </div>
        )}
      </ScrollArea>
    </section>
  )
}
