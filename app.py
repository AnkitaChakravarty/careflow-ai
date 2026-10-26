"""CareFlow AI - obstetric ward assistant (prototype, synthetic data only).

Run:   python app.py        then open http://127.0.0.1:8050
Uses only the Python standard library for the web server (no web framework to install).
"""
from __future__ import annotations

import email
import email.policy
import html
import json
import re
import sys
import threading
import traceback
import uuid
import webbrowser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from careflow import config, db, drafts, events, extract, llm, qa, record, rules

if config.MOCK:
    from careflow import mock
    mock.install()

CONN = db.connect()
db.init(CONN)
LOCK = threading.Lock()  # one writer at a time (SQLite + small ward)
ROLES = ["Resident doctor", "Nurse", "Consultant"]
e = html.escape


# --------------------------------------------------------------------------- helpers
def badge(text, kind="grey"):
    return f'<span class="badge b-{kind}">{e(str(text))}</span>'


def conf_badge(c):
    return badge(c or "low", {"high": "green", "medium": "yellow", "low": "red"}.get(c, "red"))


def cite_html(text: str, unsupported: list | None = None) -> str:
    out = e(text)
    out = re.sub(r"\[([A-Z]\d+(?:\.p[\d?]+)?)\]", r'<span class="cite">\1</span>', out)
    for n in unsupported or []:
        out = re.sub(rf"(?<![\d.]){re.escape(n)}(?![\d.])", f'<span class="unsup" title="Number not found in record">{n}</span>', out)
    return out


def money(x):
    return f"₹{x:,.0f}"


def fmt_vitals(d: dict) -> str:
    parts = []
    if d.get("sbp") or d.get("dbp"):
        parts.append(f"BP {d.get('sbp', '?')}/{d.get('dbp', '?')}")
    for k, lab in (("hr", "HR"), ("rr", "RR"), ("spo2", "SpO2"), ("fhr", "FHR")):
        if d.get(k) not in (None, ""):
            parts.append(f"{lab} {d[k]}")
    if d.get("temp_c") not in (None, ""):
        parts.append(f"T {d['temp_c']}°C")
    if d.get("temp_f") not in (None, ""):
        parts.append(f"T {d['temp_f']}°F")
    if d.get("avpu"):
        parts.append(f"AVPU {d['avpu']}")
    return ", ".join(parts)


def event_text(r) -> str:
    d = json.loads(r["data"])
    t = r["type"]
    if t == "vitals":
        extra = f" <small>(scanned note p{d.get('page')}: “{e(str(d.get('verbatim') or ''))}”)</small>" if r["source"] == "document" else ""
        return e(fmt_vitals(d)) + extra
    if t == "med_admin":
        return e(f"{d.get('name')} × {d.get('qty')} {d.get('unit', '')} — {d.get('dose', '')} {d.get('route', '')}".strip())
    if t == "service":
        return e(f"{d.get('name')} × {d.get('qty')}")
    return e(d.get("text", ""))


def page(title: str, body: str, role: str, msg: str = "", err: str = "") -> bytes:
    pending = CONN.execute("SELECT COUNT(*) FROM alerts WHERE status='pending'").fetchone()[0]
    opts = "".join(f'<option {"selected" if r == role else ""}>{e(r)}</option>' for r in ROLES)
    flash = (f'<div class="msg ok">{e(msg)}</div>' if msg else "") + (f'<div class="msg err">{e(err)}</div>' if err else "")
    mock = ' <span class="badge b-yellow">MOCK MODE – canned AI replies</span>' if config.MOCK else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · CareFlow AI</title><link rel="stylesheet" href="/static/style.css"></head><body>
<header><a class="logo" href="/">Care<span>Flow</span> AI</a>
<nav><a href="/">Ward</a><a href="/alerts">Consultant alerts {badge(pending, 'red') if pending else ''}</a><a href="/inventory">Inventory</a>
<a href="/usage">AI usage &amp; cost</a><a href="/rules">Red-flag rules</a><a href="/audit">Audit log</a></nav>
<form class="who" method="post" action="/role">Acting as <select name="role" onchange="this.form.submit()">{opts}</select></form></header>
<div class="banner">Prototype for demonstration with <b>synthetic patients only</b>. Not a medical device; red-flag thresholds are demo values. All AI output must be checked by a clinician.{mock}</div>
<main>{flash}{body}</main><footer>CareFlow AI · model {config.MODEL} via Portkey · MGT 409 final project</footer>
<script>document.querySelectorAll('form[data-busy]').forEach(f=>f.addEventListener('submit',()=>{{f.classList.add('busy');
f.querySelectorAll('button').forEach(b=>b.disabled=true);}}));</script></body></html>""".encode("utf-8")


# --------------------------------------------------------------------------- views
def view_ward(role, msg="", err=""):
    cards = []
    for p in CONN.execute("SELECT * FROM patients WHERE status='admitted' ORDER BY CAST(bed AS INTEGER)"):
        pend = CONN.execute("SELECT level FROM alerts WHERE patient_id=? AND status='pending' ORDER BY id DESC", (p["id"],)).fetchall()
        v = CONN.execute("SELECT * FROM events WHERE patient_id=? AND type='vitals' ORDER BY ts DESC, id DESC LIMIT 1", (p["id"],)).fetchone()
        tag = badge(f"{len(pend)} alert{'s' if len(pend) > 1 else ''} pending", "red" if any(a["level"] == "red" for a in pend) else "yellow") if pend else badge("no pending alerts", "green")
        last = f"<div class='muted' style='font-size:13px;margin-top:6px'>Last obs {e(v['ts'][5:16])}: {e(fmt_vitals(json.loads(v['data'])))}</div>" if v else ""
        cards.append(f"""<a class="pcard" href="/patient/{p['id']}"><div class="bed">Bed {e(p['bed'])} · {e(p['uhid'])} · {e(p['consultant'])}</div>
