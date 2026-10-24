/* 玻璃输入框——按 uiarc Input 的交互自实现（官方源码为 Pro 专属）。
   毛玻璃底、聚焦时主题色描边，label / hint 可选。密码框直接用 type="password"。 */
import { forwardRef, useId } from "react"
import styles from "./input.module.css"

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string
  hint?: string
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, hint, className, id, ...rest }, ref,
) {
  const autoId = useId()
  const inputId = id || `arc-input-${autoId}`
  return (
    <div className={[styles.wrap, className].filter(Boolean).join(" ")}>
      {label && (
        <label className={styles.label} htmlFor={inputId}>{label}</label>
      )}
      <input ref={ref} id={inputId} className={styles.input} {...rest} />
      {hint && <div className={styles.hint}>{hint}</div>}
    </div>
  )
})

export default Input
