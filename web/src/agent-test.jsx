import { useEffect, useRef, useState } from "react"
import { createRoot } from "react-dom/client"
import AgentPanel from "./components/beautiful/agent/agent-panel"
import "./styles/globals.css"
import "./styles.css"

const now = Date.now()
const steps = [
  { id: "market-1", tool: "read_market", status: "succeeded", round: 1, startedAt: now - 7000, finishedAt: now - 6500, arguments: { symbols: ["BTCUSDT"] }, result: { trend: "震荡", volatility: "中等" } },
  { id: "risk-1", tool: "read_risk", status: "running", round: 2, startedAt: now - 1000 },
]
const completedSteps = steps.map((step) => ({ ...step, status: "succeeded", finishedAt: now, result: step.result || { drawdown: "1.8%", concentrated: true } }))
const initialTasks = [
  { id: "running", title: "全市场深度诊断", prompt: "扫描当前监控币种与风险变化", kind: "once", intervalSec: 0, status: "running", createdAt: now - 18000, nextRunAt: now, runCount: 0, result: "", steps, progress: { phase: "tool", round: 2, maxRounds: 8, toolCalls: 2 } },
  { id: "scheduled", title: "持仓风险巡检", prompt: "检查回撤、集中度与止损", kind: "recurring", intervalSec: 900, status: "scheduled", createdAt: now - 3600000, nextRunAt: now + 480000, runCount: 4, result: "上一轮风险可控。", steps: completedSteps, runs: [{ id: "run-1", status: "succeeded", finishedAt: now - 3600000, result: "上轮回撤 1.8%。", steps: completedSteps }, { id: "run-2", status: "failed", finishedAt: now - 1800000, error: "数据源超时", steps: [{ id: "failed-tool", tool: "read_market", status: "failed", error: "数据源超时" }] }] },
  { id: "done", title: "交易复盘", prompt: "复盘近期交易", kind: "once", intervalSec: 0, status: "succeeded", createdAt: now - 7200000, nextRunAt: now, runCount: 1, result: "结论：当前组合风险中性。\n\n1. 两笔盈利来自趋势延续。\n2. 回撤仍低于 5% 熔断线。\n3. 建议维持现有 ATR 止损，不追高加仓。", steps: completedSteps },
  { id: "failed", title: "公告摘要", prompt: "汇总交易所公告", kind: "once", intervalSec: 0, status: "failed", createdAt: now - 8600000, nextRunAt: now, runCount: 1, error: "模型连接超时，请稍后重试。", result: "" },
]
const initialChat = [
  { id: "u1", role: "user", content: "帮我看一下当前组合风险", ts: now - 30000 },
  { id: "a1", role: "assistant", content: "当前回撤为 1.8%，低于 5% 熔断线。主要风险是同方向持仓偏集中，建议暂不增加净敞口。", steps: completedSteps, status: "succeeded", ts: now - 28000 },
]
const configuredModels = [{ id: "deepseek-chat", label: "DeepSeek V3", active: true }, { id: "gpt-4o-mini", label: "GPT-4o mini" }]
const params = new URLSearchParams(location.search)
const scenario = params.get("scenario")
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function Harness() {
  const [tasks, setTasks] = useState(scenario === "empty" ? [] : initialTasks)
  const [chat, setChat] = useState(scenario === "empty" ? [] : scenario === "long-chat" ? Array.from({ length: 20 }, (_, i) => ({ id: `long-${i}`, role: i % 2 ? "assistant" : "user", content: `第 ${i + 1} 条分析\n${"行情波动需要关注仓位与风险。".repeat(8)}`, status: "succeeded" })) : initialChat)
  const [models, setModels] = useState(["no-model", "models-loading", "models-error"].includes(scenario) ? [] : configuredModels)
  const [connected, setConnected] = useState(scenario !== "offline")
  const [locked, setLocked] = useState(scenario === "locked")
  const requests = useRef([])
  const timers = useRef(new Set())
  const later = (callback, ms) => {
    const timer = setTimeout(() => { timers.current.delete(timer); callback() }, ms)
    timers.current.add(timer)
  }
  useEffect(() => {
    // Fixture-only hooks for browser regression tests; never imported by App.
    window.__agentFixture = { requests: requests.current, setTasks, setChat, setModels, setConnected, setLocked }
    return () => { timers.current.forEach(clearTimeout); delete window.__agentFixture }
  }, [])
  const finishTask = (id) => later(() => setTasks((current) => current.map((item) => {
    if (item.id !== id || item.status !== "running") return item
    const result = "后台分析完成：趋势分化加大，但组合回撤仍受控。建议保留现有风控参数。"
    const run = { id: `r-${Date.now()}`, status: "succeeded", finishedAt: Date.now(), result, steps: completedSteps }
    return { ...item, status: item.kind === "recurring" ? "scheduled" : "succeeded", nextRunAt: Date.now() + item.intervalSec * 1000, steps: completedSteps, progress: { phase: "succeeded", round: 2, maxRounds: 8, toolCalls: 2 }, runCount: item.runCount + 1, result, runs: [...(item.runs || []), run].slice(-10) }
  })), 650)
  const onSend = async ({ action, payload }) => {
    requests.current.push({ action, payload, requestId: `fixture-${requests.current.length + 1}` })
    await wait(Number(params.get("ackDelay")) || 180)
    if (params.get("reject") === action) throw new Error("测试：连接中断")
    if (params.get("fail") === action) return { ok: false, error: "测试：服务端拒绝，请重试" }
    if (!connected || locked) return { ok: false, error: "连接不可用" }
    if (action === "chat") {
      const id = `a${Date.now()}`
      setChat((current) => [...current, { id: `u${Date.now()}`, role: "user", content: payload.prompt }, { id, role: "assistant", content: "", status: "running", steps: [steps[0]], progress: { phase: "model", round: 1, maxRounds: 8, toolCalls: 1 } }])
      later(() => setChat((current) => current.map((item) => item.id === id && item.status === "running" ? { ...item, steps, progress: { phase: "tool", round: 2, maxRounds: 8, toolCalls: 2 } } : item)), 250)
      later(() => setChat((current) => current.map((item) => item.id === id && item.status === "running" ? { ...item, status: "succeeded", steps: completedSteps, content: "已完成后台分析。当前系统参数稳定，建议先观察 15 分钟，再决定是否调整风险敞口。" } : item)), 700)
      return { ok: true, id }
    }
    if (action === "cancel_chat") {
      setChat((current) => current.map((item) => item.id === payload.id ? { ...item, status: "cancelled", steps: (item.steps || []).map((step) => step.status === "running" ? { ...step, status: "cancelled" } : step) } : item))
      return { ok: true, id: payload.id }
    }
    if (action === "create") {
      const id = `t${Date.now()}`
      const task = { ...payload, id, title: payload.title || "市场诊断", kind: payload.intervalSec ? "recurring" : "once", intervalSec: payload.intervalSec || 0, status: payload.startAt > Date.now() ? "scheduled" : "running", createdAt: Date.now(), nextRunAt: payload.startAt || Date.now(), runCount: 0, result: "", steps: payload.startAt > Date.now() ? [] : steps }
      setTasks((current) => [task, ...current])
      if (task.status === "running") finishTask(id)
      return { ok: true, id, task }
    }
    if (action === "delete") {
      setTasks((current) => current.filter((item) => item.id !== payload.id))
      return { ok: true, id: payload.id }
    }
    setTasks((current) => current.map((item) => {
      if (item.id !== payload.id) return item
      if (action === "edit") return { ...item, ...payload, kind: payload.intervalSec ? "recurring" : "once", status: "scheduled", nextRunAt: payload.startAt }
      if (action === "pause") return { ...item, status: "paused" }
      if (action === "resume") return { ...item, status: "scheduled" }
      if (action === "cancel") return { ...item, status: "cancelled" }
      if (action === "run") return { ...item, status: "running", error: "", result: "", steps }
      return item
    }))
    if (action === "run") finishTask(payload.id)
    return { ok: true, id: payload.id }
  }
  const onUnlock = async (token) => {
    await wait(180)
    if (params.get("fail") === "unlock" || token !== "fixture-token") return { ok: false, error: "令牌无效" }
    sessionStorage.setItem("oddd-fixture-session-token", token)
    setLocked(false)
    return { ok: true }
  }
  const width = Number(params.get("width")) || 230
  const height = Number(params.get("height")) || 520
  return <div style={{ width, height, padding: 12, background: "#f4f6fa", boxSizing: "border-box" }}>
    <AgentPanel tasks={tasks} chat={chat} models={models} onSend={onSend} onUnlock={onUnlock} locked={locked} connected={connected}
      modelsLoading={scenario === "models-loading"} modelsError={scenario === "models-error" ? "模型获取失败" : ""}
      loading={scenario === "loading"} error={scenario === "error" ? "状态加载失败" : ""} />
  </div>
}
createRoot(document.getElementById("root")).render(<Harness />)
