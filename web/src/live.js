// WS 客户端 + 数据仓库：消息同步写入 store（卡片挂载时读全量），再分发给订阅者。

function toBar(a) {
  return {
    time: a[0], open: a[1], high: a[2], low: a[3], close: a[4], volume: a[5],
    buyVol: a[6] || 0, sellVol: a[7] || 0,
  }
}

export class LiveClient {
  constructor(url) {
    this.url = url
    this.ws = null
    this.store = {}          // symbol -> interval -> { bars: [] }
    this.structures = {}     // symbol -> interval -> 结构分析
    this.depths = {}         // symbol -> 订单簿深度快照（真实挂单）
    this.meta = null
    this.hits = []
    this.subs = new Map()    // key -> Set<fn>
    this.listeners = { meta: [], stats: [], status: [] }
    this._retry = 0
    this._closed = false
    this._lastStatus = 'reconnecting'
    this.agentLocked = true
    this.agentReadOnly = false
    this.agentPersistenceError = ''
    this.requests = new Map()
    this._requestId = 0
    this._retryTimer = null
    this._onVisibility = () => {
      if (document.hidden || this._closed) return
      this._emitStatus(this._lastStatus)
      this._emitKey('resume', {})
    }
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this._onVisibility)
    }
  }

  connect() {
    this._closed = false
    try {
      this.ws = new WebSocket(this.url)
    } catch {
      this._scheduleRetry()
      return
    }
    this.ws.onopen = () => {
      this._retry = 0
      this._emitStatus('connected')
      this.agentLocked = true
      let token = ''
      try { token = sessionStorage.getItem('oddd-access') || '' } catch {}
      if (token) this.unlockAgent(token)
      if (this.detectInterval) this.setDetectInterval(this.detectInterval)   // 重连后重新下发检测周期
      if (this.quantSymbols) this.setQuantSymbols(this.quantSymbols)         // 重连后重新下发量化范围
    }
    this.ws.onmessage = (ev) => {
      try { this._dispatch(JSON.parse(ev.data)) } catch { /* 忽略坏帧 */ }
    }
    this.ws.onclose = () => {
      this._rejectRequests('连接已断开，请重试')
      this.agentLocked = true
      this._emitKey('agent_access', { locked: true })
      if (this._closed) return
      this._emitStatus('reconnecting')
      this._scheduleRetry()
    }
    this.ws.onerror = () => this.ws.close()
  }

  close() {
    this._closed = true
    clearTimeout(this._retryTimer)
    this._rejectRequests('连接已关闭')
    if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', this._onVisibility)
    this.ws?.close()
    this.subs.clear()
    this.listeners = { meta: [], stats: [], status: [] }
  }

  _scheduleRetry() {
    clearTimeout(this._retryTimer)
    const delay = Math.min(1000 * 2 ** this._retry++, 10000)
    this._retryTimer = setTimeout(() => { if (!this._closed) this.connect() }, delay)
  }

  _dispatch(m) {
    if (m.requestId && this.requests.has(m.requestId)) {
      const pending = this.requests.get(m.requestId)
      clearTimeout(pending.timer)
      this.requests.delete(m.requestId)
      pending.resolve(m.result || { ok: false, error: '无效响应' })
    }
    if (m.type === 'agent_access') {
      this.agentLocked = m.locked !== false
      this._emitKey('agent_access', m)
    } else if (m.type === 'init') {
      for (const sym of m.symbols) {
        const cell = (this.store[sym] ||= {})
        for (const iv of m.intervals) {
          cell[iv] = { bars: (m.history[sym]?.[iv] || []).map(toBar) }
        }
      }
      if (m.structures) {
        for (const [sym, byIv] of Object.entries(m.structures)) {
          this.structures[sym] = { ...(this.structures[sym] || {}) }
          for (const [iv, st] of Object.entries(byIv)) {
            this.structures[sym][iv] = { ...st, __bars: (this.store[sym]?.[iv]?.bars || []).map((b) => ({ time: b.time })) }
          }
        }
      }
      this.meta = m
      if (typeof document === 'undefined' || !document.hidden) this.listeners.meta.forEach((f) => f(m))
      this._emitKey('init', m)
    } else if (m.type === 'structure') {
      const sym = m.symbol
      const iv = m.interval
      this.structures[sym] = { ...(this.structures[sym] || {}) }
      this.structures[sym][iv] = {
        ...m.structure,
        __bars: (this.store[sym]?.[iv]?.bars || []).map((b) => ({ time: b.time })),
      }
      this._emitKey(`structure|${sym}|${iv}`, this.structures[sym][iv])
      this._emitKey(`structure|${sym}`, this.structures[sym][iv])
      this._emitKey('structure', { symbol: sym, interval: iv, structure: this.structures[sym][iv] })
    } else if (m.type === 'candle') {
      const cell = (this.store[m.symbol] ||= {})[m.interval] ||= { bars: [] }
      const bars = cell.bars
      const b = toBar(m.bar)
      const last = bars[bars.length - 1]
      if (last && last.time === b.time) Object.assign(last, b)
      else bars.push(b)
      if (bars.length > 1200) bars.splice(0, bars.length - 1200)
      this._emitKey(`candle|${m.symbol}|${m.interval}`, b)
    } else if (m.type === 'hit') {
      this.hits.unshift(m)
      if (this.hits.length > 50) this.hits.pop()
      this._emitKey(`hit|${m.symbol}`, m)
      this._emitKey('hit', m)
    } else if (m.type === 'pattern_stats') {
      this.patternStats = m.stats
      this._emitKey('pattern_stats', m)
    } else if (m.type === 'bar_detail') {
      this._emitKey(`bar_detail|${m.symbol}`, m)
    } else if (m.type === 'accounts') {
      this.accounts = m.accounts
      this._emitKey('accounts', m)
    } else if (m.type === 'settings') {
      this.settings = m.settings
      this._emitKey('settings', m)
    } else if (m.type === 'quant') {
      this.quant = m.snapshot
      this._emitKey('quant', m)
    } else if (m.type === 'agent_state') {
      this.agentTasks = m.tasks || []
      this.agentChat = m.chat || []
      this.agentReadOnly = m.readOnly === true
      this.agentPersistenceError = m.persistenceError || ''
      this._emitKey('agent_state', m)
    } else if (m.type === 'agent_ack') {
      this._emitKey('agent_ack', m)
    } else if (m.type === 'depths') {
      this.depths = m.depths || {}
      this._emitKey('depths', m)
    } else if (m.type === 'quant_events') {
      this.quantEvents = m.events || []
      this._emitKey('quant_events', m)
    } else if (m.type === 'quant_trades') {
      this.quantTrades = m.trades || []
      this._emitKey('quant_trades', m)
    } else if (m.type === 'quant_trade') {
      this.quantTrades = [m.trade, ...(this.quantTrades || [])].slice(0, 500)
      this._emitKey('quant_trade', m.trade)
    } else if (m.type === 'news') {
      this.news = m.items || []
      this._emitKey('news', m)
    } else if (m.type === 'ai_models') {
      this._emitKey('ai_models', m)
    } else if (m.type === 'ai_test') {
      this._emitKey('ai_test', m)
    } else if (m.type === 'ai_meta') {
      this._emitKey('ai_meta', m)
    } else if (m.type === 'quant_event') {
      const event = m.event
      if (!event) return
      const key = event.id ?? `${event.ts}-${event.reason}`
      const events = this.quantEvents || []
      if (!events.some((item) => (item.id ?? `${item.ts}-${item.reason}`) === key)) {
        this.quantEvents = [...events, { ...event, id: key }].slice(-2000)
      }
      this._emitKey('quant_event', event)
    } else if (m.type === 'stats') {
      if (typeof document === 'undefined' || !document.hidden) this.listeners.stats.forEach((f) => f(m))
    }
  }

  on(key, fn) {
    const set = this.subs.get(key) || new Set()
    set.add(fn)
    this.subs.set(key, set)
    return () => set.delete(fn)
  }

  onMeta(fn) { this.listeners.meta.push(fn) }
  onStats(fn) { this.listeners.stats.push(fn) }
  onStatus(fn) { this.listeners.status.push(fn) }

  /** 告知服务端当前检测周期（前端设置的默认周期），断线重连后自动重发 */
  setDetectInterval(iv) {
    this.detectInterval = iv
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({ type: "set_detect_interval", interval: iv }))
    }
  }

  /** 告知服务端量化引擎的交易范围（当前展示的币种），重连后自动重发 */
  setQuantSymbols(symbols) {
    this.quantSymbols = Array.isArray(symbols) ? symbols : []
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({ type: "set_quant_symbols", symbols: this.quantSymbols }))
    }
  }

  /** 查询某根K线的存储记录（形态命中 + K线数据） */
  queryBar(symbol, interval, openTime) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({ type: "query_bar", symbol, interval, open_time: openTime }))
    }
  }

  /** 下发设置（账户/形态/外观/AI）：服务端热生效并持久化到 config.yaml */
  sendSettings(settings) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({ type: "set_settings", settings }))
    }
  }

  /** 通用指令下发（AI 列模型/测连通等一次性请求） */
  send(msg) {
    if (this.ws && this.ws.readyState === 1 && msg && msg.type) {
      this.ws.send(JSON.stringify(msg))
      return true
    }
    return false
  }

  request(msg, timeout = 25000) {
    if (!this.ws || this.ws.readyState !== 1) return Promise.resolve({ ok: false, error: '尚未连接服务端' })
    if (this.requests.size >= 16) return Promise.resolve({ ok: false, error: '请求过多，请稍后重试' })
    const requestId = String(++this._requestId)
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.requests.delete(requestId)
        resolve({ ok: false, error: '请求超时，请检查任务列表后再重试' })
      }, timeout)
      this.requests.set(requestId, { resolve, timer })
      if (!this.send({ ...msg, requestId })) this._rejectRequests('连接已断开')
    })
  }

  async unlockAgent(token) {
    const result = await this.request({ type: 'agent_auth', token })
    this.agentLocked = !result.ok
    try {
      if (result.ok) sessionStorage.setItem('oddd-access', token)
      else sessionStorage.removeItem('oddd-access')
    } catch {}
    this._emitKey('agent_access', { locked: this.agentLocked })
    return result
  }

  _rejectRequests(error) {
    for (const { resolve, timer } of this.requests.values()) {
      clearTimeout(timer)
      resolve({ ok: false, error })
    }
    this.requests.clear()
  }

  _emitKey(key, payload) {
    const control = /^(ai_|agent_access|agent_ack|settings|bar_detail)/.test(key)
    if (!control && typeof document !== 'undefined' && document.hidden) return
    this.subs.get(key)?.forEach((f) => {
      try { f(payload) } catch (e) { console.error(e) }
    })
  }

  _emitStatus(s) {
    this._lastStatus = s
    if (typeof document !== 'undefined' && document.hidden) return
    this.listeners.status.forEach((f) => f(s))
  }
}
