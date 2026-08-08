"""SQLite 저장소. 스레드 안전(연결 per-call + WAL)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '분석 중...',
    category TEXT NOT NULL DEFAULT 'general',      -- valuable | general | food
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'stored',         -- stored | retrieved | disposed
    photo_path TEXT NOT NULL DEFAULT '',
    patch_path TEXT NOT NULL DEFAULT '',
    bbox TEXT NOT NULL DEFAULT '',                 -- JSON [x, y, w, h] (처리 해상도 기준)
    registered_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    retrieved_at TEXT,
    disposed_at TEXT,
    warn_sent INTEGER NOT NULL DEFAULT 0,
    expire_sent INTEGER NOT NULL DEFAULT 0,
    ai_provider TEXT NOT NULL DEFAULT '',
    ai_confidence REAL NOT NULL DEFAULT 0,
    ai_status TEXT NOT NULL DEFAULT 'pending',     -- pending | done | failed
    source TEXT NOT NULL DEFAULT 'camera'          -- camera | manual
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    item_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── settings ─────────────────────────────────────────────
    def get_all_settings(self) -> dict[str, str]:
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_settings(self, values: dict[str, str]) -> None:
        with self._lock, self._conn() as c:
            c.executemany(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                list(values.items()),
            )

    # ── items ────────────────────────────────────────────────
    def insert_item(self, **fields) -> int:
        fields.setdefault("registered_at", _now())
        cols = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        with self._lock, self._conn() as c:
            cur = c.execute(
                f"INSERT INTO items({cols}) VALUES({marks})", list(fields.values())
            )
            return int(cur.lastrowid)

    def update_item(self, item_id: int, **fields) -> bool:
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn() as c:
            cur = c.execute(
                f"UPDATE items SET {sets} WHERE id=?", [*fields.values(), item_id]
            )
            return cur.rowcount > 0

    def get_item(self, item_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return self._item_dict(row) if row else None

    def delete_item(self, item_id: int) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM items WHERE id=?", (item_id,))
            return cur.rowcount > 0

    def list_items(
        self,
        status: str | None = None,
        category: str | None = None,
        query: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        sql = "SELECT * FROM items"
        conds, args = [], []
        if status:
            conds.append("status=?")
            args.append(status)
        if category:
            conds.append("category=?")
            args.append(category)
        if query:
            conds.append("(name LIKE ? OR description LIKE ?)")
            args += [f"%{query}%", f"%{query}%"]
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [self._item_dict(r) for r in rows]

    def stored_items(self) -> list[dict]:
        return self.list_items(status="stored", limit=1000)

    @staticmethod
    def _item_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        try:
            d["bbox"] = json.loads(d["bbox"]) if d["bbox"] else None
        except (json.JSONDecodeError, TypeError):
            d["bbox"] = None
        return d

    # ── events ───────────────────────────────────────────────
    def add_event(self, type_: str, message: str, item_id: int | None = None) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO events(type, message, item_id, created_at) VALUES(?,?,?,?)",
                (type_, message, item_id, _now()),
            )

    def list_events(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── stats ────────────────────────────────────────────────
    def stats(self) -> dict:
        today = datetime.now().date().isoformat()
        with self._conn() as c:
            stored = c.execute(
                "SELECT COUNT(*) FROM items WHERE status='stored'"
            ).fetchone()[0]
            retrieved = c.execute(
                "SELECT COUNT(*) FROM items WHERE status='retrieved'"
            ).fetchone()[0]
            disposed = c.execute(
                "SELECT COUNT(*) FROM items WHERE status='disposed'"
            ).fetchone()[0]
            today_cnt = c.execute(
                "SELECT COUNT(*) FROM items WHERE registered_at >= ?", (today,)
            ).fetchone()[0]
            expired = c.execute(
                "SELECT COUNT(*) FROM items WHERE status='stored' AND deadline < ?",
                (_now(),),
            ).fetchone()[0]
        return {
            "stored": stored,
            "retrieved": retrieved,
            "disposed": disposed,
            "registered_today": today_cnt,
            "expired": expired,
        }
