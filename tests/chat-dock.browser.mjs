/* 聊天窗渲染验证（打真实站点）：多气泡、发送键、表情包。

   为什么必须跑真实浏览器：
   - 后端 split_reply 落成多条消息 ≠ 前端显示成多个气泡。之前就是
     后端正确、前端把多段塞进同一个 .bubble，看着永远只有一条。
   - 发送键是否还是"停止"形态，只能看真实渲染出来的 DOM。
   - 表情包能不能显示，只能看 img 的 naturalWidth（HTTP 200 不代表能渲染）。

   用法：node tests/chat-dock.browser.mjs http://154.36.184.56:8000/
*/
import { spawn } from "node:child_process"
import { mkdtemp, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

const base = process.argv[2] || "http://127.0.0.1:8000/"
const delay = (ms) => new Promise((r) => setTimeout(r, ms))
const profile = await mkdtemp(join(tmpdir(), "kline-chat-"))
const PORT = 9334
const chrome = spawn(process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", [
  "--headless=new", "--hide-scrollbars", "--no-first-run",
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
  "about:blank",
], { stdio: "ignore" })

let socket
let failed = 0
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`)
  if (!ok) failed += 1
}

try {
  let targets
  const deadline = Date.now() + 15000
  while (!targets && Date.now() < deadline) {
    try { targets = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json() }
    catch { await delay(120) }
  }
  if (!targets) throw new Error("Chrome 没起来")
  const page = targets.find((t) => t.type === "page")
  socket = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => {
    socket.addEventListener("open", res, { once: true })
    socket.addEventListener("error", rej, { once: true })
  })

  let seq = 0
  const pending = new Map()
  const errors = []
  socket.addEventListener("message", (e) => {
    const m = JSON.parse(e.data)
    if (m.method === "Runtime.exceptionThrown") {
      errors.push(m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text)
    }
    if (!m.id) return
    const req = pending.get(m.id)
    if (!req) return
    pending.delete(m.id)
    m.error ? req.reject(new Error(JSON.stringify(m.error))) : req.resolve(m.result)
  })
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++seq
    pending.set(id, { resolve, reject })
    socket.send(JSON.stringify({ id, method, params }))
  })
  const evaluate = async (expr) => {
    const r = await cdp("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true })
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails))
    return r.result.value
  }
  const until = async (expr, timeout = 20000) => {
    const end = Date.now() + timeout
    while (Date.now() < end) {
      try { if (await evaluate(expr)) return true } catch {}
      await delay(150)
    }
    return false
  }

  await cdp("Page.enable")
  await cdp("Runtime.enable")
  await cdp("Emulation.setDeviceMetricsOverride", {
    width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
  })
  await cdp("Page.navigate", { url: base })

  // 对话面板现在是页面第二行的固定板块，不再需要点胶囊展开
  const ready = await until(`!!document.querySelector('[aria-label="ODDD 对话"]')`, 25000)
  check("页面加载并渲染对话面板", ready)
  const island = await evaluate(`!!document.querySelector('[aria-label="打开 ODDD 对话"]')`)
  check("没有折叠胶囊（已改为固定面板）", !island)
  await delay(1200)

  // 1) 不该有"停止"按钮
  const stopButtons = await evaluate(`
    Array.from(document.querySelectorAll('button')).filter(b =>
      /停止/.test((b.getAttribute('aria-label') || '') + ' ' + (b.title || ''))
    ).length`)
  check("没有停止按钮（发送键不做停止形态）", stopButtons === 0, `找到 ${stopButtons} 个`)

  // 2) 发送按钮存在且有图标
  const sendBtn = await evaluate(`
    (() => {
      const b = Array.from(document.querySelectorAll('button')).find(x =>
        (x.getAttribute('aria-label') || '') === '发送')
      return b ? { svg: !!b.querySelector('svg'), cls: b.className } : null
    })()`)
  check("存在发送按钮（箭头图标）", !!sendBtn?.svg, JSON.stringify(sendBtn || null))

  // 3) 聊天区里每个气泡是独立 DOM 节点（多气泡结构）
  const struct = await evaluate(`
    (() => {
      const dock = document.querySelector('[aria-label="ODDD 对话"]')
      if (!dock) return null
      // 气泡 = 有 data-role 的元素
      const rows = Array.from(dock.querySelectorAll('[data-role]'))
      // 统计"纯文本气泡"里包含多个 <p> 的（旧实现就是多段塞一个气泡）
      const multiP = rows.filter(r => r.querySelectorAll('p').length > 1).length
      return { roleNodes: rows.length, multiParagraphBubbles: multiP }
    })()`)
  check("聊天区有气泡", (struct?.roleNodes || 0) > 0, JSON.stringify(struct))
  check("没有「一个气泡塞多段」的旧结构",
    (struct?.multiParagraphBubbles || 0) === 0,
    JSON.stringify(struct))

  // 4) 表情包图片真的能渲染（naturalWidth > 0）
  const sticker = await evaluate(`
    (() => {
      const imgs = Array.from(document.querySelectorAll('img'))
        .filter(i => (i.src || '').includes('/agent-attachments/stickers/'))
      return imgs.map(i => ({ src: i.src.split('/').pop(), w: i.naturalWidth, h: i.naturalHeight }))
    })()`)
  if (sticker && sticker.length) {
    const broken = sticker.filter((s) => !s.w || !s.h)
    check("表情包图片全部可渲染", broken.length === 0,
      `共 ${sticker.length} 张，坏 ${broken.length} 张: ${JSON.stringify(broken)}`)
  } else {
    check("表情包图片全部可渲染", true, "本次页面里没有表情包（跳过）")
  }

  check("无 JS 运行异常", errors.length === 0, errors.slice(0, 2).join(" | "))

  const shot = await cdp("Page.captureScreenshot", { format: "png" })
  const path = join(tmpdir(), "chat-dock-live.png")
  await writeFile(path, Buffer.from(shot.data, "base64"))
  console.log("截图:", path)

  console.log(failed === 0 ? "\n全部通过" : `\n${failed} 项失败`)
  process.exitCode = failed === 0 ? 0 : 1
} catch (err) {
  console.error("ERROR", err.message)
  process.exitCode = 1
} finally {
  try { socket?.close() } catch {}
  chrome.kill()
}
