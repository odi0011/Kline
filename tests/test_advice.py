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

    def test_holding_reports_open_position_instead(self):
        state = {"price": 78000.0, "atr": 600.0, "atrPct": 45.0}
        holding = {"side": "BUY", "sl": 77000.0, "tp": 79500.0,
                   "slPct": 1.2, "tpPct": 2.0, "holdMin": 12.5}
        out = build_advice("btcusdt", state, evalof(), 150, holding)
        self.assertEqual(out["action"], "hold")
        self.assertEqual(out["side"], "long")
        self.assertEqual(out["sl"], 77000.0)
        self.assertEqual(out["holdMin"], 12.5)

    def test_take_profit_is_never_inside_stop(self):
        # 低波动时 ATR 被夹到下限，止盈也必须按最低盈亏比拉开
        state = {"price": 78000.0, "atr": 20.0, "atrPct": 90.0, "regime": "range"}
        out = build_advice("btcusdt", state, evalof(), 150)
        if out["action"] in ("long", "short"):
            self.assertGreaterEqual(out["rr"], 1.5)


if __name__ == "__main__":
    unittest.main()
