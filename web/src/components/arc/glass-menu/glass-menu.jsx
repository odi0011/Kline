/* 玻璃拟态菜单——按 uiarc GlassMenu 的交互文档自实现（官方源码为 Pro 专属）。
   API 对齐：groups / children 触发器 / onSelect / checked+keepOpen / align / width。
   动效：面板从触发器弹出（spring），高亮滑块用 layoutId 在行间滑动。 */
import { useEffect, useRef, useState } from "react"
import { AnimatePresence, motion } from "motion/react"
import { Check, ChevronLeft, Search } from "lucide-react"

const spring = { type: "spring", stiffness: 520, damping: 40, mass: 0.8 }

const pillStyle = {
  display: "grid",
  placeItems: "center",
  width: 42,
  height: 42,
  borderRadius: 999,
  border: "1px solid rgba(255,255,255,0.75)",
  background: "rgba(255,255,255,0.62)",
  backdropFilter: "blur(14px)",
  WebkitBackdropFilter: "blur(14px)",
  boxShadow: "0 8px 24px rgba(16,24,40,0.18)",
  color: "#1c2333",
  cursor: "pointer",
}

const panelStyle = {
  background: "rgba(255,255,255,0.74)",
  backdropFilter: "blur(20px) saturate(160%)",
  WebkitBackdropFilter: "blur(20px) saturate(160%)",
  border: "1px solid rgba(255,255,255,0.78)",
  borderRadius: 14,
  boxShadow: "0 12px 36px rgba(16,24,40,0.18)",
  padding: 6,
  maxHeight: "62vh",
  overflowY: "auto",
  overscrollBehavior: "contain",
}

export function GlassMenu({
  label = "Actions",
  triggerLabel,
  groups = [],
  children,
  onSelect,
  align = "end",
  width = 256,
  className = "",
  style,
}) {
  const [open, setOpen] = useState(false)
  const [hoverId, setHoverId] = useState(null)
  const [sub, setSub] = useState(null)          // 打开的子菜单父项
  const [subQuery, setSubQuery] = useState("")  // searchable 子菜单的搜索词
  const rootRef = useRef(null)

  useEffect(() => {
    if (!open) { setSub(null); setHoverId(null); setSubQuery(""); return }
    const onDoc = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === "Escape") setOpen(false) }
    document.addEventListener("pointerdown", onDoc)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("pointerdown", onDoc)
      document.removeEventListener("keydown", onKey)
    }
  }, [open])

  const rowClick = (item) => {
    if (item.disabled) return
    if (item.items) { setSub(item); setSubQuery(""); return }
    onSelect?.(item.id, item)
    if (!item.keepOpen) setOpen(false)
  }

  const renderRow = (item) => (
    <div
      key={item.id}
      role={item.checked != null ? "menuitemcheckbox" : "menuitem"}
      aria-checked={item.checked}
      aria-disabled={item.disabled}
      onMouseEnter={() => setHoverId(item.id)}
      onClick={() => rowClick(item)}
      style={{
        position: "relative",
        display: "flex",
        alignItems: "center",
        gap: 10,
        height: 36,
        padding: "0 10px",
        borderRadius: 10,
        cursor: item.disabled ? "default" : "pointer",
        fontSize: 13,
        color: item.danger ? "#dc2626" : "#1c2333",
        opacity: item.disabled ? 0.45 : 1,
      }}
    >
      {hoverId === item.id && (
        <motion.div
          layoutId="glass-menu-pill"
          transition={spring}
          style={{ position: "absolute", inset: 0, borderRadius: 10, background: "rgba(240,185,11,0.16)" }}
        />
      )}
      {item.items && sub ? null : null}
      {item.icon && (
        <span style={{ position: "relative", display: "grid", placeItems: "center", color: "#b45309" }}>
          {item.icon}
        </span>
      )}
      <span style={{ position: "relative", flex: 1, whiteSpace: "nowrap" }}>{item.label}</span>
      {item.shortcut && <span style={{ position: "relative", fontSize: 11, color: "#8b949e" }}>{item.shortcut}</span>}
      {item.checked != null && (
        <span style={{ position: "relative", color: item.checked ? "#b45309" : "transparent" }}>
          <Check size={15} strokeWidth={2.4} />
        </span>
      )}
      {item.items && !sub && (
        <span style={{ position: "relative", color: "#8b949e", fontSize: 12 }}>›</span>
      )}
    </div>
  )

  return (
    <div ref={rootRef} className={className} style={{ position: "relative", ...style }}>
      <motion.button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={triggerLabel || label}
        onClick={() => setOpen((o) => !o)}
        whileTap={{ scale: 0.92 }}
        style={pillStyle}
      >
        {children}
      </motion.button>

      <AnimatePresence>
        {open && (
          <motion.div
            role="menu"
            aria-label={label}
            initial={{ opacity: 0, scale: 0.9, y: 10, filter: "blur(4px)" }}
            animate={{ opacity: 1, scale: 1, y: 0, filter: "blur(0px)" }}
            exit={{ opacity: 0, scale: 0.94, y: 8, filter: "blur(3px)" }}
            transition={spring}
            style={{
              position: "absolute",
              bottom: "calc(100% + 12px)",
              [align === "end" ? "right" : "left"]: 0,
              width,
              ...panelStyle,
            }}
          >
            {sub && (
              <div
                onClick={() => setSub(null)}
                style={{
                  position: "relative", display: "flex", alignItems: "center", gap: 8,
                  height: 34, padding: "0 10px", marginBottom: 4, borderRadius: 10,
                  cursor: "pointer", fontSize: 12.5, color: "#5b6472",
                }}
              >
                <ChevronLeft size={15} /> {sub.label}
              </div>
            )}
            {sub && sub.searchable && (
              <div
                style={{
                  display: "flex", alignItems: "center", gap: 8, height: 32,
                  margin: "0 4px 6px", padding: "0 10px", borderRadius: 10,
                  background: "rgba(28,35,51,0.06)",
                }}
              >
                <Search size={13} color="#8b949e" aria-hidden />
                <input
                  autoFocus
                  value={subQuery}
                  onChange={(e) => setSubQuery(e.target.value)}
                  placeholder={sub.placeholder || "搜索"}
                  style={{
                    flex: 1, minWidth: 0, border: "0", outline: "none", background: "transparent",
                    fontSize: 12.5, color: "#1c2333",
                  }}
                />
                {sub.hint && (
                  <span style={{ fontSize: 11, color: "#8b949e", whiteSpace: "nowrap" }}>{sub.hint}</span>
                )}
              </div>
            )}
            {sub ? (
              (() => {
                const q = subQuery.trim().toLowerCase()
                const rows = q ? sub.items.filter((it) => it.label.toLowerCase().includes(q)) : sub.items
                return (
                  <>
                    {rows.map(renderRow)}
                    {sub.searchable && rows.length === 0 && (
                      <div style={{ padding: 10, fontSize: 12, color: "#a0a8b4", textAlign: "center" }}>
                        无匹配
                      </div>
                    )}
                  </>
                )
              })()
            ) : (
              groups.map((g, gi) => (
                <div key={g.id}>
                  {g.label && (
                    <div style={{ fontSize: 11, color: "#8b949e", padding: "6px 10px 2px", letterSpacing: 1 }}>
                      {g.label}
                    </div>
                  )}
                  {g.items.map(renderRow)}
                  {gi < groups.length - 1 && (
                    <div style={{ height: 1, background: "rgba(28,35,51,0.08)", margin: "5px 8px" }} />
                  )}
                </div>
              ))
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
