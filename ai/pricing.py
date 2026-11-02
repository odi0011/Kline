"""模型调用计价：按服务商官方价目表算每次对话的真实花费。

**只做算术，不猜价格。** 价目表硬编码在下面，附官方来源与核对日期；
服务商调价时改这张表即可。

DeepSeek 计费规则（api-docs.deepseek.com/quick_start/pricing，2026-11 核对）：
  · 输入分"缓存命中"与"缓存未命中"两档，按实际命中的 token 数分别计价
  · 输出单独一档
  · **峰谷计价**：高峰时段按标准价，其余时段五折
  · 高峰时段（UTC）：01:00–04:00 与 06:00–10:00，周一至周五，
    排除中国法定节假日；其余时间（周末、节假日、其他钟点）均为谷时
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 各模型的**真实上下文窗口**（token）。
# 不能用配置里的 ctx：那是用户随手填的（线上填了 2000，实际窗口 128K），
# 用错的上限会让"一句话就占 69%"这种荒唐数字。
MODEL_CONTEXT = {
    "deepseek-flash": 131072,
    "deepseek-v4-pro": 1048576,
    "deepseek-chat": 65536,
    "deepseek-reasoner": 65536,
}

# 每 100 万 token 的美元价格：(谷时, 峰时)
# 谷时 = 峰时的 50%，这是官方公布的规则，不是我们自己折的。
PRICES: dict[str, dict[str, tuple[float, float]]] = {
    "deepseek-flash": {
        "cache_hit": (0.003, 0.006),
        "cache_miss": (0.15, 0.30),
        "output": (0.60, 1.20),
    },
    "deepseek-v4-pro": {
        "cache_hit": (0.022, 0.044),
        "cache_miss": (0.66, 1.32),
        "output": (1.98, 3.96),
    },
}

# 按前缀兜底：服务商常给带日期的型号名（deepseek-flash-1017 之类）
_ALIASES = (
    ("deepseek-flash", "deepseek-flash"),
    ("deepseek-v4-pro", "deepseek-v4-pro"),
    ("deepseek-reasoner", "deepseek-v4-pro"),
    ("deepseek-chat", "deepseek-flash"),
)

# 高峰时段（UTC 小时，左闭右开）
PEAK_WINDOWS = ((1, 4), (6, 10))
CHINA_TZ = timezone(timedelta(hours=8))

# 人民币展示汇率。可以在 config 的 ai 段用 fx_rate 覆盖。
DEFAULT_FX = 7.2

# 中国法定节假日（含调休上班日不在此表）。只用于峰谷判定：
# 这些日子的**全天**都算谷时。每年更新一次即可。
CN_HOLIDAYS = {
    # 2025
    "2025-01-01",
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-01", "2025-02-02",
    "2025-02-03", "2025-02-04",
    "2025-04-04", "2025-04-05", "2025-04-06",
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",
    "2025-05-31", "2025-06-01", "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04", "2025-10-05",
    "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    "2026-10-06", "2026-10-07",
}


def canonical_model(model_id: str) -> str:
    """把带日期/后缀的型号名归一到价目表里的键。"""
    name = str(model_id or "").strip().lower()
    if name in PRICES:
        return name
    for prefix, key in _ALIASES:
        if name.startswith(prefix):
            return key
    return ""


def is_peak(moment: datetime | None = None) -> bool:
    """当前是否处于高峰计价时段。

    高峰 = UTC 周一至周五、01:00–04:00 或 06:00–10:00，且不是中国法定假日。
    其它一切情况都是谷时——包括周末、节假日和其余钟点。
    """
    now = moment or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    utc = now.astimezone(timezone.utc)
    # 周末（UTC 的周六周日）全天谷时
    if utc.weekday() >= 5:
        return False
    # 中国节假日按北京时间日期判断
    if utc.astimezone(CHINA_TZ).strftime("%Y-%m-%d") in CN_HOLIDAYS:
        return False
    hour = utc.hour
    return any(start <= hour < end for start, end in PEAK_WINDOWS)


def context_window(model_id: str, configured=None) -> int | None:
    """模型真实上下文窗口。

    优先用内置的真实值（按型号前缀归一）；型号不认识时才退回配置值。
    **不取两者的较小值**——配置里的 ctx 常常填错（线上填 2000，
    真实窗口 128K），取较小值会算出"一句话占 69%"这种假数字。
    """
    key = canonical_model(model_id)
    if key and key in MODEL_CONTEXT:
        return MODEL_CONTEXT[key]
    try:
        value = int(configured or 0)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def price_of(model_id: str, moment: datetime | None = None):
    """返回 (价目表, 是否峰时)；型号不认识时返回 (None, False)。"""
    table = PRICES.get(canonical_model(model_id))
    if table is None:
        return None, False
    return table, is_peak(moment)


def compute_cost(model_id: str, usage: dict, moment: datetime | None = None) -> dict:
    """按 usage 算这次调用的美元花费。

    返回 {usd, cny, peak, known}：
      known=False 表示型号不在价目表里，金额不可信，调用方应显示 "—"。
    """
    table, peak = price_of(model_id, moment)
    if table is None:
        return {"usd": None, "cny": None, "peak": peak, "known": False}

    def num(key):
        value = usage.get(key)
        return float(value) if type(value) in (int, float) and value > 0 else 0.0

    prompt = num("prompt_tokens")
    completion = num("completion_tokens")
    # 服务商给的命中数；没给就当作全部未命中（保守，宁可算贵一点）
    hit = num("prompt_cache_hit_tokens")
    if "prompt_cache_hit_tokens" not in usage:
        hit = 0.0
    hit = min(hit, prompt)
    miss = max(0.0, prompt - hit)

    idx = 1 if peak else 0
    cost_hit = hit / 1_000_000 * table["cache_hit"][idx]
    cost_miss = miss / 1_000_000 * table["cache_miss"][idx]
    cost_out = completion / 1_000_000 * table["output"][idx]
    usd = cost_hit + cost_miss + cost_out
    return {
        "usd": round(usd, 8),
        "cny": None,            # 汇率由调用方补，便于统一配置
        "peak": peak,
        "known": True,
        "parts": {
            "cacheHitTokens": int(hit),
            "cacheMissTokens": int(miss),
            "outputTokens": int(completion),
            "usdHit": round(cost_hit, 8),
            "usdMiss": round(cost_miss, 8),
            "usdOut": round(cost_out, 8),
        },
    }


def describe(model_id: str, usage: dict, fx: float | None = None,
             moment: datetime | None = None) -> dict:
    """给前端的一行计价摘要：金额 + 峰谷 + 逐项明细。"""
    out = compute_cost(model_id, usage, moment)
    rate = fx if (isinstance(fx, (int, float)) and fx > 0) else DEFAULT_FX
    if not out["known"]:
        return {"cost": None, "costUsd": None, "peak": None,
                "note": f"{model_id or '未知型号'} 不在价目表内，费用未计"}
    out["cny"] = round(out["usd"] * rate, 6)
    parts = out["parts"]
    phase = "峰时" if out["peak"] else "谷时"
    return {
        "cost": out["cny"],                     # 前端展示用人民币
        "costUsd": out["usd"],
        "peak": out["peak"],
        "parts": parts,
        "fx": rate,
        # 一行可读说明，挂在 title 上：谁乘谁一目了然
        "note": (
            f"{phase}｜命中 {parts['cacheHitTokens']:,} tok × "
            f"${parts['usdHit']:.6f} + 未命中 {parts['cacheMissTokens']:,} tok × "
            f"${parts['usdMiss']:.6f} + 输出 {parts['outputTokens']:,} tok × "
            f"${parts['usdOut']:.6f} = ${out['usd']:.6f} ≈ ¥{out['cny']:.4f}"
        ),
    }
