/* 设置弹窗——玻璃面板（与图表设置菜单同款毛玻璃材质）+ 左侧菜单 + 右侧表单。
   所有设置均为真实系统功能：账户增删、形态启停、盘中预警、外观——
   下发服务端热生效并持久化到 config.yaml（币种展示选择除外，存浏览器本地）。 */
import { useEffect, useId, useState } from "react"
import { AnimatePresence, motion } from "motion/react"
import { X, Palette, Wallet, Activity, Image as ImageIcon, Plus, Bot } from "lucide-react"
import AiPane from "./ai-pane"
import Input from "@/components/arc/input/input"
import ColorPicker from "@/components/arc/color-picker/color-picker"
import MorphSelect from "@/components/arc/morph-select/morph-select"
import { PATTERN_META } from "@/patterns_meta.js"
import styles from "./settings-dialog.module.css"

const spring = { type: "spring", stiffness: 460, damping: 40, mass: 0.85 }

const SECTIONS = [
  { id: "appearance", label: "外观", Icon: Palette },
  { id: "account", label: "账户", Icon: Wallet },
  { id: "patterns", label: "形态", Icon: Activity },
  { id: "ai", label: "AI", Icon: Bot },
]

const THEME_PRESETS = ["#f0b90b", "#3b82f6", "#16a34a", "#8b5cf6", "#f97316", "#06b6d4"]
const FONT_OPTIONS = [
  { id: "douyin", label: "抖音黑体" },
  { id: "system", label: "系统默认" },
  { id: "serif", label: "衬线" },
]
const UPDOWN_OPTIONS = [
  { id: "green-red", label: "绿涨红跌" },
  { id: "red-green", label: "红涨绿跌" },
  { id: "custom", label: "自定义" },
]
const DIR_LABEL = { bullish: "涨", bearish: "跌", neutral: "变盘" }

function Row({ label, children }) {
  return (
    <div className={styles.row}>
      <div className={styles.rowLabel}>
        <span className={styles.rowName}>{label}</span>
      </div>
      <div className={styles.rowControl}>{children}</div>
    </div>
  )
}

function SectionTitle({ children }) {
  return <div className={styles.sectionTitle}>{children}</div>
}

function AppearancePane({ ui, onUpdateUi }) {
  return (
    <>
      <SectionTitle>主题</SectionTitle>
      <Row label="主题色">
        <ColorPicker value={ui.themeColor} onChange={(v) => onUpdateUi({ themeColor: v })}
          presets={THEME_PRESETS} />
      </Row>
      <Row label="字体">
        <MorphSelect options={FONT_OPTIONS} value={ui.font} onChange={(id) => onUpdateUi({ font: id })} />
      </Row>
      <SectionTitle>K线颜色</SectionTitle>
      <Row label="涨跌配色">
        <MorphSelect options={UPDOWN_OPTIONS} value={ui.upDown} onChange={(id) => onUpdateUi({ upDown: id })} />
      </Row>
      {ui.upDown === "custom" && (
        <Row label="自定义涨/跌色">
          <div className={styles.colorPair}>
            <ColorPicker label="涨" value={ui.upColor} onChange={(v) => onUpdateUi({ upColor: v })}
              presets={["#16a34a", "#3b82f6", "#06b6d4"]} />
            <ColorPicker label="跌" value={ui.downColor} onChange={(v) => onUpdateUi({ downColor: v })}
              presets={["#dc2626", "#f97316", "#8b5cf6"]} />
          </div>
        </Row>
      )}
    </>
  )
}

/* 账户：只维护一个账户。下方表单直接编辑该账户（名称/头像/API Key/Secret），
   保存即整体替换服务端账户（凭据掩码表示沿用原值，前端从不持有明文）。
   未接入时表单为空，填完即为新增。 */
