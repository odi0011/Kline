"""MySQL 存储层（服务器本机 127.0.0.1 的 kline 库，专用账号仅授权该库）：
- candles 表：K线滚动保留（默认最近30分钟）
- hits 表：形态命中记录（含胜负评估），保留 7 天
- 胜率：confirmed 命中后 eval_bars 根K线收盘，按方向对比价格判定 win/loss
注意：interval 是 MySQL 保留字，列名统一用反引号。
"""
from __future__ import annotations

import threading
import time

import pymysql


class Storage:
    def __init__(self, mysql: dict, keep_minutes: int = 30,
                 hit_days: int = 7, eval_minutes: int = 10,
                 eval_threshold: float = 0.0015):
        self.keep_ms = keep_minutes * 60 * 1000
        self.hit_ms = hit_days * 86400 * 1000
        self.eval_ms = eval_minutes * 60_000    # 命中后10分钟的走势判定胜负
        self.eval_threshold = eval_threshold    # 窗口内有效波动门槛（先触及判定）
        self._lock = threading.Lock()
        self._conn = pymysql.connect(
            host=mysql.get("host", "127.0.0.1"), port=int(mysql.get("port", 3306)),
            user=mysql.get("user", "kline"), password=mysql.get("password", ""),
            database=mysql.get("database", "kline"), autocommit=True,
            charset="utf8mb4", connect_timeout=5,
        )
        self._last_prune = 0
        self._init()

    def _q(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._conn.ping(reconnect=True)
            with self._conn.cursor() as cur:
                cur.execute(sql, args)

    def _qall(self, sql: str, args: tuple = ()) -> list[tuple]:
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

    # ---- 账户权益 ----

    def save_equity(self, acct: str, ts: int, bal: float, upnl: float) -> None:
        self._q("REPLACE INTO equity VALUES(%s,%s,%s,%s)",
                (acct, ts, round(bal, 4), round(upnl, 4)))

    def equity_series(self, acct: str, minutes: int = 480, limit: int = 960) -> list[dict]:
        """最近 minutes 分钟的权益采样（升序），供前端画权益曲线。"""
        cutoff = int(time.time() * 1000) - minutes * 60_000
        rows = self._qall(
            "SELECT ts,bal,upnl FROM equity WHERE acct=%s AND ts>=%s ORDER BY ts DESC LIMIT %s",
            (acct, cutoff, limit))
        return [{"t": int(r[0]), "bal": float(r[1]), "upnl": float(r[2])} for r in reversed(rows)]

    def migrate_equity(self, old_keys: list[str], new_key: str) -> int:
        """把旧键（历史账户名 / 旧 ID）下的权益采样迁移到新键（稳定 ID）。

        账户改名/首次升级时调用：不迁移的话曲线会因为查不到历史而"重置"。
        时间戳冲突时保留新键已有数据（INSERT IGNORE 后再删旧行）。
        """
        moved = 0
        for old in old_keys:
            if not old or old == new_key:
                continue
            try:
                rows = self._qall("SELECT ts,bal,upnl FROM equity WHERE acct=%s", (old,))
                for ts, bal, upnl in rows:
                    self._q("INSERT IGNORE INTO equity VALUES(%s,%s,%s,%s)",
                            (new_key, int(ts), float(bal), float(upnl)))
                self._q("DELETE FROM equity WHERE acct=%s", (old,))
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
        rows = self._qall(
            "SELECT pattern, COUNT(*), "
            "SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) "
            "FROM hits GROUP BY pattern")
        return {r[0]: {"count": int(r[1]), "wins": int(r[2] or 0), "losses": int(r[3] or 0)}
                for r in rows}

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
