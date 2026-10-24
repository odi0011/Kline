// WS 客户端 + 数据仓库：消息同步写入 store（卡片挂载时读全量），再分发给订阅者。

function toBar(a) {
  return { time: a[0], open: a[1], high: a[2], low: a[3], close: a[4], volume: a[5] }
}

export class LiveClient {
  constructor(url) {
    this.url = url
    this.ws = null
    this.store = {}          // symbol -> interval -> { bars: [] }
    this.meta = null
    this.hits = []
    this.subs = new Map()    // key -> Set<fn>
    this.listeners = { meta: [], stats: [], status: [] }
    this._retry = 0
    this._closed = false
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
      if (this.detectInterval) this.setDetectInterval(this.detectInterval)   // 重连后重新下发检测周期
    }
    this.ws.onmessage = (ev) => {
      try { this._dispatch(JSON.parse(ev.data)) } catch { /* 忽略坏帧 */ }
    }
    this.ws.onclose = () => {
      if (this._closed) return
      this._emitStatus('reconnecting')
      this._scheduleRetry()
    }
    this.ws.onerror = () => this.ws.close()
  }

  close() {
    this._closed = true
    this.ws?.close()
  }

  _scheduleRetry() {
    const delay = Math.min(1000 * 2 ** this._retry++, 10000)
    setTimeout(() => { if (!this._closed) this.connect() }, delay)
  }

  _dispatch(m) {
    if (m.type === 'init') {
      for (const sym of m.symbols) {
        const cell = (this.store[sym] ||= {})
        for (const iv of m.intervals) {
          cell[iv] = { bars: (m.history[sym]?.[iv] || []).map(toBar) }
        }
      }
      this.meta = m
      this.listeners.meta.forEach((f) => f(m))
      this._emitKey('init', m)
    } else if (m.type === 'candle') {
      const cell = (this.store[m.symbol] ||= {})[m.interval] ||= { bars: [] }
      const bars = cell.bars
      const b = toBar(m.bar)
      const last = bars[bars.length - 1]
      if (last && last.time === b.time) Object.assign(last, b)
      else bars.push(b)
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
    } else if (m.type === 'stats') {
      this.listeners.stats.forEach((f) => f(m))
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

  /** 查询某根K线的存储记录（形态命中 + K线数据） */
  queryBar(symbol, interval, openTime) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({ type: "query_bar", symbol, interval, open_time: openTime }))
    }
  }

  _emitKey(key, payload) {
    this.subs.get(key)?.forEach((f) => {
      try { f(payload) } catch (e) { console.error(e) }
    })
  }

  _emitStatus(s) {
    this.listeners.status.forEach((f) => f(s))
  }
}
