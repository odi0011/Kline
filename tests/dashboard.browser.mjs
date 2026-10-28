import { spawn } from 'node:child_process'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import assert from 'node:assert/strict'

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const profile = await mkdtemp(join(tmpdir(), 'kline-browser-'))
const chrome = spawn(process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', [
  '--headless=new', '--hide-scrollbars', '--no-first-run', '--remote-debugging-port=9231',
  `--user-data-dir=${profile}`, '--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader', 'about:blank',
], { stdio: 'ignore' })
let socket
try {
  let targets
  const startupDeadline = Date.now() + 10000
  while (!targets && Date.now() < startupDeadline) {
    try { targets = await (await fetch('http://127.0.0.1:9231/json')).json() }
    catch { await delay(120) }
  }
  if (!targets) throw new Error('Isolated Chrome did not start')
  const page = targets.find((target) => target.type === 'page')
  socket = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true })
    socket.addEventListener('error', reject, { once: true })
  })
  let sequence = 0
  const pending = new Map(), errors = []
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data)
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text)
    if (message.method === 'Log.entryAdded' && message.params.entry.level === 'error') errors.push(message.params.entry.text)
    if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') errors.push(message.params.args.map(a => a.description || a.value).join(' '))
    if (!message.id) return
    const request = pending.get(message.id)
    if (!request) return
    pending.delete(message.id)
    if (message.error) request.reject(new Error(JSON.stringify(message.error)))
    else request.resolve(message.result)
  })
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence
    pending.set(id, { resolve, reject })
    socket.send(JSON.stringify({ id, method, params }))
  })
  const evaluate = async (expression) => {
    const result = await cdp('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails))
    return result.result.value
  }
  const until = async (expression, timeout = 12000) => {
    const deadline = Date.now() + timeout
    while (Date.now() < deadline) {
      if (await evaluate(expression)) return
      await delay(120)
    }
    console.log('Browser errors', errors)
    console.log('Browser state', await evaluate(`({text:document.body.innerText.slice(0,1500),live:!!window.__live,locked:window.__live?.agentLocked,status:window.__live?._lastStatus,accounts:window.__live?.accounts?.length})`))
    throw new Error(`Timed out: ${expression}`)
  }
  const capture = async (name) => {
    const shot = await cdp('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
    const path = join(tmpdir(), `${name}.png`)
    await writeFile(path, Buffer.from(shot.data, 'base64'))
    console.log('Screenshot', path)
  }
  await cdp('Page.enable')
  await cdp('Runtime.enable')
  await cdp('Log.enable')
  await cdp('Page.addScriptToEvaluateOnNewDocument', { source: `
    const NativeWebSocket = window.WebSocket;
    window.WebSocket = class extends NativeWebSocket {
      constructor(url, ...args) { super(String(url).replace(':8765', ':8877'), ...args); }
    };
    sessionStorage.setItem('oddd-access', 'local-browser-test-token-not-for-production');
  ` })
  await cdp('Emulation.setDeviceMetricsOverride', { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false })
  await cdp('Page.navigate', { url: 'http://127.0.0.1:5173/' })
  await until(`window.__live?.accounts?.length && window.__live?.agentLocked === false`)
  await delay(1200)
  await capture('dashboard-overview')
  const geometry = await evaluate(`(() => {
    const scene = document.querySelector('[aria-label="自动量化交易三维闭环"]');
    const agent = document.querySelector('[aria-label="ODDD 个人交易 Agent"]');
    return {scene:scene?.getBoundingClientRect().toJSON(), agent:agent?.getBoundingClientRect().toJSON(), canvases:document.querySelectorAll('canvas').length};
  })()`)
  console.log('Geometry', JSON.stringify(geometry))
  const newsViewport = `Array.from(document.querySelectorAll('[role="region"]')).find(e => /新闻|公告/.test(e.getAttribute('aria-label') || ''))`
  await until(`${newsViewport}?.scrollHeight > ${newsViewport}?.clientHeight`)
  assert.ok(await evaluate(`Math.abs(${newsViewport}.scrollHeight - ${newsViewport}.scrollTop - ${newsViewport}.clientHeight) < 2`), 'News starts at latest')
  await evaluate(`${newsViewport}.scrollTop = 0`)
  await delay(180)
  await until(`Array.from(document.querySelectorAll('button')).some(b => b.textContent.trim() === '最新')`)
  await evaluate(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '最新').click()`)
  await delay(450)
  await evaluate(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('测试新闻 79')).click()`)
  await until(`document.body.innerText.includes('测试正文')`)
  await capture('dashboard-news-dialog')
  await evaluate(`Array.from(document.querySelectorAll('dialog button,[role="dialog"] button')).find(b => /AI.*解读|^解读$/.test(b.textContent.trim())).click()`)
  await until(`document.body.innerText.includes('测试分析完成')`)
  await capture('dashboard-news-analysis')
  await cdp('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await cdp('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await delay(250)
  await evaluate(`(() => {
    const live=window.__live;
    const next=structuredClone(live.quant);
    const event={id:'browser-signal',kind:'signal',symbol:'ethusdt',direction:'bullish',score:88,price:2480,ts:Date.now(),reason:'评分跨越阈值'};
    next.analysis.liveEvaluations.ethusdt.score=88; next.analysis.liveEvaluations.ethusdt.ts=Date.now();
    live._dispatch({type:'quant',snapshot:next}); live._dispatch({type:'quant_event',event});
  })()`)
  await delay(1400)
  await capture('dashboard-signal-camera')
  await cdp('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })
  await delay(700)
  await capture('dashboard-1440')
  const overflow = await evaluate(`document.documentElement.scrollWidth > innerWidth + 2`)
  assert.equal(overflow, false, 'No horizontal page overflow')
  console.log(JSON.stringify({ errors, geometry, newsBottomFollow: true, articleRead: true, articleAnalysis: true }, null, 2))
  assert.deepEqual(errors, [])
} finally {
  socket?.close()
  chrome.kill()
}
