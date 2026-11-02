"""K线回补：币安被地区限制（451/403）时必须自动回退欧易。"""
import unittest
import urllib.error
from unittest import mock

from feed import backfill
from core.candle import Candle


class BackfillFallbackTest(unittest.TestCase):
    def _http_error(self, code):
        return urllib.error.HTTPError("https://fapi.binance.com", code, "blocked", {}, None)

    def test_falls_back_to_okx_on_451(self):
        okx_bars = [Candle(1_700_000_000_000, 1, 2, 0.5, 1.5, volume=10,
                           interval="3m", closed=True)]
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=self._http_error(451)), \
             mock.patch.object(backfill, "_fetch_okx", return_value=okx_bars) as okx:
            out = backfill.fetch_klines("btcusdt", "3m", 100)
        self.assertEqual(len(out), 1)
        self.assertTrue(okx.called, "451 时应调用欧易回退")

    def test_falls_back_on_403(self):
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=self._http_error(403)), \
             mock.patch.object(backfill, "_fetch_okx", return_value=[]) as okx:
            backfill.fetch_klines("btcusdt", "3m", 100)
        self.assertTrue(okx.called)

    def test_other_http_errors_are_not_masked(self):
        """500 之类不该被悄悄换成别家数据，应该抛出去让人看见。"""
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=self._http_error(500)), \
             mock.patch.object(backfill, "_fetch_okx", return_value=[]) as okx:
            with self.assertRaises(urllib.error.HTTPError):
                backfill.fetch_klines("btcusdt", "3m", 100)
        self.assertFalse(okx.called)

    def test_okx_candles_are_ascending_and_closed(self):
        payload = {
            "code": "0",
            "data": [
                ["1793602800000", "100", "110", "90", "105", "5", "0", "0", "0"],
                ["1793602620000", "99", "101", "98", "100", "6", "0", "0", "0"],
            ],
        }
        fake = mock.MagicMock()
        fake.read.return_value = __import__("json").dumps(payload).encode()
        fake.__enter__.return_value = fake
        opener = mock.MagicMock()
        opener.open.return_value = fake
        with mock.patch("urllib.request.build_opener", return_value=opener):
            bars = backfill._fetch_okx("btcusdt", "3m", 10, None, 5)
        self.assertEqual(len(bars), 2)
        self.assertLess(bars[0].open_time, bars[1].open_time, "必须按时间正序")
        self.assertTrue(all(b.closed for b in bars))
        self.assertEqual(bars[1].close, 105)


if __name__ == "__main__":
    unittest.main()
