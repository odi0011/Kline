import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { LiveClient } from './live.js'
import CoinCard from './CoinCard.jsx'
import PatternGlyph from './PatternGlyph.jsx'
import { PATTERN_META } from './patterns_meta.js'
import { Notification, NotificationViewport } from '@/components/base/notification/notification'
import { Card } from '@/components/arc/card/card'
import { ScrollArea } from '@/components/arc/scroll-area/scroll-area'
import { GlassMenu } from '@/components/arc/glass-menu/glass-menu'
import AccountCard from '@/components/arc/account-card/account-card'
import { Badge } from '@/components/base/badges/badge'
import { AnimatedCounter } from '@/components/arc/animated-counter/animated-counter'
import CoinAvatar from '@/components/ui/coin-avatar'
import SettingsDialog from '@/components/arc/settings-dialog/settings-dialog'
import QuantPanel from '@/components/arc/quant-panel/quant-panel'
import PositionHistory from '@/components/arc/position-history/position-history'
import NewsFeed from '@/components/arc/news-feed/news-feed'
import StrategyPanel from '@/components/arc/strategy-panel/strategy-panel' 
import DecisionOrchestrator from '@/components/arc/decision-orchestrator/decision-orchestrator'
import MetricFlights from '@/components/arc/decision-orchestrator/metric-flights'
import HitFlight from './HitFlight.jsx'
import { hexHue } from './lib/account-theme'
import { SlidersHorizontal, Clock3, BarChart3, Grid3x3, MoveHorizontal, Coins, Settings } from 'lucide-react'

const WS_PORT = 8765

// HTTPS 走同源 wss（nginx 反代 /ws）；本地 HTTP 直连行情端口。
function liveHubUrl() {
  if (location.protocol === 'https:') return `wss://${location.host}/ws`
  return `ws://${location.hostname}:${WS_PORT}`
}

const HIT_TTL_MS = 9_000
// 命中动画队列：最多排队 2 条；排队超过 5 秒的放弃动画（交还流水显示）。
// 多币同时收线时会一起触发，串行播完要好幾秒，动画必然落后于数据。
const MAX_QUEUE = 2
const MAX_WAIT_MS = 5_000
const SETTINGS_KEY = 'kline.settings.v1'
const MIN_SYMBOLS = 4
const MAX_SYMBOLS = 10
const SETTINGS_VERSION = 2
const DEFAULT_SETTINGS = {
  v: SETTINGS_VERSION,
  interval: '3m', visibleBars: 70, showVolume: true, showGrid: true,
  // 空数组 = 尚未选择 → 自动取服务端监控池的前 MIN_SYMBOLS 个。
  // 不再硬编码币种名：换项目/换配置时旧的默认值会与实际池不符。
  symbols: [],
}
const DEFAULT_UI = {
  themeColor: '#f0b90b', font: 'douyin',
  upDown: 'green-red', upColor: '#16a34a', downColor: '#dc2626',
}   // 外观默认值；连接后以服务端下发的 ui 为准（设置即真实功能）

function loadSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}
    // 版本不一致：旧结构（硬编码币种、旧字段语义）直接丢弃，用新默认值
    if (saved.v !== SETTINGS_VERSION) {
      return { ...DEFAULT_SETTINGS }
    }
    return { ...DEFAULT_SETTINGS, ...saved }
  } catch {
    return { ...DEFAULT_SETTINGS }
  }
}

const STATUS_BY_DIR = { bullish: 'success', bearish: 'error', neutral: 'information' }
const DIR_LABEL = { bullish: '上涨', bearish: '下跌', neutral: '变盘' }
const fmtPrice = (p) => (p >= 1000 ? p.toFixed(1) : p >= 1 ? p.toFixed(3) : p.toPrecision(4))

