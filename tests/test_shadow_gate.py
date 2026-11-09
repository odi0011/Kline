"""影子对照的结算口径：防前视、同根双触、跳空、超时、成本。"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "shadow_gate", ROOT / "scripts" / "shadow_gate.py")
SG = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SG)

KW = dict(sl_pct=0.004, rr=3.0, hold_bars=60, fee_bps=4.0,
          slip_bps=1.0, notional=1000.0)


def _bars(*specs):
    """(open_time, open, high, low, close)"""
    out = list(specs)
    return out, [b[0] for b in out]


def _sig(direction="bullish", ts=1000):
    return {"ts": ts, "direction": direction}


def test_entry_uses_first_bar_after_signal_no_lookahead():
    # ts=1000 之前有两根，入场必须取 t=2000 的 open=100
    bars, times = _bars(
        (0, 90.0, 90.5, 89.5, 90.0),
        (1000, 95.0, 95.5, 94.5, 95.0),
        (2000, 100.0, 100.1, 99.9, 100.0),
        (3000, 100.0, 100.1, 99.9, 100.0),
    )
    r = SG.settle(_sig(), bars, times, **KW)
    assert r is not None
    assert abs(r["entry"] - 100.01) < 1e-6, r   # 100 × (1 + 1bp)


def test_long_target_and_stop():
    bars, times = _bars((2000, 100.0, 100.1, 99.9, 100.0),
                        (3000, 100.0, 102.0, 99.9, 101.5))
    r = SG.settle(_sig(), bars, times, **KW)
    assert r["outcome"] == "target", r
    assert r["pnl"] > 0

    bars, times = _bars((2000, 100.0, 100.1, 99.9, 100.0),
                        (3000, 100.0, 100.1, 99.0, 99.5))
    r = SG.settle(_sig(), bars, times, **KW)
    assert r["outcome"] == "stop" and r["pnl"] < 0, r


def test_same_bar_both_touched_conservatively_counts_as_stop():
    bars, times = _bars((2000, 100.0, 100.1, 99.9, 100.0),
                        (3000, 100.0, 102.0, 99.0, 100.0))
    r = SG.settle(_sig(), bars, times, **KW)
    assert r["outcome"] == "stop", r


def test_gap_through_stop_fills_at_open():
    bars, times = _bars((2000, 100.0, 100.1, 99.9, 100.0),
                        (3000, 99.0, 99.2, 98.8, 99.0))
    r = SG.settle(_sig(), bars, times, **KW)
    assert r["outcome"] == "gap_stop" and r["exit_price"] < 99.1, r


def test_timeout_closes_at_last_close():
    bars, times = _bars((2000, 100.0, 100.1, 99.9, 100.0),
                        (3000, 100.0, 100.1, 99.9, 100.05),
                        (4000, 100.05, 100.2, 99.95, 100.1))
    r = SG.settle(_sig(), bars, times, hold_bars=3, **{k: v for k, v in KW.items()
                                                        if k != "hold_bars"})
    assert r["outcome"] == "timeout" and r["hold_bars"] == 3, r


def test_costs_are_deducted():
    # 入场即离场、价格不动时，ret 应约等于 -双边成本
    bars, times = _bars((2000, 100.0, 100.05, 99.95, 100.0))
    r = SG.settle(_sig(), bars, times, hold_bars=1,
                  **{k: v for k, v in KW.items() if k != "hold_bars"})
    assert r["outcome"] == "timeout"
    assert r["ret_pct"] < 0, r
    assert abs(r["ret_pct"] + 0.1) < 0.03, r     # 2×(4+1)bps = 0.1%


def test_short_direction_is_mirrored():
    bars, times = _bars((2000, 100.0, 100.1, 99.9, 100.0),
                        (3000, 100.0, 100.1, 98.0, 98.5))
    r = SG.settle(_sig("bearish"), bars, times, **KW)
    assert r["outcome"] == "target" and r["pnl"] > 0, r


def test_no_future_bars_returns_none():
    bars, times = _bars((0, 100.0, 100.1, 99.9, 100.0))
    assert SG.settle(_sig(), bars, times, **KW) is None


def test_group_stats_uses_notional_as_equity_base():
    rows = [{"pnl": 10.0, "ret_pct": 1.0}, {"pnl": -20.0, "ret_pct": -2.0}]
    g = SG.group_stats(rows, base=1000.0)
    assert g["n"] == 2 and g["wins"] == 1 and g["losses"] == 1
    assert g["winrate"] == 50.0
    # 1000 -> 1010 -> 990：回撤相对峰值 1010，是 20/1010
    assert g["maxDDPct"] == 1.9802, g


def test_reason_bucket():
    assert SG.reason_bucket("影线噪声 0.55 ≥ 0.45") == "高影线噪声"
    assert SG.reason_bucket("形态 hammer 在两段回放里均为负") == "形态黑名单"
