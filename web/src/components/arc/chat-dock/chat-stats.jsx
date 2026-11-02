/* 输入框上方的实时性能条：TPS / 首字延迟 / 缓存命中 / 上下文占用。

   形状是「扣在输入框上的一枚标签」：上圆角与输入框一致，下沿去掉圆角
   直接和输入框顶边贴住，看起来像从输入框里拉出来的一条。
   数据全部来自后端真实用量，没有测到的项显示 "—" 而不是 0。 */
import { memo } from "react"
import styles from "./chat-stats.module.css"

/** 大数字用紧凑写法：12800 → 12.8k */
const compact = (value) => {
  const n = Number(value)
  if (!Number.isFinite(n)) return "—"
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10000) return `${(n / 1000).toFixed(1)}k`
  return String(Math.round(n))
}

const ms = (value) => {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return "—"
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`
}

const pct = (value) => {
  const n = Number(value)
  if (!Number.isFinite(n)) return "—"
  return `${n.toFixed(n >= 10 ? 0 : 1)}%`
}

/** 上下文占用：用量 / 窗口，外加一条同宽的比例条。
    超过 80% 转警示色——那是"该清对话了"的真实信号。 */
const ratio = (used, max) => {
  const u = Number(used)
  const m = Number(max)
  if (!Number.isFinite(u) || !Number.isFinite(m) || m <= 0) return null
  return Math.min(1, Math.max(0, u / m))
}

const Item = ({ label, value, tone }) => (
  <span className={styles.item} data-tone={tone || undefined}>
    <span className={styles.label}>{label}</span>
    <b className={styles.value}>{value}</b>
  </span>
)

const ChatStats = memo(function ChatStats({ stats, busy }) {
  if (!stats) return null
  const { tps, ttftMs, cacheHit, ctxUsed, ctxMax } = stats
  const used = ratio(ctxUsed, ctxMax)
  // 全都没有数据时整条不显示：一条全是 "—" 的栏占地方还没意义
  const blank = tps == null && ttftMs == null && cacheHit == null && used == null
  if (blank && !busy) return null

  return (
    <div className={styles.bar} data-busy={busy || undefined} role="status"
      aria-label="对话性能指标">
      {/* 回复进行中还没有真实 token 数：TPS 留空，别用帧数编一个低一个数量级的错值 */}
      <Item label="TPS" value={tps == null ? "—" : tps.toFixed(tps >= 100 ? 0 : 1)} />
      <Item label="首字" value={ms(ttftMs)} />
      <Item label="缓存" value={pct(cacheHit)} tone={cacheHit >= 50 ? "good" : undefined} />
      <span className={styles.item}>
        <span className={styles.label}>上下文</span>
        <b className={styles.value}>
          {used == null ? "—" : `${Math.round(used * 100)}%`}
        </b>
        {used != null && (
          <span className={styles.track} data-warn={used >= 0.8 || undefined}>
            <i style={{ width: `${Math.max(2, used * 100)}%` }} />
          </span>
        )}
      </span>
      {busy && <span className={styles.pulse} aria-hidden />}
    </div>
  )
})

export default ChatStats
