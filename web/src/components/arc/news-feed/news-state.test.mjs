import test from "node:test"
import assert from "node:assert/strict"
import { ROW_H, analysisKey, createAnalysisTracker, readResult, scrollAnchor, sortNews, syncViewport, taskById } from "./news-state.js"

const item = (ts, title = String(ts)) => ({ ts, title, url: `https://www.coindesk.com/${title}` })
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test("ascending stable sort does not mutate snapshot", () => {
  const input = [item(3), item(1, "a"), item(1, "b"), item(2)]
  assert.deepEqual(sortNews(input).map((x) => x.title), ["a", "b", "2", "3"])
  assert.equal(input[0].ts, 3)
  assert.equal(sortNews([item("bad"), item(2)])[0].ts, "bad")
})

test("initial layout and new data follow bottom, including empty first load", () => {
  const node = { scrollTop: 0, clientHeight: 220, scrollHeight: 0 }
  syncViewport(node, [], true, null)
  assert.equal(node.scrollTop, 0)
  const list = Array.from({ length: 100 }, (_, i) => item(i))
  node.scrollHeight = list.length * ROW_H
  syncViewport(node, list, true, null)
  assert.equal(node.scrollTop, 1980)
  list.push(item(100))
  node.scrollHeight += ROW_H
  syncViewport(node, list, true, null)
  assert.equal(node.scrollTop, 2002)
})

test("resize follows only when following, preserving user's reading anchor", () => {
  const list = Array.from({ length: 100 }, (_, i) => item(i))
  const node = { scrollTop: 1980, clientHeight: 330, scrollHeight: 2200 }
  syncViewport(node, list, true, null)
  assert.equal(node.scrollTop, 1870)
  node.scrollTop = 445
  const anchor = scrollAnchor(list, node.scrollTop)
  node.clientHeight = 110
  syncViewport(node, list, false, anchor)
  assert.equal(node.scrollTop, 445)
  node.scrollHeight += 220
  syncViewport(node, list, false, anchor)
  assert.equal(node.scrollTop, 445)
})

test("same-size rolling updates retain identity and row offset while scrolled up", () => {
  const list = Array.from({ length: 100 }, (_, i) => item(i))
  const node = { scrollTop: 445, clientHeight: 220, scrollHeight: 2200 }
  const anchor = scrollAnchor(list, node.scrollTop)
  const next = [...list.slice(1), item(100)]
  syncViewport(node, next, false, anchor)
  assert.equal(node.scrollTop, 423)
  assert.equal(scrollAnchor(next, node.scrollTop).key, anchor.key)
  // Explicit latest action resumes follow.
  syncViewport(node, next, true, anchor)
  assert.equal(node.scrollTop, 1980)
})

test("removed anchors and a shrinking list clamp without negative windows", () => {
  const node = { scrollTop: 440, clientHeight: 220, scrollHeight: 220 }
  syncViewport(node, [item(9)], false, { key: "removed", index: 20, offset: 0 })
  assert.equal(node.scrollTop, 0)
})

test("summary fallback cannot be labeled full, malformed response is safe", () => {
  assert.deepEqual(readResult({ ok: false, error: "offline" }, "summary"), { content: "summary", partial: true, error: "offline" })
  assert.equal(readResult({ ok: true, content: "", partial: false }, "summary").partial, true)
  assert.equal(readResult({ ok: true, content: 123, partial: false }, "summary").partial, true)
  assert.equal(readResult({ ok: true, content: "body", partial: false }).partial, false)
})

test("AI is deduplicated synchronously before the first response", async () => {
  let entries = new Map(), calls = 0
  const gate = deferred()
  const tracker = createAnalysisTracker((state) => { entries = state })
  const article = item(1)
  const callback = (it, mode, body) => {
    calls++
    assert.equal(it, article)
    assert.equal(mode, "interpret")
    assert.equal(body, "full body")
    return gate.promise
  }
  const first = tracker.submit(article, "interpret", "full body", callback)
  await tracker.submit(article, "interpret", "full body", callback)
  assert.equal(calls, 1)
  assert.equal(entries.get(analysisKey(article, "interpret")).pending, true)
  gate.resolve({ ok: true, id: "server-1" })
  await first
  assert.equal(entries.get(analysisKey(article, "interpret")).id, "server-1")
})

test("out-of-order submissions and closing a modal preserve correct task IDs", async () => {
  let entries = new Map()
  const tracker = createAnalysisTracker((state) => { entries = state })
  const first = deferred(), second = deferred()
  const a = item(1), b = item(2)
  const pendingA = tracker.submit(a, "impact", "A", () => first.promise)
  const pendingB = tracker.submit(b, "risk", "B", () => second.promise)
  // No modal lifecycle or cancellation callback exists in the tracker.
  second.resolve({ ok: true, id: "task-b" })
  await pendingB
  first.resolve({ ok: true, id: "task-a" })
  await pendingA
  const tasks = [{ id: "unrelated", status: "running" }, { id: "task-a", status: "succeeded", result: "real A" }, { id: "task-b", status: "running", progress: 37 }]
  assert.equal(taskById(tasks, entries.get(analysisKey(a, "impact")).id).result, "real A")
  assert.equal(taskById(tasks, entries.get(analysisKey(b, "risk")).id).progress, 37)
  assert.equal(taskById(tasks, "not-broadcast-yet"), undefined)
})

test("submission errors/missing IDs are shown and can be retried", async () => {
  let entries = new Map()
  const article = item(1), key = analysisKey(article, "risk")
  const tracker = createAnalysisTracker((state) => { entries = state })
  await tracker.submit(article, "risk", "body", () => Promise.reject(new Error("offline")))
  assert.equal(entries.get(key).error, "offline")
  await tracker.submit(article, "risk", "body", async () => ({ ok: true }))
  assert.match(entries.get(key).error, /ID/)
  await tracker.submit(article, "risk", "body", async () => ({ ok: true, id: 9 }))
  assert.equal(entries.get(key).id, 9)
})

test("only terminal server state permits resubmission, not a modal reopen", async () => {
  let calls = 0
  const tracker = createAnalysisTracker(() => {})
  const article = item(1)
  const callback = async () => ({ ok: true, id: `id-${++calls}` })
  await tracker.submit(article, "interpret", "body", callback)
  await tracker.submit(article, "interpret", "body", callback, [])
  await tracker.submit(article, "interpret", "body", callback, [{ id: "id-1", status: "running" }])
  assert.equal(calls, 1)
  await tracker.submit(article, "interpret", "body", callback, [{ id: "id-1", status: "succeeded" }])
  assert.equal(calls, 2)
})