<div class="name">{e(p['name'])}, {p['age']}</div><div style="font-size:14px">{e(p['admitting_diagnosis'])}</div>
<div style="margin-top:8px">{tag}</div>{last}</a>""")
    used = llm.tokens_today(CONN)
    cost = CONN.execute("SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE substr(ts,1,10)=date('now','localtime')").fetchone()[0]
    low = CONN.execute("SELECT COUNT(*) FROM inventory WHERE stock<=reorder_level").fetchone()[0]
    return page("Ward", f"""<h1>Obstetric ward</h1><p class="sub">Pick a patient to read their notes, record bedside observations, see alerts, ask questions and draft handover or discharge documents.</p>
<div class="metrics"><div class="metric"><div class="v">{CONN.execute("SELECT COUNT(*) FROM patients WHERE status='admitted'").fetchone()[0]}</div><div class="l">patients admitted</div></div>
<div class="metric"><div class="v">{CONN.execute("SELECT COUNT(*) FROM alerts WHERE status='pending'").fetchone()[0]}</div><div class="l">alerts awaiting consultant</div></div>
<div class="metric"><div class="v">{used:,}</div><div class="l">AI tokens today (budget {config.DAILY_TOKEN_BUDGET:,})</div></div>
<div class="metric"><div class="v">${cost:.4f}</div><div class="l">AI cost today</div></div>
<div class="metric"><div class="v">{low}</div><div class="l">medicines at/below reorder level</div></div></div>
<div class="grid">{''.join(cards)}</div>""", role, msg, err)


def view_patient(pid: int, role: str, msg="", err="", qa_result=None):
    p = CONN.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    if not p:
        return None
    key = uuid.uuid4().hex
    parts = [f"""<p><a href="/">← Ward</a></p><h1>{e(p['name'])}, {p['age']}</h1>
<p class="sub">Bed {e(p['bed'])} · {e(p['uhid'])} · Consultant {e(p['consultant'])} · Admitted {e(p['admitted_at'])}<br>
<b>Admitting diagnosis:</b> {e(p['admitting_diagnosis'])}</p>
<div class="tabs"><a href="#docs">1 · Scanned notes</a><a href="#bedside">2 · Bedside entry</a><a href="#alerts">3 · Alerts</a>
<a href="#timeline">Timeline</a><a href="#ask">4 · Ask the record</a><a href="#drafts">5 · Handover / discharge</a><a href="#bill">6 · Bill</a></div>"""]

    # ---- 1. documents & extraction
    docs = CONN.execute("SELECT * FROM documents WHERE patient_id=? ORDER BY id DESC", (pid,)).fetchall()
    exts = {x["document_id"]: x for x in extract.latest(CONN, pid)}
    doc_rows = []
    for d in docs:
        x = exts.get(d["id"])
        status = (badge("read" + (" (reused, no AI cost)" if x["cached_from"] else ""), "green") if x else
                  f"""<form method="post" action="/patient/{pid}/extract" data-busy class="inline"><input type="hidden" name="document_id" value="{d['id']}">
<button class="small">Read with AI</button><span class="spinner">Reading handwriting… up to a minute</span></form>""")
        doc_rows.append(f"<tr><td>D{d['id']}</td><td>{e(d['filename'])}</td><td>{d['pages']}</td><td>{e(d['uploaded_at'])}</td><td>{status}</td></tr>")
    parts.append(f"""<section class="card" id="docs"><h2>1 · Scanned notes → structured record</h2>
<p class="muted">Upload OPD notes, progress notes or reports (PDF/JPG/PNG, ≤{config.MAX_PAGES} pages). The Record Reader agent transcribes them;
every field shows its source page and how sure it was. Fields marked for review need a clinician's check.</p>
<div class="two"><form method="post" action="/patient/{pid}/upload" enctype="multipart/form-data">
<label>Upload a scanned note</label><input type="file" name="file" accept=".pdf,.jpg,.jpeg,.png" required>
<div style="margin-top:8px"><button>Upload</button></div></form>
<form method="post" action="/patient/{pid}/sample"><label>No file handy? Attach a synthetic sample</label>
<select name="which"><option value="meera_sharma_opd_and_progress_note.pdf">Handwritten OPD + progress note (Meera Sharma)</option>
<option value="injection_test_nursing_note.png">Safety test: nursing note containing a prompt-injection attack</option></select>
<div style="margin-top:8px"><button class="secondary">Attach sample</button></div></form></div>
{'<table style="margin-top:12px"><tr><th>ID</th><th>File</th><th>Pages</th><th>Uploaded</th><th>Status</th></tr>' + ''.join(doc_rows) + '</table>' if doc_rows else ''}""")
    for x in extract.latest(CONN, pid):
        parts.append(render_extraction(pid, x))
    parts.append("</section>")

    # ---- 2. bedside entry
    inv = CONN.execute("SELECT * FROM inventory ORDER BY name").fetchall()
    tar = CONN.execute("SELECT * FROM tariff WHERE code<>'ROOM_GEN' ORDER BY name").fetchall()
    inv_opts = "".join(f'<option value="{e(i["item_code"])}">{e(i["name"])} (stock {i["stock"]}, {money(i["unit_price"])}/{e(i["unit"])})</option>' for i in inv)
    tar_opts = "".join(f'<option value="{e(t["code"])}">{e(t["name"])} – {money(t["price"])}</option>' for t in tar)
    parts.append(f"""<section class="card" id="bedside"><h2>2 · Bedside entry <small>(nurse / resident)</small></h2>
