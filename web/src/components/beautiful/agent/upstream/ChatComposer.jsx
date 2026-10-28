// Adapted from https://www.beautifului.dev/r/chat-composer.json — MIT, see ../LICENSE.
// Split the original header, conversation and composer into controlled slots.
// Removed scripted replies, phases and initial prompt; textarea supports IME.
import { useId, useRef } from "react"
import { Button } from "./Button"
import styles from "../agent-panel.module.css"

export function AgentTabs({ value, onChange, running, prefix }) {
  const suggestions = [{ id: "chat", label: "对话" }, { id: "tasks", label: "任务" }, { id: "schedule", label: "计划" }]
  return <div className={styles.tabs} role="tablist" aria-label="ODDD 工作区">
    {suggestions.map((item, index) => <button key={item.id} type="button" role="tab"
      id={`${prefix}-tab-${item.id}`} aria-controls={`${prefix}-panel-${item.id}`}
      aria-selected={value === item.id} tabIndex={value === item.id ? 0 : -1}
      onClick={() => onChange(item.id)} onKeyDown={(event) => {
        let next
        if (event.key === "ArrowRight") next = (index + 1) % suggestions.length
        if (event.key === "ArrowLeft") next = (index + suggestions.length - 1) % suggestions.length
        if (event.key === "Home") next = 0
        if (event.key === "End") next = suggestions.length - 1
        if (next === undefined) return
        event.preventDefault()
        onChange(suggestions[next].id)
        document.getElementById(`${prefix}-tab-${suggestions[next].id}`)?.focus()
      }} className={`${styles.tab} rounded-[6px] transition-[background-color,opacity] duration-100`} data-active={value === item.id || undefined}>
      {item.label}{item.id === "tasks" && running > 0 && <span className={styles.tabDot} />}
    </button>)}
  </div>
}

export function ChatThread({ messages, viewportRef, onScroll, renderMessage, empty }) {
  return <div className={styles.messageViewport} ref={viewportRef} onScroll={onScroll}>
    {!messages.length && empty}
    {messages.map((message) => <div key={message.id} className={styles.message} data-role={message.role}>
      {message.role === "assistant" && <img className={styles.messageAvatar} src="/icon-32.png" alt="" />}
      <div className={styles.messageBody}>{renderMessage(message)}</div>
    </div>)}
  </div>
}

export function ModelSelect({ models, value, onChange, disabled, state, className, label = "选择对话模型" }) {
  return <select className={className} aria-label={label} value={value} disabled={disabled || !models.length}
    onChange={(event) => onChange(event.target.value)}>
    {!models.length && <option value="">{state || "未配置模型"}</option>}
    {models.map((item) => <option key={item.id} value={item.id}>{item.label || item.name || item.id}</option>)}
  </select>
}

export function PromptBar({ value, onChange, onSend, busy, disabled, models, model, onModelChange, modelState }) {
  const inputRef = useRef(null)
  const composing = useRef(false)
  const hint = useId()
  const canSend = !!value.trim() && !busy && !disabled && models.some((item) => item.id === model)
  return <form className={styles.composer} onSubmit={(event) => { event.preventDefault(); if (canSend && !composing.current) onSend() }} aria-busy={busy}>
    <div className="flex cursor-text flex-col gap-2" onClick={(event) => { if (event.target === event.currentTarget) inputRef.current?.focus() }}>
      <textarea ref={inputRef} rows={2} maxLength={6000} value={value} readOnly={busy}
        onChange={(event) => onChange(event.target.value)}
        onCompositionStart={() => { composing.current = true }} onCompositionEnd={() => { composing.current = false }}
        onKeyDown={(event) => {
          if (event.key !== "Enter" || event.shiftKey) return
          if (composing.current || event.nativeEvent.isComposing || event.keyCode === 229) return
          event.preventDefault()
          if (canSend) onSend()
        }} placeholder="问市场、持仓或系统…" aria-label="向 ODDD 提问" aria-describedby={modelState ? hint : undefined}
        className="min-h-4.5 bg-transparent leading-[1.4] text-ink outline-none placeholder:text-ink-3" />
      <div className={styles.composerFoot}>
        <ModelSelect models={models} value={model} onChange={onModelChange} disabled={busy} state={modelState} className={styles.modelSelect} />
        <Button type="submit" aria-label="发送" disabled={!canSend} variant="primary" className={styles.sendButton}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
        </Button>
      </div>
      {modelState && <span id={hint} className={styles.muted}>{modelState}</span>}
    </div>
  </form>
}