function AccountPane({ accounts, onSaveAccount }) {
  const cur = (accounts || [])[0] || null
  const [draft, setDraft] = useState({ name: "", key: "", secret: "" })
  const [icon, setIcon] = useState("")
  const [dirty, setDirty] = useState(false)

  // 服务端账户变化（首次下发/保存回包）时同步表单，但不覆盖正在编辑的内容
  useEffect(() => {
    if (dirty) return
    setDraft({ name: cur?.name || "", key: cur?.api_key || "", secret: cur?.api_secret || "" })
    setIcon(cur?.icon || "")
  }, [cur?.name, cur?.api_key, cur?.api_secret, cur?.icon])

  const ready = draft.name.trim() && draft.key.trim() && draft.secret.trim()
  const save = () => {
    if (!ready) return
    onSaveAccount({
      id: cur?.id || undefined,     // 带回稳定 ID：改名时服务端据此保留权益曲线历史
      name: draft.name.trim(),
      api_key: draft.key.trim(),
      api_secret: draft.secret.trim(),
      icon,
    })
    setDirty(false)
  }
  const clear = () => {
    onSaveAccount(null)
    setDraft({ name: "", key: "", secret: "" })
    setIcon("")
    setDirty(false)
  }
  const edit = (patch) => { setDraft((d) => ({ ...d, ...patch })); setDirty(true) }

  return (
    <>
      <SectionTitle>{cur ? "账户信息" : "接入账户"}</SectionTitle>
      <div className={styles.form}>
        <div className={styles.formRow}>
          <Input label="账户名称" placeholder="例如：主账户" value={draft.name}
            onChange={(e) => edit({ name: e.target.value })} />
          <div className={styles.avatarField}>
            <span className={styles.label2}>
              <ImageIcon size={13} /> 头像
            </span>
            <label className={styles.avatarPick}>
              {icon ? <img src={icon} alt="" /> : <Plus size={16} />}
              <input type="file" accept="image/*" style={{ display: "none" }}
                onChange={(e) => {
                  const f = e.target.files && e.target.files[0]
                  if (!f) return
                  const reader = new FileReader()
                  reader.onload = () => { setIcon(String(reader.result)); setDirty(true) }   // dataURL，随配置持久化
                  reader.readAsDataURL(f)
                }} />
            </label>
          </div>
        </div>
        <Input label="API Key" placeholder="币安合约 API Key" value={draft.key}
          onChange={(e) => edit({ key: e.target.value })} autoComplete="off" />
        <Input label="API Secret" type="password" placeholder="币安合约 API Secret" value={draft.secret}
          onChange={(e) => edit({ secret: e.target.value })} autoComplete="new-password" />
        <div className={styles.btnRow}>
          <button type="button" className={styles.saveBtn} onClick={save} disabled={!ready}
            data-ready={ready || undefined}>
            {ready ? (cur ? "保存修改" : "接入账户") : "填写完整后可保存"}
          </button>
          {cur && (
            <button type="button" className={styles.clearBtn} onClick={clear}>移除账户</button>
          )}
        </div>
      </div>
    </>
  )
}

function PatternsPane({ patternsEnabled, livePreview, onTogglePattern, onToggleLivePreview }) {
  const enabled = patternsEnabled || []
  return (
    <>
      <SectionTitle>预警</SectionTitle>
      <Row label="盘中预警">
        <label className={styles.switch}>
          <input type="checkbox" checked={!!livePreview}
            onChange={() => onToggleLivePreview()} />
          <span className={styles.knob} aria-hidden />
        </label>
      </Row>
      <SectionTitle>形态启停</SectionTitle>
      <div className={styles.patternToggles}>
        {Object.entries(PATTERN_META).map(([key, p]) => {
          const on = enabled.includes(key)
          return (
            <button key={key} type="button" className={styles.patternToggle} data-on={on || undefined}
              disabled={on && enabled.length <= 1}
              onClick={() => onTogglePattern(key)}>
              <span className={styles.ptName}>{p.name}</span>
              <span className={styles.ptDir} data-dir={p.dir}>{DIR_LABEL[p.dir]}</span>
              <span className={styles.ptCheck}>{on ? "✓" : "+"}</span>
            </button>
          )
        })}
      </div>
    </>
  )
}

export function SettingsDialog({ open, onClose, section, onSectionChange,
  ui, onUpdateUi, patternsEnabled, livePreview, onTogglePattern, onToggleLivePreview,
  accounts, onSaveAccount, ai, onSaveAi, onAiAction, aiModelsResult, aiTestResult }) {
  const [hover, setHover] = useState(null)
  const groupId = useId()

  useEffect(() => {
    if (!open) return
    const onKey = (e) => { if (e.key === "Escape") onClose() }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [open, onClose])

  return (
    <AnimatePresence>
      {open && (
        <motion.div className={styles.overlay} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          transition={{ duration: 0.16 }} onClick={onClose}>
          <motion.div className={styles.panel} role="dialog" aria-label="设置"
            initial={{ opacity: 0, y: 16, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 10, scale: 0.98 }}
            transition={spring}
            onClick={(e) => e.stopPropagation()}>
            <header className={styles.head}>
              <h2 className={styles.title}>设置</h2>
              <button type="button" className={styles.close} onClick={onClose} aria-label="关闭设置">
                <X size={15} />
              </button>
            </header>
            <div className={styles.body}>
              <nav className={styles.nav}>
                {SECTIONS.map(({ id, label, Icon }) => {
                  const on = section === id
                  return (
                    <button key={id} type="button" className={styles.navItem} data-on={on || undefined}
                      onMouseEnter={() => setHover(id)}
                      onMouseLeave={() => setHover((h) => (h === id ? null : h))}
                      onClick={() => onSectionChange(id)}>
                      {(hover === id || on) && (
                        <motion.div layoutId={`setnav-${groupId}`} className={styles.navPill}
                          data-hover={hover === id && !on ? "" : undefined} transition={spring} />
                      )}
                      <Icon size={15} className={styles.navIcon} aria-hidden />
                      <span style={{ position: "relative" }}>{label}</span>
                    </button>
                  )
                })}
              </nav>
              <div className={styles.pane}>
                {section === "appearance" && <AppearancePane ui={ui} onUpdateUi={onUpdateUi} />}
                {section === "account" && <AccountPane accounts={accounts} onSaveAccount={onSaveAccount} />}
                {section === "patterns" && <PatternsPane patternsEnabled={patternsEnabled} livePreview={livePreview}
                  onTogglePattern={onTogglePattern} onToggleLivePreview={onToggleLivePreview} />}
                {section === "ai" && <AiPane ai={ai} onSave={onSaveAi} onAiAction={onAiAction}
                  aiModelsResult={aiModelsResult} aiTestResult={aiTestResult} />}
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

export default SettingsDialog
