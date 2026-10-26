"""SQLite storage. Everything that matters (events, alerts, charges, stock, logins) is a row
with a timestamp and an author, so the record is auditable."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import config

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'mother', mother_id INTEGER,
    name TEXT NOT NULL, age INTEGER, sex TEXT, uhid TEXT UNIQUE, bed TEXT, room_type TEXT DEFAULT 'general', consultant TEXT,
    admitted_at TEXT, admitting_diagnosis TEXT, status TEXT DEFAULT 'intake',
    allergies TEXT, blood_group TEXT, gravida_para TEXT, gestational_age TEXT, edd TEXT,
    email TEXT, phone TEXT,
    birth_at TEXT, birth_weight_g INTEGER,
    intake_completed_by TEXT, intake_completed_at TEXT, discharged_at TEXT, discharged_by TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, filename TEXT, path TEXT, doc_type TEXT,
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
    category TEXT, level TEXT, score TEXT, reasons TEXT, source TEXT, status TEXT DEFAULT 'pending',
    acknowledged_by TEXT, acknowledged_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tariff (code TEXT PRIMARY KEY, name TEXT, price REAL, unit TEXT);
CREATE TABLE IF NOT EXISTS inventory (
    item_code TEXT PRIMARY KEY, name TEXT, unit TEXT, unit_price REAL, stock INTEGER,
    reorder_level INTEGER, category TEXT DEFAULT 'medicine', drug_class TEXT
);
CREATE TABLE IF NOT EXISTS stock_ledger (
    id INTEGER PRIMARY KEY, item_code TEXT, delta INTEGER, event_id INTEGER UNIQUE,
    ts TEXT, reason TEXT, by_user TEXT
);
CREATE TABLE IF NOT EXISTS charges (
    id INTEGER PRIMARY KEY, patient_id INTEGER, event_id INTEGER, code TEXT, description TEXT,
    qty REAL, unit_price REAL, amount REAL, ts TEXT,
    UNIQUE(event_id, code)
);
CREATE TABLE IF NOT EXISTS final_bills (
    id INTEGER PRIMARY KEY, patient_id INTEGER UNIQUE, total REAL, data TEXT,
    finalized_by TEXT, finalized_at TEXT, paid_at TEXT, payment_mode TEXT, received_by TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY, patient_id INTEGER, kind TEXT, content TEXT, grounding TEXT, appendix TEXT,
    status TEXT DEFAULT 'draft', approved_by TEXT, approved_at TEXT, created_at TEXT, created_by TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY, patient_id INTEGER, kind TEXT, due_date TEXT, to_email TEXT,
    subject TEXT, body TEXT, status TEXT DEFAULT 'draft', dedupe_key TEXT UNIQUE,
    created_at TEXT, created_by TEXT, authorised_by TEXT, authorised_at TEXT,
    sent_at TEXT, sent_via TEXT, cancelled_by TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL, role TEXT NOT NULL,
    pw_hash TEXT NOT NULL, pw_salt TEXT NOT NULL, pw_iterations INTEGER NOT NULL,
    active INTEGER DEFAULT 1, must_change_pw INTEGER DEFAULT 0, is_demo INTEGER DEFAULT 0,
    failed_attempts INTEGER DEFAULT 0, locked_until TEXT, created_at TEXT, last_login TEXT, pw_changed_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, csrf TEXT NOT NULL,
    created_at TEXT, last_seen TEXT, ip TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY, user_id INTEGER, ts TEXT, role TEXT, content TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY, ts TEXT, feature TEXT, patient_id INTEGER, user_id INTEGER, model TEXT,
    input_tokens INTEGER, cached_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
    latency_s REAL, ok INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT, detail TEXT
);
"""

FMT = "%Y-%m-%d %H:%M:%S"


def now() -> str:
    return datetime.now().strftime(FMT)


