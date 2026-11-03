"""quant.py / decision.risk 状态持久化与 JSON 原子写回归测试。

覆盖审计 P0/P1：
  · 风控状态 snapshot/restore：跨重启后暂停/冷却/日亏限额判断与原引擎一致
  · 决策 trace / gate 落库（含 stages/status 完整内容）
  · 启动恢复：trace/gate 顺序、latest_traces、风控与漂移状态生效
  · 风控/漂移状态节流落库 + 成交/漂移事件强制落库
  · 本地 JSON 原子写：无 .tmp 残留、失败返回 False 且不抛
"""
from __future__ import annotations

import copy
import json
import os

import quant as q
from decision.config import DecisionConfig
from decision.drift import ADWIN
from decision.risk import RiskEngine, RiskInput, TradeRecord
from quant import QuantEngine

NOW = 1_760_000_000_000   # 固定毫秒时间戳（UTC 2025-10-09）


def _risk_input(account="a", **patch):
    values = dict(
        account=account, symbol="ethusdt", side="BUY", entry=100.0, sl=99.0,
        equity=10_000.0, available=5_000.0, now_ms=NOW, step_size=0.001,
        positions=[], data_ok=True, signal_score=70.0,
    )
    values.update(patch)
    return RiskInput(**values)


def _engine(tmp_path, **kwargs):
    return QuantEngine({"list": []}, str(tmp_path / "q.json"), **kwargs)


# ---------- 1. risk snapshot / restore ----------

def test_risk_snapshot_restore_roundtrip():
    """两账户+（日亏限额）状态快照必须可 json 序列化，恢复后各风控判断一致。"""
    cfg = DecisionConfig(consecutive_loss_limit=2)
    engine = RiskEngine(cfg)
    # 账户 pause：连续亏损触发熔断
    engine.update_equity("pause", 10_000, NOW)
    engine.record_trade("pause", TradeRecord(NOW - 3_000, -30.0, "btcusdt", "BUY"))
    engine.record_trade("pause", TradeRecord(NOW - 2_000, -25.0, "ethusdt", "SELL"))
    # 账户 cooldown：单笔小亏 + 自定义连亏/最近离场时间，触发同币种冷却
    engine.update_equity("cooldown", 20_000, NOW)
    engine.record_order_attempt("cooldown", NOW - 1_000)
    engine.record_order_attempt("cooldown", NOW - 1_500)
    cooldown_state = engine._acc("cooldown")
    cooldown_state.symbol_loss_streak["btcusdt"] = 3
    cooldown_state.last_exit_ms["btcusdt"] = NOW - 60_000
    # 账户 daily：当日 -4% 触发日亏限额（连亏 1 < limit 2，不会先被暂停拦下）
    engine.update_equity("daily", 10_000, NOW)
    engine.record_trade("daily", TradeRecord(NOW - 500, -400.0, "solusdt", "BUY"))

    snapshot = engine.snapshot()
    dumped = json.dumps(snapshot)                      # 必须可序列化
    assert json.loads(dumped) == snapshot              # 原生类型往返无损
    assert snapshot["accounts"]["cooldown"]["symbolLossStreak"]["btcusdt"] == 3
    assert snapshot["accounts"]["cooldown"]["lastExitMs"]["btcusdt"] == NOW - 60_000
    assert len(snapshot["accounts"]["cooldown"]["orderTimes"]) == 2
    assert snapshot["accounts"]["pause"]["consecutiveLosses"] == 2
    assert snapshot["accounts"]["daily"]["recentTrades"][-1]["reason"] == ""

    restored = RiskEngine(cfg)
    assert restored.restore(snapshot) == 3

    cases = {
        "pause": (10_000.0, "ethusdt", "loss_streak_pause"),
        "cooldown": (20_000.0, "btcusdt", "cooldown"),
        "daily": (10_000.0, "ethusdt", "daily_loss_limit"),
    }
    for account, (equity, symbol, expected) in cases.items():
        original = engine.evaluate(_risk_input(account, equity=equity, symbol=symbol))
        recovered = restored.evaluate(_risk_input(account, equity=equity, symbol=symbol))
        assert (original.allowed, original.code) == (recovered.allowed, recovered.code), account
        assert recovered.code == expected, (account, recovered.code, recovered.reason)


