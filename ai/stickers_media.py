"""表情包标记的解析与落地：把 `[sticker:...]` 变成真正能发出去的东西。

## 为什么需要它

网页端由前端渲染标记（`web/src/components/arc/chat-dock/stickers.jsx` 负责
把名字映射到 `/stickers/*.png`）。但 Telegram / 微信这些外部渠道不认识这个
标记，原样发过去就是用户看到的 `[sticker:崩溃]` 一串代码。

这里给出后端侧的同一份解析：
  · `split_stickers(text)` → (去掉标记的正文, [StickerRef...])
  · `StickerRef.resolve()` → 本地文件路径（内置图 / 落盘的 GIPHY 图），
    或者一个 https 地址（GIPHY 直链，渠道自己会去取）

## 名字映射是手工维护的

图片文件名用 ASCII（中文名在静态服务 + URL 编码这条路上不可靠，线上实测
只有"笑死"能取到 200）。所以名字 → 文件名这一层必须显式写出来，
`tests/test_stickers.py` 会和前端那份做交叉校验，改一处忘另一处会红。
"""
from __future__ import annotations

import os
import re
import urllib.parse

# 名字 → web/public/stickers/ 下的文件名。与前端 STICKERS 一一对应。
STICKER_FILES = {
    "崩溃": "breakdown.png",
    "疑问": "question.png",
    "嘲讽": "mock.png",
    "笑死": "laugh.gif",
    "问号": "what.gif",
    "困惑": "confused.gif",
    "生气": "angry.gif",
    "大哭": "cry.gif",
    "无语": "speechless.gif",
    "开心": "happy.gif",
    "点赞": "thumbsup.gif",
    "得意": "smug.gif",
    "惊吓": "shocked.gif",
}

# 与 ai/agent.py 的 _STICKER_TOKEN 保持一致（中英文冒号都认）
_MARKER = re.compile(r"\[sticker[:：]\s*([^\]]{1,600})\]")


class StickerRef:
    """一张待发送的表情。`kind` 决定渠道用哪个接口（photo / animation）。"""

    __slots__ = ("inner", "local_path", "remote_url", "kind")

    def __init__(self, inner: str, local_path: str = "", remote_url: str = ""):
        self.inner = inner
        self.local_path = local_path
        self.remote_url = remote_url
        ext = os.path.splitext(local_path or remote_url)[1].lower()
        # GIF 走 animation：Telegram 用 sendPhoto 发 GIF 会变成静止首帧，
        # 用户看到的就是"图不动"。动图必须用 sendAnimation。
        self.kind = "animation" if ext in (".gif", ".webp") else "photo"

    @property
    def available(self) -> bool:
        return bool(self.local_path and os.path.isfile(self.local_path)) or bool(self.remote_url)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<StickerRef {self.inner!r} local={self.local_path!r} url={self.remote_url!r}>"


def _builtin_path(name: str, public_dir: str) -> str:
    filename = STICKER_FILES.get(str(name or "").strip())
    if not filename:
        return ""
    return os.path.join(public_dir, filename)


def resolve(inner: str, public_dir: str, attach_dir: str) -> StickerRef:
    """把标记内容解析成可发送的引用。

    inner 的两种形式：
      崩溃                       —— 内置图名字
      url=%2Fagent-attachments%2Fstickers%2Fx.gif&w=480&h=480
      或 url=https://media.giphy.com/...  —— 落盘/远程图
    """
    inner = str(inner or "").strip()
    if not inner:
        return StickerRef("")
    if inner.startswith("url=") or "&url=" in inner:
        try:
            params = urllib.parse.parse_qs(inner, keep_blank_values=True)
        except ValueError:
            params = {}
        url = (params.get("url") or [""])[0]
        url = urllib.parse.unquote(url)
        if url.startswith("/agent-attachments/"):
            sub = url[len("/agent-attachments/"):].split("?", 1)[0]
            base = os.path.abspath(attach_dir)
            target = os.path.abspath(os.path.join(base, sub))
            # 目录穿越防护：标记是模型写出来的，不能让它读到任意文件
            if target == base or target.startswith(base + os.sep):
                return StickerRef(inner, local_path=target)
            return StickerRef(inner)
        if url.startswith(("https://", "http://")):
            return StickerRef(inner, remote_url=url)
        return StickerRef(inner)
    # 裸名字：内置图
    return StickerRef(inner, local_path=_builtin_path(inner, public_dir))


def locate(inner: str, public_dir: str = "", attach_dir: str = "",
           dist_dir: str = "") -> str:
    """给"需要公网 URL"的渠道找内置图的实际文件路径。

    内置图的映射表指向 web/public/stickers（源目录，永远在仓库里），
    但静态服务对外提供的是 web/dist/stickers（构建产物）。两者内容一致，
    路径不同——所以这里按"先源目录、再 dist"的顺序找，找不到返回空。
    """
    name = str(inner or "").strip()
    filename = STICKER_FILES.get(name)
    if not filename:
        return ""
    for base in (public_dir, dist_dir):
        if not base:
            continue
        target = os.path.join(base, filename)
        if os.path.isfile(target):
            return target
    return ""


def split_stickers(text: str, public_dir: str = "", attach_dir: str = "") -> tuple[str, list[StickerRef]]:
    """把一段回复拆成「正文 + 表情列表」。标记本身不会留在正文里。

    public_dir / attach_dir 为空时只做剥离、不解析路径（有些调用方只关心正文）。
    """
    raw = str(text or "")
    found = []
    for match in _MARKER.finditer(raw):
        inner = match.group(1).strip()
        if public_dir and attach_dir:
            ref = resolve(inner, public_dir, attach_dir)
        else:
            ref = StickerRef(inner)
        if ref.inner:
            found.append(ref)
    body = _MARKER.sub("", raw)
    # 标记常独占一行：清掉留下的空行，否则气泡里会出现一段空白
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, found


def to_markdown(text: str, base_url: str, public_dir: str,
                attach_dir: str, dist_dir: str = "") -> str:
    """把标记换成 Markdown 图片，给只能收文本的渠道（OpenClaw/微信）用。

    为什么不直接删掉：微信里用户就是想看图。用 `![](url)` 让对方客户端
    去渲染，是这类渠道最通用的表达方式；对方不渲染时至少还能点开链接，
    不会变成一串 `[sticker:xxx]` 代码。
    """
    body, refs = split_stickers(text, public_dir, attach_dir)
    parts = [body] if body else []
    for ref in refs:
        if ref.remote_url:
            parts.append(f"![]({ref.remote_url})")
            continue
        name = os.path.basename(ref.local_path or "")
        if not name:
            # 内置图：按名字找到真实文件（源目录优先，再退回构建产物）
            name = os.path.basename(
                locate(ref.inner, public_dir, attach_dir, dist_dir) or "")
        if name:
            # 内置图随 frontend 一起对外提供，直接用公网地址
            parts.append(f"![{ref.inner}]({base_url.rstrip('/')}/stickers/{name})")
    return "\n\n".join(p for p in parts if p)
