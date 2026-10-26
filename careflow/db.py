"""SQLite storage. Everything that matters (events, alerts, charges, stock) is a row
with a timestamp and an author, so the record is auditable."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER, uhid TEXT UNIQUE,
    bed TEXT, consultant TEXT, admitted_at TEXT, admitting_diagnosis TEXT,
    status TEXT DEFAULT 'admitted'
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, filename TEXT, path TEXT,
    sha256 TEXT, pages INTEGER, uploaded_at TEXT, uploaded_by TEXT
);
CREATE TABLE IF NOT EXISTS extractions (
    id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, patient_id INTEGER NOT NULL,
    data TEXT, created_at TEXT, model TEXT, cached_from INTEGER
);
CREATE TABLE IF NOT EXISTS field_reviews (
    id INTEGER PRIMARY KEY, extraction_id INTEGER, field_key TEXT, status TEXT,
    corrected_value TEXT, reviewer TEXT, reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, ts TEXT NOT NULL, type TEXT NOT NULL,
    data TEXT NOT NULL, source TEXT NOT NULL, entered_by TEXT, idempotency_key TEXT UNIQUE,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, event_id INTEGER, ts TEXT,
    level TEXT, score TEXT, reasons TEXT, source TEXT, status TEXT DEFAULT 'pending',
    acknowledged_by TEXT, acknowledged_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tariff (
    code TEXT PRIMARY KEY, name TEXT, price REAL, unit TEXT
);
CREATE TABLE IF NOT EXISTS inventory (
    item_code TEXT PRIMARY KEY, name TEXT, unit TEXT, unit_price REAL, stock INTEGER,
    reorder_level INTEGER
);
CREATE TABLE IF NOT EXISTS stock_ledger (
    id INTEGER PRIMARY KEY, item_code TEXT, delta INTEGER, event_id INTEGER UNIQUE,
    ts TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS charges (
    id INTEGER PRIMARY KEY, patient_id INTEGER, event_id INTEGER, code TEXT, description TEXT,
    qty REAL, unit_price REAL, amount REAL, ts TEXT,
    UNIQUE(event_id, code)
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY, patient_id INTEGER, kind TEXT, content TEXT, grounding TEXT,
    status TEXT DEFAULT 'draft', approved_by TEXT, approved_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY, ts TEXT, feature TEXT, patient_id INTEGER, model TEXT,
    input_tokens INTEGER, cached_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
    latency_s REAL, ok INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT, detail TEXT
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection, seed: bool = True) -> None:
    conn.executescript(SCHEMA)
    if seed and conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0:
        load_seed(conn)
    conn.commit()


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: dict | str) -> None:
    conn.execute(
        "INSERT INTO audit (ts, actor, action, detail) VALUES (?,?,?,?)",
        (now(), actor, action, detail if isinstance(detail, str) else json.dumps(detail)),
    )


def load_seed(conn: sqlite3.Connection) -> None:
    """Load the synthetic test dataset (no real patients)."""
    from . import events as ev  # local import avoids a cycle

    seed = json.loads((config.DATA / "seed.json").read_text(encoding="utf-8"))
    for t in seed["tariff"]:
        conn.execute("INSERT INTO tariff VALUES (?,?,?,?)", (t["code"], t["name"], t["price"], t["unit"]))
    for i in seed["inventory"]:
        conn.execute(
            "INSERT INTO inventory VALUES (?,?,?,?,?,?)",
            (i["item_code"], i["name"], i["unit"], i["unit_price"], i["stock"], i["reorder_level"]),
        )
    for p in seed["patients"]:
        cur = conn.execute(
            "INSERT INTO patients (name, age, uhid, bed, consultant, admitted_at, admitting_diagnosis) "
            "VALUES (?,?,?,?,?,?,?)",
            (p["name"], p["age"], p["uhid"], p["bed"], p["consultant"], p["admitted_at"], p["admitting_diagnosis"]),
        )
        pid = cur.lastrowid
        for e in p.get("events", []):
            ev.record_event(conn, pid, e["type"], e["data"], ts=e["ts"], source="seed",
                            entered_by=e.get("entered_by", "seed"), key=f"seed-{p['uhid']}-{e['ts']}-{e['type']}")
    audit(conn, "system", "seed_loaded", {"patients": len(seed["patients"])})
