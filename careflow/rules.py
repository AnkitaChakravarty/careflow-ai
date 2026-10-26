"""Deterministic red-flag rules. No AI decides whether a reading is dangerous.

Sources (DEMO THRESHOLDS ONLY - not a clinically validated monitoring system; a hospital would
configure and validate its own values here):
- Mother: Modified Early Obstetric Warning Score (MEOWS; Singh et al., Anaesthesia 2012); fetal heart
  rate band from FIGO 2015 (normal 110-160 bpm).
- Labour: WHO Labour Care Guide (2020) "alert" criteria, incl. time limits per cm of cervical dilatation.
- Newborn: WHO / IMNCI young-infant danger signs and normal ranges.
- Labs: severe-feature thresholds commonly used for pre-eclampsia/HELLP and anaemia in pregnancy.

Trigger: any single RED finding, or two or more YELLOW findings in the same entry.
"""
from __future__ import annotations

# ----------------------------------------------------------------------------- mother (MEOWS)
RULES = [
    # (field, label, red_test, yellow_test, unit)
    ("sbp", "Systolic BP", lambda v: v < 90 or v >= 160, lambda v: 150 <= v < 160 or 90 <= v < 100, "mmHg"),
    ("dbp", "Diastolic BP", lambda v: v >= 110, lambda v: 90 <= v < 110, "mmHg"),
    ("hr", "Heart rate", lambda v: v < 40 or v > 120, lambda v: 100 < v <= 120 or 40 <= v < 50, "/min"),
    ("rr", "Respiratory rate", lambda v: v < 10 or v > 30, lambda v: 21 <= v <= 30, "/min"),
    ("temp_c", "Temperature", lambda v: v < 35 or v >= 38, lambda v: 35 <= v < 36, "°C"),
    ("spo2", "SpO2", lambda v: v < 95, lambda v: False, "%"),
    ("fhr", "Fetal heart rate", lambda v: v < 110 or v > 160, lambda v: False, "/min"),
]
CONSCIOUSNESS_RED = {"pain", "unresponsive"}
CONSCIOUSNESS_YELLOW = {"voice"}

THRESHOLD_TABLE = [
    ("Systolic BP", "<90 or ≥160", "150–159 or 90–99"),
    ("Diastolic BP", "≥110", "90–109"),
    ("Heart rate", "<40 or >120", "101–120 or 40–49"),
    ("Respiratory rate", "<10 or >30", "21–30"),
    ("Temperature (°C)", "<35 or ≥38", "35–35.9"),
    ("SpO2", "<95%", "—"),
    ("Consciousness (AVPU)", "Pain / Unresponsive", "Voice"),
    ("Fetal heart rate", "<110 or >160", "—"),
]


def f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 1)


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _result(reds: list, yellows: list) -> dict:
    trigger = bool(reds) or len(yellows) >= 2
    level = "red" if reds else ("yellow" if yellows else "none")
    return {"level": level, "trigger": trigger, "reasons": reds + yellows, "score": f"{len(reds)} red / {len(yellows)} yellow"}


def _apply(rules, vals, reds, yellows):
    for field, label, red, yellow, unit in rules:
        v = _num(vals.get(field))
        if v is None:
            continue
        shown = f"{label} {v:g}{unit}"
        if red(v):
            reds.append(f"RED: {shown}")
        elif yellow(v):
            yellows.append(f"YELLOW: {shown}")


def evaluate(obs: dict) -> dict:
    """Maternal observations (MEOWS). Returns {'level','trigger','reasons','score'}."""
    reds, yellows = [], []
    vals = dict(obs)
    if _num(vals.get("temp_c")) is None and _num(vals.get("temp_f")) is not None:
        vals["temp_c"] = f_to_c(float(vals["temp_f"]))
    _apply(RULES, vals, reds, yellows)
    avpu = str(vals.get("avpu") or "").strip().lower()
    if avpu in CONSCIOUSNESS_RED:
        reds.append(f"RED: Consciousness responds to {avpu}")
    elif avpu in CONSCIOUSNESS_YELLOW:
        yellows.append("YELLOW: Consciousness responds to voice")
    return _result(reds, yellows)


