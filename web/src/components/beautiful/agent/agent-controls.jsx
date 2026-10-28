import { useEffect, useId, useRef } from "react"
import { Button } from "./upstream/Button"
import TaskRows from "./upstream/TaskRows"
import ToolChips from "./upstream/ToolChips"
import ThinkingState from "./upstream/ThinkingState"
import ApprovalCard from "./upstream/ApprovalCard"
import styles from "./agent-panel.module.css"

export { Button, ToolChips, ThinkingState }
export { AgentTabs, ChatThread, ModelSelect, PromptBar } from "./upstream/ChatComposer"
export const STATUS = { running: "运行中", succeeded: "已完成", failed: "失败", scheduled: "已计划", queued: "排队中", paused: "已暂停", cancelled: "已取消", interrupted: "已中断" }
export const isWorking = (status) => status === "running" || status === "queued"
export const textOf = (value) => {
  if (value == null) return ""
  if (typeof value === "string") return value
  try { return JSON.stringify(value, null, 2) } catch { return "无法显示结果" }
}
export const dateLabel = (value) => {
  if (!value) return "—"
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })
}
const intervalLabel = (seconds) => !seconds ? "单次" : seconds < 3600 ? `每 ${seconds / 60} 分钟` : seconds < 86400 ? `每 ${seconds / 3600} 小时` : `每 ${seconds / 86400} 天`

export function Activity({ steps = [], progress, status }) {
  const rows = (Array.isArray(steps) ? steps : []).filter(Boolean).map((step, index) => ({
    id: step.id || `${index}-${step.tool || step.name}`,
    icon: "run", label: step.tool || step.name || step.label || `步骤 ${index + 1}`,
    chip: STATUS[step.status] || step.status || "待执行", status: step.status,
    detail: [
      step.round && `第 ${step.round} 轮`,
      step.arguments != null && `参数：${textOf(step.arguments)}`,
      step.result != null && textOf(step.result),
      step.error && textOf(step.error),
      step.finishedAt && `完成 ${dateLabel(step.finishedAt)}`,
    ].filter(Boolean).map((text) => ({ text })),
  }))
  const phases = { model: "模型分析", tool: "调用工具", waiting: "等待执行" }
  const label = status === "queued" ? "排队中" : phases[progress?.phase] || "分析中"
  return <>
    {isWorking(status) && <ThinkingState working active={label} />}
    {progress?.round > 0 && <p className={styles.progress}>{`第 ${progress.round}${progress.maxRounds ? ` / ${progress.maxRounds}` : ""} 轮`}{Number.isFinite(progress.toolCalls) ? ` · ${progress.toolCalls} 次工具` : ""}</p>}
    {!!rows.length && <ToolChips steps={rows} header={`工具轨迹 · ${rows.length}`} />}
  </>
}

export function ResultText({ text, title, onRead }) {
  const value = textOf(text)
  if (!value) return null
  return <div className={styles.result}>
    <p>{value.length > 900 ? `${value.slice(0, 900)}…` : value}</p>
    {value.length > 900 && <Button variant="quiet" onClick={() => onRead({ title, text: value })}>展开阅读</Button>}
  </div>
}

export function TaskRow({ task, expanded, onToggle, onAction, onDelete, onEdit, onRead, disabled, canRun }) {
  const detailId = useId()
  const runs = Array.isArray(task.runs) ? task.runs.slice(-10).reverse() : []
  const row = { ...task, key: task.id, detailId, label: task.title || "未命名任务", statusLabel: STATUS[task.status] || task.status,
    step: isWorking(task.status) ? task.progress?.round || undefined : undefined,
    amount: intervalLabel(task.intervalSec), meta: task.status === "scheduled" ? dateLabel(task.nextRunAt) : `${task.runCount || 0} 次` }
  return <TaskRows rows={[row]} expanded={expanded ? task.id : null} onToggleRow={onToggle} renderDetail={() => <>
    <p className={styles.taskPrompt}>{task.prompt}</p>
    <Activity steps={task.steps} progress={task.progress} status={task.status} />
    {task.error && <p className={styles.error}>{textOf(task.error)}</p>}
    <ResultText text={task.result} title={row.label} onRead={onRead} />
    {!task.result && !task.error && !isWorking(task.status) && <p className={styles.muted}>暂无结果</p>}
    <div className={styles.taskActions}>
      {!isWorking(task.status) && <Button disabled={disabled || !canRun} onClick={() => onAction("run")}>{task.status === "scheduled" ? "立即运行" : "运行"}</Button>}
      {task.kind === "recurring" && task.status === "paused" && <Button disabled={disabled} onClick={() => onAction("resume")}>继续</Button>}
      {task.kind === "recurring" && !["paused", "cancelled"].includes(task.status) && <Button disabled={disabled} onClick={() => onAction("pause")}>暂停</Button>}
      {["queued", "running", "scheduled", "paused"].includes(task.status) && <Button disabled={disabled} onClick={() => onAction("cancel")}>停止</Button>}
      {!isWorking(task.status) && <Button disabled={disabled} onClick={onEdit}>编辑</Button>}
      <Button className={styles.deleteAction} disabled={disabled} onClick={onDelete}>删除</Button>
    </div>
    <details className={styles.history}>
      <summary>历史结果 · {runs.length}</summary>
      {!runs.length && <p className={styles.muted}>暂无历史</p>}
      {runs.map((run, index) => <details key={run.id || index} className={styles.historyRun}>
        <summary>{dateLabel(run.finishedAt || run.startedAt)} · {STATUS[run.status] || run.status}</summary>
        {run.error && <p className={styles.error}>{textOf(run.error)}</p>}
        <ResultText text={run.result} title={`${row.label} · ${dateLabel(run.finishedAt)}`} onRead={onRead} />
        {!run.result && !run.error && <p className={styles.muted}>无结果</p>}
        <Activity steps={run.steps} status={run.status} />
      </details>)}
    </details>
  </>} />
}

export function NativeDialog({ children, onClose, titleId, busy = false, className = "", role = "dialog" }) {
  const ref = useRef(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    const node = ref.current
    const previous = document.activeElement
    const root = node.closest("section")
    node.showModal()
    node.querySelector("[data-dialog-initial]")?.focus()
    return () => {
      node.close()
      const target = previous?.isConnected ? previous : root?.querySelector('[role="tab"][aria-selected="true"]')
      target?.focus({ preventScroll: true })
    }
  }, [])
  return <dialog ref={ref} role={role} aria-labelledby={titleId} aria-modal="true" className={`${styles.dialog} ${className}`}
    onCancel={(event) => { event.preventDefault(); if (!busy) closeRef.current() }}>
    {children}
  </dialog>
}

export function DeleteDialog({ task, onConfirm, onClose, busy, disabled, error }) {
  const titleId = useId()
  return <NativeDialog onClose={onClose} titleId={titleId} busy={busy} role="alertdialog" className={styles.deleteDialog}>
    <ApprovalCard title={`删除「${task.title || "任务"}」？`} titleId={titleId} onConfirm={onConfirm} onCancel={onClose} busy={busy} disabled={disabled} error={error}>
      <p>历史结果也会删除。</p>
    </ApprovalCard>
  </NativeDialog>
}

export function ReadingDialog({ result, onClose }) {
  const titleId = useId()
  return <NativeDialog onClose={onClose} titleId={titleId}>
    <div className={styles.readingHeader}><strong id={titleId}>{result.title || "结果"}</strong><Button onClick={onClose} data-dialog-initial>关闭</Button></div>
    <p className={styles.readingText}>{result.text}</p>
  </NativeDialog>
}
