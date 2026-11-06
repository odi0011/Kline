"""清理贴纸库里"内容为空"的图。

线上真实事故：GIPHY 用「无语」搜出来的第一张是加载圈动画
（480×480、10 帧、99.6% 全透明）。字节完全合法，所以之前所有
校验都通过了；但在白底上它就是一片空白，用户看到的是"图打不开"。

按新规则（_usable_image：透明/近白像素超过 98% 视为空图）扫一遍，
把不合格的记录和文件一起删掉。只读之外唯一的动作就是删这些垃圾。
"""
import os
import sys

sys.path.insert(0, "/opt/kline-monitor")

from ai.sticker_cache import StickerCache, _usable_image

ROOT = "/opt/kline-monitor/agent-attachments/stickers"
store = None
try:
    import secrets_store
    import yaml

    from storage import Storage
    CFG = "/opt/kline-monitor/config.yaml"
    cfg = secrets_store.decrypt_config(
        yaml.safe_load(open(CFG, encoding="utf-8")), CFG)
    store = Storage(cfg.get("mysql") or {})
except Exception as exc:
    print("（无数据库，只清本地）", str(exc)[:80])

cache = StickerCache(ROOT, store=store)
removed = []
kept = 0
# 拷一份再遍历：_drop_record 会改索引
for sid, record in list(cache._index["stickers"].items()):
    if not isinstance(record, dict):
        continue
    filename = str(record.get("file") or "")
    path = os.path.join(ROOT, filename)
    if not filename or not os.path.isfile(path):
        continue
    try:
        raw = open(path, "rb").read(2_000_000)
    except OSError:
        continue
    if _usable_image(raw):
        kept += 1
        continue
    removed.append((sid, record.get("name") or filename,
                    os.path.getsize(path)))
    try:
        cache._drop_record(sid)
    except Exception:
        pass
    try:
        os.remove(path)
    except OSError:
        pass

print(f"保留 {kept} 张，删除 {len(removed)} 张空图：")
for sid, name, size in removed:
    print(f"  - {name} ({sid}, {size}B)")
