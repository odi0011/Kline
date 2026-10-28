// Adapted from https://www.beautifului.dev/r/approval-card.json — MIT, see ../LICENSE.
// Single confirmation question: retain card, dismiss and footer actions, with
// controlled async submission instead of quiz selection/auto-advance animation.
import { Button } from "./Button"
import styles from "../agent-panel.module.css"

export default function ApprovalCard({ title, titleId, children, onConfirm, onCancel, busy, disabled, error }) {
  return <div className="w-full">
    <div className={`relative overflow-hidden rounded-card bg-surface shadow-card ${styles.approval}`}>
      <Button variant="quiet" aria-label="关闭" onClick={onCancel} disabled={busy} className={styles.approvalClose}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12" /></svg>
      </Button>
      <div className={styles.approvalContent}>
        <strong id={titleId}>{title}</strong>
        {children}
        {error && <p role="alert" className={styles.error}>{error}</p>}
      </div>
      <div className={`flex items-center justify-between gap-3 ${styles.approvalActions}`}>
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy} data-dialog-initial>保留</Button>
        <Button variant="accent" size="sm" onClick={onConfirm} disabled={busy || disabled} className={styles.danger}>{busy ? "提交中" : "删除"}</Button>
      </div>
    </div>
  </div>
}
