/* 设置弹窗——玻璃面板（与图表设置菜单同款毛玻璃材质）+ 左侧菜单 + 右侧表单。
   区块：外观（主题色/字体/K线涨跌颜色）、账户（Input 表单，密钥最终落在服务端）、形态。 */
import { useEffect, useId, useState } from "react"
import { AnimatePresence, motion } from "motion/react"
import { X, Palette, Wallet, Activity, Image as ImageIcon, Plus } from "lucide-react"
import Input from "@/components/arc/input/input"
import ColorPicker from "@/components/arc/color-picker/color-picker"
import MorphSelect from "@/components/arc/morph-select/morph-select"
import { LiquidGlass } from "@/components/ui/liquid-glass"
import CoinAvatar from "@/components/ui/coin-avatar"
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

function AppearancePane({ settings, updateSetting }) {
  return (
    <>
      <SectionTitle>主题</SectionTitle>
      <Row label="主题色" hint="影响菜单高亮、胜率徽标等点缀色">
        <ColorPicker value={settings.themeColor} onChange={(v) => updateSetting({ themeColor: v })}
          presets={THEME_PRESETS} />
      </Row>
      <Row label="字体">
        <MorphSelect options={FONT_OPTIONS} value={settings.font} onChange={(id) => updateSetting({ font: id })} />
      </Row>
      <SectionTitle>K线颜色</SectionTitle>
      <Row label="涨跌配色" hint="自定义可分别指定涨/跌色">
        <MorphSelect options={UPDOWN_OPTIONS} value={settings.upDown} onChange={(id) => updateSetting({ upDown: id })} />
      </Row>
      {settings.upDown === "custom" && (
        <Row label="自定义涨/跌色">
          <div className={styles.colorPair}>
            <ColorPicker label="涨" value={settings.upColor} onChange={(v) => updateSetting({ upColor: v })}
              presets={["#16a34a", "#3b82f6", "#06b6d4"]} />
            <ColorPicker label="跌" value={settings.downColor} onChange={(v) => updateSetting({ downColor: v })}
              presets={["#dc2626", "#f97316", "#8b5cf6"]} />
          </div>
        </Row>
      )}
      <div className={styles.note}>全部外观设置即时生效并保存在浏览器本地。</div>
    </>
  )
}

function AccountPane({ accounts }) {
  const [draft, setDraft] = useState({ name: "", key: "", secret: "" })
  const [icon, setIcon] = useState(null)
  const [saved, setSaved] = useState(() => {
    try { return JSON.parse(localStorage.getItem("kline.accountDrafts")) || [] } catch { return [] }
  })
  const save = () => {
    if (!draft.name.trim() || !draft.key.trim()) return
    const next = [...saved, { ...draft, icon }]
    setSaved(next)
    localStorage.setItem("kline.accountDrafts", JSON.stringify(next))
    setDraft({ name: "", key: "", secret: "" })
    setIcon(null)
  }
  return (
    <>
      <SectionTitle>已接入账户（来自服务端 config）</SectionTitle>
      <div className={styles.acctList}>
        {accounts.map((a) => (
          <div key={a.name} className={styles.acctRow}>
            {a.icon ? <img className={styles.acctIcon} src={a.icon} alt="" /> : <CoinAvatar symbol={a.name} size={30} />}
            <span className={styles.acctName}>{a.name}</span>
            <span className={styles.acctBal}>{a.balance == null ? "--" : Number(a.balance).toLocaleString()}</span>
          </div>
        ))}
        {accounts.length === 0 && <div className={styles.note}>暂无（服务端 config.yaml 的 accounts 为空）</div>}
      </div>
      <SectionTitle>添加账户（本地暂存，密钥写入服务端 config.yaml 后重启生效，后续版本支持一键下发）</SectionTitle>
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
                  if (f) setIcon(URL.createObjectURL(f))
                }} />
            </label>
          </div>
        </div>
        <Input label="API Key" placeholder="币安 API Key" value={draft.key}
          onChange={(e) => setDraft((d) => ({ ...d, key: e.target.value }))} autoComplete="off" />
        <Input label="API Secret" type="password" placeholder="币安 API Secret" value={draft.secret}
          onChange={(e) => setDraft((d) => ({ ...d, secret: e.target.value }))} autoComplete="new-password" />
        <button type="button" className={styles.saveBtn} onClick={save}>保存到本地草稿</button>
        {saved.length > 0 && (
          <div className={styles.draftList}>
            {saved.map((d, i) => (
              <div key={i} className={styles.draftRow}>
                {d.icon && <img src={d.icon} alt="" className={styles.draftIcon} />}
                <span>{d.name}</span>
                <span className={styles.draftKey}>{d.key.slice(0, 6)}····{d.key.slice(-4)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  )
}

function PatternsPane({ settings, updateSetting }) {
  return (
    <>
      <SectionTitle>预警</SectionTitle>
      <Row label="盘中预警" hint="K线未收线时形态一旦满足立即提示（收线确认不受影响）">
        <label className={styles.switch}>
          <input type="checkbox" checked={!!settings.livePreview}
            onChange={(e) => updateSetting({ livePreview: e.target.checked })} />
          <span className={styles.knob} aria-hidden />
        </label>
      </Row>
      <div className={styles.note}>
        该开关保存在浏览器本地；服务端检测仍以 config.yaml 的 patterns.live_preview 为准，
        后续版本支持前端一键下发。
      </div>
      <SectionTitle>形态库</SectionTitle>
      <div className={styles.note}>
        当前启用全部 19 种内置形态（见 patterns/builtin.py）。按形态启停的表单将在下一版本加入。
      </div>
    </>
  )
}

export function SettingsDialog({ open, onClose, settings, updateSetting, accounts = [] }) {
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
            <LiquidGlass radius={16} scale={28} aberration={0} frost={5} tint={0.5}
              style={{ position: "absolute", inset: 0 }} />
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
                {section === "appearance" && <AppearancePane settings={settings} updateSetting={updateSetting} />}
                {section === "account" && <AccountPane accounts={accounts} />}
                {section === "patterns" && <PatternsPane settings={settings} updateSetting={updateSetting} />}
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

export default SettingsDialog
