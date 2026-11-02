/* Telegram 设置：把 ODDD 对话接到 Telegram。
   与网页端共用同一个 Agent 会话——两边看到的是同一份对话记录和上下文。 */
import { useEffect, useState } from "react"
import { Check, Send } from "lucide-react"
import Input from "@/components/arc/input/input"
import styles from "./telegram-pane.module.css"

const fmtError = (value) => (typeof value === "string" ? value : "")

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

export default function TelegramPane({ telegram = {}, onSave }) {
  const status = telegram.status || {}
  const [draft, setDraft] = useState(() => ({
    enabled: Boolean(telegram.enabled),
    token: "",
    allowed: (telegram.allowed_chats || []).join(", "),
  }))
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState("")

  // 服务端状态更新后同步（但不覆盖用户正在输入的内容）
  useEffect(() => {
    setDraft((cur) => ({
      ...cur,
      enabled: Boolean(telegram.enabled),
      allowed: (telegram.allowed_chats || []).join(", "),
    }))
  }, [telegram.enabled, (telegram.allowed_chats || []).join(",")])

  const save = () => {
    setError("")
    const chats = draft.allowed.split(/[,，\s]+/).map((x) => x.trim()).filter(Boolean)
    const invalid = chats.filter((x) => !/^-?\d{3,32}$/.test(x))
    if (invalid.length) { setError(`会话 ID 需为数字：${invalid.join(", ")}`); return }
    if (draft.enabled && !draft.token.trim() && !telegram.hasToken) {
      setError("启用前请先填写 Bot Token")
      return
    }
    onSave?.({
      enabled: draft.enabled,
      token: draft.token.trim(),          // 留空表示沿用服务端已存的 token
      allowed_chats: chats,
    })
    setDraft((cur) => ({ ...cur, token: "" }))
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  const running = status.running
  const configured = status.configured

  return (
    <div className={styles.root}>
      <p className={styles.note}>
        在 Telegram 里对话时，消息进入的是网页端同一个会话：这边问的问题、那边的回复，
        两边都能看到，上下文也是连续的一份。
      </p>

      <Row label="启用 Telegram Bot" hint="关闭后不再接收消息，已保存的对话保留">
        <label className={styles.switch}>
          <input type="checkbox" checked={draft.enabled}
            onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
          <span>{draft.enabled ? "已启用" : "未启用"}</span>
        </label>
      </Row>

      <Row label="Bot Token" hint="从 @BotFather 获取；留空则沿用已保存的 token">
        <Input value={draft.token} type="password" placeholder={telegram.hasToken ? "已保存（留空不修改）" : "123456789:AA..."}
          onChange={(e) => setDraft({ ...draft, token: e.target.value })} aria-label="Bot Token" />
      </Row>

      <Row label="允许的会话 ID" hint="逗号分隔；留空表示不限制（不推荐）">
        <Input value={draft.allowed} placeholder="例如 123456789, 987654321"
          onChange={(e) => setDraft({ ...draft, allowed: e.target.value })} aria-label="允许的会话 ID" />
      </Row>

      <div className={styles.status}>
        <span className={styles.badge} data-on={running || undefined}>
          {running ? <Check size={11} /> : <Send size={11} />}
          {running ? "运行中" : configured ? "未运行" : "未配置"}
        </span>
        {status.bot?.username && <span className={styles.botName}>@{status.bot.username}</span>}
        {configured && !running && <span className={styles.hint}>检查 token 后保存即可启动</span>}
        {fmtError(status.error) && <span className={styles.error}>{fmtError(status.error)}</span>}
      </div>

      <p className={styles.hint}>
        不知道自己的会话 ID？先在 Telegram 里给这个 bot 发一句话，它会把 ID 回复给你。
      </p>

      {error && <p className={styles.error} role="alert">{error}</p>}

      <div className={styles.actions}>
        <button type="button" className={styles.save} onClick={save}>
          {saved ? <><Check size={12} /> 已保存</> : "保存并应用"}
        </button>
      </div>
    </div>
  )
}
