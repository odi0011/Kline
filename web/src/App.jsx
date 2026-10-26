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
import HitFlight from './HitFlight.jsx'
import { hexHue } from './lib/account-theme'
import { SlidersHorizontal, Clock3, BarChart3, Grid3x3, MoveHorizontal, Coins, Settings } from 'lucide-react'

const WS_PORT = 8765
const HIT_TTL_MS = 9_000
const SETTINGS_KEY = 'kline.settings.v1'
const MIN_SYMBOLS = 4
const MAX_SYMBOLS = 10
const DEFAULT_SETTINGS = {
  interval: '3m', visibleBars: 70, showVolume: true, showGrid: true,
  symbols: ['btcusdt', 'tslausdt', 'xauusdt', 'xagusdt'],   // 默认展示的四个币种（浏览器本地）
}
const DEFAULT_UI = {
  themeColor: '#f0b90b', font: 'douyin',
  upDown: 'green-red', upColor: '#16a34a', downColor: '#dc2626',
}   // 外观默认值；连接后以服务端下发的 ui 为准（设置即真实功能）

function loadSettings() {
  try {
    return { ...DEFAULT_SETTINGS, ...(JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}) }
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
  const [quantSnap, setQuantSnap] = useState(null)              // 量化引擎快照
  const [quantEvents, setQuantEvents] = useState(null)          // 持久化决策流水（MySQL）
  // 命中动画为 (币种,形态) 插入过流水行的时间戳：抑制引擎随后推送的重复信号
  const hitRowKeysRef = useRef(new Map())
  const liveRef = useRef(null)
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
  const pool = meta?.symbols || DEFAULT_SETTINGS.symbols
  const displayed = pool.filter((s) => settings.symbols.includes(s))
  const shownSymbols = displayed.length ? displayed : pool.slice(0, MIN_SYMBOLS)

  // 决策流水只显示当前展示币种的记录（无币种的系统/审计事件始终保留）：
  // 引擎可能在其它币上产生决策，但用户看不到那些K线，显示出来只会造成困惑。
  const visibleEvents = useMemo(() => {
    if (!quantEvents) return null
    const set = new Set(shownSymbols)
    const now = Date.now()
    const hitKeys = hitRowKeysRef.current
    return quantEvents.filter((ev) => {
      if (ev.symbol && !set.has(ev.symbol)) return false
      // 命中动画已为该 (币种, 形态) 插入了一行动画记录，
      // 引擎随后推送的同一条信号不再重复显示（20 秒窗口）。
      if (ev.kind === "signal" && ev.symbol && ev.pattern) {
        const t = hitKeys.get(`${ev.symbol}|${ev.pattern}`)
        if (t && now - t < 20000) return false
      }
      return true
    })
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

  // 账户权益曲线：服务端每次广播都带最近 8 小时的采样（持久化在 MySQL），
  // 前端不再本地采样——刷新页面也能立刻看到真实历史，而不是从一条平线开始。
  const accountSeries = account && Array.isArray(account.series) ? account.series : undefined

  useEffect(() => {
    const live = new LiveClient(`ws://${location.hostname}:${WS_PORT}`)
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
    })
    const offQuant = live.on('quant', (m) => setQuantSnap(m.snapshot))
    const offQEvents = live.on('quant_events', (m) => setQuantEvents(m.events || []))
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
    live.connect()
    return () => { offHit(); offStats(); offSymStats(); offAcc(); offSettings(); offQuant(); offQEvents(); offQEvent(); live.close() }
  }, [])

  // ==================== 命中动画：串行队列 ====================
  // 多个币 / 多个形态同时触发时，逐条完整播放（第一段→卡片填充→第二段→行揭示），
  // 后一条等前一条播完再开始。并发播会互相覆盖状态、导致飞线错乱。
  const [flight, setFlight] = useState(null)          // 当前飞线（arc / wobble）
  const [cardFill, setCardFill] = useState(null)      // 形态卡背景扩散 {pattern, ox, oy, grow}
  const [reservedRows, setReservedRows] = useState([]) // 流水占位行（先空白，飞线到达后揭示）
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
  const rowSeqRef = useRef(0)

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

  /** 跑一条命中动画（完整链路） */
  const runOne = async (hit) => {
    // 标记该形态卡为高亮态（滚动与背景扩散都用它）
    setCardActive({ pattern: hit.pattern, direction: hit.direction || "neutral", symbol: hit.symbol })
    // 1) 形态卡滚动居中，等滚动动画结束（120ms 延迟 + 680ms 动画）
    scrollToPattern(hit.pattern)
    await sleep(860)

    const cardEl = document.querySelector(`.pattern-card[data-key="${hit.pattern}"]`)
    if (!cardEl) return
    const d = cardEl.getBoundingClientRect()
    if (!d.width) return

    // 2) 起点：源 K 线卡片中最后一根蜡烛的屏幕坐标（取不到退回卡片内偏右位置）
    const anchorFn = anchorsRef.current[hit.symbol]
    const src = anchorFn ? anchorFn() : null
    const srcBox = document.querySelector(`.card[data-symbol="${hit.symbol}"]`)
    const sb = srcBox ? srcBox.getBoundingClientRect() : null
    const from1 = src || (sb ? { x: sb.left + sb.width * 0.62, y: sb.top + sb.height * 0.3 } : null)
    if (!from1) return

    // 3) 第一段：蜡烛 → 形态卡中心
    const land1 = await fly({
      mode: "arc",
      from: from1,
      to: { x: d.left + d.width / 2, y: d.top + d.height / 2 },
      direction: hit.direction || "neutral",
      pattern: hit.pattern,
      dot: 3,
      fly: 700,
    })

    // 4) 卡片背景从落点铺开
    const p1 = {
      pattern: hit.pattern,
      ox: Math.max(0, Math.min(100, ((land1.to.x - d.left) / d.width) * 100)),
      oy: Math.max(0, Math.min(100, ((land1.to.y - d.top) / d.height) * 100)),
      grow: false,
    }
    setCardFill(p1)
    await sleep(40)
    setCardFill({ ...p1, grow: true })
    await sleep(680)

    // 5) 先留一行空白（此刻才插入，避免文字提前出现）
    rowSeqRef.current += 1
    const rowId = `hitrow-${rowSeqRef.current}`
    const dirText = hit.direction === "bullish" ? "看涨" : hit.direction === "bearish" ? "看跌" : "中性"
    const px = Number(hit.price) || 0
    const ev = {
      id: rowId,
      ts: Date.now(),
      kind: "signal",
      account: account ? account.name : "",
      symbol: hit.symbol,
      reason: `收到 ${hit.name_zh || hit.pattern} ${dirText}信号 @ ${px >= 1000 ? px.toFixed(1) : px.toFixed(4)}`,
      pattern: hit.pattern,
      name_zh: hit.name_zh,
      direction: hit.direction,
    }
    hitRowKeysRef.current.set(`${hit.symbol}|${hit.pattern}`, Date.now())
    setReservedRows((prev) => [...prev, { id: rowId, ev, revealed: false }])
    await sleep(340)      // 等占位行挂载并自动滚到底

    // 6) 第二段：形态卡徽标 → 刚留出的那一行
    const badge = cardEl.querySelector('[class*="dir-badge"]') || cardEl
    const b = badge.getBoundingClientRect()
    const rowEl = streamRef.current ? streamRef.current.querySelector(`[data-row-id="${rowId}"]`) : null
    const rr = rowEl ? rowEl.getBoundingClientRect() : null
    const sRect = streamRef.current ? streamRef.current.getBoundingClientRect() : null
    const to2 = rr && rr.width
      ? { x: rr.left + rr.width * 0.5, y: rr.top + rr.height / 2 }
      : { x: (sRect ? sRect.left : 0) + 120, y: (sRect ? sRect.bottom : 0) - 14 }
    const land2 = await fly({
      mode: "wobble",
      from: { x: b.left + b.width / 2, y: b.top + b.height / 2 },
      to: to2,
      direction: hit.direction || "neutral",
      pattern: hit.pattern,
      dot: 2.6,
      fly: 620,
    })

    // 7) 揭示：文本出现，背景从落点展开
    const fx = rr ? Math.max(0, Math.min(100, ((land2.to.x - rr.left) / rr.width) * 100)) : 50
    const fy = rr ? Math.max(0, Math.min(100, ((land2.to.y - rr.top) / rr.height) * 100)) : 50
    setReservedRows((prev) => prev.map((r) => (
      r.id === rowId ? { ...r, revealed: true, tone: hit.direction || "neutral", fx, fy, grow: false } : r
    )))
    await sleep(40)
    setReservedRows((prev) => prev.map((r) => (
      r.id === rowId ? { ...r, grow: true } : r
    )))
    await sleep(760)

    // 8) 行背景收回并淡出（之前漏了这步，clip 一直停在 150% 不消散）
    setReservedRows((prev) => prev.map((r) => (
      r.id === rowId ? { ...r, fade: true } : r
    )))
    await sleep(420)
    setReservedRows((prev) => prev.filter((r) => r.id !== rowId))

    // 9) 卡片背景淡出，交棒下一条（仅当没有新任务接管时才清高亮）
    setCardFill(null)
    if (!jobQueueRef.current.length) setCardActive(null)
    await sleep(120)
  }

  /** 队列驱动：一次只跑一条 */
  const pump = () => {
    if (runningRef.current) return
    runningRef.current = true
    ;(async () => {
      while (jobQueueRef.current.length) {
        const job = jobQueueRef.current.shift()
        try {
          await runOne(job)
        } catch (e) {
          console.error("[flight] job failed", e)
        }
      }
      runningRef.current = false
    })()
  }

  // 新命中入队（按时间正序播放；同一 hit 只入队一次）
  useEffect(() => {
    if (!hits.length) return
    const fresh = []
    for (const h of hits.slice(0, 8)) {
      const key = h.id || `${h.ts}-${h.symbol}-${h.pattern}`
      if (queuedRef.current.has(key)) continue
      queuedRef.current.add(key)
      fresh.push(h)
    }
    if (!fresh.length) return
    // hits 最新在前 → 反转成时间正序；上限 8 条防止突发刷屏
    jobQueueRef.current.push(...fresh.reverse())
    if (jobQueueRef.current.length > 8) jobQueueRef.current.splice(0, jobQueueRef.current.length - 8)
    pump()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hits])

  /** 演示：模拟一次形态命中（只在前端注入，不落库、不触发真实交易）。
      kind = "single" 单个形态 / "multi" 多个币同时触发 / "variety" 不同形态依次触发 */
  const runDemo = (kind = "single") => {
    const patterns = Object.keys(PATTERN_META)
    const pool = shownSymbols.length ? shownSymbols : ["btcusdt"]
    const pick = (arr) => arr[Math.floor(Math.random() * arr.length)]
    const make = (sym, key) => {
      const meta = PATTERN_META[key]
      return {
        id: `demo-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        symbol: sym, interval: defaultIv, pattern: key,
        name_zh: meta.name, direction: meta.dir,
        mode: "confirmed", price: 100 + Math.random() * 100,
        ts: Date.now(),
      }
    }
    // 动画链会在第二段飞线前自行插入占位行，这里只注入 hit。
    // 多个同时注入，用来验证串行队列：应一条条完整播放而不是齐飞。
    const fire = (h) => setHits((x) => [h, ...x].slice(0, 8))
    if (kind === "multi") {
      const n = Math.min(pool.length, 4)
      const syms = [...pool].sort(() => Math.random() - 0.5).slice(0, n)
      syms.forEach((sym) => fire(make(sym, pick(patterns))))
      return
    }
    if (kind === "variety") {
      const sym = pick(pool)
      const keys = [...patterns].sort(() => Math.random() - 0.5).slice(0, 3)
      keys.forEach((key) => fire(make(sym, key)))
      return
    }
    fire(make(pick(pool), pick(patterns)))
  }

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

  // 币种勾选：最少 MIN_SYMBOLS 个、最多 MAX_SYMBOLS 个，超限直接忽略
  const toggleSymbol = (sym) => setSettings((s) => {
    const cur = s.symbols || []
    if (cur.includes(sym)) return cur.length <= MIN_SYMBOLS ? s : { ...s, symbols: cur.filter((x) => x !== sym) }
    if (cur.length >= MAX_SYMBOLS) return s
    return { ...s, symbols: [...cur, sym] }
  })

  // 滚动到指定形态卡片（居中）：纯数据计算目标位置（不依赖布局测量，页面冻结时也能滚动）
  const lastScrollPatternRef = useRef('')
  const scrollToPattern = (patternKey) => {
    if (!patternKey || lastScrollPatternRef.current === patternKey) return
    const vp = cardsViewportRef.current || document.querySelector('.right [class*="viewport"]')
    const keys = Object.keys(PATTERN_META)
    const idx = keys.indexOf(patternKey)
    if (!vp || idx < 0) return
    lastScrollPatternRef.current = patternKey
    const colW = 176 + 12                              // grid-auto-columns + gap
    const target = Math.max(0, idx * colW + colW / 2 - vp.clientWidth / 2)
    let rafAlive = false
    requestAnimationFrame(() => { rafAlive = true })
    setTimeout(() => {
      if (!rafAlive) { vp.scrollLeft = target; return }
      const start = vp.scrollLeft
      const diff = target - start
      if (Math.abs(diff) < 2) return
      const t0 = performance.now()
      const dur = 680
      const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)  // easeInOutCubic
      const step = (now) => {
        const p = Math.min(1, (now - t0) / dur)
        vp.scrollLeft = start + diff * ease(p)
        if (p < 1) requestAnimationFrame(step)
      }
      requestAnimationFrame(step)
    }, 120)
  }

  const sel = settings.symbols || []
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
        {mode === 'demo' && (
          <span className="mini-tag demo-float" title="币安连接失败，当前为模拟随机数据，非真实行情">演示</span>
        )}
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
            <div className="demo-actions">
              <button type="button" className="demo-btn" onClick={() => runDemo("single")}>
                模拟触发
              </button>
              <button type="button" className="demo-btn" onClick={() => runDemo("multi")}>
                多币同时
              </button>
              <button type="button" className="demo-btn" onClick={() => runDemo("variety")}>
                不同形态
              </button>
            </div>
          </div>
        </div>
      <div className="row3">
        <QuantPanel snapshot={quantSnap} events={visibleEvents} chartColors={chartColors}
          reservedRows={reservedRows} streamRef={streamRef}
          onToggle={(v) => liveRef.current && liveRef.current.sendSettings({ quant: { enabled: v } })} />
      </div>
      </aside>

      {/* 设置弹窗 */}
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)}
        section={settingsSection} onSectionChange={setSettingsSection}
        ui={ui} onUpdateUi={updateUi}
        patternsEnabled={patternsEnabled} livePreview={livePreview}
        onTogglePattern={togglePattern} onToggleLivePreview={toggleLivePreview}
        accounts={srvAccounts} onSaveAccount={saveAccount} />

      {/* 命中飞线：K线卡片 → 形态卡片，落地后触发卡片背景扩散 */}
      <HitFlight flight={flight} onLand={onFlightLand} onDone={onFlightDone} />

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
