/* 单行输入框：文字与附件 chip 都在同一行，发送键内嵌在右侧。
   粘贴 / 拖入 / 选文件都会转成行内附件，只在这里维护，不向上冒泡重渲染整个聊天窗。 */
import { useCallback, useEffect, useRef, useState } from "react"
import { ArrowUp, FileText, Image as ImageIcon, Loader2, Paperclip, Square, X } from "lucide-react"
import styles from "./chat-composer.module.css"

const MAX_ATTACHMENTS = 4
const MAX_IMAGE_BYTES = 2_000_000
const MAX_TEXT_BYTES = 200_000
const IMAGE_MIMES = ["image/png", "image/jpeg", "image/webp", "image/gif"]

const readAsDataURL = (file) => new Promise((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve(String(reader.result || ""))
  reader.onerror = () => reject(reader.error || new Error("读取失败"))
  reader.readAsDataURL(file)
})

/** 文件 → 附件。图片保留 base64 走多模态；文本文件内联成文本。 */
async function toAttachment(file) {
  const name = file.name || "粘贴的内容"
  if (file.type.startsWith("image/")) {
    if (!IMAGE_MIMES.includes(file.type)) throw new Error(`${name}：仅支持 PNG / JPEG / WebP / GIF`)
    if (file.size > MAX_IMAGE_BYTES) throw new Error(`${name}：图片不能超过 2MB`)
    const data = await readAsDataURL(file)
    return { kind: "image", name, mime: file.type, size: file.size, data }
  }
  if (file.type.startsWith("text/") || /\.(txt|log|md|csv|json|ya?ml)$/i.test(name)) {
    if (file.size > MAX_TEXT_BYTES) throw new Error(`${name}：文本文件不能超过 200KB`)
    const data = await readAsDataURL(file)
    return { kind: "text", name, mime: file.type || "text/plain", size: file.size, data }
  }
  throw new Error(`${name}：暂不支持这种文件类型`)
}

const fmtSize = (bytes) => {
  if (!Number.isFinite(bytes)) return ""
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)}KB`
  return `${bytes}B`
}

export default function ChatComposer({ onSend, busy, disabled, onCancel, stacked = false }) {
  const [text, setText] = useState("")
  const [files, setFiles] = useState([])
  const [sending, setSending] = useState(false)
  const [error, setError] = useState("")
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef(null)
  const fileRef = useRef(null)
  const rowRef = useRef(null)

  const addFiles = useCallback(async (list) => {
    const incoming = Array.from(list || [])
    if (!incoming.length) return
    setError("")
    const added = []
    for (const file of incoming) {
      if (files.length + added.length >= MAX_ATTACHMENTS) {
        setError(`附件最多 ${MAX_ATTACHMENTS} 个`)
        break
      }
      try {
        added.push(await toAttachment(file))
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "附件读取失败")
      }
    }
    if (added.length) setFiles((current) => [...current, ...added].slice(0, MAX_ATTACHMENTS))
  }, [files.length])

  // 全局粘贴：焦点在输入框里也能识别图片/文件
  useEffect(() => {
    const onPaste = (event) => {
      const items = Array.from(event.clipboardData?.items || [])
      const picked = items
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter(Boolean)
      if (!picked.length) return
      event.preventDefault()
      addFiles(picked)
    }
    document.addEventListener("paste", onPaste)
    return () => document.removeEventListener("paste", onPaste)
  }, [addFiles])

  const removeAt = (index) => setFiles((current) => current.filter((_, i) => i !== index))

  const submit = async () => {
    const prompt = text.trim()
    if ((!prompt && !files.length) || sending || disabled) return
    setSending(true)
    setError("")
    try {
      await onSend?.(prompt, files)
      setText("")
      setFiles([])
      inputRef.current?.focus()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "发送失败")
    } finally {
      setSending(false)
    }
  }

  const onKeyDown = (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      submit()
    }
    if (event.key === "Backspace" && !text && files.length) {
      event.preventDefault()
      removeAt(files.length - 1)
    }
  }

  const canSend = (text.trim() || files.length) && !sending && !disabled

  return (
    <div className={styles.wrap}>
      {error && <p className={styles.error} role="alert">{error}</p>}
      <div ref={rowRef} className={styles.row} data-dragging={dragging || undefined}
        data-stacked={stacked || undefined}
        onDragOver={(event) => { event.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          addFiles(event.dataTransfer?.files)
        }}>
        <button type="button" className={styles.addBtn} aria-label="添加附件"
          title="添加附件（也可直接粘贴或拖入）" disabled={disabled}
          onClick={() => fileRef.current?.click()}>
          <Paperclip size={16} />
        </button>
        <input ref={fileRef} type="file" multiple hidden
          accept={`${IMAGE_MIMES.join(",")},text/plain,.txt,.log,.md,.csv,.json,.yaml,.yml`}
          onChange={(event) => { addFiles(event.target.files); event.target.value = "" }} />

        <div className={styles.field}>
          {files.map((file, index) => (
            <span key={`${file.name}-${index}`} className={styles.chip} data-kind={file.kind}
              title={`${file.name} ${fmtSize(file.size)}`}>
              {file.kind === "image"
                ? <ImageIcon size={12} aria-hidden />
                : <FileText size={12} aria-hidden />}
              <span className={styles.chipName}>{file.name}</span>
              <button type="button" className={styles.chipX} aria-label={`移除 ${file.name}`}
                onClick={() => removeAt(index)}><X size={10} strokeWidth={3} /></button>
            </span>
          ))}
          <textarea ref={inputRef} className={styles.input} rows={1} value={text}
            placeholder={files.length ? "" : "向 ODDD 提问…"}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={onKeyDown}
            maxLength={6000} aria-label="输入消息" />
        </div>

        {busy
          ? <button type="button" className={styles.stopBtn} aria-label="停止回复" title="停止回复"
              onClick={() => onCancel?.()}><Square size={11} fill="currentColor" /></button>
          : <button type="button" className={styles.sendBtn} disabled={!canSend}
              aria-label="发送" title={disabled ? "未连接服务端" : "发送"} onClick={submit}>
              {sending ? <Loader2 size={15} className={styles.spin} /> : <ArrowUp size={15} strokeWidth={2.6} />}
            </button>}
      </div>
    </div>
  )
}
