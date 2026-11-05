"""表情包触发率的离线调研：拿真实聊天记录跑规则，看到底该发多少张。

用途：改 `wants_sticker` / `_auto_sticker_query` 之前先量一遍，别凭感觉调正则。

    python scripts/sticker_research.py transcript.json

transcript.json：agent_chat 导出（scripts/export_chat.py 生成），
形如 [{"role": "user"|"assistant", "content": "..."}]。
按"用户这句 + Agent 那句"配对判定——真正决定配不配图的是**说出口的话**。
"""
import json
import sys

sys.path.insert(0, ".")

from ai.agent import wants_sticker  # noqa: E402


def pair_up(rows):
    """把对话配成 (user, assistant) 对。Agent 主动说的那种只有 assistant。"""
    pairs, pending = [], []
    for row in rows:
        if row.get("role") == "user":
            pending.append(row.get("content") or "")
        elif row.get("role") == "assistant":
            prompt = pending.pop(0) if pending else ""
            pairs.append((prompt, row.get("content") or ""))
    return pairs


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "transcript.json"
    rows = json.load(open(path, encoding="utf-8"))
    pairs = pair_up(rows)
    hits = 0
    print(f"共 {len(pairs)} 轮对话\n")
    for prompt, reply in pairs:
        query = wants_sticker(prompt, reply)
        if query:
            hits += 1
        mark = f"[{query}]" if query else "[  不发]"
        p = prompt.replace("\n", " ")[:34] or "(主动)"
        r = reply.replace("\n", " / ")[:56]
        print(f"{mark} 用户: {p:<34} → {r}")
    pct = hits / max(1, len(pairs)) * 100
    print(f"\n会配图: {hits}/{len(pairs)} ({pct:.0f}%)")


if __name__ == "__main__":
    main()
