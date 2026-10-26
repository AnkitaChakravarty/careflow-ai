"""The closed loop. Every bedside entry is an event; deterministic code then
(1) checks red-flag rules, (2) posts charges, (3) moves stock.
The AI never calls any of these functions - it has no write access."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime

from . import rules
from .db import audit, now

EVENT_TYPES = {"vitals", "med_admin", "service", "note", "investigation"}


class EventError(ValueError):
    pass


def record_event(conn: sqlite3.Connection, patient_id: int, etype: str, data: dict, *, ts: str | None = None,
                 source: str = "bedside", entered_by: str = "staff", key: str | None = None) -> dict:
    """Insert one event and run its side effects inside a single transaction.
    `key` is an idempotency key: submitting the same form twice does nothing the second time."""
    if etype not in EVENT_TYPES:
        raise EventError(f"Unknown event type {etype!r}")
    ts = ts or now()
    if key:
        row = conn.execute("SELECT id FROM events WHERE idempotency_key=?", (key,)).fetchone()
        if row:
            return {"event_id": row["id"], "duplicate": True, "alert_id": None}

    if etype == "med_admin":
        item = conn.execute("SELECT * FROM inventory WHERE item_code=?", (data.get("item_code"),)).fetchone()
        if not item:
            raise EventError("Medicine not in inventory list - choose an item from the list.")
        qty = int(data.get("qty") or 0)
        if qty <= 0:
            raise EventError("Quantity must be a positive whole number.")
        if item["stock"] < qty:
            raise EventError(f"Only {item['stock']} {item['unit']} of {item['name']} in stock.")
        data = {**data, "name": item["name"], "unit": item["unit"], "qty": qty}
    if etype == "service":
        t = conn.execute("SELECT * FROM tariff WHERE code=?", (data.get("code"),)).fetchone()
        if not t:
            raise EventError("Service code not in tariff.")
        data = {**data, "name": t["name"], "qty": float(data.get("qty") or 1)}

    cur = conn.execute(
        "INSERT INTO events (patient_id, ts, type, data, source, entered_by, idempotency_key, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (patient_id, ts, etype, json.dumps(data), source, entered_by, key, now()),
    )
    eid = cur.lastrowid
    alert_id = None

    if etype == "vitals":
        result = rules.evaluate(data)
        if result["trigger"]:
            note = "Values read by AI from a document - verify against the original." if source == "document" else ""
            c = conn.execute(
                "INSERT INTO alerts (patient_id, event_id, ts, level, score, reasons, source, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (patient_id, eid, ts, result["level"], result["score"], json.dumps(result["reasons"] + ([note] if note else [])),
                 source, "pending", now()),
            )
            alert_id = c.lastrowid

    if etype == "med_admin":
        conn.execute("UPDATE inventory SET stock = stock - ? WHERE item_code=?", (data["qty"], data["item_code"]))
        conn.execute("INSERT INTO stock_ledger (item_code, delta, event_id, ts, reason) VALUES (?,?,?,?,?)",
                     (data["item_code"], -data["qty"], eid, ts, f"Administered to patient {patient_id}"))
        price = conn.execute("SELECT unit_price FROM inventory WHERE item_code=?", (data["item_code"],)).fetchone()[0]
        conn.execute("INSERT INTO charges (patient_id, event_id, code, description, qty, unit_price, amount, ts) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (patient_id, eid, data["item_code"], data["name"], data["qty"], price, price * data["qty"], ts))

    if etype == "service":
        t = conn.execute("SELECT * FROM tariff WHERE code=?", (data["code"],)).fetchone()
        conn.execute("INSERT INTO charges (patient_id, event_id, code, description, qty, unit_price, amount, ts) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (patient_id, eid, t["code"], t["name"], data["qty"], t["price"], t["price"] * data["qty"], ts))

    audit(conn, entered_by, f"event_{etype}", {"patient_id": patient_id, "event_id": eid, "source": source})
    conn.commit()
    return {"event_id": eid, "duplicate": False, "alert_id": alert_id}


def acknowledge_alert(conn: sqlite3.Connection, alert_id: int, consultant: str) -> None:
    conn.execute("UPDATE alerts SET status='acknowledged', acknowledged_by=?, acknowledged_at=? WHERE id=? AND status='pending'",
                 (consultant, now(), alert_id))
    audit(conn, consultant, "alert_acknowledged", {"alert_id": alert_id})
    conn.commit()


def room_days(admitted_at: str, until: str | None = None) -> int:
    start = datetime.strptime(admitted_at[:16], "%Y-%m-%d %H:%M")
    end = datetime.strptime((until or now())[:16], "%Y-%m-%d %H:%M")
    return max(1, math.ceil((end - start).total_seconds() / 86400))


def bill(conn: sqlite3.Connection, patient_id: int, until: str | None = None) -> dict:
    """Deterministic bill: room charges by day + every charge row posted by an event."""
    p = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    room = conn.execute("SELECT * FROM tariff WHERE code='ROOM_GEN'").fetchone()
    days = room_days(p["admitted_at"], until)
    lines = [{"code": room["code"], "description": f"{room['name']} ({days} day{'s' if days > 1 else ''})",
              "qty": days, "unit_price": room["price"], "amount": room["price"] * days, "event_id": None, "ts": p["admitted_at"]}]
    for r in conn.execute("SELECT * FROM charges WHERE patient_id=? ORDER BY ts", (patient_id,)):
        lines.append({k: r[k] for k in ("code", "description", "qty", "unit_price", "amount", "event_id", "ts")})
    total = round(sum(l["amount"] for l in lines), 2)
    return {"lines": lines, "total": total, "currency": "INR"}
