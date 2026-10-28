// Adapted from https://www.beautifului.dev/r/task-rows.json — MIT, see ../LICENSE.
// Removed TICKS/useTick/demo rows. Status, progress and expansion are controlled.
import styles from "../agent-panel.module.css"

function SpinnerRing({ active, children }) {
  const size = 24, stroke = 2
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  return (
    <span className="relative inline-flex shrink-0 items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className={`absolute inset-0 ${active ? styles.spin : ""}`} aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--line)" strokeWidth={stroke} />
        {active && <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--ink-3)" strokeWidth={stroke} strokeLinecap="round" strokeDasharray={`${c * 0.28} ${c * 0.72}`} />}
      </svg>
      <span className="relative text-[10.5px] font-semibold tabular-nums text-ink">{children}</span>
    </span>
  )
}
function Badge({ tone, children }) {
  return <span className={styles.statusIcon} data-tone={tone}>{children}</span>
}
const XIcon = <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round"><path d="M18 6L6 18M6 6l12 12" /></svg>
const CheckIcon = <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>

export default function TaskRows({ rows = [], expanded, onToggleRow, renderDetail, className = "" }) {
  const badgeFor = (row) => {
    if (row.status === "succeeded") return <Badge tone="success">{CheckIcon}</Badge>
    if (row.status === "failed") return <Badge tone="failed">{XIcon}</Badge>
    return <SpinnerRing active={row.status === "running"}>{row.step}</SpinnerRing>
  }
  return (
    <div className={`flex w-full flex-col ${className}`}>
      {rows.map((row) => {
        const open = expanded === row.key
        return (
          <article key={row.key} className={styles.taskRow} data-status={row.status}>
            <button type="button" aria-expanded={open} aria-controls={row.detailId}
              onClick={() => onToggleRow(row.key, !open)} className={styles.taskHead}>
              <span className="flex size-6 shrink-0 items-center justify-center" aria-hidden="true">{badgeFor(row)}</span>
              <span className={styles.taskTitle}>{row.label}</span>
              <span className={styles.taskBadge} data-tone={row.status}>{row.statusLabel}</span>
              <span aria-hidden="true" className="flex shrink-0 items-center justify-center text-ink-3">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="transition-transform duration-300" style={{ transform: open ? "rotate(180deg)" : "rotate(0)" }}><path d="M6 9l6 6 6-6" /></svg>
              </span>
            </button>
            <div className={styles.taskMeta}><span>{row.amount}</span><span>{row.meta}</span></div>
            <div id={row.detailId} className="grid transition-[grid-template-rows,opacity] duration-300" style={{ gridTemplateRows: open ? "1fr" : "0fr", opacity: open ? 1 : 0 }} inert={!open} aria-hidden={!open}>
              <div className="min-h-0 overflow-hidden">
                {open && <div className={styles.taskDetail}>{renderDetail(row)}</div>}
              </div>
            </div>
          </article>
        )
      })}
    </div>
  )
}
