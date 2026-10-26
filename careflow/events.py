"""The closed loop. Every entry made on the ward is an event; deterministic code then
(1) checks red-flag rules, (2) posts charges, (3) moves stock, (4) creates the baby's record at birth.
The AI never calls any of these functions - it has no write access."""
from __future__ import annotations

import json
import math
import sqlite3

from . import rules, vaccines
from .db import audit, hours_since, now, parse_ts

EVENT_TYPES = {"vitals", "med_admin", "vaccine", "service", "note", "investigation", "labour", "delivery", "newborn_obs"}
DOCTOR_ROLES = {"doctor", "consultant"}

DELIVERY_MODES = {"NVD": "Normal vaginal delivery", "VACUUM": "Assisted vaginal (vacuum)",
                  "FORCEPS": "Assisted vaginal (forceps)", "LSCS": "Caesarean section (LSCS)"}
# Charges posted once when a delivery is recorded (itemised, from the hospital's price list).
# Surgeon's fees are separate from labour-room charges, so vaginal deliveries also carry LABOUR_ROOM.
DELIVERY_PACKAGE = {"NVD": ["NVD_PKG", "LABOUR_ROOM"], "VACUUM": ["ASSIST_PKG", "LABOUR_ROOM"], "FORCEPS": ["ASSIST_PKG", "LABOUR_ROOM"],
                    "LSCS": ["LSCS_PKG", "ASST_SURGEON", "OT", "ANAES_LSCS"]}
LIVE_BIRTH_CHARGES = ["BABY_CARE", "LACTATION"]  # per childbirth, only when the baby is born alive
ROOM_CHARGES = {"general": "ROOM_GEN", "private": "ROOM_PVT"}  # one of these per day, by the patient's room type
DAILY_CHARGES = ["NURSING", "RMO"]                  # billed per day of stay for mothers, on top of the room
ADMISSION_CHARGES = ["REG"]                         # once per admission (mothers)
OUTCOMES = {"live_birth": "Live birth", "stillbirth_fresh": "Stillbirth (fresh) – fetal death",
            "stillbirth_macerated": "Stillbirth (macerated) – intrauterine fetal death"}
PLACENTA = {"complete": "Complete", "incomplete": "Incomplete", "manual_removal": "Manual removal"}
PERINEUM = {"intact": "Intact", "first": "1st-degree tear", "second": "2nd-degree tear", "third_fourth": "3rd/4th-degree tear",
            "episiotomy": "Episiotomy", "na": "Not applicable (LSCS)"}


class EventError(ValueError):
    pass


def _int(v, field, lo=None, hi=None, required=False):
    if v in (None, ""):
        if required:
            raise EventError(f"{field} is required.")
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        raise EventError(f"{field} must be a number.") from None
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        raise EventError(f"{field} must be between {lo} and {hi}.")
    return n


def _float(v, field, lo=None, hi=None):
    if v in (None, ""):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise EventError(f"{field} must be a number.") from None
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        raise EventError(f"{field} must be between {lo} and {hi}.")
    return n


