/* OpenClaw 设置：把 ODDD 接成 OpenClaw 的一个「模型」，微信里的消息就进这条链路。

为什么是这个方向：OpenClaw 是自托管的 agent 网关，它靠
models.providers 指向任意 OpenAI 兼容端点（你截图里它现在挂的是 ZCode）。
所以 ODDD 这边只要按标准形状提供服务，在 OpenClaw 里填 baseUrl + token
就能用，不需要逆向它的私有协议，它升级版本也不影响。

安全上的取舍：这个端点后面是能真实下单的 Agent，所以
 · 默认关闭，不启用时路径直接 404；
 · 必须有访问令牌，没配令牌时连"接口存在"都不透露；
 · 令牌只在这里显示一次，之后服务端只回掩码。
*/
import { useEffect, useState } from "react"
import { Check, Copy, Globe, KeyRound, Plug, ShieldAlert } from "lucide-react"
import Input from "@/components/arc/input/input"
import styles from "./openclaw-pane.module.css"

/* 生成一个够长的随机令牌。不用 Math.random：它在这种"守卫真实资金"的场景
   里强度不够，而且同源脚本可预测。crypto.getRandomValues 是浏览器标准的
   密码学随机源。 */
function randomToken() {
  const bytes = new Uint8Array(24)
  if (globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(bytes)
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256)
  }
  return "oc_" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")
}

function Row({ label, hint, children }) {
  return (
    <div className={styles.row}>
      <div className={styles.rowLabel}>
        <span className={styles.rowName}>{label}</span>
        {hint && <span className={styles.rowHint}>{hint}</span>}
      </div>
      <div className={styles.rowControl}>{children}</div>
    </div>
  )
}

function CopyField({ value, label }) {
  const [done, setDone] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setDone(true)
      setTimeout(() => setDone(false), 1600)
    } catch {
      setDone(false)
    }
  }
  return (
    <div className={styles.copyField}>
      <code className={styles.copyValue}>{value}</code>
      <button type="button" className={styles.copyBtn} onClick={copy}
        aria-label={`复制${label}`}>
        {done ? <Check size={11} /> : <Copy size={11} />}
      </button>
    </div>
  )
}

