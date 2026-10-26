"""Read-only views of the ward record, each item tagged with a citation ID:
  E12  = ward event #12 (observation, labour entry, medicine, vaccine, lab result, note, delivery)
  P5   = patient #5's doctor-confirmed record (demographics, allergies, blood group)
  A3   = alert #3          D2.p1 = scanned admission paper #2, page 1 (read by AI)
These functions are the only tools the AI agents get. All are read-only; none can change anything.
Citation IDs are unique across the whole ward, so answers about several patients stay checkable."""
from __future__ import annotations

import json
import sqlite3

from . import events, intake, vaccines
from .db import hours_since


def _p(conn, pid):
    p = conn.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    if not p:
        raise KeyError(f"No patient with id {pid}")
    return p


def phase(conn: sqlite3.Connection, p) -> str:
    if p["status"] == "discharged":
        return "discharged"
    if p["kind"] == "baby":
        return "newborn"
    if p["status"] == "intake":
        return "admission – papers being verified"
    if conn.execute("SELECT 1 FROM events WHERE patient_id=? AND type='delivery'", (p["id"],)).fetchone():
        return "postnatal"
    if conn.execute("SELECT 1 FROM events WHERE patient_id=? AND type='labour'", (p["id"],)).fetchone():
        return "in labour"
    return "antenatal"


def list_patients(conn: sqlite3.Connection, include_discharged: bool = False) -> list[dict]:
    q = "SELECT * FROM patients" + ("" if include_discharged else " WHERE status<>'discharged'") + " ORDER BY COALESCE(mother_id, id), id"
    out = []
    for p in conn.execute(q):
        pend = conn.execute("SELECT COUNT(*) FROM alerts WHERE patient_id=? AND status='pending'", (p["id"],)).fetchone()[0]
        out.append({"cite": f"P{p['id']}", "patient_id": p["id"], "uhid": p["uhid"], "name": p["name"], "type": p["kind"],
                    "mother_patient_id": p["mother_id"], "bed": p["bed"], "consultant": p["consultant"],
                    "phase": phase(conn, p), "pending_alerts": pend})
    return out


def overview(conn: sqlite3.Connection, pid: int) -> dict:
    p = _p(conn, pid)
    d = {"cite": f"P{p['id']}", "patient_id": p["id"], "type": p["kind"], "name": p["name"], "uhid": p["uhid"], "bed": p["bed"],
         "consultant": p["consultant"], "admitted_at": p["admitted_at"], "admitting_diagnosis": p["admitting_diagnosis"],
         "phase": phase(conn, p), "status": p["status"],
         "allergies_doctor_confirmed": p["allergies"] or ("not yet confirmed – admission papers being verified" if p["status"] == "intake" else "not recorded")}
    if p["kind"] == "mother":
        d.update({"age": p["age"], "blood_group": p["blood_group"], "gravida_para": p["gravida_para"],
                  "gestational_age_at_admission": p["gestational_age"], "edd": p["edd"],
                  "admission_verified_by": p["intake_completed_by"],
                  "babies": [{"patient_id": b["id"], "name": b["name"]} for b in
                             conn.execute("SELECT id, name FROM patients WHERE mother_id=?", (pid,))]})
        st = intake.status(conn, pid)
        if st["total"]:
            d["ai_read_admission_papers"] = {**st, "caveat": intake.CAVEAT}
    else:
        d.update({"sex": p["sex"], "born_at": p["birth_at"], "age_hours": round(hours_since(p["birth_at"]), 1),
                  "birth_weight_g": p["birth_weight_g"], "mother_patient_id": p["mother_id"]})
    if p["status"] == "discharged":
        d.update({"discharged_at": p["discharged_at"], "discharged_by": p["discharged_by"]})
    return d


def document_findings(conn: sqlite3.Connection, pid: int, section: str = "all") -> list[dict]:
    out = []
    for i in intake.items(conn, pid):
        if section not in ("all", i["section"]):
            continue
        rv = i["review"]
        if rv and rv["status"] == "rejected":
            continue  # a doctor marked this reading as not on the paper
        item = {"cite": i["cite"], "section": i["section"], "value": i["final_value"], **i["extra"],
                "verification": (f"{rv['status']} by {rv['reviewer']}" if rv else "UNVERIFIED AI reading – may be inaccurate")}
        out.append(item)
    return out


