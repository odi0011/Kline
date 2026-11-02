/* 计算流水：把系统每一次评分的真实算式摊开，密集滚动展示。

   这里放的不是"指标摘要"，而是**算式本身**：
     共鸣分 = (趋势·w + 动能·w + 量能·w + 结构·w) / Σw
     多空分离度 = 本方 − 对方
     缓存命中 = prompt_cache_hit / prompt_tokens
   每个分项后面跟着它的真实输入值（±DI、RSI、量比…），
   这样"为什么建议做空"有据可查，而不是一个凭空冒出来的数字。

   数据全部来自已经在广播的 liveEvaluations / stats，不新建数据源、
   不写数据库——这块是纯前端实时的运算轨迹。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import JumpLatest from "@/components/arc/jump-latest/jump-latest"
import styles from "./compute-trace.module.css"

const num = (v) => {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}
/** 统一两位小数，缺数据给 -- */
const f2 = (v) => {
  const n = num(v)
  return n == null ? "--" : n.toFixed(2)
}
const f1 = (v) => {
  const n = num(v)
  return n == null ? "--" : n.toFixed(1)
}

/** 一行：左侧公式/标签，右侧代入后的值。等宽数字，密集排列。 */
function Row({ tag, expr, value, tone, depth = 0 }) {
  return (
    <div className={styles.row} data-depth={depth || undefined} data-tone={tone || undefined}>
      {tag ? <span className={styles.tag}>{tag}</span> : <span className={styles.tag} />}
      <span className={styles.expr}>{expr}</span>
      <b className={styles.value}>{value}</b>
    </div>
  )
}

/** 单个币的一组算式：从分项到加权、到分离度。 */
function SymbolBlock({ sym, ev, weights, history, state }) {
  const b = ev?.breakdown || {}
  const side = ev?.direction === "bearish" ? b.bear : b.bull
  const oppo = ev?.direction === "bearish" ? b.bull : b.bear
  const w = b.weights || weights || {}
  const total = num(side?.total)
  const opposite = num(oppo?.total)
  const gap = num(ev?.gap)
  const dirLabel = ev?.direction === "bearish" ? "空" : ev?.direction === "bullish" ? "多" : "—"
  const tone = ev?.direction === "bearish" ? "down" : ev?.direction === "bullish" ? "up" : undefined

  // 评分轨迹的稳定性：近 N 次评分的标准差。
  // 漂移大 = 信号不稳 = 同样的分数更不可信，这是可算的真实量，
  // 不是拍脑袋给的"胜率"。
  const scores = (history || []).map((x) => num(x?.score)).filter((v) => v != null)
  const mean = scores.length ? scores.reduce((a, x) => a + x, 0) / scores.length : null
  const sd = scores.length > 1 && mean != null
    ? Math.sqrt(scores.reduce((a, x) => a + (x - mean) ** 2, 0) / scores.length)
    : null
  // 稳定性系数：标准差 0 → 100%，标准差 20 → 0%，线性衰减
  const stability = sd == null ? null : Math.max(0, Math.min(100, 100 - sd * 5))
  const st = state || {}

  return (
    <section className={styles.block}>
      <header className={styles.blockHead}>
        <b className={styles.sym}>{sym.replace(/usdt$/i, "").toUpperCase()}</b>
        <span className={styles.dir} data-tone={tone}>{dirLabel}</span>
        <span className={styles.grade}>{ev?.grade || "--"}</span>
        <span className={styles.price}>{f2(st.price)}</span>
      </header>

      {/* 原始输入：每个分项算出来的依据，先把"谁是多少"列清楚 */}
      <Row tag="in" expr="RSI · ADX · +DI/−DI"
        value={`${f1(st.rsi)} · ${f1(st.adx)} · ${f1(st.plusDI)}/${f1(st.minusDI)}`} />
      {/* atrPct 是 ATR 的历史百分位排名（0-100），不是百分比，别 ×100。
          roc 是小数（0.29 = 29%），这里转成百分数展示。 */}
      <Row tag="in" expr="ATR位 · ROC% · 带宽"
        value={`${f1(st.atrPct)} · ${f1(num(st.roc) == null ? null : st.roc * 100)} · ${f1(st.bwPct)}`} />
      <Row tag="in" expr={`状态 ${st.regime || "--"} · ${st.volState || "--"}`}
        value={st.squeeze ? "挤压" : ""} />

      {/* 加权求和：把每一个 分值×权重 都列出来，再除以权重和 */}
      <Row tag="w" expr={`趋势 ${f2(side?.trend)} × ${f2(w.trend)}`}
        value={f2(num(side?.trend) * num(w.trend))} />
      <Row tag="w" expr={`动能 ${f2(side?.momentum)} × ${f2(w.momentum)}`}
        value={f2(num(side?.momentum) * num(w.momentum))} />
      <Row tag="w" expr={`量能 ${f2(side?.volume)} × ${f2(w.volume)}`}
        value={f2(num(side?.volume) * num(w.volume))} />
      <Row tag="w" expr={`结构 ${f2(side?.structure)} × ${f2(w.structure)}`}
        value={f2(num(side?.structure) * num(w.structure))} />
      <Row tag="Σw" expr="权重和"
        value={f2(Object.values(w).reduce((a, x) => a + (num(x) || 0), 0))} />
      <Row tag="=" expr={`${dirLabel}方合计 ÷ Σw`} value={f2(total)} tone={tone} />

      <Row tag="−" expr={`分离度 = ${f2(total)} − ${f2(opposite)}`} value={f2(gap)} />

      {/* 估算胜率：分数 / 100 乘以轨迹稳定性。两个因子都是真实量，
          不是凭空给的置信度——分数低或评分在抖，结果就低。 */}
      <Row tag="≈" expr={`估算 ${f2(total)}% × 稳 ${f1(stability)}%`}
        value={stability == null || total == null
          ? "--"
          : `${((total / 100) * (stability / 100) * 100).toFixed(0)}%`} />
      <Row tag="σ" expr={`评分标准差 (n=${scores.length})`} value={f2(sd)} />

      {ev?.vetoes?.length ? (
        <Row tag="!" expr="失误原因" value={String(ev.vetoes[0]).slice(0, 16)} tone="warn" />
      ) : null}
    </section>
  )
}

