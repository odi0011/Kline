/* 账户卡截图：用 CDP 直连无头 Chrome，只截账户卡区域。
   用法：node tests/account-card.shot.mjs

   为什么单独一个脚本：账户卡有仓位/无仓位是两套高度，
   改布局后必须看着真实渲染结果验收，不能只看 CSS。 */
import { spawn } from "node:child_process"
import { mkdtemp, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

const delay = (ms) => new Promise((r) => setTimeout(r, ms))
const profile = await mkdtemp(join(tmpdir(), "kline-shot-"))
const PORT = 9251
// 用正斜杠：Windows 路径里的反斜杠在 JS 字符串里会被当转义符吃掉
const CHROME = process.env.CHROME_PATH
  || "C:/Program Files/Google/Chrome/Application/chrome.exe"
const chrome = spawn(CHROME, [
  "--headless=new", "--hide-scrollbars", "--no-first-run",
  "--remote-debugging-port=" + PORT,
  "--user-data-dir=" + profile,
  "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
  "--window-size=1600,1000", "about:blank",
], { stdio: "ignore" })

let socket
try {
  let targets
  const deadline = Date.now() + 10000
  while (!targets && Date.now() < deadline) {
    try {
      targets = await (await fetch("http://127.0.0.1:" + PORT + "/json")).json()
    } catch {
      await delay(120)
    }
  }
  const page = targets.find((t) => t.type === "page")
  socket = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => {
    socket.addEventListener("open", res, { once: true })
    socket.addEventListener("error", rej, { once: true })
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
  const cdp = (method, params = {}) => new Promise((res, rej) => {
    const id = ++seq
    pending.set(id, { resolve: res, reject: rej })
    socket.send(JSON.stringify({ id, method, params }))
  })

  await cdp("Page.enable")
  await cdp("Runtime.enable")
  await cdp("Page.navigate", { url: process.argv[2] || "https://od1.site/" })
  await delay(11000)

  const probe = await cdp("Runtime.evaluate", {
    expression: [
      "(() => {",
      "  const card = document.querySelector('[aria-label=\"账户\"]');",
      "  if (!card) return null;",
      "  const b = card.getBoundingClientRect();",
      "  return { x: b.x, y: b.y, width: b.width, height: b.height,",
      "           scrollH: card.scrollHeight, clientH: card.clientHeight };",
      "})()",
    ].join("\n"),
    returnByValue: true,
  })
  const box = probe.result.value
  if (!box) throw new Error("账户卡没渲染出来")

  const shot = await cdp("Page.captureScreenshot", {
    format: "png",
    clip: { x: box.x - 4, y: box.y - 4, width: box.width + 8, height: box.height + 8, scale: 2 },
  })
  await writeFile("card.png", Buffer.from(shot.data, "base64"))
  console.log("saved card.png", JSON.stringify(box))
} finally {
  try { socket?.close() } catch {}
  chrome.kill()
}