const fmtTime = (ts) => {
  // ts 为 UTC 毫秒：+8h 后用 UTC 取值才是北京时间（本地时区取值会叠加成 +16h）
  const d = new Date(ts + 8 * 3600 * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`
}

const QUANT_STATE_KEYS = [
  "price", "bars", "atr", "atrPct", "realizedVol", "boll", "bwPct",
  "rsi", "z", "adx", "plusDI", "minusDI", "roc", "regime", "volState", "squeeze",
]

/** 量化快照偶尔会先广播一个指标尚未填充的帧。展示层保留上一份非空值，
    避免左侧真实数据卡瞬间退回“计算中”再恢复。 */
function mergeQuantSnapshot(current, next) {
  if (!current?.analysis || !next?.analysis) return next
  const oldAnalysis = current.analysis
  const newAnalysis = next.analysis
  const symbols = { ...(oldAnalysis.symbols || {}) }
  for (const [symbol, incoming] of Object.entries(newAnalysis.symbols || {})) {
    const previous = oldAnalysis.symbols?.[symbol]
    if (!previous) {
      symbols[symbol] = incoming
      continue
    }
    const merged = { ...previous, ...incoming }
    for (const key of QUANT_STATE_KEYS) {
      if (incoming?.[key] == null && previous?.[key] != null) merged[key] = previous[key]
    }
    if (incoming?.boll && previous?.boll) {
      merged.boll = { ...previous.boll }
      for (const [key, value] of Object.entries(incoming.boll)) {
        if (value != null) merged.boll[key] = value
      }
    }
    symbols[symbol] = merged
  }
  const mergedAnalysis = { ...oldAnalysis, ...newAnalysis, symbols }
  for (const key of ["liveFeatures", "liveEvaluations", "liveScoreHistory"]) {
    if (!Object.keys(newAnalysis[key] || {}).length && Object.keys(oldAnalysis[key] || {}).length) {
      mergedAnalysis[key] = oldAnalysis[key]
    }
  }
  return { ...next, analysis: mergedAnalysis }
}

export default function App() {
  const [meta, setMeta] = useState(null)
  const [stats, setStats] = useState({ status: 'connecting', mode: '', lag: null })
  const [hits, setHits] = useState([])
  const [accounts, setAccounts] = useState([])
  const [patternStats, setPatternStats] = useState({})
  const [symbolStats, setSymbolStats] = useState({})
  const [hoverPattern, setHoverPattern] = useState(null)   // {pattern, symbol} 悬停K线触发的形态
  const [settings, setSettings] = useState(loadSettings)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsSection, setSettingsSection] = useState('appearance')  // 打开设置时定位的分区
  const [ui, setUi] = useState(DEFAULT_UI)           // 外观：服务端下发（config 持久化）
  const [patternsEnabled, setPatternsEnabled] = useState(null)  // null = 未从服务端加载
  const [livePreview, setLivePreview] = useState(true)
  const [srvAccounts, setSrvAccounts] = useState(null)          // 服务端账户列表（含密钥）
  const [aiCfg, setAiCfg] = useState({})                        // AI 配置（服务端，密钥为掩码）
  const [aiModelsResult, setAiModelsResult] = useState(null)     // 一键获取模型的结果
  const [aiTestResult, setAiTestResult] = useState(null)         // 单模型连通测试结果
  const [quantSnap, setQuantSnap] = useState(null)              // 量化引擎快照
  const [quantEvents, setQuantEvents] = useState(null)          // 持久化决策流水（MySQL）
  const [quantTrades, setQuantTrades] = useState([])            // 仓位历史（MySQL，7 天）
  const [newsItems, setNewsItems] = useState([])                // 交易所公告
  const [structureVersion, setStructureVersion] = useState(0)   // 结构位更新触发决策流刷新
  const [metricPulses, setMetricPulses] = useState([])          // 指标条 → 计算网络的实时数据波
  const liveRef = useRef(null)
  const pushMetricPulse = useCallback((pulse) => {
    if (!pulse?.id) return
    setMetricPulses((current) => [...current, pulse].slice(-48))
  }, [])
  const finishMetricPulse = useCallback((id) => {
    setMetricPulses((current) => current.filter((pulse) => pulse.id !== id))
  }, [])
  const cardsViewportRef = useRef(null)
  const settingsRef = useRef(settings)
  settingsRef.current = settings

  // 外观即时生效：主题色 → --accent（菜单高亮/徽标等点缀），字体 → 全局
  // 同时把主题色换算成色相变量，供账户卡等组件做同色系淡色材质
  useEffect(() => {
    document.documentElement.style.setProperty('--accent', ui.themeColor)
    document.documentElement.style.setProperty('--accent-hue', String(hexHue(ui.themeColor).toFixed(1)))
  }, [ui.themeColor])
  useEffect(() => {
    const fonts = {
      douyin: '"Douyin Sans", "Microsoft YaHei", system-ui, sans-serif',
      system: 'system-ui, -apple-system, "Microsoft YaHei", sans-serif',
      serif: 'Georgia, "Times New Roman", "SimSun", serif',
    }
    document.body.style.fontFamily = fonts[ui.font] || fonts.douyin
  }, [ui.font])

  // K线涨跌颜色（预设或自定义）
  const chartColors = useMemo(() => {
    if (ui.upDown === 'red-green') return { up: '#dc2626', down: '#16a34a' }
    if (ui.upDown === 'custom') return { up: ui.upColor, down: ui.downColor }
    return { up: '#16a34a', down: '#dc2626' }
  }, [ui.upDown, ui.upColor, ui.downColor])

  const connected = stats.status === 'connected'
  const mode = stats.mode || meta?.mode || ''
  const defaultIv = meta
    ? (meta.intervals.includes(settings.interval) ? settings.interval : meta.intervals[0])
    : settings.interval
  // 监控池 = 服务端 config symbols；展示 = 池 ∩ 用户选择（本地持久化）
  const pool = (meta?.symbols || []).filter(Boolean)
  const valid = settings.symbols.filter((s) => pool.includes(s))
  // 选择与当前池交集不足（换项目/配置缩水/首次访问）→ 自动补成池的前几个，
  // 否则会只剩一两个币、看起来像"币种丢失"。
  const shownSymbols = (() => {
    if (!pool.length) return []
    if (valid.length >= MIN_SYMBOLS) return valid
    const fill = pool.filter((s) => !valid.includes(s)).slice(0, MIN_SYMBOLS - valid.length)
    return [...valid, ...fill]
  })()

  // 决策流水只显示当前展示币种的记录（无币种的系统/审计事件始终保留）：
  // 引擎可能在其它币上产生决策，但用户看不到那些K线，显示出来只会造成困惑。
  // 注意：这里不再压制任何 signal 行——流水必须完整展示引擎的真实决策链
  // （收到信号 → 各种过滤/跳过 → 下单），动画只负责高亮，不影响数据。
  const visibleEvents = useMemo(() => {
    if (!quantEvents) return null
    const set = new Set(shownSymbols)
    return quantEvents.filter((ev) => !(ev.symbol && !set.has(ev.symbol)))
  }, [quantEvents, shownSymbols.join(",")])

  // 唯一账户：持仓币种排前，池内持仓但未展示的自动添加到其后
  const account = accounts[0] || null
  const posSyms = account && Array.isArray(account.positions) ? account.positions.map((p) => p.symbol) : []
  const orderedSymbols = useMemo(() => {
    if (!posSyms.length) return shownSymbols
    const posSet = new Set(posSyms)
    const inList = shownSymbols.filter((s) => posSet.has(s))
    const rest = shownSymbols.filter((s) => !posSet.has(s))
    const autoAdd = posSyms.filter((s) => pool.includes(s) && !shownSymbols.includes(s))
    // 持仓币种（含自动添加的）整体排在非持仓币种之前
    return [...inList, ...autoAdd, ...rest]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownSymbols.join(","), posSyms.join(","), pool.join(",")])
  const marketStructures = useMemo(() => {
    const out = {}
    for (const symbol of orderedSymbols) {
      const byInterval = liveRef.current?.structures?.[symbol]
      const structure = byInterval?.[defaultIv] || Object.values(byInterval || {})[0]
      out[symbol] = { levels: (structure?.levels || []).slice(0, 8) }
    }
    return out
  }, [structureVersion, defaultIv, orderedSymbols.join(",")])

  // 账户权益曲线：服务端每次广播都带最近 8 小时的采样（持久化在 MySQL），
  // 前端不再本地采样——刷新页面也能立刻看到真实历史，而不是从一条平线开始。
  const accountSeries = account && Array.isArray(account.series) ? account.series : undefined

  useEffect(() => {
    const live = new LiveClient(liveHubUrl())
    liveRef.current = live
    if (typeof window !== "undefined") window.__live = live   // 调试钩子：控制台可注入模拟信号
    live.onMeta((m) => {
      setMeta(m)
      setStats((s) => ({ ...s, mode: m.mode }))
    })
    live.onStats((s) => setStats((prev) => ({ ...prev, ...s, status: 'connected' })))
    live.onStatus((s) => setStats((prev) => ({ ...prev, status: s })))
    const offHit = live.on('hit', (h) => {
      if (!settingsRef.current.symbols.includes(h.symbol)) return   // 只提示当前展示的币种
      const item = { ...h, id: `${h.ts}-${h.symbol}-${h.pattern}-${h.mode}-${Math.random().toString(36).slice(2, 7)}` }
      setHits((x) => [item, ...x].slice(0, 6))
    })
    const offStats = live.on('pattern_stats', (m) => setPatternStats(m.stats || {}))
    const offSymStats = live.on('symbol_stats', (m) => setSymbolStats(m.stats || {}))
    const offAcc = live.on('accounts', (m) => setAccounts(m.accounts || []))
    const offSettings = live.on('settings', (m) => {
      const s = m.settings || {}
      if (s.ui) setUi((prev) => ({ ...prev, ...s.ui }))
      if (s.patterns) {
        if (Array.isArray(s.patterns.enabled)) setPatternsEnabled(s.patterns.enabled)
        if (typeof s.patterns.live_preview === 'boolean') setLivePreview(s.patterns.live_preview)
      }
      if (Array.isArray(s.accounts)) setSrvAccounts(s.accounts)
      if (s.ai) setAiCfg(s.ai)
    })
    const offAiModels = live.on('ai_models', (m) => setAiModelsResult(m.result || null))
    const offAiTest = live.on('ai_test', (m) => setAiTestResult({ ...(m.result || {}), at: Date.now() }))
    const offQuant = live.on('quant', (m) => {
      setQuantSnap((current) => mergeQuantSnapshot(current, m.snapshot))
    })
    const offQEvents = live.on('quant_events', (m) => setQuantEvents(m.events || []))
    const offStructureInit = live.on('init', () => setStructureVersion((v) => v + 1))
    const offStructure = live.on('structure', () => setStructureVersion((v) => v + 1))
    const offTrades = live.on('quant_trades', (m) => setQuantTrades(m.trades || []))
    const offTrade = live.on('quant_trade', (t) => {
      if (!t) return
      setQuantTrades((cur) => [t, ...(cur || [])].slice(0, 500))
    })
    const offNews = live.on('news', (m) => setNewsItems(m.items || []))
    // 单条实时事件：追加到主列表（前端负责逐条入场，不整批涌入）
    const offQEvent = live.on('quant_event', (ev) => {
      if (!ev) return
      setQuantEvents((cur) => {
        const list = cur || []
        const key = ev.id ?? `${ev.ts}-${ev.reason}`
        if (list.some((x) => (x.id ?? `${x.ts}-${x.reason}`) === key)) return list
        return [...list, { ...ev, id: key }]
      })
    })
    // 后台标签页期间 _emitKey 被暂停（避免 motion 帧调度队列无限累积）；
    // 回到前台时从仓库全量重同步，图表与面板一次性补齐隐藏期间的数据。
    const offResume = live.on('resume', () => {
      if (live.meta) {
        setMeta(live.meta)
        setStats((s) => ({ ...s, mode: live.meta.mode }))
      }
      setPatternStats(live.patternStats || {})
      setAccounts(live.accounts || [])
      const s = live.settings || {}
      if (s.ui) setUi((prev) => ({ ...prev, ...s.ui }))
      if (s.patterns) {
        if (Array.isArray(s.patterns.enabled)) setPatternsEnabled(s.patterns.enabled)
        if (typeof s.patterns.live_preview === 'boolean') setLivePreview(s.patterns.live_preview)
      }
      if (Array.isArray(s.accounts)) setSrvAccounts(s.accounts)
      if (s.ai) setAiCfg(s.ai)
      if (live.quant) setQuantSnap((current) => mergeQuantSnapshot(current, live.quant))
      setQuantEvents(live.quantEvents || [])
      setQuantTrades(live.quantTrades || [])
      setNewsItems(live.news || [])
      setStructureVersion((v) => v + 1)
    })
    live.connect()
    return () => { offHit(); offStats(); offSymStats(); offAcc(); offSettings(); offQuant(); offQEvents(); offStructureInit(); offStructure(); offQEvent(); offTrades(); offTrade(); offNews(); offAiModels(); offAiTest(); offResume(); live.close() }
  }, [])

  // ==================== 命中动画：串行队列 ====================
  // 多个币 / 多个形态同时触发时，逐条完整播放（第一段→卡片填充→第二段→行揭示），
  // 后一条等前一条播完再开始。并发播会互相覆盖状态、导致飞线错乱。
  const [flight, setFlight] = useState(null)          // 当前飞线（arc / wobble）
  const [cardFill, setCardFill] = useState(null)      // 形态卡背景扩散 {pattern, ox, oy, grow}
  // 飞线落点指向的「真实流水行」高亮：{ key: "symbol|pattern", tone }
  // 不再伪造占位行——那会在揭示后被删除，导致"闪一下就没了"。
  const [flashRow, setFlashRow] = useState(null)
  // 形态卡高亮是动画自己的状态，不能用 hits.find——hits 只留最近 6 条，
  // 动画多时旧 hit 被挤出数组会导致卡片背景直接卸载。
  const [cardActive, setCardActive] = useState(null)   // {pattern, direction, symbol}
  const flightKeyRef = useRef(0)
  const flightResolveRef = useRef(null)
  const landInfoRef = useRef(null)
  const jobQueueRef = useRef([])
  const runningRef = useRef(false)
  const queuedRef = useRef(new Set())
  const anchorsRef = useRef({})      // symbol -> () => 最后一根蜡烛的屏幕坐标
  const streamRef = useRef(null)

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

  const registerAnchor = useCallback((sym, fn) => {
    if (fn) anchorsRef.current[sym] = fn
    else delete anchorsRef.current[sym]
  }, [])

  /** 播放一段飞线；resolve 出落地信息（to/direction/pattern/mode） */
  const fly = (cfg) => new Promise((resolve) => {
    flightKeyRef.current += 1
    landInfoRef.current = null
    flightResolveRef.current = resolve          // 保存 resolve 函数本身
    setFlight({ ...cfg, key: flightKeyRef.current })
  })

  const onFlightLand = (to, direction, pattern, mode) => {
    landInfoRef.current = { to, direction, pattern, mode }
  }
  const onFlightDone = () => {
    // 注意：这里必须调用保存的 resolve 函数（之前误写成把 info 赋给 ref，
    // 导致 await fly() 永远不返回、整个串行队列卡死在第一条）
    const info = landInfoRef.current || {}
    const done = flightResolveRef.current
    flightResolveRef.current = null
    setFlight(null)
    if (done) done(info)
  }

  /** 跑一条命中动画（完整链路）。返回 true 表示流水行已被动画揭示；
   *  false 表示中途放弃（调用方需撤掉去重键，让流水正常显示该行）。 */
  const runOne = async (hit) => {
    let revealed = false
    try {
      revealed = await runOneInner(hit)
      return revealed
    } finally {
      // 弃播（找不到卡片/锚点等）时也要把高亮与填充清掉，
      // 否则卡片会一直亮着、背景层卡在半展开状态。
      if (!revealed) {
        setCardFill(null)
        setCardActive(null)
      }
    }
  }

  const runOneInner = async (hit) => {
    // 标记该形态卡为高亮态（滚动与背景扩散都用它）
    setCardActive({ pattern: hit.pattern, direction: hit.direction || "neutral", symbol: hit.symbol })
    // 1) 形态卡滚动居中。滚动距离为 0 时不必等满，否则流水动画会明显滞后。
    const needScroll = scrollToPattern(hit.pattern)
    await sleep(needScroll ? 720 : 60)

    const cardEl = document.querySelector(`.pattern-card[data-key="${hit.pattern}"]`)
    if (!cardEl) return false
    const d = cardEl.getBoundingClientRect()
    if (!d.width) return false

    // 2) 起点：源 K 线卡片中最后一根蜡烛的屏幕坐标（取不到退回卡片内偏右位置）
    const anchorFn = anchorsRef.current[hit.symbol]
    const src = anchorFn ? anchorFn() : null
    const srcBox = document.querySelector(`.card[data-symbol="${hit.symbol}"]`)
    const sb = srcBox ? srcBox.getBoundingClientRect() : null
    const from1 = src || (sb ? { x: sb.left + sb.width * 0.62, y: sb.top + sb.height * 0.3 } : null)
    if (!from1) return false

    // 3) 第一段：蜡烛 → 形态卡中心
    const land1 = await fly({
      mode: "arc",
      from: from1,
      to: { x: d.left + d.width / 2, y: d.top + d.height / 2 },
      direction: hit.direction || "neutral",
      pattern: hit.pattern,
      dot: 3,
      fly: 520,
    })

    // 4) 卡片背景从落点铺开
    const p1 = {
      pattern: hit.pattern,
      ox: Math.max(0, Math.min(100, ((land1.to.x - d.left) / d.width) * 100)),
      oy: Math.max(0, Math.min(100, ((land1.to.y - d.top) / d.height) * 100)),
      grow: false,
    }
    setCardFill(p1)
    await sleep(30)
    setCardFill({ ...p1, grow: true })
    await sleep(400)

    // 5) 等引擎那条真实的「收到信号」流水行出现（后端在 on_signal 入口统一落库）。
    //    不再伪造占位行——伪造的行揭示完就被删，用户会看到"闪一下就没了"，
    //    而真正该看的那一行反而被压掉。现在飞线指向的就是真实行，动画只是
    //    "把它指出来并高亮一下"，不承担承载数据的职责。
    //    选择器必须带 kind="signal"：同一币同形态还会有一条 skip/open 记录
    //    （如「收到十字星中性信号」+「十字星没有方向，不下单」），
    //    不带 kind 会把两行都选中，波点会同时展开在两行上。
    const sel = `[data-event-row][data-ev-kind="signal"]`
      + `[data-ev-symbol="${hit.symbol}"][data-ev-pattern="${hit.pattern}"]`
    // 同一个 (币,形态) 可能有多条历史 signal 行：取时间最新的那一条（DOM 末尾）
    const findRow = () => {
      const vp = streamRef.current
      if (!vp) return null
      const all = vp.querySelectorAll(sel)
      return all.length ? all[all.length - 1] : null
    }
    let rowEl = findRow()
    for (let i = 0; i < 12 && !rowEl; i++) {
      await sleep(120)
      rowEl = findRow()
    }
    if (!rowEl) {
      // 引擎还没发出这条记录（或已被过滤规则丢弃）→ 不做无意义的飞线
      setCardFill(null)
      if (!jobQueueRef.current.length) setCardActive(null)
      return true
    }
    // 目标行可能在视窗外：先滚进来再测量，否则飞线终点落到屏幕外
    let rr = rowEl.getBoundingClientRect()
    if (rr.bottom > window.innerHeight - 12 || rr.top < 12) {
      rowEl.scrollIntoView({ block: "nearest", inline: "nearest" })
      await sleep(160)
      const rr2 = rowEl.getBoundingClientRect()
      if (rr2.width) rr = rr2
    }

    // 6) 第二段：形态卡徽标 → 这条真实流水行
    const badge = cardEl.querySelector('[class*="dir-badge"]') || cardEl
    const b = badge.getBoundingClientRect()
    const sRect = streamRef.current ? streamRef.current.getBoundingClientRect() : null
    const clampToViewport = (pt) => ({
      x: Math.min(Math.max(pt.x, 6), window.innerWidth - 6),
      y: Math.min(Math.max(pt.y, 6), window.innerHeight - 6),
    })
    const to2 = clampToViewport(rr.width
      ? { x: rr.left + rr.width * 0.5, y: rr.top + rr.height / 2 }
      : { x: (sRect ? sRect.left : 0) + 120, y: (sRect ? sRect.bottom : 0) - 14 })
    await fly({
      mode: "wobble",
      from: { x: b.left + b.width / 2, y: b.top + b.height / 2 },
      to: to2,
      direction: hit.direction || "neutral",
      pattern: hit.pattern,
      dot: 2.6,
      fly: 620,
    })

    // 7) 高亮这条已存在的行（底色脉冲），只作用于这一条 signal 行。
    //    用「币+形态+kind+时间下限」定位：同一币同形态的历史行有很多条，
    //    只认动画开始之后新出现的那一条（ts >= 入队时刻）。
    setFlashRow({
      symbol: hit.symbol,
      pattern: hit.pattern,
      kind: "signal",
      since: Number(hit.ts) || 0,
      tone: hit.direction || "neutral",
    })
    await sleep(1200)
    setFlashRow(null)

    // 8) 卡片背景淡出，交棒下一条（仅当没有新任务接管时才清高亮）
    setCardFill(null)
    if (!jobQueueRef.current.length) setCardActive(null)
    await sleep(120)
    return true
  }

  /** 队列驱动：一次只跑一条。
   *
   * 一致性优先于动画完整：多币同收线时会有多条命中同时到达，串行播完要好幾秒，
   * 那时数据早已过时、流水里也已经有了新记录。所以：
   *   · 排队超过 MAX_WAIT_MS 的任务直接放弃动画，交还给流水正常显示；
   *   · runOne 中途失败（找不到卡片/锚点）同样放弃，让流水行立刻出现。
   * 判断依据是「这一行最终有没有被揭示」——没揭示就必须撤掉去重键，
   * 否则那条 signal 会被过滤掉，流水反而缺行。 */
  const pump = () => {
    if (runningRef.current) return
    runningRef.current = true
    ;(async () => {
      while (jobQueueRef.current.length) {
        const job = jobQueueRef.current.shift()
        const waited = Date.now() - job.enqueuedAt
        if (waited > MAX_WAIT_MS) continue           // 排队太久 → 跳过动画（流水本就有真实记录）
        try {
          await runOne(job.hit)
        } catch (e) {
          console.error("[flight] job failed", e)
        }
      }
      runningRef.current = false
    })()
  }

  // 新命中入队（按时间正序播放；同一 hit 只入队一次）
  //
  // 只播「收线确认」的命中：盘中预警（mode === "live"）是同一根未收盘K线上的
  // 反复试算，币安 4 个币各报一次就会一次性冒出好几段动画；而且盘中预警不进
  // 量化决策（main.py 只对 confirmed 调 on_signal），动画落到流水后没有任何
  // 真实记录跟着出现——正是"虚拟动画跑完却没有数据"的来源。
  useEffect(() => {
    if (!hits.length) return
    const fresh = []
    const now = Date.now()
    for (const h of hits.slice(0, 8)) {
      if (h.mode !== "confirmed") continue          // 盘中预警不播动画
      if (now - (Number(h.ts) || 0) > 20000) continue  // 陈旧命中（切回前台/重连积压）不补播
      const key = h.id || `${h.ts}-${h.symbol}-${h.pattern}`
      if (queuedRef.current.has(key)) continue
      queuedRef.current.add(key)
      fresh.push(h)
    }
    if (!fresh.length) return
    // 去重集合只保留最近 500 条，防长时间运行无限增长
    if (queuedRef.current.size > 500) queuedRef.current.clear()
    // hits 最新在前 → 反转成时间正序
    const ordered = fresh.reverse()
    // 队列容量：正在播的 1 条 + 排队 MAX_QUEUE 条。多币同时收线时超出容量的
    // 命中不再排队（串行播完要好几秒，那时数据早已过时）——流水有真实记录，不影响可读性。
    let room = MAX_QUEUE - jobQueueRef.current.length
    for (const h of ordered) {
      if (room <= 0) continue
      jobQueueRef.current.push({ hit: h, enqueuedAt: Date.now() })
      room--
    }
    if (!jobQueueRef.current.length) return
    pump()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hits])

  // 设置持久化
  useEffect(() => {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings))
  }, [settings])

  // 检测周期跟随前端设置（连接后下发，重连由 live 自动重发）
  useEffect(() => {
    if (connected) liveRef.current?.setDetectInterval(settings.interval)
  }, [settings.interval, connected])

  // 量化交易范围 = 当前实际展示的币种：
  // 否则引擎会在用户没打开K线的币上开仓，持仓里凭空多出看不懂的仓位。
  useEffect(() => {
    if (!connected) return
    liveRef.current?.setQuantSymbols(shownSymbols)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownSymbols.join(","), connected])

  // 选择与池不一致时回写（补齐后持久化，下次直接可用）
  useEffect(() => {
    if (!pool.length || !shownSymbols.length) return
    const same = shownSymbols.length === settings.symbols.length
      && shownSymbols.every((s, i) => s === settings.symbols[i])
    if (!same) setSettings((s) => ({ ...s, symbols: shownSymbols }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pool.join(","), shownSymbols.join(",")])

  // meta 的周期列表里没有已存设置时回退
  useEffect(() => {
    if (meta && !meta.intervals.includes(settings.interval)) {
      setSettings((s) => ({ ...s, interval: meta.intervals[0] }))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta])

  const updateSetting = (patch) => setSettings((s) => ({ ...s, ...patch }))

  // 设置弹窗 → 服务端（热生效 + 持久化到 config.yaml）
  const updateUi = (patch) => {
    const next = { ...ui, ...patch }
    setUi(next)
    liveRef.current && liveRef.current.sendSettings({ ui: next })
  }
  const updatePatterns = (nextEnabled, nextLive = livePreview) => {
    setPatternsEnabled(nextEnabled)
    setLivePreview(nextLive)
    liveRef.current && liveRef.current.sendSettings({ patterns: { enabled: nextEnabled, live_preview: nextLive } })
  }
  const togglePattern = (key) => {
    const cur = patternsEnabled || []
    const next = cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key]
    if (next.length) updatePatterns(next)          // 至少保留一个形态
  }
  const toggleLivePreview = () => updatePatterns(patternsEnabled || [], !livePreview)
  // 账户：后端只保留一个，设置页保存即整体替换
  const saveAccount = (acct) => {
    const next = acct ? [acct] : []
    setSrvAccounts(next)
    liveRef.current && liveRef.current.sendSettings({ accounts: next })
  }

  // AI 设置：保存走 settings（服务端 merge，密钥留空/掩码即沿用）
  const saveAi = (ai) => {
    setAiCfg((prev) => ({ ...prev, ...ai, key: ai.key && ai.key.includes('•') ? prev.key : ai.key }))
    liveRef.current && liveRef.current.sendSettings({ ai })
  }
  const aiAction = (msg) => liveRef.current && liveRef.current.send(msg)

  // 币种勾选：最少 MIN_SYMBOLS 个、最多 MAX_SYMBOLS 个，超限直接忽略
  const toggleSymbol = (sym) => setSettings((s) => {
    const cur = s.symbols || []
    if (cur.includes(sym)) return cur.length <= MIN_SYMBOLS ? s : { ...s, symbols: cur.filter((x) => x !== sym) }
    if (cur.length >= MAX_SYMBOLS) return s
    return { ...s, symbols: [...cur, sym] }
  })

  // 滚动到指定形态卡片（居中）：纯数据计算目标位置（不依赖布局测量，页面冻结时也能滚动）
  // 返回是否真的需要滚动（调用方据此决定要不要等动画时长）。
  const lastScrollPatternRef = useRef('')
  const scrollToPattern = (patternKey) => {
    if (!patternKey || lastScrollPatternRef.current === patternKey) return false
    const vp = cardsViewportRef.current || document.querySelector('.right [class*="viewport"]')
    const keys = Object.keys(PATTERN_META)
    const idx = keys.indexOf(patternKey)
    if (!vp || idx < 0) return false
    lastScrollPatternRef.current = patternKey
    const colW = 176 + 12                              // grid-auto-columns + gap
    const target = Math.max(0, idx * colW + colW / 2 - vp.clientWidth / 2)
    if (Math.abs(vp.scrollLeft - target) < 2) return false
    let rafAlive = false
    requestAnimationFrame(() => { rafAlive = true })
    setTimeout(() => {
      if (!rafAlive) { vp.scrollLeft = target; return }
      const start = vp.scrollLeft
      const diff = target - start
      if (Math.abs(diff) < 2) return
      const t0 = performance.now()
      const dur = 520
      const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)  // easeInOutCubic
      const step = (now) => {
        const p = Math.min(1, (now - t0) / dur)
        vp.scrollLeft = start + diff * ease(p)
        if (p < 1) requestAnimationFrame(step)
      }
      requestAnimationFrame(step)
    }, 60)
    return true
  }

  const sel = shownSymbols || []   // 勾选状态 = 实际展示，避免未回写时全不勾
  const atMax = sel.length >= MAX_SYMBOLS
  const atMin = sel.length <= MIN_SYMBOLS
  const menuGroups = [
    {
      id: 'g-symbols',
      items: [{
        id: 'symbols', label: '选择币种', icon: <Coins size={16} />, shortcut: `${sel.length} 个`,
        items: pool.map((s) => ({
          id: `sym:${s}`, label: s.toUpperCase(),
          icon: <CoinAvatar symbol={s} size={18} />,
          checked: sel.includes(s), keepOpen: true,
          disabled: (atMax && !sel.includes(s)) || (atMin && sel.includes(s)),
        })),
        searchable: true, placeholder: '搜索币种',
        hint: `${sel.length}/${MAX_SYMBOLS} · 至少 ${MIN_SYMBOLS}`,
      }],
    },
    {
      id: 'g-interval',
      items: [{
        id: 'interval', label: 'K线周期', icon: <Clock3 size={16} />, shortcut: defaultIv,
        items: (meta?.intervals || ['1m', '3m']).map((iv) => ({
          id: `iv:${iv}`, label: iv, icon: <Clock3 size={16} />, checked: settings.interval === iv, keepOpen: true,
        })),
      }],
    },
    {
      id: 'g-range',
      items: [{
        id: 'range', label: '显示范围', icon: <MoveHorizontal size={16} />, shortcut: `最近 ${settings.visibleBars} 根`,
        items: [50, 70, 100, 150].map((n) => ({
          id: `bars:${n}`, label: `最近 ${n} 根`, icon: <MoveHorizontal size={16} />, checked: settings.visibleBars === n, keepOpen: true,
        })),
      }],
    },
    {
      id: 'g-chart',
      label: '图表',
      items: [
        { id: 'volume', label: '成交量', icon: <BarChart3 size={16} />, checked: settings.showVolume, keepOpen: true },
        { id: 'grid', label: '网格线', icon: <Grid3x3 size={16} />, checked: settings.showGrid, keepOpen: true },
      ],
    },
    {
      id: 'g-settings',
      items: [{ id: 'settings', label: '设置', icon: <Settings size={16} /> }],
    },
  ]

  const onMenuSelect = (id) => {
    if (id === 'settings') { setSettingsOpen(true); return }
    if (id.startsWith('sym:')) toggleSymbol(id.slice(4))
    else if (id.startsWith('iv:')) updateSetting({ interval: id.slice(3) })
    else if (id === 'volume') updateSetting({ showVolume: !settings.showVolume })
    else if (id === 'grid') updateSetting({ showGrid: !settings.showGrid })
    else if (id.startsWith('bars:')) updateSetting({ visibleBars: Number(id.slice(5)) })
  }

  return (
    <div className="app">
      {mode === 'demo' && (
        <div className="demo-banner" role="alert">
          演示行情：当前显示的是随机模拟数据，不是真实行情；量化不会据此下单
        </div>
      )}
      <main className="left">
        <ScrollArea orientation="vertical" label="币种K线" className="left-scroll" fade={20}>
          <div className="left-inner">
            {meta && orderedSymbols.map((s) => (
              <CoinCard key={s} symbol={s} intervals={meta.intervals}
                settings={{ ...settings, interval: defaultIv }}
                live={liveRef.current}
                chartColors={chartColors}
                account={account}
                accountSeries={accountSeries}
                registerAnchor={registerAnchor}
                activeHit={hits.find((h) => h.symbol === s)}
                onHoverHits={(hs, sym) => {
                  setHoverPattern(hs.length ? { pattern: hs[0].pattern, symbol: sym } : null)
                  if (hs.length) scrollToPattern(hs[0].pattern)
                }} />
            ))}
          </div>
        </ScrollArea>
        <div className="glass-wrap">
          <GlassMenu label="图表设置" triggerLabel="图表设置" align="start" width={240}
            groups={menuGroups} onSelect={onMenuSelect}>
            <SlidersHorizontal size={18} />
          </GlassMenu>
        </div>
      </main>

      <aside className="right">
        <div className="top-row">
          <div className="pattern-zone">
            <ScrollArea orientation="horizontal" scrollbars="auto" scrollButtons label="K线形态图鉴" viewportRef={cardsViewportRef}>
              <div className="cards-grid">
                {Object.entries(PATTERN_META).map(([key, p]) => {
                  // 高亮来源：动画进行中的 cardActive（优先）→ 最近命中 → 悬停
                  // 用 cardActive 而不是只查 hits，避免 hits 被挤出后背景层卸载
                  const active = (cardActive && cardActive.pattern === key ? cardActive : null)
                    || hits.find((h) => h.pattern === key)
                    || (hoverPattern?.pattern === key ? { symbol: hoverPattern.symbol } : null)
                  const st = patternStats[key] || { count: 0, wins: 0, losses: 0 }
                  const decided = st.wins + st.losses
                  const winPct = decided > 0 ? Math.round((st.wins / decided) * 100) : null
                  const tier = decided > 0 ? (winPct >= 50 ? 'hi' : winPct >= 10 ? 'mid' : 'low') : null
                  return (
                    <div className={`pattern-card ${active ? 'hit' : ''}`} data-key={key} key={key}>
                      {active && (
                        <div className={`pn-hit-bg ${active.direction || p.dir}`} aria-hidden
                          style={cardFill && cardFill.pattern === key
                            ? { clipPath: `circle(${cardFill.grow ? 150 : 0}% at ${cardFill.ox}% ${cardFill.oy}%)` }
                            : undefined} />
                      )}
                      <Card title={<span className="pc-title">{p.name}
                        <span className="cnt-mini" title={`累计触发 ${st.count} 次`}>
                          <AnimatedCounter value={st.count} decimals={0} />
                        </span>
                      </span>}
                        description={p.desc} media={<PatternGlyph candles={p.candles} />} />
                      <div className="badges">
                        <Badge className={`dir-badge ${p.dir}`}>{DIR_LABEL[p.dir]}</Badge>
                        {tier && <Badge className={`win-badge ${tier}`}>胜{winPct}%</Badge>}
                      </div>
                    </div>
                  )
                })}
              </div>
            </ScrollArea>
          </div>
          <div className="wallet-zone">
            <AccountCard account={account} series={accountSeries} accent={ui.themeColor}
              chartColors={chartColors} />
          </div>
        </div>
      {/* 第二行：原有币种分析 30% / 实时决策编排 50% / 后续板块预留 20% */}
      <div className="row2">
        <div className="strategy-slot">
          <StrategyPanel analysis={quantSnap && quantSnap.analysis} symbols={orderedSymbols}
            chartColors={chartColors} onMetricPulse={pushMetricPulse} />
        </div>
        <div className="orchestrator-slot">
          <DecisionOrchestrator analysis={quantSnap && quantSnap.analysis} snapshot={quantSnap}
            symbols={orderedSymbols} events={visibleEvents} metricPulses={metricPulses} />
        </div>
        <div className="row2-reserved" aria-hidden />
      </div>
      {/* 第三行：决策流水 / 仓位历史 / 交易所公告（各 1/3） */}
      <div className="row3">
        <QuantPanel snapshot={quantSnap} events={visibleEvents} chartColors={chartColors}
          flashRow={flashRow} streamRef={streamRef}
          onToggle={(v) => liveRef.current && liveRef.current.sendSettings({ quant: { enabled: v } })} />
        <PositionHistory trades={quantTrades} chartColors={chartColors} />
        <NewsFeed items={newsItems} />
      </div>
      </aside>

      {/* 设置弹窗 */}
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)}
        section={settingsSection} onSectionChange={setSettingsSection}
        ui={ui} onUpdateUi={updateUi}
        patternsEnabled={patternsEnabled} livePreview={livePreview}
        onTogglePattern={togglePattern} onToggleLivePreview={toggleLivePreview}
        accounts={srvAccounts} onSaveAccount={saveAccount}
        ai={aiCfg} onSaveAi={saveAi} onAiAction={aiAction}
        aiModelsResult={aiModelsResult} aiTestResult={aiTestResult} />

      {/* 命中飞线：K线卡片 → 形态卡片，落地后触发卡片背景扩散 */}
      <HitFlight flight={flight} onLand={onFlightLand} onDone={onFlightDone} />
      <MetricFlights pulses={metricPulses} onDone={finishMetricPulse} />

      {/* 形态信号通知：左上角 */}
      <NotificationViewport position="top-left">
        {hits.map((h) => {
          return (
            <Notification
              key={h.id}
              status={STATUS_BY_DIR[h.direction] || 'information'}
              leading={<CoinAvatar symbol={h.symbol} size={40} />}
              title={`${h.symbol.toUpperCase()} · ${h.interval}  ${h.name_zh} ${h.direction === 'bullish' ? '↑' : h.direction === 'bearish' ? '↓' : '◆'}`}
              description={`${h.mode === 'live' ? '盘中预警' : '收线确认'} @ ${fmtPrice(h.price)}`}
              timestamp={fmtTime(h.ts)}
              autoDismissDuration={HIT_TTL_MS}
              onDismiss={() => setHits((x) => x.filter((it) => it.id !== h.id))}
            />
          )
        })}
      </NotificationViewport>
    </div>
  )
}
