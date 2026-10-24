import { useEffect, useMemo, useRef, useState } from 'react'
import { LiveClient } from './live.js'
import CoinCard from './CoinCard.jsx'
import PatternGlyph from './PatternGlyph.jsx'
import { PATTERN_META } from './patterns_meta.js'
import { Notification, NotificationViewport } from '@/components/base/notification/notification'
import { Card } from '@/components/arc/card/card'
import { ScrollArea } from '@/components/arc/scroll-area/scroll-area'
import { GlassMenu } from '@/components/arc/glass-menu/glass-menu'
import { WalletStack } from '@/components/arc/wallet-stack/wallet-stack'
import { Badge } from '@/components/base/badges/badge'
import { AnimatedCounter } from '@/components/arc/animated-counter/animated-counter'
import CoinAvatar from '@/components/ui/coin-avatar'
import SettingsDialog from '@/components/arc/settings-dialog/settings-dialog'
import AccountPanel from '@/components/arc/account-panel/account-panel'
import QuantPanel from '@/components/arc/quant-panel/quant-panel'
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
  const [frontName, setFrontName] = useState(null)   // 置前的账户（卡片点击）
  const [ui, setUi] = useState(DEFAULT_UI)           // 外观：服务端下发（config 持久化）
  const [patternsEnabled, setPatternsEnabled] = useState(null)  // null = 未从服务端加载
  const [livePreview, setLivePreview] = useState(true)
  const [srvAccounts, setSrvAccounts] = useState(null)          // 服务端账户列表（含密钥）
  const [quantSnap, setQuantSnap] = useState(null)              // 量化引擎快照
  const liveRef = useRef(null)
  const cardsViewportRef = useRef(null)
  const equityRef = useRef({})                       // 账户名 → [{t, bal, upnl}] 权益采样
  const settingsRef = useRef(settings)
  settingsRef.current = settings

  // 外观即时生效：主题色 → --accent（菜单高亮/徽标等点缀），字体 → 全局
  useEffect(() => {
    document.documentElement.style.setProperty('--accent', ui.themeColor)
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

  // 置前账户的持仓 → 左列排序：持仓币种排前，池内持仓但未展示的自动添加到其后
  const frontAcct = accounts.find((a) => a.name === frontName)
  const posSyms = frontAcct && Array.isArray(frontAcct.positions) ? frontAcct.positions.map((p) => p.symbol) : []
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

  // 第二行数据面板：置前账户（默认最后一张卡）
  const frontAccount = accounts.find((a) => a.name === (frontName || accounts[accounts.length - 1]?.name))
  const frontSeries = frontAccount ? equityRef.current[frontAccount.name] : undefined

  // 账户权益采样（每 30s 广播时记一个点，供卡片迷你曲线；本地保留最近 120 点）
  useEffect(() => {
    const now = Date.now()
    accounts.forEach((a) => {
      if (a.balance == null) return
      const arr = (equityRef.current[a.name] ||= [])
      const last = arr[arr.length - 1]
      if (!last || now - last.t > 25000) {
        arr.push({ t: now, bal: a.balance, upnl: a.unrealized ?? 0 })
        if (arr.length > 120) arr.shift()
      }
    })
  }, [accounts])

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
    live.connect()
    return () => { offHit(); offStats(); offSymStats(); offAcc(); offSettings(); offQuant(); live.close() }
  }, [])

  // 命中时：滚动到最新命中的形态卡片
  useEffect(() => {
    if (!hits.length) return
    scrollToPattern(hits[0].pattern)
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
  const addAccount = (acct) => {
    const next = [...(srvAccounts || []), acct]
    setSrvAccounts(next)
    liveRef.current && liveRef.current.sendSettings({ accounts: next })
  }
  const removeAccount = (name) => {
    const next = (srvAccounts || []).filter((a) => a.name !== name)
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
                  const active = hits.find((h) => h.pattern === key)
                    || (hoverPattern?.pattern === key ? { symbol: hoverPattern.symbol } : null)
                  const st = patternStats[key] || { count: 0, wins: 0, losses: 0 }
                  const decided = st.wins + st.losses
                  const winPct = decided > 0 ? Math.round((st.wins / decided) * 100) : null
                  const tier = decided > 0 ? (winPct >= 50 ? 'hi' : winPct >= 10 ? 'mid' : 'low') : null
                  return (
                    <div className={`pattern-card ${active ? 'hit' : ''}`} data-key={key} key={key}>
                      {active && <div className={`pn-hit-bg ${active.direction || p.dir}`} aria-hidden />}
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
            <WalletStack accounts={accounts} seriesMap={equityRef.current}
              chartColors={chartColors} onFrontChange={setFrontName} />
          </div>
        </div>
      <div className="row2">
        <AccountPanel account={frontAccount} series={frontSeries} chartColors={chartColors} />
      </div>
      <div className="row3">
        <QuantPanel snapshot={quantSnap} chartColors={chartColors}
          onToggle={(v) => liveRef.current && liveRef.current.sendSettings({ quant: { enabled: v } })} />
      </div>
      </aside>

      {/* 设置弹窗 */}
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)}
        ui={ui} onUpdateUi={updateUi}
        patternsEnabled={patternsEnabled} livePreview={livePreview}
        onTogglePattern={togglePattern} onToggleLivePreview={toggleLivePreview}
        accounts={srvAccounts} onAddAccount={addAccount} onRemoveAccount={removeAccount} />

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
