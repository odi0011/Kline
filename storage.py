"""MySQL 存储层（服务器本机 127.0.0.1 的 kline 库，专用账号仅授权该库）：
- candles 表：K线滚动保留（默认最近30分钟）
- hits 表：形态命中记录（含胜负评估），默认保留 90 天（storage.hit_days）
- 胜率：confirmed 命中后 eval_bars 根K线收盘，按方向对比价格判定 win/loss
- paper_positions / paper_trades 表：模拟仓（纸上交易）的未平仓快照与平仓流水
- decision_traces 表：决策 trace / gate 拦截明细（默认 30 天，重启可回看）
- quant_state 表：风控/引擎状态快照（启动时恢复，避免重启把限额/熔断清零）
- candle_archive 表：K线长期归档（candles 只滚动保留最近 N 分钟，归档长期可查）
- agent_state / agent_chat 表：Agent 任务/记忆/聊天摘要与聊天记录（重启恢复）
各业务表保留期统一由 storage.*_days 配置，0 = 永久保留（prune 跳过）。
注意：interval 是 MySQL 保留字，列名统一用反引号。
"""
from __future__ import annotations

import json
import math
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import pymysql


def _safe_float(value, default: float = 0.0) -> float:
    """把 None/空串/NaN/Inf 之类的脏值转成有限 float；坏值取默认值。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _safe_int(value, default: int = 0) -> int:
    """同 _safe_float：NaN/Inf/非数字都回退到默认值，写入 SQL 不报错。"""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _keep_days(value, default: int = 0) -> int:
    """保留期天数归一化：负数/脏值视为 0（永久保留），天数按天保存。"""
    return max(0, _safe_int(value, default))


def _timeout_seconds(value, default: int = 30):
    """MySQL 连接超时归一化：0/None -> None（不设超时），非法/负数回退默认 30 秒。"""
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return default
    if seconds < 0:
        return default
    return seconds or None


def _json_or(value, fallback) -> str:
    """JSON 序列化：脏值回落到空结构，保证整块状态仍能落库。"""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(fallback, ensure_ascii=False)


def _json_load(raw, default):
    """JSON 解析容错：坏数据/空值回落默认结构。"""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class Storage:
    def __init__(self, mysql: dict, keep_minutes: int = 30,
                 hit_days: int = 90, eval_minutes: int = 10,
                 eval_threshold: float = 0.0015,
                 stats_days: int = 7, stats_alpha: float = 0.94,
                 paper_keep_days: int = 0,
                 quant_trade_days: int = 365, quant_event_days: int = 30,
                 news_days: int = 7, trace_days: int = 30,
                 candle_archive_days: int = 0, agent_chat_days: int = 0):
        # 保留期统一按"天"配置：负数/脏值归一化成 0，0 = 永久保留（prune 跳过）。
        hit_days = _keep_days(hit_days, 90)
        self.keep_ms = keep_minutes * 60 * 1000
        self.hit_ms = hit_days * 86400 * 1000
        self.hit_days = hit_days                # 形态命中保留天数（默认 90）
        self.eval_ms = eval_minutes * 60_000    # 命中后10分钟的走势判定胜负
        self.eval_threshold = eval_threshold    # 窗口内有效波动门槛（先触及判定）
        self.stats_days = max(1, int(stats_days))
        self.stats_alpha = min(0.999, max(0.50, float(stats_alpha)))
        self.paper_keep_days = _keep_days(paper_keep_days)          # 0 = 永久保留
        self.trade_keep_days = _keep_days(quant_trade_days, 365)    # 实盘成交（原硬编码 7 天）
        self.quant_event_days = _keep_days(quant_event_days, 30)    # 决策事件（原 3 天）
        self.news_days = _keep_days(news_days, 7)                   # 新闻存档（原 3 天）
        self.trace_days = _keep_days(trace_days, 30)                # 决策 trace/gate
        self.candle_archive_days = _keep_days(candle_archive_days)  # K线归档（0=永久）
        self.agent_chat_days = _keep_days(agent_chat_days)          # Agent 聊天（0=永久）
        self._lock = threading.Lock()
        self._conn = None
        self.enabled = False
        self._ddl_failures: list[str] = []   # 建表/迁移失败的流水，供 /health 与日志排查
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
                    # 读写超时（秒）：0/None -> None（不设超时），非法值回退 30。
                    read_timeout=_timeout_seconds(mysql.get("read_timeout", 30)),
                    write_timeout=_timeout_seconds(mysql.get("write_timeout", 30)),
                )
                self.enabled = True
            except Exception as e:
                print(f"[storage] MySQL 连接失败，以无持久化模式运行: {e}")
        self._last_prune = 0
        if self.enabled:
            try:
                self._init()
            except Exception as e:
                # _init 内部的建表/迁移都已逐条兜住；能走到这里说明是
                # 连接级故障（权限、库不存在），确实没法持久化。
                print(f"[storage] 初始化失败，降级为无持久化模式: {e}")
                self.enabled = False
                self._conn = None
            else:
                if self._ddl_failures:
                    print(f"[storage] 已启用，但有 {len(self._ddl_failures)} 条建表/迁移失败；"
                          f"相关功能不可用，其余照常")

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

    def _ddl(self, sql: str) -> bool:
        """执行一条建表/迁移语句；失败只告警，不拖垮整个存储层。

        曾经一个列名撞上 MySQL 保留字（`trailing`），这一句抛出就
        让 _init 中断，整个服务降级成无持久化——K线、权益、决策流水
        全丢。单条 DDL 失败只应影响它自己那张表。
        返回值用于统计失败数，全失败才认定存储不可用。
        """
        try:
            self._q(sql)
            return True
        except Exception as e:
            self._ddl_failures.append(f"{e}")
            print(f"[storage] 建表/迁移失败（其余表继续）: {e}")
            return False

    def _qmany(self, sql: str, rows: list[tuple]) -> None:
        """批量写入。计算流水一次几十行，逐条 execute 太浪费往返。"""
        if not self.enabled or not rows:
            return
        with self._lock:
            self._conn.ping(reconnect=True)
            with self._conn.cursor() as cur:
                cur.executemany(sql, rows)

    def _init(self) -> None:
        self._ddl("""CREATE TABLE IF NOT EXISTS candles(
            symbol VARCHAR(16), `interval` VARCHAR(8), open_time BIGINT,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, trades INT,
            PRIMARY KEY(symbol, `interval`, open_time)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # K线长期归档：candles 只保留最近 N 分钟（滚动窗口，供实时计算），
        # 历史行情在这里按 storage.candle_archive_days 长期保留（0=永久），
        # 供回测/补算与 Agent 回看历史。interval 是保留字，统一反引号。
        self._ddl("""CREATE TABLE IF NOT EXISTS candle_archive(
            symbol VARCHAR(16), `interval` VARCHAR(8), open_time BIGINT,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, trades INT,
            PRIMARY KEY(symbol, `interval`, open_time),
            INDEX idx_ca_time(open_time)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        self._ddl("""CREATE TABLE IF NOT EXISTS hits(
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts BIGINT, symbol VARCHAR(16), `interval` VARCHAR(8), pattern VARCHAR(40),
            name_zh VARCHAR(40), direction VARCHAR(10), mode VARCHAR(10),
            price DOUBLE, bar_open_time BIGINT,
            outcome VARCHAR(10) DEFAULT 'pending', eval_price DOUBLE,
            INDEX idx_hits_bar(symbol, `interval`, bar_open_time),
            INDEX idx_hits_pat(pattern)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # 账户权益采样：卡片迷你曲线用（服务端持久化，刷新页面/换设备都保留历史）
        self._ddl("""CREATE TABLE IF NOT EXISTS equity(
            acct VARCHAR(64), ts BIGINT, bal DOUBLE, upnl DOUBLE,
            PRIMARY KEY(acct, ts)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # 永久日档案：day 是 UTC 零点毫秒，完全不依赖数据库会话时区。
        self._ddl("""CREATE TABLE IF NOT EXISTS equity_daily(
            acct VARCHAR(64) NOT NULL, day BIGINT NOT NULL,
            first_ts BIGINT NOT NULL, last_ts BIGINT NOT NULL,
            open DOUBLE NOT NULL, close DOUBLE NOT NULL,
            high DOUBLE NOT NULL, low DOUBLE NOT NULL,
            PRIMARY KEY(acct, day)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        self._backfill_equity_daily()
        # 仓位历史（已平仓记录）：按账户隔离，保留 7 天
        self._ddl("""CREATE TABLE IF NOT EXISTS quant_trades(
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
        # 计算流水：每次评估产生的算式行。**只保留最近 1 分钟**，
        # 更早的在写入时顺手清掉。存库是为了两个目的：
        #   1) 页面刷新时能立刻显示最近 20 秒，而不是从 0 开始跑
        #   2) Agent 可以查"刚才系统在算什么"
        # 这不是业务数据，丢了随时能重算，所以保留窗口很短。
        self._ddl("""CREATE TABLE IF NOT EXISTS calc_trace(
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts BIGINT NOT NULL, symbol VARCHAR(16), kind VARCHAR(12), tag VARCHAR(16),
            expr VARCHAR(120), val DOUBLE, tone VARCHAR(8),
            INDEX idx_ct_ts(ts)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # 新闻存档：入库即保存正文，前端点开只读库，不再实时爬源站（避免 429）。
        # content 是正文（RSS 全文或后台抓取的正文），summary 是 RSS 摘要。
        self._ddl("""CREATE TABLE IF NOT EXISTS news(
            url VARCHAR(512) NOT NULL,
            source VARCHAR(64), cat VARCHAR(32), title VARCHAR(600),
            ts BIGINT, summary TEXT, content MEDIUMTEXT,
            partial TINYINT DEFAULT 1, content_state VARCHAR(16) DEFAULT 'pending',
            fetched_ts BIGINT DEFAULT 0, sentiment DOUBLE DEFAULT 0,
            ai_text MEDIUMTEXT, ai_state VARCHAR(16) DEFAULT 'pending',
            ai_ts BIGINT DEFAULT 0,
            PRIMARY KEY(url), INDEX idx_news_ts(ts)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        for column, ddl in (
            ("content_state", "ALTER TABLE news ADD COLUMN content_state VARCHAR(16) DEFAULT 'pending'"),
            ("fetched_ts", "ALTER TABLE news ADD COLUMN fetched_ts BIGINT DEFAULT 0"),
            ("sentiment", "ALTER TABLE news ADD COLUMN sentiment DOUBLE DEFAULT 0"),
            ("ai_text", "ALTER TABLE news ADD COLUMN ai_text MEDIUMTEXT"),
            ("ai_state", "ALTER TABLE news ADD COLUMN ai_state VARCHAR(16) DEFAULT 'pending'"),
            ("ai_ts", "ALTER TABLE news ADD COLUMN ai_ts BIGINT DEFAULT 0"),
        ):
            try:
                self._q(ddl)
            except Exception:
                pass
        self._ddl("""CREATE TABLE IF NOT EXISTS quant_events(
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts BIGINT, kind VARCHAR(16), acct VARCHAR(64), symbol VARCHAR(16),
            reason VARCHAR(600), pnl DOUBLE, extra TEXT,
            INDEX idx_qe_ts(ts), INDEX idx_qe_acct(acct, ts)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # 模拟仓（纸上交易）落库：账本不能只活在内存里，重启归零等于白跑。
        # paper_positions 每币一条未平仓快照（data=完整 PaperPosition JSON），
        # paper_trades 把平仓记录拆成真实列，胜率/分组统计直接在库里算。
        self._ddl("""CREATE TABLE IF NOT EXISTS paper_positions(
            symbol VARCHAR(16) NOT NULL PRIMARY KEY,
            ts BIGINT NOT NULL,
            data MEDIUMTEXT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        self._ddl("""CREATE TABLE IF NOT EXISTS paper_trades(
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            closed_ts BIGINT NOT NULL,
            entry_ts BIGINT DEFAULT 0,
            symbol VARCHAR(16), direction VARCHAR(8),
            entry DOUBLE, exit_price DOUBLE, sl DOUBLE, tp DOUBLE,
            leverage INT, margin DOUBLE, qty DOUBLE,
            pnl DOUBLE, pnl_pct DOUBLE,
            reason VARCHAR(200), score DOUBLE, gap DOUBLE,
            winrate DOUBLE, winrate_parts TEXT,
            grade VARCHAR(16), pattern VARCHAR(40), regime VARCHAR(16),
            atr_at_entry DOUBLE, held_ms BIGINT, `trailing` TINYINT DEFAULT 0,
            recovered TINYINT DEFAULT 0,
            open_seq INT DEFAULT 0, settle_bars INT DEFAULT 0,
            settle_approx TINYINT DEFAULT 0,
            trade_key VARCHAR(48) DEFAULT NULL,
            UNIQUE (trade_key),
            INDEX idx_paper_trades_closed(closed_ts),
            INDEX idx_paper_trades_symbol(symbol, closed_ts)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # 老库渐进迁移：补"重启补结算"标记列与幂等键列，不重建表、不丢历史。
        # （生产首次上线时表本来就是新建的；这里兜住开发/预发已经建过表的库。）
        for column, ddl, unique in (
            ("recovered", "ALTER TABLE paper_trades ADD COLUMN recovered TINYINT DEFAULT 0", False),
            ("trade_key", "ALTER TABLE paper_trades ADD COLUMN trade_key VARCHAR(48) DEFAULT NULL", True),
            # 模拟仓风险修复配套列：进程内开仓序号（同毫秒开仓也不再撞幂等键）、
            # 补算用了多少根K线、是否近似补算。
            ("open_seq", "ALTER TABLE paper_trades ADD COLUMN open_seq INT DEFAULT 0", False),
            ("settle_bars", "ALTER TABLE paper_trades ADD COLUMN settle_bars INT DEFAULT 0", False),
            ("settle_approx", "ALTER TABLE paper_trades ADD COLUMN settle_approx TINYINT DEFAULT 0", False),
        ):
            try:
                if not self._qall(f"SHOW COLUMNS FROM paper_trades LIKE '{column}'"):
                    self._q(ddl)
                    if unique:
                        try:
                            self._q("ALTER TABLE paper_trades ADD UNIQUE (trade_key)")
                        except Exception:
                            pass      # 唯一键已存在/方言不支持都不影响启动
            except Exception:
                pass
        # 决策 trace / gate 拦截明细：分阶段决策记录与过滤器拦截的结构化原因。
        # 之前只活在引擎内存 deque 里，重启即丢；落库后审计/复盘能看到
        # "当时为什么开/没开"，前端与 Agent 也能回看完整 stages/factors/risk。
        self._ddl("""CREATE TABLE IF NOT EXISTS decision_traces(
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts BIGINT NOT NULL,
            kind VARCHAR(12) NOT NULL,
            symbol VARCHAR(16),
            status VARCHAR(24),
            payload MEDIUMTEXT,
            INDEX idx_dt_ts(ts),
            INDEX idx_dt_kind(kind, ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # 引擎/风控状态快照（日亏限额、连亏熔断、机制漂移保护…）：重启接着上次状态跑。
        self._ddl("""CREATE TABLE IF NOT EXISTS quant_state(
            name VARCHAR(32) NOT NULL PRIMARY KEY,
            ts BIGINT NOT NULL,
            data MEDIUMTEXT NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # Agent 状态进库：任务列表/记忆/聊天摘要，重启后恢复而不是从零开始。
        # 三行独立覆盖（name=tasks/memory/meta），meta 带版本号与聊天摘要。
        self._ddl("""CREATE TABLE IF NOT EXISTS agent_state(
            name VARCHAR(32) NOT NULL PRIMARY KEY,
            ts BIGINT,
            data MEDIUMTEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # Agent 聊天归档：按 storage.agent_chat_days 保留（0=永久），data 存完整消息 JSON。
        self._ddl("""CREATE TABLE IF NOT EXISTS agent_chat(
            msg_id VARCHAR(64) NOT NULL PRIMARY KEY,
            ts BIGINT, seq INT DEFAULT 0, role VARCHAR(16),
            content MEDIUMTEXT, data MEDIUMTEXT,
            INDEX idx_ac_ts(ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # 老库迁移：补 seq 列。同一毫秒内的 user/assistant 消息只按 ts/message_id
        # 排序会颠倒，seq 记录"本批消息内的先后顺序"，重启后聊天窗口顺序才稳定。
        try:
            if not self._qall("SHOW COLUMNS FROM agent_chat LIKE 'seq'"):
                self._q("ALTER TABLE agent_chat ADD COLUMN seq INT DEFAULT 0")
        except Exception:
            pass
        # 表情包库：GIPHY 搜到的贴纸落盘后在这里登记一份元数据。
        # 这是 Agent 挑选表情时的**真实数据源**——它能列出库里有什么、按 id 直接发，
        # 而不是只能猜一个查不到的名字。图片本体在 agent-attachments/stickers/，
        # 表里只存文件名和尺寸（浏览器据此预占空间，不会加载后撑开气泡）。
        self._ddl("""CREATE TABLE IF NOT EXISTS stickers(
            id VARCHAR(64) NOT NULL PRIMARY KEY,
            file VARCHAR(128) NOT NULL,
            title VARCHAR(200) DEFAULT '',
            tags VARCHAR(600) DEFAULT '',
            width INT DEFAULT 0, height INT DEFAULT 0,
            bytes INT DEFAULT 0,
            created_ts BIGINT DEFAULT 0,
            last_used_ts BIGINT DEFAULT 0,
            use_count INT DEFAULT 0,
            INDEX idx_stickers_used(last_used_ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # hits 保留期从 7 天拉长到 90 天后，pattern_stats 的 "WHERE ts>=..." 查询
        # 不能再依赖全表扫：老库补一条 ts 索引。SQLite 等方言不支持 SHOW INDEX /
        # ADD INDEX 语法，探测失败或迁移失败都静默跳过，不影响服务启动。
        try:
            have = self._qall("SHOW INDEX FROM hits WHERE Key_name='idx_hits_ts'")
        except Exception:
            have = None
        if have is not None and not have:
            try:
                self._q("ALTER TABLE hits ADD INDEX idx_hits_ts(ts)")
            except Exception:
                pass

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

    # ---- 计算流水（只留 1 分钟） ----

    def save_calc_rows(self, rows: list[dict]) -> int:
        """批量写入计算流水，并顺手清掉 1 分钟前的。

        为什么保留窗口这么短：这是运算轨迹不是业务数据，
        重算就有；存着只是为了让页面刷新时不是一片空白、
        以及 Agent 能查"刚才在算什么"。
        """
        if not self.enabled or not rows:
            return 0
        payload = []
        for r in rows:
            try:
                ts = int(r.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if ts <= 0:
                continue
            val = r.get("value")
            try:
                val = float(val) if val is not None else None
            except (TypeError, ValueError):
                val = None
            payload.append((
                ts, str(r.get("sym") or "")[:16], str(r.get("kind") or "")[:12],
                str(r.get("tag") or "")[:16], str(r.get("expr") or "")[:120],
                val, str(r.get("tone") or "")[:8],
            ))
        if not payload:
            return 0
        self._qmany("INSERT INTO calc_trace(ts,symbol,kind,tag,expr,val,tone) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)", payload)
        # 只留最近 1 分钟：更早的整行删掉，表不会无限增长
        cutoff = int(time.time() * 1000) - 60_000
        self._q("DELETE FROM calc_trace WHERE ts < %s", (cutoff,))
        return len(payload)

    def calc_recent(self, seconds: int = 20) -> list[dict]:
        """最近 N 秒的流水，按时间升序。页面刷新时用它填充。

        默认 20 秒：前端一进来就有内容，不会从 0 开始跑。
        """
        cutoff = int(time.time() * 1000) - max(1, int(seconds)) * 1000
        rows = self._qall(
            "SELECT ts,symbol,kind,tag,expr,val,tone FROM calc_trace "
            "WHERE ts >= %s ORDER BY ts ASC, id ASC", (cutoff,))
        out = []
        for ts, sym, kind, tag, expr, val, tone in rows:
            out.append({
                "ts": int(ts or 0), "sym": sym or "", "kind": kind or "",
                "tag": tag or "", "expr": expr or "",
                "value": val, "tone": tone or "",
            })
        return out

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

    # ---- 决策 trace / 引擎状态 ----

    def save_decision_trace(self, trace: dict, kind: str = "trace") -> None:
        """落一条决策 trace（kind='trace'）或 gate 拦截明细（kind='gate'）。

        payload 存完整结构化 dict（阶段/因子/风控/多周期明细），便于重启后
        页面和 Agent 回看"当时为什么开/没开"；列上的 ts/kind/symbol/status
        冗余出来只为按时间和类型过滤，不替代 payload。
        """
        if not self.enabled or not isinstance(trace, dict):
            return
        now = int(time.time() * 1000)
        ts = _safe_int(trace.get("ts"), now) or now
        kind = str(kind or "trace")[:12]
        symbol = str(trace.get("symbol") or "")[:16]
        # 注意 or 链最后必须再 or ""：gate 记录常常没有 status/code，
        # 否则 str(None) 会把冗余列写成字符串 "None"。
        code = trace.get("code") if kind == "gate" else ""
        status = str(trace.get("status") or code or "")[:24]
        try:
            payload = json.dumps(trace, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        self._q("INSERT INTO decision_traces(ts,kind,symbol,status,payload) "
                "VALUES(%s,%s,%s,%s,%s)", (ts, kind, symbol, status, payload))

    def decision_traces(self, limit: int = 100, kind: str = "trace") -> list[dict]:
        """最近的决策 trace / gate 拦截记录（newest-first，默认只看 trace）。

        返回 payload 原 dict，再用 setdefault 补 "id"/"kind"/"savedTs" 三个元数据键；
        payload 里已有同名键时保留 payload 的值（引擎恢复依赖原始结构）。
        坏 JSON 只丢 payload、仍返回带元数据的条目：一条脏数据不能清空整页。
        """
        limit = max(1, min(1000, _safe_int(limit, 100)))
        if not self.enabled:
            return []
        rows = self._qall(
            "SELECT id,ts,kind,symbol,status,payload FROM decision_traces "
            "WHERE kind=%s ORDER BY ts DESC, id DESC LIMIT %s",
            (str(kind or "trace")[:12], limit))
        out = []
        for r in rows:
            try:
                item = json.loads(r[5]) if r[5] else {}
            except (TypeError, ValueError):
                item = {}
            if not isinstance(item, dict):
                item = {}
            item.setdefault("id", int(r[0]))
            item.setdefault("kind", r[2] or "")
            item.setdefault("savedTs", int(r[1]))
            out.append(item)
        return out

    def save_quant_state(self, name: str, data: dict) -> None:
        """保存一份引擎/风控状态快照（同 name 覆盖），如 'risk' / 'drift'。"""
        if not self.enabled:
            return
        name = str(name or "")[:32]
        if not name:
            return
        try:
            payload = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        self._q("REPLACE INTO quant_state(name,ts,data) VALUES(%s,%s,%s)",
                (name, int(time.time() * 1000), payload))

    def quant_state(self, name: str) -> dict | None:
        """读回同名状态快照；没有/坏 JSON/非 dict 都返回 None。"""
        if not self.enabled:
            return None
        name = str(name or "")[:32]
        if not name:
            return None
        rows = self._qall("SELECT data FROM quant_state WHERE name=%s LIMIT 1", (name,))
        if not rows:
            return None
        try:
            data = json.loads(rows[0][0])
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ---- Agent 状态（任务 / 记忆 / 聊天摘要 + 聊天归档） ----

    def save_agent_state(self, tasks: list, memory: dict,
                         chat_summary: str = "") -> None:
        """保存 Agent 状态：任务列表、记忆、聊天摘要（三行独立覆盖）。

        meta 行带版本号，摘要截 4000 字；任一段序列化失败回落到空结构，
        保证三行状态始终能整体恢复（不能因一段脏数据整块写不进去）。
        """
        if not self.enabled:
            return
        now = int(time.time() * 1000)
        meta = {"version": 2, "chatSummary": str(chat_summary or "")[:4000]}
        rows = [
            ("tasks", now, _json_or(tasks, [])),
            ("memory", now, _json_or(memory, {})),
            ("meta", now, _json_or(meta, {})),
        ]
        self._qmany("REPLACE INTO agent_state(name,ts,data) VALUES(%s,%s,%s)", rows)

    def load_agent_state(self) -> dict | None:
        """读回 Agent 状态；三行全无/异常返回 None，坏 JSON 按空结构处理。"""
        if not self.enabled:
            return None
        try:
            rows = self._qall("SELECT name,data FROM agent_state")
        except Exception:
            return None
        found = {str(name): data for name, data in rows}
        if not ({"tasks", "memory", "meta"} & set(found)):
            return None
        out = {"tasks": [], "memory": {}, "chatSummary": ""}
        tasks = _json_load(found.get("tasks"), [])
        out["tasks"] = tasks if isinstance(tasks, list) else []
        memory = _json_load(found.get("memory"), {})
        out["memory"] = memory if isinstance(memory, dict) else {}
        meta = _json_load(found.get("meta"), {})
        if isinstance(meta, dict):
            out["chatSummary"] = str(meta.get("chatSummary") or "")[:4000]
        return out

    def save_agent_chat(self, messages: list[dict]) -> int:
        """逐条保存 Agent 聊天消息（msg_id 幂等覆盖），返回成功写入条数。

        无 id 的消息跳过（没有稳定主键就无法去重）；content 截 200000 字，
        data 存完整消息 JSON，坏结构回落成 {id,role,content}。
        """
        if not self.enabled or not messages:
            return 0
        now = int(time.time() * 1000)
        rows = []
        for seq, item in enumerate(messages):
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            if raw_id is None:
                continue
            msg_id = str(raw_id)[:64]
            if not msg_id:
                continue
            role = str(item.get("role") or "")[:16]
            content = item.get("content")
            content = "" if content is None else str(content)[:200000]
            try:
                data = json.dumps(item, ensure_ascii=False)
            except (TypeError, ValueError):
                data = json.dumps({"id": raw_id, "role": role, "content": content},
                                  ensure_ascii=False)
            rows.append((msg_id, _safe_int(item.get("ts"), 0) or now, seq, role,
                         content, data))
        if not rows:
            return 0
        self._qmany("REPLACE INTO agent_chat(msg_id,ts,seq,role,content,data) "
                    "VALUES(%s,%s,%s,%s,%s,%s)", rows)
        return len(rows)

    def load_agent_chat(self, limit: int = 40) -> list[dict]:
        """取最近 limit 条聊天，按时间升序返回（limit 夹 1..500）。"""
        limit = max(1, min(500, _safe_int(limit, 40)))
        if not self.enabled:
            return []
        rows = self._qall(
            "SELECT msg_id,ts,seq,role,content,data FROM agent_chat "
            "ORDER BY ts DESC, seq DESC, msg_id DESC LIMIT %s", (limit,))
        out = []
        for msg_id, ts, _seq, role, content, data in reversed(rows):
            item = _json_load(data, None)
            if not isinstance(item, dict):
                # 坏 data 用列上的最小字段重建，不能因为一条脏消息丢掉整页
                item = {"id": msg_id, "role": role or "", "content": content or ""}
            out.append(item)
        return out

    def clear_agent_chat(self) -> None:
        """清空聊天归档（用户点「清空对话」时调用；任务/偏好在 agent_state 里不受影响）。"""
        if not self.enabled:
            return
        self._q("DELETE FROM agent_chat")

    def trim_agent_chat(self, keep_ids: list[str]) -> int:
        """只保留给定 msg_id 的聊天归档，返回删除行数。

        Agent 内存窗口被压缩/清空后调用：库里比当前窗口更旧的消息要同步删掉，
        否则重启时按"最近 40 条"恢复会把已经清掉/压缩掉的对话重新复活。
        空列表 = 清空（等价 clear_agent_chat）。
        """
        if not self.enabled:
            return 0
        ids = []
        for value in keep_ids or []:
            text = str(value or "")[:64]
            if text and text not in ids:
                ids.append(text)
            if len(ids) > 500:
                break
        if not ids:
            self.clear_agent_chat()
            return 0
        marks = ",".join(["%s"] * len(ids))
        before = self._qall("SELECT COUNT(*) FROM agent_chat")
        self._q(f"DELETE FROM agent_chat WHERE msg_id NOT IN ({marks})", tuple(ids))
        return max(0, int((before[0][0] if before else 0) or 0) - len(ids))

    # ---- 模拟仓（纸上交易） ----

    def save_paper_position(self, pos: dict) -> None:
        """UPSERT 未平仓仓位快照（每币一条）。重启后 PaperBook 靠它恢复仓位。"""
        if not self.enabled or not isinstance(pos, dict):
            return
        symbol = str(pos.get("symbol") or "")[:16]
        if not symbol:
            return
        self._q("REPLACE INTO paper_positions(symbol,ts,data) VALUES(%s,%s,%s)",
                (symbol, int(time.time() * 1000),
                 json.dumps(pos, ensure_ascii=False)))

    def delete_paper_position(self, symbol: str) -> None:
        """平仓或移出监控后删掉该币的未平仓快照。"""
        if not self.enabled:
            return
        symbol = str(symbol or "")[:16]
        if not symbol:
            return
        self._q("DELETE FROM paper_positions WHERE symbol=%s", (symbol,))

    def paper_positions(self) -> list[dict]:
        """全部未平仓仓位（按保存时间升序）。坏行跳过，一条脏数据不能拖垮恢复。"""
        rows = self._qall("SELECT data FROM paper_positions ORDER BY ts ASC")
        out = []
        for (data,) in rows:
            try:
                item = json.loads(data)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def save_paper_trade(self, trade: dict) -> None:
        """记录一笔已平仓的模拟仓交易（camelCase 字典，字段与 PaperBook 平仓记录一致）。

        统计要用的 count/timeout/pnl 是真实列，胜率分量这种嵌套结构才进 JSON，
        这样 summary 不用把所有历史读进内存再算（内存窗口会漏掉更早的样本）。
        """
        if not self.enabled or not isinstance(trade, dict):
            return
        now = int(time.time() * 1000)
        # 胜率分量只是辅助解释，序列化失败不能拖累整笔交易入库。
        parts = trade.get("winrateParts")
        try:
            parts_json = json.dumps(parts, ensure_ascii=False) if parts else None
        except (TypeError, ValueError):
            parts_json = None
        symbol = str(trade.get("symbol") or "")[:16]
        entry_ts = _safe_int(trade.get("entryTs"), 0)
        open_seq = _safe_int(trade.get("openSeq"), 0)
        # 幂等键：同一仓位实例（symbol + 开仓毫秒 + 进程内开仓序号）只允许一条平仓流水。
        # 网络抖动可能"INSERT 已提交但客户端报错"，重试若再插一条就会让
        # 胜率/盈亏统计翻倍；INSERT IGNORE + 唯一键把重试变成幂等操作。
        # 只有 openSeq>0 才拼第三段：老数据/老调用方仍走 symbol:entryTs，
        # 两种格式各自唯一，互不冲突。
        if symbol and entry_ts > 0:
            trade_key = (f"{symbol}:{entry_ts}:{open_seq}" if open_seq > 0
                         else f"{symbol}:{entry_ts}")
        else:
            trade_key = None
        self._q(
            "INSERT IGNORE INTO paper_trades(closed_ts,entry_ts,symbol,direction,entry,"
            "exit_price,sl,tp,leverage,margin,qty,pnl,pnl_pct,reason,score,gap,"
            "winrate,winrate_parts,grade,pattern,regime,atr_at_entry,held_ms,`trailing`,"
            "recovered,open_seq,settle_bars,settle_approx,trade_key) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (_safe_int(trade.get("closedAt"), now) or now,
             entry_ts,
             symbol,
             str(trade.get("direction") or "")[:8],
             _safe_float(trade.get("entry")),
             _safe_float(trade.get("exit")),
             _safe_float(trade.get("sl")),
             _safe_float(trade.get("tp")),
             _safe_int(trade.get("leverage"), 0),
             _safe_float(trade.get("margin")),
             _safe_float(trade.get("qty")),
             _safe_float(trade.get("pnl")),
             _safe_float(trade.get("pnlPct")),
             str(trade.get("reason") or "")[:200],
             _safe_float(trade.get("score")),
             _safe_float(trade.get("gap")),
             _safe_float(trade.get("winrate")),
             parts_json,
             str(trade.get("grade") or "")[:16],
             str(trade.get("pattern") or "")[:40],
             str(trade.get("regime") or "")[:16],
             _safe_float(trade.get("atrAtEntry")),
             _safe_int(trade.get("heldMs"), 0),
             1 if trade.get("trailing") else 0,
             1 if trade.get("recovered") else 0,
             open_seq,
             _safe_int(trade.get("settleBars"), 0),
             _safe_int(trade.get("settleApprox"), 0),
             trade_key))

    def paper_trades(self, limit: int = 200) -> list[dict]:
        """最近 limit 笔已平仓记录（时间倒序），字段与 PaperBook 平仓记录一致。"""
        limit = max(1, min(2000, _safe_int(limit, 200)))
        rows = self._qall(
            "SELECT id,symbol,direction,entry,exit_price,sl,tp,leverage,margin,qty,"
            "pnl,pnl_pct,reason,winrate,score,gap,grade,pattern,regime,atr_at_entry,"
            "held_ms,closed_ts,entry_ts,`trailing`,recovered,winrate_parts,"
            "open_seq,settle_bars,settle_approx "
            "FROM paper_trades ORDER BY closed_ts DESC, id DESC LIMIT %s", (limit,))
        out = []
        for r in rows:
            item = {
                "id": int(r[0]), "symbol": r[1] or "", "direction": r[2] or "",
                "entry": float(r[3] or 0), "exit": float(r[4] or 0),
                "sl": float(r[5] or 0), "tp": float(r[6] or 0),
                "leverage": int(r[7] or 0), "margin": float(r[8] or 0),
                "qty": float(r[9] or 0), "pnl": float(r[10] or 0),
                "pnlPct": float(r[11] or 0), "reason": r[12] or "",
                "winrate": float(r[13] or 0), "score": float(r[14] or 0),
                "gap": float(r[15] or 0), "grade": r[16] or "",
                "pattern": r[17] or "", "regime": r[18] or "",
                "atrAtEntry": float(r[19] or 0), "heldMs": int(r[20] or 0),
                "closedAt": int(r[21] or 0), "entryTs": int(r[22] or 0),
                "trailing": bool(r[23]), "recovered": bool(r[24]),
                # SELECT 顺序与上面对齐：winrate_parts 在 25，新增三列在 26..28。
                "openSeq": int(r[26] or 0), "settleBars": int(r[27] or 0),
                "settleApprox": int(r[28] or 0),
            }
            try:
                parts = json.loads(r[25]) if r[25] else {}
            except (TypeError, ValueError):
                parts = {}
            item["winrateParts"] = parts if isinstance(parts, dict) else {}
            out.append(item)
        return out

    def paper_trade_stats(self, days: int = 0) -> dict:
        """模拟仓平仓汇总。days=0 统计全部，否则只看最近 days 天的 closed_ts。"""
        sql = ("SELECT COUNT(*), "
               "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), "
               "SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END), "
               "SUM(CASE WHEN reason='timeout' THEN 1 ELSE 0 END), SUM(pnl) "
               "FROM paper_trades")
        days = _safe_int(days, 0)
        if days > 0:
            since = int(time.time() * 1000) - days * 86400_000
            rows = self._qall(sql + " WHERE closed_ts>=%s", (since,))
        else:
            rows = self._qall(sql)
        r = rows[0] if rows else (0, 0, 0, 0, 0)
        return {"count": int(r[0] or 0), "wins": int(r[1] or 0),
                "losses": int(r[2] or 0), "timeouts": int(r[3] or 0),
                "pnl": round(float(r[4] or 0), 4)}

    def paper_symbol_stats(self, days: int = 0) -> list[dict]:
        """按币种汇总模拟仓战绩（含 netPnl），空 symbol 跳过。"""
        sql = ("SELECT symbol, COUNT(*), "
               "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), "
               "SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END), "
               "SUM(CASE WHEN reason='timeout' THEN 1 ELSE 0 END), SUM(pnl) "
               "FROM paper_trades")
        days = _safe_int(days, 0)
        if days > 0:
            since = int(time.time() * 1000) - days * 86400_000
            rows = self._qall(
                sql + " WHERE closed_ts>=%s GROUP BY symbol ORDER BY symbol", (since,))
        else:
            rows = self._qall(sql + " GROUP BY symbol ORDER BY symbol")
        out = []
        for r in rows:
            if not r[0]:
                continue
            out.append({"symbol": r[0], "closed": int(r[1] or 0),
                        "wins": int(r[2] or 0), "losses": int(r[3] or 0),
                        "timeouts": int(r[4] or 0),
                        "netPnl": round(float(r[5] or 0), 4)})
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

    # ---- K线长期归档 ----

    @staticmethod
    def _archive_row(symbol: str, interval: str, candle) -> tuple | None:
        """把 Candle（或带同名属性的对象）转成归档行；坏数据返回 None。"""
        try:
            open_time = int(getattr(candle, "open_time", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if open_time <= 0:
            return None
        return (symbol, interval, open_time,
                _safe_float(getattr(candle, "open", 0)),
                _safe_float(getattr(candle, "high", 0)),
                _safe_float(getattr(candle, "low", 0)),
                _safe_float(getattr(candle, "close", 0)),
                _safe_float(getattr(candle, "volume", 0)),
                _safe_int(getattr(candle, "trades", 0), 0))

    def archive_candle(self, symbol: str, interval: str, candle) -> None:
        """归档一根K线（同主键 REPLACE 覆盖）；坏数据静默跳过。"""
        self.archive_candles(symbol, interval, [candle])

    def archive_candles(self, symbol: str, interval: str, candles) -> int:
        """批量归档K线（REPLACE），返回实际写入根数；坏数据逐根跳过。"""
        if not self.enabled or not candles:
            return 0
        sym = str(symbol or "")[:16]
        iv = str(interval or "")[:8]
        if not sym or not iv:
            return 0
        rows = [row for row in (self._archive_row(sym, iv, c) for c in candles)
                if row is not None]
        if not rows:
            return 0
        self._qmany(
            "REPLACE INTO candle_archive(symbol,`interval`,open_time,open,high,"
            "low,close,volume,trades) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
        return len(rows)

    def archive_range(self, symbol: str, interval: str, start_ms: int,
                      end_ms: int, limit: int = 20000) -> list[dict]:
        """按时间范围（含两端）读归档K线，升序返回；limit 夹 1..200000。"""
        limit = max(1, min(200_000, _safe_int(limit, 20000)))
        if not self.enabled:
            return []
        rows = self._qall(
            "SELECT open_time,open,high,low,close,volume,trades FROM candle_archive "
            "WHERE symbol=%s AND `interval`=%s AND open_time>=%s AND open_time<=%s "
            "ORDER BY open_time ASC LIMIT %s",
            (str(symbol or "")[:16], str(interval or "")[:8],
             _safe_int(start_ms, 0), _safe_int(end_ms, 0), limit))
        return [{"t": int(r[0]), "o": _safe_float(r[1]), "h": _safe_float(r[2]),
                 "l": _safe_float(r[3]), "c": _safe_float(r[4]),
                 "v": _safe_float(r[5]), "trades": _safe_int(r[6], 0)}
                for r in rows]

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
        ``alpha**2``, and so on within the configured hit retention window (storage.hit_days, default 90 days).
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

    # ---- 新闻存档 ----

    def save_news(self, items: list[dict]) -> int:
        """把一批抓到的新闻写入/更新存档（按 url 去重）。

        只写「比库里更新」的字段：正文抓到了就保留，不会被后续轮询的
        摘要覆盖；标题/摘要/时间则总是跟随最新一次 RSS。
        """
        if not self.enabled or not items:
            return 0
        rows = []
        for item in items:
            url = str(item.get("url") or "")[:512]
            if not url:
                continue
            content = item.get("content")
            content = content[:2_000_000] if isinstance(content, str) else None
            summary = str(item.get("desc") or "")[:8000]
            rows.append((
                url, str(item.get("source") or "")[:64], str(item.get("cat") or "")[:32],
                str(item.get("title") or "")[:600], int(item.get("ts") or 0),
                summary, content,
                0 if content is not None and item.get("contentPartial") is False else 1,
                "done" if content else "pending",
                int(time.time() * 1000) if content else 0,
                float(item.get("sentiment") or 0),
            ))
        if not rows:
            return 0
        with self._lock:
            self._conn.ping(reconnect=True)
            with self._conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO news(url,source,cat,title,ts,summary,content,partial,"
                    "content_state,fetched_ts,sentiment) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE source=VALUES(source), cat=VALUES(cat), "
                    "title=VALUES(title), ts=VALUES(ts), summary=VALUES(summary), "
                    "sentiment=VALUES(sentiment), "
                    # 正文只在拿到时覆盖，不会被空值冲掉
                    "content=IF(VALUES(content) IS NULL, content, VALUES(content)), "
                    "partial=IF(VALUES(content) IS NULL, partial, VALUES(partial)), "
                    "content_state=IF(VALUES(content) IS NULL, content_state, VALUES(content_state)), "
                    "fetched_ts=IF(VALUES(content) IS NULL, fetched_ts, VALUES(fetched_ts))",
                    rows)
        return len(rows)

    def save_news_content(self, url: str, content: str, partial: bool) -> None:
        """后台预取回写。

        拿到全文 → content_state='done'，以后不再抓；
        只拿到摘要/失败 → 保留 'pending'，只更新 fetched_ts（30 分钟后可重试），
        否则一条抓不到正文的新闻会被永久跳过。
        """
        if not self.enabled or not url:
            return
        content = (content or "")[:2_000_000]
        state = "done" if content and not partial else "pending"
        self._q("UPDATE news SET content=COALESCE(NULLIF(%s,''), content), partial=%s, "
                "content_state=%s, fetched_ts=%s WHERE url=%s",
                (content, 0 if not partial else 1, state,
                 int(time.time() * 1000), url[:512]))

    def news_full(self, url: str) -> dict | None:
        """读一条新闻的完整存档（含正文）。前端点开时只走这里。"""
        rows = self._qall(
            "SELECT url,source,cat,title,ts,summary,content,partial,content_state,fetched_ts "
            "FROM news WHERE url=%s", (url[:512],))
        if not rows:
            return None
        r = rows[0]
        return {"url": r[0], "source": r[1], "cat": r[2], "title": r[3], "ts": r[4],
                "desc": r[5] or "", "content": r[6], "partial": bool(r[7]),
                "contentState": r[8], "fetchedTs": r[9]}

    def news_pending(self, limit: int = 12) -> list[dict]:
        """还没抓到正文、且最近没试失败过的新闻（供后台预取队列）。"""
        cutoff = int(time.time() * 1000) - 30 * 60 * 1000
        rows = self._qall(
            "SELECT url,source,cat,title,ts,summary FROM news "
            "WHERE content_state='pending' AND fetched_ts < %s "
            "ORDER BY ts DESC LIMIT %s", (cutoff, min(int(limit), 50)))
        return [{"url": r[0], "source": r[1], "cat": r[2], "title": r[3],
                 "ts": r[4], "desc": r[5] or ""} for r in rows]

    def news_recent(self, limit: int = 300) -> list[dict]:
        """最近新闻（不含正文，用于首屏快照）。"""
        rows = self._qall(
            "SELECT url,source,cat,title,ts,summary,content IS NOT NULL "
            "FROM news ORDER BY ts DESC LIMIT %s", (min(int(limit), 1000),))
        return [{"url": r[0], "source": r[1], "cat": r[2], "title": r[3],
                 "ts": r[4], "desc": r[5] or "", "hasContent": bool(r[6])} for r in rows]

    # ---- 新闻 AI 分析（正文入库后自动生成，用户点开直接读） ----

    def save_news_ai(self, url: str, text: str, state: str = "done") -> None:
        if not self.enabled or not url:
            return
        self._q("UPDATE news SET ai_text=%s, ai_state=%s, ai_ts=%s WHERE url=%s",
                ((text or "")[:200_000], state[:16], int(time.time() * 1000), url[:512]))

    def news_ai(self, url: str) -> dict | None:
        rows = self._qall("SELECT ai_text, ai_state, ai_ts FROM news WHERE url=%s",
                          (url[:512],))
        if not rows:
            return None
        text, state, ts = rows[0]
        return {"text": text or "", "state": state or "pending", "ts": ts or 0}

    def news_ai_pending(self, limit: int = 6) -> list[dict]:
        """还没分析过的新闻（正文或摘要皆可，按时间从新到旧）。

        线上源站经常 429/451 拿不到正文，如果只挑"有正文"的去分析，
        这一块会一直空着。摘要也能出方向判断，只是标注资料不完整。
        """
        rows = self._qall(
            "SELECT url,source,title,ts,content,summary,partial FROM news "
            "WHERE (ai_state='pending' OR ai_state IS NULL) "
            "AND (content IS NOT NULL OR (summary IS NOT NULL AND summary <> '')) "
            "ORDER BY ts DESC LIMIT %s", (min(int(limit), 30),))
        return [{"url": r[0], "source": r[1], "title": r[2], "ts": r[3],
                 "content": r[4], "desc": r[5] or "", "partial": bool(r[6])} for r in rows]

    @staticmethod
    def _keep_since(now_ms: int, days: float) -> int:
        """保留 N 个完整自然日（向下对齐到整点边界）。

        原来直接用 now - N*86400000 截断，会把第 N 天上午的数据削掉一半，
        看起来就像"站点突然被清空了一段"。按整天对齐后，被保留的那一天
        永远是完整的。
        """
        delta = int(days * 86400_000)
        cutoff = int(now_ms) - delta
        if days >= 1:
            # 对齐到 UTC 零点：那一天整天都保留
            return cutoff - (cutoff % 86400_000)
        return cutoff

    # ---- 表情包库（Agent 挑选用的真实数据） ----

    def save_sticker(self, record: dict) -> bool:
        """登记/更新一张已落盘的贴纸。tags 是它对应的情绪词（空格分隔）。"""
        if not self.enabled:
            return False
        sid = str(record.get("id") or "")[:64]
        file = str(record.get("file") or "")[:128]
        if not sid or not file:
            return False
        now = int(record.get("ts") or time.time() * 1000)
        self._q(
            "REPLACE INTO stickers(id,file,title,tags,width,height,bytes,"
            "created_ts,last_used_ts,use_count) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (sid, file, str(record.get("title") or "")[:200],
             str(record.get("tags") or "")[:600],
             _safe_int(record.get("width"), 0), _safe_int(record.get("height"), 0),
             _safe_int(record.get("bytes"), 0), now, 0, 0))
        return True

    def stickers(self, query: str = "", limit: int = 200) -> list[dict]:
        """表情包清单（最近用过的排前面）。

        query 为空时返回全部；否则按 tags 里的词做包含匹配。
        Agent 就是靠这个看到"库里到底有什么"，再按 id 指定发送。
        """
        limit = max(1, min(1000, _safe_int(limit, 200)))
        like = f"%{str(query or '').strip().lower()[:60]}%" if str(query or "").strip() else ""
        if like:
            rows = self._qall(
                "SELECT id,file,title,tags,width,height,bytes,created_ts,"
                "last_used_ts,use_count FROM stickers WHERE LOWER(tags) LIKE %s "
                "ORDER BY last_used_ts DESC, created_ts DESC LIMIT %s", (like, limit))
        else:
            rows = self._qall(
                "SELECT id,file,title,tags,width,height,bytes,created_ts,"
                "last_used_ts,use_count FROM stickers "
                "ORDER BY last_used_ts DESC, created_ts DESC LIMIT %s", (limit,))
        return [{"id": r[0], "file": r[1], "title": r[2] or "", "tags": r[3] or "",
                 "width": int(r[4] or 0), "height": int(r[5] or 0),
                 "bytes": int(r[6] or 0), "createdTs": int(r[7] or 0),
                 "lastUsedTs": int(r[8] or 0), "useCount": int(r[9] or 0)}
                for r in rows]

    def touch_sticker(self, sticker_id: str) -> None:
        """记一次使用：让"最近发过的"排在前面，也让重复发送有据可查。"""
        if not self.enabled:
            return
        self._q("UPDATE stickers SET last_used_ts=%s, use_count=use_count+1 WHERE id=%s",
                (int(time.time() * 1000), str(sticker_id or "")[:64]))

    def forget_stickers(self, keep_ids: list[str]) -> list[str]:
        """删掉不在 keep_ids 里的记录，返回被删掉的 id（供调用方清理文件）。"""
        if not self.enabled:
            return []
        keep = [str(i)[:64] for i in (keep_ids or [])]
        rows = self._qall("SELECT id FROM stickers")
        all_ids = [r[0] for r in rows]
        drop = [sid for sid in all_ids if sid not in set(keep)]
        if not drop:
            return []
        placeholders = ",".join(["%s"] * len(drop))
        try:
            self._q(f"DELETE FROM stickers WHERE id IN ({placeholders})", tuple(drop))
        except Exception:
            return []
        return drop

    def sticker_stats(self) -> dict:
        rows = self._qall("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM stickers")
        if not rows:
            return {"count": 0, "bytes": 0}
        return {"count": int(rows[0][0] or 0), "bytes": int(rows[0][1] or 0)}

    def prune(self, now_ms: int) -> None:
        if now_ms - self._last_prune < 60_000:
            return
        self._last_prune = now_ms
        self._q("DELETE FROM candles WHERE open_time < %s", (now_ms - self.keep_ms,))
        # 各业务表按 storage.*_days 清理，0 = 永久保留（跳过清理）。
        # _keep_since 对齐到整天：被保留的那一天永远是完整的，不会削掉半天。
        if self.hit_days > 0:
            self._q("DELETE FROM hits WHERE ts < %s",
                    (self._keep_since(now_ms, self.hit_days),))
        # 权益采样保留 2 天（曲线只看最近 8 小时，多留一天便于回看）
        self._q("DELETE FROM equity WHERE ts < %s", (self._keep_since(now_ms, 2),))
        for table, days in (
            ("quant_events", self.quant_event_days),   # 决策流水（原硬编码 3 天）
            ("quant_trades", self.trade_keep_days),    # 实盘成交（原硬编码 7 天）
            ("news", self.news_days),                  # 新闻存档（原硬编码 3 天）
            ("decision_traces", self.trace_days),      # 决策 trace / gate 拦截
        ):
            if days > 0:
                self._q(f"DELETE FROM {table} WHERE ts < %s",
                        (self._keep_since(now_ms, days),))
        # 模拟仓平仓历史按 paper_keep_days 保留（0=永久）。未平仓仓位不能删：
        # 重启后 PaperBook 要靠它恢复仓位，再由策略正常平掉。
        if self.paper_keep_days > 0:
            self._q("DELETE FROM paper_trades WHERE closed_ts < %s",
                    (self._keep_since(now_ms, self.paper_keep_days),))
        # K线长期归档 / Agent 聊天记录：0 = 永久保留（跳过清理）。
        if self.candle_archive_days > 0:
            self._q("DELETE FROM candle_archive WHERE open_time < %s",
                    (self._keep_since(now_ms, self.candle_archive_days),))
        if self.agent_chat_days > 0:
            self._q("DELETE FROM agent_chat WHERE ts < %s",
                    (self._keep_since(now_ms, self.agent_chat_days),))
