/* AI 设置面板：左侧连接配置，右侧模型清单。
   服务商使用 Arc Morph Select；模型未自定义图标时继承当前服务商图标。 */
import { useEffect, useId, useMemo, useRef, useState } from "react"
import { AnimatePresence, motion, useReducedMotion } from "motion/react"
import {
  Check,
  ChevronDown,
  Eye,
  EyeOff,
  Image as ImageIcon,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Server,
  Trash2,
  X,
  Zap,
} from "lucide-react"
import Input from "@/components/arc/input/input"
import MorphSelect from "@/components/arc/morph-select/morph-select"
import ProviderMark, { providerBrandLabel } from "./provider-mark"
import styles from "./ai-pane.module.css"

const CAP_LABEL = { text: "文本", vision: "识图", pdf: "PDF", video: "视频" }
const CAP_KEYS = Object.keys(CAP_LABEL)

const FALLBACK_PROVIDERS = [
  { id: "openai", label: "OpenAI", base: "https://api.openai.com/v1" },
  { id: "deepseek", label: "DeepSeek", base: "https://api.deepseek.com/v1" },
  { id: "moonshot", label: "Moonshot 月之暗面", base: "https://api.moonshot.cn/v1" },
  { id: "zhipu", label: "智谱 GLM", base: "https://open.bigmodel.cn/api/paas/v4" },
  { id: "qwen", label: "阿里通义千问", base: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
  { id: "siliconflow", label: "硅基流动", base: "https://api.siliconflow.cn/v1" },
  { id: "openrouter", label: "OpenRouter", base: "https://openrouter.ai/api/v1" },
  { id: "groq", label: "Groq", base: "https://api.groq.com/openai/v1" },
  { id: "ollama", label: "Ollama（本地）", base: "http://127.0.0.1:11434/v1" },
  { id: "custom", label: "自定义", base: "" },
]

const META_RULES = [
  [/gpt-4o|gpt-4\.1|gpt-5/, ["text", "vision", "pdf"], 128000],
  [/^o1|^o3|^o4/, ["text", "vision"], 200000],
  [/deepseek/, ["text"], 65536],
  [/claude/, ["text", "vision", "pdf"], 200000],
  [/gemini/, ["text", "vision", "pdf", "video"], 1000000],
  [/qwen|qvq/, ["text", "vision"], 131072],
  [/glm-4v/, ["text", "vision"], 8192],
  [/glm/, ["text"], 131072],
  [/moonshot|kimi/, ["text"], 131072],
  [/llava|minicpm-v|internvl/, ["text", "vision"], 32768],
]

function guessMeta(id) {
  const value = String(id || "").toLowerCase()
  for (const [pattern, caps, ctx] of META_RULES) {
    if (pattern.test(value)) return { caps: [...caps], ctx }
  }
  return { caps: ["text"], ctx: 8192 }
}

function endpointHost(base) {
  if (!base) return "未设置"
  try {
    return new URL(base.includes("://") ? base : `https://${base}`).host
  } catch {
    return String(base).replace(/^https?:\/\//, "").split("/")[0] || "未设置"
  }
}

function formatTokens(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return "未设置"
  if (n >= 1_000_000) return `${Number((n / 1_000_000).toFixed(2))}M tokens`
  if (n >= 1000) return `${Number((n / 1000).toFixed(1))}K tokens`
  return `${Math.round(n)} tokens`
}

function MorphProviderField({ providers, value, onChange }) {
  const items = useMemo(() => {
    const option = (provider) => ({
      value: provider.id,
      label: provider.label,
      icon: <ProviderMark provider={provider.id} size={20} label={provider.label} />,
      meta: provider.id === "custom" ? "OpenAI 兼容" : endpointHost(provider.base),
      keywords: [
        provider.id,
        provider.label,
        providerBrandLabel(provider.id),
        provider.base,
        provider.id === "ollama" ? "本地 模型 local" : "",
      ].filter(Boolean).join(" "),
    })
    const cloud = providers.filter((p) => p.id !== "ollama" && p.id !== "custom").map(option)
    const local = providers.filter((p) => p.id === "ollama" || p.id === "custom").map(option)
    return [
      cloud.length ? { label: "云端服务", options: cloud } : null,
      local.length ? { label: "本地与其他", options: local } : null,
    ].filter(Boolean)
  }, [providers])

  return (
    <MorphSelect
      label="服务商"
      hideLabel
      items={items}
      value={value}
      onValueChange={onChange}
      placeholder="选择服务商"
      searchPlaceholder="搜索服务商"
      panelWidth={330}
      triggerMinWidth={190}
      maxListHeight={306}
      className={styles.providerMorph}
    />
  )
}

function SecretField({ value, onChange }) {
  const [visible, setVisible] = useState(false)
  const id = useId()
  return (
    <div className={styles.field}>
      <label className={styles.fieldLabel} htmlFor={id}>API Key</label>
      <div className={styles.secretField}>
        <KeyRound size={14} aria-hidden="true" />
        <input
          id={id}
          type={visible ? "text" : "password"}
          value={value}
          placeholder="sk-..."
          autoComplete="off"
          onChange={(event) => onChange(event.target.value)}
        />
        <button
          type="button"
          className={styles.secretToggle}
          onClick={() => setVisible((current) => !current)}
          aria-label={visible ? "隐藏 API Key" : "显示 API Key"}
          title={visible ? "隐藏 API Key" : "显示 API Key"}
        >
          {visible ? <EyeOff size={15} /> : <Eye size={15} />}
        </button>
      </div>
    </div>
  )
}

function CapabilityChips({ caps, onChange }) {
  const current = caps || []
  return (
    <div className={styles.capabilityField}>
      <span className={styles.fieldLabel}>能力</span>
      <div className={styles.capRow}>
        {CAP_KEYS.map((key) => {
          const active = current.includes(key)
          return (
            <button
              key={key}
              type="button"
              className={styles.capChip}
              data-on={active || undefined}
              aria-pressed={active}
              onClick={() => onChange(active
                ? current.filter((capability) => capability !== key)
                : [...current, key])}
            >
              {active && <Check size={11} />}
              {CAP_LABEL[key]}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function ModelCard({ model, provider, onChange, onRemove, onTest, testing, testResult }) {
  const [open, setOpen] = useState(false)
  const reduced = useReducedMotion()
  const capabilities = model.caps || []

  const uploadIcon = (file) => {
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => onChange({ icon: String(reader.result) })
    reader.readAsDataURL(file)
  }

  return (
    <article className={styles.modelCard} data-active={model.active || undefined} data-open={open || undefined}>
      <div className={styles.modelHead}>
        <label className={styles.modelIcon} title="更换模型图标">
          <ProviderMark provider={provider} icon={model.icon} size={38} />
          <span className={styles.modelIconEdit}><ImageIcon size={10} /></span>
          <input
            type="file"
            accept="image/*"
            aria-label={`更换 ${model.label || model.id} 的图标`}
            onChange={(event) => uploadIcon(event.target.files?.[0])}
          />
        </label>

        <button
          type="button"
          className={styles.modelSummary}
          aria-expanded={open}
          onClick={() => setOpen((current) => !current)}
        >
          <span className={styles.modelIdentity}>
            <span className={styles.modelName}>
              {model.label || model.id}
              {model.active && <em className={styles.activeBadge}>当前</em>}
            </span>
            <span className={styles.modelId}>{model.id}</span>
          </span>
          <span className={styles.modelFacts}>
            <span className={styles.modelCaps}>
              {capabilities.map((capability) => (
                <em key={capability} data-cap={capability}>{CAP_LABEL[capability] || capability}</em>
              ))}
            </span>
            <span className={styles.contextSummary}>{formatTokens(model.ctx)}</span>
          </span>
          <span className={styles.modelChevron} data-open={open || undefined}>
            <ChevronDown size={16} />
          </span>
        </button>
      </div>

      {testResult && testResult.ok !== null && (
        <div className={styles.testLine} data-ok={testResult.ok || undefined}>
          <span className={styles.testDot} />
          <span>
            {testResult.ok
              ? `连通正常 · ${testResult.ms}ms${testResult.reply ? ` · ${testResult.reply}` : ""}`
              : `连接失败 · ${testResult.error || "未知错误"}${testResult.status ? ` (HTTP ${testResult.status})` : ""}`}
          </span>
        </div>
      )}

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            className={styles.modelBody}
            initial={reduced ? { opacity: 0 } : { height: 0, opacity: 0 }}
            animate={reduced ? { opacity: 1 } : { height: "auto", opacity: 1 }}
            exit={reduced ? { opacity: 0 } : { height: 0, opacity: 0 }}
            transition={{ duration: reduced ? 0 : 0.2, ease: [0.22, 1, 0.36, 1] }}
          >
            <div className={styles.modelDetailGrid}>
              <Input
                label="显示名称"
                value={model.label || ""}
                placeholder={model.id}
                onChange={(event) => onChange({ label: event.target.value })}
              />
              <label className={styles.contextField}>
                <span className={styles.fieldLabel}>上下文长度</span>
                <span className={styles.tokenInput}>
                  <input
                    type="number"
                    inputMode="numeric"
                    min="1"
                    max="10000000"
                    step="1"
                    value={model.ctx || ""}
                    placeholder="128000"
                    onChange={(event) => onChange({ ctx: Number(event.target.value) || 0 })}
                    onWheel={(event) => event.currentTarget.blur()}
                  />
                  <span>tokens</span>
                </span>
                <span className={styles.fieldHint}>{formatTokens(model.ctx)}</span>
              </label>
            </div>

            <CapabilityChips caps={capabilities} onChange={(caps) => onChange({ caps })} />

            <label className={styles.promptField}>
              <span className={styles.fieldLabel}>预设提示词</span>
              <textarea
                className={styles.textarea}
                rows={3}
                value={model.system || ""}
                placeholder="该模型被 AI 模块调用时自动带上"
                onChange={(event) => onChange({ system: event.target.value })}
              />
            </label>

            <div className={styles.modelActions}>
              <button
                type="button"
                className={styles.modelAction}
                data-on={model.active || undefined}
                onClick={() => onChange({ active: !model.active })}
              >
                <Check size={13} />
                {model.active ? "当前模型" : "设为当前"}
              </button>
              <button
                type="button"
                className={styles.modelAction}
                disabled={testing}
                onClick={onTest}
              >
                {testing ? <Loader2 size={13} className={styles.spin} /> : <Zap size={13} />}
                {testing ? "测试中" : "测试连接"}
              </button>
              {model.icon && (
                <button
                  type="button"
                  className={styles.modelAction}
                  onClick={() => onChange({ icon: "" })}
                >
                  <RotateCcw size={13} />
                  使用厂商图标
                </button>
              )}
              <button
                type="button"
                className={styles.modelRemove}
                onClick={onRemove}
                title="从列表移除"
              >
                <Trash2 size={13} />
                移除
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </article>
  )
}

function ModelPicker({
  result,
  fetching,
  picked,
  existingIds,
  provider,
  onToggle,
  onAdd,
  onClose,
  onManual,
}) {
  const [query, setQuery] = useState("")
  const remote = result?.models || []
  const used = useMemo(() => new Set(existingIds), [existingIds])
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return remote
    return remote.filter((model) => `${model.id} ${model.owned_by || ""}`.toLowerCase().includes(needle))
  }, [query, remote])

  return (
    <motion.section
      className={styles.pickerBox}
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
    >
      <header className={styles.pickerHead}>
        <div className={styles.pickerTitle}>
          <b>可用模型</b>
          <span>
            {fetching ? "正在获取" : result?.ok ? `${remote.length} 个` : result ? "获取失败" : "准备获取"}
          </span>
        </div>
        <button type="button" className={styles.pickerClose} onClick={onClose} aria-label="关闭模型列表">
          <X size={15} />
        </button>
      </header>

      {fetching && (
        <div className={styles.pickerLoading}>
          <Loader2 size={16} className={styles.spin} />
          <span>读取模型列表</span>
        </div>
      )}

      {!fetching && result && !result.ok && (
        <div className={styles.pickerError}>
          <span>{result.error || "无法从当前端点获取模型"}</span>
          <button type="button" onClick={onManual}>手动添加</button>
        </div>
      )}

      {!fetching && remote.length > 0 && (
        <>
          <label className={styles.modelSearch}>
            <Search size={14} aria-hidden="true" />
            <input
              type="search"
              value={query}
              placeholder="搜索模型"
              autoComplete="off"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <div className={styles.discoverList}>
            {filtered.map((model) => {
              const selected = picked.includes(model.id)
              const alreadyUsed = used.has(model.id)
              return (
                <button
                  key={model.id}
                  type="button"
                  className={styles.discoverRow}
                  data-on={selected || undefined}
                  data-used={alreadyUsed || undefined}
                  disabled={alreadyUsed}
                  onClick={() => onToggle(model.id)}
                >
                  <ProviderMark provider={provider} size={24} />
                  <span className={styles.discoverName}>
                    <b>{model.id}</b>
                    {model.owned_by && <small>{model.owned_by}</small>}
                  </span>
                  {alreadyUsed
                    ? <em className={styles.alreadyUsed}>已添加</em>
                    : <span className={styles.discoverCheck} data-on={selected || undefined}>
                        {selected && <Check size={12} />}
                      </span>}
                </button>
              )
            })}
            {filtered.length === 0 && <div className={styles.discoverEmpty}>没有匹配的模型</div>}
          </div>
          <footer className={styles.pickerFoot}>
            <span>{picked.length ? `已选 ${picked.length} 个` : "选择要添加的模型"}</span>
            <button type="button" className={styles.addBtn} disabled={!picked.length} onClick={onAdd}>
              <Plus size={12} />
              添加
            </button>
          </footer>
        </>
      )}

      {!fetching && result?.ok && remote.length === 0 && (
        <div className={styles.pickerError}>
          <span>接口没有返回可用模型</span>
          <button type="button" onClick={onManual}>手动添加</button>
        </div>
      )}
    </motion.section>
  )
}

export default function AiPane({ ai, onSave, onAiAction, aiModelsResult, aiTestResult }) {
  const [draft, setDraft] = useState(() => ({
    provider: ai?.provider || "custom",
    base: ai?.base || "",
    key: ai?.key || "",
    models: Array.isArray(ai?.models) ? ai.models : [],
  }))
  const [dirty, setDirty] = useState(false)
  const [fetching, setFetching] = useState(false)
  const [picked, setPicked] = useState([])
  const [showPicker, setShowPicker] = useState(false)
  const [manualOpen, setManualOpen] = useState(false)
  const [manualId, setManualId] = useState("")
  const [testingId, setTestingId] = useState(null)
  const [testRes, setTestRes] = useState(null)
  const lastAiRef = useRef("")
  const fetchTimerRef = useRef(null)

  useEffect(() => {
    if (dirty) return
    const signature = JSON.stringify({
      provider: ai?.provider,
      base: ai?.base,
      key: ai?.key,
      models: ai?.models,
    })
    if (signature === lastAiRef.current) return
    lastAiRef.current = signature
    setDraft({
      provider: ai?.provider || "custom",
      base: ai?.base || "",
      key: ai?.key || "",
      models: Array.isArray(ai?.models) ? ai.models : [],
    })
  }, [ai?.provider, ai?.base, ai?.key, ai?.models, dirty])

  useEffect(() => () => window.clearTimeout(fetchTimerRef.current), [])

  useEffect(() => {
    if (!aiModelsResult) return
    window.clearTimeout(fetchTimerRef.current)
    setFetching(false)
  }, [aiModelsResult])

  useEffect(() => {
    if (!aiTestResult) return
    setTestingId(null)
    setTestRes(aiTestResult)
    const timer = window.setTimeout(() => setTestRes(null), 12000)
    return () => window.clearTimeout(timer)
  }, [aiTestResult])

  const providers = ai?.providers?.length ? ai.providers : FALLBACK_PROVIDERS
  const provider = providers.find((item) => item.id === draft.provider)
  const providerLabel = provider?.label || providerBrandLabel(draft.provider)
  const activeModel = draft.models.find((model) => model.active)
  const endpointReady = Boolean(draft.base.trim())

  const edit = (patch) => {
    setDraft((current) => ({ ...current, ...patch }))
    setDirty(true)
  }

  const pickProvider = (id) => {
    const next = providers.find((item) => item.id === id)
    edit({
      provider: id,
      base: next?.base || draft.base,
    })
  }

  const fetchModels = () => {
    if (!endpointReady) return
    setFetching(true)
    setShowPicker(true)
    setManualOpen(false)
    setPicked([])
    onAiAction?.({ type: "ai_list_models", base: draft.base, key: draft.key })
    window.clearTimeout(fetchTimerRef.current)
    fetchTimerRef.current = window.setTimeout(() => setFetching(false), 25000)
  }

  const addModels = (ids) => {
    const currentIds = new Set(draft.models.map((model) => model.id))
    const newIds = [...new Set(ids.map((id) => String(id).trim()).filter(Boolean))]
      .filter((id) => !currentIds.has(id))
    if (!newIds.length) return 0
    const additions = newIds.map((id, index) => {
      const meta = aiModelsResult?.meta?.[id] || guessMeta(id)
      return {
        id,
        label: id,
        icon: "",
        caps: [...meta.caps],
        ctx: meta.ctx,
        system: "",
        active: draft.models.length === 0 && index === 0,
      }
    })
    edit({ models: [...draft.models, ...additions] })
    return additions.length
  }

  const addPicked = () => {
    addModels(picked)
    setPicked([])
    setShowPicker(false)
  }

  const addManual = () => {
    if (!addModels([manualId])) return
    setManualId("")
    setManualOpen(false)
  }

  const updateModel = (index, patch) => {
    const next = draft.models.map((model, modelIndex) => (
      modelIndex === index ? { ...model, ...patch } : model
    ))
    if (patch.active) {
      next.forEach((model, modelIndex) => {
        if (modelIndex !== index) model.active = false
      })
    }
    edit({ models: next })
  }

  const removeModel = (index) => {
    edit({ models: draft.models.filter((_, modelIndex) => modelIndex !== index) })
  }

  const testOne = (id) => {
    setTestingId(id)
    setTestRes({ id, ok: null })
    onAiAction?.({ type: "ai_test_model", model: id, base: draft.base, key: draft.key })
  }

  const save = () => {
    onSave?.({
      provider: draft.provider,
      base: draft.base,
      key: draft.key,
      models: draft.models,
    })
    setDirty(false)
  }

  return (
    <div className={styles.aiRoot}>
      <div className={styles.aiWorkspace}>
        <aside className={styles.connectionColumn}>
          <section className={styles.connectionCard}>
            <header className={styles.connectionHead}>
              <div className={styles.connectionIdentity}>
                <ProviderMark provider={draft.provider} size={44} label={providerLabel} />
                <div>
                  <span className={styles.connectionCaption}>服务商</span>
                  <b>{providerLabel}</b>
                </div>
              </div>
              <span className={styles.connectionState} data-ready={endpointReady || undefined}>
                <span />
                {endpointReady ? "已配置端点" : "待配置"}
              </span>
            </header>

            <div className={styles.field}>
              <span className={styles.fieldLabel}>切换服务商</span>
              <MorphProviderField providers={providers} value={draft.provider} onChange={pickProvider} />
            </div>

            <Input
              label="API 端点"
              placeholder="https://api.example.com/v1"
              value={draft.base}
              onChange={(event) => edit({ base: event.target.value })}
            />

            <SecretField value={draft.key} onChange={(key) => edit({ key })} />

            <div className={styles.connectionMeta}>
              <span title={draft.base}><Server size={13} />{endpointHost(draft.base)}</span>
              <span data-ready={Boolean(draft.key) || undefined}>
                <KeyRound size={13} />
                {draft.key ? "密钥已填写" : "未设置密钥"}
              </span>
            </div>
          </section>
        </aside>

        <section className={styles.modelWorkspace}>
          <header className={styles.modelWorkspaceHead}>
            <div>
              <div className={styles.modelTitleLine}>
                <h3>模型</h3>
                <span className={styles.modelCount}>{draft.models.length}</span>
              </div>
              <span className={styles.activeSummary}>
                {activeModel ? `当前使用 ${activeModel.label || activeModel.id}` : "尚未选择当前模型"}
              </span>
            </div>
            <div className={styles.workspaceActions}>
              <button
                type="button"
                className={styles.secondaryBtn}
                onClick={() => setManualOpen((current) => !current)}
                aria-expanded={manualOpen}
              >
                <Plus size={13} />
                手动添加
              </button>
              <button
                type="button"
                className={styles.fetchBtn}
                onClick={fetchModels}
                disabled={!endpointReady || fetching}
              >
                {fetching ? <Loader2 size={13} className={styles.spin} /> : <RefreshCw size={13} />}
                {fetching ? "获取中" : "获取模型"}
              </button>
            </div>
          </header>

          <AnimatePresence initial={false}>
            {manualOpen && (
              <motion.div
                className={styles.manualAdd}
                initial={{ opacity: 0, y: -6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.16 }}
              >
                <input
                  value={manualId}
                  placeholder="输入模型 ID"
                  autoComplete="off"
                  onChange={(event) => setManualId(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault()
                      addManual()
                    }
                  }}
                />
                <button type="button" onClick={addManual} disabled={!manualId.trim()}>
                  添加模型
                </button>
              </motion.div>
            )}
          </AnimatePresence>

          {showPicker && (
            <ModelPicker
              result={aiModelsResult}
              fetching={fetching}
              picked={picked}
              existingIds={draft.models.map((model) => model.id)}
              provider={draft.provider}
              onToggle={(id) => setPicked((current) => (
                current.includes(id) ? current.filter((item) => item !== id) : [...current, id]
              ))}
              onAdd={addPicked}
              onClose={() => setShowPicker(false)}
              onManual={() => {
                setShowPicker(false)
                setManualOpen(true)
              }}
            />
          )}

          <div className={styles.modelScroll}>
            <div className={styles.modelList}>
              {draft.models.length === 0 && (
                <div className={styles.modelEmpty}>
                  <span className={styles.emptyMark}>
                    <ProviderMark provider={draft.provider} size={34} />
                  </span>
                  <b>还没有模型</b>
                  <span>从服务商获取，或直接输入模型 ID</span>
                  <div>
                    <button type="button" className={styles.secondaryBtn} onClick={() => setManualOpen(true)}>
                      <Plus size={13} />
                      手动添加
                    </button>
                    <button type="button" className={styles.fetchBtn} onClick={fetchModels} disabled={!endpointReady}>
                      <RefreshCw size={13} />
                      获取模型
                    </button>
                  </div>
                </div>
              )}

              {draft.models.map((model, index) => (
                <ModelCard
                  key={model.id}
                  model={model}
                  provider={draft.provider}
                  onChange={(patch) => updateModel(index, patch)}
                  onRemove={() => removeModel(index)}
                  onTest={() => testOne(model.id)}
                  testing={testingId === model.id}
                  testResult={testRes?.id === model.id ? testRes : null}
                />
              ))}
            </div>
          </div>
        </section>
      </div>

      <footer className={styles.aiFooter}>
        <div className={styles.saveState}>
          <span className={styles.saveDot} data-dirty={dirty || undefined} />
          {dirty ? "有未保存的更改" : "设置已同步"}
        </div>
        <button
          type="button"
          className={styles.saveBtn}
          onClick={save}
          data-ready={dirty || undefined}
          disabled={!dirty}
        >
          {dirty ? "保存更改" : "已保存"}
        </button>
      </footer>
    </div>
  )
}
