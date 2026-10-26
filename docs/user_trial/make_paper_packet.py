"""Builds paper_packet.html: the printouts for the "today's way" half of the user trial.

It reads the live demo database (data/careflow.db), so run it right before the trial, after resetting
the demo data, and the paper case sheets will match what the app shows.

    python docs/user_trial/make_paper_packet.py
"""
from __future__ import annotations

import base64
import html
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from careflow import config, db, events, views  # noqa: E402

e = html.escape
OUT = Path(__file__).with_name("paper_packet.html")
CONN = db.connect()
db.init(CONN)
views.CONN = CONN


def note_image() -> str:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(config.DATA / "samples" / "meera_sharma_opd_and_progress_note.pdf"))
    im = pdf[0].render(scale=130 / 72).to_pil().convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def patient(name_part: str):
    return CONN.execute("SELECT * FROM patients WHERE name LIKE ? AND kind='mother'", (f"%{name_part}%",)).fetchone()


def case_sheet(p) -> str:
    """What a paper case file would hold: demographics + every bedside entry. No alerts, no AI."""
    rows = "".join(f"<tr><td>{views.dt(r['ts'])}</td><td>{e(views.EVENT_LABEL.get(r['type'], r['type']))}</td><td>{views.event_text(r)}</td>"
                   f"<td>{e(r['entered_by'] or '')}</td></tr>"
                   for r in CONN.execute("SELECT * FROM events WHERE patient_id=? AND source<>'document' ORDER BY ts, id", (p["id"],)))
    if p["kind"] == "mother":
        head = (f"Bed {e(p['bed'])} · {e(p['uhid'])} · Age {p['age']} · {e(p['gravida_para'] or '')} · Blood group {e(p['blood_group'] or '—')} · "
                f"Allergies: {e(p['allergies'] or 'not recorded')}<br>Admitted {views.dt(p['admitted_at'])} · {e(p['admitting_diagnosis'])}")
    else:
        head = f"{e(p['uhid'])} · Born {views.dt(p['birth_at'])} · Birth weight {p['birth_weight_g']} g · rooming-in with mother"
    return (f"<div class='sheet'><h3>Case sheet – {e(p['name'])}</h3><p class='small'>{head}</p><table><tr><th>Time</th><th>Entry</th><th>Detail</th>"
            f"<th>By</th></tr>{rows}</table></div>")


def blank(lines: list[str], tall: bool = False) -> str:
    return "<table class='blank'>" + "".join(f"<tr><td class='lab'>{e(l)}</td><td class='{'tall' if tall else ''}'></td></tr>" for l in lines) + "</table>"


def kavita_done_list(k) -> str:
    days = events.room_days(k["admitted_at"])
    items = [f"Admitted {views.dt(k['admitted_at'])} to a {k['room_type'] or 'general'} room – now {days} day(s) in hospital (bed, nursing and RMO are charged per day)",
             "New admission (registration)"]
    for r in CONN.execute("SELECT * FROM events WHERE patient_id IN (SELECT id FROM patients WHERE id=? OR mother_id=?) "
                          "AND type IN ('service','med_admin','vaccine','delivery') ORDER BY ts", (k["id"], k["id"])):
        d = json.loads(r["data"])
        who = "baby" if r["patient_id"] != k["id"] else "mother"
        if r["type"] == "delivery":
            items.append(f"{views.dt(r['ts'])} – {events.DELIVERY_MODES[d['mode']]}, live birth (check the price list for every charge that applies to this delivery)")
        elif r["type"] == "service":
            items.append(f"{views.dt(r['ts'])} – {d['name']} × {d['qty']:g} ({who})")
        else:
            items.append(f"{views.dt(r['ts'])} – {d.get('vaccine') or d['name']} × {d['qty']} ({who})")
    return "<ol>" + "".join(f"<li>{e(i)}</li>" for i in items) + "</ol>"


def price_list() -> str:
    t = "".join(f"<tr><td>{e(r['name'])}</td><td>{e(r['unit'])}</td><td class='num'>₹{r['price']:,.0f}</td></tr>" for r in CONN.execute("SELECT * FROM tariff ORDER BY name"))
    i = "".join(f"<tr><td>{e(r['name'])}</td><td>per {e(r['unit'])}</td><td class='num'>₹{r['unit_price']:,.0f}</td></tr>" for r in CONN.execute("SELECT * FROM inventory ORDER BY name"))
    return (f"<div class='two'><div><h4>Services &amp; packages</h4><table>{t}</table></div>"
            f"<div><h4>Pharmacy &amp; vaccines</h4><table>{i}</table></div></div>")


def main():
    meera, priya, sunita, kavita = (patient(n) for n in ("Meera", "Priya", "Sunita", "Kavita"))
    mothers = CONN.execute("SELECT * FROM patients WHERE kind='mother' AND status<>'discharged' ORDER BY CAST(bed AS INTEGER)").fetchall()
    babies = CONN.execute("SELECT * FROM patients WHERE kind='baby' AND status<>'discharged'").fetchall()
    sunita_baby = next((b for b in babies if b["mother_id"] == sunita["id"]), None)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CareFlow trial – paper packet</title>
