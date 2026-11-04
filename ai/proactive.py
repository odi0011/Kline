"""主动说话的闸门：什么时候该由系统自己开口，什么时候闭嘴。

为什么需要这个模块：
  之前的"主动汇报"只有一条路径——定时任务跑完，把结果整段推给用户。
  于是出现两个问题：
    1. 只有定时，没有事件。形态命中、仓位开平、数据异常这些**真正
       值得立刻说**的事，反而要等下一轮定时才可能被提及。
    2. 说什么由任务的固定 prompt 决定，所以每轮都是同一个版式
       （"【BTC 盯 76000】没跌破…不打扰"）。说人话的余地被模板占满了。

这里把"要不要开口"和"开口说什么"分开：
  · 本模块只管**要不要开口**（概率、冷却、预算、去重、安静时段）
  · 说什么完全交给模型，prompt 里只给事实和语气要求

判定顺序（先便宜后贵，全部同步、无 IO）：
  1. 去重：同一类事在窗口内说过就不再提
  2. 冷却：这一类事件刚说过，等冷却期
  3. 预算：每小时最多开口 N 次（防止行情剧烈时刷屏）
  4. 概率：按优先级给不同的开口概率——低优先级事件绝大多数时候沉默
  5. 深夜更克制：本地 0-7 点把概率再乘一个系数（不是硬性静音，
     真出了大事仍然要说）
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

# 每类事件的默认策略：冷却时间（秒）、基础开口概率、去重窗口（秒）
# 概率不是"重要性"，而是"值不值得为它打断用户"——数据异常几乎每次都要说，
# 普通信号大部分时候不必说。
DEFAULT_POLICY = {
    # 真正紧急：仓位裸奔、平仓失败、连续亏损熔断。
    # urgent=True → 深夜也不降概率。凌晨三点仓位裸奔，必须叫醒。
    "risk": {"cooldown": 180, "prob": 1.0, "dedupe": 300, "urgent": True},
    # 开仓/平仓：用户会想知道，但不必每次都打断
    "position": {"cooldown": 240, "prob": 0.85, "dedupe": 120},
    # 形态命中：频繁，只有强信号才值得说
    "pattern": {"cooldown": 900, "prob": 0.28, "dedupe": 3600},
    # 数据异常
    "error": {"cooldown": 600, "prob": 0.9, "dedupe": 1800, "urgent": True},
    # 自己去找乐子（新闻、趣事、炫耀）：低频、随机
    "chatter": {"cooldown": 3600, "prob": 0.35, "dedupe": 7200},
    # 模拟仓自检判出胜负
    "paper": {"cooldown": 1200, "prob": 0.4, "dedupe": 600},
}
# 没登记的 kind 用这套保守策略：大概率沉默，别让新事件类型一上来就刷屏
FALLBACK_POLICY = {"cooldown": 900, "prob": 0.15, "dedupe": 1800}

# 每小时最多主动开口次数。行情剧烈时事件会成串，没有这个上限就是刷屏。
MAX_PER_HOUR = 6


@dataclass
class ProactiveEvent:
    """一条"可能值得说"的系统事件。"""

    kind: str                     # risk/position/pattern/error/chatter/paper
    summary: str                  # 一句话事实，给模型看，也是去重键的一部分
    symbol: str = ""
    detail: dict = field(default_factory=dict)
    dedupe_key: str = ""          # 空则按 kind+symbol+summary 去重


class ProactiveGate:
    """决定这条事件要不要开口。纯内存状态，线程/协程内单线程使用即可。"""

    def __init__(self, policy: dict | None = None, max_per_hour: int = MAX_PER_HOUR,
                 rng: random.Random | None = None):
        self.policy = dict(DEFAULT_POLICY)
        if policy:
            for kind, cfg in policy.items():
                base = dict(self.policy.get(kind) or FALLBACK_POLICY)
                base.update(cfg or {})
                self.policy[kind] = base
        self.max_per_hour = max(0, int(max_per_hour))
        self._rng = rng or random.Random()
        self._last_by_kind: dict[str, float] = {}
        self._last_by_key: dict[str, float] = {}
        self._recent_speaks: list[float] = []
        # 给出判定理由，方便排查"为什么刚才没说"——静默最难调试
        self.last_reason = ""

    def _policy(self, kind: str) -> dict:
        """取某个 kind 的生效策略。

        注意：调用方可能整体替换 self.policy[kind]（测试或运行时调参），
        那样会丢掉 urgent 这类"安全相关"的默认标记。所以这里在返回前
        **补回**默认里的 urgent——漏写它会让紧急事件在深夜被静默。
        """
        cfg = dict(self.policy.get(kind) or FALLBACK_POLICY)
        default_urgent = bool((DEFAULT_POLICY.get(kind) or {}).get("urgent"))
        if default_urgent:
            cfg["urgent"] = True
        return cfg

    def _prune(self, now: float) -> None:
        cutoff = now - 3600
        self._recent_speaks = [t for t in self._recent_speaks if t >= cutoff]

    @staticmethod
    def _hour_local(now: float) -> int:
        return int(time.localtime(now).tm_hour)

    def should_speak(self, event: ProactiveEvent, now: float | None = None) -> bool:
        """返回 True 表示这次该开口。同时把判定理由写进 last_reason。"""
        if not isinstance(event, ProactiveEvent):
            self.last_reason = "invalid"
            return False
        now = time.time() if now is None else float(now)
        cfg = self._policy(event.kind)
        key = event.dedupe_key or f"{event.kind}:{event.symbol}:{event.summary[:60]}"

        # 1) 去重：同一件事在窗口内说过就闭嘴
        last_key = self._last_by_key.get(key)
        if last_key is not None and now - last_key < float(cfg.get("dedupe") or 0):
            self.last_reason = f"dedupe {int(now - last_key)}s"
            return False

        # 2) 同类冷却
        last_kind = self._last_by_kind.get(event.kind)
        if last_kind is not None and now - last_kind < float(cfg.get("cooldown") or 0):
            self.last_reason = f"cooldown {int(now - last_kind)}s"
            return False

        # 3) 每小时预算
        self._prune(now)
        if len(self._recent_speaks) >= self.max_per_hour:
            self.last_reason = f"budget {len(self._recent_speaks)}/{self.max_per_hour}"
            return False

        # 4) 概率。深夜（本地 0-7 点）更克制，但：
        #    · prob=1.0 的类别表示"必说"，永不打折——包括 risk（已标 urgent）
        #      和任何调用方显式设成 1.0 的场景（测试/用户要求）。
        #      第一版只判断 urgent 标记，结果测试里把 prob 设成 1.0 仍被
        #      折成 0.5，触发了"必说却不说"的诡异行为。
        prob = float(cfg.get("prob") or 0.0)
        urgent = bool(cfg.get("urgent")) or prob >= 1.0
        hour = self._hour_local(now)
        if not urgent and 0 <= hour < 7:
            prob *= 0.5
        if prob < 1.0 and self._rng.random() >= prob:
            self.last_reason = f"roll {prob:.2f}"
            return False

        self._mark_spoke(event, key, now)
        self.last_reason = "ok"
        return True

    def force(self, event: ProactiveEvent, now: float | None = None) -> bool:
        """无条件开口（只用于测试/用户显式要求），仍然记录状态。"""
        now = time.time() if now is None else float(now)
        key = event.dedupe_key or f"{event.kind}:{event.symbol}:{event.summary[:60]}"
        self._mark_spoke(event, key, now)
        self.last_reason = "forced"
        return True

    def _mark_spoke(self, event: ProactiveEvent, key: str, now: float) -> None:
        self._last_by_kind[event.kind] = now
        self._last_by_key[key] = now
        self._recent_speaks.append(now)

    def stats(self) -> dict:
        now = time.time()
        self._prune(now)
        return {
            "spokenLastHour": len(self._recent_speaks),
            "budget": self.max_per_hour,
            "lastReason": self.last_reason,
        }
