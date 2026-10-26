"""Read-only views of ONE patient's record, each item tagged with a citation ID:
  E12  = bedside event #12        A3 = alert #3
  D2.p1 = document #2, page 1      (extracted by the Record Reader)
These functions are the only tools the AI agents get. All are read-only and
scoped to the patient chosen by the user - the model cannot pick another patient."""
from __future__ import annotations

import json
import sqlite3

from . import events, extract


def overview(conn: sqlite3.Connection, pid: int) -> dict:
    p = conn.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    return {"name": p["name"], "age": p["age"], "uhid": p["uhid"], "bed": p["bed"], "consultant": p["consultant"],
            "admitted_at": p["admitted_at"], "admitting_diagnosis": p["admitting_diagnosis"], "status": p["status"]}


def document_findings(conn: sqlite3.Connection, pid: int, section: str = "all") -> list[dict]:
    out = []
    for ext in extract.latest(conn, pid):
        did = ext["document_id"]
        d = ext["data"]
        reviews = {r["field_key"]: dict(r) for r in conn.execute(
            "SELECT * FROM field_reviews WHERE extraction_id=? ORDER BY id", (ext["id"],))}
        def add(sec, key, f):
            if section not in ("all", sec) or not f:
                return
            rv = reviews.get(key)
            item = {"cite": f"D{did}.p{f.get('page') or '?'}", "section": sec, "value": f["value"],
                    "confidence": f.get("confidence"), "needs_review": f.get("needs_review")}
            for extra in ("certainty", "dose", "frequency", "route", "status", "result", "date"):
                if f.get(extra):
                    item[extra] = f[extra]
            if rv:
                item["review"] = rv["status"]
                if rv["corrected_value"]:
                    item["value"] = rv["corrected_value"]
                    item["corrected_by"] = rv["reviewer"]
            out.append(item)
        for k, f in d["patient"].items():
            add("patient", f"patient.{k}", f)
        for k, f in d["obstetric"].items():
            add("obstetric", f"obstetric.{k}", f)
        for sec in extract.FIELD_LISTS:
            for i, f in enumerate(d[sec]):
                add(sec, f"{sec}.{i}", f)
        if section in ("all", "notes"):
            for n in d.get("notes_by_date", []):
                out.append({"cite": f"D{did}.p{n.get('page') or '?'}", "section": "notes",
                            "datetime": n.get("datetime"), "value": n.get("summary")})
    return out


def vitals(conn: sqlite3.Connection, pid: int, last_n: int = 12) -> list[dict]:
    rows = conn.execute("SELECT * FROM events WHERE patient_id=? AND type='vitals' ORDER BY ts DESC, id DESC LIMIT ?",
                        (pid, int(last_n))).fetchall()
    out = []
    for r in reversed(rows):
        d = json.loads(r["data"])
        out.append({"cite": f"E{r['id']}", "ts": r["ts"], "source": r["source"], "by": r["entered_by"],
                    **{k: v for k, v in d.items() if k not in ("verbatim",)}})
    return out


def medications_given(conn: sqlite3.Connection, pid: int) -> list[dict]:
    return [{"cite": f"E{r['id']}", "ts": r["ts"], "by": r["entered_by"], **json.loads(r["data"])}
            for r in conn.execute("SELECT * FROM events WHERE patient_id=? AND type='med_admin' ORDER BY ts", (pid,))]


def notes_and_services(conn: sqlite3.Connection, pid: int) -> list[dict]:
    return [{"cite": f"E{r['id']}", "ts": r["ts"], "type": r["type"], "by": r["entered_by"], **json.loads(r["data"])}
            for r in conn.execute("SELECT * FROM events WHERE patient_id=? AND type IN ('note','service','investigation') "
                                  "ORDER BY ts", (pid,))]


def alerts(conn: sqlite3.Connection, pid: int) -> list[dict]:
    return [{"cite": f"A{r['id']}", "ts": r["ts"], "level": r["level"], "reasons": json.loads(r["reasons"]),
             "status": r["status"], "acknowledged_by": r["acknowledged_by"], "trigger_event": f"E{r['event_id']}"}
            for r in conn.execute("SELECT * FROM alerts WHERE patient_id=? ORDER BY ts", (pid,))]


def bill_summary(conn: sqlite3.Connection, pid: int) -> dict:
    b = events.bill(conn, pid)
    return {"total_inr": b["total"], "lines": [{"description": l["description"], "amount_inr": l["amount"],
                                                "cite": f"E{l['event_id']}" if l["event_id"] else "room"} for l in b["lines"]]}


def full(conn: sqlite3.Connection, pid: int) -> dict:
    """Compact whole-record view used by the drafting agent."""
    return {"patient": overview(conn, pid), "document_findings": document_findings(conn, pid),
            "vitals": vitals(conn, pid, 50), "medications_given": medications_given(conn, pid),
            "notes_and_services": notes_and_services(conn, pid), "alerts": alerts(conn, pid)}


def valid_cites(conn: sqlite3.Connection, pid: int) -> set[str]:
    rec = full(conn, pid)
    cites = set()
    for key in ("document_findings", "vitals", "medications_given", "notes_and_services", "alerts"):
        for item in rec[key]:
            cites.add(item["cite"])
    cites.update({c.split(".")[0] for c in list(cites) if c.startswith("D")})  # "D2" alone is acceptable
    return cites