def test_risk_restore_tolerates_bad_data():
    """data / accounts 结构不对返回 0；坏账户跳过；坏字段安全转换；全都不抛。"""
    engine = RiskEngine(DecisionConfig())
    assert engine.restore(None) == 0
    assert engine.restore({}) == 0
    assert engine.restore([]) == 0
    assert engine.restore({"accounts": []}) == 0
    assert engine.restore({"accounts": "bad"}) == 0
    assert engine.restore({"accounts": {"x": "bad", "y": None, "z": [1]}}) == 0
    # 坏字段类型：账户可恢复（类型安全转换），绝不抛异常
    assert engine.restore({"accounts": {"a": {
        "day": 3,
        "dayStartEquity": "not-a-number",
        "realizedDailyPnl": None,
        "consecutiveLosses": None,
        "pauseUntilMs": "oops",
        "symbolLossStreak": "bad",
        "lastExitMs": [1],
        "recentTrades": "bad",
        "orderTimes": "bad",
    }}}) == 1
    assert engine._acc("a").day == "3"
    assert engine._acc("a").day_start_equity == 0.0
    assert engine._acc("a").pause_until_ms == 0
    # 再次 restore 空 dict 不应清掉已恢复的账户
    assert engine.restore({}) == 0
    assert "a" in engine._state


def test_risk_restore_truncates_deques_and_keeps_other_state():
    engine = RiskEngine(DecisionConfig(consecutive_loss_limit=2))
    engine.update_equity("keep", 5_000, NOW)
    engine.record_trade("keep", TradeRecord(NOW - 1_000, -10.0, "btcusdt", "BUY"))

    trades = [
        {"ts": NOW - i, "pnl": -1.0, "symbol": "btcusdt", "side": "BUY",
         "reason": "x", "pattern": "hammer", "isControl": i % 2 == 0}
        for i in range(150)
    ]
    order_times = list(range(300))
    payload = {"accounts": {"big": {"recentTrades": trades, "orderTimes": order_times}}}
    assert engine.restore(payload) == 1

    big = engine._acc("big")
    assert len(big.recent_trades) == 100              # deque maxlen 截断
    assert big.recent_trades[-1].ts == NOW - 149      # 保留最近 100 条
    assert big.recent_trades[0].is_control is True
    assert len(big.order_times) == 200
    assert big.order_times[-1] == 299
    # 不清空原有账户状态
    assert engine._state["keep"].realized_daily_pnl == -10.0


# ---------- 2. trace / gate 落库 ----------

def test_trace_and_gate_are_persisted(tmp_path):
    engine = _engine(tmp_path)
    saved = []

    def fake_storage(payload, kind):
        saved.append((kind, copy.deepcopy(payload)))

    engine.trace_storage_fn = fake_storage
    hit = {"symbol": "btcusdt", "pattern": "hammer", "name_zh": "锤子线",
           "direction": "bullish"}
    trace = engine._trace_start(hit, 100.0)
    engine._trace_stage(trace, "intake", "信号接收", "done", "收到锤子线信号")
    engine._finish_trace(trace, "blocked", "评分未通过", "共鸣分不足",
                         score={"score": 42.0, "allowed": False})

    assert [kind for kind, _ in saved] == ["trace"]
    kind, payload = saved[0]
    assert kind == "trace"
    assert payload["status"] == "blocked"
    assert payload["statusLabel"] == "评分未通过"
    assert payload["stages"][0]["id"] == "intake"
    assert payload["score"] == {"score": 42.0, "allowed": False}
    assert payload["symbol"] == "btcusdt"
    assert engine.decision_traces[-1] is trace
    assert engine.latest_traces["btcusdt"] is trace

    gate_entry = {"ts": NOW, "symbol": "btcusdt", "direction": "bullish",
                  "reason": "信号评分拦截"}
    engine._append_gate(gate_entry)
    assert [kind for kind, _ in saved] == ["trace", "gate"]
    assert saved[1][1] == gate_entry
    assert engine.gate_log[-1] is gate_entry


