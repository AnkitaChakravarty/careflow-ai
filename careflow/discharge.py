"""Discharge: deterministic checks, final bill, and draft reminders. No AI decides anything here.

A mother (and her babies) can be discharged only when:
- admission papers were verified (admission complete),
- the mother's discharge summary - and each baby's - has been approved by a doctor,
- there are no pending RED alerts for the mother or her babies,
- each baby's birth doses are given, or the doctor records why they were deferred.
Then the family bill is frozen and email reminders are DRAFTED (sent only after consultant authorisation)."""
from __future__ import annotations

import json
import sqlite3

from . import events, record, reminders
from .db import audit, now


class DischargeError(ValueError):
    pass


def blockers(conn: sqlite3.Connection, mother_id: int) -> list[str]:
    m = conn.execute("SELECT * FROM patients WHERE id=?", (mother_id,)).fetchone()
    out = []
    if m["status"] == "intake":
        out.append("Admission papers have not been verified (admission incomplete).")
    babies = conn.execute("SELECT * FROM patients WHERE mother_id=? AND status<>'discharged'", (mother_id,)).fetchall()
    for p in [m, *babies]:
        kind = "discharge" if p["kind"] == "mother" else "baby_discharge"
        if not conn.execute("SELECT 1 FROM drafts WHERE patient_id=? AND kind=? AND status='approved'", (p["id"], kind)).fetchone():
            out.append(f"Approved discharge summary missing for {p['name']}.")
        red = conn.execute("SELECT COUNT(*) FROM alerts WHERE patient_id=? AND status='pending' AND level='red'", (p["id"],)).fetchone()[0]
        if red:
            out.append(f"{red} pending RED alert(s) for {p['name']} – a consultant must review them first.")
    return out


def missing_birth_doses(conn: sqlite3.Connection, mother_id: int) -> list[str]:
    out = []
    for b in conn.execute("SELECT * FROM patients WHERE mother_id=? AND status<>'discharged'", (mother_id,)):
        for d in record.vaccinations(conn, b["id"])["birth_doses"]:
            if d["status"] != "given":
                out.append(f"{d['name']} ({b['name']})")
    return out


def finalize(conn: sqlite3.Connection, mother_id: int, doctor: str, *, follow_up_days: int | None,
             follow_up_note: str, deferral_note: str = "", email: str = "") -> dict:
    m = conn.execute("SELECT * FROM patients WHERE id=?", (mother_id,)).fetchone()
    if not m or m["kind"] != "mother":
        raise DischargeError("Discharge is done from the mother's record.")
    if m["status"] == "discharged":
        raise DischargeError("Already discharged.")
    if b := blockers(conn, mother_id):
        raise DischargeError("Cannot discharge yet: " + " ".join(b))
    missing = missing_birth_doses(conn, mother_id)
    if missing and len(deferral_note.strip()) < 10:
        raise DischargeError("Birth doses not given: " + ", ".join(missing) + ". Give them, or record why they are deferred.")
    if follow_up_days is not None and not 1 <= follow_up_days <= 120:
        raise DischargeError("Follow-up must be 1–120 days after discharge.")
    email = (email or "").strip()
    if email:
        if "@" not in email or " " in email:
            raise DischargeError("Email address looks wrong.")
        conn.execute("UPDATE patients SET email=? WHERE id=? OR mother_id=?", (email, mother_id, mother_id))
    ts = now()
    fam = events.family_bill(conn, mother_id, until=ts)
    ids = [mother_id] + [r[0] for r in conn.execute("SELECT id FROM patients WHERE mother_id=? AND status<>'discharged'", (mother_id,))]
    for pid in ids:
        conn.execute("UPDATE patients SET status='discharged', discharged_at=?, discharged_by=? WHERE id=?", (ts, doctor, pid))
    conn.execute("INSERT INTO final_bills (patient_id, total, data, finalized_by, finalized_at) VALUES (?,?,?,?,?)",
                 (mother_id, fam["total"], json.dumps(fam), doctor, ts))
    if missing:
        conn.execute("INSERT INTO events (patient_id, ts, type, data, source, entered_by, created_at) VALUES (?,?,?,?,?,?,?)",
                     (mother_id, ts, "note", json.dumps({"text": f"Birth doses deferred at discharge: {', '.join(missing)}. "
                                                                 f"Reason: {deferral_note.strip()[:300]}", "category": "discharge_advice"}),
                      "bedside", doctor, ts))
    audit(conn, doctor, "discharged", {"mother_id": mother_id, "patients": ids, "bill_total": fam["total"]})
    conn.commit()
    n = reminders.draft_for_discharge(conn, mother_id, follow_up_days, follow_up_note, doctor)
    return {"bill_total": fam["total"], "reminders_drafted": n, "patients": ids}


def record_payment(conn: sqlite3.Connection, mother_id: int, mode: str, by: str) -> None:
    if mode not in ("cash", "upi", "card", "insurance", "scheme"):
        raise DischargeError("Choose a payment mode.")
    r = conn.execute("SELECT * FROM final_bills WHERE patient_id=?", (mother_id,)).fetchone()
    if not r:
        raise DischargeError("The bill is final only after discharge.")
    if r["paid_at"]:
        raise DischargeError("Payment already recorded.")
    conn.execute("UPDATE final_bills SET paid_at=?, payment_mode=?, received_by=? WHERE patient_id=?", (now(), mode, by, mother_id))
    audit(conn, by, "payment_recorded", {"mother_id": mother_id, "mode": mode, "total": r["total"]})
    conn.commit()
