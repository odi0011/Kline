import assert from "node:assert/strict"
import { spawn } from "node:child_process"

// Requires the local Vite fixture server; no browser package or remote scripts.
const base = process.env.AGENT_TEST_URL || "http://127.0.0.1:5173/agent-test.html"
const chrome = spawn(process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", [
  "--headless=new", "--no-first-run", "--remote-debugging-port=0", "about:blank",
], { stdio: ["ignore", "ignore", "pipe"] })
let socket
const errors = []
const results = []
try {
  const endpoint = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Chrome startup timeout")), 15000)
    chrome.once("error", reject)
    chrome.stderr.on("data", (chunk) => {
      const match = String(chunk).match(/DevTools listening on (ws:\/\/[^\s]+)/)
      if (match) { clearTimeout(timer); resolve(match[1]) }
    })
  })
  socket = new WebSocket(endpoint)
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }) })
  let seq = 0, sessionId
  const pending = new Map()
  socket.addEventListener("message", ({ data }) => {
    const message = JSON.parse(data)
    if (message.method === "Runtime.exceptionThrown") errors.push(message.params.exceptionDetails.text)
    const request = pending.get(message.id)
    if (!request) return
    pending.delete(message.id)
    clearTimeout(request.timer)
    message.error ? request.reject(new Error(JSON.stringify(message.error))) : request.resolve(message.result)
  })
  const cdp = (method, params = {}, browser = false) => new Promise((resolve, reject) => {
    const id = ++seq
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)) }, 15000)
    pending.set(id, { resolve, reject, timer })
    socket.send(JSON.stringify({ id, method, params, ...(!browser && sessionId ? { sessionId } : {}) }))
  })
  const { targetId } = await cdp("Target.createTarget", { url: "about:blank" }, true)
  ;({ sessionId } = await cdp("Target.attachToTarget", { targetId, flatten: true }, true))
  await cdp("Page.enable")
  await cdp("Runtime.enable")
  await cdp("Emulation.setDeviceMetricsOverride", { width: 900, height: 760, deviceScaleFactor: 1, mobile: false })
  const evaluate = async (expression) => {
    const result = await cdp("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true })
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails))
    return result.result.value
  }
  const waitFor = async (expression) => {
    const end = Date.now() + 6000
    while (Date.now() < end) {
      try { if (await evaluate(expression)) return } catch { /* Navigation replaces the execution context. */ }
      await new Promise((resolve) => setTimeout(resolve, 30))
    }
    throw new Error(`Timed out: ${expression}`)
  }
  const navigate = async (query = "") => {
    await cdp("Page.navigate", { url: `${base}?${query}` })
    await waitFor(`location.search === ${JSON.stringify(`?${query}`)} && !!window.__agentFixture && !!document.querySelector('section')`)
  }
  const frame = () => evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
  const clickText = async (text, selector = "button") => {
    await evaluate(`(() => { const el = Array.from(document.querySelectorAll(${JSON.stringify(selector)})).find(el => el.textContent.trim() === ${JSON.stringify(text)}); if (!el) throw Error('Missing button'); el.focus(); el.click() })()`)
    await frame()
  }
  const tab = (text) => clickText(text, '[role="tab"]')
  const set = async (selector, value) => {
    await evaluate(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, ${JSON.stringify(value)}); el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true })) })()`)
    await frame()
  }
  const prompt = 'textarea[aria-label="向 ODDD 提问"]'
  const send = () => evaluate(`document.querySelector('button[aria-label="发送"]').click()`)
  const count = () => evaluate("window.__agentFixture.requests.length")
  const check = async (name, test) => { await test(); results.push(name) }

  await check("ack failure retains chat; double submit guarded", async () => {
    await navigate("scenario=empty&fail=chat&ackDelay=400")
    await set(prompt, "保留这条输入")
    await evaluate(`(() => { const form = document.querySelector('form'); form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })()`)
    assert.equal(await count(), 1)
    assert.equal(await evaluate(`document.querySelector(${JSON.stringify(prompt)}).value`), "保留这条输入")
    await waitFor("document.body.innerText.includes('服务端拒绝')")
    assert.equal(await evaluate(`document.querySelector(${JSON.stringify(prompt)}).value`), "保留这条输入")
    assert.equal(await evaluate("document.querySelector('[role=tab][aria-selected=true]').textContent"), "对话")
  })
  await check("ack success clears only after confirmation", async () => {
    await navigate("scenario=empty&ackDelay=400")
    await set(prompt, "检查市场")
    await send()
    assert.equal(await evaluate(`document.querySelector(${JSON.stringify(prompt)}).value`), "检查市场")
    await waitFor(`document.querySelector(${JSON.stringify(prompt)}).value === ''`)
    await waitFor("document.body.innerText.includes('已完成后台分析')")
  })
  await check("IME, absent model and disconnected cannot send", async () => {
    await navigate("scenario=empty")
    await set(prompt, "中文输入")
    await evaluate(`(() => { const el = document.querySelector(${JSON.stringify(prompt)}); el.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true })); el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); el.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true })); el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true })) })()`)
    assert.equal(await count(), 0)
    for (const scenario of ["no-model", "offline", "models-loading", "models-error"]) {
      await navigate(`scenario=${scenario}`)
      await set(prompt, "不要发送")
      await evaluate(`document.querySelector(${JSON.stringify(prompt)}).dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))`)
      await send()
      assert.equal(await count(), 0)
      assert.equal(await evaluate("document.querySelector('button[aria-label=发送]').disabled"), true)
    }
  })
  await check("create failure keeps form and selected tab", async () => {
    await navigate("fail=create")
    await tab("计划")
    await set('input[placeholder="例如：每日风险巡检"]', "保留计划")
    await set("textarea[required]", "保留指令")
    await clickText("开始运行")
    await waitFor("document.body.innerText.includes('服务端拒绝')")
    assert.equal(await evaluate("document.querySelector('textarea[required]').value"), "保留指令")
    assert.equal(await evaluate("document.querySelector('[role=tab][aria-selected=true]').textContent"), "计划")
  })
  await check("future one-shot creation and edit payload", async () => {
    await navigate("scenario=empty")
    await tab("计划")
    await set('input[placeholder="例如：每日风险巡检"]', "未来单次")
    await set("textarea[required]", "只执行一次")
    const date = new Date(Date.now() + 3600000)
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
    await set('input[type="datetime-local"]', local)
    await clickText("创建计划")
    await waitFor("document.querySelector('[role=tab][aria-selected=true]').textContent === '任务'")
    let payload = await evaluate("window.__agentFixture.requests[0].payload")
    assert.equal(payload.intervalSec, 0)
    assert.equal(payload.startAt, new Date(local).getTime())
    await clickText("编辑")
    await set('input[placeholder="例如：每日风险巡检"]', "修改后的计划")
    await set("form select", "3600")
    await clickText("保存计划")
    await waitFor("window.__agentFixture.requests.length === 2 && document.body.innerText.includes('修改后的计划')")
    payload = await evaluate("window.__agentFixture.requests[1].payload")
    assert.equal(payload.title, "修改后的计划")
    assert.equal(payload.intervalSec, 3600)
    assert.equal(typeof payload.id, "string")
    assert.equal(payload.startAt, new Date(local).getTime())
  })
  await check("scheduled recurring run, tool details and history", async () => {
    await navigate("width=204")
    await tab("任务")
    await evaluate("Array.from(document.querySelectorAll('button[aria-expanded]')).find(el => el.textContent.includes('持仓风险巡检')).click()")
    await frame()
    await clickText("历史结果 · 2", "summary")
    assert.equal(await evaluate("document.body.innerText.includes('数据源超时')"), false)
    await evaluate("Array.from(document.querySelectorAll('summary')).find(el => el.textContent.includes('失败')).click()")
    await frame()
    assert.equal(await evaluate("document.body.innerText.includes('数据源超时')"), true)
    await clickText("立即运行")
    assert.equal(await evaluate("window.__agentFixture.requests[0].action"), "run")
    await waitFor("document.body.innerText.includes('后台分析完成')")
  })
  await check("stop reply sends cancel_chat with id", async () => {
    await navigate("scenario=empty")
    await set(prompt, "测试停止")
    await send()
    await waitFor("Array.from(document.querySelectorAll('button')).some(el => el.textContent === '停止回复' && !el.disabled)")
    await clickText("停止回复")
    await waitFor("document.body.innerText.includes('已停止')")
    const action = await evaluate("window.__agentFixture.requests[1]")
    assert.equal(action.action, "cancel_chat")
    assert.equal(typeof action.payload.id, "string")
  })
  await check("native delete dialog focus, Esc, failed ack", async () => {
    await navigate("fail=delete")
    await tab("任务")
    await evaluate("Array.from(document.querySelectorAll('button[aria-expanded]')).find(el => el.textContent.includes('交易复盘')).click()")
    await frame()
    await clickText("删除")
    assert.equal(await evaluate("document.activeElement.textContent"), "保留")
    await cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 })
    await cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 })
    await waitFor("!document.querySelector('dialog')")
    assert.equal(await evaluate("document.activeElement.textContent"), "删除")
    await clickText("删除")
    await clickText("删除", "dialog button")
    await waitFor("document.querySelector('dialog').innerText.includes('服务端拒绝')")
    assert.equal(await count(), 1)
    assert.equal(await evaluate("document.querySelector('dialog').open"), true)
  })
  await check("successful deletion returns focus to tasks tab", async () => {
    await navigate("scenario=empty")
    await evaluate("window.__agentFixture.setTasks([{id:'delete-me',title:'删除目标',status:'succeeded',kind:'once',prompt:'测试',result:''}])")
    await tab("任务")
    await evaluate("document.querySelector('[class*=taskHead]').click()")
    await frame()
    await clickText("删除")
    await clickText("删除", "dialog button")
    await waitFor("!document.querySelector('dialog') && !document.querySelector('[class*=taskHead]')")
    assert.equal(await evaluate("document.activeElement.getAttribute('role')"), "tab")
  })
  await check("long result remains in column with optional reading dialog", async () => {
    await navigate("scenario=empty")
    await evaluate("window.__agentFixture.setChat([{id:'read-more',role:'assistant',status:'succeeded',content:'完整结果'.repeat(400)}])")
    await frame()
    await clickText("展开阅读")
    assert.equal(await evaluate("document.querySelector('dialog p').textContent.length"), 1600)
    assert.equal(await evaluate("document.activeElement.textContent"), "关闭")
    await cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 })
    await cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 })
    await waitFor("!document.querySelector('dialog')")
    assert.equal(await evaluate("document.activeElement.textContent"), "展开阅读")
  })
  await check("scroll follows only at bottom and restores across tabs", async () => {
    await navigate("scenario=long-chat&height=430")
    await waitFor("document.querySelector('[class*=messageViewport]').scrollTop > 0")
    await evaluate("(() => { const el = document.querySelector('[class*=messageViewport]'); el.scrollTop = 40; el.dispatchEvent(new Event('scroll')) })()")
    await frame()
    await evaluate("window.__agentFixture.setChat(items => [...items, {id:'new',role:'assistant',content:'新的分析',status:'succeeded'}])")
    await frame()
    assert.equal(await evaluate("document.querySelector('[class*=messageViewport]').scrollTop"), 40)
    await tab("任务")
    await tab("对话")
    assert.equal(await evaluate("document.querySelector('[class*=messageViewport]').scrollTop"), 40)
    await clickText("回到底部")
    await evaluate("window.__agentFixture.setChat(items => [...items, {id:'new2',role:'assistant',content:'继续分析'.repeat(30),status:'succeeded'}])")
    await frame()
    assert.equal(await evaluate("(() => {const el = document.querySelector('[class*=messageViewport]'); return el.scrollHeight - el.scrollTop - el.clientHeight < 2})()"), true)
  })
  await check("locked token is session-only; failure never echoes it", async () => {
    await navigate("scenario=locked&fail=unlock")
    await set('input[type="password"]', "do-not-echo-this-token")
    await clickText("解锁")
    await waitFor("document.body.innerText.includes('解锁失败')")
    assert.equal(await evaluate("document.body.innerText.includes('do-not-echo-this-token')"), false)
    await navigate("scenario=locked")
    await set('input[type="password"]', "fixture-token")
    await clickText("解锁")
    await waitFor("!!document.querySelector('[role=tab]')")
    assert.equal(await evaluate("sessionStorage.getItem('oddd-fixture-session-token')"), "fixture-token")
    assert.equal(await evaluate("localStorage.getItem('oddd-fixture-session-token')"), null)
  })
  await check("160–220px column has no horizontal overflow", async () => {
    for (const width of [160, 180, 220]) {
      await navigate(`width=${width + 24}&height=520`)
      for (const name of ["对话", "任务", "计划"]) {
        await tab(name)
        assert.equal(await evaluate("(() => { const root = document.querySelector('section'); return root.scrollWidth <= root.clientWidth && Array.from(root.querySelectorAll('form, input, textarea, select')).every(el => el.getBoundingClientRect().right <= root.getBoundingClientRect().right) })()"), true, `${width}: ${name}`)
      }
    }
  })
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ passed: results.length, tests: results, errors }, null, 2))
} finally {
  socket?.close()
  chrome.kill()
}
