/* Morph 选择器——按 uiarc Morph select 的交互自实现（官方源码为 Pro 专属）。
   分段式选项，高亮圆片在选项间滑动（layoutId morph），用于少量互斥预设的切换。 */
import { useId, useState } from "react"
import { motion } from "motion/react"
import styles from "./morph-select.module.css"

export interface MorphSelectProps {
  options: { id: string; label: string }[]
  value: string
  onChange: (id: string) => void
  className?: string
}

const spring = { type: "spring", stiffness: 480, damping: 38, mass: 0.8 }

export function MorphSelect({ options, value, onChange, className }: MorphSelectProps) {
  const [hover, setHover] = useState(null)
  const groupId = useId()
  return (
    <div className={[styles.seg, className].filter(Boolean).join(" ")}
      style={{ gridTemplateColumns: `repeat(${options.length}, 1fr)` }}
      role="radiogroup">
      {options.map((o) => {
        const on = value === o.id
        return (
          <button
            key={o.id}
            type="button"
            role="radio"
            aria-checked={on}
            className={styles.opt}
            data-on={on || undefined}
            onMouseEnter={() => setHover(o.id)}
            onMouseLeave={() => setHover((h) => (h === o.id ? null : h))}
            onClick={() => onChange(o.id)}
          >
            {(hover === o.id || on) && (
              <motion.div
                layoutId={`morph-${groupId}`}
                className={styles.pill}
                data-hover={hover === o.id && !on ? "" : undefined}
                transition={spring}
              />
            )}
            <span style={{ position: "relative" }}>{o.label}</span>
          </button>
        )
      })}
    </div>
  )
}

export default MorphSelect
