"""MySQL 存储层（服务器本机 127.0.0.1 的 kline 库，专用账号仅授权该库）：
- candles 表：K线滚动保留（默认最近30分钟）
- hits 表：形态命中记录（含胜负评估），保留 7 天
- 胜率：confirmed 命中后 eval_bars 根K线收盘，按方向对比价格判定 win/loss
注意：interval 是 MySQL 保留字，列名统一用反引号。
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import pymysql


class Storage:
    def __init__(self, mysql: dict, keep_minutes: int = 30,
                 hit_days: int = 7, eval_minutes: int = 10,
                 eval_threshold: float = 0.0015,
                 stats_days: int = 7, stats_alpha: float = 0.94):
        self.keep_ms = keep_minutes * 60 * 1000
        self.hit_ms = hit_days * 86400 * 1000
        self.eval_ms = eval_minutes * 60_000    # 命中后10分钟的走势判定胜负
        self.eval_threshold = eval_threshold    # 窗口内有效波动门槛（先触及判定）
        self.stats_days = max(1, int(stats_days))
        self.stats_alpha = min(0.999, max(0.50, float(stats_alpha)))
        self._lock = threading.Lock()
        self._conn = None
        self.enabled = False
        # 空白部署时 mysql 段是空的/库不存在 → 不能让整个服务起不来。
        # 降级为"无持久化"模式：行情/形态/量化照常跑，只是不落库。
        if not mysql or not mysql.get("user"):
            print("[storage] 未配置 MySQL，以无持久化模式运行"
                  "（K线记录/命中统计/权益曲线/决策流水将不保存）")
        else:
            try:
                self._conn = pymysql.connect(
                    host=mysql.get("host", "127.0.0.1"), port=int(mysql.get("port", 3306)),
                    user=mysql.get("user", "kline"), password=mysql.get("password", ""),
                    database=mysql.get("database", "kline"), autocommit=True,
                    charset="utf8mb4", connect_timeout=5,
                )
                self.enabled = True
            except Exception as e:
                print(f"[storage] MySQL 连接失败，以无持久化模式运行: {e}")
        self._last_prune = 0
        if self.enabled:
            try:
                self._init()
            except Exception as e:
                print(f"[storage] 建表失败，降级为无持久化模式: {e}")
                self.enabled = False
                self._conn = None

    def _q(self, sql: str, args: tuple = ()) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._conn.ping(reconnect=True)
            with self._conn.cursor() as cur:
                cur.execute(sql, args)

    def _qall(self, sql: str, args: tuple = ()) -> list[tuple]:
        if not self.enabled:
            return []
        with self._lock:
            self._conn.ping(reconnect=True)
            with self._conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchall()

    def _init(self) -> None:
        self._q("""CREATE TABLE IF NOT EXISTS candles(
            symbol VARCHAR(16), `interval` VARCHAR(8), open_time BIGINT,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, trades INT,
            PRIMARY KEY(symbol, `interval`, open_time)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        self._q("""CREATE TABLE IF NOT EXISTS hits(
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts BIGINT, symbol VARCHAR(16), `interval` VARCHAR(8), pattern VARCHAR(40),
            name_zh VARCHAR(40), direction VARCHAR(10), mode VARCHAR(10),
            price DOUBLE, bar_open_time BIGINT,
            outcome VARCHAR(10) DEFAULT 'pending', eval_price DOUBLE,
            INDEX idx_hits_bar(symbol, `interval`, bar_open_time),
            INDEX idx_hits_pat(pattern)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # 账户权益采样：卡片迷你曲线用（服务端持久化，刷新页面/换设备都保留历史）
        self._q("""CREATE TABLE IF NOT EXISTS equity(
            acct VARCHAR(64), ts BIGINT, bal DOUBLE, upnl DOUBLE,
            PRIMARY KEY(acct, ts)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # 永久日档案：day 是 UTC 零点毫秒，完全不依赖数据库会话时区。
        self._q("""CREATE TABLE IF NOT EXISTS equity_daily(
            acct VARCHAR(64) NOT NULL, day BIGINT NOT NULL,
            first_ts BIGINT NOT NULL, last_ts BIGINT NOT NULL,
            open DOUBLE NOT NULL, close DOUBLE NOT NULL,
            high DOUBLE NOT NULL, low DOUBLE NOT NULL,
            PRIMARY KEY(acct, day)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        self._backfill_equity_daily()
        # 仓位历史（已平仓记录）：按账户隔离，保留 7 天
        self._q("""CREATE TABLE IF NOT EXISTS quant_trades(
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts BIGINT, acct VARCHAR(64), symbol VARCHAR(16), side VARCHAR(8),
            qty DOUBLE, entry DOUBLE, exit_price DOUBLE, pnl DOUBLE, move_pct DOUBLE,
            reason VARCHAR(200), entry_ts BIGINT, hold_min DOUBLE, pattern VARCHAR(40),
            order_id BIGINT, close_id BIGINT,
            slippage_atr DOUBLE DEFAULT 0, entry_slippage_atr DOUBLE DEFAULT 0,
            INDEX idx_qt_ts(ts), INDEX idx_qt_acct(acct, ts)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # 老库渐进迁移：保留已有交易历史，只补执行质量字段
        for column, ddl in (
            ("slippage_atr", "ALTER TABLE quant_trades ADD COLUMN slippage_atr DOUBLE DEFAULT 0"),
            ("entry_slippage_atr", "ALTER TABLE quant_trades ADD COLUMN entry_slippage_atr DOUBLE DEFAULT 0"),
        ):
            try:
                if not self._qall(f"SHOW COLUMNS FROM quant_trades LIKE '{column}'"):
                    self._q(ddl)
            except Exception:
                pass
        # 量化决策流水：引擎的每个决策/思考都落库，按账户隔离，重启不清空
        self._q("""CREATE TABLE IF NOT EXISTS quant_events(
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts BIGINT, kind VARCHAR(16), acct VARCHAR(64), symbol VARCHAR(16),
            reason VARCHAR(600), pnl DOUBLE, extra TEXT,
            INDEX idx_qe_ts(ts), INDEX idx_qe_acct(acct, ts)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")

    # ---- 仓位历史（已平仓） ----

    def save_quant_trade(self, t: dict) -> int:
        """记录一笔已平仓交易。字段与 quant._record_trade 的 trade 结构对齐。"""
        self._q(
            "INSERT INTO quant_trades(ts,acct,symbol,side,qty,entry,exit_price,pnl,"
            "move_pct,reason,entry_ts,hold_min,pattern,order_id,close_id,"
            "slippage_atr,entry_slippage_atr) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (int(t.get("ts") or 0), str(t.get("account") or ""),
             str(t.get("symbol") or ""), str(t.get("side") or ""),
             float(t.get("qty") or 0), float(t.get("entry") or 0),
             float(t.get("exit") or 0), float(t.get("pnl") or 0),
             float(t.get("movePct") or 0), str(t.get("reason") or "")[:200],
             int(t.get("entryTs") or 0), float(t.get("holdMin") or 0),
             str(t.get("pattern") or ""),
             int(t["orderId"]) if t.get("orderId") else None,
             int(t["closeId"]) if t.get("closeId") else None,
             float(t.get("slippageAtr") or 0),
             float(t.get("entrySlippageAtr") or 0)))
        row = self._qall("SELECT LAST_INSERT_ID()")
        return int(row[0][0]) if row else 0

    def quant_trades(self, acct: str = "", limit: int = 300) -> list[dict]:
        """最近 limit 笔已平仓记录（时间倒序）。acct 非空时只取该账户。"""
        if acct:
            rows = self._qall(
                "SELECT id,ts,acct,symbol,side,qty,entry,exit_price,pnl,move_pct,"
                "reason,entry_ts,hold_min,pattern,slippage_atr,entry_slippage_atr "
                "FROM quant_trades "
                "WHERE acct=%s ORDER BY ts DESC LIMIT %s", (acct, limit))
        else:
            rows = self._qall(
                "SELECT id,ts,acct,symbol,side,qty,entry,exit_price,pnl,move_pct,"
                "reason,entry_ts,hold_min,pattern,slippage_atr,entry_slippage_atr "
                "FROM quant_trades "
                "ORDER BY ts DESC LIMIT %s", (limit,))
        out = []
        for r in rows:
            out.append({
                "id": int(r[0]), "ts": int(r[1]), "account": r[2] or "",
                "symbol": r[3] or "", "side": r[4] or "",
                "qty": float(r[5] or 0), "entry": float(r[6] or 0),
                "exit": float(r[7] or 0), "pnl": float(r[8] or 0),
                "movePct": float(r[9] or 0), "reason": r[10] or "",
                "entryTs": int(r[11] or 0), "holdMin": float(r[12] or 0),
                "pattern": r[13] or "", "slippageAtr": float(r[14] or 0),
                "entrySlippageAtr": float(r[15] or 0),
            })
        return out

    def trade_stats(self, acct: str = "", days: int = 7) -> dict:
        """近 N 天已平仓胜负统计（胜率用）。"""
        since = int(time.time() * 1000) - days * 86400_000
        if acct:
            rows = self._qall(
                "SELECT COUNT(*), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END), SUM(pnl), "
                "AVG(ABS(slippage_atr)), AVG(ABS(entry_slippage_atr)) FROM quant_trades "
                "WHERE acct=%s AND ts>=%s", (acct, since))
        else:
            rows = self._qall(
                "SELECT COUNT(*), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END), SUM(pnl), "
                "AVG(ABS(slippage_atr)), AVG(ABS(entry_slippage_atr)) FROM quant_trades "
                "WHERE ts>=%s", (since,))
        r = rows[0] if rows else (0, 0, 0, 0, 0, 0)
        return {"count": int(r[0] or 0), "wins": int(r[1] or 0),
                "losses": int(r[2] or 0), "pnl": round(float(r[3] or 0), 2),
                "avgSlippageAtr": round(float(r[4] or 0), 4),
                "avgEntrySlippageAtr": round(float(r[5] or 0), 4)}

    # ---- 量化决策流水 ----

    def save_quant_event(self, ev: dict) -> int:
        """写一条决策/事件流水。extra 里放结构化细节（形态、胜率、数量、价格等）。"""
        extra = {k: v for k, v in ev.items()
                 if k not in ("ts", "kind", "account", "symbol", "reason", "pnl")}
        self._q("INSERT INTO quant_events(ts,kind,acct,symbol,reason,pnl,extra) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (int(ev.get("ts") or 0), str(ev.get("kind") or ""),
                 str(ev.get("account") or ""), str(ev.get("symbol") or ""),
                 str(ev.get("reason") or "")[:600],
                 float(ev["pnl"]) if isinstance(ev.get("pnl"), (int, float)) else None,
                 json.dumps(extra, ensure_ascii=False) if extra else None))
        row = self._qall("SELECT LAST_INSERT_ID()")
        return int(row[0][0]) if row else 0

    def quant_events(self, acct: str = "", limit: int = 400) -> list[dict]:
        """取最近 limit 条决策流水（升序）。acct 非空时只取该账户 + 全局事件
        （无账户归属的信号/系统消息对所有账户可见）。"""
        if acct:
            rows = self._qall(
                "SELECT id,ts,kind,acct,symbol,reason,pnl,extra FROM quant_events "
                "WHERE acct=%s OR acct='' ORDER BY id DESC LIMIT %s", (acct, limit))
        else:
            rows = self._qall(
                "SELECT id,ts,kind,acct,symbol,reason,pnl,extra FROM quant_events "
                "ORDER BY id DESC LIMIT %s", (limit,))
        out = []
        for r in reversed(rows):
            ev = {"id": int(r[0]), "ts": int(r[1]), "kind": r[2] or "",
                  "account": r[3] or "", "symbol": r[4] or "", "reason": r[5] or ""}
            if r[6] is not None:
                ev["pnl"] = float(r[6])
            if r[7]:
                try:
                    ev.update(json.loads(r[7]))
                except Exception:
                    pass
            out.append(ev)
        return out

    # ---- 账户权益 ----

    @contextmanager
    def _equity_transaction(self):
        """同一连接上串行提交 raw / daily；中途失败不能删除旧键或漏存日档案。"""
        with self._lock:
            self._conn.ping(reconnect=True)
            self._conn.begin()
            try:
                with self._conn.cursor() as cur:
                    yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _merge_equity_day(cur, acct, day, first_ts, last_ts, open_value,
                          close_value, high, low, *, replace_equal=False):
        """按采样时间合并，保留观测极值；相同时间戳的迁移让目标优先。"""
        cur.execute(
            "INSERT IGNORE INTO equity_daily "
            "(acct,day,first_ts,last_ts,open,close,high,low) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (acct, day, first_ts, last_ts, open_value, close_value, high, low))
        # 先计算 open/close，最后更新时间戳，兼容 MySQL UPDATE 的从左到右赋值。
        first_op, last_op = ("<=", ">=") if replace_equal else ("<", ">")
        cur.execute(
            f"UPDATE equity_daily SET open=CASE WHEN %s {first_op} first_ts "
            "THEN %s ELSE open END, "
            f"close=CASE WHEN %s {last_op} last_ts THEN %s ELSE close END, "
            "high=GREATEST(high,%s), low=LEAST(low,%s), "
            "first_ts=LEAST(first_ts,%s), last_ts=GREATEST(last_ts,%s) "
            "WHERE acct=%s AND day=%s",
            (first_ts, open_value, last_ts, close_value, high, low,
             first_ts, last_ts, acct, day))

    def _backfill_equity_daily(self) -> None:
        """启动时幂等合并现存 raw；已被清理的旧历史无法恢复，也不补造缺失日。"""
        with self._equity_transaction() as cur:
            cur.execute("""SELECT d.acct,d.day,d.first_ts,d.last_ts,
                    ROUND(o.bal,4),ROUND(c.bal,4),d.high,d.low
                FROM (
                    SELECT acct,ts-MOD(ts,86400000) AS day,
                        MIN(ts) AS first_ts,MAX(ts) AS last_ts,
                        MAX(ROUND(bal,4)) AS high,MIN(ROUND(bal,4)) AS low
                    FROM equity GROUP BY acct,day
                ) d
                JOIN equity o ON o.acct=d.acct AND o.ts=d.first_ts
                JOIN equity c ON c.acct=d.acct AND c.ts=d.last_ts""")
            for row in cur.fetchall():
                self._merge_equity_day(cur, *row)

    def save_equity(self, acct: str, ts: int, bal: float, upnl: float) -> None:
        if not self.enabled:
            return
        ts, bal, upnl = int(ts), round(bal, 4), round(upnl, 4)
        total = bal
        with self._equity_transaction() as cur:
            cur.execute("REPLACE INTO equity VALUES(%s,%s,%s,%s)", (acct, ts, bal, upnl))
            self._merge_equity_day(cur, acct, ts - ts % 86400_000,
                                   ts, ts, total, total, total, total, replace_equal=True)

    def equity_heatmap(self, acct: str) -> list[dict]:
        """永久 UTC 日权益档案；bal 已包含未实现盈亏。"""
        rows = self._qall(
            "SELECT day,first_ts,last_ts,open,close,high,low FROM equity_daily "
            "WHERE acct=%s ORDER BY day ASC", (acct,))
        return [{"date": datetime.fromtimestamp(int(r[0]) / 1000, timezone.utc).date().isoformat(),
                 "firstTs": int(r[1]), "lastTs": int(r[2]),
                 "open": float(r[3]), "close": float(r[4]),
                 "high": float(r[5]), "low": float(r[6])} for r in rows]

    def equity_series(self, acct: str, minutes: int = 480, limit: int = 960) -> list[dict]:
        """最近 minutes 分钟的权益采样（升序），供前端画权益曲线。"""
        cutoff = int(time.time() * 1000) - minutes * 60_000
        rows = self._qall(
            "SELECT ts,bal,upnl FROM equity WHERE acct=%s AND ts>=%s ORDER BY ts DESC LIMIT %s",
            (acct, cutoff, limit))
        return [{"t": int(r[0]), "bal": float(r[1]), "upnl": float(r[2])} for r in reversed(rows)]

    def migrate_equity(self, old_keys: list[str], new_key: str) -> int:
        """原子迁移原始采样及永久档案，同时间戳保留新键数据。"""
        if not self.enabled:
            return 0
        moved = 0
        for old in old_keys:
            if not old or old == new_key:
                continue
            try:
                with self._equity_transaction() as cur:
                    cur.execute("SELECT ts,bal,upnl FROM equity WHERE acct=%s", (old,))
                    rows = cur.fetchall()
                    for ts, bal, upnl in rows:
                        cur.execute("INSERT IGNORE INTO equity VALUES(%s,%s,%s,%s)",
                                    (new_key, int(ts), float(bal), float(upnl)))
                    cur.execute("SELECT day,first_ts,last_ts,open,close,high,low "
                                "FROM equity_daily WHERE acct=%s", (old,))
                    for row in cur.fetchall():
                        self._merge_equity_day(cur, new_key, *row)
                    cur.execute("DELETE FROM equity WHERE acct=%s", (old,))
                    cur.execute("DELETE FROM equity_daily WHERE acct=%s", (old,))
                moved += len(rows)
            except Exception as e:
                print(f"[equity] 迁移 {old} -> {new_key} 失败: {e}")
        return moved

    def adopt_orphan_equity(self, new_key: str, known_keys: list[str]) -> int:
        """认领"孤儿"权益数据：库里除 new_key 与已知账户键外的其他键，若是同一账户
        改名留下的（例如改名发生在旧版本、没有留下 id 关联），搬到 new_key。

        只认领与 new_key 现价接近的系列（同一账户的权益量级一致），避免误吞
        其他账户的数据。返回迁移条数。
        """
        moved = 0
        try:
            rows = self._qall("SELECT acct, COUNT(*), AVG(bal) FROM equity GROUP BY acct")
        except Exception:
            return 0
        protect = set(known_keys) | {new_key}
        mine = next((r for r in rows if r[0] == new_key), None)
        mine_avg = float(mine[2]) if mine else None
        for acct, count, avg in rows:
            if acct in protect:
                continue
            avg = float(avg or 0)
            # 同一账户的量级应接近（改名前后权益不会突变一个数量级）
            if mine_avg and mine_avg > 0 and not (0.5 <= avg / mine_avg <= 2.0):
                continue
            moved += self.migrate_equity([acct], new_key)
            if moved:
                print(f"[equity] 认领孤儿历史 {acct} -> {new_key}")
        return moved

    # ---- K线 ----

    def save_candle(self, symbol: str, interval: str, c) -> None:
        self._q("REPLACE INTO candles VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (symbol, interval, c.open_time, c.open, c.high, c.low, c.close,
                 round(c.volume, 6), c.trades))

    def candle_record(self, symbol: str, interval: str, open_time: int) -> dict | None:
        rows = self._qall(
            "SELECT open_time,open,high,low,close,volume,trades FROM candles "
            "WHERE symbol=%s AND `interval`=%s AND open_time=%s",
            (symbol, interval, open_time))
        if not rows:
            return None
        r = rows[0]
        return {"open_time": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                "trades": int(r[6])}

    # ---- 形态命中 ----

    def save_hit(self, hit) -> None:
        self._q("INSERT INTO hits(ts,symbol,`interval`,pattern,name_zh,direction,mode,price,bar_open_time) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (hit.ts, hit.symbol, hit.interval, hit.pattern, hit.name_zh,
                 hit.direction, hit.mode, hit.price, hit.bar_open_time))

    def hits_for_bar(self, symbol: str, interval: str, bar_open_time: int) -> list[dict]:
        rows = self._qall(
            "SELECT ts,pattern,name_zh,direction,mode,price,outcome,eval_price FROM hits "
            "WHERE symbol=%s AND `interval`=%s AND bar_open_time=%s ORDER BY ts",
            (symbol, interval, bar_open_time))
        return [{"ts": int(r[0]), "pattern": r[1], "name_zh": r[2], "direction": r[3],
                 "mode": r[4], "price": float(r[5]), "outcome": r[6],
                 "eval_price": float(r[7]) if r[7] is not None else None} for r in rows]

    # ---- 胜率评估：命中后 eval_minutes 分钟窗口内，价格先触及哪一侧 ----
    # 窗口内价格先向形态方向走满 eval_threshold → win；先反向走满 → loss；
    # 两侧都没触及（横盘）→ flat，不计入胜率。判定只统计收线确认信号。

    def eval_outcomes(self, symbol: str, interval: str, iv_ms: int, candle) -> None:
        rows = self._qall(
            "SELECT id, direction, price, bar_open_time FROM hits "
            "WHERE symbol=%s AND `interval`=%s AND mode='confirmed' AND outcome='pending' "
            "AND direction IN ('bullish','bearish')", (symbol, interval))
        due = [r for r in rows if (candle.open_time + iv_ms) >= r[3] + self.eval_ms]
        for hit_id, direction, price, bar_open_time in due:
            price = float(price)
            wrows = self._qall(
                "SELECT high, low, close FROM candles "
                "WHERE symbol=%s AND `interval`=%s AND open_time > %s AND open_time <= %s "
                "ORDER BY open_time",
                (symbol, interval, bar_open_time, bar_open_time + self.eval_ms))
            if len(wrows) < 3:
                continue        # 窗口内K线数据不足（如服务中途重启），留待下次评估
            up_lvl = price * (1 + self.eval_threshold)
            dn_lvl = price * (1 - self.eval_threshold)
            result = "flat"
            for high, low, close in wrows:
                touch_up = high >= up_lvl
                touch_dn = low <= dn_lvl
                if touch_up and touch_dn:
                    result = "loss"      # 同一根内上下都触及，保守判负
                    break
                if direction == "bullish":
                    if touch_up: result = "win"; break
                    if touch_dn: result = "loss"; break
                else:
                    if touch_dn: result = "win"; break
                    if touch_up: result = "loss"; break
            self._q("UPDATE hits SET outcome=%s, eval_price=%s WHERE id=%s",
                    (result, float(wrows[-1][2]), hit_id))

    # ---- 统计与清理 ----

    def pattern_stats(self) -> dict:
        """Recent pattern outcomes with exponential recency weighting.

        Newest evaluated hit has weight 1, the next ``alpha``, then
        ``alpha**2``, and so on within the configured 7-day retention window.
        Raw counts stay available for audit; the quality gate consumes the
        effective weighted values.
        """
        cutoff = int(time.time() * 1000) - self.stats_days * 86400_000
        rows = self._qall(
            "SELECT pattern, ts, outcome FROM hits "
            "WHERE ts>=%s AND outcome IN ('win','loss') "
            "ORDER BY ts DESC", (cutoff,))
        grouped: dict[str, list[str]] = {}
        for pattern, _ts, outcome in rows:
            grouped.setdefault(str(pattern), []).append(str(outcome))
        out = {}
        for pattern, outcomes in grouped.items():
            wins = outcomes.count("win")
            losses = outcomes.count("loss")
            eff_wins = 0.0
            eff_losses = 0.0
            for rank, outcome in enumerate(outcomes):
                weight = self.stats_alpha ** rank
                if outcome == "win":
                    eff_wins += weight
                else:
                    eff_losses += weight
            effective_n = eff_wins + eff_losses
            out[pattern] = {
                "count": wins + losses,
                "wins": wins,
                "losses": losses,
                "effectiveWins": round(eff_wins, 4),
                "effectiveLosses": round(eff_losses, 4),
                "effectiveSamples": round(effective_n, 4),
                "weightedWinrate": round(eff_wins / effective_n, 6) if effective_n else 0.0,
                "alpha": self.stats_alpha,
                "windowDays": self.stats_days,
            }
        return out

    def symbol_stats(self) -> dict:
        rows = self._qall(
            "SELECT symbol, COUNT(*), "
            "SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) "
            "FROM hits GROUP BY symbol")
        return {r[0]: {"count": int(r[1]), "wins": int(r[2] or 0), "losses": int(r[3] or 0)}
                for r in rows}

    def prune(self, now_ms: int) -> None:
        if now_ms - self._last_prune < 60_000:
            return
        self._last_prune = now_ms
        self._q("DELETE FROM candles WHERE open_time < %s", (now_ms - self.keep_ms,))
        self._q("DELETE FROM hits WHERE ts < %s", (now_ms - self.hit_ms,))
        # 权益采样保留 2 天（曲线只看最近 8 小时，多留一天便于回看）
        self._q("DELETE FROM equity WHERE ts < %s", (now_ms - 2 * 86400_000,))
        # 决策流水保留 3 天（数据更迭快，留够回看即可）
        self._q("DELETE FROM quant_events WHERE ts < %s", (now_ms - 3 * 86400_000,))
        # 仓位历史保留 7 天
        self._q("DELETE FROM quant_trades WHERE ts < %s", (now_ms - 7 * 86400_000,))