<div class="two"><form method="post" action="/patient/{pid}/event"><input type="hidden" name="type" value="vitals"><input type="hidden" name="key" value="v-{key}">
<h3>Observations</h3><div class="row"><div><label>Systolic BP</label><input name="sbp" type="number"></div><div><label>Diastolic BP</label><input name="dbp" type="number"></div>
<div><label>Pulse /min</label><input name="hr" type="number"></div><div><label>Resp. rate</label><input name="rr" type="number"></div></div>
<div class="row"><div><label>Temp °C</label><input name="temp_c" type="number" step="0.1"></div><div><label>SpO2 %</label><input name="spo2" type="number"></div>
<div><label>Fetal HR</label><input name="fhr" type="number"></div><div><label>AVPU</label><select name="avpu"><option value="alert">Alert</option><option value="voice">Voice</option><option value="pain">Pain</option><option value="unresponsive">Unresponsive</option></select></div></div>
<button>Save observations</button> <small>Checked instantly against the red-flag rules.</small></form>
<div><form method="post" action="/patient/{pid}/event"><input type="hidden" name="type" value="med_admin"><input type="hidden" name="key" value="m-{key}">
<h3>Medicine given</h3><div class="row"><div style="grid-column:span 2"><label>Medicine (from pharmacy stock)</label><select name="item_code">{inv_opts}</select></div></div>
<div class="row"><div><label>Units</label><input name="qty" type="number" value="1" min="1"></div><div><label>Dose</label><input name="dose" placeholder="e.g. 100 mg"></div>
<div><label>Route</label><select name="route"><option>oral</option><option>IV</option><option>IM</option><option>SC</option><option>PV</option></select></div></div>
<button>Record administration</button> <small>Deducts stock and adds to bill once.</small></form>
<form method="post" action="/patient/{pid}/event" style="margin-top:14px"><input type="hidden" name="type" value="service"><input type="hidden" name="key" value="s-{key}">
<h3>Service / investigation</h3><div class="row"><div style="grid-column:span 2"><select name="code">{tar_opts}</select></div><div><input name="qty" type="number" value="1" min="1"></div></div>
<button class="secondary">Add service</button></form>
<form method="post" action="/patient/{pid}/event" style="margin-top:14px"><input type="hidden" name="type" value="note"><input type="hidden" name="key" value="n-{key}">
<h3>Clinical note / discharge instruction</h3><textarea name="text" placeholder="e.g. Review by consultant: continue labetalol 200 mg BD. Plan discharge day 3, follow-up OPD 1 week."></textarea>
<button class="secondary" style="margin-top:6px">Add note</button></form></div></div></section>""")

    # ---- 3. alerts
    al = CONN.execute("SELECT * FROM alerts WHERE patient_id=? ORDER BY ts DESC, id DESC", (pid,)).fetchall()
    parts.append(f'<section class="card" id="alerts"><h2>3 · Red-flag alerts → consultant</h2>'
                 + ("".join(render_alert(a, role) for a in al) or "<p class='muted'>No alerts.</p>") + "</section>")

    # ---- timeline
    rows = []
    for r in CONN.execute("SELECT * FROM events WHERE patient_id=? ORDER BY ts DESC, id DESC", (pid,)):
        src = badge("from scanned note (AI)", "blue") if r["source"] == "document" else badge(r["source"], "grey")
        rows.append(f"<tr><td class='mono'>E{r['id']}</td><td>{e(r['ts'])}</td><td>{e(r['type'])}</td><td>{event_text(r)}</td><td>{e(r['entered_by'] or '')} {src}</td></tr>")
    parts.append(f"""<section class="card" id="timeline"><h2>Timeline</h2><table><tr><th>ID</th><th>Time</th><th>Type</th><th>Detail</th><th>By / source</th></tr>{''.join(rows)}</table></section>""")

    # ---- 4. ask
    ans = ""
    if qa_result:
        chk = qa_result["check"]
        flags = []
        if chk["invalid"]:
            flags.append(f'<div class="danger-box">Citations not found in this record: {e(", ".join(chk["invalid"]))} – treat those statements as unreliable.</div>')
        if not chk["has_citations"]:
            flags.append('<div class="warn">This answer contains no citations. Verify before relying on it.</div>')
        trace = " → ".join(f"{t['tool']}({', '.join(f'{k}={v}' for k, v in t['args'].items())})" for t in qa_result["trace"]) or "no tools"
        ans = f"""<h3>Q: {e(qa_result['question'])}</h3><div class="answer">{cite_html(qa_result['answer'])}</div>{''.join(flags)}
