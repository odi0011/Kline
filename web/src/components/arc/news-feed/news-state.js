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
  return {
    content, partial,
    error: typeof result?.error === "string" && result.error
      ? result.error : (!result?.ok || !content ? "正文读取失败；有摘要时仅显示摘要。" : ""),
  }
}

export const ANALYSIS_MODES = [
  { mode: "interpret", label: "AI 解读" },
  { mode: "impact", label: "影响分析" },
  { mode: "risk", label: "风险核查" },
]
export const analysisKey = (item, mode) => `${newsKey(item)}\u0000${mode}`
export const taskById = (tasks, id) => id == null ? undefined : tasks.find((task) => String(task.id) === String(id))
export const taskBusy = (entry, task) => Boolean(entry?.pending || (entry?.id != null &&
  (!task || !["succeeded", "completed", "failed", "cancelled", "canceled"].includes(task.status))))

// Lifetime belongs to the feed, not the modal. Closing a reader never aborts a task.
export function createAnalysisTracker(publish) {
  const entries = new Map()
  return {
    async submit(item, mode, content, callback, tasks = []) {
      const key = analysisKey(item, mode)
      const existing = entries.get(key)
      if (taskBusy(existing, taskById(tasks, existing?.id))) return
      const update = (entry) => { entries.set(key, entry); publish(new Map(entries)) }
      update({ pending: true })
      try {
        const response = await callback(item, mode, content)
        if (!response?.ok || response.id == null || response.id === "") {
          throw new Error(response?.error || "后台未返回任务 ID，请重试。")
        }
        update({ pending: false, id: response.id })
      } catch (error) {
        update({ pending: false, error: error?.message || "AI 任务提交失败，请重试。" })
      }
    },
  }
}