def _events(conn, pid, types, last_n=None):
    q = f"SELECT * FROM events WHERE patient_id=? AND type IN ({','.join('?' * len(types))}) ORDER BY ts DESC, id DESC"
    rows = conn.execute(q + (" LIMIT ?" if last_n else ""), (pid, *types, *([int(last_n)] if last_n else []))).fetchall()
    out = []
    for r in reversed(rows):
        d = {k: v for k, v in json.loads(r["data"]).items() if k != "verbatim"}
        item = {"cite": f"E{r['id']}", "ts": r["ts"], "type": r["type"], "by": r["entered_by"], **d}
        if r["source"] == "document":
            item["source"] = "read by AI from a handwritten paper – verify"
        out.append(item)
    return out


def vitals(conn: sqlite3.Connection, pid: int, last_n: int = 12) -> list[dict]:
    return _events(conn, pid, ("vitals", "newborn_obs"), max(1, min(50, last_n)))


def labour(conn: sqlite3.Connection, pid: int) -> list[dict]:
    return _events(conn, pid, ("labour",))


def delivery(conn: sqlite3.Connection, pid: int) -> list[dict]:
    out = _events(conn, pid, ("delivery",))
    for d in out:
        d["mode_text"] = events.DELIVERY_MODES.get(d.get("mode"), d.get("mode"))
        d["outcome_text"] = events.OUTCOMES.get(d.get("outcome"), d.get("outcome"))
    return out


def vaccinations(conn: sqlite3.Connection, pid: int) -> dict:
    p = _p(conn, pid)
    if p["kind"] != "baby":
        return {"note": "Vaccinations are recorded on the baby's record."}
    given = {json.loads(r["data"])["code"]: r["ts"] for r in conn.execute(
        "SELECT * FROM events WHERE patient_id=? AND type='vaccine'", (pid,))}
    return {"cite": f"P{pid}", "baby": p["name"], "birth_doses": vaccines.birth_dose_status(p["birth_at"], given),
            "given_events": _events(conn, pid, ("vaccine",)),
            "next_visits_iap": [{"visit": v["visit"], "due_date": v["due_date"], "vaccines": [x["name"] for x in v["vaccines"]]}
                                for v in vaccines.upcoming_visits(p["birth_at"])],
            "schedule_source": vaccines.SOURCE_NOTE}


def due_tasks(conn: sqlite3.Connection) -> list[dict]:
    """Everything due on the ward right now, computed by code (the same list as the dashboard's "Needs attention").
    One deterministic call instead of the agent looping over every patient - cheaper and nothing gets missed."""
    out = []
    for b in conn.execute("SELECT * FROM patients WHERE kind='baby' AND status='admitted' ORDER BY id"):
        given = {json.loads(r["data"])["code"]: r["ts"] for r in conn.execute(
            "SELECT * FROM events WHERE patient_id=? AND type='vaccine'", (b["id"],))}
        for d in vaccines.birth_dose_status(b["birth_at"], given):
            if d["status"] != "given":
                out.append({"cite": f"P{b['id']}", "patient_id": b["id"], "patient": b["name"], "task": f"Give {d['name']}",
                            "status": d["status"], "deadline": f"within {d['deadline_h']} h of birth"})
    for p in conn.execute("SELECT * FROM patients WHERE kind='mother' AND status<>'discharged' ORDER BY id"):
        if p["status"] == "intake":
            st = intake.status(conn, p["id"])
            out.append({"cite": f"P{p['id']}", "patient_id": p["id"], "patient": p["name"], "status": "due",
                        "task": f"Verify admission papers ({st['unresolved']} AI-read items need a doctor) and complete the admission checklist"})
        last = conn.execute("SELECT id, ts FROM events WHERE patient_id=? AND type='labour' ORDER BY ts DESC LIMIT 1", (p["id"],)).fetchone()
        delivered = conn.execute("SELECT 1 FROM events WHERE patient_id=? AND type='delivery'", (p["id"],)).fetchone()
        if last and not delivered and hours_since(last["ts"]) > 0.5:
            out.append({"cite": f"E{last['id']}", "patient_id": p["id"], "patient": p["name"], "status": "overdue",
                        "task": f"Labour check (FHR every 30 min) – last entry {hours_since(last['ts']) * 60:.0f} min ago"})
    for a in alerts(conn, None, pending_only=True):
        out.append({"cite": a["cite"], "patient_id": a["patient_id"], "patient": a["patient"], "status": "pending",
                    "task": f"Consultant review of {a['level'].upper()} {a['category']} alert: {a['reasons'][0]}"})
    return out


