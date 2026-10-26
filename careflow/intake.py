"""Admission intake: handwritten papers brought at admission (previous OPD notes, lab and scan reports)
are read by the Record Reader, then a DOCTOR must verify them before the admission is complete.

- Every field that is uncertain (AI confidence not high, a "?" diagnosis, missing page reference) or
  safety-critical (name, blood group, allergies, diagnoses, medicines, lab results, obstetric dates)
  must be confirmed, corrected, or rejected ("not on the paper") by a doctor.
- The doctor fills an admission checklist (allergies, blood group, G/P, gestation, diagnosis). The AI's
  reading is only offered as a suggestion, labelled as such.
- Anything AI-read that is not verified keeps a caveat everywhere it appears (record, chatbot, drafts).
"""
from __future__ import annotations

import json
import sqlite3

from . import extract
from .db import audit, now

CRITICAL_SECTIONS = {"allergies", "diagnoses", "medications", "investigations"}
CRITICAL_KEYS = {"patient.name", "patient.blood_group", "obstetric.gravida_para", "obstetric.gestational_age",
                 "obstetric.lmp", "obstetric.edd"}
BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
REVIEW_STATES = {"confirmed": "Confirmed", "corrected": "Corrected", "rejected": "Not on the paper (rejected)"}
CAVEAT = ("Read by AI from a handwritten paper – the reading may not be accurate. "
          "Check against the original before acting on it.")


class IntakeError(ValueError):
    pass


def _fields(data: dict):
    for k, f in data["patient"].items():
        yield f"patient.{k}", f"Patient · {k.replace('_', ' ')}", f
    for k, f in data["obstetric"].items():
        yield f"obstetric.{k}", f"Obstetric · {k.replace('_', ' ')}", f
    for sec in extract.FIELD_LISTS:
        for i, f in enumerate(data[sec]):
            yield f"{sec}.{i}", sec.replace("_", " ").capitalize(), f
    yield "clinician", "Clinician", data.get("clinician")


def items(conn: sqlite3.Connection, pid: int) -> list[dict]:
    """All AI-read fields for a patient (original readings only; a re-upload of the same file is not listed twice)."""
    out = []
    for x in conn.execute("SELECT e.*, d.filename, d.doc_type FROM extractions e JOIN documents d ON d.id=e.document_id "
                          "WHERE e.patient_id=? AND e.cached_from IS NULL ORDER BY e.id", (pid,)):
        data = json.loads(x["data"])
        reviews = {r["field_key"]: dict(r) for r in conn.execute(
            "SELECT * FROM field_reviews WHERE extraction_id=? ORDER BY id", (x["id"],))}
        for key, label, f in _fields(data):
            if not f:
                continue
            section = key.split(".")[0]
            critical = key in CRITICAL_KEYS or section in CRITICAL_SECTIONS
            rv = reviews.get(key)
            out.append({"extraction_id": x["id"], "document_id": x["document_id"], "filename": x["filename"],
                        "doc_type": x["doc_type"], "key": key, "section": section, "label": label,
                        "value": f["value"], "verbatim": f.get("verbatim"), "page": f.get("page"),
                        "confidence": f.get("confidence"), "certainty": f.get("certainty"),
                        "extra": {k: f[k] for k in ("certainty", "dose", "frequency", "route", "status", "result", "date") if f.get(k)},
                        "needs_review": bool(f.get("needs_review")), "critical": critical,
                        "must_resolve": bool(f.get("needs_review")) or critical,
                        "review": rv, "final_value": (rv["corrected_value"] if rv and rv["corrected_value"] else f["value"]),
                        "cite": f"D{x['document_id']}.p{f.get('page') or '?'}"})
    return out


def status(conn: sqlite3.Connection, pid: int) -> dict:
    its = items(conn, pid)
    unresolved = [i for i in its if i["must_resolve"] and not i["review"]]
    unverified = [i for i in its if not i["review"]]
    return {"total": len(its), "unresolved": len(unresolved), "unverified": len(unverified),
            "verified": sum(1 for i in its if i["review"] and i["review"]["status"] != "rejected"),
            "rejected": sum(1 for i in its if i["review"] and i["review"]["status"] == "rejected")}


def suggestions(conn: sqlite3.Connection, pid: int) -> dict:
    """AI readings offered to pre-fill the admission checklist (never applied without the doctor saving)."""
    s: dict[str, list[str]] = {}
    for i in items(conn, pid):
        if i["review"] and i["review"]["status"] == "rejected":
            continue
        tag = "" if i["review"] else " (AI reading – unverified)"
        target = {"patient.blood_group": "blood_group", "obstetric.gravida_para": "gravida_para",
                  "obstetric.gestational_age": "gestational_age", "obstetric.edd": "edd"}.get(i["key"])
        if i["section"] == "allergies":
            target = "allergies"
        elif i["section"] == "diagnoses":
            target = "admitting_diagnosis"
        if target:
            s.setdefault(target, []).append(i["final_value"] + tag)
    return {k: "; ".join(v) for k, v in s.items()}