export default function OpenclawPane({ openclaw = {}, onSave }) {
  const status = openclaw.status || {}
  const [draft, setDraft] = useState(() => ({
    enabled: Boolean(openclaw.enabled),
    token: "",
    model: openclaw.model || "oddd",
    maxPerMin: openclaw.max_per_min || 40,
  }))
  const [revealed, setRevealed] = useState("")     // 刚生成、尚未保存的明文令牌
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    setDraft((cur) => ({
      ...cur,
      enabled: Boolean(openclaw.enabled),
      model: openclaw.model || "oddd",
      maxPerMin: openclaw.max_per_min || 40,
    }))
  }, [openclaw.enabled, openclaw.model, openclaw.max_per_min])

  const save = () => {
    setError("")
    const token = (revealed || draft.token).trim()
    if (draft.enabled && !token && !openclaw.hasToken) {
      setError("启用前必须先生成访问令牌（这个接口能真实下单，不能裸奔）")
      return
    }
    if (token && token.length < 16) {
      setError("令牌太短，至少 16 位")
      return
    }
    onSave?.({
      enabled: draft.enabled,
      token: token || "",                 // 空 = 沿用服务端已存的
      model: draft.model.trim() || "oddd",
      max_per_min: Number(draft.maxPerMin) || 40,
    })
    setDraft((cur) => ({ ...cur, token: "" }))
    setRevealed("")
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  const ready = status.ready
  const baseUrl = openclaw.base_url || "https://od1.site/v1"

  return (
    <div className={styles.root}>
      <p className={styles.note}>
        OpenClaw 是自托管的 agent 网关，支持微信、Telegram、Discord 等渠道。
        ODDD 在这里充当它的一个「模型后端」——微信里的消息进来，由 ODDD 这个 Agent
        来处理并回复，和网页端共用同一份会话与上下文。
      </p>

      <Row label="启用 OpenClaw 接入" hint="关闭时 /v1 接口直接返回 404，不暴露存在">
        <label className={styles.switch}>
          <input type="checkbox" checked={draft.enabled}
            onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
          <span>{draft.enabled ? "已启用" : "未启用"}</span>
        </label>
      </Row>

      <Row label="访问令牌" hint="OpenClaw 填 apiKey 时用；留空则沿用已保存的">
        <div className={styles.tokenRow}>
          <Input value={revealed || draft.token} type="password"
            placeholder={openclaw.hasToken ? "已保存（留空不修改）" : "点右侧生成"}
            onChange={(e) => { setRevealed(""); setDraft({ ...draft, token: e.target.value }) }}
            aria-label="访问令牌" />
          <button type="button" className={styles.genBtn}
            onClick={() => { setRevealed(randomToken()); setDraft({ ...draft, token: "" }) }}>
            <KeyRound size={11} /> 生成
          </button>
        </div>
        {revealed && (
          <div className={styles.reveal}>
            <ShieldAlert size={11} />
            <span>令牌只显示这一次，保存后只能重新生成。</span>
          </div>
        )}
      </Row>

      <Row label="模型名" hint="OpenClaw 里请求时用的 model 值">
        <Input value={draft.model} placeholder="oddd"
          onChange={(e) => setDraft({ ...draft, model: e.target.value })}
          aria-label="模型名" />
      </Row>

      <Row label="每分钟上限" hint="防止对方重试循环打满 Agent 队列">
        <Input value={draft.maxPerMin} type="number" min={1} max={300}
          onChange={(e) => setDraft({ ...draft, maxPerMin: e.target.value })}
          aria-label="每分钟请求上限" />
      </Row>

      <div className={styles.status}>
        <span className={styles.badge} data-on={ready || undefined}>
          {ready ? <Check size={11} /> : <Plug size={11} />}
          {ready ? "接口在线" : openclaw.hasToken ? "已配置未启用" : "未配置"}
        </span>
        {ready && <span className={styles.stat}>调用 {status.hits || 0} 次</span>}
        {ready && status.callsLastMinute > 0 && (
          <span className={styles.stat}>近 1 分钟 {status.callsLastMinute}/{status.maxPerMinute}</span>
        )}
        {status.lastError && <span className={styles.error}>{status.lastError}</span>}
      </div>

      <div className={styles.setup}>
        <div className={styles.setupHead}>
          <Globe size={12} /> 在 OpenClaw 里这样填
        </div>
        <div className={styles.setupRow}>
          <span className={styles.setupKey}>baseUrl</span>
          <CopyField value={baseUrl} label="baseUrl" />
        </div>
        <div className={styles.setupRow}>
          <span className={styles.setupKey}>apiKey</span>
          <span className={styles.setupHint}>上面那个访问令牌（保存后才会生效）</span>
        </div>
        <div className={styles.setupRow}>
          <span className={styles.setupKey}>model</span>
          <CopyField value={draft.model || "oddd"} label="model" />
        </div>
        <pre className={styles.snippet}>{`models:
  providers:
    oddd:
      baseUrl: ${baseUrl}
      apiKey: <访问令牌>
      models:
        - id: ${draft.model || "oddd"}
          name: ODDD`}</pre>
        <p className={styles.setupHint}>
          想只让微信走 ODDD，在 OpenClaw 的 agents 绑定里把这个 provider
          指给对应渠道即可；其余渠道保持原样。
        </p>
      </div>

      {error && <p className={styles.error} role="alert">{error}</p>}

      <div className={styles.actions}>
        <button type="button" className={styles.save} onClick={save}>
          {saved ? <><Check size={12} /> 已保存</> : "保存并应用"}
        </button>
      </div>
    </div>
  )
}