# ----------------------------------------------------------------------------- labour (WHO LCG)
# Hours a woman may stay at the same dilatation in active first stage before an alert (WHO Labour Care Guide).
LCG_HOURS_AT_CM = {5: 6, 6: 5, 7: 3, 8: 2.5, 9: 2}
LIQUOR = {"intact": "Membranes intact", "clear": "Clear", "meconium_thin": "Meconium (thin)",
          "meconium_thick": "Meconium (thick)", "blood_stained": "Blood-stained", "absent": "Absent"}
MOULDING = ["0", "+", "++", "+++"]

LABOUR_TABLE = [
    ("Fetal heart rate", "<110 or ≥160 /min", "—"),
    ("Amniotic fluid", "Thick meconium or blood-stained", "Thin meconium"),
    ("Contractions", ">5 in 10 min (tachysystole)", "≤2 in 10 min in active labour (≥5 cm)"),
    ("Moulding", "+++", "—"),
    ("Progress (active first stage)", "Same dilatation for ≥ 6 h at 5 cm, 5 h at 6 cm, 3 h at 7 cm, 2.5 h at 8 cm, 2 h at 9 cm", "—"),
    ("Second stage (fully dilated)", "≥ 3 h", "≥ 2 h"),
    ("Maternal BP / pulse / temp", "MEOWS red values", "MEOWS yellow values"),
]


def evaluate_labour(entry: dict, history: list[dict]) -> dict:
    """entry/history items: {'ts', 'dilatation_cm', 'contractions_10min', 'fhr', 'liquor', 'moulding', + maternal obs}.
    history = earlier labour entries for this woman, oldest first."""
    from .db import parse_ts
    reds, yellows = [], []
    fhr = _num(entry.get("fhr"))
    if fhr is not None and (fhr < 110 or fhr >= 160):
        reds.append(f"RED: Fetal heart rate {fhr:g}/min (WHO LCG alert <110 or ≥160)")
    liq = entry.get("liquor")
    if liq in ("meconium_thick", "blood_stained"):
        reds.append(f"RED: Amniotic fluid {LIQUOR[liq].lower()}")
    elif liq == "meconium_thin":
        yellows.append("YELLOW: Amniotic fluid thin meconium")
    dil = _num(entry.get("dilatation_cm"))
    con = _num(entry.get("contractions_10min"))
    if con is not None:
        if con > 5:
            reds.append(f"RED: {con:g} contractions in 10 min (tachysystole){' – review oxytocin' if entry.get('oxytocin') else ''}")
        elif con <= 2 and dil is not None and dil >= 5:
            yellows.append(f"YELLOW: Only {con:g} contractions in 10 min in active labour")
    if entry.get("moulding") == "+++":
        reds.append("RED: Moulding +++")
    if dil is not None and dil >= 5:
        # contiguous run of entries at this same dilatation, ending with this entry
        start = entry["ts"]
        for h in reversed(history):
            if _num(h.get("dilatation_cm")) == dil:
                start = h["ts"]
            else:
                break
        hours = (parse_ts(entry["ts"]) - parse_ts(start)).total_seconds() / 3600
        if dil >= 10:
            if hours >= 3:
                reds.append(f"RED: Fully dilated for {hours:.1f} h (second stage ≥ 3 h)")
            elif hours >= 2:
                yellows.append(f"YELLOW: Fully dilated for {hours:.1f} h")
        else:
            limit = LCG_HOURS_AT_CM.get(int(dil))
            if limit and hours >= limit:
                reds.append(f"RED: Labour not progressing – {dil:g} cm for {hours:.1f} h (WHO LCG limit {limit:g} h)")
    maternal = evaluate({k: entry.get(k) for k in ("sbp", "dbp", "hr", "temp_c")})
    return _result(reds + [r for r in maternal["reasons"] if r.startswith("RED")],
                   yellows + [r for r in maternal["reasons"] if r.startswith("YELLOW")])