def medications_given(conn: sqlite3.Connection, pid: int) -> list[dict]:
    return _events(conn, pid, ("med_admin", "vaccine"))


def notes_and_services(conn: sqlite3.Connection, pid: int) -> list[dict]:
    return _events(conn, pid, ("note", "service", "investigation"))


def alerts(conn: sqlite3.Connection, pid: int | None = None, pending_only: bool = False) -> list[dict]:
    q, args = "SELECT a.*, p.name FROM alerts a JOIN patients p ON p.id=a.patient_id WHERE 1=1", []
    if pid is not None:
        q += " AND a.patient_id=?"
        args.append(pid)
    if pending_only:
        q += " AND a.status='pending'"
    return [{"cite": f"A{r['id']}", "patient_id": r["patient_id"], "patient": r["name"], "ts": r["ts"],
             "category": r["category"], "level": r["level"], "reasons": json.loads(r["reasons"]), "status": r["status"],
             "acknowledged_by": r["acknowledged_by"], "trigger_event": f"E{r['event_id']}"}
            for r in conn.execute(q + " ORDER BY a.ts", args)]


def bill_summary(conn: sqlite3.Connection, pid: int) -> dict:
    b = events.bill(conn, pid)
    return {"total_inr": b["total"], "lines": [{"description": l["description"], "amount_inr": l["amount"],
                                                "cite": f"E{l['event_id']}" if l["event_id"] else "room"} for l in b["lines"]]}


def full(conn: sqlite3.Connection, pid: int) -> dict:
    """Compact whole-record view used by the drafting agent."""
    p = _p(conn, pid)
    rec = {"patient": overview(conn, pid), "admission_papers_ai_read": document_findings(conn, pid),
           "observations": vitals(conn, pid, 50), "medicines_and_vaccines_given": medications_given(conn, pid),
           "notes_services_labs": notes_and_services(conn, pid), "alerts": alerts(conn, pid)}
    if p["kind"] == "mother":
        rec["labour"] = labour(conn, pid)
        rec["delivery"] = delivery(conn, pid)
        rec["babies"] = [overview(conn, b["id"]) for b in conn.execute("SELECT id FROM patients WHERE mother_id=?", (pid,))]
    else:
        rec["vaccinations"] = vaccinations(conn, pid)
        rec["mother"] = {"patient_id": p["mother_id"], "delivery": delivery(conn, p["mother_id"])}
    return rec


def valid_cites(conn: sqlite3.Connection, pid: int | None = None) -> set[str]:
    """Every citation ID that exists (for one patient, or ward-wide when pid is None)."""
    where, args = ("WHERE patient_id=?", (pid,)) if pid is not None else ("", ())
    cites = {f"E{r[0]}" for r in conn.execute(f"SELECT id FROM events {where}", args)}
    cites |= {f"P{r[0]}" for r in conn.execute(f"SELECT id FROM patients {where.replace('patient_id', 'id')}", args)}
    cites |= {f"A{r[0]}" for r in conn.execute(f"SELECT id FROM alerts {where}", args)}
    for r in conn.execute(f"SELECT document_id, data FROM extractions {where}", args):
        cites.add(f"D{r[0]}")
        cites.add(f"D{r[0]}.p?")
        d = json.loads(r[1])
        for f in [*d["patient"].values(), *d["obstetric"].values(), *[x for k in ("allergies", "presenting_complaints", "history",
                  "examination", "diagnoses", "medications", "investigations", "plan") for x in d[k]], d.get("clinician"), *d["vitals"]]:
            if f and f.get("page"):
                cites.add(f"D{r[0]}.p{f['page']}")
        for n in d.get("notes_by_date", []):
            if n.get("page"):
                cites.add(f"D{r[0]}.p{n['page']}")
    return cites