<style>
body{{font:14px/1.5 "Segoe UI",Arial,sans-serif;color:#1d2f4a;max-width:900px;margin:20px auto;padding:0 16px}}
h1{{color:#2f6bc2}}h2{{border-bottom:2px solid #cfe2fb;padding-bottom:4px;margin-top:28px;color:#2f6bc2}}
table{{width:100%;border-collapse:collapse;margin:6px 0 12px}}th,td{{border:1px solid #d3e0f2;padding:5px 7px;text-align:left;vertical-align:top;font-size:13px}}
th{{background:#eef5fe}}.num{{text-align:right}}.small{{font-size:12.5px;color:#5f7190}}
.card{{background:#f3f8ff;border:1px solid #cfe2fb;border-radius:10px;padding:10px 14px;margin:8px 0}}
.blank td.lab{{width:30%;background:#f7faff;font-weight:600}}.blank td{{height:30px}}.blank td.tall{{height:120px}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}img{{max-width:100%;border:1px solid #d3e0f2}}
.sheet{{page-break-inside:avoid}}.pb{{page-break-before:always}}
@media print{{.noprint{{display:none}}h2{{page-break-before:always}}h2.first{{page-break-before:avoid}}}}
</style></head><body>
<h1>CareFlow AI user trial – paper packet (“today’s way”)</h1>
<p>Printouts for the <b>today’s way</b> half of each task: work exactly as you would on the ward today, with pen, paper and a calculator.
All patients are <b>synthetic</b>. Generated {views.dt(db.now())} from the demo data – regenerate it if the demo data is reset.</p>
<p class="noprint"><b>Print this page</b> (Ctrl+P) or keep it open on a second screen.</p>

<h2 class="first">Task 1 – Admission papers: Meera Sharma</h2>
<div class="card">Read the handwritten OPD + progress note and fill the admission sheet below: everything the next doctor must know.</div>
<img src="{note_image()}" alt="Handwritten OPD and progress note">
<h4>Admission sheet</h4>{blank(["Name / age", "G / P / L", "Gestational age, LMP, EDD", "Drug allergies", "Blood group", "Complaints", "Past history",
                                  "Examination & BP readings", "Diagnosis", "Medicines ordered", "Investigations & results", "Plan"])}

<h2>Task 2 – Labour check and delivery: Priya Verma</h2>
<div class="card"><b>Now:</b> cervix 8 cm, descent 2/5, 4 contractions in 10 min lasting 45 s, fetal heart 150/min, liquor clear, moulding +, BP 124/80, pulse 90.<br>
<b>40 minutes later she delivers:</b> normal vaginal delivery, live girl, 3,050 g, APGAR 8 and 9, blood loss 350 ml, placenta complete, 1st-degree tear.</div>
<p>Today’s way: write the labour entry in the partograph/labour register, then record the birth (birth register, open the baby’s file,
note what must be charged and tell billing).</p>
<h4>Labour register entry</h4>{blank(["Time", "Cervix / descent", "Contractions", "Fetal heart / liquor / moulding", "Maternal BP / pulse"])}
<h4>Birth register & baby file</h4>{blank(["Mode / outcome / time", "Baby sex / weight / APGAR", "Blood loss / placenta / perineum", "Baby file opened (UHID)",
                                            "Charges to add to the bill"])}

<h2>Task 3 – Shift handover: Sunita Devi and her baby</h2>
<div class="card">Write the handover you would give the night doctor (Situation, Background, Assessment, Recommendation).</div>
{case_sheet(sunita)}{case_sheet(sunita_baby) if sunita_baby else ''}
<h4>Handover</h4>{blank(["Situation", "Background", "Assessment", "Recommendation"], tall=True)}

<h2>Task 4 – Ward question</h2>
<div class="card"><b>“Which babies still need their Hepatitis B birth dose, and which mothers have abnormal latest observations?”</b><br>
Today’s way: look through the ward case sheets below.</div>
{''.join(case_sheet(m) for m in mothers if m['id'] != sunita['id'])}{''.join(case_sheet(b) for b in babies if not sunita_baby or b['id'] != sunita_baby['id'])}
<p class="small">(Sunita Devi’s and her baby’s case sheets are in Task 3.)</p>
<h4>Answer</h4>{blank(["Babies needing Hep B", "Mothers with abnormal observations"])}

<h2>Task 5 – Discharge bill: Kavita Yadav and baby</h2>
<div class="card">Work out the family’s final bill from what was done and the price list, as the front office does today.</div>
<h4>What was done</h4>{kavita_done_list(kavita)}
<h4>Price list</h4>{price_list()}
<h4>Bill</h4>{blank(["Item 1", "Item 2", "Item 3", "Item 4", "Item 5", "Item 6", "Item 7", "Item 8", "Item 9", "Item 10", "Item 11", "Item 12",
                     "Item 13", "Item 14", "Item 15", "Item 16", "Item 17", "Item 18", "Item 19", "Item 20", "TOTAL"])}
</body></html>"""
    OUT.write_text(page, encoding="utf-8")
    print(f"Wrote {OUT}  (family total in the app right now: ₹{events.family_bill(CONN, kavita['id'])['total']:,.0f})")


if __name__ == "__main__":
    main()
