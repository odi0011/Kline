/* 决策依据：币种宫格（单列，滚动）。
   面板占第二行 30%（约 240~360px），每张卡片占满整行宽度。

   指标按语义分三组，各用最合适的微图表（而不是所有指标都堆成色块）：

   动量 · 极值  RSI / %B / Z
     带安全带的区间滑块：5px 圆角细轨 + 中间常态区浅色块 + 8px 位置圆点。
     常态不干扰视线，突破临界才变色（RSI 30/70、%B 破轨、Z ±2σ）。

   趋势 · 动能  ADX / ROC
     ADX：0~50 单向阈值能量条，25 为强弱分水岭（<20 灰、20~25 青、≥25 亮蓝）。
     ROC：0 轴对称双向偏转条，从中轴向右（正）/向左（负）延伸。

   波动 · 振幅  BW / ATR / RV
     BW：挤压监测条——常态是低对比灰条；跌破 15 分位时轨道收窄 + 琥珀"挤压"胶囊。
     ATR / RV：紧凑双列文字行（3px 状态点 + 标签 + 数值），ATR 是绝对价格不能横向
               比例尺化，因此不做条形。

   所有数值来自 snapshot.analysis.symbols，前端只做映射与着色。 */
import { useEffect, useMemo, useRef } from "react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import CoinAvatar from "@/components/ui/coin-avatar"
import { Progress } from "@/components/arc/progress/progress"
import styles from "./strategy-panel.module.css"

const SYM = (s) => String(s || "").replace(/usdt$/i, "").toUpperCase()
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))

const REGIME = {
  trend: { text: "趋势", tone: "trend" },
  range: { text: "震荡", tone: "range" },
  mixed: { text: "过渡", tone: "mixed" },
  unknown: { text: "—", tone: "muted" },
}
const VOL = {
  high: { text: "高波", tone: "high" },
  normal: { text: "常波", tone: "normal" },
  low: { text: "低波", tone: "low" },
}

/** 文字是否落在填充区（决定用深色还是浅色字，实现自动反色） */
const overFill = (textMidPct, fillPct) => textMidPct <= fillPct

/** 条内文字层：左侧「标签 数值」、右侧「状态判定」。
 *  两段文字各自按所在位置的填充覆盖率决定前景色 —— 填充到哪儿，
 *  哪儿的字就翻成白字，未填充区保持深灰，不需要额外状态维护。 */
function BarText({ label, value, status, pos, tone }) {
  // 粗略估计文字所占区间中点：左段约占 0..38%，右段约占 62..100%
  const leftLight = overFill(19, pos)
  const rightLight = overFill(81, pos)
  return (
    <span className={styles.barText}>
      <span className={styles.barLeft} data-light={leftLight || undefined}>
        <u>{label}</u>
        <b>{value}</b>
      </span>
      <span className={styles.barRight} data-light={rightLight || undefined} data-tone={tone || undefined}>
        {status}
      </span>
    </span>
  )
}

/** 带安全带的区间滑块：官方 Progress 填充条 + 安全带浅色窗口 + 内嵌文字。
 *  不画圆点——文字本身就标出了当前值所在比例。 */
function RangeSlider({ v, min, max, safe, label, value, status, tone, metricKey }) {
  const pos = clamp(((v - min) / (max - min)) * 100, 0, 100)
  const s0 = clamp(((safe[0] - min) / (max - min)) * 100, 0, 100)
  const s1 = clamp(((safe[1] - min) / (max - min)) * 100, 0, 100)
  return (
    <span className={styles.slider} data-tone={tone} data-metric={metricKey}>
      <Progress value={pos} max={100} className={styles.arcBar} />
      <span className={styles.sliderSafe} style={{ left: `${s0}%`, width: `${s1 - s0}%` }} />
      <BarText label={label} value={value} status={status} pos={pos} tone={tone} />
    </span>
  )
}

/** 单向阈值能量条：官方 Progress + 阈值刻线 + 内嵌文字 */
function Gauge({ v, min, max, threshold, label, value, status, metricKey }) {
  const pos = clamp(((v - min) / (max - min)) * 100, 0, 100)
  const tpos = clamp(((threshold - min) / (max - min)) * 100, 0, 100)
  const tone = v >= threshold ? "hot" : v >= threshold - 5 ? "warm" : "cool"
  return (
    <span className={styles.gauge} data-tone={tone} data-metric={metricKey}>
      <Progress value={pos} max={100} className={styles.arcBar} />
      <span className={styles.gaugeTick} style={{ left: `${tpos}%` }} />
      <BarText label={label} value={value} status={status} pos={pos} tone={tone} />
    </span>
  )
}

