"""Deterministic first-pass news sentiment for the decision layer.

The feed already exposes exchange catalog names and headlines.  This module
does not call an LLM; it provides a cheap, auditable classifier that can bias a
signal score and veto long/short entries only when a clearly adverse, highly
relevant announcement is fresh.
"""
from __future__ import annotations

import math
import re
import time

from feed.news import BROAD_WORDS, COIN_ALIASES


NEGATIVE = {
    "hack": -0.9, "exploit": -0.9, "breach": -0.85, "stolen": -0.8,
    "delist": -0.9, "delisting": -0.9, "suspend": -0.8, "halt": -0.75,
    "outage": -0.7, "maintenance": -0.2, "ban": -0.8, "lawsuit": -0.6,
    "fraud": -0.85, "bankrupt": -0.95, "liquidation": -0.7, "crash": -0.7,
    "下架": -0.9, "暂停": -0.75, "维护": -0.2, "攻击": -0.9,
    "被盗": -0.9, "漏洞": -0.75, "破产": -0.95, "诉讼": -0.6,
    "处罚": -0.65, "暴跌": -0.7, "清算": -0.65, "风险警告": -0.8,
}
POSITIVE = {
    "listing": 0.65, "listed": 0.6, "launch": 0.45, "partnership": 0.4,
    "approval": 0.55, "approved": 0.55, "buyback": 0.5, "integrate": 0.4,
    "support": 0.35, "adoption": 0.5, "record high": 0.45,
    "上线": 0.65, "新增": 0.5, "合作": 0.4, "批准": 0.55,
    "回购": 0.5, "支持": 0.35, "新高": 0.45, "接入": 0.4,
}


def _catalog_score(cat: str) -> float:
    text = (cat or "").lower()
    if any(word in text for word in ("delist", "remove", "下架", "移除")):
        return -0.85
    if any(word in text for word in ("new-list", "listing", "上线", "new crypto")):
        return 0.6
    if any(word in text for word in ("maintenance", "system", "维护", "系统")):
        return 0.0
    if any(word in text for word in ("campaign", "competition", "活动", "比赛")):
        return 0.0
    return 0.0


def _text_score(text: str) -> tuple[float, list[str]]:
    low = (text or "").lower()
    total = 0.0
    hits: list[str] = []
    for word, weight in {**NEGATIVE, **POSITIVE}.items():
        if word.isascii():
            matched = re.search(rf"\b{re.escape(word)}\b", low)
        else:
            matched = word in low
        if matched:
            total += weight
            hits.append(word)
    return max(-1.0, min(1.0, total)), hits


def score_item(item: dict) -> dict:
    text = f"{item.get('title', '')} {item.get('desc', '')}"
    text_score, hits = _text_score(text)
    catalog = _catalog_score(str(item.get("cat") or ""))
    score = max(-1.0, min(1.0, catalog + text_score * 0.7))
    return {
        "score": round(score, 4),
        "catalogScore": catalog,
        "hits": hits,
        "title": item.get("title") or "",
        "source": item.get("source") or "",
        "ts": int(item.get("ts") or 0),
    }


def _relevant(item: dict, symbol: str) -> bool:
    base = re.sub(r"usdt$", "", symbol.lower())
    aliases = COIN_ALIASES.get(base, [base])
    text = f"{item.get('title', '')} {item.get('desc', '')}".lower()
    if any(alias in text for alias in aliases):
        return True
    return int(item.get("rel") or 0) > 0 or any(word in text for word in BROAD_WORDS)


def aggregate_news_sentiment(items: list[dict], symbol: str,
                             now_ms: int | None = None,
                             half_life_hours: float = 24.0) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    weighted = 0.0
    weight_sum = 0.0
    details = []
    for item in items or []:
        if not _relevant(item, symbol):
            continue
        scored = score_item(item)
        age_hours = max(0.0, (now_ms - scored["ts"]) / 3_600_000) if scored["ts"] else 0.0
        decay = math.exp(-math.log(2) * age_hours / max(half_life_hours, 0.1))
        relevance = max(1.0, float(item.get("rel") or 0) / 100.0)
        weight = decay * relevance
        weighted += scored["score"] * weight
        weight_sum += weight
        details.append({**scored, "weight": round(weight, 4)})
    score = weighted / weight_sum if weight_sum > 0 else 0.0
    confidence = min(1.0, weight_sum / 3.0)
    return {
        "score": round(score, 4),
        "confidence": round(confidence, 4),
        "count": len(details),
        "details": sorted(details, key=lambda item: -item["weight"])[:8],
    }