# ----------------------------------------------------------------------------- newborn
NEWBORN_RULES = [
    ("temp_c", "Temperature", lambda v: v < 35.5 or v >= 38, lambda v: 35.5 <= v < 36.5 or 37.5 < v < 38, "°C"),
    ("hr", "Heart rate", lambda v: v < 100 or v > 180, lambda v: 100 <= v < 110 or 160 < v <= 180, "/min"),
    ("rr", "Respiratory rate", lambda v: v >= 60 or v < 30, lambda v: False, "/min"),
    ("spo2", "SpO2", lambda v: v < 90, lambda v: 90 <= v < 95, "%"),
]
JAUNDICE = {"none": "None", "face": "Face only", "trunk": "Down to trunk", "palms_soles": "Palms / soles"}
FEEDING = {"good": "Feeding well", "poor": "Feeding poorly", "not_feeding": "Not feeding"}
ACTIVITY = {"active": "Active", "irritable": "Irritable", "lethargic": "Lethargic / floppy", "convulsions": "Convulsions"}
CORD = {"clean": "Clean & dry", "red_or_discharge": "Redness / discharge"}

NEWBORN_TABLE = [
    ("Temperature (°C)", "<35.5 or ≥38", "35.5–36.4 or 37.6–37.9"),
    ("Heart rate", "<100 or >180", "100–109 or 161–180"),
    ("Respiratory rate", "≥60 or <30, or severe chest indrawing", "—"),
    ("SpO2", "<90%", "90–94%"),
    ("Feeding", "Not feeding", "Feeding poorly"),
    ("Jaundice", "Palms/soles, or any jaundice in first 24 h", "Down to trunk"),
    ("Activity", "Lethargic / convulsions", "Irritable"),
    ("Umbilicus", "—", "Redness / discharge"),
    ("Weight loss from birth weight", ">10%", "7–10%"),
    ("At birth", "5-min APGAR <7, weight <1800 g", "Weight <2500 g (always alerts)"),
]


def evaluate_newborn(obs: dict, age_hours: float | None = None, birth_weight_g: float | None = None) -> dict:
    reds, yellows = [], []
    _apply(NEWBORN_RULES, obs, reds, yellows)
    if obs.get("chest_indrawing"):
        reds.append("RED: Severe chest indrawing")
    feed = obs.get("feeding")
    if feed == "not_feeding":
        reds.append("RED: Not feeding")
    elif feed == "poor":
        yellows.append("YELLOW: Feeding poorly")
    j = obs.get("jaundice")
    if j == "palms_soles":
        reds.append("RED: Jaundice to palms/soles")
    elif j in ("face", "trunk") and age_hours is not None and age_hours < 24:
        reds.append(f"RED: Jaundice at {age_hours:.0f} h of life (first 24 h)")
    elif j == "trunk":
        yellows.append("YELLOW: Jaundice down to trunk")
    act = obs.get("activity")
    if act in ("lethargic", "convulsions"):
        reds.append(f"RED: {ACTIVITY[act]}")
    elif act == "irritable":
        yellows.append("YELLOW: Irritable")
    if obs.get("cord") == "red_or_discharge":
        yellows.append("YELLOW: Umbilical redness / discharge")
    w = _num(obs.get("weight_g"))
    if w and birth_weight_g:
        loss = (birth_weight_g - w) / birth_weight_g * 100
        if loss > 10:
            reds.append(f"RED: Weight loss {loss:.1f}% of birth weight")
        elif loss >= 7:
            yellows.append(f"YELLOW: Weight loss {loss:.1f}% of birth weight")
    return _result(reds, yellows)


# ----------------------------------------------------------------------------- labs
LAB_FIELDS = {"hb": ("Haemoglobin", "g/dL"), "platelets": ("Platelets", "×10³/µL"), "ast": ("AST/SGOT", "U/L"),
              "alt": ("ALT/SGPT", "U/L"), "creatinine": ("Creatinine", "mg/dL"), "bilirubin": ("Serum bilirubin", "mg/dL")}
