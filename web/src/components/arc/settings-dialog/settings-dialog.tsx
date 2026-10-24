/* 设置弹窗——玻璃面板（与图表设置菜单同款毛玻璃材质）+ 左侧菜单 + 右侧表单。
   所有设置均为真实系统功能：账户增删、形态启停、盘中预警、外观——
   下发服务端热生效并持久化到 config.yaml（币种展示选择除外，存浏览器本地）。 */
import { useEffect, useId, useState } from "react"
import { AnimatePresence, motion } from "motion/react"
import { X, Palette, Wallet, Activity, Image as ImageIcon, Plus, Trash2 } from "lucide-react"
import Input from "@/components/arc/input/input"
import ColorPicker from "@/components/arc/color-picker/color-picker"
import MorphSelect from "@/components/arc/morph-select/morph-select"
import CoinAvatar from "@/components/ui/coin-avatar"
import { PATTERN_META } from "@/patterns_meta.js"
import styles from "./settings-dialog.module.css"

const spring = { type: "spring", stiffness: 460, damping: 40, mass: 0.85 }

const SECTIONS = [
  { id: "appearance", label: "外观", Icon: Palette },
  { id: "account", label: "账户", Icon: Wallet },
  { id: "patterns", label: "形态", Icon: Activity },
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

function Row({ label, children, hint }) {
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

function SectionTitle({ children }) {
  return <div className={styles.sectionTitle}>{children}</div>
}

function AppearancePane({ ui, onUpdateUi }) {
  return (
    <>
      <SectionTitle>主题</SectionTitle>
      <Row label="主题色" hint="影响菜单高亮、胜率徽标等点缀色">
        <ColorPicker value={ui.themeColor} onChange={(v) => onUpdateUi({ themeColor: v })}
          presets={THEME_PRESETS} />
      </Row>
      <Row label="字体">
        <MorphSelect options={FONT_OPTIONS} value={ui.font} onChange={(id) => onUpdateUi({ font: id })} />
      </Row>
      <SectionTitle>K线颜色</SectionTitle>
      <Row label="涨跌配色" hint="自定义可分别指定涨/跌色">
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
      <div className={styles.note}>外观保存在服务端配置，对所有打开此页面的人生效。</div>
    </>
  )
}

function AccountPane({ accounts, onAddAccount, onRemoveAccount }) {
  const [draft, setDraft] = useState({ name: "", key: "", secret: "" })
  const [icon, setIcon] = useState("")
  const list = accounts || []
  const ready = draft.name.trim() && draft.key.trim() && draft.secret.trim()
  const add = () => {
    if (!ready) return
    onAddAccount({ name: draft.name.trim(), api_key: draft.key.trim(), api_secret: draft.secret.trim(), icon })
    setDraft({ name: "", key: "", secret: "" })
    setIcon("")
  }
  return (
    <>
      <SectionTitle>已接入账户（增删立即生效并写入服务端配置）</SectionTitle>
      <div className={styles.acctList}>
        {list.map((a) => (
          <div key={a.name} className={styles.acctRow}>
            {a.icon ? <img className={styles.acctIcon} src={a.icon} alt="" /> : <CoinAvatar symbol={a.name} size={30} />}
            <span className={styles.acctName}>{a.name}</span>
            <span className={styles.acctKey}>{(a.api_key || "").slice(0, 6)}····{(a.api_key || "").slice(-4)}</span>
            <button type="button" className={styles.delBtn} aria-label={`删除 ${a.name}`}
              onClick={() => onRemoveAccount(a.name)}>
              <Trash2 size={14} />
            </button>
          </div>
        ))}
        {list.length === 0 && <div className={styles.note}>暂无账户，用下方表单添加</div>}
      </div>
      <SectionTitle>添加账户</SectionTitle>
      <div className={styles.form}>
        <div className={styles.formRow}>
          <Input label="账户名称" placeholder="例如：主账户" value={draft.name}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))} />
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
                  reader.onload = () => setIcon(String(reader.result))   // dataURL，随配置持久化
                  reader.readAsDataURL(f)
                }} />
            </label>
          </div>
        </div>
        <Input label="API Key" placeholder="币安 API Key" value={draft.key}
          onChange={(e) => setDraft((d) => ({ ...d, key: e.target.value }))} autoComplete="off" />
        <Input label="API Secret" type="password" placeholder="币安 API Secret" value={draft.secret}
          onChange={(e) => setDraft((d) => ({ ...d, secret: e.target.value }))} autoComplete="new-password" />
        <button type="button" className={styles.saveBtn} onClick={add} disabled={!ready}
          data-ready={ready || undefined}>
          {ready ? "添加账户" : "填写完整后可添加"}
        </button>
      </div>
    </>
  )
}

function PatternsPane({ patternsEnabled, livePreview, onTogglePattern, onToggleLivePreview }) {
  const enabled = patternsEnabled || []
  return (
    <>
      <SectionTitle>预警</SectionTitle>
      <Row label="盘中预警" hint="K线未收线时形态一旦满足立即提示（收线确认不受影响）">
        <label className={styles.switch}>
          <input type="checkbox" checked={!!livePreview}
            onChange={() => onToggleLivePreview()} />
          <span className={styles.knob} aria-hidden />
        </label>
      </Row>
      <SectionTitle>形态启停（关闭后立即停止检测该形态，保存到服务端）</SectionTitle>
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
      <div className={styles.note}>至少保留一个形态；变更立即影响服务端检测并持久化。</div>
    </>
  )
}

export function SettingsDialog({ open, onClose, ui, onUpdateUi,
  patternsEnabled, livePreview, onTogglePattern, onToggleLivePreview,
  accounts, onAddAccount, onRemoveAccount }) {
  const [section, setSection] = useState("appearance")
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
                      onClick={() => setSection(id)}>
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
                {section === "account" && <AccountPane accounts={accounts} onAddAccount={onAddAccount} onRemoveAccount={onRemoveAccount} />}
                {section === "patterns" && <PatternsPane patternsEnabled={patternsEnabled} livePreview={livePreview}
                  onTogglePattern={onTogglePattern} onToggleLivePreview={onToggleLivePreview} />}
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

export default SettingsDialog