<p class="muted" style="font-size:13px">Agent steps: {qa_result['steps']} · tools used: {e(trace)}</p>"""
    parts.append(f"""<section class="card" id="ask"><h2>4 · Ask the record</h2>
<p class="muted">Answers come only from this patient's record, with citations: <span class="cite">E12</span> bedside event, <span class="cite">D2.p1</span> scanned document page, <span class="cite">A3</span> alert.</p>
<form method="post" action="/patient/{pid}/ask" data-busy><div class="row"><div style="grid-column:span 5"><input name="q" required placeholder="e.g. What is the BP trend and has the consultant seen the alert? Any allergies?"></div>
<div><button>Ask</button><span class="spinner">Thinking…</span></div></div></form>
<p class="muted" style="font-size:13px">Try: “Any drug allergies?” · “Summarise BP since admission” · “What medicines has she actually received?” · “Is pre-eclampsia confirmed?”</p>{ans}</section>""")

    # ---- 5. drafts
    dr = CONN.execute("SELECT * FROM drafts WHERE patient_id=? ORDER BY id DESC LIMIT 4", (pid,)).fetchall()
    parts.append(f"""<section class="card" id="drafts"><h2>5 · Handover &amp; discharge drafts</h2>
<p class="muted">The Summary Writer drafts from the accumulated record. A grounding check then verifies every citation and number against the record. Nothing is final until a clinician approves it.</p>
<form method="post" action="/patient/{pid}/draft" data-busy class="inline"><input type="hidden" name="kind" value="handover"><button>Draft shift handover</button></form>
<form method="post" action="/patient/{pid}/draft" data-busy class="inline"><input type="hidden" name="kind" value="discharge"><button class="secondary">Draft discharge summary</button><span class="spinner">Drafting…</span></form>
{''.join(render_draft(d) for d in dr)}</section>""")

    # ---- 6. bill
    b = events.bill(CONN, pid)
    lines = "".join(f"<tr><td>{e(l['ts'][:16])}</td><td>{e(l['description'])}</td><td class='num'>{l['qty']:g}</td><td class='num'>{money(l['unit_price'])}</td>"
                    f"<td class='num'>{money(l['amount'])}</td><td class='mono'>{'E' + str(l['event_id']) if l['event_id'] else 'room'}</td></tr>" for l in b["lines"])
    parts.append(f"""<section class="card" id="bill"><h2>6 · Running bill (INR)</h2><p class="muted">Calculated by fixed rules from recorded services and administered medicines only – a medicine merely mentioned in a note is never billed.</p>
<table><tr><th>Date</th><th>Item</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th><th>Source</th></tr>{lines}
<tr><th colspan="4">Total</th><th class="num">{money(b['total'])}</th><th></th></tr></table></section>""")
    return page(p["name"], "".join(parts), role, msg, err)


def render_extraction(pid, x):
    d = x["data"]
    reviews = {r["field_key"]: r for r in CONN.execute("SELECT * FROM field_reviews WHERE extraction_id=? ORDER BY id", (x["id"],))}
    total, review = extract.review_count(d)
    rows = []

    def row(label, key, f):
        if not f:
            return
        rv = reviews.get(key)
        extra = " · ".join(f"{k}: {f[k]}" for k in ("certainty", "dose", "frequency", "route", "status", "result", "date") if f.get(k))
        value = e(f["value"]) + (f"<div class='muted' style='font-size:12px'>{e(extra)}</div>" if extra else "")
        if rv:
            state = badge(f"{rv['status']} by {rv['reviewer']}", "green" if rv["status"] == "confirmed" else "blue")
            if rv["corrected_value"]:
                value = f"<s class='muted'>{e(f['value'])}</s> {e(rv['corrected_value'])}"
        elif f.get("needs_review"):
            state = f"""{badge('needs review', 'yellow')}<form method="post" action="/patient/{pid}/review" class="inline">
