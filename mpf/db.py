"""SQLite storage. One table of items, with score columns filled in by the scorer."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from mpf.models import Item

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key TEXT PRIMARY KEY,
    source TEXT, item_id TEXT, url TEXT, title TEXT, language TEXT,
    country TEXT, province TEXT, water_body TEXT, lat REAL, lon REAL,
    proponent TEXT, project_type_raw TEXT,
    date_posted TEXT, comment_deadline TEXT, decision_date TEXT,
    status_raw TEXT, stage TEXT,
    full_text TEXT, raw_path TEXT, is_test_set INTEGER, extra TEXT,
    first_seen TEXT, last_seen TEXT,
    -- filled in by the scorer
    score INTEGER, flagged INTEGER, project_types TEXT, size_summary TEXT,
    reasons TEXT, facts TEXT, timing TEXT, days_to_deadline INTEGER, too_late INTEGER,
    -- filled in by the optional LLM step
    summary_en TEXT, llm_confidence TEXT, llm_quotes TEXT, llm_translated INTEGER, llm_model TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_score ON items(score);
"""

ITEM_COLUMNS = [
    "source", "item_id", "url", "title", "language", "country", "province", "water_body",
    "lat", "lon", "proponent", "project_type_raw", "date_posted", "comment_deadline",
    "decision_date", "status_raw", "stage", "full_text", "raw_path", "is_test_set", "extra",
]


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_item(conn: sqlite3.Connection, item: Item) -> bool:
    """Insert or update an item. Returns True if it is new."""
    now = datetime.now().isoformat(timespec="seconds")
    d = item.to_dict()
    d["extra"] = json.dumps(d["extra"], ensure_ascii=False)
    d["is_test_set"] = int(d["is_test_set"])
    exists = conn.execute("SELECT 1 FROM items WHERE key=?", (item.key,)).fetchone()
    if exists:
        sets = ", ".join(f"{c}=?" for c in ITEM_COLUMNS)
        conn.execute(f"UPDATE items SET {sets}, last_seen=? WHERE key=?",
                     [d[c] for c in ITEM_COLUMNS] + [now, item.key])
        return False
    cols = ["key"] + ITEM_COLUMNS + ["first_seen", "last_seen"]
    conn.execute(f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 [item.key] + [d[c] for c in ITEM_COLUMNS] + [now, now])
    return True
