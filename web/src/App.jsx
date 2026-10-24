import { useEffect, useRef, useState } from 'react'
import { LiveClient } from './live.js'
import CoinCard from './CoinCard.jsx'
import PatternGlyph from './PatternGlyph.jsx'
import { PATTERN_META, coinGradient } from './patterns_meta.js'
import { Notification, NotificationViewport } from '@/components/base/notification/notification'
import { Card } from '@/components/arc/card/card'
import { ScrollArea } from '@/components/arc/scroll-area/scroll-area'
import { GlassMenu } from '@/components/arc/glass-menu/glass-menu'
import { WalletStack } from '@/components/arc/wallet-stack/wallet-stack'
import { ComboChartCard } from '@/components/base/combo-chart-card/combo-chart-card'
import { NoiseBackground } from '@/components/ui/noise-background'
import { Badge } from '@/components/base/badges/badge'
import { AnimatedCounter } from '@/components/arc/animated-counter/animated-counter'
import CoinAvatar from '@/components/ui/coin-avatar'
import { SlidersHorizontal, Clock3, BarChart3, Grid3x3, MoveHorizontal, Coins } from 'lucide-react'

const WS_PORT = 8765
const HIT_TTL_MS = 9_000
const SETTINGS_KEY = 'kline.settings.v1'
const MIN_SYMBOLS = 4
const MAX_SYMBOLS = 10
const DEFAULT_SETTINGS = {
  interval: '3m', visibleBars: 70, showVolume: true, showGrid: true,
  symbols: ['btcusdt', 'tslausdt', 'xauusdt', 'xagusdt'],   // 默认展示的四个币种
}

function loadSettings() {
  try {
    return { ...DEFAULT_SETTINGS, ...(JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}) }
  } catch {
    return { ...DEFAULT_SETTINGS }
  }
}

const STATUS_BY_DIR = { bullish: 'success', bearish: 'error', neutral: 'information' }
const DIR_LABEL = { bullish: '上涨', bearish: '下跌', neutral: '其他' }
const fmtPrice = (p) => (p >= 1000 ? p.toFixed(1) : p >= 1 ? p.toFixed(3) : p.toPrecision(4))

// 账户收益图模拟数据（布局用，后续可接测试盘收益记录）
const comboWeek = [
  { label: "一", pnl: 12.4, cum: 12.4 }, { label: "二", pnl: -8.2, cum: 4.2 },
  { label: "三", pnl: 21.6, cum: 25.8 }, { label: "四", pnl: 5.1, cum: 30.9 },
  { label: "五", pnl: -3.4, cum: 27.5 }, { label: "六", pnl: 18.9, cum: 46.4 },
  { label: "日", pnl: 9.2, cum: 55.6 },
]
const comboMonth = [
  { label: "W1", pnl: 46.4, cum: 46.4 }, { label: "W2", pnl: -12.3, cum: 34.1 },
  { label: "W3", pnl: 38.8, cum: 72.9 }, { label: "W4", pnl: 24.5, cum: 97.4 },
]
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
  const liveRef = useRef(null)
  const cardsViewportRef = useRef(null)
  const settingsRef = useRef(settings)
  settingsRef.current = settings

  const connected = stats.status === 'connected'
  const mode = stats.mode || meta?.mode || ''
  const defaultIv = meta
    ? (meta.intervals.includes(settings.interval) ? settings.interval : meta.intervals[0])
    : settings.interval
  // 监控池 = 服务端 config symbols；展示 = 池 ∩ 用户选择（本地持久化）
  const pool = meta?.symbols || DEFAULT_SETTINGS.symbols
  const displayed = pool.filter((s) => settings.symbols.includes(s))
  const shownSymbols = displayed.length ? displayed : pool.slice(0, MIN_SYMBOLS)

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
    live.connect()
    return () => { offHit(); offStats(); offSymStats(); offAcc(); live.close() }
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
      id: 'g-chart',
      label: '图表',
      items: [
        { id: 'volume', label: '成交量', icon: <BarChart3 size={16} />, checked: settings.showVolume, keepOpen: true },
        { id: 'grid', label: '网格线', icon: <Grid3x3 size={16} />, checked: settings.showGrid, keepOpen: true },
      ],
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
  ]

  const onMenuSelect = (id) => {
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
            {meta && shownSymbols.map((s) => (
              <CoinCard key={s} symbol={s} intervals={meta.intervals}
                settings={{ ...settings, interval: defaultIv }}
                live={liveRef.current}
                hitActive={hits.some((h) => h.symbol === s)}
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
                  const gradBase = active ? active.symbol : 'btcusdt'
                  return (
                    <div className={`pattern-card ${active ? 'hit' : ''}`} data-key={key} key={key}>
                      {active ? (
                        <NoiseBackground className="pn-content" containerClassName="pn-container"
                          gradientColors={coinGradient(gradBase)} noiseIntensity={0.3} speed={0.09}>
                          <Card title={<span className="pc-title">{p.name}
                            <span className="cnt-mini" title={`累计触发 ${st.count} 次`}>
                              <AnimatedCounter value={st.count} decimals={0} />
                            </span>
                          </span>}
                            description={p.desc} media={<PatternGlyph candles={p.candles} />} />
                        </NoiseBackground>
                      ) : (
                        <Card title={<span className="pc-title">{p.name}
                          <span className="cnt-mini" title={`累计触发 ${st.count} 次`}>
                            <AnimatedCounter value={st.count} decimals={0} />
                          </span>
                        </span>}
                          description={p.desc} media={<PatternGlyph candles={p.candles} />} />
                      )}
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
            <WalletStack accounts={accounts} />
          </div>
        </div>
        <div className="row2">
          <ComboChartCard
            className="combo-card"
            title="账户收益（模拟数据）"
            data={comboWeek}
            bar={{ key: "pnl", label: "每日盈亏", format: (n) => `${n.toFixed(2)} U` }}
            line={{ key: "cum", label: "累计收益", format: (n) => `${n.toFixed(1)} U` }}
            delta={0.124}
            ranges={[
              { id: "week", label: "本周", data: comboWeek, delta: 0.124 },
              { id: "month", label: "本月", data: comboMonth, delta: 0.32 },
            ]}
            tiles
          />
          <div className="row2-rest" />
        </div>
      </aside>

      {/* 形态信号通知：左上角 */}
      <NotificationViewport position="top-left">
        {hits.map((h) => {
          return (
            <Notification
              key={h.id}
              status={STATUS_BY_DIR[h.direction] || 'information'}
              icon={function CoinIcon() { return <CoinAvatar symbol={h.symbol} size={34} /> }}
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