<input type="hidden" name="extraction_id" value="{x['id']}"><input type="hidden" name="field_key" value="{e(key)}">
<input name="corrected" placeholder="correction (optional)" style="width:150px;padding:3px 6px;font-size:12px"> <button class="small secondary">Confirm</button></form>"""
        else:
            state = badge("ok", "green")
        rows.append(f"<tr><td>{e(label)}</td><td>{value}</td><td class='mono'>“{e(f.get('verbatim') or '')}”</td>"
                    f"<td>{'p' + str(f['page']) if f.get('page') else '<b>?</b>'}</td><td>{conf_badge(f.get('confidence'))}</td><td>{state}</td></tr>")

    for k, f in d["patient"].items():
        row(f"Patient · {k}", f"patient.{k}", f)
    for k, f in d["obstetric"].items():
        row(f"Obstetric · {k.replace('_', ' ')}", f"obstetric.{k}", f)
    for sec in extract.FIELD_LISTS:
        for i, f in enumerate(d[sec]):
            row(sec.replace("_", " ").capitalize(), f"{sec}.{i}", f)
    row("Clinician", "clinician", d.get("clinician"))
    vit = "".join(f"<li>{e(v.get('datetime') or 'time not written')}: {e(fmt_vitals({**v, 'temp_f' if (v.get('temp') or 0) > 45 else 'temp_c': v.get('temp')}))} "
                  f"<small>(p{v.get('page')}, {e(v.get('confidence'))})</small></li>" for v in d["vitals"])
    warn = ""
    if d.get("embedded_instructions"):
        items = "".join(f"<li>p{e(str(i.get('page')))}: “{e(str(i.get('text'))[:200])}”</li>" for i in d["embedded_instructions"])
        warn = (f'<div class="danger-box"><b>Instructions found inside the document were ignored (possible prompt injection).</b>'
                f'<ul>{items}</ul>No action was taken; the AI has no ability to change bills, alerts or discharge status.</div>')
    ill = ""
    if d.get("illegible"):
        ill = '<div class="warn"><b>Could not read:</b><ul>' + "".join(f"<li>p{e(str(i.get('page')))}: {e(str(i.get('description')))}</li>" for i in d["illegible"]) + "</ul></div>"
    probs = f'<div class="warn">Validation: {e("; ".join(d["validation_problems"]))}</div>' if d.get("validation_problems") else ""
    return f"""<h3 style="margin-top:20px">Extracted from D{x['document_id']} · {e(x['filename'])} <small class="muted">({total} fields, {review} need review)</small></h3>
{warn}{ill}{probs}<table><tr><th>Field</th><th>Value</th><th>As written</th><th>Page</th><th>Confidence</th><th>Review</th></tr>{''.join(rows)}</table>
{'<h3>Vital signs found in the note</h3><ul>' + vit + '</ul><p class="muted" style="font-size:13px">These were sent through the same red-flag rules as bedside entries (see Alerts).</p>' if vit else ''}"""


def render_alert(a, role):
    reasons = json.loads(a["reasons"])
    cls = "alert" + (" yellow" if a["level"] == "yellow" else "") + (" ack" if a["status"] != "pending" else "")
    p = CONN.execute("SELECT name, bed FROM patients WHERE id=?", (a["patient_id"],)).fetchone()
    if a["status"] == "pending":
        if role == "Consultant":
            action = (f'<form method="post" action="/alerts/{a["id"]}/ack" class="inline" style="float:right">'
                      f'<input type="hidden" name="back" value="/patient/{a["patient_id"]}#alerts"><button class="small">Acknowledge</button></form>')
        else:
            action = '<span style="float:right">' + badge("pending consultant review", "red") + "</span>"
    else:
        action = '<span style="float:right">' + badge(f"acknowledged by {a['acknowledged_by']} at {a['acknowledged_at'][11:16]}", "green") + "</span>"
    src = " · " + badge("from scanned note – verify", "blue") if a["source"] == "document" else ""
    return f"""<div class="{cls}">{action}<b>{e(a['level'].upper())} · A{a['id']}</b> · {e(a['ts'])} · <a href="/patient/{a['patient_id']}">{e(p['name'])}, bed {e(p['bed'])}</a>{src}
<div style="font-size:14px;margin-top:4px">{'<br>'.join(e(r) for r in reasons)}</div><div class="muted" style="font-size:12px">Score {e(a['score'] or '')} · triggered by E{a['event_id']}</div></div>"""


def render_draft(d):
    sections = json.loads(d["content"])
    g = json.loads(d["grounding"])
    title = drafts.KINDS[d["kind"]]["title"]
    ok = badge("grounding check passed", "green") if g["passed"] else badge("grounding check: issues", "red")
    issues = ""
    if g["unsupported_numbers"]:
        issues += f'<div class="danger-box">Numbers not found anywhere in the record (highlighted): {e(", ".join(g["unsupported_numbers"]))}. Check before use.</div>'
    if g["invalid_citations"]:
        issues += f'<div class="danger-box">Invalid citations: {e(", ".join(g["invalid_citations"]))}</div>'
    if g["sections_without_citations"]:
        issues += f'<div class="warn">Sections without citations: {e(", ".join(g["sections_without_citations"]))}</div>'
    body = "".join(f"<h4>{e(s['heading'])}</h4><div>{cite_html(s['text'], g['unsupported_numbers'])}</div>" for s in sections)
    if d["status"] == "approved":
        state = badge(f"APPROVED by {d['approved_by']} · {d['approved_at']}", "green")
    else:
        state = (badge("DRAFT – not for use until approved", "yellow") +
                 f"""<form method="post" action="/drafts/{d['id']}/approve" class="inline" style="margin-left:8px"><input name="clinician" required placeholder="Approving clinician name" style="width:220px;padding:4px 8px">
<input type="hidden" name="back" value="/patient/{d['patient_id']}#drafts"> <button class="small">Approve</button></form>""")
    return f"""<div class="draft" style="margin-top:14px"><b>{e(title)}</b> · D-{d['id']} · {e(d['created_at'])} · {ok}<div style="margin:6px 0">{state}</div>{issues}{body}</div>"""


def view_alerts(role):
    pend = CONN.execute("SELECT * FROM alerts WHERE status='pending' ORDER BY CASE level WHEN 'red' THEN 0 ELSE 1 END, ts DESC").fetchall()
    done = CONN.execute("SELECT * FROM alerts WHERE status<>'pending' ORDER BY acknowledged_at DESC LIMIT 20").fetchall()
    hint = "" if role == "Consultant" else '<div class="warn">Switch “Acting as” to <b>Consultant</b> (top right) to acknowledge alerts.</div>'
    return page("Consultant alerts", f"""<h1>Consultant alerts</h1><p class="sub">Raised automatically by fixed rules (not by AI) whenever observations cross the MEOWS-based thresholds.</p>{hint}