def test_persistence_errors_are_swallowed(tmp_path):
    """落库异常不能中断决策：trace/gate 仍进内存，save 不抛。"""
    engine = _engine(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("storage unavailable")

    engine.trace_storage_fn = boom
    engine.state_storage_fn = boom
    trace = engine._trace_start({"symbol": "btcusdt"}, 1.0)
    engine._finish_trace(trace, "blocked", "测试", "存储失败")
    engine._append_gate({"ts": 1, "reason": "存储失败"})
    assert engine.decision_traces[-1] is trace
    assert engine.gate_log[-1]["reason"] == "存储失败"
    engine.save_persistent_state(force=True)          # 不抛

    engine._finish_trace(None, "blocked", "空", "trace 为 None 直接返回")
    assert len(engine.decision_traces) == 1


# ---------- 3. 启动恢复 ----------

def test_restore_persistent_state_on_startup(tmp_path):
    cfg = DecisionConfig(consecutive_loss_limit=2)
    source = RiskEngine(cfg)
    source.update_equity("acct", 10_000, NOW)
    source.record_trade("acct", TradeRecord(NOW - 1_000, -30.0, "btcusdt", "BUY"))
    source.record_trade("acct", TradeRecord(NOW - 500, -20.0, "ethusdt", "BUY"))
    risk_data = source.snapshot()

    old_trace = {"symbol": "ethusdt", "status": "blocked", "ts": 100, "stages": []}
    new_trace = {"symbol": "btcusdt", "status": "opened", "ts": 200,
                 "stages": [{"id": "risk", "status": "done"}]}
    old_gate = {"ts": 10, "reason": "旧拦截"}
    new_gate = {"ts": 20, "reason": "新拦截"}
    drift = {"cautionUntil": NOW + 7_200_000, "shadowUntil": NOW + 3_600_000}

    engine = QuantEngine({"list": []}, str(tmp_path / "q.json"), decision_config=cfg)
    read_calls = []

    def fake_trace_read(limit, kind):
        read_calls.append((limit, kind))
        return [new_trace, old_trace] if kind == "trace" else [new_gate, old_gate]

    engine.trace_read_fn = fake_trace_read
    engine.state_read_fn = lambda name: risk_data if name == "risk" else drift

    result = engine.restore_persistent_state()
    assert result == {"traces": 2, "gates": 2, "risk": True, "drift": True}
    assert read_calls == [(24, "trace"), (q.GATE_MAX_LOG, "gate")]
    # newest-first → reversed 后按时间顺序回填：旧 → 新
    assert [trace["ts"] for trace in engine.decision_traces] == [100, 200]
    assert [gate["reason"] for gate in engine.gate_log] == ["旧拦截", "新拦截"]
    assert engine.latest_traces["ethusdt"]["status"] == "blocked"
    assert engine.latest_traces["btcusdt"]["status"] == "opened"
    assert engine._drift_caution_until == NOW + 7_200_000
    assert engine._drift_shadow_until == NOW + 3_600_000
    # 恢复的风控状态必须真正生效：连亏熔断 → 拒绝开仓
    denied = engine.risk.evaluate(_risk_input("acct", equity=10_000))
    assert not denied.allowed and denied.code == "loss_streak_pause"


def test_restore_persistent_state_survives_storage_errors(tmp_path):
    engine = _engine(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("io error")

    engine.trace_read_fn = boom
    engine.state_read_fn = boom
    assert engine.restore_persistent_state() == {
        "traces": 0, "gates": 0, "risk": False, "drift": False}
    assert list(engine.decision_traces) == []
    assert list(engine.gate_log) == []


# ---------- 4. save_persistent_state 节流 / 强制 ----------

def test_save_persistent_state_throttles_and_forces(tmp_path):
    engine = _engine(tmp_path)
    calls = []

    def fake_state_storage(name, data):
        calls.append((name, copy.deepcopy(data)))

    engine.state_storage_fn = fake_state_storage
    engine._drift_caution_until = NOW + 1_000
    engine._drift_shadow_until = NOW + 2_000

    engine.save_persistent_state()          # 首次：距 0 >= 60s → 写
    engine.save_persistent_state()          # 60s 内非 force：跳过
    assert [name for name, _ in calls] == ["risk", "drift"]
    assert calls[0][1] == {"accounts": {}}
    assert calls[1][1] == {"cautionUntil": NOW + 1_000, "shadowUntil": NOW + 2_000}
    assert isinstance(calls[1][1]["cautionUntil"], int)

    engine.save_persistent_state(force=True)
    assert [name for name, _ in calls] == ["risk", "drift", "risk", "drift"]

    # 未注入 storage 时静默返回
    engine.state_storage_fn = None
    engine.save_persistent_state(force=True)
    assert len(calls) == 4


def test_manage_saves_at_end_and_trade_forces_save(tmp_path):
    engine = _engine(tmp_path)
    calls = []
    engine.state_storage_fn = lambda name, data: calls.append(name)

    engine.manage()
    assert calls == ["risk", "drift"]       # manage 末尾节流落库
    engine.manage()                         # 60s 内不再写
    assert calls == ["risk", "drift"]

    trade = {"ts": NOW, "account": "acct", "symbol": "btcusdt", "side": "BUY",
             "qty": 0.1, "entry": 100.0, "exit": 101.0, "pnl": 0.1,
             "reason": "止盈"}
    engine._record_trade(trade)             # 成交：强制落库
    assert calls[-2:] == ["risk", "drift"]
    assert (tmp_path / "quant-trades.json").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_drift_change_forces_state_save(tmp_path):
    engine = _engine(tmp_path)
    saves = []
    engine.state_storage_fn = lambda name, data: saves.append(
        (name, copy.deepcopy(data)))

    engine._handle_drift("acct", "winrate", ADWIN(engine.decision_cfg.drift_delta))

    assert [name for name, _ in saves] == ["risk", "drift"]
    assert engine._drift_caution_until > 0
    assert saves[1][1]["cautionUntil"] == engine._drift_caution_until
    assert saves[1][1]["shadowUntil"] == 0


# ---------- 5. 本地 JSON 原子写 ----------

def test_atomic_write_json_success_and_failure(tmp_path, monkeypatch):
    target = tmp_path / "data.json"
    assert q._atomic_write_json(str(target), {"a": 1, "中文": "值"}) is True
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "中文": "值"}
    assert not list(tmp_path.glob("*.tmp"))

    # 目录不存在 → 返回 False，不抛
    assert q._atomic_write_json(str(tmp_path / "missing" / "x.json"), {"a": 1}) is False

    # os.replace 失败 → 返回 False，旧文件保持完整，无 .tmp 残留
    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    assert q._atomic_write_json(str(target), {"a": 2}) is False
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "中文": "值"}
    assert not list(tmp_path.glob("*.tmp"))


def test_save_positions_manual_unsupported_use_atomic_write(tmp_path):
    engine = _engine(tmp_path)
    engine._st("A")["positions"]["btcusdt"] = {
        "symbol": "btcusdt", "side": "BUY", "qty": 0.5, "entry": 100.0,
    }
    engine._save_positions()
    data = json.loads((tmp_path / "quant-positions.json").read_text(encoding="utf-8"))
    assert data["A"]["btcusdt"]["qty"] == 0.5
    assert not list(tmp_path.glob("*.tmp"))

    engine._manual.add(("A", "btcusdt"))
    engine._save_manual()
    assert json.loads((tmp_path / "quant-manual.json").read_text(
        encoding="utf-8")) == ["A|btcusdt"]

    engine._unsupported.add("xusdt")
    engine._save_unsupported()
    assert json.loads((tmp_path / "quant-unsupported.json").read_text(
        encoding="utf-8")) == ["xusdt"]
    assert not list(tmp_path.glob("*.tmp"))
