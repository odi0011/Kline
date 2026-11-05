/* 对话用的表情包：直接使用用户提供的漫画分镜素材。

   素材放在 web/public/stickers/，按下面 STICKERS 里的名字映射。
   新增一个表情 = 把图丢进 public/stickers/ + 在这里加一行。
   后端 ai/agent.py 的 STICKER_NAMES 必须同步（有测试强校验）。

   另外支持"在线贴纸"：配了 GIPHY key 后，Agent 可以按情绪词去网上找图，
   第一次命中时服务器会把它下载到 agent-attachments/stickers/，
   之后同一个词直接读本地文件——浏览器始终只访问我们自己的地址，
   不受 GIPHY CDN 可达性影响，也几乎不消耗 API 配额。 */
import { useState } from "react"

export const STICKERS = {
  // 崩溃 / 破防：仰头抓头发、哭喊
  崩溃: "/stickers/breakdown.png",
  // 疑问 / 追问：家人们会涨还是会跌啊
  疑问: "/stickers/question.png",
  // 嘲讽 / 调侃：老实赚钱很枯燥吧？
  嘲讽: "/stickers/mock.png",
  // ---- 二次元梗图（GIPHY anime 类目里手挑的）----
  笑死: "/stickers/笑死.gif",
  问号: "/stickers/问号.gif",
  困惑: "/stickers/困惑.gif",
  生气: "/stickers/生气.gif",
  大哭: "/stickers/大哭.gif",
  无语: "/stickers/无语.gif",
  开心: "/stickers/开心.gif",
  点赞: "/stickers/点赞.gif",
  得意: "/stickers/得意.gif",
  惊吓: "/stickers/惊吓.gif",
}

export const STICKER_NAMES = Object.keys(STICKERS)

/** 真实像素尺寸（本机 Pillow 读取 web/public/stickers/* 的宽高）。
    渲染时按最大边长等比缩到 max 以内，并把最终宽高同时写进 img 的
    width/height 属性——图片下载完成前浏览器就按比例占好位置，
    加载完不会再突然撑开、顶动下方内容（CLS）。 */
export const STICKER_SIZES = {
  崩溃: { w: 739, h: 415 },
  疑问: { w: 568, h: 488 },
  嘲讽: { w: 640, h: 452 },
  笑死: { w: 320, h: 320 },
  问号: { w: 320, h: 160 },
  困惑: { w: 240, h: 240 },
  生气: { w: 320, h: 320 },
  大哭: { w: 219, h: 240 },
  无语: { w: 320, h: 320 },
  开心: { w: 320, h: 320 },
  点赞: { w: 302, h: 300 },
  得意: { w: 320, h: 320 },
  惊吓: { w: 240, h: 225 },
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

/** 从一段文字里拆出 [sticker:...]，返回 { text, sticker, remote }

    两种标记：
      [sticker:崩溃]                       → 内置图，sticker 是名字
      [sticker:url=https%3A%2F%2F...&w=480&h=270]
                                           → 服务器已落盘的在线贴纸，remote 是
                                             { url, w, h }（本地地址，不跨境）
    在线贴纸的地址和尺寸一起带过来，前端不用再发请求就知道该占多大，
    加载完不会撑开气泡。
*/
export function splitSticker(raw) {
  const src = String(raw || "")
  // 内置名很短；在线标记带 url=/w=/h=，长度远超旧实现的 12 字符上限
  const match = src.match(/\[sticker[:：]\s*([^\]]{1,600})\]/)
  if (!match) return { text: src.trim(), sticker: "", remote: null }
  const payload = match[1].trim()
  const text = (src.slice(0, match.index) + src.slice(match.index + match[0].length)).trim()
  if (payload.includes("url=")) {
    try {
      const params = new URLSearchParams(payload)
      const url = params.get("url") || ""
      const w = Number(params.get("w")) || 0
      const h = Number(params.get("h")) || 0
      // 只接受服务器本地地址或 GIPHY 域名：别让模型能塞任意第三方图片进页面
      const local = url.startsWith("/agent-attachments/")
      const giphy = /^https:\/\/[^/]*giphy/i.test(url)
      if ((local || giphy) && w > 0 && h > 0) {
        // credit：这张图是否需要署名。GIPHY 的 API 条款要求展示其内容时
        // 标注 "Powered by GIPHY"；我们自己/用户提供的图不需要，
        // 否则每张贴纸下面都挂一行，很碍眼。
        const credit = params.get("src") === "giphy" || giphy
        return { text, sticker: "", remote: { url, w, h, credit } }
      }
    } catch {
      // 解析失败就当没有表情包，文字照常显示
    }
    return { text, sticker: "", remote: null }
  }
  return { text, sticker: STICKERS[payload] ? payload : "", remote: null }
}

/** 在线贴纸：地址和尺寸都已知，先按比例占位再加载，失败则静默隐藏。 */
export function RemoteSticker({ remote, max = 132 }) {
  const [failed, setFailed] = useState(false)
  if (!remote || failed) return null
  const scale = Math.min(1, max / remote.w, max / remote.h)
  const width = Math.round(remote.w * scale)
  const height = Math.round(remote.h * scale)
  return (
    <img
      src={remote.url}
      alt="表情"
      width={width}
      height={height}
      loading="eager"
      decoding="async"
      onError={() => setFailed(true)}
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

/* 已按要求移除 "Powered by GIPHY" 署名：这是个人自用站点，观感优先。 */
