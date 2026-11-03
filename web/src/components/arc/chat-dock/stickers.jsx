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

/** 真实像素尺寸（本机 Pillow 读取 web/public/stickers/*.png 的宽高）。
    渲染时按最大边长等比缩到 max 以内，并把最终宽高同时写进 img 的
    width/height 属性——图片下载完成前浏览器就按比例占好位置，
    加载完不会再突然撑开、顶动下方内容（CLS）。 */
export const STICKER_SIZES = {
  崩溃: { w: 739, h: 415 },
  疑问: { w: 568, h: 488 },
  嘲讽: { w: 640, h: 452 },
}

/** 单个表情包：按原始比例显示，限制最大边长；加载前就有确定占位。 */
export function Sticker({ name, max = 132 }) {
  const src = STICKERS[name]
  if (!src) return null
  const size = STICKER_SIZES[name]
  const scale = size ? Math.min(1, max / size.w, max / size.h) : 1
  const width = size ? Math.round(size.w * scale) : max
  const height = size ? Math.round(size.h * scale) : max
  return (
    <img
      src={src}
      alt={`表情：${name}`}
      width={width}
      height={height}
      loading="eager"
      decoding="async"
      style={{
        display: "block",
        width,
        height,
        maxWidth: max,
        maxHeight: max,
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