<h2>Pending ({len(pend)})</h2>{''.join(render_alert(a, role) for a in pend) or '<p class="muted">None pending.</p>'}
<h2 style="margin-top:20px">Recently acknowledged</h2>{''.join(render_alert(a, role) for a in done) or '<p class="muted">None.</p>'}""", role)


def view_inventory(role):
    rows = "".join(f"<tr><td class='mono'>{e(i['item_code'])}</td><td>{e(i['name'])}</td><td class='num'>{i['stock']} {e(i['unit'])}</td><td class='num'>{i['reorder_level']}</td>"
                   f"<td class='num'>{money(i['unit_price'])}</td><td>{badge('reorder', 'red') if i['stock'] <= i['reorder_level'] else badge('ok', 'green')}</td></tr>"
                   for i in CONN.execute("SELECT * FROM inventory ORDER BY name"))
    led = "".join(f"<tr><td>{e(r['ts'])}</td><td class='mono'>{e(r['item_code'])}</td><td class='num'>{r['delta']}</td><td class='mono'>E{r['event_id']}</td><td>{e(r['reason'])}</td></tr>"
                  for r in CONN.execute("SELECT * FROM stock_ledger ORDER BY id DESC LIMIT 40"))
    return page("Inventory", f"""<h1>Pharmacy stock (simulated)</h1><p class="sub">Stock moves only when a nurse records an administration. Each event can move stock once (database-enforced).</p>
<div class="card"><table><tr><th>Code</th><th>Medicine</th><th class="num">In stock</th><th class="num">Reorder at</th><th class="num">Price</th><th></th></tr>{rows}</table></div>
<div class="card"><h2>Stock ledger</h2><table><tr><th>Time</th><th>Item</th><th class="num">Change</th><th>Event</th><th>Reason</th></tr>{led}</table></div>""", role)


def view_usage(role):
    by = CONN.execute("""SELECT feature, COUNT(*) n, SUM(input_tokens) i, SUM(cached_tokens) c, SUM(output_tokens) o, SUM(cost_usd) cost,
        AVG(latency_s) lat, SUM(CASE WHEN note='mock' THEN 1 ELSE 0 END) mocks FROM usage WHERE ok=1 GROUP BY feature ORDER BY feature""").fetchall()
    rows = "".join(f"<tr><td>{e(r['feature'])}</td><td class='num'>{r['n']}</td><td class='num'>{r['i']:,}</td><td class='num'>{r['c']:,}</td><td class='num'>{r['o']:,}</td>"
                   f"<td class='num'>{(r['i'] + r['o']) // r['n']:,}</td><td class='num'>${r['cost']:.5f}</td><td class='num'>{r['lat']:.1f}s</td><td>{badge('mock', 'yellow') if r['mocks'] else ''}</td></tr>" for r in by)
    recent = "".join(f"<tr><td>{e(r['ts'])}</td><td>{e(r['feature'])}</td><td>{r['patient_id'] or ''}</td><td class='num'>{r['input_tokens']:,}</td><td class='num'>{r['cached_tokens']:,}</td>"
                     f"<td class='num'>{r['output_tokens']:,}</td><td class='num'>${r['cost_usd']:.5f}</td><td>{badge('ok', 'green') if r['ok'] else badge('failed', 'red')} {e(r['note'] or '')}</td></tr>"
                     for r in CONN.execute("SELECT * FROM usage ORDER BY id DESC LIMIT 40"))
    return page("AI usage", f"""<h1>AI usage &amp; cost</h1><p class="sub">Every model call is logged with real token counts returned by the API. Prices: ${config.PRICE_INPUT}/M input, ${config.PRICE_CACHED_INPUT}/M cached input, ${config.PRICE_OUTPUT}/M output ({config.MODEL}).</p>
<div class="metrics"><div class="metric"><div class="v">{llm.tokens_today(CONN):,}</div><div class="l">tokens today</div></div><div class="metric"><div class="v">{config.DAILY_TOKEN_BUDGET:,}</div><div class="l">daily budget (whole app)</div></div>
<div class="metric"><div class="v">{config.PER_PATIENT_DAILY_BUDGET:,}</div><div class="l">daily budget per patient</div></div><div class="metric"><div class="v">{config.MAX_AGENT_STEPS}</div><div class="l">max tool rounds per question</div></div></div>
<div class="card"><h2>By feature</h2><table><tr><th>Feature</th><th class="num">Calls</th><th class="num">Input</th><th class="num">Cached input</th><th class="num">Output</th><th class="num">Avg tokens/call</th><th class="num">Cost</th><th class="num">Avg latency</th><th></th></tr>{rows}</table></div>
<div class="card"><h2>Recent calls</h2><table><tr><th>Time</th><th>Feature</th><th>Patient</th><th class="num">Input</th><th class="num">Cached</th><th class="num">Output</th><th class="num">Cost</th><th>Status</th></tr>{recent}</table></div>""", role)


def view_rules(role):
    rows = "".join(f"<tr><td>{e(a)}</td><td>{badge(b, 'red')}</td><td>{badge(c, 'yellow') if c != '—' else '—'}</td></tr>" for a, b, c in rules.THRESHOLD_TABLE)
    return page("Red-flag rules", f"""<h1>Red-flag rules</h1><p class="sub">Deterministic code, not AI. An alert is raised for <b>any one red</b> value or <b>two or more yellow</b> values in one set of observations.</p>
