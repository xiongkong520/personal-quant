"""DuckDB 连接管理（状态库 ``data/db/quant.duckdb``，契约 §3.3 / §3.4）。

职责
----
- 统一 ``connect(db_path, read_only=False)``：自动建父目录、读写锁冲突重试、
  同进程内按库文件串行（避免同一文件同时以读写/只读两种配置打开导致 DuckDB 报错）；
- 返回 **原生 DuckDB 连接**，因此 ``?`` 占位符、``executemany``、``.df()`` /
  ``.fetchdf()``、``$name`` 参数等 DuckDB 2.x 能力全部可用；
- 提供行转 dict、事务、建表检查等小工具，避免各模块各写一套。

DuckDB 并发要点（实测 2.0.1）
- 同进程多连接：支持，但同一文件不能同时存在「读写」与「只读」两种配置；
  这里用进程内可重入锁把同一库文件的连接串行化（连接关闭时释放）。
- 跨进程：单写多读并非无锁——某进程持有写连接时，其它进程连只读也拿不到文件锁，
  会抛 ``Conflicting lock is held``。``open_connection`` 对此类错误退避重试
  ``RETRY_ATTEMPTS`` 次；仍失败则抛 ``DatabaseUnavailable``（调用方可降级返回空数据）。
- 只读打开要求文件存在且是合法 DuckDB 库；否则抛 ``FileNotFoundError`` /
  ``DatabaseUnavailable``，由 API 等调用方决定降级策略。
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar

import duckdb

logger = logging.getLogger(__name__)

#: 锁冲突时的重试次数（含首次尝试）与退避秒数。
RETRY_ATTEMPTS = 3
RETRY_BACKOFF: tuple[float, ...] = (0.05, 0.15, 0.35)

T = TypeVar("T")

# 同一库文件在进程内的串行锁：值为 (RLock, 引用计数)。关闭连接时释放。
_LOCK_GUARD = threading.Lock()
_FILE_LOCKS: dict[str, threading.RLock] = {}


class DatabaseUnavailable(RuntimeError):
    """数据库文件不可用（被其它进程锁定 / 非 DuckDB 文件 / 文件损坏）。"""


# ---------------------------------------------------------------------------
# 锁 / 重试
# ---------------------------------------------------------------------------
def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCK_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
        return lock


def is_lock_error(exc: BaseException) -> bool:
    """判断异常是否为 DuckDB 文件锁冲突（可重试）。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return "conflicting lock" in text or "could not set lock" in text


