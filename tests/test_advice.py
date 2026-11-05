"""K 线图右上角「现在该干嘛」胶囊的建议生成。"""
import unittest

from decision.advice import build_advice


def evalof(direction="bullish", score=80.0, opposite=30.0, gap=50.0,
           grade="strong", vetoes=None):
    return {"direction": direction, "score": score, "oppositeScore": opposite,
            "gap": gap, "grade": grade, "confidence": score / 100,
            "vetoes": vetoes or []}


class AdviceTest(unittest.TestCase):
    def test_strong_signal_gives_direction_stops_and_leverage(self):
        state = {"price": 78000.0, "atr": 600.0, "atrPct": 45.0, "regime": "trend"}
        out = build_advice("btcusdt", state, evalof(), 150)
        self.assertEqual(out["action"], "long")
        self.assertEqual(out["maxLeverage"], 150)
        self.assertLess(out["sl"], state["price"])
        self.assertGreater(out["tp"], state["price"])
        self.assertGreater(out["rr"], 1.0)
        self.assertGreaterEqual(out["holdMin"], 8)
        self.assertLessEqual(out["holdMin"], 60)

    def test_bearish_mirrors_stops(self):
        state = {"price": 78000.0, "atr": 600.0, "atrPct": 45.0, "regime": "trend"}
        out = build_advice("btcusdt", state, evalof(direction="bearish"), 150)
        self.assertEqual(out["action"], "short")
        self.assertGreater(out["sl"], state["price"])
        self.assertLess(out["tp"], state["price"])

    def test_leverage_never_exceeds_exchange_cap(self):
        state = {"price": 400.0, "atr": 2.0, "atrPct": 30.0, "regime": "trend"}
        capped = build_advice("tslausdt", state, evalof(), 20)
        self.assertLessEqual(capped["leverage"], 20)
        # 同样的行情，上限更高的合约可以给出更高杠杆
        loose = build_advice("btcusdt", state, evalof(), 150)
        self.assertGreaterEqual(loose["leverage"], capped["leverage"])

    def test_wide_stop_means_lower_leverage(self):
        tight = build_advice("btcusdt", {"price": 78000.0, "atr": 300.0, "atrPct": 40.0}, evalof(), 150)
        wide = build_advice("btcusdt", {"price": 78000.0, "atr": 1500.0, "atrPct": 40.0}, evalof(), 150)
        self.assertLess(tight["slPct"], wide["slPct"])
        self.assertGreater(tight["leverage"], wide["leverage"])

    def test_weak_or_vetoed_signals_ask_to_wait(self):
        state = {"price": 78000.0, "atr": 600.0, "atrPct": 45.0, "regime": "trend"}
        weak = build_advice("btcusdt", state, evalof(score=45, gap=3, grade="none"), 150)
        self.assertEqual(weak["action"], "wait")
        vetoed = build_advice("btcusdt", state, evalof(vetoes=["形态胜率偏低"]), 150)
        self.assertEqual(vetoed["action"], "wait")
        neutral = build_advice("btcusdt", state, evalof(direction="neutral", grade="none"), 150)
        self.assertEqual(neutral["action"], "wait")

    def test_missing_data_does_not_invent_numbers(self):
        out = build_advice("btcusdt", {}, None, 150)
        self.assertEqual(out["action"], "wait")
        self.assertNotIn("sl", out)
        self.assertEqual(out["maxLeverage"], 150)

    def test_always_returns_a_direction_never_a_hold_state(self):
        """建议只回答"该做多还是做空"；持仓状态不归这里管。"""
        state = {"price": 78000.0, "atr": 600.0, "atrPct": 45.0, "regime": "trend"}
        out = build_advice("btcusdt", state, evalof(), 150)
        self.assertEqual(out["action"], "long")
        self.assertNotIn("hold", {out["action"]})
        # 反向信号同样给出明确方向
        bear = build_advice("btcusdt", state, evalof(direction="bearish"), 150)
        self.assertEqual(bear["action"], "short")

    def test_take_profit_is_never_inside_stop(self):
        # 低波动时 ATR 被夹到下限，止盈也必须按最低盈亏比拉开
        state = {"price": 78000.0, "atr": 20.0, "atrPct": 90.0, "regime": "range"}
        out = build_advice("btcusdt", state, evalof(), 150)
        if out["action"] in ("long", "short"):
            self.assertGreaterEqual(out["rr"], 1.5)


if __name__ == "__main__":
    unittest.main()


class StructureAdviceTest(unittest.TestCase):
    """建议的止损止盈必须落在图上画出来的结构线上。

    线上原话："这数据滞后性也太大了吧，这都是啥价了，上面推荐止盈止损是多少？"
    截图里 ETH 现价 2416.39、推荐止损 2425.9 —— 那正好是 ATR 下限
    0.4% 算出来的 2426.1，而图上阻力明明画在 2421.4 / 2425.4。
    """

    def _struct(self):
        return {
            "price": 2419.27, "atr": 2.125,
            "levels": [
                {"kind": "resistance", "price": 2421.37},
                {"kind": "resistance", "price": 2425.35},
                {"kind": "resistance", "price": 2428.50},
                {"kind": "support", "price": 2416.66},
                {"kind": "support", "price": 2412.55},
            ],
            "pattern": None,
        }

    def _adv(self, direction):
        ev = evalof(direction=direction, score=63.0, opposite=40.0,
                    gap=23.0, grade="medium")
        return build_advice("ethusdt", {"price": 2419.27, "atr": 2.125, "atrPct": 6.0},
                            ev, 125, self._struct())

    def test_short_stop_anchors_on_a_drawn_resistance(self):
        out = self._adv("bearish")
        self.assertEqual(out["action"], "short")
        # 止损必须比所有"太近"的线更远，并且锚在图上某条阻力上
        self.assertGreater(out["sl"], out["price"])
        # slLevel 走 _round_price 展示精度（>=1000 保留 1 位），按 0.1 容差比
        self.assertTrue(
            any(abs(out["slLevel"] - lv) < 0.1 for lv in (2421.37, 2425.35, 2428.5)),
            f"止损锚点 {out['slLevel']} 不在图上任何一条阻力上")
        self.assertEqual(out["slBasis"], "阻力位")
        self.assertLess(abs(out["sl"] - out["slLevel"]), 1.0)

    def test_long_stop_anchors_on_a_drawn_support(self):
        out = self._adv("bullish")
        self.assertEqual(out["action"], "long")
        self.assertLess(out["sl"], out["price"])
        self.assertTrue(
            any(abs(out["slLevel"] - lv) < 0.1 for lv in (2416.66, 2412.55)),
            f"止损锚点 {out['slLevel']} 不在图上任何一条支撑上")
        self.assertEqual(out["slBasis"], "支撑位")

    def test_stop_is_never_left_inside_noise(self):
        """最近的线贴着现价（0.08%）时不能把止损放在那儿——必被扫。"""
        out = self._adv("bearish")
        self.assertGreaterEqual(out["slPct"], 0.25)

    def test_take_profit_keeps_min_rr(self):
        for d in ("bearish", "bullish"):
            out = self._adv(d)
            self.assertGreaterEqual(out["rr"], 1.5, out)

    def test_falls_back_to_atr_without_structure(self):
        ev = evalof(direction="bearish", score=63.0, opposite=40.0, gap=23.0,
                    grade="medium")
        out = build_advice("ethusdt", {"price": 2419.27, "atr": 2.125},
                           ev, 125, None)
        self.assertEqual(out["action"], "short")
        self.assertEqual(out["slBasis"], "ATR")
        self.assertGreater(out["sl"], out["price"])
