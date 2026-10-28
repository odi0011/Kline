// Adapted from https://www.beautifului.dev/r/thinking-state.json — MIT, see ../LICENSE.
// Steps variant only; no sequence, inferred completion, demo rows or minimum height.
import { useState } from "react"
import styles from "../agent-panel.module.css"

export default function ThinkingState({ working = false, rows = [], active = "分析中", done = "工具轨迹" }) {
  const [manualExpanded, setManualExpanded] = useState(null)
  const expanded = manualExpanded ?? working
  return (
    <div className={styles.thinking}>
      <button type="button" aria-expanded={rows.length ? expanded : undefined} disabled={!rows.length}
        onClick={() => setManualExpanded((current) => !(current ?? working))}
        className="flex w-fit items-center gap-2 rounded-control px-1.5 py-1 transition-colors duration-100 hover:bg-hover-2">
        <svg width="14" height="14" viewBox="0 0 24 24" fill={working ? "var(--ink-2)" : "var(--ink-3)"} aria-hidden="true"><path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" /></svg>
        <span role="status" className={working ? styles.shimmer : "text-ink-2"}>{working ? active : done}</span>
        {!!rows.length && <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--ink-3)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" style={{ transform: expanded ? "rotate(180deg)" : "rotate(0)" }} aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>}
      </button>
      {!!rows.length && <div className="grid transition-[grid-template-rows,opacity] duration-300" style={{ gridTemplateRows: expanded ? "1fr" : "0fr", opacity: expanded ? 1 : 0 }} inert={!expanded} aria-hidden={!expanded}>
        <div className="min-h-0 overflow-hidden">
          <div className="relative mt-1 ml-[5px] pl-4">
            <span aria-hidden="true" className={styles.traceLine} />
            <div className="flex flex-col gap-1 py-1">
              {rows.map((row, i) => <div key={row.id ?? i} className="flex min-h-7 w-full items-center gap-2 rounded-[6px] px-1.5 py-0.5 text-left">
                {row.status === "succeeded" ? <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--ink-3)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="shrink-0" aria-hidden="true"><path d="M20 6L9 17l-5-5" /></svg> : <span className={row.status === "running" ? styles.stepSpinner : styles.stepDot} aria-hidden="true" />}
                <span className="min-w-0 text-ink-2">{row.primary}</span>
                {row.secondary && <span className="text-ink-3">{row.secondary}</span>}
              </div>)}
            </div>
          </div>
        </div>
      </div>}
    </div>
  )
}
