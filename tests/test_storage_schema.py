"""存储层建表健壮性：单条 DDL 失败不能拖垮整个持久化。

线上事故：paper_trades 的列名 `trailing` 撞上 MySQL 保留字，
CREATE TABLE 抛错 → _init 中断 → 整个服务降级成"无持久化模式"，
K线归档、权益曲线、决策流水全部静默丢失，日志里只有一行。

两道防线，各自有测试：
  1. SQL 里所有 MySQL 保留字列名必须加反引号（静态检查，覆盖建表与 DML）。
  2. 某一条建表失败只影响它自己那张表，其余照常（行为测试）。
"""
import re

import storage
from storage import Storage
from tests.test_paper_store import SQLConnection

# MySQL 5.7 / 8.0 **保留字**中，本仓库 schema 可能踩到、且看着像普通列名的那些。
# 注意 data / status / level / type / current 在 MySQL 里是**非**保留字，
# 不能列进来——那会把合法 schema 误判成错误。
RESERVED = {
    # 5.7 就有
    "trailing", "interval", "order", "group", "index", "key", "keys",
    "desc", "asc", "range", "partition", "precision", "usage", "option",
    "match", "read", "write", "table", "column", "set", "values", "to",
    # 8.0 新增的窗口函数/CTE 关键字
    "rank", "rows", "groups", "window", "over", "lead", "lag", "system",
    "recursive", "cume_dist", "dense_rank", "ntile", "percent_rank",
    "row_number", "first_value", "last_value", "nth_value", "except",
}
_SQL_HINT = re.compile(r"\b(CREATE TABLE|ALTER TABLE|SELECT|INSERT|UPDATE|DELETE|SHOW COLUMNS)\b")
_TABLE_CONSTRAINT = re.compile(r"^(INDEX|UNIQUE|PRIMARY|KEY|CONSTRAINT|FOREIGN)\b", re.I)
# 列定义：<col> <TYPE>...。group(1)=列名，group(2)="`" 表示带反引号。
_COLUMN_DEF = re.compile(
    r"^(`?)([A-Za-z_]\w*)\1\s+(?:VARCHAR|INT|BIGINT|DOUBLE|TEXT|MEDIUMTEXT|TINYINT|"
    r"FLOAT|DECIMAL|JSON|BLOB|CHAR|DATETIME|SMALLINT|DATE|TIME|BOOL)", re.I)


def _unquoted_reserved(definition: str) -> str | None:
    """列定义里裸用了保留字就返回列名，否则 None。"""
    m = _COLUMN_DEF.match(definition)
    if not m:
        return None
    quoted, name = m.group(1), m.group(2)
    return None if quoted else (name if name.lower() in RESERVED else None)


def _split_columns(body: str) -> list[str]:
    """把建表语句体按逗号切成"一个列/约束一项"，括号内的逗号不切。

    不能按行切：schema 里常有多列写在同一行
    （`atr_at_entry DOUBLE, held_ms BIGINT, trailing TINYINT`），
    按行只会看到第一个列名，正好漏掉这次的真凶。
    """
    items, depth, buf = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        items.append("".join(buf).strip())
    return items


def _create_table_bodies(src: str) -> list[tuple[str, str]]:
    return re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\)\s*ENGINE", src, re.S)


def _sql_strings(src: str) -> list[str]:
    """只挑出"看起来是 SQL"的字符串常量，跳过 dict 键/普通文案。"""
    out = []
    for literal in re.findall(r'"(?:[^"\\]|\\.)*"', src):
        body = literal[1:-1]
        if _SQL_HINT.search(body):
            out.append(body)
    return out


def test_no_reserved_word_column_is_left_unquoted():
    """schema 里出现保留字列名时，必须写成 `trailing` 而不是 trailing。"""
    src = open(storage.__file__, encoding="utf-8").read()
    offenders = []
    for table, body in _create_table_bodies(src):
        for item in _split_columns(body):
            if _TABLE_CONSTRAINT.match(item):
                continue
            name = _unquoted_reserved(item)
            if name:
                offenders.append(f"{table}.{name}")
    assert not offenders, f"这些列名是 MySQL 保留字，必须加反引号: {offenders}"


