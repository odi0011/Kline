/* 对话用的表情包：直接使用用户提供的漫画分镜素材。

   素材放在 web/public/stickers/，按下面 STICKERS 里的名字映射。
   新增一个表情 = 把图丢进 public/stickers/ + 在这里加一行。
   后端 ai/agent.py 的 STICKER_NAMES 必须同步（有测试强校验）。 */

export const STICKERS = {
  // 崩溃 / 破防：仰头抓头发、哭喊
  崩溃: "/stickers/breakdown.png",
  // 疑问 / 追问：家人们会涨还是会跌啊
  疑问: "/stickers/question.png",
  // 嘲讽 / 调侃：老实赚钱很枯燥吧？
  嘲讽: "/stickers/mock.png",
}

export const STICKER_NAMES = Object.keys(STICKERS)

/** 单个表情包：按原始比例显示，限制最大边长 */
export function Sticker({ name, max = 130 }) {
  const src = STICKERS[name]
  if (!src) return null
  return (
    <img
      src={src}
      alt={`表情：${name}`}
      loading="lazy"
      decoding="async"
      style={{
        display: "block",
        maxWidth: max,
        maxHeight: max,
        width: "auto",
        height: "auto",
        borderRadius: 10,
      }}
    />
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
