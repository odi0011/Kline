/* AI 设置面板：服务商端点 + 密钥 + 模型清单（能力 / 上下文 / 图标 / 名称 / 预设提示词）。

   交互流程：
     1. 选服务商预设（自动填端点）或手填任意 OpenAI 兼容端点
     2. 填 API Key → 点「获取模型」→ 服务端用该密钥请求 {base}/models
     3. 从返回列表里添加模型 → 逐条设定能力、上下文长度、图标、名称、预设提示词
     4. 每个模型可「测试」，服务端发一条 max_tokens=1 的请求验证连通性
     5. 全部随设置保存到服务端 config.yaml（密钥只回传掩码）

   能力标记：识图 / PDF / 视频 —— 供将来的 AI 复盘模块挑选可用模型时过滤。 */
import { useEffect, useMemo, useRef, useState } from "react"
import { Check, Image as ImageIcon, Loader2, Plus, RefreshCw, Trash2, Zap } from "lucide-react"
import Input from "@/components/arc/input/input"
import MorphSelect from "@/components/arc/morph-select/morph-select"
import styles from "./settings-dialog.module.css"

const CAP_LABEL = { text: "文本", vision: "识图", pdf: "PDF", video: "视频" }
const CTX_OPTIONS = [8192, 16384, 32768, 65536, 131072, 200000, 1000000]

/** 单个模型卡片 */
function ModelCard({ m, onChange, onRemove, onTest, testing, testResult, idx }) {
  const [open, setOpen] = useState(false)
  return (
    <div className={styles.modelCard} data-active={m.active || undefined}>
      <div className={styles.modelHead}>
        <label className={styles.modelIcon}>
          {m.icon ? <img src={m.icon} alt="" /> : <span>{(m.label || m.id).slice(0, 2).toUpperCase()}</span>}
          <input type="file" accept="image/*" style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files && e.target.files[0]
              if (!f) return
              const r = new FileReader()
              r.onload = () => onChange({ icon: String(r.result) })
              r.readAsDataURL(f)
            }} />
        </label>
        <div className={styles.modelTitle}>
          <b>{m.label || m.id}</b>
          <u>{m.id}</u>
        </div>
        <div className={styles.modelCaps}>
          {(m.caps || []).map((c) => (
            <em key={c} data-cap={c}>{CAP_LABEL[c] || c}</em>
          ))}
        </div>
        <button type="button" className={styles.iconBtn} onClick={() => setOpen((v) => !v)}
          title={open ? "收起" : "展开设置"}>
          {open ? "−" : "⋯"}
        </button>
        <button type="button" className={styles.iconBtn} data-tone="active"
          data-on={m.active || undefined}
          onClick={() => onChange({ active: !m.active })}
          title={m.active ? "当前使用中" : "设为当前模型"}>
          <Check size={12} />
        </button>
        <button type="button" className={styles.iconBtn} data-tone="test"
          onClick={() => onTest(m.id)} disabled={testing}
          title="测试连通性">
          {testing ? <Loader2 size={12} className={styles.spin} /> : <Zap size={12} />}
        </button>
        <button type="button" className={styles.iconBtn} data-tone="danger"
          onClick={onRemove} title="移除">
          <Trash2 size={12} />
        </button>
      </div>

      {testResult && (
        <div className={styles.testLine} data-ok={testResult.ok || undefined}>
          {testResult.ok
            ? `连通正常 · ${testResult.ms}ms${testResult.reply ? ` · 回复「${testResult.reply}」` : ""}`
            : `失败：${testResult.error || "未知错误"}${testResult.status ? ` (HTTP ${testResult.status})` : ""}`}
        </div>
      )}

      {open && (
        <div className={styles.modelBody}>
          <div className={styles.modelRow}>
            <Input label="显示名称" value={m.label || ""}
              onChange={(e) => onChange({ label: e.target.value })} />
            <div className={styles.ctxField}>
              <span className={styles.label2}>上下文长度</span>
              <MorphSelect
                options={CTX_OPTIONS.map((n) => ({ id: String(n), label: `${n / 1000}K` }))}
                value={String(m.ctx || 8192)}
                onChange={(v) => onChange({ ctx: Number(v) })} />
            </div>
          </div>
          <div className={styles.capRow}>
            <span className={styles.label2}>能力</span>
            {Object.entries(CAP_LABEL).map(([k, label]) => {
              const on = (m.caps || []).includes(k)
              return (
                <button key={k} type="button" className={styles.capChip} data-on={on || undefined}
                  onClick={() => {
                    const next = on ? (m.caps || []).filter((c) => c !== k) : [...(m.caps || []), k]
                    onChange({ caps: next })
                  }}>{label}</button>
              )
            })}
          </div>
          <label className={styles.promptField}>
            <span className={styles.label2}>预设提示词</span>
            <textarea className={styles.textarea} rows={3} value={m.system || ""}
              placeholder="该模型的系统提示词，AI 模块调用时自动带上"
              onChange={(e) => onChange({ system: e.target.value })} />
          </label>
        </div>
      )}
    </div>
  )
}