def _is_retryable(exc: BaseException) -> bool:
    return is_lock_error(exc)


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = RETRY_ATTEMPTS,
    backoff: tuple[float, ...] = RETRY_BACKOFF,
    what: str = "duckdb",
) -> T:
    """执行 ``fn``，锁冲突时退避重试；非锁错误立即抛出。"""
    last: BaseException | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 按 DuckDB 锁错误类型过滤
            if not _is_retryable(exc):
                raise
            last = exc
            if attempt >= attempts - 1:
                break
            delay = backoff[min(attempt, len(backoff) - 1)]
            logger.warning("%s 文件锁冲突，%.2fs 后重试（%d/%d）：%s", what, delay, attempt + 1, attempts, exc)
            time.sleep(delay)
    raise DatabaseUnavailable(f"{what} 数据库被其它进程锁定，重试 {attempts} 次仍失败：{last}") from last


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def _open(path: Path, read_only: bool) -> duckdb.DuckDBPyConnection:
    if read_only:
        if not path.is_file():
            raise FileNotFoundError(f"DuckDB 库不存在: {path}")
        if path.stat().st_size == 0:
            raise DatabaseUnavailable(f"DuckDB 库文件为空: {path}")
        return duckdb.connect(str(path), read_only=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


@contextmanager
def connect(
    db_path: str | Path,
    read_only: bool = False,
    *,
    attempts: int = RETRY_ATTEMPTS,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """打开 DuckDB 连接（上下文管理器），成功提交、异常回滚由调用方 statement 语义决定。

    用法::

        with connect(db_path, read_only=True) as con:
            con.execute("SELECT ...").fetchall()

    连接关闭后自动释放进程内串行锁；锁冲突按 ``RETRY_ATTEMPTS`` 重试。
    """
    path = Path(db_path)
    lock = _lock_for(path)
    lock.acquire()
    con: duckdb.DuckDBPyConnection | None = None
    try:
        con = retry_call(
            lambda: _open(path, read_only),
            attempts=attempts,
            what=f"DuckDB({'ro' if read_only else 'rw'})",
        )
        yield con
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001 - 关闭失败不掩盖业务异常
                logger.debug("DuckDB 连接关闭失败", exc_info=True)
        lock.release()


def open_connection(
    db_path: str | Path,
    read_only: bool = False,
    *,
    attempts: int = RETRY_ATTEMPTS,
) -> tuple[duckdb.DuckDBPyConnection, threading.RLock]:
    """打开连接并返回 ``(conn, lock)``；调用方负责 ``conn.close()`` 与 ``lock.release()``。

    仅供需要手动管理连接生命周期的低层代码使用；一般请优先用 ``connect()``。
    """
    path = Path(db_path)
    lock = _lock_for(path)
    lock.acquire()
    try:
        con = retry_call(lambda: _open(path, read_only), attempts=attempts, what="duckdb")
    except BaseException:
        lock.release()
        raise
    return con, lock


# ---------------------------------------------------------------------------
# 查询小工具
# ---------------------------------------------------------------------------
def fetch_dicts(
    conn: duckdb.DuckDBPyConnection, sql: str, params: Iterable[Any] | None = None
) -> list[dict[str, Any]]:
    """执行查询并把每行转成 ``{列名: 值}``（不依赖 DuckDB 的 row_factory）。"""
    result = conn.execute(sql, list(params or ()))
    columns = [str(desc[0]) for desc in (result.description or ())]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def fetch_dict(
    conn: duckdb.DuckDBPyConnection, sql: str, params: Iterable[Any] | None = None
) -> dict[str, Any] | None:
    rows = fetch_dicts(conn, sql, params)
    return rows[0] if rows else None


def fetch_scalar(
    conn: duckdb.DuckDBPyConnection, sql: str, params: Iterable[Any] | None = None, default: Any = None
) -> Any:
    # 必须 fetchall：DuckDB 单连接在结果未取完时不允许再执行下一条语句
    rows = conn.execute(sql, list(params or ())).fetchall()
    if not rows or rows[0][0] is None:
        return default
    return rows[0][0]


def fetch_df(conn: duckdb.DuckDBPyConnection, sql: str, params: Iterable[Any] | None = None):
    """执行查询返回 pandas DataFrame（DuckDB ``.df()`` 的薄封装）。"""
    return conn.execute(sql, list(params or ())).df()


@contextmanager
def transaction(conn: duckdb.DuckDBPyConnection) -> Iterator[duckdb.DuckDBPyConnection]:
    """显式事务：正常退出 COMMIT，异常 ROLLBACK。

    DuckDB 的 ``with conn:`` 是「关闭连接」而不是事务，所以这里手动 BEGIN/COMMIT；
    DuckDB 不支持嵌套事务，调用方不要在同一连接上嵌套使用。
    """
    conn.execute("BEGIN TRANSACTION")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - 事务可能已被 DuckDB 自动中止
            logger.debug("DuckDB ROLLBACK 失败", exc_info=True)
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------
def table_exists(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    try:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=? AND table_schema='main'",
            [str(table)],
        ).fetchall()
    except duckdb.Error:
        return False
    return bool(rows)


def table_names(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """当前库 main schema 的用户表（按名字排序）。"""
    rows = fetch_dicts(
        conn,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main' AND table_catalog=current_database() ORDER BY table_name",
    )
    return [str(r["table_name"]) for r in rows]


def columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    """表列名（不存在返回 []）。"""
    try:
        rows = fetch_dicts(
            conn,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='main' AND table_name=? ORDER BY ordinal_position",
            [str(table)],
        )
    except duckdb.Error:
        return []
    return [str(r["column_name"]) for r in rows]


def count_rows(conn: duckdb.DuckDBPyConnection, table: str, where: str = "1=1") -> int:
    """``COUNT(*)``（表不存在返回 0）。"""
    if not table_exists(conn, table):
        return 0
    value = fetch_scalar(conn, f"SELECT COUNT(*) FROM {table} WHERE {where}", default=0)
    return int(value or 0)


def is_duckdb_file(path: str | Path) -> bool:
    """判断路径是否为可读的 DuckDB 库（不创建、不修改）。"""
    target = Path(path)
    if not target.is_file() or target.stat().st_size == 0:
        return False
    try:
        with connect(target, read_only=True, attempts=1) as con:
            con.execute("SELECT 1").fetchall()
    except (FileNotFoundError, DatabaseUnavailable, duckdb.Error, OSError):
        return False
    return True


__all__ = [
    "DatabaseUnavailable",
    "RETRY_ATTEMPTS",
    "columns",
    "connect",
    "count_rows",
    "fetch_df",
    "fetch_dict",
    "fetch_dicts",
    "fetch_scalar",
    "is_duckdb_file",
    "is_lock_error",
    "open_connection",
    "retry_call",
    "table_exists",
    "table_names",
    "transaction",
]
