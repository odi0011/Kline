import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react"
import {
  Activity, AgentTabs, Button, ChatThread, DeleteDialog, ModelSelect, PromptBar,
  ReadingDialog, ResultText, TaskRow, isWorking, textOf,
} from "./agent-controls"
import styles from "./agent-panel.module.css"

const QUICK_TASKS = [
  { title: "市场诊断", prompt: "分析当前监控币种的趋势、动量与波动，指出重要变化及风险。" },
  { title: "持仓复盘", prompt: "复盘当前持仓与最近交易，检查盈亏、回撤和集中度，给出改进建议。" },
  { title: "系统巡检", prompt: "检查引擎、数据健康、风控与近期错误，列出异常及优先级。" },
]
const SCHEDULES = [
  { value: 0, label: "单次" }, { value: 300, label: "每 5 分钟" },
  { value: 900, label: "每 15 分钟" }, { value: 3600, label: "每 1 小时" }, { value: 86400, label: "每 1 天" },
]
const emptyDraft = () => ({ title: "", prompt: "", intervalSec: 0, startAt: "" })
const localDate = (timestamp) => {
  if (!timestamp || timestamp <= Date.now()) return ""
  const date = new Date(timestamp)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}

// onSend(message) -> Promise<{ok,id?,task?,error?}>; no optimistic mutations.
// onUnlock owns sessionStorage persistence; this component never persists tokens.
export default function AgentPanel({ tasks = [], chat = [], models = [], onSend, onUnlock,
  locked = false, connected = true, modelsLoading = false, modelsError = "", loading = false,
  error: stateError = "", readOnly = false }) {
  const prefix = useId()
  const [tab, setTab] = useState("chat")
  const [prompt, setPrompt] = useState("")
  const [model, setModel] = useState("")
  const [expanded, setExpanded] = useState(null)
  const [deleting, setDeleting] = useState(null)
  const [reading, setReading] = useState(null)
  const [editing, setEditing] = useState(null)
  const [draft, setDraft] = useState(emptyDraft)
  const [token, setToken] = useState("")
  const [pending, setPending] = useState("")
  const [error, setError] = useState("")
  const [following, setFollowing] = useState(true)
  const pendingRef = useRef(false)
  const mounted = useRef(true)
  const scrollRef = useRef(null)
  const scrollPosition = useRef(0)
  const followRef = useRef(true)
  const taskItems = Array.isArray(tasks) ? tasks : []
  const messages = Array.isArray(chat) ? chat : []
  const activeModels = useMemo(() => (Array.isArray(models) ? models : []).filter((item) => item?.id && item.enabled !== false), [models])
  const modelState = modelsLoading || models == null ? "模型加载中" : modelsError ? "模型加载失败" : !activeModels.length ? "未配置模型" : ""
  const hasModel = !modelState && activeModels.some((item) => item.id === model)
  const running = taskItems.filter((task) => task.status === "running").length
  const chatBusy = messages.some((message) => message.role === "assistant" && isWorking(message.status))
  const unavailable = !connected || locked || readOnly || !onSend || loading
  const disabled = unavailable || !!pending
  const canSubmit = !disabled && hasModel

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  useEffect(() => {
    if (!activeModels.some((item) => item.id === model)) setModel((activeModels.find((item) => item.active) || activeModels[0])?.id || "")
  }, [activeModels, model])
  useEffect(() => { if (!locked) setToken("") }, [locked])

  useLayoutEffect(() => {
    const node = scrollRef.current
    if (!node || tab !== "chat") return
    node.scrollTop = followRef.current ? node.scrollHeight : scrollPosition.current
  }, [chat, tab, locked])
  const onScroll = (event) => {
    const node = event.currentTarget
    scrollPosition.current = node.scrollTop
    followRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 20
    setFollowing(followRef.current)
  }
  const follow = () => {
    followRef.current = true
    setFollowing(true)
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }

  const request = async (action, payload, success) => {
    if (pendingRef.current || unavailable) return
    pendingRef.current = true
    setPending(action)
    setError("")
    try {
      const result = await onSend({ type: "agent_action", action, payload })
      if (!result?.ok) throw new Error(textOf(result?.error) || "未确认，请重试")
      if (mounted.current) success?.(result)
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : "操作失败，请重试")
    } finally {
      pendingRef.current = false
      if (mounted.current) setPending("")
    }
  }
  const sendChat = () => {
    if (!prompt.trim() || !canSubmit || chatBusy) return
    request("chat", { prompt: prompt.trim(), model }, () => { setPrompt(""); follow() })
  }
  const createQuickTask = (task) => {
    if (!canSubmit) return
    request("create", { ...task, model, intervalSec: 0 }, (ack) => {
      setExpanded(ack.id || ack.task?.id || null)
      setTab("tasks")
    })
  }
  const newTask = () => { setEditing(null); setDraft(emptyDraft()); setTab("schedule"); setError("") }
  const editTask = (task) => {
    setEditing(task.id)
    setDraft({ title: task.title || "", prompt: task.prompt || "", intervalSec: task.intervalSec || 0, startAt: localDate(task.nextRunAt || task.startAt) })
    setError("")
    setTab("schedule")
  }
  const submitSchedule = (event) => {
    event.preventDefault()
    if (!draft.prompt.trim() || disabled || (!editing && !hasModel)) return
    const startAt = draft.startAt ? new Date(draft.startAt).getTime() : Date.now()
    if (!Number.isFinite(startAt) || (draft.startAt && startAt <= Date.now())) { setError("请选择未来时间"); return }
    const payload = { title: draft.title.trim(), prompt: draft.prompt.trim(), intervalSec: Number(draft.intervalSec), startAt }
    request(editing ? "edit" : "create", editing ? { ...payload, id: editing } : { ...payload, model }, (ack) => {
      setExpanded(editing || ack.id || ack.task?.id || null)
      setDraft(emptyDraft())
      setEditing(null)
      setTab("tasks")
    })
  }
  const unlock = async (event) => {
    event.preventDefault()
    if (!token.trim() || !connected || !onUnlock || pendingRef.current) return
    pendingRef.current = true
    setPending("unlock")
    setError("")
    try {
      const result = await onUnlock(token.trim())
      if (!result?.ok) throw new Error("令牌未通过，请重试")
      if (mounted.current) setToken("")
    } catch {
      if (mounted.current) setError("解锁失败，请检查令牌和连接")
    } finally {
      pendingRef.current = false
      if (mounted.current) setPending("")
    }
  }
  const panelProps = (name) => ({ role: "tabpanel", id: `${prefix}-panel-${name}`, "aria-labelledby": `${prefix}-tab-${name}` })

  return <section className={styles.root} aria-label="ODDD 个人交易 Agent">
    <header className={styles.header}>
      <span className={styles.identity}><img src="/icon-32.png" alt="" className={styles.avatar} /><strong>ODDD</strong></span>
      <span className={styles.headerActions}>
        {running > 0 && <span className={styles.running} title={`${running} 个任务运行中`}><i />{running}</span>}
        <Button variant="quiet" className={styles.newTask} aria-label="新建后台任务" disabled={!!pending || locked} onClick={newTask}>+</Button>
      </span>
    </header>
    {!connected && <p className={styles.notice} role="status">连接已断开</p>}
    {(stateError || readOnly) && <p className={styles.error} role="alert">{textOf(stateError) || "当前只读"}</p>}
    {error && !deleting && <p className={styles.error} role="alert">{error}</p>}
    {locked ? <form className={styles.unlock} onSubmit={unlock} aria-busy={pending === "unlock"}>
      <label className={styles.field}><span>服务端访问令牌</span><input type="password" autoComplete="off" aria-label="服务端访问令牌" value={token} readOnly={!!pending} onChange={(event) => setToken(event.target.value)} required /></label>
      <Button type="submit" variant="primary" disabled={!token.trim() || !connected || !!pending || !onUnlock}>{pending ? "验证中" : "解锁"}</Button>
    </form> : <>
      <AgentTabs value={tab} onChange={setTab} running={running} prefix={prefix} />
      {loading && <p role="status" className={styles.notice}>加载中</p>}
      {tab === "chat" && <div className={styles.chatView} {...panelProps("chat")}>
        <ChatThread messages={messages} viewportRef={scrollRef} onScroll={onScroll} empty={!loading && <div className={styles.welcome}>
          <div className={styles.chips}>{QUICK_TASKS.map((item) => <Button key={item.title} className={styles.chip} disabled={!canSubmit} onClick={() => createQuickTask(item)}>{item.title}</Button>)}</div>
        </div>} renderMessage={(message) => <>
          {message.role === "assistant" && <Activity steps={message.steps} progress={message.progress} status={message.status} />}
          <ResultText text={message.content} title="ODDD" onRead={setReading} />
          {message.status === "failed" && <p className={styles.error}>{textOf(message.error) || "回复失败，请重试"}</p>}
          {message.status === "cancelled" && <p className={styles.muted}>已停止</p>}
          {message.role === "assistant" && message.status === "succeeded" && !message.content && <p className={styles.muted}>无回复内容</p>}
          {message.role === "assistant" && isWorking(message.status) && <Button disabled={disabled} onClick={() => request("cancel_chat", { id: message.id })}>停止回复</Button>}
        </>} />
        {!following && <Button variant="quiet" className={styles.follow} onClick={follow}>回到底部</Button>}
        <div className={styles.promptWrap}><PromptBar value={prompt} onChange={setPrompt} onSend={sendChat} busy={chatBusy || !!pending}
          disabled={unavailable || !hasModel} models={activeModels} model={model} onModelChange={setModel} modelState={modelState} /></div>
      </div>}
      {tab === "tasks" && <div className={styles.taskViewport} {...panelProps("tasks")}>
        {!taskItems.length && !loading && <div className={styles.empty}><p>暂无任务</p><Button onClick={newTask}>新建任务</Button></div>}
        {taskItems.map((task) => <TaskRow key={task.id} task={task} expanded={expanded === task.id}
          onToggle={() => setExpanded(expanded === task.id ? null : task.id)} disabled={disabled} canRun={hasModel}
          onAction={(action) => request(action, { id: task.id })} onEdit={() => editTask(task)} onRead={setReading}
          onDelete={() => { setError(""); setDeleting(task) }} />)}
      </div>}
      {tab === "schedule" && <div className={styles.scheduleViewport} {...panelProps("schedule")}>
        <form className={styles.scheduleForm} onSubmit={submitSchedule} aria-busy={!!pending}>
          {editing && <div className={styles.editHeader}><span>编辑计划</span><Button variant="quiet" onClick={newTask} disabled={!!pending}>取消</Button></div>}
          <fieldset disabled={!!pending}>
            <label className={styles.field}><span>任务名称</span><input value={draft.title} maxLength={80} placeholder="例如：每日风险巡检" onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
            <label className={styles.field}><span>执行指令</span><textarea value={draft.prompt} rows={3} required maxLength={6000} placeholder="要分析什么…" onChange={(event) => setDraft({ ...draft, prompt: event.target.value })} /></label>
            <label className={styles.field}><span>运行方式</span><select value={draft.intervalSec} onChange={(event) => setDraft({ ...draft, intervalSec: Number(event.target.value) })}>
              {!SCHEDULES.some((item) => item.value === draft.intervalSec) && <option value={draft.intervalSec}>每 {draft.intervalSec} 秒</option>}
              {SCHEDULES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select></label>
            <label className={styles.field}><span>开始时间 · 留空即刻</span><input type="datetime-local" aria-label="开始时间" value={draft.startAt} onChange={(event) => setDraft({ ...draft, startAt: event.target.value })} /></label>
            {!editing && <label className={styles.field}><span>使用模型</span><ModelSelect models={activeModels} value={model} onChange={setModel} state={modelState} disabled={!!modelState} label="任务模型" /></label>}
            {modelState && !editing && <p role="status" className={styles.muted}>{modelState}</p>}
            <Button type="submit" variant="primary" className={styles.startTask} disabled={!draft.prompt.trim() || disabled || (!editing && !hasModel)}>{pending ? "提交中" : editing ? "保存计划" : draft.startAt ? "创建计划" : "开始运行"}</Button>
          </fieldset>
        </form>
      </div>}
    </>}
    {deleting && <DeleteDialog task={deleting} busy={pending === "delete"} disabled={disabled} error={error}
      onClose={() => { setDeleting(null); setError("") }} onConfirm={() => request("delete", { id: deleting.id }, () => { setDeleting(null); setExpanded(null) })} />}
    {reading && <ReadingDialog result={reading} onClose={() => setReading(null)} />}
  </section>
}
