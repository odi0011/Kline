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
import { useMemo } from "react"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import CoinAvatar from "@/components/ui/coin-avatar"
import styles from "./strategy-panel.module.css"

const SYM = (s) => String(s || "").replace(/usdt$/i, "").toUpperCase()
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))
/** 数值 → 轨道百分比（0~100） */
const pct = (v, min, max) => clamp(((v - min) / (max - min)) * 100, 0, 100)

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

/** 带安全带的区间滑块（RSI / %B / Z） */
function RangeSlider({ v, min, max, safe, tone }) {
  const pos = pct(v, min, max)
  const s0 = pct(safe[0], min, max)
  const s1 = pct(safe[1], min, max)
  return (
    <span className={styles.slider}>
      <span className={styles.sliderTrack}>
        <span className={styles.sliderSafe} style={{ left: `${s0}%`, width: `${s1 - s0}%` }} />
      </span>
      <span className={styles.sliderDot} data-tone={tone} style={{ left: `${pos}%` }} />
    </span>
  )
}

/** 单向阈值能量条（ADX）：threshold 为分水岭 */
function Gauge({ v, min, max, threshold }) {
  const pos = pct(v, min, max)
  const tone = v >= threshold ? "hot" : v >= threshold - 5 ? "warm" : "cool"
  return (
    <span className={styles.gauge}>
      <span className={styles.gaugeFill} data-tone={tone} style={{ width: `${pos}%` }} />
      <span className={styles.gaugeTick} style={{ left: `${pct(threshold, min, max)}%` }} />
    </span>
  )
}

/** 0 轴对称双向偏转条（ROC）：正向右、负向左，颜色跟随涨跌配色 */
function BiBar({ v, scale, up, down }) {
  const len = clamp(Math.abs(v) / scale, 0, 1) * 50
  const positive = v >= 0
  return (
    <span className={styles.bibar}>
      <span className={styles.bibarAxis} />
      <span
        className={styles.bibarFill}
        style={{
          [positive ? "left" : "right"]: "50%",
          width: `${len}%`,
          background: positive ? up : down,
        }}
      />
    </span>
  )
}

/** 布林挤压监测条（BW 分位）：常态低对比灰条；挤压时轨道收窄 + 琥珀胶囊 */
function SqueezeBar({ v }) {
  const squeezed = v != null && v < 15
  return (
    <span className={styles.squeeze} data-squeeze={squeezed || undefined}>
      <span className={styles.squeezeTrack}>
        {v != null && <span className={styles.squeezeFill} style={{ width: `${clamp(v, 0, 100)}%` }} />}
      </span>
    </span>
  )
}

/** 单行：60px 标签+数值 / 自适应微图表 / 45px 状态判定 */
function Row({ label, value, status, tone, na, children }) {
  return (
    <div className={styles.mRow} data-na={na || undefined}>
      <span className={styles.mLabel}>
        <u>{label}</u>
        <b>{value}</b>
      </span>
      <span className={styles.mChart}>{children}</span>
      <span className={styles.mStatus} data-tone={tone || undefined}>{status}</span>
    </div>
  )
}

export default function StrategyPanel({ analysis, symbols, chartColors }) {
  const syms = analysis?.symbols || {}
  const params = analysis?.params
  const plans = analysis?.positionPlans || []
  const up = chartColors?.up || "#16a34a"
  const down = chartColors?.down || "#dc2626"

  // 严格跟随左侧 K 线展示的币种（App 传入的 symbols 已按"持仓优先"排序）
  const cards = useMemo(() => {
    const list = Array.isArray(symbols) ? symbols : []
    return list
      .map((s) => String(s).toLowerCase())
      .filter((s) => syms[s])
      .map((s) => [s, syms[s]])
  }, [symbols, syms])

  const planBySym = useMemo(() => {
    const m = new Map()
    for (const p of plans) if (p?.symbol) m.set(String(p.symbol).toLowerCase(), p)
    return m
  }, [plans])

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

              return (
                <div key={sym} className={styles.cell} data-held={side ? "y" : undefined}>
                  {/* 头部：图标 + 币种 + 方向（左）／状态标签（右） */}
                  <div className={styles.cellHead}>
                    <CoinAvatar symbol={sym} size={16} />
                    <span className={styles.sym}>
                      {SYM(sym)}
                      {side && <i data-side={side === "多" ? "long" : "short"}>{side}</i>}
                    </span>
                    <span className={styles.tags}>
                      {st?.regime && <em data-tone={regime.tone}>{regime.text}</em>}
                      {vol && <em data-tone={vol.tone}>{vol.text}</em>}
                      {st?.squeeze && <em data-tone="squeeze">挤压</em>}
                    </span>
                  </div>

                  {/* ---------- 动量 · 极值 ---------- */}
                  <div className={styles.groupLabel}>动量 · 极值</div>
                  <Row label="RSI" value={rsi == null ? "--" : String(Math.round(rsi))}
                    status={rsiStatus} tone={rsiTone} na={rsi == null}>
                    {rsi == null ? <span className={styles.chartNa} /> :
                      <RangeSlider v={rsi} min={0} max={100} safe={[30, 70]} tone={rsiTone} />}
                  </Row>
                  <Row label="%B" value={pctB == null ? "--" : pctB.toFixed(2)}
                    status={bStatus} tone={bTone} na={pctB == null}>
                    {pctB == null ? <span className={styles.chartNa} /> :
                      <RangeSlider v={pctB} min={-0.2} max={1.2} safe={[0, 1]} tone={bTone} />}
                  </Row>
                  <Row label="Z值" value={z == null ? "--" : z.toFixed(1)}
                    status={zStatus} tone={zTone} na={z == null}>
                    {z == null ? <span className={styles.chartNa} /> :
                      <RangeSlider v={z} min={-3} max={3} safe={[-2, 2]} tone={zTone} />}
                  </Row>

                  {/* ---------- 趋势 · 动能 ---------- */}
                  <div className={styles.groupLabel}>趋势 · 动能</div>
                  <Row label="ADX" value={adx == null ? "--" : String(Math.round(adx))}
                    status={adxStatus} tone={adxTone} na={adx == null}>
                    {adx == null ? <span className={styles.chartNa} /> :
                      <Gauge v={adx} min={0} max={50} threshold={25} />}
                  </Row>
                  <Row label="ROC" value={roc == null ? "--" : (roc * 100).toFixed(2)}
                    status={rocStatus} tone={rocTone} na={roc == null}>
                    {roc == null ? <span className={styles.chartNa} /> :
                      <BiBar v={roc} scale={0.015} up={up} down={down} />}
                  </Row>

                  {/* ---------- 波动 · 振幅 ---------- */}
                  <div className={styles.groupLabel}>波动 · 振幅</div>
                  <Row label="BW" value={bw == null ? "--" : String(Math.round(bw))}
                    status={bwStatus} tone={bwTone} na={bw == null}>
                    {bw == null ? <span className={styles.chartNa} /> : <SqueezeBar v={bw} />}
                  </Row>
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
                    <span data-tone="sl" title="止损距离（1.5×ATR，夹紧 0.4%~2%）">-{b.sl.toFixed(2)}%</span>
                    <span data-tone="tp" title="止盈距离（2.5×ATR，夹紧 0.18%~3%）">+{b.tp.toFixed(2)}%</span>
                    <span data-tone="rr" title="盈亏比 = 止盈 ÷ 止损">{(b.sl > 0 ? b.tp / b.sl : 0).toFixed(2)}</span>
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