export default function AiPane({ ai, onSave, onAiAction, aiModelsResult, aiTestResult }) {
  // ai: 服务端下发的配置 { base, key(掩码), models[], providers[], capabilities[] }
  const [draft, setDraft] = useState(() => ({
    provider: ai?.provider || "custom",
    base: ai?.base || "",
    key: ai?.key || "",
    models: Array.isArray(ai?.models) ? ai.models : [],
  }))
  const [dirty, setDirty] = useState(false)
  const [fetching, setFetching] = useState(false)
  const [picked, setPicked] = useState([])       // 待添加的模型 id
  const [testingId, setTestingId] = useState(null)
  const [testRes, setTestRes] = useState(null)
  const [showPicker, setShowPicker] = useState(false)
  const lastAiRef = useRef("")

  // 服务端配置变化（首次下发 / 保存回包）时同步，但不覆盖正在编辑的内容
  useEffect(() => {
    if (dirty) return
    const sig = JSON.stringify({ b: ai?.base, k: ai?.key, n: (ai?.models || []).length })
    if (sig === lastAiRef.current) return
    lastAiRef.current = sig
    setDraft({
      provider: ai?.provider || "custom",
      base: ai?.base || "",
      key: ai?.key || "",
      models: Array.isArray(ai?.models) ? ai.models : [],
    })
  }, [ai?.base, ai?.key, ai?.models, dirty])

  const providers = ai?.providers || []
  const providerOptions = useMemo(
    () => providers.map((p) => ({ id: p.id, label: p.label })), [providers])

  const edit = (patch) => { setDraft((d) => ({ ...d, ...patch })); setDirty(true) }

  const pickProvider = (id) => {
    const p = providers.find((x) => x.id === id)
    edit({ provider: id, base: p?.base || draft.base })
  }

  // 拉取模型列表（带上当前草稿里的端点与密钥，不用先保存）
  const fetchModels = () => {
    setFetching(true)
    setShowPicker(true)
    setPicked([])
    onAiAction && onAiAction({ type: "ai_list_models", base: draft.base, key: draft.key })
    setTimeout(() => setFetching(false), 8000)
  }

  useEffect(() => {
    if (aiModelsResult) setFetching(false)
  }, [aiModelsResult])

  const remote = aiModelsResult?.models || []
  const addPicked = () => {
    const existing = new Set(draft.models.map((m) => m.id))
    const add = picked
      .filter((id) => !existing.has(id))
      .map((id) => {
        const meta = aiModelsResult?.meta?.[id] || {}
        return {
          id, label: id, icon: "",
          caps: meta.caps || ["text"],
          ctx: meta.ctx || 8192,
          system: "",
          active: draft.models.length === 0 && id === picked[0],
        }
      })
    if (add.length) edit({ models: [...draft.models, ...add] })
    setPicked([])
    setShowPicker(false)
  }

  const updateModel = (idx, patch) => {
    const next = draft.models.map((m, i) => (i === idx ? { ...m, ...patch } : m))
    // active 互斥：设一个为当前，其余取消
    if (patch.active) next.forEach((m, i) => { if (i !== idx) m.active = false })
    edit({ models: next })
  }
  const removeModel = (idx) => edit({ models: draft.models.filter((_, i) => i !== idx) })

  const testOne = (id) => {
    setTestingId(id)
    setTestRes({ id, ok: null })     // 占位：表示"这一条正在测"
    onAiAction && onAiAction({ type: "ai_test_model", model: id })
  }
  useEffect(() => {
    if (!aiTestResult) return
    setTestingId(null)
    setTestRes(aiTestResult)         // { id, ok, ms, error }
    const t = setTimeout(() => setTestRes(null), 12000)
    return () => clearTimeout(t)
  }, [aiTestResult])

  const save = () => {
    onSave && onSave({
      provider: draft.provider, base: draft.base, key: draft.key,
      models: draft.models,
    })
    setDirty(false)
  }

  return (
    <>
      <div className={styles.aiForm}>
        <div className={styles.formRow}>
          <div className={styles.ctxField}>
            <span className={styles.label2}>服务商</span>
            <MorphSelect options={providerOptions} value={draft.provider}
              onChange={pickProvider} />
          </div>
          <Input label="API 端点" placeholder="https://api.example.com/v1"
            value={draft.base} onChange={(e) => edit({ base: e.target.value })} />
        </div>
        <div className={styles.formRow}>
          <Input label="API Key" type="password" placeholder="sk-..."
            value={draft.key} autoComplete="off"
            onChange={(e) => edit({ key: e.target.value })} />
          <div className={styles.fetchWrap}>
            <button type="button" className={styles.fetchBtn} onClick={fetchModels}
              disabled={!draft.base}>
              <RefreshCw size={12} /> 获取模型
            </button>
          </div>
        </div>
      </div>

      {showPicker && (
        <div className={styles.pickerBox}>
          <div className={styles.pickerHead}>
            <b>可用模型</b>
            {fetching && <Loader2 size={12} className={styles.spin} />}
            {aiModelsResult && !aiModelsResult.ok && (
              <span className={styles.pickerErr}>
                {aiModelsResult.error || "获取失败"}
              </span>
            )}
            {aiModelsResult?.ok && <span className={styles.pickerCount}>{remote.length} 个</span>}
            <button type="button" className={styles.pickerClose} onClick={() => setShowPicker(false)}>×</button>
          </div>
          {remote.length > 0 && (
            <>
              <div className={styles.pickerList}>
                {remote.map((m) => {
                  const on = picked.includes(m.id)
                  const used = draft.models.some((x) => x.id === m.id)
                  return (
                    <button key={m.id} type="button" className={styles.pickerItem}
                      data-on={on || undefined} data-used={used || undefined}
                      disabled={used}
                      onClick={() => setPicked((p) => on ? p.filter((x) => x !== m.id) : [...p, m.id])}>
                      {m.id}
                    </button>
                  )
                })}
              </div>
              <div className={styles.pickerFoot}>
                <button type="button" className={styles.addBtn} onClick={addPicked}
                  disabled={!picked.length}>
                  <Plus size={12} /> 添加 {picked.length || ""} 个
                </button>
              </div>
            </>
          )}
        </div>
      )}

      <div className={styles.modelList}>
        {draft.models.length === 0 && (
          <div className={styles.modelEmpty}>尚未添加模型</div>
        )}
        {draft.models.map((m, i) => (
          <ModelCard key={m.id} m={m} idx={i}
            onChange={(patch) => updateModel(i, patch)}
            onRemove={() => removeModel(i)}
            onTest={testOne}
            testing={testingId === m.id}
            testResult={testRes && testRes.id === m.id ? testRes : null} />
        ))}
      </div>

      <div className={styles.btnRow}>
        <button type="button" className={styles.saveBtn} onClick={save} data-ready={dirty || undefined}
          disabled={!dirty}>
          {dirty ? "保存 AI 设置" : "已保存"}
        </button>
      </div>
    </>
  )
}
