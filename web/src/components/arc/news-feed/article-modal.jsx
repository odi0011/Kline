import { useEffect, useId, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { X } from "lucide-react"
import { Button } from "@/components/base/buttons/button"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import { readResult } from "./news-state"
import styles from "./news-feed.module.css"

export default function ArticleModal({ item, onRead, onClose, fallbackFocus }) {
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const readRef = useRef(onRead)
  readRef.current = onRead
  const titleId = useId()
  const metaId = useId()
  const [retry, setRetry] = useState(0)
  const [reading, setReading] = useState({ loading: true, content: "", partial: true, pending: false, error: "" })
  const [analysis, setAnalysis] = useState({ text: "", state: "" })
  const backdropDown = useRef(false)

  useLayoutEffect(() => {
    const dialog = dialogRef.current
    const previousFocus = document.activeElement
    const bodyOverflow = document.body.style.overflow
    const rootOverflow = document.documentElement.style.overflow
    dialog.showModal()
    document.body.style.overflow = "hidden"
    document.documentElement.style.overflow = "hidden"
    closeRef.current?.focus({ preventScroll: true })
    return () => {
      dialog.close()
      document.body.style.overflow = bodyOverflow
      document.documentElement.style.overflow = rootOverflow
      const target = previousFocus?.isConnected ? previousFocus : fallbackFocus.current
      target?.focus({ preventScroll: true })
    }
  }, [fallbackFocus])

  useEffect(() => {
    let active = true
    setReading({ loading: true, content: "", partial: true, pending: false, error: "" })
    setAnalysis({ text: "", state: "", })
    Promise.resolve().then(() => {
      if (!active) return
      if (typeof readRef.current !== "function") throw new Error("正文阅读接口尚未接入")
      return readRef.current(item)
    }).then((result) => {
      if (!active) return
      setReading({ loading: false, ...readResult(result, item.desc || "") })
      // AI 分析随正文一起下发（服务端在正文入库时已生成），这里只展示
      setAnalysis({ text: String(result?.analysis?.text || ""), state: String(result?.analysis?.state || "") })
    }).catch((error) => {
      if (active) setReading({ loading: false, ...readResult({ ok: false, error: error?.message || "正文读取失败" }, item.desc || "") })
    })
    // Ignore stale reads after switching/closing; no AI cancellation is sent.
    return () => { active = false }
  }, [item, retry])

  const outside = (event) => {
    if (event.target !== event.currentTarget) return false
    const rect = event.currentTarget.getBoundingClientRect()
    return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom
  }
  const trapTab = (event) => {
    if (event.key !== "Tab") return
    const buttons = [...event.currentTarget.querySelectorAll("button:not(:disabled)")]
    const first = buttons[0], last = buttons.at(-1)
    // Native dialog makes the page inert; also prevent Tab escaping to browser chrome.
    if ((event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last)) {
      event.preventDefault()
      const target = event.shiftKey ? last : first
      target?.focus()
    }
  }
  const date = new Date(Number(item.ts))
  return createPortal(
    <dialog ref={dialogRef} className={styles.dialog} aria-labelledby={titleId} aria-describedby={metaId}
      onKeyDown={trapTab} onCancel={(event) => { event.preventDefault(); onClose() }}
      onPointerDown={(event) => { backdropDown.current = outside(event) }}
      onClick={(event) => { if (backdropDown.current && outside(event)) onClose(); backdropDown.current = false }}>
      <header className={styles.modalHeader}>
        <div><h2 id={titleId}>{item.title}</h2>
          <p id={metaId} className={styles.meta}>{item.source || "未知来源"} · {Number.isFinite(date.getTime())
            ? date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) + " (UTC+8)" : "时间未知"}</p>
        </div>
        <button type="button" ref={closeRef} className={styles.close} aria-label="关闭新闻正文" onClick={onClose}><X size={18} /></button>
      </header>
      {/* AI 分析紧跟标题：这是用户最先要看的东西，不该埋在正文末尾 */}
      <section className={styles.analysisTop} aria-label="新闻 AI 分析">
        <span className={styles.analysisTag}>AI 分析</span>
        {analysis.text
          ? <p className={styles.analysisText}>{analysis.text}</p>
          : <p className={styles.analysisWait}>
              {reading.loading ? "读取中…"
                : reading.pending ? "正文抓取中，完成后自动分析"
                : analysis.state === "failed" ? "分析未生成，稍后会自动重试"
                : "分析生成中…"}
            </p>}
      </section>
      <ScrollArea orientation="vertical" fade={20} scrollbars="auto" label="新闻正文"
        className={styles.modalScroll} viewportClassName={styles.modalBody}>
        {reading.loading ? <p role="status">正在读取正文…</p> : <>
          {reading.pending
            ? <p className={styles.pendingBody}>正文正在后台抓取，先显示 RSS 摘要；稍后重新打开即可看到全文。</p>
            : reading.partial && <p className={styles.partial}>仅摘要 / 部分正文，并非全文。</p>}
          {reading.error && <div className={styles.error} role="alert">{reading.error}</div>}
          {reading.content && <div className={styles.article}>{reading.content}</div>}
          {(reading.error || (reading.partial && !reading.pending)) && (
            <Button variant="secondary" size="small" className={styles.retry}
              onClick={() => setRetry((n) => n + 1)}>重新读取</Button>
          )}
        </>}
      </ScrollArea>
    </dialog>, document.body,
  )
}