<div class="card"><table><tr><th>Parameter</th><th>Red</th><th>Yellow</th></tr>{rows}</table>
<p class="muted" style="font-size:13px">Based on the Modified Early Obstetric Warning Score (Singh et al., <i>Anaesthesia</i> 2012) and FIGO fetal heart rate guidance. Demo thresholds – each hospital must set and validate its own.</p></div>""", role)


def view_audit(role):
    rows = "".join(f"<tr><td>{e(r['ts'])}</td><td>{e(r['actor'])}</td><td>{e(r['action'])}</td><td class='mono'>{e(r['detail'][:160])}</td></tr>"
                   for r in CONN.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100"))
    return page("Audit", f"<h1>Audit log</h1><p class='sub'>Who did what, when.</p><div class='card'><table><tr><th>Time</th><th>Actor</th><th>Action</th><th>Detail</th></tr>{rows}</table></div>", role)


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "CareFlow/1.0"

    def log_message(self, fmt, *args):  # quieter console
        sys.stderr.write("  %s %s\n" % (self.command, self.path))

    @property
    def role(self) -> str:
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        r = c["role"].value if "role" in c else ROLES[0]
        return r if r in ROLES else ROLES[0]

    def send_html(self, body: bytes, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, url, msg="", err="", cookie=None):
        if msg or err:
            base, _, frag = url.partition("#")
            url = base + ("&" if "?" in base else "?") + (f"msg={quote(msg)}" if msg else f"err={quote(err)}") + (f"#{frag}" if frag else "")
        self.send_response(303)
        self.send_header("Location", url)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        msg, err = qs.get("msg", [""])[0], qs.get("err", [""])[0]
        try:
            if u.path == "/static/style.css":
                body = (config.ROOT / "static" / "style.css").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/css")
                self.end_headers()
                self.wfile.write(body)
                return
            with LOCK:
                if u.path == "/":
                    return self.send_html(view_ward(self.role, msg, err))
                m = re.fullmatch(r"/patient/(\d+)", u.path)
                if m:
                    body = view_patient(int(m.group(1)), self.role, msg, err)
                    return self.send_html(body) if body else self.send_html(page("Not found", "<h1>Patient not found</h1>", self.role), 404)
                views = {"/alerts": view_alerts, "/inventory": view_inventory, "/usage": view_usage, "/rules": view_rules, "/audit": view_audit}
                if u.path in views:
                    return self.send_html(views[u.path](self.role))
            self.send_html(page("Not found", "<h1>Not found</h1>", self.role), 404)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self.send_html(page("Error", "<h1>Something went wrong</h1><p>See the terminal for details.</p>", self.role), 500)

    def form(self) -> tuple[dict, dict]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > (config.MAX_UPLOAD_MB + 1) * 1024 * 1024:
            raise ValueError("Upload too large.")
        raw = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw, policy=email.policy.HTTP)
            fields, files = {}, {}
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if part.get_filename():
                    files[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
                else:
                    fields[name] = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            return fields, files
        return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}, {}

    def do_POST(self):
        u = urlparse(self.path)
        try:
            f, files = self.form()
        except ValueError as exc:
            return self.redirect("/", err=str(exc))
        actor = self.role
        try:
            if u.path == "/role":
                return self.redirect(self.headers.get("Referer") or "/", cookie=f"role={quote(f.get('role', ROLES[0]))}; Path=/; SameSite=Lax")
            m = re.fullmatch(r"/alerts/(\d+)/ack", u.path)
            if m:
                if actor != "Consultant":
                    return self.redirect(f.get("back") or "/alerts", err="Only a consultant can acknowledge alerts.")
                with LOCK:
                    events.acknowledge_alert(CONN, int(m.group(1)), "Consultant (demo)")
                return self.redirect(f.get("back") or "/alerts", msg="Alert acknowledged.")
            m = re.fullmatch(r"/drafts/(\d+)/approve", u.path)
            if m:
                name = (f.get("clinician") or "").strip()
                if actor == "Nurse" or len(name) < 3:
                    return self.redirect(f.get("back") or "/", err="A doctor must enter their name to approve.")
                with LOCK:
                    drafts.approve(CONN, int(m.group(1)), f"{name} ({actor})")
                return self.redirect(f.get("back") or "/", msg="Draft approved.")
            m = re.fullmatch(r"/patient/(\d+)/(\w+)", u.path)
            if not m:
                return self.redirect("/", err="Unknown action.")
            pid, action = int(m.group(1)), m.group(2)
            back = f"/patient/{pid}"
            if not CONN.execute("SELECT 1 FROM patients WHERE id=?", (pid,)).fetchone():
                return self.redirect("/", err="Unknown patient.")
            if action == "event":
                return self.post_event(pid, f, actor, back)
            if action in ("upload", "sample"):
                if action == "sample":
                    samples = {p.name: p for p in (config.DATA / "samples").iterdir()}  # whitelist, no path traversal
                    path = samples.get(f.get("which") or "meera_sharma_opd_and_progress_note.pdf")
                    if not path:
                        return self.redirect(back + "#docs", err="Unknown sample.")
                    content, fname = path.read_bytes(), path.name
                else:
                    fname, content = files.get("file", ("", b""))
                    if not content:
                        return self.redirect(back + "#docs", err="Choose a file first.")
                with LOCK:
                    did = extract.add_document(CONN, pid, fname, content, actor)
                return self.redirect(back + "#docs", msg=f"Uploaded as D{did}. Click “Read with AI”.")
            if action == "extract":
                with LOCK:
                    r = extract.run(CONN, pid, int(f["document_id"]), actor)
                total, review = extract.review_count(r["data"])
                note = " (identical file read before – reused, no AI cost)" if r["reused"] else ""
                inj = " Instructions inside the document were detected and ignored." if r["data"].get("embedded_instructions") else ""
                return self.redirect(back + "#docs", msg=f"Read {total} fields; {review} need review; {r['vitals_events']} vital-sign sets checked against red-flag rules.{note}{inj}")
            if action == "review":
                with LOCK:
                    corrected = (f.get("corrected") or "").strip()
                    CONN.execute("INSERT INTO field_reviews (extraction_id, field_key, status, corrected_value, reviewer, reviewed_at) VALUES (?,?,?,?,?,?)",
                                 (int(f["extraction_id"]), f["field_key"], "corrected" if corrected else "confirmed", corrected or None, actor, db.now()))
                    db.audit(CONN, actor, "field_reviewed", {"extraction_id": f["extraction_id"], "field": f["field_key"], "corrected": bool(corrected)})
                    CONN.commit()
                return self.redirect(back + "#docs", msg="Field reviewed.")
            if action == "ask":
                q = (f.get("q") or "").strip()
                if not q:
                    return self.redirect(back + "#ask", err="Type a question.")
                with LOCK:
                    res = qa.ask(CONN, pid, q)
                    res["question"] = q
                    return self.send_html(view_patient(pid, actor, qa_result=res))
            if action == "draft":
                kind = f.get("kind")
                if kind not in drafts.KINDS:
                    return self.redirect(back, err="Unknown draft type.")
                with LOCK:
                    drafts.generate(CONN, pid, kind, actor)
                return self.redirect(back + "#drafts", msg="Draft created – review the grounding check before approving.")
            return self.redirect(back, err="Unknown action.")
        except (llm.BudgetExceeded, llm.ModelUnavailable, events.EventError, ValueError) as exc:
            target = u.path.rsplit("/", 1)[0] if u.path.startswith("/patient/") else "/"
            return self.redirect(target, err=str(exc))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self.redirect("/", err=f"Unexpected error: {type(exc).__name__}. See terminal.")

    def post_event(self, pid, f, actor, back):
        t = f.get("type")
        key = f"form-{f.get('key')}" if f.get("key") else None
        if t == "vitals":
            data = {}
            for k in ("sbp", "dbp", "hr", "rr", "spo2", "fhr", "temp_c"):
                if f.get(k, "").strip():
                    try:
                        data[k] = float(f[k]) if k == "temp_c" else int(float(f[k]))
                    except ValueError:
                        return self.redirect(back + "#bedside", err=f"{k} must be a number.")
            if not data:
                return self.redirect(back + "#bedside", err="Enter at least one observation.")
            data["avpu"] = f.get("avpu", "alert")
            with LOCK:
                r = events.record_event(CONN, pid, "vitals", data, entered_by=actor, key=key)
            if r["duplicate"]:
                return self.redirect(back + "#timeline", msg="Already saved (duplicate submission ignored).")
            return self.redirect(back + ("#alerts" if r["alert_id"] else "#timeline"),
                                 msg=f"Observations saved as E{r['event_id']}." + (f" RED FLAG raised (A{r['alert_id']}) – consultant notified." if r["alert_id"] else " No red flags."))
        if t == "med_admin":
            data = {"item_code": f.get("item_code"), "qty": f.get("qty"), "dose": f.get("dose", ""), "route": f.get("route", "")}
        elif t == "service":
            data = {"code": f.get("code"), "qty": f.get("qty")}
        elif t == "note":
            text = (f.get("text") or "").strip()
            if not text:
                return self.redirect(back + "#bedside", err="Note is empty.")
            data = {"text": text[:4000]}
        else:
            return self.redirect(back, err="Unknown entry type.")
        with LOCK:
            r = events.record_event(CONN, pid, t, data, entered_by=actor, key=key)
        if r["duplicate"]:
            return self.redirect(back + "#timeline", msg="Already saved (duplicate submission ignored).")
        return self.redirect(back + "#timeline", msg=f"Saved as E{r['event_id']}.")


def main():
    port = 8050
    for a in sys.argv[1:]:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    if not config.MOCK and not config.api_key():
        print("WARNING: PORTKEY_API_KEY not found in .env (repo folder or the folder above). AI buttons will show an error.")
    print(f"CareFlow AI running at http://127.0.0.1:{port}   (Ctrl+C to stop)")
    print(f".env files loaded: {', '.join(config.ENV_FILES) or 'none'}")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
