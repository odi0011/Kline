/* 取色器——按 uiarc ColorPicker 的交互自实现（官方源码为 Pro 专属）。
   圆形色块触发器，点开玻璃弹层：预设色板 + 系统取色 + HEX 输入。 */
import { useEffect, useId, useRef, useState } from "react"
import styles from "./color-picker.module.css"

export interface ColorPickerProps {
  value: string
  onChange: (hex: string) => void
  presets?: string[]
  label?: string
  size?: number
}

export function ColorPicker({ value, onChange, presets = [], label, size = 26 }: ColorPickerProps) {
  const [open, setOpen] = useState(false)
  const [hex, setHex] = useState(value)
  const rootRef = useRef<HTMLDivElement>(null)
  const popId = useId()

  useEffect(() => { setHex(value) }, [value])

  useEffect(() => {
    if (!open) return
    const onDoc = (e: PointerEvent) => { if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false) }
    document.addEventListener("pointerdown", onDoc)
    document.addEventListener("keydown", onKey)
    return () => { document.removeEventListener("pointerdown", onDoc); document.removeEventListener("keydown", onKey) }
  }, [open])

  const commit = (v: string) => {
    const t = v.trim()
    if (/^#[0-9a-fA-F]{6}$/.test(t) || /^#[0-9a-fA-F]{3}$/.test(t)) onChange(t)
  }

  return (
    <div ref={rootRef} className={styles.wrap}>
      {label && <span className={styles.label}>{label}</span>}
      <button type="button" className={styles.trigger} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className={styles.swatch} style={{ background: value, width: size, height: size }} aria-hidden />
        <span className={styles.hex}>{value}</span>
      </button>
      {open && (
        <div className={styles.pop} id={popId} role="dialog" aria-label={label || "颜色选择"}>
          {presets.length > 0 && (
            <div className={styles.presets}>
              {presets.map((p) => (
                <button key={p} type="button" className={styles.preset} style={{ background: p }}
                  data-on={p.toLowerCase() === value.toLowerCase() || undefined}
                  aria-label={p} onClick={() => { onChange(p); setOpen(false) }} />
              ))}
            </div>
          )}
          <div className={styles.row}>
            <label className={styles.native} style={{ background: value }}>
              <input type="color" value={/^#[0-9a-fA-F]{6}$/.test(value) ? value : "#f0b90b"}
                onChange={(e) => onChange(e.target.value)} />
            </label>
            <input className={styles.hexInput} value={hex} onChange={(e) => { setHex(e.target.value); commit(e.target.value) }}
              placeholder="#f0b90b" spellCheck={false} maxLength={7} />
          </div>
        </div>
      )}
    </div>
  )
}

export default ColorPicker
