/* 页面性能探针：量真实帧率、长任务、WebSocket 消息体积。
   用法：node tests/perf-probe.mjs http://154.36.184.56:8000/ [秒数] */
import { spawn } from "node:child_process"
import { mkdtemp } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

const base = process.argv[2] || "http://127.0.0.1:8000/"
const seconds = Number(process.argv[3] || 10)
const delay = (ms) => new Promise((r) => setTimeout(r, ms))
const profile = await mkdtemp(join(tmpdir(), "kline-perf-"))
const PORT = 9336
const chrome = spawn(process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe", [
  "--headless=new", "--hide-scrollbars", "--no-first-run",
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
  "--window-size=1440,1000", "about:blank",
], { stdio: "ignore" })

let socket
try {
  let targets
  const end = Date.now() + 15000
  while (!targets && Date.now() < end) {
    try { targets = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json() } catch { await delay(120) }
  }
  const page = targets.find((t) => t.type === "page")
  socket = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => {
    socket.addEventListener("open", res, { once: true })
    socket.addEventListener("error", rej, { once: true })
  })
  let seq = 0
  const pending = new Map()
  socket.addEventListener("message", (e) => {
    const m = JSON.parse(e.data)
    if (!m.id) return
    const q = pending.get(m.id); if (!q) return
    pending.delete(m.id)
    m.error ? q.reject(new Error(JSON.stringify(m.error))) : q.resolve(m.result)
  })
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++seq; pending.set(id, { resolve, reject })
    socket.send(JSON.stringify({ id, method, params }))
  })
  const evaluate = async (expr) => {
    const r = await cdp("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true })
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails))
    return r.result.value
  }

  await cdp("Page.enable")
  await cdp("Runtime.enable")
  await cdp("Page.addScriptToEvaluateOnNewDocument", { source: `
    window.__perf = { longTasks: [], wsBytes: 0, wsCount: 0, frames: 0 };
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) window.__perf.longTasks.push(Math.round(e.duration));
      }).observe({ entryTypes: ['longtask'] });
    } catch {}
    const NativeWS = window.WebSocket;
    window.WebSocket = class extends NativeWS {
      constructor(...a) {
        super(...a);
        this.addEventListener('message', (ev) => {
          window.__perf.wsCount += 1;
          window.__perf.wsBytes += (ev.data?.length || 0);
        });
      }
    };
    (function countFrames() {
      window.__perf.frames += 1;
      requestAnimationFrame(countFrames);
    })();
  ` })
  await cdp("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false })
  await cdp("Page.navigate", { url: base })
  await delay(2500)
  await evaluate(`window.__perf.frames = 0; window.__perf.longTasks = []; window.__perf.wsBytes = 0; window.__perf.wsCount = 0;`)
  await delay(seconds * 1000)

  const stats = await evaluate(`(() => {
    const p = window.__perf;
    const lt = p.longTasks.slice().sort((a,b)=>a-b);
    const canvas = document.querySelectorAll('canvas');
    return {
      fps: Math.round(p.frames / ${seconds}),
      longTaskCount: lt.length,
      longTaskMax: lt.length ? lt[lt.length-1] : 0,
      longTaskTotal: lt.reduce((a,b)=>a+b,0),
      wsCount: p.wsCount,
      wsKbPerSec: Math.round(p.wsBytes / ${seconds} / 1024),
      canvasCount: canvas.length,
      nodes: document.querySelectorAll('*').length,
    };
  })()`)
  console.log(JSON.stringify(stats, null, 1))
} catch (e) {
  console.error("ERROR", e.message)
} finally {
  try { socket?.close() } catch {}
  chrome.kill()
}
