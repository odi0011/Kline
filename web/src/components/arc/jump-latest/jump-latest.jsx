/* 列表「回到最新」按钮：新闻 / 决策流水 / 仓位历史共用一套材质与位置。
   玻璃底 + 白描边，绝对定位贴在滚动区底部居中，由父容器 position:relative 决定落点。 */
import { ArrowDown } from "lucide-react"
import { LiquidGlass } from "@/components/ui/liquid-glass"
import styles from "./jump-latest.module.css"

export default function JumpLatest({ onClick, label = "最新", withIcon = false, className = "" }) {
  return (
    <div className={`${styles.wrap} ${className}`.trim()}>
      <LiquidGlass radius={999} scale={22} aberration={0} frost={2} tint={0.14} className={styles.glass}>
        <button type="button" className={styles.btn} onClick={onClick}>
          {withIcon && <ArrowDown size={11} strokeWidth={2.4} aria-hidden />}
          {label}
        </button>
      </LiquidGlass>
    </div>
  )
}
