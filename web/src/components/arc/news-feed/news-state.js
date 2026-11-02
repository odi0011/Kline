export const ROW_H = 22
export const newsKey = (item) => String(item.url || `${item.source || ""}|${item.ts || 0}|${item.title || ""}`)
const timestamp = (item) => Number.isFinite(Number(item.ts)) ? Number(item.ts) : 0

// Do not mutate server snapshots; preserve input order for equal timestamps.
export function sortNews(items = []) {
  return [...items].sort((a, b) => timestamp(a) - timestamp(b))
}

export function scrollAnchor(list, top) {
  const index = Math.max(0, Math.floor(top / ROW_H))
  return { key: list[index] && newsKey(list[index]), index, offset: top % ROW_H }
}

export function anchoredTop(list, anchor, current) {
  if (!anchor) return current
  const index = list.findIndex((item) => newsKey(item) === anchor.key)
  // If retention removed the anchor, stay near the old position, never jump to latest.
  return index < 0 ? current : index * ROW_H + anchor.offset
}

export function syncViewport(node, list, follow, anchor) {
  const max = Math.max(0, node.scrollHeight - node.clientHeight)
  node.scrollTop = follow ? max : Math.max(0, Math.min(max, anchoredTop(list, anchor, node.scrollTop)))
  return { top: node.scrollTop, height: node.clientHeight, anchor: scrollAnchor(list, node.scrollTop) }
}

export function readResult(result, summary = "") {
  const hasContent = typeof result?.content === "string" && Boolean(result.content.trim())
  const content = hasContent ? result.content : summary
  const partial = !result?.ok || result?.partial !== false || !hasContent
  // pending = 正文还在后台抓，不是错误。用户看到的是"抓取中"，不是一次失败。
  const pending = result?.pending === true
  return {
    content, partial, pending,
    error: pending ? ""
      : typeof result?.error === "string" && result.error
        ? result.error : (!result?.ok || !content ? "正文读取失败；有摘要时仅显示摘要。" : ""),
  }
}

// AI 分析已改为「正文入库时由服务端生成、随正文一起下发」，
// 前端不再提交分析任务，原先的 modes/key/tracker 一并移除。