def _raise_alert(conn, pid, eid, ts, category, level, reasons, score, source) -> int:
    if source == "document":
        reasons = reasons + ["Values read by AI from a handwritten document – verify against the original."]
    c = conn.execute("INSERT INTO alerts (patient_id, event_id, ts, category, level, score, reasons, source, status, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (pid, eid, ts, category, level, score, json.dumps(reasons), source, "pending", now()))
    return c.lastrowid


def _alert_from(conn, pid, eid, ts, category, result, source) -> int | None:
    if result["trigger"]:
        return _raise_alert(conn, pid, eid, ts, category, result["level"], result["reasons"], result["score"], source)
    return None


def _charge(conn, pid, eid, code, description, qty, price, ts):
    conn.execute("INSERT INTO charges (patient_id, event_id, code, description, qty, unit_price, amount, ts) VALUES (?,?,?,?,?,?,?,?)",
                 (pid, eid, code, description, qty, price, round(price * qty, 2), ts))


def _tariff(conn, code):
    t = conn.execute("SELECT * FROM tariff WHERE code=?", (code,)).fetchone()
    if not t:
        raise EventError(f"Service code {code} not in tariff.")
    return t


# ----------------------------------------------------------------------------- validation per type
def _validate(conn, p, etype, data, actor_role, entered_by, ts) -> tuple[dict, dict]:
    """Returns (clean data, context for side effects). Raises EventError on bad input."""
    ctx: dict = {}
    if etype == "vitals":
        clean = {k: data.get(k) for k in ("sbp", "dbp", "hr", "rr", "spo2", "fhr", "temp_c", "temp_f", "avpu",
                                          "from_document", "page", "verbatim") if data.get(k) not in (None, "")}
        for k, lo, hi in (("sbp", 40, 260), ("dbp", 20, 180), ("hr", 20, 250), ("rr", 4, 80), ("spo2", 50, 100), ("fhr", 50, 240)):
            if k in clean:
                clean[k] = _int(clean[k], k.upper(), lo, hi)
        for k, lo, hi in (("temp_c", 30, 43), ("temp_f", 86, 110)):
            if k in clean:
                clean[k] = _float(clean[k], "Temperature", lo, hi)
        if not any(k in clean for k in ("sbp", "dbp", "hr", "rr", "spo2", "fhr", "temp_c", "temp_f")):
            raise EventError("Enter at least one observation.")
        return clean, ctx

    if etype in ("med_admin", "vaccine"):
        if etype == "vaccine":
            code = data.get("code")
            if code not in vaccines.BIRTH_DOSES:
                raise EventError("Unknown birth dose.")
            if p["kind"] != "baby":
                raise EventError("Birth doses are recorded on the baby's record.")
            prev = conn.execute("SELECT ts FROM events WHERE patient_id=? AND type='vaccine' AND json_extract(data,'$.code')=?",
                                (p["id"], code)).fetchone()
            if prev:
                raise EventError(f"{vaccines.NAMES[code]} was already given at {prev['ts'][:16]}.")
            data = {**data, "item_code": vaccines.BIRTH_DOSES[code], "qty": 1, "vaccine": vaccines.NAMES[code]}
        item = conn.execute("SELECT * FROM inventory WHERE item_code=?", (data.get("item_code"),)).fetchone()
        if not item:
            raise EventError("Item not in the pharmacy list – choose one from the list.")
        qty = _int(data.get("qty") or 1, "Quantity", 1, 100)
        if item["stock"] < qty:
            raise EventError(f"Only {item['stock']} {item['unit']} of {item['name']} in stock.")
        chk = rules.allergy_check(p["allergies"], item["name"], item["drug_class"])
        override = (data.get("override_reason") or "").strip()
        if chk["level"] != "ok":
            if actor_role not in DOCTOR_ROLES:
                raise EventError(f"ALLERGY {chk['level'].upper()}: {chk['reason']} Only a doctor can override, with a reason.")
            if len(override) < 10:
                raise EventError(f"ALLERGY {chk['level'].upper()}: {chk['reason']} To proceed, a doctor must enter an override reason.")
            ctx["allergy_override"] = {**chk, "reason_given": override[:300]}
        clean = {**{k: data[k] for k in ("code", "vaccine", "dose", "route", "site", "batch") if data.get(k)},
                 "item_code": item["item_code"], "name": item["name"], "unit": item["unit"], "qty": qty}
        if ctx.get("allergy_override"):
            clean["allergy_override"] = ctx["allergy_override"]
        return clean, ctx

    if etype == "service":
        t = _tariff(conn, data.get("code"))
        return {"code": t["code"], "name": t["name"], "qty": _float(data.get("qty") or 1, "Quantity", 0.5, 100)}, ctx

    if etype == "note":
        text = (data.get("text") or "").strip()
        if not text:
            raise EventError("Note is empty.")
        clean = {"text": text[:4000]}
        if data.get("category") in ("progress", "plan", "discharge_advice", "follow_up", "nursing"):
            clean["category"] = data["category"]
        return clean, ctx

    if etype == "investigation":
        test = (data.get("test") or "").strip()[:80]
        if not test:
            raise EventError("Choose the test.")
        values = {}
        for k in rules.LAB_FIELDS:
            v = _float((data.get("values") or {}).get(k), rules.LAB_FIELDS[k][0], 0, 2000)
            if v is not None:
                values[k] = v
        up = (data.get("values") or {}).get("urine_protein")
        if up in rules.URINE_PROTEIN:
            values["urine_protein"] = up
        clean = {"test": test, "values": values, "result_text": (data.get("result_text") or "").strip()[:500]}
        if not values and not clean["result_text"]:
            raise EventError("Enter a result.")
        if data.get("code"):
            clean["code"] = _tariff(conn, data["code"])["code"]
        return clean, ctx

    if etype == "labour":
        if p["kind"] != "mother":
            raise EventError("Labour monitoring is for the mother's record.")
        if conn.execute("SELECT 1 FROM events WHERE patient_id=? AND type='delivery'", (p["id"],)).fetchone():
            raise EventError("Delivery is already recorded – use postnatal observations.")
        clean = {"dilatation_cm": _float(data.get("dilatation_cm"), "Cervical dilatation", 0, 10),
                 "descent_fifths": _int(data.get("descent_fifths"), "Descent", 0, 5),
                 "contractions_10min": _int(data.get("contractions_10min"), "Contractions", 0, 10),
                 "contraction_secs": _int(data.get("contraction_secs"), "Contraction duration", 0, 180),
                 "fhr": _int(data.get("fhr"), "Fetal heart rate", 50, 240),
                 "liquor": data.get("liquor") if data.get("liquor") in rules.LIQUOR else None,
                 "moulding": data.get("moulding") if data.get("moulding") in rules.MOULDING else None,
                 "oxytocin": (data.get("oxytocin") or "").strip()[:60] or None,
                 "sbp": _int(data.get("sbp"), "Systolic BP", 40, 260), "dbp": _int(data.get("dbp"), "Diastolic BP", 20, 180),
                 "hr": _int(data.get("hr"), "Maternal pulse", 20, 250), "temp_c": _float(data.get("temp_c"), "Temperature", 30, 43)}
        clean = {k: v for k, v in clean.items() if v is not None}
        if not clean:
            raise EventError("Enter at least one labour finding.")
        return clean, ctx

    if etype == "delivery":
        if p["kind"] != "mother":
            raise EventError("Delivery is recorded on the mother's record.")
        mode, outcome = data.get("mode"), data.get("outcome")
        if mode not in DELIVERY_MODES:
            raise EventError("Choose the mode of delivery.")
        if outcome not in OUTCOMES:
            raise EventError("Choose the outcome.")
        n = _int(data.get("baby_number") or 1, "Baby number", 1, 4)
        if conn.execute("SELECT 1 FROM events WHERE patient_id=? AND type='delivery' AND json_extract(data,'$.baby_number')=?",
                        (p["id"], n)).fetchone():
            raise EventError(f"Delivery of baby {n} is already recorded.")
        clean = {"mode": mode, "outcome": outcome, "baby_number": n,
                 "sex": data.get("sex") if data.get("sex") in ("M", "F", "ambiguous") else None,
                 "weight_g": _int(data.get("weight_g"), "Birth weight", 300, 6500, required=True),
                 "apgar1": _int(data.get("apgar1"), "APGAR 1 min", 0, 10), "apgar5": _int(data.get("apgar5"), "APGAR 5 min", 0, 10),
                 "ebl_ml": _int(data.get("ebl_ml"), "Blood loss", 0, 6000, required=True),
                 "placenta": data.get("placenta") if data.get("placenta") in PLACENTA else None,
                 "perineum": data.get("perineum") if data.get("perineum") in PERINEUM else None,
                 "indication": (data.get("indication") or "").strip()[:200] or None,
                 "complications": (data.get("complications") or "").strip()[:500] or None,
                 "resuscitation": bool(data.get("resuscitation"))}
        if outcome == "live_birth" and (clean["apgar1"] is None or clean["apgar5"] is None or not clean["sex"]):
            raise EventError("For a live birth enter the baby's sex and APGAR at 1 and 5 minutes.")
        if mode == "LSCS" and not clean["indication"]:
            raise EventError("Enter the indication for caesarean section.")
        return {k: v for k, v in clean.items() if v is not None}, ctx

    if etype == "newborn_obs":
        if p["kind"] != "baby":
            raise EventError("Newborn observations are recorded on the baby's record.")
        clean = {"temp_c": _float(data.get("temp_c"), "Temperature", 30, 43), "hr": _int(data.get("hr"), "Heart rate", 40, 260),
                 "rr": _int(data.get("rr"), "Respiratory rate", 5, 120), "spo2": _int(data.get("spo2"), "SpO2", 50, 100),
                 "weight_g": _int(data.get("weight_g"), "Weight", 300, 6500),
                 "feeding": data.get("feeding") if data.get("feeding") in rules.FEEDING else None,
                 "jaundice": data.get("jaundice") if data.get("jaundice") in rules.JAUNDICE else None,
                 "activity": data.get("activity") if data.get("activity") in rules.ACTIVITY else None,
                 "cord": data.get("cord") if data.get("cord") in rules.CORD else None,
                 "urine": data.get("urine") if data.get("urine") in ("yes", "no") else None,
                 "stool": data.get("stool") if data.get("stool") in ("yes", "no") else None,
                 "chest_indrawing": True if data.get("chest_indrawing") else None,
                 "apgar1": data.get("apgar1"), "apgar5": data.get("apgar5"), "at_birth": data.get("at_birth")}
        clean = {k: v for k, v in clean.items() if v is not None}
        if not clean:
            raise EventError("Enter at least one observation.")
        return clean, ctx

    raise EventError(f"Unknown event type {etype!r}")


# ----------------------------------------------------------------------------- record
def record_event(conn: sqlite3.Connection, patient_id: int, etype: str, data: dict, *, ts: str | None = None,
                 source: str = "bedside", entered_by: str = "staff", key: str | None = None,
                 actor_role: str | None = None) -> dict:
    """Insert one event and run its side effects inside a single transaction.
    `key` is an idempotency key: submitting the same form twice does nothing the second time."""
    if etype not in EVENT_TYPES:
        raise EventError(f"Unknown event type {etype!r}")
    p = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if not p:
        raise EventError("Unknown patient.")
    if p["status"] == "discharged":
        raise EventError("This patient has been discharged; the record is closed.")
    ts = ts or now()
    if key:
        row = conn.execute("SELECT id FROM events WHERE idempotency_key=?", (key,)).fetchone()
        if row:
            return {"event_id": row["id"], "duplicate": True, "alert_id": None, "baby_id": None, "alerts": []}

    clean, ctx = _validate(conn, p, etype, data, actor_role, entered_by, ts)
    cur = conn.execute("INSERT INTO events (patient_id, ts, type, data, source, entered_by, idempotency_key, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (patient_id, ts, etype, json.dumps(clean), source, entered_by, key, now()))
    eid = cur.lastrowid
    alerts: list[int] = []
    baby_id = None

    def add(a):
        if a:
            alerts.append(a)

    if etype == "vitals":
        if p["kind"] == "baby":
            add(_alert_from(conn, patient_id, eid, ts, "newborn", rules.evaluate_newborn(clean, hours_since(p["birth_at"], ts)), source))
        else:
            add(_alert_from(conn, patient_id, eid, ts, "maternal", rules.evaluate(clean), source))

    elif etype in ("med_admin", "vaccine"):
        conn.execute("UPDATE inventory SET stock = stock - ? WHERE item_code=?", (clean["qty"], clean["item_code"]))
        conn.execute("INSERT INTO stock_ledger (item_code, delta, event_id, ts, reason, by_user) VALUES (?,?,?,?,?,?)",
                     (clean["item_code"], -clean["qty"], eid, ts, f"{'Vaccinated' if etype == 'vaccine' else 'Administered to'} {p['uhid']}", entered_by))
        price = conn.execute("SELECT unit_price FROM inventory WHERE item_code=?", (clean["item_code"],)).fetchone()[0]
        _charge(conn, patient_id, eid, clean["item_code"], clean["name"], clean["qty"], price, ts)
        if ctx.get("allergy_override"):
            o = ctx["allergy_override"]
            add(_raise_alert(conn, patient_id, eid, ts, "allergy", "red" if o["level"] == "block" else "yellow",
                             [f"{'RED' if o['level'] == 'block' else 'YELLOW'}: Allergy override – {o['reason']}",
                              f"Override by {entered_by}: “{o['reason_given']}”"], "override", source))

    elif etype == "service":
        t = _tariff(conn, clean["code"])
        _charge(conn, patient_id, eid, t["code"], t["name"], clean["qty"], t["price"], ts)

    elif etype == "investigation":
        add(_alert_from(conn, patient_id, eid, ts, "lab", rules.evaluate_labs(clean["values"]), source))
        if clean.get("code"):
            t = _tariff(conn, clean["code"])
            _charge(conn, patient_id, eid, t["code"], t["name"], 1, t["price"], ts)

    elif etype == "labour":
        hist = [{**json.loads(r["data"]), "ts": r["ts"]} for r in conn.execute(
            "SELECT * FROM events WHERE patient_id=? AND type='labour' AND id<>? AND ts<=? ORDER BY ts, id", (patient_id, eid, ts))]
        add(_alert_from(conn, patient_id, eid, ts, "labour", rules.evaluate_labour({**clean, "ts": ts}, hist), source))

    elif etype == "delivery":
        first = not conn.execute("SELECT 1 FROM events WHERE patient_id=? AND type='delivery' AND id<>?", (patient_id, eid)).fetchone()
        if first:
            codes = DELIVERY_PACKAGE[clean["mode"]] + (LIVE_BIRTH_CHARGES if clean["outcome"] == "live_birth" else [])
            for code in codes:
                t = conn.execute("SELECT * FROM tariff WHERE code=?", (code,)).fetchone()
                if t:  # an item removed from the price list is simply not charged
                    _charge(conn, patient_id, eid, t["code"], t["name"], 1, t["price"], ts)
        reds, yellows = [], []
        limit = 1000 if clean["mode"] == "LSCS" else 500
        if clean["ebl_ml"] >= limit:
            reds.append(f"RED: Postpartum haemorrhage – blood loss {clean['ebl_ml']} ml (≥{limit} ml after {clean['mode']})")
        elif clean["ebl_ml"] >= limit * 0.8:
            yellows.append(f"YELLOW: Blood loss {clean['ebl_ml']} ml – close to PPH threshold")
        if clean.get("placenta") in ("incomplete", "manual_removal"):
            reds.append(f"RED: Placenta {PLACENTA[clean['placenta']].lower()} – risk of retained products / PPH")
        if clean.get("perineum") == "third_fourth":
            reds.append("RED: 3rd/4th-degree perineal tear – consultant repair")
        if clean["outcome"] != "live_birth":
            reds.append(f"RED: {OUTCOMES[clean['outcome']]} – consultant review, documentation and bereavement support")
        if reds or yellows:
            add(_raise_alert(conn, patient_id, eid, ts, "delivery", "red" if reds else "yellow", reds + yellows,
                             f"{len(reds)} red / {len(yellows)} yellow", source))
        if clean["outcome"] == "live_birth":
            baby_id = _create_baby(conn, p, clean, ts, entered_by, eid)
            b_reds, b_yellows = [], []
            if clean["apgar5"] < 7:
                b_reds.append(f"RED: APGAR {clean['apgar5']} at 5 minutes")
            if clean["weight_g"] < 1800:
                b_reds.append(f"RED: Very low birth weight {clean['weight_g']} g")
            elif clean["weight_g"] < 2500:
                b_yellows.append(f"YELLOW: Low birth weight {clean['weight_g']} g – feeding & warmth plan")
            if clean.get("resuscitation"):
                b_reds.append("RED: Needed resuscitation at birth")
            if b_reds or b_yellows:
                add(_raise_alert(conn, baby_id, eid, ts, "newborn", "red" if b_reds else "yellow", b_reds + b_yellows,
                                 f"{len(b_reds)} red / {len(b_yellows)} yellow", source))

    elif etype == "newborn_obs":
        add(_alert_from(conn, patient_id, eid, ts, "newborn",
                        rules.evaluate_newborn(clean, hours_since(p["birth_at"], ts), p["birth_weight_g"]), source))

    audit(conn, entered_by, f"event_{etype}", {"patient_id": patient_id, "event_id": eid, "source": source,
                                               **({"allergy_override": True} if ctx.get("allergy_override") else {})})
    conn.commit()
    return {"event_id": eid, "duplicate": False, "alert_id": alerts[0] if alerts else None, "alerts": alerts, "baby_id": baby_id}


def _create_baby(conn, mother, d, ts, entered_by, eid) -> int:
    n = conn.execute("SELECT COUNT(*) FROM patients WHERE mother_id=?", (mother["id"],)).fetchone()[0] + 1
    name = f"Baby {'girl' if d['sex'] == 'F' else 'boy' if d['sex'] == 'M' else ''} of {mother['name']}".replace("  ", " ")
    cur = conn.execute(
        "INSERT INTO patients (kind, mother_id, name, age, sex, uhid, bed, consultant, admitted_at, admitting_diagnosis, status, "
        "allergies, gestational_age, email, phone, birth_at, birth_weight_g, intake_completed_by, intake_completed_at) "
        "VALUES ('baby',?,?,0,?,?,?,?,?,?,'admitted',?,?,?,?,?,?,?,?)",
        (mother["id"], name, d["sex"], f"{mother['uhid']}-B{n}", f"{mother['bed']} (rooming-in)", "Paediatrician on call",
         ts, f"Newborn – {DELIVERY_MODES[d['mode']]}, {d['weight_g']} g, APGAR {d['apgar1']}/{d['apgar5']}",
         None, mother["gestational_age"], mother["email"], mother["phone"], ts, d["weight_g"],
         f"Born in hospital (E{eid})", ts))
    bid = cur.lastrowid
    conn.execute("INSERT INTO events (patient_id, ts, type, data, source, entered_by, created_at) VALUES (?,?,?,?,?,?,?)",
                 (bid, ts, "newborn_obs", json.dumps({"weight_g": d["weight_g"], "apgar1": d["apgar1"], "apgar5": d["apgar5"],
                                                      "at_birth": True}), "delivery", entered_by, now()))
    audit(conn, entered_by, "baby_record_created", {"mother_id": mother["id"], "baby_id": bid, "delivery_event": eid})
    return bid


def acknowledge_alert(conn: sqlite3.Connection, alert_id: int, consultant: str) -> None:
    conn.execute("UPDATE alerts SET status='acknowledged', acknowledged_by=?, acknowledged_at=? WHERE id=? AND status='pending'",
                 (consultant, now(), alert_id))
    audit(conn, consultant, "alert_acknowledged", {"alert_id": alert_id})
    conn.commit()


def room_days(admitted_at: str, until: str | None = None) -> int:
    hours = (parse_ts(until or now()) - parse_ts(admitted_at)).total_seconds() / 3600
    return max(1, math.ceil(hours / 24))


def bill(conn: sqlite3.Connection, patient_id: int, until: str | None = None) -> dict:
    """Deterministic bill for one patient: room charges by day (mothers only; babies room in) + every charge
    row posted by an event."""
    p = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    until = until or p["discharged_at"]
    lines = []
    if p["kind"] == "mother":
        days = room_days(p["admitted_at"], until)
        room = ROOM_CHARGES.get(p["room_type"] or "general", "ROOM_GEN")
        for code, qty in [(c, 1) for c in ADMISSION_CHARGES] + [(c, days) for c in [room] + DAILY_CHARGES]:
            t = conn.execute("SELECT * FROM tariff WHERE code=?", (code,)).fetchone()
            if not t:
                continue
            label = t["name"] if code in ADMISSION_CHARGES else f"{t['name']} ({days} day{'s' if days > 1 else ''})"
            lines.append({"code": t["code"], "description": label, "qty": qty, "unit_price": t["price"],
                          "amount": t["price"] * qty, "event_id": None, "ts": p["admitted_at"]})
    for r in conn.execute("SELECT * FROM charges WHERE patient_id=? ORDER BY ts, id", (patient_id,)):
        lines.append({k: r[k] for k in ("code", "description", "qty", "unit_price", "amount", "event_id", "ts")})
    total = round(sum(l["amount"] for l in lines), 2)
    return {"patient_id": patient_id, "name": p["name"], "lines": lines, "total": total, "currency": "INR"}


def family_bill(conn: sqlite3.Connection, mother_id: int, until: str | None = None) -> dict:
    """Mother's bill plus her babies' bills - what the family pays at discharge."""
    parts = [bill(conn, mother_id, until)]
    for b in conn.execute("SELECT id FROM patients WHERE mother_id=? ORDER BY id", (mother_id,)):
        parts.append(bill(conn, b["id"], until))
    return {"parts": parts, "total": round(sum(x["total"] for x in parts), 2), "currency": "INR"}
