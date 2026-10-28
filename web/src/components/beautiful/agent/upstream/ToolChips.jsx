// Adapted from https://www.beautifului.dev/r/tool-chips.json — MIT, see ../LICENSE.
// Keep expandable tool-call rows; remove STEP_MS, staged reveal and file-diff demo.
import { useState } from "react"
import styles from "../agent-panel.module.css"

const Icons = {
  think: <path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" />,
  run: <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 17l6-5-6-5M12 19h8" /></g>,
  read: <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></g>,
}
export default function ToolChips({ steps = [], header = "工具轨迹" }) {
  const [open, setOpen] = useState(true)
  const [openRows, setOpenRows] = useState(new Set())
  const toggleRow = (id) => setOpenRows((current) => {
    const next = new Set(current)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  return (
    <div className={styles.tools}>
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className={styles.traceHeader}>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="transition-transform duration-200" style={{ transform: open ? "rotate(0deg)" : "rotate(-90deg)" }} aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>
        <span className="tabular-nums">{header}</span>
      </button>
      <div className="grid transition-[grid-template-rows,opacity] duration-300" style={{ gridTemplateRows: open ? "1fr" : "0fr", opacity: open ? 1 : 0 }} inert={!open} aria-hidden={!open}>
        <div className="min-h-0 overflow-hidden">
          <div className="mt-1.5 flex flex-col gap-1">
            {steps.map((row) => {
              const rowOpen = openRows.has(row.id)
              return <div key={row.id}>
                <button type="button" aria-expanded={rowOpen} onClick={() => toggleRow(row.id)} className={`group/row ${styles.toolRow}`}>
                  <span className="relative flex size-4 shrink-0 items-center justify-center text-ink-3" aria-hidden="true">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill={row.icon === "think" ? "currentColor" : "none"} stroke="currentColor" className={`transition-opacity duration-100 group-hover/row:opacity-0 ${rowOpen ? "opacity-0" : ""}`}>{Icons[row.icon] || Icons.run}</svg>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className={`absolute transition-[opacity,transform] duration-150 group-hover/row:opacity-100 ${rowOpen ? "opacity-100" : "opacity-0"}`} style={{ transform: rowOpen ? "rotate(0deg)" : "rotate(-90deg)" }}><path d="M6 9l6 6 6-6" /></svg>
                  </span>
                  <span className={styles.toolLabel}>{row.label}</span>
                  <span className={styles.toolChip} data-tone={row.status}>{row.chip}</span>
                </button>
                <div className="grid transition-[grid-template-rows,opacity] duration-300" style={{ gridTemplateRows: rowOpen ? "1fr" : "0fr", opacity: rowOpen ? 1 : 0 }} inert={!rowOpen} aria-hidden={!rowOpen}>
                  <div className="min-h-0 overflow-hidden"><div className={styles.toolDetail}>
                    {row.detail.map((line, i) => <span key={i}>{line.text}</span>)}
                  </div></div>
                </div>
              </div>
            })}
          </div>
        </div>
      </div>
    </div>
  )
}
