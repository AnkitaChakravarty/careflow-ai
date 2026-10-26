"""Indian Academy of Pediatrics (IAP) immunisation schedule - deterministic due dates.

Based on the IAP Advisory Committee on Vaccines & Immunization Practices (ACVIP) schedule 2023.
Dates are computed by code from the date of birth, never by the AI. Each hospital should check the
current IAP schedule before use; brand-specific variations (e.g. 2- vs 3-dose rotavirus) are noted.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .db import parse_ts

# (visit label, days after birth, [(code, vaccine name)])
SCHEDULE = [
    ("Birth", 0, [("BCG", "BCG"), ("OPV0", "OPV-0 (oral polio)"), ("HEPB1", "Hepatitis B-1")]),
    ("6 weeks", 42, [("DTP1", "DTwP/DTaP-1"), ("IPV1", "IPV-1"), ("HIB1", "Hib-1"), ("HEPB2", "Hepatitis B-2"),
                     ("RV1", "Rotavirus-1"), ("PCV1", "PCV-1")]),
    ("10 weeks", 70, [("DTP2", "DTwP/DTaP-2"), ("IPV2", "IPV-2"), ("HIB2", "Hib-2"), ("HEPB3", "Hepatitis B-3"),
                      ("RV2", "Rotavirus-2"), ("PCV2", "PCV-2")]),
    ("14 weeks", 98, [("DTP3", "DTwP/DTaP-3"), ("IPV3", "IPV-3"), ("HIB3", "Hib-3"), ("HEPB4", "Hepatitis B-4"),
                      ("RV3", "Rotavirus-3 (3-dose brands only)"), ("PCV3", "PCV-3")]),
    ("6 months", 182, [("FLU1", "Influenza-1"), ("TCV", "Typhoid conjugate vaccine (6–9 months)")]),
    ("7 months", 213, [("FLU2", "Influenza-2")]),
    ("9 months", 274, [("MMR1", "MMR-1")]),
    ("12 months", 365, [("HEPA1", "Hepatitis A-1")]),
    ("12–15 months", 365, [("PCVB", "PCV booster")]),
    ("15 months", 456, [("MMR2", "MMR-2"), ("VAR1", "Varicella-1")]),
    ("16–18 months", 487, [("DTPB1", "DTwP/DTaP booster-1"), ("HIBB1", "Hib booster-1"), ("IPVB1", "IPV booster-1")]),
    ("18–19 months", 548, [("HEPA2", "Hepatitis A-2 (inactivated vaccine)"), ("VAR2", "Varicella-2")]),
    ("2–5 years", 730, [("FLUA", "Influenza (yearly)")]),
    ("4–6 years", 1461, [("DTPB2", "DTwP/DTaP booster-2"), ("IPVB2", "IPV booster-2"), ("MMR3", "MMR-3")]),
]

# Given in hospital before discharge; maps schedule code -> inventory item.
BIRTH_DOSES = {"BCG": "BCG_VAC", "OPV0": "OPV_VAC", "HEPB1": "HEPB_VAC", "VITK": "VITK_INJ"}
EXTRA_NAMES = {"VITK": "Vitamin K 1 mg IM (birth prophylaxis)"}
# How long after birth each birth dose should be given (hours) before it is shown as overdue.
BIRTH_DEADLINE_H = {"HEPB1": 24, "VITK": 6, "BCG": 72, "OPV0": 72}

NAMES = {code: name for _, _, vs in SCHEDULE for code, name in vs} | EXTRA_NAMES
SOURCE_NOTE = ("IAP ACVIP immunisation schedule 2023 (Indian Academy of Pediatrics). Dates computed from date of birth; "
               "confirm against the current IAP schedule and the vaccine brands used.")


def birth_dose_status(birth_at: str, given: dict[str, str], now: datetime | None = None) -> list[dict]:
    """given: {code: ts}. Returns one row per in-hospital birth dose with status given/due/overdue."""
    now = now or datetime.now()
    age_h = (now - parse_ts(birth_at)).total_seconds() / 3600
    out = []
    for code in ("VITK", "BCG", "OPV0", "HEPB1"):
        if code in given:
            status = "given"
        else:
            status = "overdue" if age_h > BIRTH_DEADLINE_H[code] else "due"
        out.append({"code": code, "name": NAMES[code], "status": status, "given_at": given.get(code),
                    "deadline_h": BIRTH_DEADLINE_H[code], "item_code": BIRTH_DOSES[code]})
    return out


def due_list(birth_at: str, given: dict[str, str] | None = None) -> list[dict]:
    """Full IAP schedule with due dates for one baby."""
    given = given or {}
    dob = parse_ts(birth_at).date()
    rows = []
    for label, days, vs in SCHEDULE:
        due = dob + timedelta(days=days)
        rows.append({"visit": label, "due_date": due.isoformat(), "days": days,
                     "vaccines": [{"code": c, "name": n, "given_at": given.get(c)} for c, n in vs]})
    return rows


def upcoming_visits(birth_at: str, after_days: int = 1, limit: int = 4) -> list[dict]:
    """Next vaccination visits after discharge (used for reminder emails)."""
    return [r for r in due_list(birth_at) if r["days"] >= after_days][:limit]