def parse_ts(ts: str) -> datetime:
    ts = ts.strip().replace("T", " ")
    for fmt, n in ((FMT, 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(ts[:n], fmt)
        except ValueError:
            continue
    raise ValueError(f"Bad timestamp {ts!r}")


def hours_since(ts: str, until: str | None = None) -> float:
    end = parse_ts(until) if until else datetime.now()
    return (end - parse_ts(ts)).total_seconds() / 3600


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _retire_old_schema(path)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _retire_old_schema(path: Path) -> None:
    """A database from an older version of the app is renamed (not deleted) and rebuilt from seed."""
    c = sqlite3.connect(str(path))
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if "meta" in tables else None
    finally:
        c.close()
    if tables and (not version or int(version[0]) != SCHEMA_VERSION):
        path.rename(path.with_name(f"{path.stem}.old-{datetime.now():%Y%m%d%H%M%S}.db"))


def init(conn: sqlite3.Connection, seed: bool = True) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    if seed and conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0:
        load_seed(conn)
    conn.commit()


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: dict | str) -> None:
    conn.execute(
        "INSERT INTO audit (ts, actor, action, detail) VALUES (?,?,?,?)",
        (now(), actor, action, detail if isinstance(detail, str) else json.dumps(detail)),
    )


def rel(base: datetime, hours: float) -> str:
    """Seed times are written as hours relative to first run, so the demo ward always looks current."""
    return (base + timedelta(hours=hours)).strftime(FMT)


def load_seed(conn: sqlite3.Connection) -> None:
    """Load the synthetic test dataset (no real patients, staff or hospital)."""
    from . import auth
    from . import events as ev  # local import avoids a cycle

    seed = json.loads((config.DATA / "seed.json").read_text(encoding="utf-8"))
    base = datetime.now().replace(second=0, microsecond=0)
    for t in seed["tariff"]:
        conn.execute("INSERT INTO tariff VALUES (?,?,?,?)", (t["code"], t["name"], t["price"], t["unit"]))
    for i in seed["inventory"]:
        conn.execute("INSERT INTO inventory VALUES (?,?,?,?,?,?,?,?)",
                     (i["item_code"], i["name"], i["unit"], i["unit_price"], i["stock"], i["reorder_level"],
                      i.get("category", "medicine"), i.get("drug_class")))
    for u in seed["users"]:
        auth.create_user(conn, u["username"], u["full_name"], u["role"], u["demo_password"],
                         actor="seed", is_demo=True, must_change=False, check_policy=False)
    for p in seed["patients"]:
        done = p["status"] == "admitted"
        cur = conn.execute(
            "INSERT INTO patients (kind, name, age, sex, uhid, bed, consultant, admitted_at, admitting_diagnosis, status, "
            "allergies, blood_group, gravida_para, gestational_age, edd, email, phone, intake_completed_by, intake_completed_at) "
            "VALUES ('mother',?,?,'F',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["name"], p["age"], p["uhid"], p["bed"], p["consultant"], rel(base, p["admitted_h"]),
             p["admitting_diagnosis"], p["status"], p.get("allergies"), p.get("blood_group"), p.get("gravida_para"),
             p.get("gestational_age"), p.get("edd"), p.get("email"), p.get("phone"),
             "Seed data (admission papers verified)" if done else None, rel(base, p["admitted_h"]) if done else None))
        pid = cur.lastrowid
        if p.get("room_type") == "private":
            conn.execute("UPDATE patients SET room_type='private' WHERE id=?", (pid,))
        for n, e in enumerate(p.get("events", [])):
            r = ev.record_event(conn, pid, e["type"], e["data"], ts=rel(base, e["h"]), source="seed",
                                entered_by=e.get("by", "seed"), key=f"seed-{p['uhid']}-{n}", actor_role="consultant")
            for m, be in enumerate(e.get("baby_events", [])):
                ev.record_event(conn, r["baby_id"], be["type"], be["data"], ts=rel(base, be["h"]), source="seed",
                                entered_by=be.get("by", "seed"), key=f"seed-{p['uhid']}-{n}-b{m}", actor_role="consultant")
        for n in p.get("acknowledge_alerts", []):
            row = conn.execute("SELECT id FROM alerts WHERE patient_id=? ORDER BY id LIMIT 1 OFFSET ?", (pid, n)).fetchone()
            if row:
                ev.acknowledge_alert(conn, row["id"], f"{p['consultant']} · consultant")
    audit(conn, "system", "seed_loaded", {"patients": len(seed["patients"]), "users": len(seed["users"])})