LAB_RULES = [
    ("hb", "Haemoglobin", lambda v: v < 7, lambda v: 7 <= v < 9, " g/dL"),
    ("platelets", "Platelets", lambda v: v < 100, lambda v: 100 <= v < 150, "×10³/µL"),
    ("ast", "AST/SGOT", lambda v: v >= 70, lambda v: 40 <= v < 70, " U/L"),
    ("alt", "ALT/SGPT", lambda v: v >= 70, lambda v: 40 <= v < 70, " U/L"),
    ("creatinine", "Creatinine", lambda v: v > 1.1, lambda v: 0.9 < v <= 1.1, " mg/dL"),
    ("bilirubin", "Serum bilirubin (newborn)", lambda v: v >= 15, lambda v: 12 <= v < 15, " mg/dL"),
]
URINE_PROTEIN = ["nil", "trace", "1+", "2+", "3+"]
LAB_TABLE = [
    ("Haemoglobin", "<7 g/dL", "7–8.9 g/dL"),
    ("Platelets", "<100 ×10³/µL", "100–149 ×10³/µL"),
    ("AST / ALT", "≥70 U/L (≈2× upper limit)", "40–69 U/L"),
    ("Creatinine", ">1.1 mg/dL", "0.9–1.1 mg/dL"),
    ("Urine protein (dipstick)", "3+", "2+"),
    ("Serum bilirubin (newborn)", "≥15 mg/dL", "12–14.9 mg/dL"),
]


def evaluate_labs(values: dict) -> dict:
    reds, yellows = [], []
    _apply(LAB_RULES, values, reds, yellows)
    up = values.get("urine_protein")
    if up == "3+":
        reds.append("RED: Urine protein 3+")
    elif up == "2+":
        yellows.append("YELLOW: Urine protein 2+")
    return _result(reds, yellows)


# ----------------------------------------------------------------------------- allergy
# allergen keyword (matched in the doctor-confirmed allergy list) -> drug classes blocked / needing a doctor's override
ALLERGY_RULES = {
    "penicillin": {"block": {"penicillin"}, "caution": {"cephalosporin"}},
    "amoxicillin": {"block": {"penicillin"}, "caution": {"cephalosporin"}},
    "ampicillin": {"block": {"penicillin"}, "caution": {"cephalosporin"}},
    "cephalosporin": {"block": {"cephalosporin"}, "caution": {"penicillin"}},
    "ceftriaxone": {"block": {"cephalosporin"}, "caution": {"penicillin"}},
    "sulfa": {"block": {"sulfonamide"}, "caution": set()},
    "nsaid": {"block": {"nsaid"}, "caution": set()},
    "diclofenac": {"block": {"nsaid"}, "caution": set()},
    "ibuprofen": {"block": {"nsaid"}, "caution": set()},
    "aspirin": {"block": {"nsaid"}, "caution": set()},
    "metronidazole": {"block": {"nitroimidazole"}, "caution": set()},
    "paracetamol": {"block": {"paracetamol"}, "caution": set()},
}


def allergy_check(allergies: str | None, item_name: str, drug_class: str | None) -> dict:
    """Returns {'level': 'block'|'caution'|'ok', 'reason': str}. Deterministic; based only on doctor-confirmed allergies."""
    text = (allergies or "").lower()
    if not text or text.strip() in ("nkda", "no known drug allergies", "none", "nil"):
        return {"level": "ok", "reason": ""}
    cls = (drug_class or "").lower()
    name = item_name.lower()
    for key, r in ALLERGY_RULES.items():
        if key not in text:
            continue
        if cls in r["block"] or key in name:
            return {"level": "block", "reason": f"Documented allergy to {key}: {item_name} ({cls or 'same drug'}) is contraindicated."}
        if cls in r["caution"]:
            return {"level": "caution", "reason": f"Documented allergy to {key}: {item_name} is a {cls} – possible cross-reactivity."}
    return {"level": "ok", "reason": ""}