/** 0 轴对称双向偏转条：中轴 + 官方 Progress（值转成半幅宽度）+ 内嵌文字。
 *  反色按「离中轴的距离」判断：正值看右半幅覆盖率，负值看左半幅。 */
function BiBar({ v, scale, up, down, label, value, status, metricKey }) {
  const half = clamp(Math.abs(v) / scale, 0, 1) * 50
  const positive = v >= 0
  const leftLight = overFill(19, positive ? 0 : half * 2)   // 负值才填充左半
  const rightLight = overFill(81, positive ? half * 2 : 0)
  return (
    <span className={styles.bibar} data-dir={positive ? "up" : "down"} data-metric={metricKey}
      style={{ "--bibar-color": positive ? up : down, "--arc-fill": positive ? up : down }}>
      <span className={styles.bibarHalf} data-side="l">
        {!positive && <Progress value={(half / 50) * 100} max={100} className={styles.arcBar} />}
      </span>
      <span className={styles.bibarAxis} />
      <span className={styles.bibarHalf} data-side="r">
        {positive && <Progress value={(half / 50) * 100} max={100} className={styles.arcBar} />}
      </span>
      <span className={styles.barText}>
        <span className={styles.barLeft} data-light={leftLight || undefined}>
          <u>{label}</u>
          <b>{value}</b>
        </span>
        <span className={styles.barRight} data-light={rightLight || undefined} data-tone={positive ? "up" : "down"}>
          {status}
        </span>
      </span>
    </span>
  )
}

/** 布林挤压监测：官方 Progress + 内嵌文字；挤压时整条收窄并换琥珀色 */
function SqueezeBar({ v, label, value, status, metricKey }) {
  const squeezed = v != null && v < 15
  const pos = clamp(v ?? 0, 0, 100)
  return (
    <span className={styles.squeeze} data-squeeze={squeezed || undefined} data-metric={metricKey}>
      <Progress value={pos} max={100} className={styles.arcBar} />
      <BarText label={label} value={value} status={status} pos={squeezed ? 0 : pos} tone={squeezed ? "warn" : "normal"} />
    </span>
  )
}