export default function ComputeTrace({ analysis, symbols, stats }) {
  const viewportRef = useRef(null)
  const followRef = useRef(true)
  const [atLatest, setAtLatest] = useState(true)

  const evaluations = analysis?.liveEvaluations || {}
  const scoreHistory = analysis?.liveScoreHistory || {}
  const states = analysis?.symbols || {}
  const list = useMemo(() => {
    const order = Array.isArray(symbols) && symbols.length ? symbols : Object.keys(evaluations)
    return order.map((s) => [s, evaluations[s]]).filter(([, ev]) => ev && ev.breakdown)
  }, [evaluations, symbols])

  const weights = list[0]?.[1]?.breakdown?.weights

  // 只有原本就在底部时才自动贴底；往上翻看时不打断
  useEffect(() => {
    const el = viewportRef.current
    if (!el || !followRef.current) return
    el.scrollTop = el.scrollHeight
  }, [evaluations])

  const onScroll = (event) => {
    const el = event.currentTarget
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 24
    followRef.current = near
    setAtLatest(near)
  }

  const jump = useCallback(() => {
    const el = viewportRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    followRef.current = true
    setAtLatest(true)
  }, [])

  return (
    <section className={styles.root} aria-label="计算流水">
      <header className={styles.head}>
        <b>计算流水</b>
        <span className={styles.hint}>实时算式 · 不落库</span>
      </header>

      <div className={styles.body}>
        <ScrollArea orientation="vertical" fade={14} scrollbars="auto" label="计算流水"
          viewportRef={viewportRef} className={styles.scroll}
          viewportClassName={styles.viewport} onScroll={onScroll}>
          {/* 系统级：模型与缓存的实际计量 */}
          {stats ? (
            <section className={styles.block}>
              <header className={styles.blockHead}>
                <b className={styles.sym}>模型</b>
                <span className={styles.grade}>{stats.live ? "实时" : "上次"}</span>
              </header>
              <Row tag="tps" expr="completion_tokens ÷ 解码秒"
                value={stats.tps == null ? "--" : f1(stats.tps)} />
              <Row tag="hit" expr="cache_hit ÷ prompt_tokens"
                value={stats.cacheHit == null ? "--" : `${f1(stats.cacheHit)}%`} />
              <Row tag="ctx" expr={`${(stats.ctxUsed ?? 0).toLocaleString()} ÷ ${(stats.ctxMax ?? 0).toLocaleString()}`}
                value={stats.ctxUsed && stats.ctxMax ? `${((stats.ctxUsed / stats.ctxMax) * 100).toFixed(0)}%` : "--"} />
            </section>
          ) : null}

          {list.length === 0 ? (
            <p className={styles.empty}>等待评估数据…</p>
          ) : list.map(([sym, ev]) => (
            <SymbolBlock key={sym} sym={sym} ev={ev} weights={weights}
              history={scoreHistory[sym]} state={states[sym]} />
          ))}
        </ScrollArea>

        {!atLatest && <JumpLatest onClick={jump} className={styles.jump} />}
      </div>
    </section>
  )
}