def review(conn: sqlite3.Connection, pid: int, extraction_id: int, key: str, state: str, corrected: str, reviewer: str) -> None:
    if state not in REVIEW_STATES:
        raise IntakeError("Unknown review action.")
    x = conn.execute("SELECT * FROM extractions WHERE id=? AND patient_id=?", (extraction_id, pid)).fetchone()
    if not x or key not in {k for k, _, f in _fields(json.loads(x["data"])) if f}:
        raise IntakeError("That field does not exist for this patient.")
    corrected = (corrected or "").strip()[:300]
    if state == "corrected" and not corrected:
        raise IntakeError("Type the correct value to save a correction.")
    conn.execute("DELETE FROM field_reviews WHERE extraction_id=? AND field_key=?", (extraction_id, key))
    conn.execute("INSERT INTO field_reviews (extraction_id, field_key, status, corrected_value, reviewer, reviewed_at) "
                 "VALUES (?,?,?,?,?,?)", (extraction_id, key, state, corrected if state == "corrected" else None, reviewer, now()))
    audit(conn, reviewer, "field_reviewed", {"patient_id": pid, "extraction_id": extraction_id, "field": key, "state": state})
    conn.commit()


def complete(conn: sqlite3.Connection, pid: int, form: dict, doctor: str) -> None:
    p = conn.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    if p["status"] != "intake":
        raise IntakeError("Admission is already complete.")
    st = status(conn, pid)
    if st["unresolved"]:
        raise IntakeError(f"{st['unresolved']} AI-read item(s) still need a doctor's decision (confirm, correct or reject).")
    vals = {k: (form.get(k) or "").strip()[:300] for k in
            ("allergies", "blood_group", "gravida_para", "gestational_age", "edd", "admitting_diagnosis", "email", "phone")}
    missing = [lab for k, lab in (("allergies", "allergies (write NKDA if none)"), ("blood_group", "blood group"),
                                  ("gravida_para", "gravida/para"), ("gestational_age", "gestational age"),
                                  ("admitting_diagnosis", "admitting diagnosis")) if not vals[k]]
    if missing:
        raise IntakeError("Please fill: " + ", ".join(missing) + ".")
    if vals["blood_group"] not in BLOOD_GROUPS:
        raise IntakeError("Choose a blood group from the list.")
    if vals["email"] and ("@" not in vals["email"] or " " in vals["email"]):
        raise IntakeError("Email address looks wrong.")
    conn.execute("UPDATE patients SET allergies=?, blood_group=?, gravida_para=?, gestational_age=?, edd=?, admitting_diagnosis=?, "
                 "email=?, phone=?, status='admitted', intake_completed_by=?, intake_completed_at=? WHERE id=?",
                 (vals["allergies"], vals["blood_group"], vals["gravida_para"], vals["gestational_age"], vals["edd"] or None,
                  vals["admitting_diagnosis"], vals["email"] or None, vals["phone"] or None, doctor, now(), pid))
    audit(conn, doctor, "admission_completed", {"patient_id": pid, "verified_items": st["verified"], "rejected_items": st["rejected"]})
    conn.commit()


def new_admission(conn: sqlite3.Connection, form: dict, actor: str) -> int:
    name = (form.get("name") or "").strip()[:80]
    if len(name) < 3:
        raise IntakeError("Enter the patient's full name.")
    try:
        age = int(form.get("age") or 0)
    except ValueError:
        age = 0
    if not 12 <= age <= 60:
        raise IntakeError("Age must be between 12 and 60.")
    n = conn.execute("SELECT COUNT(*) FROM patients WHERE kind='mother'").fetchone()[0] + 1001
    while conn.execute("SELECT 1 FROM patients WHERE uhid=?", (f"CF-{n}",)).fetchone():
        n += 1
    cur = conn.execute("INSERT INTO patients (kind, name, age, sex, uhid, bed, room_type, consultant, admitted_at, admitting_diagnosis, status, "
                       "email, phone) VALUES ('mother',?,?,'F',?,?,?,?,?,?,'intake',?,?)",
                       (name, age, f"CF-{n}", (form.get("bed") or "").strip()[:10] or "—", "private" if form.get("room_type") == "private" else "general", (form.get("consultant") or "").strip()[:60] or "—",
                        now(), (form.get("reason") or "").strip()[:200] or "Admission – papers to be verified",
                        (form.get("email") or "").strip()[:120] or None, (form.get("phone") or "").strip()[:30] or None))
    audit(conn, actor, "admission_started", {"patient_id": cur.lastrowid})
    conn.commit()
    return cur.lastrowid