export default function StrategyPanel({ analysis, symbols, chartColors, onMetricPulse }) {
  const syms = analysis?.symbols || {}
  const params = analysis?.params
  const plans = analysis?.positionPlans || []
  const up = chartColors?.up || "#16a34a"
  const down = chartColors?.down || "#dc2626"

  // 严格跟随左侧 K 线展示的币种（App 传入的 symbols 已按"持仓优先"排序）
  // 左侧 K 线有几个币，这里就永远有几张卡（含指标尚未就绪的）。
  // 不能因为后端 transient 缺数据就把卡片藏起来——否则会「突然出现/消失」。
  // 数据缺失时卡片内显示占位（--），结构保持稳定。
  const cards = useMemo(() => {
    const list = Array.isArray(symbols) ? symbols : []
    return list.map((s) => {
      const k = String(s).toLowerCase()
      return [k, syms[k] || null]
    })
  }, [symbols, syms])

  const planBySym = useMemo(() => {
    const m = new Map()
    for (const p of plans) if (p?.symbol) m.set(String(p.symbol).toLowerCase(), p)
    return m
  }, [plans])

  const previousMetricsRef = useRef(new Map())
  const lastPulseRef = useRef(new Map())

  // 指标只在发生足够明显的增量时发射一次数据脉冲。进度条先收缩回弹，
  // 随后由全局细线层从当前填充位置飞向右侧计算网络的接收节点。
  useEffect(() => {
    if (!onMetricPulse) return
    const now = Date.now()
    const reduced = typeof window !== "undefined"
      && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
    const panelRect = document.querySelector(".strategy-slot")?.getBoundingClientRect()
    for (const [sym, st] of cards) {
      const metrics = [
        { id: "rsi", label: "RSI", value: st?.rsi, threshold: 2, ratio: (v) => clamp(v, 0, 100) / 100 },
        { id: "pctB", label: "%B", value: st?.boll?.pctB, threshold: 0.04, ratio: (v) => clamp((v + 0.2) / 1.4, 0, 1) },
        { id: "z", label: "Z", value: st?.z, threshold: 0.12, ratio: (v) => clamp((v + 3) / 6, 0, 1) },
        { id: "adx", label: "ADX", value: st?.adx, threshold: 1.2, ratio: (v) => clamp(v / 50, 0, 1) },
        { id: "roc", label: "ROC", value: st?.roc, threshold: 0.0008, ratio: (v) => 0.5 + clamp(v / 0.015, -1, 1) * 0.5 },
        { id: "bw", label: "BW", value: st?.bwPct, threshold: 3, ratio: (v) => clamp(v / 100, 0, 1) },
      ]
      for (const metric of metrics) {
        const key = `${sym}:${metric.id}`
        if (metric.value == null) {
          previousMetricsRef.current.delete(key)
          continue
        }
        const current = Number(metric.value)
        if (!Number.isFinite(current)) {
          previousMetricsRef.current.delete(key)
          continue
        }
        if (!previousMetricsRef.current.has(key)) {
          previousMetricsRef.current.set(key, current)
          continue
        }
        const previous = previousMetricsRef.current.get(key)
        previousMetricsRef.current.set(key, current)
        if (!Number.isFinite(previous) || Math.abs(current - previous) < metric.threshold) continue
        const last = lastPulseRef.current.get(key) || 0
        if (now - last < 700) continue
        lastPulseRef.current.set(key, now)

        const bar = document.querySelector(`[data-metric="${key}"]`)
        if (!bar) continue
        const rect = bar.getBoundingClientRect()
        if (rect.width <= 0 || !panelRect || rect.bottom <= panelRect.top || rect.top >= panelRect.bottom) continue
        const fill = bar.querySelector('[class*="fill"]')
        const fillColor = fill ? getComputedStyle(fill).backgroundColor : ""
        const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim()
        const safeFillColor = fillColor && fillColor !== "rgba(0, 0, 0, 0)"
          ? fillColor
          : (accent || "rgb(100, 116, 139)")
        if (!reduced) {
          bar.animate([
            { transform: "scale(1, 1)" },
            { transform: "scale(0.965, 0.76)", offset: 0.2 },
            { transform: "scale(1.018, 1.08)", offset: 0.48 },
            { transform: "scale(0.994, 0.96)", offset: 0.72 },
            { transform: "scale(1, 1)" },
          ], {
            duration: 430,
            easing: "cubic-bezier(.22,1.35,.36,1)",
          })
        }
        onMetricPulse({
          id: `${key}-${now}`,
          symbol: sym,
          metric: metric.id,
          label: metric.label,
          value: current,
          delta: current - previous,
          color: safeFillColor,
          source: {
            x: rect.left + rect.width * metric.ratio(current),
            y: rect.top + rect.height / 2,
          },
          delay: reduced ? 0 : 115,
          duration: reduced ? 300 : 1180 + Math.random() * 620,
          ts: now,
        })
      }
    }
  }, [analysis, cards, onMetricPulse])

  /** 与后端 adaptive_sl_tp 同一口径 */
  const band = (st) => {
    const price = Number(st?.price) || 0
    const atr = Number(st?.atr) || 0
    const slMin = (params?.slMinPct ?? 0.4) / 100
    const slMax = (params?.slMaxPct ?? 2.0) / 100
    const tpMin = (params?.tpMinPct ?? 0.18) / 100
    const tpMax = (params?.tpMaxPct ?? 3.0) / 100
    let sl = slMin, tp = tpMin
    if (price > 0 && atr > 0) {
      sl = Math.min(slMax, Math.max(slMin, (atr * (params?.atrSlMult ?? 1.5)) / price))
      tp = Math.min(tpMax, Math.max(tpMin, (atr * (params?.atrTpMult ?? 2.5)) / price))
    }
    return { sl: sl * 100, tp: tp * 100 }
  }

  return (
    <section className={styles.wrap} aria-label="决策依据">
      {cards.length === 0 ? (
        <div className={styles.empty}>等待指标计算（需至少 30 根K线）…</div>
      ) : (
        <ScrollArea orientation="vertical" fade={10} scrollbars="auto"
          className={styles.gridScroll} label="币种决策宫格">
          <div className={styles.grid}>
            {cards.map(([sym, st]) => {
              const plan = planBySym.get(String(sym).toLowerCase())
              const side = plan ? (plan.side === "BUY" ? "多" : "空") : null
              const b = band(st)
              const regime = REGIME[st?.regime] || REGIME.unknown
              const vol = VOL[st?.volState]

              // ---- RSI ----
              const rsi = st?.rsi
              const rsiTone = rsi == null ? null : rsi >= 70 ? "extreme" : rsi <= 30 ? "oversold"
                : (rsi >= 62 || rsi <= 38) ? "warn" : "normal"
              const rsiStatus = rsi == null ? "" : rsi >= 70 ? "超买" : rsi <= 30 ? "超卖"
                : rsi >= 62 ? "警戒" : rsi <= 38 ? "警戒" : "常态"

              // ---- %B ----
              const pctB = st?.boll?.pctB
              const bTone = pctB == null ? null : (pctB > 1 || pctB < 0) ? "extreme" : "normal"
              const bStatus = pctB == null ? "" : pctB > 1 ? "破上轨" : pctB < 0 ? "破下轨" : "常态"

              // ---- Z ----
              const z = st?.z
              const az = z == null ? 0 : Math.abs(z)
              const zTone = z == null ? null : az >= 2 ? "extreme" : az >= 1.5 ? "warn" : "normal"
              const zStatus = z == null ? "" : az >= 2 ? "极值" : az >= 1.5 ? "偏离" : "常态"

              // ---- ADX ----
              const adx = st?.adx
              const adxTone = adx == null ? null : adx >= 25 ? "hot" : adx >= 20 ? "warm" : "cool"
              const adxStatus = adx == null ? "" : adx >= 25 ? "趋势" : adx >= 20 ? "初生" : "无势"

              // ---- ROC ----
              const roc = st?.roc
              const rocStatus = roc == null ? "" : Math.abs(roc) < 0.0005 ? "持平"
                : roc > 0 ? "多头" : "空头"
              const rocTone = roc == null ? null : roc > 0 ? "up" : roc < 0 ? "down" : "normal"

              // ---- BW 挤压 ----
              const bw = st?.bwPct
              const squeezed = bw != null && bw < 15
              const bwStatus = bw == null ? "" : squeezed ? "挤压" : bw > 80 ? "扩张" : "常波"
              const bwTone = bw == null ? null : squeezed ? "warn" : bw > 80 ? "warm" : "normal"

              // ---- ATR / RV 状态点 ----
              const atrP = st?.atrPct
              const rv = st?.realizedVol
              const atrHot = atrP != null && atrP >= 85
              const rvHot = rv != null && rv >= 0.0015

              // ---- 是否需要提醒（卡片边框呼吸灯）----
              // 「有问题 / 数据异常 / 触发判定条件」任一成立即亮起：
              //   · 有持仓（正在场内，需要盯）
              //   · 指标尚未就绪（数据异常，卡片显示 --）
              //   · RSI 超买超卖 / %B 破轨 / |Z|≥2 极值
              //   · 布林挤压（即将变盘）/ ATR 分位≥85（极端波动）
              const alerts = []
              if (!st) alerts.push("指标未就绪")
              if (rsi != null && (rsi >= 70 || rsi <= 30)) alerts.push(rsi >= 70 ? "RSI超买" : "RSI超卖")
              if (pctB != null && (pctB > 1 || pctB < 0)) alerts.push("布林破轨")
              if (z != null && Math.abs(z) >= 2) alerts.push("Z极值")
              if (squeezed) alerts.push("布林挤压")
              if (atrHot) alerts.push("极端波动")
              const alert = alerts.length > 0

              return (
                <div key={sym} className={styles.cell}
                  data-held={side ? "y" : undefined}
                  data-alert={alert || undefined}
                  title={alert ? `需关注：${alerts.join(" · ")}` : undefined}>
                  {/* 头部：图标 + 币种 + 方向（左）／状态标签（右） */}
                  <div className={styles.cellHead}>
                    <CoinAvatar symbol={sym} size={16} />
                    <span className={styles.sym}>
                      {SYM(sym)}
                      {side && <i data-side={side === "多" ? "long" : "short"}>{side}</i>}
                    </span>
                    <span className={styles.tags}>
                      {!st && <em data-tone="muted">计算中</em>}
                      {st?.regime && <em data-tone={regime.tone}>{regime.text}</em>}
                      {vol && <em data-tone={vol.tone}>{vol.text}</em>}
                      {st?.squeeze && <em data-tone="squeeze">挤压</em>}
                    </span>
                  </div>

                  {/* ---------- 动量 · 极值 ---------- */}
                  <div className={styles.groupLabel}>动量 · 极值</div>
                  {rsi == null ? <div className={styles.mNa}>RSI --</div> : (
                    <RangeSlider v={rsi} min={0} max={100} safe={[30, 70]} tone={rsiTone}
                      label="RSI" value={String(Math.round(rsi))} status={rsiStatus}
                      metricKey={`${sym}:rsi`} />
                  )}
                  {pctB == null ? <div className={styles.mNa}>%B --</div> : (
                    <RangeSlider v={pctB} min={-0.2} max={1.2} safe={[0, 1]} tone={bTone}
                      label="%B" value={pctB.toFixed(2)} status={bStatus}
                      metricKey={`${sym}:pctB`} />
                  )}
                  {z == null ? <div className={styles.mNa}>Z值 --</div> : (
                    <RangeSlider v={z} min={-3} max={3} safe={[-2, 2]} tone={zTone}
                      label="Z值" value={z.toFixed(1)} status={zStatus}
                      metricKey={`${sym}:z`} />
                  )}

                  {/* ---------- 趋势 · 动能 ---------- */}
                  <div className={styles.groupLabel}>趋势 · 动能</div>
                  {adx == null ? <div className={styles.mNa}>ADX --</div> : (
                    <Gauge v={adx} min={0} max={50} threshold={25}
                      label="ADX" value={String(Math.round(adx))} status={adxStatus}
                      metricKey={`${sym}:adx`} />
                  )}
                  {roc == null ? <div className={styles.mNa}>ROC --</div> : (
                    <BiBar v={roc} scale={0.015} up={up} down={down}
                      label="ROC" value={(roc * 100).toFixed(2)} status={rocStatus}
                      metricKey={`${sym}:roc`} />
                  )}

                  {/* ---------- 波动 · 振幅 ---------- */}
                  <div className={styles.groupLabel}>波动 · 振幅</div>
                  {bw == null ? <div className={styles.mNa}>BW --</div> : (
                    <SqueezeBar v={bw} label="BW" value={String(Math.round(bw))} status={bwStatus}
                      metricKey={`${sym}:bw`} />
                  )}
                  <div className={styles.dual}>
                    <span className={styles.dualItem} data-hot={atrHot || undefined}
                      title="ATR 绝对值（价格单位）+ 历史分位">
                      <i />
                      <u>ATR</u>
                      <b>{st?.atr == null ? "--" : st.atr >= 100 ? st.atr.toFixed(1) : st.atr >= 1 ? st.atr.toFixed(2) : st.atr.toPrecision(3)}</b>
                      <em>{atrP == null ? "" : `${Math.round(atrP)}分位`}</em>
                    </span>
                    <span className={styles.dualItem} data-hot={rvHot || undefined}
                      title="实测波动率（收益率标准差）">
                      <i />
                      <u>RV</u>
                      <b>{rv == null ? "--" : (rv * 100).toFixed(2) + "%"}</b>
                    </span>
                  </div>

                  {/* ---------- 风控区间 ---------- */}
                  <div className={styles.band}>
                    {st && st.price > 0 && st.atr > 0 ? (
                      <>
                        <span data-tone="sl" title="止损距离（1.5×ATR，夹紧 0.4%~2%）">-{b.sl.toFixed(2)}%</span>
                        <span data-tone="tp" title="止盈距离（2.5×ATR，夹紧 0.18%~3%）">+{b.tp.toFixed(2)}%</span>
                        <span data-tone="rr" title="盈亏比 = 止盈 ÷ 止损">{(b.sl > 0 ? b.tp / b.sl : 0).toFixed(2)}</span>
                      </>
                    ) : (
                      <span className={styles.bandNa}>
                        {st ? "ATR 未就绪，暂无风控区间" : "指标计算中…"}
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </ScrollArea>
      )}
    </section>
  )
}
