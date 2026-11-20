"""SQLite 连接与建表。

数据库路径由环境变量 MATRIX_DB_PATH 决定，默认 data/matrix.db。
连接采用 autocommit + 显式 BEGIN IMMEDIATE，配合忙等待，保证交印事务串行生效。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("MATRIX_DB_PATH", str(BASE_DIR / "data" / "matrix.db"))

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS libraries (
        library_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        edition    TEXT NOT NULL,
        priority   INTEGER NOT NULL,
        current    INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(name, edition)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS glyphs (
        glyph_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id INTEGER NOT NULL REFERENCES libraries(library_id),
        matrix_no  TEXT NOT NULL,
        glyph_char TEXT NOT NULL,
        UNIQUE(library_id, matrix_no)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_glyphs_char ON glyphs(glyph_char)",
    """
    CREATE TABLE IF NOT EXISTS volumes (
        volume_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS volume_chars (
        volume_id    INTEGER NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
        glyph_char   TEXT NOT NULL,
        source_order INTEGER NOT NULL,
        PRIMARY KEY (volume_id, glyph_char)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS volume_char_pages (
        volume_id    INTEGER NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
        glyph_char   TEXT NOT NULL,
        page_no      TEXT NOT NULL,
        source_order INTEGER NOT NULL,
        PRIMARY KEY (volume_id, glyph_char, page_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS active_matrices (
        volume_id INTEGER NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
        glyph_id  INTEGER NOT NULL REFERENCES glyphs(glyph_id),
        PRIMARY KEY (volume_id, glyph_id)
    )
    """,
    "CREATE TABLE IF NOT EXISTS submit_seq (seq INTEGER NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_no       TEXT PRIMARY KEY,
        seq            INTEGER NOT NULL UNIQUE,
        volume_id      INTEGER NOT NULL,
        volume_name    TEXT NOT NULL,
        idem_key       TEXT,
        fingerprint    TEXT NOT NULL,
        payload_json   TEXT NOT NULL,
        created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idem
    ON orders(idem_key) WHERE idem_key IS NOT NULL
    """,
    # 交印时随单归档的“当次实际用模”记录：字模身份信息快照为文本，
    # 改稿或换库后历史单仍可按 (字库, 版次, 字模号) 索引回查，无需逐单扫描。
    # page_no 为 NULL 表示该单出自无页码用字表（回查时标“页码未知”）。
    """
    CREATE TABLE IF NOT EXISTS order_matrix_pages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no     TEXT NOT NULL REFERENCES orders(order_no),
        seq          INTEGER NOT NULL,
        volume_name  TEXT NOT NULL,
        library_name TEXT NOT NULL,
        edition      TEXT NOT NULL,
        matrix_no    TEXT NOT NULL,
        glyph_char   TEXT NOT NULL,
        page_no      TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_omp_matrix
    ON order_matrix_pages(library_name, edition, matrix_no)
    """,
    "CREATE INDEX IF NOT EXISTS idx_omp_order ON order_matrix_pages(order_no)",
]


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _backfill_order_matrix_pages(conn: sqlite3.Connection) -> None:
    """为建表前已存在的旧单补建回查索引行（幂等）。

    旧单没有逐页记录：按单内 volume_after 字模清单补 page_no=NULL 的行，
    回查时与无页码单一样标“页码未知”。
    """
    import json

    rows = conn.execute(
        """
        SELECT o.order_no, o.seq, o.volume_name, o.payload_json
        FROM orders o
        WHERE NOT EXISTS (
            SELECT 1 FROM order_matrix_pages p WHERE p.order_no = o.order_no
        )
        """
    ).fetchall()
    for r in rows:
        payload = json.loads(r["payload_json"])
        pages = payload.get("pages") or []
        entries: list[tuple] = []
        if pages:
            for pg in pages:
                for it in pg.get("items", []):
                    entries.append(
                        (it["library_name"], it["edition"], it["matrix_no"],
                         it["glyph_char"], pg["page_no"])
                    )
        else:
            for it in payload.get("volume_after", []):
                entries.append(
                    (it["library_name"], it["edition"], it["matrix_no"],
                     it["glyph_char"], None)
                )
        conn.executemany(
            "INSERT INTO order_matrix_pages(order_no, seq, volume_name, "
            "library_name, edition, matrix_no, glyph_char, page_no) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (r["order_no"], r["seq"], r["volume_name"],
                 lib, ed, no, ch, page)
                for lib, ed, no, ch, page in entries
            ],
        )


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.execute("INSERT OR IGNORE INTO submit_seq(seq) VALUES (0)")
        _backfill_order_matrix_pages(conn)
    finally:
        conn.close()
