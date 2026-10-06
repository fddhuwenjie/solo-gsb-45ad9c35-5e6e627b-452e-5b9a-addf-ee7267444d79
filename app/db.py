"""SQLite 数据访问层。

并发模型：
- 所有写操作通过 ``write_tx`` 进入，先取进程级可重入锁，再以
  ``BEGIN IMMEDIATE`` 开启事务（立即持有 SQLite 写锁），因此
  "检查冲突 -> 写入" 是原子的，并发申请/更正/复核/改派不会交错，
  不会出现重复使用或中间态。
- 读操作使用独立连接，WAL 模式下读写互不阻塞；涉及多张表的一致性
  查询（如进度）走 ``read_tx``，多条 SELECT 取同一快照。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS external_courses (
    id      TEXT PRIMARY KEY,
    credits INTEGER NOT NULL CHECK (credits > 0),
    score   REAL    NOT NULL CHECK (score >= 0 AND score <= 100)
);

CREATE TABLE IF NOT EXISTS requirements (
    id               TEXT PRIMARY KEY,
    required_credits INTEGER NOT NULL CHECK (required_credits > 0)
);

CREATE TABLE IF NOT EXISTS substitutions (
    course_id      TEXT NOT NULL REFERENCES external_courses (id),
    requirement_id TEXT NOT NULL REFERENCES requirements (id),
    min_score      REAL NOT NULL CHECK (min_score >= 0 AND min_score <= 100),
    PRIMARY KEY (course_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS recognitions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id      TEXT NOT NULL REFERENCES external_courses (id),
    requirement_id TEXT NOT NULL REFERENCES requirements (id),
    status         TEXT NOT NULL CHECK (status IN ('confirmed', 'pending')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_recognitions_course ON recognitions (course_id);
CREATE INDEX IF NOT EXISTS idx_recognitions_requirement ON recognitions (requirement_id);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", os.path.join(os.getcwd(), "data", "app.db"))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    """建库建表（幂等），容器启动与测试都会调用。"""
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """写事务：进程锁 + BEGIN IMMEDIATE，提交或整体回滚。"""
    with _write_lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """只读事务：多条 SELECT 共享同一快照（WAL），保证一次进度查询
    读到同一提交状态，不会与写事务交错而混出"改派前+改派后"的结果。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