def test_reserved_words_are_quoted_in_every_sql_statement():
    """写入/读取语句里的列名同样要加反引号——建表和 DML 是两处独立的 SQL。

    只检查**列定义**与**显式列清单**这两类位置：`ORDER BY ts DESC` 里的
    DESC、`SET a=%s` 里的 SET 都是 SQL 关键字本身，不该算违规。
    """
    src = open(storage.__file__, encoding="utf-8").read()
    bad = []
    for sql in _sql_strings(src):
        # 1) CREATE TABLE 的列定义（按逗号切，一行多列也能看到）
        body = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?\w+\s*\((.*)\)\s*ENGINE", sql, re.S)
        if body:
            for item in _split_columns(body.group(1)):
                if _TABLE_CONSTRAINT.match(item):
                    continue
                name = _unquoted_reserved(item)
                if name:
                    bad.append(f"{name} @ CREATE TABLE {sql[:50]!r}")
        # 2) ALTER TABLE ... ADD COLUMN <col>（带反引号的跳过）
        for m in re.finditer(r"ADD COLUMN (`?)([A-Za-z_]\w*)\1", sql):
            if not m.group(1) and m.group(2).lower() in RESERVED:
                bad.append(f"{m.group(2)} @ {sql[:60]!r}")
        # 3) INSERT INTO t(...) 的显式列清单（带反引号的跳过）
        for m in re.finditer(r"INSERT (?:IGNORE )?INTO \w+\(([^)]*)\)", sql):
            for col in m.group(1).split(","):
                col = col.strip()
                bare = col.strip("`")
                if bare.lower() in RESERVED and not col.startswith("`"):
                    bad.append(f"{col} @ INSERT {sql[:60]!r}")
    assert not bad, "SQL 里裸用了 MySQL 保留字作为列名：\n" + "\n".join(bad)


def test_one_failing_ddl_does_not_disable_storage(monkeypatch):
    """单条 CREATE TABLE 失败后，storage 仍然可用，失败被记录而不是吞掉。"""
    conn = SQLConnection()
    conn.db.execute("CREATE TABLE equity(acct TEXT,ts BIGINT,bal DOUBLE,"
                    "upnl DOUBLE,PRIMARY KEY(acct,ts))")
    original = SQLConnection.cursor

    class FailingCursor:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def execute(self, sql, args=()):
            # 模拟 MySQL 对裸保留字的反应：只有 paper_trades 建表失败
            if "CREATE TABLE IF NOT EXISTS paper_trades(" in sql:
                raise Exception("(1064) syntax error near 'trailing'")
            return self._inner.execute(sql, args)

        def executemany(self, sql, rows):
            return self._inner.executemany(sql, rows)

        def fetchall(self):
            return self._inner.fetchall()

    def cursor(self):
        return FailingCursor(original(self))

    monkeypatch.setattr(SQLConnection, "cursor", cursor)
    monkeypatch.setattr(storage.pymysql, "connect", lambda **_: conn)
    store = Storage({"user": "isolated-test"})
    assert store.enabled is True, "一条建表失败不该让整个存储层停摆"
    assert len(store._ddl_failures) == 1
    assert "trailing" in store._ddl_failures[0]
    # 其他表照常建好：权益采样能写能读
    store.save_equity("acct", 1_800_000_000_000, 100.0, 0.0)
    assert store.equity_series("acct")


def test_ddl_helper_returns_false_on_failure_and_keeps_going():
    class Boom:
        def _q(self, sql, args=()):
            raise RuntimeError("boom")

        _ddl_failures: list = []
        _ddl = Storage._ddl

    ok = Boom()._ddl("CREATE TABLE x(...)")
    assert ok is False
    assert Boom._ddl_failures and "boom" in Boom._ddl_failures[0]
