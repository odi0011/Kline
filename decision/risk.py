"""Unified pre-order risk engine.

Every new order passes the same deterministic gate before touching an exchange
API.  The engine owns account-level protection state (daily result, loss
streak, cooldowns, order rate) and calculates a risk-budgeted quantity.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import DecisionConfig
from .features import correlation, session_minute


@dataclass
class PositionRiskInput:
    symbol: str
    side: str
    qty: float
    entry: float
    sl: float | None = None

    def stop_risk(self) -> float:
        if not self.sl or self.entry <= 0 or self.qty <= 0:
            return 0.0
        return abs(self.entry - self.sl) * abs(self.qty)


@dataclass
class TradeRecord:
    ts: int
    pnl: float
    symbol: str = ""
    side: str = ""
    reason: str = ""
    pattern: str = ""
    is_control: bool = False


@dataclass
class RiskInput:
    account: str
    symbol: str
    side: str
    entry: float
    sl: float
    equity: float
    available: float
    positions: list[PositionRiskInput] = field(default_factory=list)
    returns_by_symbol: dict[str, list[float]] = field(default_factory=dict)
    step_size: float = 0.0
    now_ms: int = 0
    data_ok: bool = True
    is_demo: bool = False
    drawdown_paused: bool = False
    drawdown_pct: float = 0.0
    signal_score: float | None = None
    signal_grade: str | None = None
    confidence: float | None = None
    atr_pct: float | None = None
    liquidation_cascade: bool = False
    funding_blocked: bool = False
    macro_blackout: str = ""
    symbol_healthy: bool = True
    max_positions_override: int | None = None
    cooldown_extra_sec: int = 0


@dataclass
class RiskResult:
    allowed: bool
    code: str
    stage: str
    reason: str
    qty: float = 0.0
    notional: float = 0.0
    size_factor: float = 0.0
    kelly_pct: float = 0.0
    risk_pct: float = 0.0
    heat_pct: float = 0.0
    correlation_max: float = 0.0
    cooldown_sec: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _AccountState:
    day: str = ""
    day_start_equity: float = 0.0
    realized_daily_pnl: float = 0.0
    consecutive_losses: int = 0
    pause_until_ms: int = 0
    symbol_loss_streak: dict[str, int] = field(default_factory=dict)
    last_exit_ms: dict[str, int] = field(default_factory=dict)
    recent_trades: deque[TradeRecord] = field(default_factory=lambda: deque(maxlen=100))
    order_times: deque[int] = field(default_factory=lambda: deque(maxlen=200))


def _utc_day(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date().isoformat()


def _floor_step(value: float, step: float) -> float:
    if value <= 0:
        return 0.0
    if step <= 0:
        return value
    return math.floor(value / step + 1e-9) * step


class RiskEngine:
    def __init__(self, config: DecisionConfig | None = None):
        self.config = config or DecisionConfig()
        self._state: dict[str, _AccountState] = {}

    def _acc(self, account: str) -> _AccountState:
        return self._state.setdefault(account, _AccountState())

    def update_equity(self, account: str, equity: float, now_ms: int) -> None:
        if equity <= 0:
            return
        state = self._acc(account)
        day = _utc_day(now_ms)
        if state.day != day:
            state.day = day
            state.day_start_equity = equity
            state.realized_daily_pnl = 0.0
        elif state.day_start_equity <= 0:
            state.day_start_equity = equity

    def seed_trades(self, account: str, trades: list[TradeRecord], now_ms: int) -> None:
        state = self._acc(account)
        day = _utc_day(now_ms)
        state.day = day
        state.realized_daily_pnl = 0.0
        state.consecutive_losses = 0
        for trade in sorted(trades, key=lambda item: item.ts):
            state.recent_trades.append(trade)
            state.last_exit_ms[trade.symbol] = trade.ts
            if _utc_day(trade.ts) == day:
                state.realized_daily_pnl += trade.pnl
            if trade.pnl < 0:
                state.consecutive_losses += 1
                state.symbol_loss_streak[trade.symbol] = state.symbol_loss_streak.get(trade.symbol, 0) + 1
            elif trade.pnl > 0:
                state.consecutive_losses = 0
                state.symbol_loss_streak[trade.symbol] = 0
            if state.consecutive_losses >= self.config.consecutive_loss_limit:
                state.pause_until_ms = trade.ts + self.config.consecutive_loss_cooldown_min * 60_000

    def record_trade(self, account: str, trade: TradeRecord) -> None:
        state = self._acc(account)
        day = _utc_day(trade.ts)
        if state.day != day:
            state.day = day
            state.realized_daily_pnl = 0.0
            state.pause_until_ms = 0
        state.recent_trades.append(trade)
        state.realized_daily_pnl += trade.pnl
        state.last_exit_ms[trade.symbol] = trade.ts
        if trade.pnl < 0:
            state.consecutive_losses += 1
            state.symbol_loss_streak[trade.symbol] = state.symbol_loss_streak.get(trade.symbol, 0) + 1
        elif trade.pnl > 0:
            state.consecutive_losses = 0
            state.symbol_loss_streak[trade.symbol] = 0
        if state.consecutive_losses >= self.config.consecutive_loss_limit:
            state.pause_until_ms = trade.ts + self.config.consecutive_loss_cooldown_min * 60_000

    def record_order_attempt(self, account: str, now_ms: int) -> None:
        state = self._acc(account)
        state.order_times.append(now_ms)
        cutoff = now_ms - 60_000
        while state.order_times and state.order_times[0] < cutoff:
            state.order_times.popleft()

    def cooldown_seconds(self, account: str, symbol: str) -> int:
        state = self._acc(account)
        streak = state.symbol_loss_streak.get(symbol, 0)
        seconds = self.config.cooldown_base_sec * (2 ** min(streak, 6))
        return int(min(seconds, self.config.cooldown_max_sec))

    def _kelly_pct(self, state: _AccountState) -> tuple[float, dict]:
        trades = [t for t in state.recent_trades if not t.is_control][-30:]
        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [-t.pnl for t in trades if t.pnl < 0]
        n = len(wins) + len(losses)
        if n < self.config.kelly_min_samples or not wins or not losses:
            return self.config.margin_pct_default, {"samples": n, "mode": "default"}
        winrate = len(wins) / n
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        payoff = avg_win / avg_loss if avg_loss > 1e-12 else 0.0
        if payoff <= 0:
            return self.config.margin_pct_min, {"samples": n, "mode": "kelly", "winrate": winrate}
        kelly = winrate - (1 - winrate) / payoff
        fraction = clamp(kelly * self.config.kelly_fraction,
                         self.config.margin_pct_min, self.config.margin_pct_max)
        return fraction, {
            "samples": n,
            "mode": "kelly",
            "winrate": round(winrate, 4),
            "payoff": round(payoff, 4),
            "kelly": round(kelly, 4),
        }

    def _correlation_bucket(self, req: RiskInput) -> tuple[float, str | None]:
        new_returns = req.returns_by_symbol.get(req.symbol)
        if not new_returns:
            return 0.0, None
        best = 0.0
        match = None
        for pos in req.positions:
            if pos.symbol == req.symbol:
                continue
            other = req.returns_by_symbol.get(pos.symbol)
            if not other:
                continue
            rho = correlation(new_returns[-self.config.correlation_lookback:],
                              other[-self.config.correlation_lookback:])
            if abs(rho) > abs(best):
                best = rho
                match = pos.symbol
        return best, match

    def evaluate(self, req: RiskInput) -> RiskResult:
        now = req.now_ms
        state = self._acc(req.account)
        self.update_equity(req.account, req.equity, now)
        if not self.config.enabled or not self.config.risk_enabled:
            return RiskResult(True, "ok", "disabled", "", qty=0.0)
        if req.is_demo:
            return RiskResult(False, "demo_feed", "data", "演示行情不允许下达真实订单")
        if not req.data_ok or not req.symbol_healthy:
            return RiskResult(False, "data_unhealthy", "data", "行情数据不健康，暂停开仓")
        if req.drawdown_paused:
            return RiskResult(False, "drawdown_pause", "account",
                              f"账户回撤熔断中（{req.drawdown_pct:.2f}%）")
        if req.liquidation_cascade:
            return RiskResult(False, "liquidation_cascade", "market",
                              "清算级联进行中，暂停开仓")
        configured_event = next(
            (event for event in self.config.macro_events if event.contains(now)), None)
        if configured_event or req.macro_blackout:
            label = configured_event.label if configured_event else req.macro_blackout
            return RiskResult(False, "macro_blackout", "event",
                              f"宏观事件黑窗：{label or 'scheduled event'}")
        minute = session_minute(now)
        for window in self.config.session_blackouts:
            if window.contains(minute):
                return RiskResult(False, "session_blackout", "event",
                                  f"低流动性时段禁开仓（{window.label}）")
        if req.funding_blocked:
            return RiskResult(False, "funding_window", "cost",
                              "资金费率结算前方向不利，暂缓开仓")
        if now < state.pause_until_ms:
            left = max(1, int((state.pause_until_ms - now) / 1000))
            return RiskResult(False, "loss_streak_pause", "account",
                              f"连续亏损熔断中，还剩 {left} 秒")
        start_equity = state.day_start_equity or req.equity
        if start_equity > 0:
            daily_return = state.realized_daily_pnl / start_equity
            if daily_return <= -self.config.daily_loss_limit_pct:
                return RiskResult(False, "daily_loss_limit", "account",
                                  f"当日已实现亏损 {daily_return * 100:.2f}% 触及限额")
            if daily_return >= self.config.daily_profit_lock_pct:
                return RiskResult(False, "daily_profit_lock", "account",
                                  f"当日已实现收益 {daily_return * 100:.2f}% 已锁盈")
        if any(pos.symbol == req.symbol for pos in req.positions):
            return RiskResult(False, "duplicate_symbol", "position", "该币已有持仓")
        max_positions = req.max_positions_override or self.config.max_positions
        if len(req.positions) >= max_positions:
            return RiskResult(False, "max_positions", "position",
                              f"仓位已满 {len(req.positions)}/{max_positions}")
        same_dir = sum(1 for pos in req.positions if pos.side.upper() == req.side.upper())
        if same_dir >= self.config.max_same_direction:
            return RiskResult(False, "max_same_direction", "position",
                              f"同方向已持 {same_dir} 仓，上限 {self.config.max_same_direction}")
        last_exit = state.last_exit_ms.get(req.symbol, 0)
        cooldown = min(
            self.config.cooldown_max_sec,
            self.cooldown_seconds(req.account, req.symbol) + max(0, req.cooldown_extra_sec),
        )
        if last_exit and now - last_exit < cooldown * 1000:
            left = max(1, int((cooldown * 1000 - (now - last_exit)) / 1000))
            return RiskResult(False, "cooldown", "timing",
                              f"亏损后冷却中，还需 {left} 秒", cooldown_sec=cooldown)
        corr, corr_symbol = self._correlation_bucket(req)
        if corr_symbol and abs(corr) >= self.config.correlation_threshold:
            return RiskResult(False, "correlation_bucket", "portfolio",
                              f"与 {corr_symbol.upper()} 相关性 {corr:.2f}，视为同一敞口",
                              correlation_max=corr)
        if req.signal_score is not None and req.signal_score < self.config.minimum_score:
            return RiskResult(False, "signal_score", "signal",
                              f"信号共鸣分 {req.signal_score:.0f} 不足",
                              correlation_max=corr)
        if req.sl <= 0 or req.entry <= 0 or abs(req.entry - req.sl) <= 1e-12:
            return RiskResult(False, "invalid_stop", "sizing", "止损距离无效")
        stop_distance = abs(req.entry - req.sl)
        risk_budget = max(0.0, req.equity) * self.config.risk_per_trade_pct
        risk_qty = risk_budget / stop_distance
        kelly_pct, kelly_meta = self._kelly_pct(state)
        capital_notional = max(0.0, req.available) * kelly_pct
        if req.available < self.config.margin_min_usdt:
            return RiskResult(False, "insufficient_available", "sizing",
                              f"可用余额不足 {self.config.margin_min_usdt:.0f} USDT")
        qty = min(risk_qty, capital_notional / req.entry)
        size_factor = 1.0
        warnings = []
        if req.atr_pct is not None:
            if req.atr_pct >= self.config.extreme_vol_atr_pct:
                return RiskResult(False, "extreme_volatility", "market",
                                  f"ATR 分位 {req.atr_pct:.0f}% 过高，停止开仓")
            if req.atr_pct >= self.config.high_vol_atr_pct:
                size_factor *= self.config.high_vol_size_factor
                warnings.append(f"高波动，仓位缩至 {self.config.high_vol_size_factor:.0%}")
        current_heat = sum(p.stop_risk() for p in req.positions)
        new_risk = qty * size_factor * stop_distance
        max_heat = max(0.0, req.equity) * self.config.portfolio_heat_max_pct
        if max_heat > 0 and current_heat + new_risk > max_heat:
            remaining = max(0.0, max_heat - current_heat)
            heat_factor = remaining / new_risk if new_risk > 0 else 0.0
            if heat_factor < 0.25:
                return RiskResult(False, "portfolio_heat", "portfolio",
                                  f"组合热度将达 {(current_heat + new_risk) / req.equity * 100:.2f}%，超过上限",
                                  heat_pct=(current_heat + new_risk) / req.equity * 100,
                                  correlation_max=corr)
            size_factor *= heat_factor
            warnings.append(f"组合热度约束，仓位缩至 {heat_factor:.0%}")
        qty *= size_factor
        notional = qty * req.entry
        if self.config.max_notional_per_order and notional > self.config.max_notional_per_order:
            qty = self.config.max_notional_per_order / req.entry
            notional = qty * req.entry
            warnings.append("触发单笔名义上限")
        qty = _floor_step(qty, req.step_size)
        notional = qty * req.entry
        if qty <= 0:
            return RiskResult(False, "qty_zero", "sizing", "按步长取整后数量为 0")
        if len(state.order_times) >= self.config.max_orders_per_min:
            return RiskResult(False, "order_rate", "execution",
                              f"每分钟下单数达到上限 {self.config.max_orders_per_min}")
        heat_pct = (current_heat + qty * stop_distance) / req.equity * 100 if req.equity > 0 else 0.0
        return RiskResult(
            True,
            "ok",
            "sizing",
            "",
            qty=round(qty, 12),
            notional=round(notional, 8),
            size_factor=round(size_factor, 4),
            kelly_pct=round(kelly_pct, 6),
            risk_pct=round(qty * stop_distance / req.equity * 100, 4) if req.equity > 0 else 0.0,
            heat_pct=round(heat_pct, 4),
            correlation_max=round(corr, 4),
            cooldown_sec=cooldown,
            metrics={
                "riskBudget": round(risk_budget, 4),
                "capitalNotional": round(capital_notional, 4),
                "stopDistance": round(stop_distance, 10),
                "kelly": kelly_meta,
                "dailyReturnPct": round(
                    state.realized_daily_pnl / start_equity * 100, 4) if start_equity > 0 else 0.0,
                "consecutiveLosses": state.consecutive_losses,
            },
            warnings=warnings,
        )


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
