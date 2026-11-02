/* 玻璃拟态菜单——项目里所有下拉/弹出菜单的唯一实现。
   API：groups / children 触发器 / onSelect / checked+keepOpen / align / width。
   行样式与 news-feed、quant-panel、position-history 的裸列表一致（22px / 圆角 5 /
   11px / 浅底 hover），面板材质与其它玻璃浮层一致，滚动交给统一 ScrollArea。

   面板用 portal 挂到 body：菜单一定要浮在所有面板之上，否则会被父级的
   z-index/overflow 裁掉、或者被层级更高的浮钮压住。 */
import { cloneElement, useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { AnimatePresence, motion } from "motion/react"
import { Check, ChevronLeft, Search } from "lucide-react"
import { LiquidGlass } from "@/components/ui/liquid-glass"
import { ScrollArea } from "@/components/arc/scroll-area/scroll-area"
import styles from "./glass-menu.module.css"

const spring = { type: "spring", stiffness: 520, damping: 40, mass: 0.8 }
// 与 .row 的 22px 必须一致，否则虚拟滚动的落点会错位
const ROW_H = 22
const OVERSCAN = 8
/** 超过这个条数才走虚拟滚动：短菜单全量渲染更省事，也避免一开就抖 */
const VIRTUAL_MIN = 40
/** 面板四周留白，也是贴边翻转的判定阈值 */
const GAP = 8

/* 子菜单长列表：固定行高 + 只渲染可视窗口。币种菜单可能几百项，
   全量挂载会同时触发几百个头像请求并拖慢菜单展开。 */
function SubList({ rows, renderRow, searchable, scroll, viewH, listRef, onScroll }) {
  const total = rows.length
  if (searchable && total === 0) return <p className={styles.empty}>无匹配</p>
  if (total <= VIRTUAL_MIN) return <>{rows.map(renderRow)}</>
  const start = Math.max(0, Math.floor(scroll / ROW_H) - OVERSCAN)
  const end = Math.min(total, Math.ceil((scroll + viewH) / ROW_H) + OVERSCAN)
  return (
    <ScrollArea orientation="vertical" fade={10} scrollbars="auto" maxHeight={320}
      viewportRef={listRef} onScroll={onScroll} label="菜单选项">
      <div style={{ position: "relative", height: total * ROW_H }}>
        {rows.slice(start, end).map((item, i) => (
          <div key={item.id} style={{ position: "absolute", top: (start + i) * ROW_H, left: 0, right: 0 }}>
            {renderRow(item)}
          </div>
        ))}
      </div>
    </ScrollArea>
  )
}

export function GlassMenu({
  label = "Actions",
  triggerLabel,
  groups = [],
  children,
  onSelect,
  align = "end",
  /** 期望的展开方向；空间不够时自动翻到另一侧 */
  side = "top",
  width = 236,
  /** "pill"=内置液态玻璃圆钮；"custom"=用 children 自己渲染按钮并接管开关 */
  trigger = "pill",
  open: controlledOpen,
  onOpenChange,
  className = "",
  style,
}) {
  const [innerOpen, setInnerOpen] = useState(false)
  const open = controlledOpen ?? innerOpen
  const setOpen = (next) => {
    const value = typeof next === "function" ? next(open) : next
    if (controlledOpen === undefined) setInnerOpen(value)
    onOpenChange?.(value)
  }
  const [hoverId, setHoverId] = useState(null)
  const [sub, setSub] = useState(null)          // 打开的子菜单父项
  const [subQuery, setSubQuery] = useState("")  // searchable 子菜单的搜索词
  const [subScroll, setSubScroll] = useState(0)
  const [subViewH] = useState(320)
  const [spot, setSpot] = useState(null)        // 面板的 fixed 定位
  const rootRef = useRef(null)
  const panelRef = useRef(null)
  const subListRef = useRef(null)

  /** 按触发器的实际位置算面板落点：优先期望方向，放不下就翻到另一边；
      高度也按可用空间收，长菜单内部滚动而不是顶出视口。 */
  const place = () => {
    // 定位基准取触发器按钮本身：pill 模式是 LiquidGlass 内层按钮，
    // custom 模式是调用方给的按钮（WAI 要求菜单贴着触发元素）
    const anchor = rootRef.current?.querySelector("button") || rootRef.current
    if (!anchor) return
    const box = anchor.getBoundingClientRect()
    const spaceAbove = box.top - GAP * 2
    const spaceBelow = window.innerHeight - box.bottom - GAP * 2
    let next = side
    if (side === "top" && spaceAbove < 120 && spaceBelow > spaceAbove) next = "bottom"
    if (side === "bottom" && spaceBelow < 120 && spaceAbove > spaceBelow) next = "top"
    const top = next === "top" ? undefined : Math.round(box.bottom + GAP)
    const bottom = next === "top" ? Math.round(window.innerHeight - box.top + GAP) : undefined
    setSpot({
      top, bottom,
      left: align === "start" ? Math.round(box.left) : undefined,
      right: align === "end" ? Math.round(window.innerWidth - box.right) : undefined,
      maxHeight: Math.max(120, (next === "top" ? spaceAbove : spaceBelow) - GAP),
    })
  }

  useLayoutEffect(() => {
    if (!open) { setSpot(null); return }
    place()
    // 滚动/缩放时跟着触发器走，别让菜单漂在错误位置
    window.addEventListener("resize", place)
    window.addEventListener("scroll", place, true)
    return () => {
      window.removeEventListener("resize", place)
      window.removeEventListener("scroll", place, true)
    }
  }, [open, side, align, trigger])

  useEffect(() => {
    if (!open) { setSub(null); setHoverId(null); setSubQuery(""); setSubScroll(0); return }
    const onDoc = (e) => {
      if (rootRef.current?.contains(e.target)) return
      if (panelRef.current?.contains(e.target)) return
      setOpen(false)
    }
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
    if (item.items) { setSub(item); setSubQuery(""); setSubScroll(0); return }
    onSelect?.(item.id, item)
    if (!item.keepOpen) setOpen(false)
  }

  const renderRow = (item) => (
    <div
      key={item.id}
      role={item.checked != null ? "menuitemcheckbox" : "menuitem"}
      aria-checked={item.checked}
      aria-disabled={item.disabled || undefined}
      data-danger={item.danger || undefined}
      onMouseEnter={() => setHoverId(item.id)}
      onClick={() => rowClick(item)}
      className={styles.row}
    >
      {hoverId === item.id && !item.disabled && (
        <motion.div
          layoutId="glass-menu-pill"
          transition={spring}
          style={{ position: "absolute", inset: 0, borderRadius: 5, background: "rgba(28, 35, 51, 0.05)" }}
        />
      )}
      {item.icon && <span className={styles.icon}>{item.icon}</span>}
      <span className={styles.label}>{item.label}</span>
      {(item.shortcut || item.checked != null || item.items) && (
        <span className={styles.trail}>
          {item.shortcut && <span>{item.shortcut}</span>}
          {item.checked != null && (
            <span className={styles.check} data-off={!item.checked || undefined}>
              <Check size={12} strokeWidth={2.8} />
            </span>
          )}
          {item.items && !sub && <span>›</span>}
        </span>
      )}
    </div>
  )

  // 子菜单渲染时按 id 取最新的菜单项：勾选态/禁用态/计数随父级状态刷新
  // （setSub 存的是旧对象快照，直接用它会导致二级面板勾选后不更新）
  const flatItems = groups.flatMap((g) => g.items)
  const subItem = sub ? flatItems.find((it) => it.id === sub.id) || sub : null

  const panel = spot && (
    <AnimatePresence>
      {open && (
        <motion.div
          ref={panelRef}
          role="menu"
          aria-label={label}
          data-glass-menu=""
          initial={{ opacity: 0, y: spot.top != null ? -8 : 8, filter: "blur(4px)" }}
          animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
          exit={{ opacity: 0, y: spot.top != null ? -6 : 6, filter: "blur(3px)" }}
          transition={spring}
          style={{
            position: "fixed",
            top: spot.top, bottom: spot.bottom, left: spot.left, right: spot.right,
            width, zIndex: 10000,
          }}
          className={styles.panel}
        >
          <ScrollArea orientation="vertical" fade={8} scrollbars="auto" maxHeight={spot.maxHeight}
            label={label}>
            {subItem && (
              <div className={styles.row} onClick={() => setSub(null)} style={{ color: "#5b6472" }}>
                <span className={styles.icon}><ChevronLeft size={12} /></span>
                <span className={styles.label}>{subItem.label}</span>
              </div>
            )}
            {subItem && subItem.searchable && (
              <div className={styles.search}>
                <Search size={11} aria-hidden />
                <input
                  autoFocus
                  value={subQuery}
                  onChange={(e) => setSubQuery(e.target.value)}
                  placeholder={subItem.placeholder || "搜索"}
                />
                {subItem.hint && <span className={styles.hint}>{subItem.hint}</span>}
              </div>
            )}
            {sub && subItem ? (
              <SubList rows={subQuery.trim() ? subItem.items.filter((it) =>
                it.label.toLowerCase().includes(subQuery.trim().toLowerCase())) : subItem.items}
                renderRow={renderRow} searchable={subItem.searchable}
                scroll={subScroll} viewH={subViewH} listRef={subListRef}
                onScroll={(event) => setSubScroll(event.currentTarget.scrollTop)} />
            ) : (
              groups.map((g, gi) => (
                <div key={g.id}>
                  {g.label && <div className={styles.groupLabel}>{g.label}</div>}
                  {g.items.map(renderRow)}
                  {gi < groups.length - 1 && <div className={styles.divider} />}
                </div>
              ))
            )}
          </ScrollArea>
        </motion.div>
      )}
    </AnimatePresence>
  )

  return (
    <div ref={rootRef} className={className} style={{ position: "relative", ...style }}>
      {trigger === "pill" ? (
        // 触发按钮：液态玻璃圆钮（Aceternity LiquidGlass，aberration=0 单次位移最轻链路）
        <LiquidGlass radius={999} scale={36} aberration={0} frost={2.5} tint={0.14}
          className="shadow-[0_8px_24px_rgba(16,24,40,0.18)]" style={{ display: "block" }}>
          <motion.button
            type="button"
            aria-haspopup="menu"
            aria-expanded={open}
            aria-label={triggerLabel || label}
            onClick={() => setOpen((o) => !o)}
            whileTap={{ scale: 0.92 }}
            style={{
              display: "grid", placeItems: "center", width: 42, height: 42, borderRadius: 999,
              border: "1px solid rgba(255,255,255,0.55)",
              background: "rgba(255,255,255,0.18)",
              color: "#1c2333", cursor: "pointer",
            }}
          >
            {children}
          </motion.button>
        </LiquidGlass>
      ) : (
        // 外部触发器：由调用方渲染按钮；这里只负责开关。
        // 用 cloneElement 把 onClick 直接挂到按钮上，而不是套一层 div ——
        // 套层会让指针事件被内层 stopPropagation 吃掉，菜单就点不开。
        cloneElement(children, {
          onClick: (event) => {
            children.props?.onClick?.(event)
            if (!event.defaultPrevented) setOpen((o) => !o)
          },
          "aria-haspopup": "menu",
          "aria-expanded": open,
        })
      )}
      {createPortal(panel, document.body)}
    </div>
  )
}
