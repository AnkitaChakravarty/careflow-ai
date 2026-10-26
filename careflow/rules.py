"""Deterministic red-flag rules. No AI decides whether a reading is dangerous.

Thresholds follow the published Modified Early Obstetric Warning Score (MEOWS) chart
(Singh et al., Anaesthesia 2012) plus a fetal heart rate band (FIGO 2015: normal 110-160 bpm).
DEMO THRESHOLDS ONLY - not a clinically validated monitoring system. A hospital would
configure its own chart here.

Trigger: any single RED parameter, or two or more YELLOW parameters in the same set of obs.
"""
from __future__ import annotations

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


def evaluate(obs: dict) -> dict:
    """Return {'level': 'red'|'yellow'|'none', 'trigger': bool, 'reasons': [...]}."""
    reds, yellows = [], []
    vals = dict(obs)
    if vals.get("temp_c") in (None, "") and vals.get("temp_f") not in (None, ""):
        vals["temp_c"] = f_to_c(float(vals["temp_f"]))
    for field, label, red, yellow, unit in RULES:
        raw = vals.get(field)
        if raw in (None, ""):
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        shown = f"{label} {v:g}{unit}"
        if red(v):
            reds.append(f"RED: {shown}")
        elif yellow(v):
            yellows.append(f"YELLOW: {shown}")
    avpu = str(vals.get("avpu") or "").strip().lower()
    if avpu in CONSCIOUSNESS_RED:
        reds.append(f"RED: Consciousness responds to {avpu}")
    elif avpu in CONSCIOUSNESS_YELLOW:
        yellows.append("YELLOW: Consciousness responds to voice")

    trigger = bool(reds) or len(yellows) >= 2
    level = "red" if reds else ("yellow" if yellows else "none")
    return {"level": level, "trigger": trigger, "reasons": reds + yellows,
            "score": f"{len(reds)} red / {len(yellows)} yellow"}
