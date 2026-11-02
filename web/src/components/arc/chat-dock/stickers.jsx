/* 对话用的表情包：原创的简笔漫画脸，不是 emoji、也不用任何他人素材。

   为什么自己画：需求要的是"像表情包那样"的反应，但直接用现成漫画
   素材有版权问题，emoji 又太"系统"。这里用 SVG 画一组风格统一的简笔脸，
   表情夸张、线条粗，接近漫画分镜的观感。

   每个表情都是 96×96 的 viewBox，前端按 44px 显示。
   新增表情只要往 STICKERS 里加一项，后端提示词里的名字列表会自动同步
   （见 ai/agent.py 的 STICKER_NAMES）。 */

const Face = ({ children, bg = "#fff" }) => (
  <>
    <circle cx="48" cy="48" r="44" fill={bg} stroke="#1c2333" strokeWidth="3.5" />
    {children}
  </>
)

/** 眼睛：默认是大圆眼，加高光；closed 时画成弧线 */
const Eyes = ({ closed = false, look = 0 }) => {
  if (closed) {
    return (
      <>
        <path d="M26 46q8 -7 16 0" stroke="#1c2333" strokeWidth="3.5" fill="none" strokeLinecap="round" />
        <path d="M54 46q8 -7 16 0" stroke="#1c2333" strokeWidth="3.5" fill="none" strokeLinecap="round" />
      </>
    )
  }
  return (
    <>
      <ellipse cx={34 + look} cy="44" rx="7" ry="9" fill="#1c2333" />
      <ellipse cx={62 + look} cy="44" rx="7" ry="9" fill="#1c2333" />
      <circle cx={36 + look} cy="41" r="2.4" fill="#fff" />
      <circle cx={64 + look} cy="41" r="2.4" fill="#fff" />
    </>
  )
}

const Mouth = ({ d, fill = "#1c2333" }) => (
  <path d={d} fill={fill} stroke="#1c2333" strokeWidth="2.6" strokeLinejoin="round" />
)

export const STICKERS = {
  // 哭：两行泪 + 张大的嘴
  哭: (
    <>
      <Face bg="#f2f5f9"><Eyes closed /></Face>
      <path d="M30 52q-4 18 2 26" stroke="#3b82f6" strokeWidth="4" fill="none" strokeLinecap="round" />
      <path d="M66 52q4 18 -2 26" stroke="#3b82f6" strokeWidth="4" fill="none" strokeLinecap="round" />
      <Mouth d="M38 66q10 12 20 0q-10 4 -20 0" fill="#e0556b" />
    </>
  ),
  // 摆手：拒绝 / 别问
  摆手: (
    <>
      <Face bg="#f7f5f0"><Eyes look={-2} /></Face>
      <path d="M36 68q12 6 24 0" stroke="#1c2333" strokeWidth="3.2" fill="none" strokeLinecap="round" />
      <path d="M74 60q10 -6 12 -14M74 68q12 -2 16 -10" stroke="#1c2333" strokeWidth="4"
        fill="none" strokeLinecap="round" />
    </>
  ),
  // 震惊：大眼 + 小圆嘴
  震惊: (
    <>
      <Face bg="#fff6e8" />
      <ellipse cx="34" cy="42" rx="9" ry="11" fill="#fff" stroke="#1c2333" strokeWidth="3" />
      <ellipse cx="62" cy="42" rx="9" ry="11" fill="#fff" stroke="#1c2333" strokeWidth="3" />
      <circle cx="34" cy="44" r="4" fill="#1c2333" />
      <circle cx="62" cy="44" r="4" fill="#1c2333" />
      <ellipse cx="48" cy="68" rx="7" ry="9" fill="#1c2333" />
    </>
  ),
  // 笑：弯眼 + 大张口
  笑: (
    <>
      <Face bg="#fff7d6"><Eyes closed /></Face>
      <Mouth d="M34 58q14 20 28 0q-14 8 -28 0" fill="#e0556b" />
    </>
  ),
  // 无语：一条横眼 + 平嘴
  无语: (
    <>
      <Face bg="#eef1f4" />
      <path d="M26 44h16M54 44h16" stroke="#1c2333" strokeWidth="3.5" strokeLinecap="round" />
      <path d="M38 68h20" stroke="#1c2333" strokeWidth="3.2" strokeLinecap="round" />
    </>
  ),
  // 疑惑：一高一低的眼 + 问号
  疑惑: (
    <>
      <Face bg="#f0f7ff" />
      <circle cx="34" cy="44" r="6.5" fill="#1c2333" />
      <path d="M54 42q8 -6 16 2" stroke="#1c2333" strokeWidth="3.4" fill="none" strokeLinecap="round" />
      <path d="M38 68q10 -5 20 0" stroke="#1c2333" strokeWidth="3.2" fill="none" strokeLinecap="round" />
      <text x="80" y="24" fontSize="26" fontWeight="900" fill="#1c2333" textAnchor="middle">?</text>
    </>
  ),
  // 得意：眯眼 + 坏笑
  得意: (
    <>
      <Face bg="#eafaf1"><Eyes closed /></Face>
      <path d="M34 62q14 14 28 -2" stroke="#1c2333" strokeWidth="3.4" fill="none" strokeLinecap="round" />
      <path d="M78 30l4 4l-8 8" stroke="#f0b90b" strokeWidth="3.4" fill="none" strokeLinecap="round" />
    </>
  ),
  // 冷汗：竖线 + 歪嘴（尴尬）
  冷汗: (
    <>
      <Face bg="#f4f6f8"><Eyes look={3} /></Face>
      <path d="M84 30q6 12 0 18q-6 -6 0 -18" fill="#7cc0f5" stroke="#4a9fe0" strokeWidth="2" />
      <path d="M38 70q6 -8 10 0q4 8 12 0" stroke="#1c2333" strokeWidth="3.2" fill="none" strokeLinecap="round" />
    </>
  ),
}

export const STICKER_NAMES = Object.keys(STICKERS)

/** 单个表情包：统一 96 viewBox，按 size 显示 */
export function Sticker({ name, size = 44 }) {
  const body = STICKERS[name]
  if (!body) return null
  return (
    <svg width={size} height={size} viewBox="0 0 96 96" role="img"
      aria-label={`表情：${name}`} style={{ display: "block" }}>
      {body}
    </svg>
  )
}

/** 从一段文字里拆出 [sticker:名字]，返回 { text, sticker } */
export function splitSticker(raw) {
  const src = String(raw || "")
  const match = src.match(/\[sticker[:：]\s*([^\]]{1,12})\]/)
  if (!match) return { text: src.trim(), sticker: "" }
  const name = match[1].trim()
  const text = (src.slice(0, match.index) + src.slice(match.index + match[0].length)).trim()
  return { text, sticker: STICKERS[name] ? name : "" }
}
