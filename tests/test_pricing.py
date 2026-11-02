"""DeepSeek 计价：峰谷时段 + 三档 token 分别计价。

价目来源 api-docs.deepseek.com/quick_start/pricing（2026-11 核对）。
这里把官方数字固化成断言，改价时测试会失败提醒。
"""
import unittest
from datetime import datetime, timezone

from ai import pricing

# 官方价（美元 / 百万 token）：(谷时, 峰时)
FLASH = {"cache_hit": (0.003, 0.006), "cache_miss": (0.15, 0.30), "output": (0.60, 1.20)}


def utc(y, m, d, h, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=timezone.utc)


class PeakWindowTest(unittest.TestCase):
    def test_price_table_matches_official(self):
        self.assertEqual(pricing.PRICES["deepseek-flash"], FLASH)

    def test_peak_hours(self):
        # 2026-11-02 是周一
        self.assertTrue(pricing.is_peak(utc(2026, 11, 2, 1)))
        self.assertTrue(pricing.is_peak(utc(2026, 11, 2, 3, 59)))
        self.assertTrue(pricing.is_peak(utc(2026, 11, 2, 6)))
        self.assertTrue(pricing.is_peak(utc(2026, 11, 2, 9, 59)))

    def test_off_peak_hours(self):
        self.assertFalse(pricing.is_peak(utc(2026, 11, 2, 0, 59)))
        self.assertFalse(pricing.is_peak(utc(2026, 11, 2, 4)))
        self.assertFalse(pricing.is_peak(utc(2026, 11, 2, 5, 59)))
        self.assertFalse(pricing.is_peak(utc(2026, 11, 2, 10)))
        self.assertFalse(pricing.is_peak(utc(2026, 11, 2, 23)))

    def test_weekend_is_always_off_peak(self):
        # 2026-11-07 周六、11-08 周日
        for day in (7, 8):
            for hour in (2, 7):
                self.assertFalse(pricing.is_peak(utc(2026, 11, day, hour)),
                                 f"{day}日 {hour}点 应为谷时")

    def test_china_holiday_is_off_peak(self):
        # 2026-10-01 是周四，但法定假日全天谷时
        self.assertFalse(pricing.is_peak(utc(2026, 10, 1, 2)))
        self.assertFalse(pricing.is_peak(utc(2026, 10, 1, 7)))


class CostTest(unittest.TestCase):
    USAGE = {"prompt_tokens": 1240, "completion_tokens": 600,
             "prompt_cache_hit_tokens": 1024}

    def test_peak_cost_matches_hand_calculation(self):
        c = pricing.compute_cost("deepseek-flash", self.USAGE, utc(2026, 11, 2, 2))
        # 1024*0.006/1e6 + 216*0.30/1e6 + 600*1.20/1e6
        expected = 1024 * 0.006 / 1e6 + 216 * 0.30 / 1e6 + 600 * 1.20 / 1e6
        # 金额按 8 位小数取整，比较也用同一精度
        self.assertAlmostEqual(c["usd"], round(expected, 8), places=8)

    def test_off_peak_is_exactly_half(self):
        peak = pricing.compute_cost("deepseek-flash", self.USAGE, utc(2026, 11, 2, 2))
        off = pricing.compute_cost("deepseek-flash", self.USAGE, utc(2026, 11, 2, 5))
        self.assertAlmostEqual(off["usd"], peak["usd"] / 2, places=10)

    def test_cache_hit_and_miss_are_priced_separately(self):
        """缓存命中比未命中便宜得多，混在一起算会严重高估。"""
        usage = {"prompt_tokens": 1000, "completion_tokens": 0,
                 "prompt_cache_hit_tokens": 1000}
        all_hit = pricing.compute_cost("deepseek-flash", usage, utc(2026, 11, 2, 2))
        usage_miss = {"prompt_tokens": 1000, "completion_tokens": 0,
                      "prompt_cache_hit_tokens": 0}
        all_miss = pricing.compute_cost("deepseek-flash", usage_miss, utc(2026, 11, 2, 2))
        self.assertAlmostEqual(all_miss["usd"] / all_hit["usd"], 50.0, places=6)

    def test_missing_hit_field_treated_as_all_miss(self):
        """服务商没回报命中数时按全部未命中算——宁可算贵，不能少算。"""
        c = pricing.compute_cost("deepseek-flash",
                                 {"prompt_tokens": 1000, "completion_tokens": 0},
                                 utc(2026, 11, 2, 2))
        self.assertAlmostEqual(c["usd"], 1000 * 0.30 / 1e6, places=10)

    def test_hit_never_exceeds_prompt(self):
        c = pricing.compute_cost("deepseek-flash",
                                 {"prompt_tokens": 100, "completion_tokens": 0,
                                  "prompt_cache_hit_tokens": 999},
                                 utc(2026, 11, 2, 2))
        self.assertEqual(c["parts"]["cacheMissTokens"], 0)
        self.assertEqual(c["parts"]["cacheHitTokens"], 100)

    def test_unknown_model_reports_unknown(self):
        c = pricing.compute_cost("some-other-model", self.USAGE, utc(2026, 11, 2, 2))
        self.assertFalse(c["known"])
        self.assertIsNone(c["usd"])

    def test_model_alias_with_date_suffix(self):
        self.assertTrue(pricing.compute_cost("deepseek-flash-1017", self.USAGE, utc(2026, 11, 2, 2))["known"])
        self.assertTrue(pricing.compute_cost("deepseek-chat", self.USAGE, utc(2026, 11, 2, 2))["known"])


class DescribeTest(unittest.TestCase):
    def test_note_spells_out_the_formula(self):
        d = pricing.describe("deepseek-flash",
                             {"prompt_tokens": 1240, "completion_tokens": 600,
                              "prompt_cache_hit_tokens": 1024},
                             fx=7.2, moment=utc(2026, 11, 2, 2))
        note = d["note"]
        self.assertIn("峰时", note)
        self.assertIn("1,024 tok", note)
        self.assertIn("216 tok", note)
        self.assertIn("600 tok", note)
        self.assertIn("¥", note)

    def test_currency_conversion(self):
        d = pricing.describe("deepseek-flash",
                             {"prompt_tokens": 1_000_000, "completion_tokens": 0,
                              "prompt_cache_hit_tokens": 0},
                             fx=7.2, moment=utc(2026, 11, 2, 2))
        self.assertAlmostEqual(d["costUsd"], 0.30, places=6)
        self.assertAlmostEqual(d["cost"], 0.30 * 7.2, places=5)

    def test_unknown_model_has_no_cost(self):
        d = pricing.describe("nope", {"prompt_tokens": 10}, moment=utc(2026, 11, 2, 2))
        self.assertIsNone(d["cost"])
        self.assertIn("不在价目表", d["note"])


if __name__ == "__main__":
    unittest.main()
