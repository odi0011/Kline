import assert from 'node:assert/strict'
import { test } from 'node:test'
import { LiveClient } from './live.js'

function documentStub() {
  const listeners = new Set()
  globalThis.document = {
    hidden: false,
    addEventListener: (_name, callback) => listeners.add(callback),
    removeEventListener: (_name, callback) => listeners.delete(callback),
  }
  return listeners
}

test('hidden tab stores new ledger events and resumes once; close removes listener', () => {
  const listeners = documentStub()
  const live = new LiveClient('ws://localhost')
  let events = 0, resumed = 0
  live.on('quant_event', () => events++)
  live.on('resume', () => resumed++)
  document.hidden = true
  for (let i = 0; i < 3000; i++) live._dispatch({ type: 'quant_event', event: { id: i, ts: i } })
  assert.equal(events, 0)
  assert.equal(live.quantEvents.length, 2000)
  assert.equal(live.quantEvents.at(-1).id, 2999)
  document.hidden = false
  for (const callback of listeners) callback()
  assert.equal(resumed, 1)
  live.close()
  assert.equal(listeners.size, 0)
  assert.equal(live.subs.size, 0)
  delete globalThis.document
})

test('candle store is bounded during prolonged updates', () => {
  const live = new LiveClient('ws://localhost')
  for (let i = 0; i < 5000; i++) live._dispatch({ type: 'candle', symbol: 'btcusdt', interval: '1m', bar: [i, 1, 2, 0, 1, 3] })
  assert.equal(live.store.btcusdt['1m'].bars.length, 1200)
  assert.equal(live.store.btcusdt['1m'].bars.at(-1).time, 4999)
  live.close()
})

test('request acknowledgement completes while page is hidden', async () => {
  documentStub()
  document.hidden = true
  const live = new LiveClient('ws://localhost')
  let sent
  live.ws = { readyState: 1, send: (data) => { sent = JSON.parse(data) }, close() {} }
  const request = live.request({ type: 'agent_action', action: 'create' })
  live._dispatch({ type: 'agent_ack', requestId: sent.requestId, result: { ok: true, id: 'task' } })
  assert.deepEqual(await request, { ok: true, id: 'task' })
  assert.equal(live.requests.size, 0)
  live.close()
  delete globalThis.document
})

test('disconnect and capacity reject requests without orphaned timers', async () => {
  const live = new LiveClient('ws://localhost')
  assert.equal((await live.request({ type: 'agent_action' })).ok, false)
  live.ws = { readyState: 1, send() {}, close() {} }
  const pending = Array.from({ length: 16 }, () => live.request({ type: 'agent_action' }))
  assert.equal((await live.request({ type: 'agent_action' })).ok, false)
  live.close()
  assert.ok((await Promise.all(pending)).every((result) => !result.ok))
  assert.equal(live.requests.size, 0)
})
