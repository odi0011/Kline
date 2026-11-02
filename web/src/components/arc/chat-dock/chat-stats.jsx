/* 输入框上方的实时性能条：速度 / 缓存命中 / 本轮费用 / 上下文占用。

   数据全部来自后端真实计量（usage + 流式计时），没有测到的项显示 "—"
   而不是 0——0 会被误读成"测出来是零"。
   费用按服务商官方价目表实时计算，含峰谷时段（详见 ai/pricing.py）。 */
import { memo } from "react"
import { Zap, Database, Wallet, CircleDot } from "lucide-react"
import styles from "./chat-stats.module.css"

const pct = (value) => {
  const n = Number(value)
  if (!Number.isFinite(n)) return "—"
  return `${n.toFixed(n >= 10 ? 0 : 1)}%`
}

/** 速度：整数 tok/s，过大时保留一位小数 */
const speed = (value) => {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return "—"
  return n >= 100 ? String(Math.round(n)) : n.toFixed(1)
}

/** 费用：小额保留 4 位，避免 ¥0.00 看着像免费 */
const money = (value) => {
  const n = Number(value)
  if (!Number.isFinite(n)) return "—"
  if (n === 0) return "¥0"
  if (n < 0.01) return `¥${n.toFixed(4)}`
  if (n < 1) return `¥${n.toFixed(3)}`
  return `¥${n.toFixed(2)}`
}

const ratio = (used, max) => {
  const u = Number(used)
  const m = Number(max)
  if (!Number.isFinite(u) || !Number.isFinite(m) || m <= 0) return null
  return Math.min(1, Math.max(0, u / m))
}

const Item = ({ icon, label, value, tone, title }) => (
  <span className={styles.item} data-tone={tone || undefined} title={title}>
    <span className={styles.icon} aria-hidden>{icon}</span>
    {label ? <span className={styles.label}>{label}</span> : null}
    <b className={styles.value}>{value}</b>
  </span>
)

const ChatStats = memo(function ChatStats({ stats, busy }) {
  if (!stats) return null
  const { tps, cacheHit, ctxUsed, ctxMax, cost } = stats
  const used = ratio(ctxUsed, ctxMax)
  const blank = tps == null && cacheHit == null && used == null && cost == null
  if (blank && !busy) return null

  return (
    <div className={styles.bar} data-busy={busy || undefined} role="status"
      aria-label="对话性能指标">
      {/* 速度：服务商回报的 completion_tokens ÷ 解码窗口 */}
      <Item icon={<Zap size={11} strokeWidth={2.2} />}
        value={speed(tps)} label="tok/s" />

      {/* 缓存命中率：cache_hit ÷ prompt_tokens */}
      <Item icon={<Database size={11} strokeWidth={2.2} />}
        value={pct(cacheHit)} label="缓存"
        tone={cacheHit >= 50 ? "good" : undefined}
        title="提示词缓存命中率" />

      {/* 本轮费用：按官方价目表逐项计算，含峰谷 */}
      <Item icon={<Wallet size={11} strokeWidth={2.2} />}
        value={money(cost)}
        title={stats.costNote || "按官方价目表计算"}/>

      {/* 上下文占用 */}
      <span className={styles.item} title={`${ctxUsed ?? 0} / ${ctxMax ?? 0} tokens`}>
        <span className={styles.icon} aria-hidden><CircleDot size={11} strokeWidth={2.2} /></span>
        <b className={styles.value}>{used == null ? "—" : `${Math.round(used * 100)}%`}</b>
        {used != null && (
          <span className={styles.track} data-warn={used >= 0.8 || undefined}>
            <i style={{ width: `${Math.max(3, used * 100)}%` }} />
          </span>
        )}
      </span>
      {busy && <span className={styles.pulse} aria-hidden />}
    </div>
  )
})

export default ChatStats
