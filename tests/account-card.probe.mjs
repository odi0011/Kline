/* 账户卡高度探针：量各区块的真实高度，定位"有仓位时被挤"的具体位置。
   用 CDP 直连（不依赖 playwright 包），与 tests/dashboard.browser.mjs 同一套。

   用法：node tests/account-card.probe.mjs https://od1.site/ */
import { spawn } from "node:child_process"
import { mkdtemp } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

const delay = (ms) => new Promise((r) => setTimeout(r, ms))
const url = process.argv[2] || "https://od1.site/"
const profile = await mkdtemp(join(tmpdir(), "kline-card-"))
const PORT = 9247

const chrome = spawn(process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", [
  "--headless=new", "--hide-scrollbars", "--no-first-run",
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
  "--window-size=1600,1000", "about:blank",
], { stdio: "ignore" })

let socket
try {
  let targets
  const deadline = Date.now() + 10000
  while (!targets && Date.now() < deadline) {
    try { targets = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json() }
    catch { await delay(120) }
  }
  if (!targets) throw new Error("Chrome did not start")
  const page = targets.find((t) => t.type === "page")
  socket = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true })
    socket.addEventListener("error", reject, { once: true })
  })
  let seq = 0
  const pending = new Map()
  socket.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data)
    if (!msg.id) return
    const req = pending.get(msg.id)
    if (!req) return
    pending.delete(msg.id)
    msg.error ? req.reject(new Error(JSON.stringify(msg.error))) : req.resolve(msg.result)
  })
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++seq
    pending.set(id, { resolve, reject })
    socket.send(JSON.stringify({ id, method, params }))
  })

  await cdp("Page.enable")
  await cdp("Runtime.enable")
  await cdp("Page.navigate", { url })
  await delay(11000)

  const expr = `(() => {
    const pick = (s) => document.querySelector(s);
    const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect();
      return { h: Math.round(r.height), top: Math.round(r.top), bottom: Math.round(r.bottom) }; };
    const card = pick('[aria-label="账户"]');
    const kids = card ? [...card.children].map((el) => ({
      tag: el.tagName,
      cls: String(el.className || "").split(" ").filter(Boolean).slice(-1)[0],
      ...box(el),
    })) : [];
    const ov = card ? card.querySelectorAll('[role="group"][aria-label="账户风险"], [aria-label="自检统计"]') : [];
    return JSON.stringify({
      card: box(card),
      cardScroll: card ? { scrollH: card.scrollHeight, clientH: card.clientHeight } : null,
      clipped: card ? card.scrollHeight > card.clientHeight + 1 : null,
      kids,
      wallet: box(pick(".wallet-zone")),
      topRow: box(pick(".top-row")),
      hasPosition: !!pick('[aria-label="当前持仓"]'),
    }, null, 2);
  })()`
  const res = await cdp("Runtime.evaluate", { expression: expr, returnByValue: true })
  console.log(res.result.value)
} finally {
  try { socket?.close() } catch {}
  chrome.kill()
}
