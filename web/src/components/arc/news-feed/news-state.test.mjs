import test from "node:test"
import assert from "node:assert/strict"
import { ROW_H, readResult, scrollAnchor, sortNews, syncViewport } from "./news-state.js"

const item = (ts, title = String(ts)) => ({ ts, title, url: `https://www.coindesk.com/${title}` })

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
  assert.deepEqual(readResult({ ok: false, error: "offline" }, "summary"),
    { content: "summary", partial: true, pending: false, error: "offline" })
  assert.equal(readResult({ ok: true, content: "", partial: false }, "summary").partial, true)
  assert.equal(readResult({ ok: true, content: 123, partial: false }, "summary").partial, true)
  assert.equal(readResult({ ok: true, content: "body", partial: false }).partial, false)
})

test("正文还在后台抓取时是 pending，不是错误", () => {
  // 服务端只回了摘要并标记 pending：界面应显示"抓取中"，不能报错也不能提示重试
  const out = readResult({ ok: true, content: "摘要", partial: true, pending: true, error: "正文仍在后台抓取" }, "摘要")
  assert.equal(out.pending, true)
  assert.equal(out.error, "")
  assert.equal(out.partial, true)
  // 非 pending 的部分正文仍然要报出限制原因
  assert.equal(readResult({ ok: true, content: "摘要", partial: true, error: "仅摘要" }, "").error, "仅摘要")
})

