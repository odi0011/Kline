import { useEffect, useId, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { X } from "lucide-react"
import { ANALYSIS_MODES, analysisKey, readResult, taskBusy, taskById } from "./news-state"
import styles from "./news-feed.module.css"

const STATUS = { queued: "排队中", pending: "等待执行", running: "运行中", succeeded: "已完成",
  completed: "已完成", failed: "失败", cancelled: "已取消", canceled: "已取消", paused: "已暂停" }
const display = (value) => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value, null, 2)

function TaskStatus({ entry, task }) {
  if (!entry) return null
  return <div className={styles.task} aria-live="polite">
    {entry.pending && <p role="status">正在提交后台任务…</p>}
    {entry.error && <p className={styles.error} role="alert">{entry.error}</p>}
    {entry.id != null && <>
      <p className={styles.taskMeta}>任务 {entry.id} · {task ? STATUS[task.status] || task.status || "等待状态" : "已提交，等待后台状态"}</p>
      {task?.progress != null && <p>后台进度：{display(task.progress)}</p>}
      {task?.message && <p>{display(task.message)}</p>}
      {task?.error && <p className={styles.error} role="alert">{display(task.error)}</p>}
      {task?.result && <pre className={styles.result}>{display(task.result)}</pre>}
    </>}
  </div>
}

export default function ArticleModal({ item, onRead, onAnalyze, entries, tasks, onClose, fallbackFocus }) {
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const readRef = useRef(onRead)
  readRef.current = onRead
  const titleId = useId()
  const metaId = useId()
  const [retry, setRetry] = useState(0)
  const [reading, setReading] = useState({ loading: true, content: "", partial: true, error: "" })
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
    setReading({ loading: true, content: "", partial: true, error: "" })
    Promise.resolve().then(() => {
      if (!active) return
      if (typeof readRef.current !== "function") throw new Error("正文阅读接口尚未接入")
      return readRef.current(item)
    }).then((result) => {
      if (active) setReading({ loading: false, ...readResult(result, item.desc || "") })
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
      <div className={styles.modalBody}>
        {reading.loading ? <p role="status">正在读取正文…</p> : <>
          {reading.partial && <p className={styles.partial}>仅摘要 / 部分正文，并非全文。AI 将基于有限内容分析。</p>}
          {reading.error && <div className={styles.error} role="alert">{reading.error}</div>}
          {reading.content && <div className={styles.article}>{reading.content}</div>}
          {(reading.error || reading.partial) && <button type="button" className={styles.action} onClick={() => setRetry((n) => n + 1)}>重新读取</button>}
        </>}
        <section className={styles.analysis} aria-label="新闻 AI 分析">
          <h3>AI 分析</h3>
          <p className={styles.meta}>任务在后台运行，关闭此窗口不会取消；重新打开可查看进度与结果。</p>
          {!onAnalyze && <p className={styles.meta}>AI 分析接口尚未接入。</p>}
          {ANALYSIS_MODES.map(({ mode, label }) => {
            const entry = entries.get(analysisKey(item, mode))
            const task = taskById(tasks, entry?.id)
            return <div key={mode} className={styles.analysisMode}>
              <button type="button" className={styles.action}
                disabled={!onAnalyze || reading.loading || !reading.content || taskBusy(entry, task)}
                onClick={() => onAnalyze(item, mode, reading.partial
                  ? `【仅摘要/部分正文，并非全文】\n${reading.content}` : reading.content)}>{label}</button>
              <TaskStatus entry={entry} task={task} />
            </div>
          })}
        </section>
      </div>
    </dialog>, document.body,
  )
}
