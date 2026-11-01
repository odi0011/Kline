/* 列表「回到最新」按钮：新闻 / 决策流水 / 仓位历史共用同一套材质、位置与内容。
   玻璃底 + 白描边，绝对定位贴在滚动区底部居中，由父容器 position:relative 决定落点。
   三处必须完全一致——不要按列表类型加图标或改文案。 */
import { LiquidGlass } from "@/components/ui/liquid-glass"
import styles from "./jump-latest.module.css"

export default function JumpLatest({ onClick, label = "最新", className = "" }) {
  return (
    <div className={`${styles.wrap} ${className}`.trim()}>
      <LiquidGlass radius={999} scale={22} aberration={0} frost={2} tint={0.14} className={styles.glass}>
        <button type="button" className={styles.btn} onClick={onClick}>{label}</button>
      </LiquidGlass>
    </div>
  )
}
