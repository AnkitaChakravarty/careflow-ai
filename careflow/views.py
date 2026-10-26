"""HTML views (server-rendered, no JavaScript framework). Every view receives the logged-in user;
what is shown and which buttons appear depend on the user's role, and app.py re-checks every action."""
from __future__ import annotations

import html
import json
import re
import uuid
from datetime import datetime

from . import auth, config, discharge, drafts, events, extract, intake, llm, record, reminders, rules, vaccines
from .db import hours_since, parse_ts

e = html.escape
CONN = None  # set by app.py


# ============================================================================ helpers
def badge(text, kind="grey"):
    return f'<span class="badge b-{kind}">{e(str(text))}</span>'


def conf_badge(c):
    return badge(c or "low", {"high": "green", "medium": "yellow", "low": "red"}.get(c, "red"))


def money(x):
    return f"₹{x:,.0f}"


def dt(ts: str | None, with_date=True) -> str:
    if not ts:
        return ""
    try:
        d = parse_ts(ts)
    except ValueError:
        return e(ts)
    return d.strftime("%d %b %H:%M" if with_date else "%H:%M")


def ago(ts: str) -> str:
    h = hours_since(ts)
    if h < 1:
        return f"{max(0, int(h * 60))} min ago"
    if h < 48:
        return f"{h:.0f} h ago"
    return f"{h / 24:.0f} days ago"


def cite_html(text: str, unsupported: list | None = None) -> str:
    out = e(text)
    out = re.sub(r"\[([A-Z]\d+(?:\.p[\d?]+)?(?:\s*[,;]\s*[A-Z]\d+(?:\.p[\d?]+)?)*)\]",
                 lambda m: "".join(f'<span class="cite">{c.strip()}</span>' for c in re.split(r"[,;]", m.group(1))), out)
    for n in unsupported or []:
        out = re.sub(rf"(?<![\d.]){re.escape(n)}(?![\d.])", f'<span class="unsup" title="Number not found in record">{n}</span>', out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    return out


def fmt_obs(d: dict) -> str:
    parts = []
    if d.get("sbp") or d.get("dbp"):
        parts.append(f"BP {d.get('sbp', '?')}/{d.get('dbp', '?')}")
    for k, lab in (("hr", "HR"), ("rr", "RR"), ("spo2", "SpO₂"), ("fhr", "FHR")):
        if d.get(k) not in (None, ""):
            parts.append(f"{lab} {d[k]}")
    if d.get("temp_c") not in (None, ""):
        parts.append(f"T {d['temp_c']}°C")
    if d.get("temp_f") not in (None, ""):
        parts.append(f"T {d['temp_f']}°F")
    if d.get("weight_g"):
        parts.append(f"Wt {d['weight_g']} g")
    if d.get("apgar1") is not None and d.get("at_birth"):
        parts.append(f"APGAR {d.get('apgar1')}/{d.get('apgar5')}")
    for k, table in (("feeding", rules.FEEDING), ("jaundice", rules.JAUNDICE), ("activity", rules.ACTIVITY), ("cord", rules.CORD)):
        if d.get(k):
            parts.append(("Jaundice: " if k == "jaundice" else "Cord: " if k == "cord" else "") + table.get(d[k], d[k]))
    if d.get("chest_indrawing"):
        parts.append("chest indrawing")
    if d.get("avpu") and d["avpu"] != "alert":
        parts.append(f"AVPU {d['avpu']}")
    return ", ".join(parts)


def event_text(r) -> str:
    d = json.loads(r["data"])
    t = r["type"]
    if t in ("vitals", "newborn_obs"):
        extra = f" <small>(paper p{d.get('page')}: “{e(str(d.get('verbatim') or ''))}”)</small>" if r["source"] == "document" else ""
        return e(fmt_obs(d)) + extra
    if t in ("med_admin", "vaccine"):
        s = e(f"{d.get('vaccine') or d.get('name')} × {d.get('qty')} {d.get('unit', '')} — {d.get('dose', '')} {d.get('route', '')}".strip())
        if d.get("allergy_override"):
            s += " " + badge("allergy override", "red") + f" <small>“{e(d['allergy_override']['reason_given'])}”</small>"
        return s
    if t == "service":
        return e(f"{d.get('name')} × {d.get('qty'):g}")
    if t == "investigation":
        vals = ", ".join(f"{rules.LAB_FIELDS[k][0]} {v:g}" if k in rules.LAB_FIELDS else f"Urine protein {v}" for k, v in d["values"].items())
        return f"<b>{e(d['test'])}</b>: {e(vals)} {e(d.get('result_text') or '')}"
    if t == "labour":
        bits = []
        if d.get("dilatation_cm") is not None:
            bits.append(f"Cx {d['dilatation_cm']:g} cm")
        if d.get("descent_fifths") is not None:
            bits.append(f"descent {d['descent_fifths']}/5")
        if d.get("contractions_10min") is not None:
            bits.append(f"{d['contractions_10min']} contr/10 min" + (f" ({d['contraction_secs']} s)" if d.get("contraction_secs") else ""))
        if d.get("fhr"):
            bits.append(f"FHR {d['fhr']}")
        if d.get("liquor"):
            bits.append(f"liquor {rules.LIQUOR[d['liquor']].lower()}")
        if d.get("moulding"):
            bits.append(f"moulding {d['moulding']}")
        if d.get("oxytocin"):
            bits.append(f"oxytocin {d['oxytocin']}")
        m = fmt_obs({k: d.get(k) for k in ("sbp", "dbp", "hr", "temp_c")})
        return e(", ".join(bits) + (f" · maternal {m}" if m else ""))
    if t == "delivery":
        return (f"<b>{e(events.DELIVERY_MODES[d['mode']])}</b> – {e(events.OUTCOMES[d['outcome']])}"
                f"{', ' + ('boy' if d.get('sex') == 'M' else 'girl' if d.get('sex') == 'F' else '') if d.get('sex') else ''}"
                f" {d['weight_g']} g" + (f", APGAR {d.get('apgar1')}/{d.get('apgar5')}" if d.get("apgar5") is not None else "")
                + f", blood loss {d['ebl_ml']} ml")
    cat = d.get("category")
    return (badge(cat.replace("_", " "), "blue") + " " if cat else "") + e(d.get("text", ""))


EVENT_LABEL = {"vitals": "Observations", "newborn_obs": "Newborn obs", "med_admin": "Medicine", "vaccine": "Vaccine",
               "service": "Service", "investigation": "Lab result", "labour": "Labour", "delivery": "Delivery", "note": "Note"}


# ============================================================================ layout
def page(title: str, body: str, user: dict | None, msg: str = "", err: str = "", bare: bool = False) -> bytes:
    flash = (f'<div class="msg ok">{e(msg)}</div>' if msg else "") + (f'<div class="msg err">{e(err)}</div>' if err else "")
    mock = ' <span class="badge b-yellow">MOCK MODE – canned AI replies</span>' if config.MOCK else ""
    nav = ""
    if user and not bare:
        pending = CONN.execute("SELECT COUNT(*) FROM alerts WHERE status='pending'").fetchone()[0]
        red = CONN.execute("SELECT COUNT(*) FROM alerts WHERE status='pending' AND level='red'").fetchone()[0]
        links = []
        if auth.can(user, "view_clinical"):
            links += [("/", "Ward"), ("/assistant", "Ask CareFlow"),
                      ("/alerts", f"Alerts {badge(pending, 'red' if red else 'yellow') if pending else ''}")]
        if auth.can(user, "draft_reminders"):
            nd = CONN.execute("SELECT COUNT(*) FROM reminders WHERE status='draft'").fetchone()[0]
            links.append(("/reminders", f"Reminders {badge(nd, 'blue') if nd else ''}"))
        if auth.can(user, "view_bills"):
            links.append(("/billing", "Billing"))
        if auth.can(user, "view_inventory"):
            low = CONN.execute("SELECT COUNT(*) FROM inventory WHERE stock<=reorder_level").fetchone()[0]
            links.append(("/inventory", f"Inventory {badge(low, 'yellow') if low else ''}"))
        if auth.can(user, "manage_users"):
            links.append(("/admin/users", "Users"))
        if auth.can(user, "view_usage"):
            links.append(("/usage", "AI usage"))
        links.append(("/rules", "Rules"))
        if auth.can(user, "view_audit"):
            links.append(("/audit", "Audit"))
        nav = (f'<nav>{"".join(f"<a href={chr(34)}{h}{chr(34)}>{t}</a>" for h, t in links)}</nav>'
               f'<div class="who"><button type="button" class="small ghost" id="font-size" title="Larger or smaller text">A+ / A−</button>'
               f'<a href="/account" class="chip">{e(user["full_name"])} · {e(auth.ROLES[user["role"]])}</a>'
               f'<form method="post" action="/logout" class="inline"><button class="small ghost">Sign out</button></form></div>')
    demo_warn = ""
    if user and user["role"] == "admin" and CONN.execute("SELECT 1 FROM users WHERE is_demo=1 AND active=1").fetchone():
        demo_warn = '<div class="msg err">Demo accounts with published passwords are active. Reset or deactivate them before any real use (Users page).</div>'
    csrf = f'<meta name="csrf" content="{e(user["csrf"])}">' if user else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · CareFlow AI</title>{csrf}<link rel="stylesheet" href="/static/style.css"></head><body>
<header><a class="logo" href="/"><svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="12" fill="#cfe2fb"/>
<path d="M4 14h5l2-5 3 9 2-4h6" fill="none" stroke="#2f6bc2" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
<b>Care<span>Flow</span> AI</b></a>{nav}</header>
<div class="banner">Prototype with <b>synthetic patients only</b>. Not a medical device; thresholds are demo values. AI output must be checked by a clinician.{mock}</div>
<main>{demo_warn}{flash}{body}</main><footer>CareFlow AI · {e(config.HOSPITAL_NAME)} · model {config.MODEL} via Portkey · MGT 409 final project</footer>
<script src="/static/app.js"></script></body></html>""".encode("utf-8")


# ============================================================================ login / account / users
def view_login(err="", msg="", next_url="/"):
    body = f"""<div class="login-wrap"><div class="card login">
<h1>Sign in</h1><p class="sub">CareFlow AI – obstetric &amp; newborn ward</p>
<form method="post" action="/login" autocomplete="on"><input type="hidden" name="next" value="{e(next_url)}">
<label>Username</label><input name="username" autocomplete="username" required autofocus maxlength="32">
<label style="margin-top:10px">Password</label><input name="password" type="password" autocomplete="current-password" required maxlength="200">
<button style="margin-top:16px;width:100%">Sign in</button></form>
<p class="muted small-print">Sessions end after {config.SESSION_IDLE_MINUTES} min of inactivity. {config.MAX_FAILED_LOGINS} wrong passwords lock the account for {config.LOCKOUT_MINUTES} min.
Demo accounts for the teaching staff are listed in the README.</p></div></div>"""
    return page("Sign in", body, None, msg, err, bare=True)


def view_account(user, msg="", err=""):
    force = '<div class="warn">You must choose a new password before continuing.</div>' if user["must_change_pw"] else ""
    body = f"""<h1>My account</h1><p class="sub">{e(user['full_name'])} · {e(user['username'])} · {e(auth.ROLES[user['role']])}</p>{force}
<div class="card narrow"><h2>Change password</h2>
<form method="post" action="/account/password"><label>Current password</label><input type="password" name="current" required autocomplete="current-password">
<label style="margin-top:10px">New password</label><input type="password" name="new" required autocomplete="new-password" minlength="{config.MIN_PASSWORD_LENGTH}">
<label style="margin-top:10px">Repeat new password</label><input type="password" name="confirm" required autocomplete="new-password">
<p class="muted small-print">At least {config.MIN_PASSWORD_LENGTH} characters, letters and numbers, not containing your username. Changing it signs you out everywhere.</p>
<button>Change password</button></form></div>"""
    return page("Account", body, user, msg, err)


def view_users(user, msg="", err=""):
    rows = []
    for u in CONN.execute("SELECT * FROM users ORDER BY role, username"):
        locked = u["locked_until"] and parse_ts(u["locked_until"]) > datetime.now()
        state = (badge("active", "green") if u["active"] else badge("inactive", "grey")) + (" " + badge("locked", "red") if locked else "") \
            + (" " + badge("demo password", "yellow") if u["is_demo"] else "") + (" " + badge("must change pw", "blue") if u["must_change_pw"] else "")
        acts = (f'<form method="post" action="/admin/users/{u["id"]}/toggle" class="inline"><button class="small secondary">{"Deactivate" if u["active"] else "Activate"}</button></form> '
                f'<form method="post" action="/admin/users/{u["id"]}/reset" class="inline"><input name="password" type="password" placeholder="temporary password" required style="width:170px" autocomplete="new-password">'
                f' <button class="small secondary">Reset</button></form>')
        if locked:
            acts += f' <form method="post" action="/admin/users/{u["id"]}/unlock" class="inline"><button class="small">Unlock</button></form>'
        if u["id"] == user["id"]:
            acts = '<span class="muted">(you – use My account)</span>'
        rows.append(f"<tr><td class='mono'>{e(u['username'])}</td><td>{e(u['full_name'])}</td><td>{e(auth.ROLES[u['role']])}</td><td>{state}</td>"
                    f"<td>{dt(u['last_login']) or '—'}</td><td>{acts}</td></tr>")
    roles = "".join(f'<option value="{k}">{e(v)}</option>' for k, v in auth.ROLES.items())
    body = f"""<h1>Staff accounts</h1><p class="sub">Passwords are stored only as salted PBKDF2-SHA256 hashes ({config.PBKDF2_ITERATIONS:,} iterations). New and reset accounts must choose their own password at first sign-in.</p>
<div class="card"><table><tr><th>Username</th><th>Name</th><th>Role</th><th>Status</th><th>Last sign-in</th><th>Actions</th></tr>{''.join(rows)}</table></div>
<div class="card narrow"><h2>Add a staff member</h2><form method="post" action="/admin/users">
<div class="row"><div><label>Username</label><input name="username" required pattern="[a-z0-9][a-z0-9._-]{{2,31}}" placeholder="e.g. sister.rekha"></div>
<div><label>Full name</label><input name="full_name" required></div><div><label>Role</label><select name="role">{roles}</select></div></div>
<label>Temporary password</label><input name="password" type="password" required autocomplete="new-password" minlength="{config.MIN_PASSWORD_LENGTH}">
<button style="margin-top:10px">Create account</button></form></div>"""
    return page("Users", body, user, msg, err)


# ============================================================================ dashboard
TODO_TITLE = {"nurse": "My to-do · nurse", "doctor": "My to-do · RMO / resident", "consultant": "My to-do · consultant"}


def _attention(user) -> list[str]:
    """Role-specific to-do list (asked for in the clinician trial): each item says who acts on it.
    Computed by code from the record - the same facts the chatbot's get_ward_due_tasks tool returns."""
    role = user["role"]
    items = []

    def add(prio, roles, html_):
        if role in roles:
            items.append((prio, html_))
    for a in CONN.execute("SELECT a.*, p.name FROM alerts a JOIN patients p ON p.id=a.patient_id WHERE a.status='pending' "
                          "ORDER BY CASE a.level WHEN 'red' THEN 0 ELSE 1 END, a.ts DESC"):
        first = re.sub(r"^(RED|YELLOW): ", "", json.loads(a["reasons"])[0])
        verb = {"consultant": "ACKNOWLEDGE", "doctor": "REVIEW", "nurse": "WATCH"}.get(role, "")
        add(0 if a["level"] == "red" else 1, {"consultant", "doctor", "nurse"},
            f'<a class="att {a["level"]}" href="/patient/{a["patient_id"]}#alerts"><b>{verb} {e(a["level"].upper())}</b> {e(a["name"])} – {e(first)} <small>{ago(a["ts"])}</small></a>')
    for p in CONN.execute("SELECT * FROM patients WHERE status='intake'"):
        st = intake.status(CONN, p["id"])
        add(1, {"doctor", "consultant"}, f'<a class="att blue" href="/patient/{p["id"]}#admission"><b>VERIFY</b> {e(p["name"])} – admission papers: '
                                         f'{st["unresolved"]} item(s) need a doctor, checklist not complete</a>')
    for p in CONN.execute("SELECT * FROM patients WHERE kind='mother' AND status='admitted'"):
        last = CONN.execute("SELECT ts FROM events WHERE patient_id=? AND type='labour' ORDER BY ts DESC LIMIT 1", (p["id"],)).fetchone()
        delivered = CONN.execute("SELECT 1 FROM events WHERE patient_id=? AND type='delivery'", (p["id"],)).fetchone()
        if last and not delivered and hours_since(last["ts"]) > 0.5:
            add(1, {"nurse", "doctor"}, f'<a class="att yellow" href="/patient/{p["id"]}#labour"><b>LABOUR CHECK</b> {e(p["name"])} – '
                                        f'FHR every 30 min – last entry {ago(last["ts"])}</a>')
    for b in CONN.execute("SELECT * FROM patients WHERE kind='baby' AND status='admitted'"):
        given = {json.loads(r["data"])["code"]: r["ts"] for r in CONN.execute("SELECT * FROM events WHERE patient_id=? AND type='vaccine'", (b["id"],))}
        due = [d for d in vaccines.birth_dose_status(b["birth_at"], given) if d["status"] != "given"]
        if due:
            over = any(d["status"] == "overdue" for d in due)
            add(1 if over else 2, {"nurse", "doctor"}, f'<a class="att {"yellow" if over else "green"}" href="/patient/{b["id"]}#vaccines"><b>GIVE{" – OVERDUE" if over else ""}</b> '
                                                        f'{e(b["name"])} – {e(", ".join(d["name"].split(" (")[0] for d in due))}</a>')
    for d in CONN.execute("SELECT d.*, p.name FROM drafts d JOIN patients p ON p.id=d.patient_id WHERE d.status='draft' AND p.status<>'discharged' ORDER BY d.id DESC"):
        add(2, {"doctor", "consultant"}, f'<a class="att blue" href="/patient/{d["patient_id"]}#docs"><b>APPROVE</b> {e(d["name"])} – '
                                         f'{e(drafts.KINDS[d["kind"]]["title"].lower())} drafted {ago(d["created_at"])}</a>')
    n = CONN.execute("SELECT COUNT(*) FROM reminders WHERE status='draft'").fetchone()[0]
    if n:
        add(2, {"consultant"}, f'<a class="att blue" href="/reminders"><b>AUTHORISE</b> {n} email reminder(s) for families</a>')
    for i in CONN.execute("SELECT * FROM inventory WHERE stock<=reorder_level ORDER BY name"):
        add(3, {"nurse"}, f'<a class="att yellow" href="/inventory"><b>REORDER</b> {e(i["name"])} – {i["stock"]} {e(i["unit"])} left (reorder at {i["reorder_level"]})</a>')
    return [h for _, h in sorted(items, key=lambda x: x[0])]


def _patient_card(p) -> str:
    pend = CONN.execute("SELECT level FROM alerts WHERE patient_id=? AND status='pending'", (p["id"],)).fetchall()
    tag = (badge(f"{len(pend)} alert{'s' if len(pend) > 1 else ''}", "red" if any(a["level"] == "red" for a in pend) else "yellow")
           if pend else badge("no pending alerts", "green"))
    ph = record.phase(CONN, p)
    ph_kind = {"in labour": "pink", "postnatal": "lilac", "antenatal": "blue", "newborn": "mint"}.get(ph, "yellow")
    v = CONN.execute("SELECT * FROM events WHERE patient_id=? AND type IN ('vitals','labour','newborn_obs') ORDER BY ts DESC, id DESC LIMIT 1",
                     (p["id"],)).fetchone()
    last = f"<div class='last'>Last {EVENT_LABEL[v['type']].lower()} {ago(v['ts'])}: {event_text(v)}</div>" if v else ""
    allergy = (f" · {badge('Allergy: ' + p['allergies'], 'red')}" if p["allergies"] and not re.search(r"no known|nkda|^none", p["allergies"], re.I) else "")
    babies = "".join(f'<a class="baby-chip" href="/patient/{b["id"]}">👶 {e(b["name"].split(" of ")[0])} · {ago(b["birth_at"])} '
                     f'{badge(CONN.execute("SELECT COUNT(*) FROM alerts WHERE patient_id=? AND status=?", (b["id"], "pending")).fetchone()[0] or "ok", "yellow" if CONN.execute("SELECT 1 FROM alerts WHERE patient_id=? AND status=?", (b["id"], "pending")).fetchone() else "green")}</a>'
                     for b in CONN.execute("SELECT * FROM patients WHERE mother_id=? AND status<>'discharged'", (p["id"],)))
    return f"""<div class="pcard"><a class="pc-main" href="/patient/{p['id']}"><div class="bed">Bed {e(p['bed'])} · {e(p['uhid'])} · {e(p['consultant'])}</div>
<div class="name">{e(p['name'])}, {p['age']}</div><div class="dx">{e(p['admitting_diagnosis'])}</div>
<div style="margin-top:8px">{badge(ph, ph_kind)} {tag}{allergy}</div>{last}</a>{('<div class="babies">' + babies + '</div>') if babies else ''}</div>"""


def view_dashboard(user, msg="", err=""):
    if not auth.can(user, "view_clinical"):
        return view_billing(user, msg, err) if auth.can(user, "view_bills") else view_users(user, msg, err)
    q = lambda s, *a: CONN.execute(s, a).fetchone()[0]  # noqa: E731
    mothers = q("SELECT COUNT(*) FROM patients WHERE kind='mother' AND status<>'discharged'")
    babies = q("SELECT COUNT(*) FROM patients WHERE kind='baby' AND status<>'discharged'")
    labour = sum(1 for p in CONN.execute("SELECT * FROM patients WHERE kind='mother' AND status='admitted'") if record.phase(CONN, p) == "in labour")
    red = q("SELECT COUNT(*) FROM alerts WHERE status='pending' AND level='red'")
    pend = q("SELECT COUNT(*) FROM alerts WHERE status='pending'")
    verify = q("SELECT COUNT(*) FROM patients WHERE status='intake'")
    low = q("SELECT COUNT(*) FROM inventory WHERE stock<=reorder_level")
    cost = q("SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE substr(ts,1,10)=date('now','localtime')")
    hour = datetime.now().hour
    greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
    att = _attention(user)
    cards = "".join(_patient_card(p) for p in CONN.execute("SELECT * FROM patients WHERE kind='mother' AND status<>'discharged' ORDER BY CAST(bed AS INTEGER)"))
    gone = CONN.execute("SELECT * FROM patients WHERE kind='mother' AND status='discharged' ORDER BY discharged_at DESC LIMIT 5").fetchall()
    gone_html = "".join(f"<li><a href='/patient/{p['id']}'>{e(p['name'])}</a> – discharged {dt(p['discharged_at'])} by {e(p['discharged_by'])}</li>" for p in gone)
    admit = '<a class="btn" href="/admit">+ New admission</a>' if auth.can(user, "admit") else ""
    tiles = [(mothers, "mothers on the ward", "lilac"), (babies, "babies (rooming-in)", "mint"), (labour, "in labour", "pink"),
             (f"{red} / {pend}", "red / all pending alerts", "red" if red else "peach"), (verify, "admissions to verify", "blue"),
             (low, "stock items to reorder", "peach" if low else "mint"), (f"${cost:.3f}", "AI cost today", "lilac")]
    body = f"""<div class="dash-head"><div><h1>{greet}, {e(user['full_name'].split(' (')[0])}</h1>
<p class="sub">{datetime.now():%A %d %B %Y, %H:%M} · obstetric &amp; newborn ward</p></div>{admit}</div>
<div class="metrics">{''.join(f'<div class="metric t-{k}"><div class="v">{v}</div><div class="l">{l}</div></div>' for v, l, k in tiles)}</div>
<div class="dash"><div><div class="card"><h2>{e(TODO_TITLE.get(user["role"], "Needs attention"))}</h2>{''.join(att) or '<p class="muted">Nothing pending for you. 🌿</p>'}
<p class="muted small-print" style="margin:8px 0 0">Shows what <b>your role</b> acts on. The whole ward’s alerts are on the <a href="/alerts">Alerts</a> page.</p></div>
<h2 style="margin:18px 0 10px">Mothers &amp; babies</h2><div class="grid">{cards}</div>
{('<div class="card" style="margin-top:18px"><h2>Recently discharged</h2><ul>' + gone_html + '</ul></div>') if gone_html else ''}</div>
<aside>{chat_panel(user, compact=True)}</aside></div>"""
    return page("Ward", body, user, msg, err)


# ============================================================================ chat
def chat_panel(user, focus_pid: int | None = None, compact=False) -> str:
    hist = CONN.execute("SELECT * FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 12", (user["id"],)).fetchall()[::-1]
    msgs = "".join(_chat_bubble(m["role"], m["content"], json.loads(m["meta"] or "{}")) for m in hist)
    hints = ["Who needs attention right now?", "Summarise Priya's labour progress", "Which babies still need Hepatitis B?",
             "Does Kavita have any drug allergies?"]
    if focus_pid:
        hints = ["Summarise this patient for handover", "Latest observations and trend?", "Any allergies?", "Pending alerts?"]
    chips = "".join(f'<button type="button" class="hint" data-q="{e(h)}">{e(h)}</button>' for h in hints)
    return f"""<div class="card chat {'compact' if compact else ''}" id="chat" data-focus="{focus_pid or ''}">
<div class="chat-head"><h2>Ask CareFlow</h2><span class="muted small-print">Answers only from the ward record, with citations · read-only</span></div>
<div class="chat-log" id="chat-log">{msgs or '<div class="muted empty">Ask about any mother or baby on the ward.</div>'}</div>
<div class="hints">{chips}</div>
<form id="chat-form" class="chat-form"><textarea id="chat-q" rows="2" maxlength="1000" placeholder="Type a question… (Enter to send, Shift+Enter for a new line)"></textarea>
<button id="chat-send">Send</button></form></div>"""


def _chat_bubble(role, content, meta) -> str:
    if role == "user":
        return f'<div class="bubble me">{e(content)}</div>'
    flags = ""
    if meta.get("invalid"):
        flags += f'<div class="danger-box small-print">Citations not found in the record: {e(", ".join(meta["invalid"]))} – treat those statements as unreliable.</div>'
    elif not meta.get("has_citations") and not meta.get("blocked"):
        flags += '<div class="warn small-print">No citations in this answer – verify before relying on it.</div>'
    trace = " → ".join(meta.get("tools", [])) or "no tools"
    return (f'<div class="bubble bot">{cite_html(content)}{flags}<div class="trace">{meta.get("steps", 1)} step(s) · {e(trace)}'
            f'{" · " + str(meta.get("tokens")) + " tokens" if meta.get("tokens") else ""}</div></div>')


def chat_bubble_html(role, content, meta) -> str:
    return _chat_bubble(role, content, meta)


def view_assistant(user, msg="", err=""):
    body = f"""<h1>Ask CareFlow</h1><p class="sub">A read-only assistant over the whole ward record. It looks things up with tools, cites every fact
(<span class="cite">E12</span> ward entry, <span class="cite">A3</span> alert, <span class="cite">D2.p1</span> admission paper read by AI) and cannot change anything.</p>
{chat_panel(user)}"""
    return page("Ask CareFlow", body, user, msg, err)


# ============================================================================ new admission
def view_admit(user, msg="", err=""):
    cons = sorted({r[0] for r in CONN.execute("SELECT consultant FROM patients WHERE kind='mother'")} |
                  {r[0] for r in CONN.execute("SELECT full_name FROM users WHERE role='consultant' AND active=1")})
    body = f"""<p><a href="/">← Ward</a></p><h1>New admission</h1>
<p class="sub">Step 1 of 2. Register the mother; then attach the handwritten papers she brought (OPD notes, lab reports) and verify what the AI reads.</p>
<div class="card narrow"><form method="post" action="/admit">
<div class="row"><div style="grid-column:span 2"><label>Full name</label><input name="name" required maxlength="80"></div><div><label>Age</label><input name="age" type="number" min="12" max="60" required></div></div>
<div class="row"><div><label>Bed</label><input name="bed" maxlength="10"></div><div><label>Room</label><select name="room_type"><option value="general">General ward</option><option value="private">Private room</option></select></div><div style="grid-column:span 2"><label>Consultant</label><select name="consultant">{''.join(f'<option>{e(c)}</option>' for c in cons)}</select></div></div>
<label>Reason for admission</label><input name="reason" maxlength="200" placeholder="e.g. 38 weeks in early labour / raised BP referred from OPD">
<div class="row" style="margin-top:10px"><div><label>Email for reminders (optional)</label><input name="email" type="email" maxlength="120"></div>
<div><label>Phone (optional)</label><input name="phone" maxlength="30"></div></div>
<button>Register &amp; continue to papers</button></form></div>"""
    return page("New admission", body, user, msg, err)


# ============================================================================ patient page
def view_patient(pid: int, user: dict, msg="", err=""):
    p = CONN.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    if not p:
        return None
    key = uuid.uuid4().hex
    closed = p["status"] == "discharged"
    ph = record.phase(CONN, p)
    is_mother = p["kind"] == "mother"
    delivered = is_mother and CONN.execute("SELECT 1 FROM events WHERE patient_id=? AND type='delivery'", (pid,)).fetchone()
    parts = [_header(p, ph)]
    tabs = []
    if is_mother:
        tabs += [("admission", "Admission papers")]
        if not delivered and p["status"] != "discharged":
            tabs += [("labour", "Labour & delivery")]
        if delivered:
            tabs += [("delivery", "Delivery & baby")]
    else:
        tabs += [("newborn", "Newborn care"), ("vaccines", "Vaccines (IAP)")]
    tabs += [("bedside", "Bedside entry"), ("alerts", "Alerts"), ("timeline", "Timeline"), ("ask", "Ask"), ("docs", "Handover & discharge"), ("bill", "Bill")]
    parts.append('<div class="tabs">' + "".join(f'<a href="#{a}">{t}</a>' for a, t in tabs) + "</div>")
    if is_mother:
        parts.append(_admission_section(p, user))
        if not delivered and not closed:
            parts.append(_labour_section(p, user, key))
        if delivered:
            parts.append(_delivery_summary(p))
    else:
        parts.append(_newborn_section(p, user, key))
        parts.append(_vaccine_section(p, user, key))
    if not closed:
        parts.append(_bedside_section(p, user, key))
    parts.append(_alerts_section(p, user))
    parts.append(_timeline_section(p))
    parts.append(f'<section id="ask">{chat_panel(user, focus_pid=pid)}</section>' if auth.can(user, "ask_ai") else "")
    parts.append(_docs_section(p, user))
    parts.append(_bill_section(p))
    return page(p["name"], "".join(parts), user, msg, err)


def _header(p, ph) -> str:
    chips = [badge(ph, {"in labour": "pink", "postnatal": "lilac", "newborn": "mint", "discharged": "grey"}.get(ph, "blue"))]
    if p["kind"] == "mother":
        if p["allergies"]:
            nk = re.search(r"no known|nkda|^none", p["allergies"], re.I)
            chips.append(badge(("Allergies: " if not nk else "") + p["allergies"], "green" if nk else "red"))
        elif p["status"] == "intake":
            chips.append(badge("Allergies not yet confirmed", "yellow"))
        for lab, v in (("Blood group", p["blood_group"]), ("", p["gravida_para"]), ("GA at admission", p["gestational_age"])):
            if v:
                chips.append(badge(f"{lab} {v}".strip(), "blue"))
        sub = f"Bed {e(p['bed'])} · {e(p['uhid'])} · Consultant {e(p['consultant'])} · Admitted {dt(p['admitted_at'])}"
        babies = CONN.execute("SELECT id, name FROM patients WHERE mother_id=?", (p["id"],)).fetchall()
        if babies:
            sub += " · " + " ".join(f'<a href="/patient/{b["id"]}">👶 {e(b["name"])}</a>' for b in babies)
        title = f"{e(p['name'])}, {p['age']}"
    else:
        m = CONN.execute("SELECT * FROM patients WHERE id=?", (p["mother_id"],)).fetchone()
        age_h = hours_since(p["birth_at"])
        chips += [badge({"M": "Boy", "F": "Girl"}.get(p["sex"], "Sex not recorded"), "blue"), badge(f"Birth weight {p['birth_weight_g']} g", "blue"),
                  badge(f"Age {age_h:.0f} h" if age_h < 72 else f"Age {age_h / 24:.0f} days", "mint")]
        sub = (f"{e(p['uhid'])} · Born {dt(p['birth_at'])} · Mother: <a href='/patient/{m['id']}'>{e(m['name'])}</a> "
               f"(blood group {e(m['blood_group'] or '—')}) · Bed {e(p['bed'])}")
        title = e(p["name"])
    caveat = ""
    if p["kind"] == "mother":
        st = intake.status(CONN, p["id"])
        if p["status"] == "intake":
            caveat = (f'<div class="caveat"><b>Admission not complete.</b> Admission papers are being verified by a doctor: {st["unresolved"]} AI-read item(s) still need '
                      f'a decision. <a href="#admission">Go to verification →</a></div>')
        elif st["unverified"]:
            caveat = (f'<div class="caveat"><b>Caveat:</b> {st["unverified"]} item(s) on this record were read by AI from handwritten papers and not individually verified. '
                      f'{e(intake.CAVEAT)}</div>')
    if p["status"] == "discharged":
        caveat += f'<div class="msg ok">Discharged {dt(p["discharged_at"])} by {e(p["discharged_by"])}. The record is closed.</div>'
    return f"""<p><a href="/">← Ward</a></p><div class="phead"><h1>{title}</h1><div class="chips">{''.join(chips)}</div>
<p class="sub">{sub}<br><b>Admitting diagnosis:</b> {e(p['admitting_diagnosis'] or '')}</p></div>{caveat}"""


# ---------------------------------------------------------------------------- admission & papers
def _admission_section(p, user) -> str:
    pid = p["id"]
    st = intake.status(CONN, pid)
    can_upload = auth.can(user, "upload_papers") and p["status"] != "discharged"
    can_verify = auth.can(user, "verify_papers")
    docs = CONN.execute("SELECT * FROM documents WHERE patient_id=? ORDER BY id", (pid,)).fetchall()
    exts = {x["document_id"]: x for x in CONN.execute("SELECT * FROM extractions WHERE patient_id=?", (pid,))}
    rows = []
    for d in docs:
        x = exts.get(d["id"])
        if x:
            status = badge("read" + (" (same file as before – reused, no AI cost)" if x["cached_from"] else ""), "green")
        elif can_upload:
            status = (f'<form method="post" action="/patient/{pid}/extract" data-busy class="inline"><input type="hidden" name="document_id" value="{d["id"]}">'
                      f'<button class="small">Read with AI</button><span class="spinner">Reading handwriting… up to a minute</span></form>')
        else:
            status = badge("not read yet", "grey")
        rows.append(f"<tr><td class='mono'>D{d['id']}</td><td>{e(d['filename'])}</td><td>{e(extract.DOC_TYPES.get(d['doc_type'], d['doc_type'] or ''))}</td>"
                    f"<td>{d['pages']}</td><td>{dt(d['uploaded_at'])} · {e(d['uploaded_by'] or '')}</td><td>{status}</td></tr>")
    types = "".join(f'<option value="{k}">{e(v)}</option>' for k, v in extract.DOC_TYPES.items())
    upload = ""
    if can_upload:
        upload = f"""<div class="two"><form method="post" action="/patient/{pid}/upload" enctype="multipart/form-data">
<label>Scan or photo of a handwritten paper (PDF/JPG/PNG, ≤{config.MAX_PAGES} pages)</label><input type="file" name="file" accept=".pdf,.jpg,.jpeg,.png" required>
<div class="row" style="margin-top:8px"><div><select name="doc_type">{types}</select></div><div><button>Upload</button></div></div></form>
<form method="post" action="/patient/{pid}/sample"><label>No paper handy? Attach a synthetic sample</label>
<select name="which"><option value="meera_sharma_opd_and_progress_note.pdf">Handwritten OPD + progress note (Meera Sharma)</option>
<option value="injection_test_nursing_note.png">Safety test: note containing a prompt-injection attack</option></select>
<div style="margin-top:8px"><button class="secondary">Attach sample</button></div></form></div>"""
    table = ('<table style="margin-top:12px"><tr><th>ID</th><th>File</th><th>Type</th><th>Pages</th><th>Uploaded</th><th>Status</th></tr>' + "".join(rows) + "</table>") if rows else ""
    verify = _verification_table(p, user) if st["total"] else ""
    checklist = _checklist(p, user, st) if p["status"] == "intake" else _admission_done(p, st)
    intro = ("Only the papers brought at admission are handwritten (previous OPD notes, lab and scan reports). The Record Reader agent reads them; "
             "a doctor then confirms, corrects or rejects every uncertain or safety-critical item. Everything after admission is entered digitally.")
    inj = ""
    for x in CONN.execute("SELECT * FROM extractions WHERE patient_id=? AND cached_from IS NULL", (pid,)):
        d = json.loads(x["data"])
        if d.get("embedded_instructions"):
            its = "".join(f"<li>D{x['document_id']} p{e(str(i.get('page')))}: “{e(str(i.get('text'))[:220])}”</li>" for i in d["embedded_instructions"])
            inj += (f'<div class="danger-box"><b>Instructions found inside an uploaded paper were ignored (possible prompt injection).</b><ul>{its}</ul>'
                    f'No action was taken: the AI cannot change bills, alerts or discharge status.</div>')
        if d.get("illegible"):
            inj += ('<div class="warn"><b>The AI could not read parts of D' + str(x["document_id"]) + ':</b><ul>' +
                    "".join(f"<li>p{e(str(i.get('page')))}: {e(str(i.get('description')))}</li>" for i in d["illegible"]) + "</ul>Please fill these in from the original.</div>")
    return f"""<section class="card" id="admission"><h2>Admission papers → verified record</h2><p class="muted">{intro}</p>
{upload}{table}{inj}{verify}{checklist}{'' if can_verify or p['status'] != 'intake' else '<div class="warn">A doctor must verify the papers and complete the admission.</div>'}</section>"""


def _verification_table(p, user) -> str:
    pid = p["id"]
    can_verify = auth.can(user, "verify_papers") and p["status"] != "discharged"
    rows = []
    for i in intake.items(CONN, pid):
        rv = i["review"]
        extra = " · ".join(f"{k}: {v}" for k, v in i["extra"].items())
        value = e(i["value"]) + (f"<div class='muted small-print'>{e(extra)}</div>" if extra else "")
        why = []
        if i["critical"]:
            why.append(badge("safety-critical", "pink"))
        if i["needs_review"]:
            why.append(badge("uncertain", "yellow"))
        if rv:
            label = {"confirmed": ("confirmed", "green"), "corrected": ("corrected", "blue"), "rejected": ("rejected – not on paper", "grey")}[rv["status"]]
            state = badge(f"{label[0]} by {rv['reviewer']}", label[1])
            if rv["corrected_value"]:
                value = f"<s class='muted'>{e(i['value'])}</s> <b>{e(rv['corrected_value'])}</b>"
            if rv["status"] == "rejected":
                value = f"<s class='muted'>{e(i['value'])}</s>"
        elif can_verify:
            state = f"""<form method="post" action="/patient/{pid}/review" class="vform"><input type="hidden" name="extraction_id" value="{i['extraction_id']}">
<input type="hidden" name="field_key" value="{e(i['key'])}"><button name="state" value="confirmed" class="small">✓ Correct</button>
<input name="corrected" placeholder="correct value" maxlength="300"><button name="state" value="corrected" class="small secondary">Save fix</button>
<button name="state" value="rejected" class="small ghost" title="The AI added something that is not on the paper">✗ Not on paper</button></form>"""
        else:
            state = badge("unverified AI reading", "yellow") if i["must_resolve"] else badge("AI reading", "grey")
        cls = "must" if i["must_resolve"] and not rv else ""
        rows.append(f"<tr class='{cls}'><td>{e(i['label'])}<div>{''.join(why)}</div></td><td>{value}</td><td class='mono'>“{e(i['verbatim'] or '')}”</td>"
                    f"<td><span class='cite'>{e(i['cite'])}</span></td><td>{conf_badge(i['confidence'])}</td><td>{state}</td></tr>")
    vit = []
    for r in CONN.execute("SELECT * FROM events WHERE patient_id=? AND source='document' ORDER BY ts", (pid,)):
        vit.append(f"<li>{dt(r['ts'])}: {event_text(r)} <span class='cite'>E{r['id']}</span></li>")
    st = intake.status(CONN, pid)
    head = (f"<h3>What the AI read <small class='muted'>· {st['total']} items · {st['unresolved']} still need a doctor · {st['verified']} verified · "
            f"{st['rejected']} rejected</small></h3>")
    return (f"{head}<div class='table-scroll'><table class='verify'><tr><th>Field</th><th>AI reading</th><th>As written</th><th>Source</th><th>AI confidence</th>"
            f"<th>Doctor's decision</th></tr>{''.join(rows)}</table></div>"
            + (f"<h3>Vital signs found in the papers</h3><ul>{''.join(vit)}</ul><p class='muted small-print'>Sent through the same red-flag rules as bedside entries; "
               f"any alert is marked “from AI-read paper – verify”.</p>" if vit else ""))


def _checklist(p, user, st) -> str:
    if not auth.can(user, "verify_papers"):
        return ""
    sug = intake.suggestions(CONN, p["id"])

    def field(name, label, required=True, value=""):
        hint = f'<div class="ai-sugg">AI reading: {e(sug[name])}</div>' if sug.get(name) else ""
        return (f'<div><label>{label}{" *" if required else ""}</label><input name="{name}" value="{e(value)}" maxlength="300"'
                f'{" required" if required else ""}>{hint}</div>')
    bg_opts = "".join(f"<option{' selected' if b == (p['blood_group'] or '') else ''}>{b}</option>" for b in intake.BLOOD_GROUPS)
    bg_hint = f'<div class="ai-sugg">AI reading: {e(sug["blood_group"])}</div>' if sug.get("blood_group") else ""
    disabled = " disabled" if st["unresolved"] else ""
    note = (f'<div class="warn">Decide on the {st["unresolved"]} highlighted item(s) above first.</div>' if st["unresolved"] else "")
    return f"""<h3 style="margin-top:22px">Admission checklist <small class="muted">· entered by the doctor – the AI only suggests</small></h3>
<form method="post" action="/patient/{p['id']}/complete_intake" class="checklist">
<div class="row">{field('allergies', 'Drug allergies (write NKDA if none)', value=p['allergies'] or '')}
<div><label>Blood group *</label><select name="blood_group" required><option value="">choose…</option>{bg_opts}</select>{bg_hint}</div>
{field('gravida_para', 'Gravida / para (e.g. G2P1L1)', value=p['gravida_para'] or '')}</div>
<div class="row">{field('gestational_age', 'Gestational age (e.g. 37+2 weeks)', value=p['gestational_age'] or '')}{field('edd', 'EDD', False, p['edd'] or '')}</div>
<div class="row"><div style="grid-column:span 2">{field('admitting_diagnosis', 'Admitting diagnosis (keep “?” if uncertain)', value=p['admitting_diagnosis'] if not p['admitting_diagnosis'].startswith('Referred') else '')}</div></div>
<div class="row"><div><label>Email for reminders</label><input name="email" type="email" value="{e(p['email'] or '')}" maxlength="120"></div>
<div><label>Phone</label><input name="phone" value="{e(p['phone'] or '')}" maxlength="30"></div></div>
{note}<button{disabled}>Complete admission</button> <small class="muted">Records you as the verifying doctor.</small></form>"""


def _admission_done(p, st) -> str:
    if not p["intake_completed_by"]:
        return ""
    return (f'<div class="msg ok" style="margin-top:14px">Admission completed by {e(p["intake_completed_by"])} on {dt(p["intake_completed_at"])}'
            f'{f" · {st[chr(118) + chr(101) + chr(114) + chr(105) + chr(102) + chr(105) + chr(101) + chr(100)]} AI-read items verified, {st[chr(114) + chr(101) + chr(106) + chr(101) + chr(99) + chr(116) + chr(101) + chr(100)]} rejected" if st["total"] else ""}.</div>')


# ---------------------------------------------------------------------------- labour
def partograph_svg(entries: list[dict]) -> str:
    pts = [(parse_ts(x["ts"]), x) for x in entries if x.get("dilatation_cm") is not None]
    fh = [(parse_ts(x["ts"]), x["fhr"]) for x in entries if x.get("fhr")]
    if not pts and not fh:
        return '<p class="muted">No labour entries yet – the partograph appears after the first entry.</p>'
    t0 = min([t for t, _ in pts] + [t for t, _ in fh])
    span = max(12.0, max((t - t0).total_seconds() / 3600 for t, _ in pts + [(t, None) for t, _ in fh]) + 1)
    W, H, L, T, PH, FH = 640, 330, 44, 12, 200, 80
    x = lambda t: L + (t - t0).total_seconds() / 3600 / span * (W - L - 12)  # noqa: E731
    xh = lambda h: L + h / span * (W - L - 12)  # noqa: E731
    y = lambda cm: T + PH - cm / 10 * PH  # noqa: E731
    fy = lambda f: T + PH + 34 + FH - (f - 80) / 120 * FH  # noqa: E731
    g = [f'<svg viewBox="0 0 {W} {H}" class="parto" role="img" aria-label="Partograph">']
    for cm in range(0, 11, 2):
        g.append(f'<line x1="{L}" x2="{W - 12}" y1="{y(cm)}" y2="{y(cm)}" class="grid"/><text x="{L - 8}" y="{y(cm) + 4}" class="ax" text-anchor="end">{cm}</text>')
    for h in range(0, int(span) + 1, 2):
        g.append(f'<line x1="{xh(h)}" x2="{xh(h)}" y1="{T}" y2="{T + PH}" class="grid"/><text x="{xh(h)}" y="{T + PH + 14}" class="ax" text-anchor="middle">{h}h</text>')
    active = next(((t, d) for t, d in pts if d["dilatation_cm"] >= 4), None)
    if active:
        ta, da = active
        h0 = (ta - t0).total_seconds() / 3600
        g.append(f'<line x1="{xh(h0)}" y1="{y(da["dilatation_cm"])}" x2="{xh(h0 + 10 - da["dilatation_cm"])}" y2="{y(10)}" class="alertline"/>')
        g.append(f'<line x1="{xh(h0 + 4)}" y1="{y(da["dilatation_cm"])}" x2="{xh(h0 + 14 - da["dilatation_cm"])}" y2="{y(10)}" class="actionline"/>')
        g.append(f'<text x="{xh(h0 + 10 - da["dilatation_cm"]) - 4}" y="{y(10) + 14}" class="lbl alert" text-anchor="end">alert</text>'
                 f'<text x="{min(W - 14, xh(h0 + 14 - da["dilatation_cm"]))}" y="{y(10) + 14}" class="lbl action" text-anchor="end">action</text>')
    if pts:
        g.append('<polyline class="dil" points="' + " ".join(f"{x(t):.1f},{y(d['dilatation_cm']):.1f}" for t, d in pts) + '"/>')
        g += [f'<circle cx="{x(t):.1f}" cy="{y(d["dilatation_cm"]):.1f}" r="4.5" class="dilpt"><title>{dt(d["ts"])}: {d["dilatation_cm"]:g} cm</title></circle>' for t, d in pts]
    g.append(f'<text x="6" y="{T + 10}" class="ax">cm</text>')
    top, bot = fy(160), fy(110)
    g.append(f'<rect x="{L}" y="{top}" width="{W - L - 12}" height="{bot - top}" class="band"/>'
             f'<text x="{L - 8}" y="{top + 4}" class="ax" text-anchor="end">160</text><text x="{L - 8}" y="{bot + 4}" class="ax" text-anchor="end">110</text>'
             f'<text x="6" y="{T + PH + 30}" class="ax">FHR</text>')
    if fh:
        g.append('<polyline class="fhr" points="' + " ".join(f"{x(t):.1f},{fy(f):.1f}" for t, f in fh) + '"/>')
        g += [f'<circle cx="{x(t):.1f}" cy="{fy(f):.1f}" r="3.5" class="{"fhrbad" if f < 110 or f >= 160 else "fhrpt"}"><title>FHR {f}</title></circle>' for t, f in fh]
    g.append("</svg>")
    return "".join(g)


def _labour_section(p, user, key) -> str:
    pid = p["id"]
    entries = record.labour(CONN, pid)
    rows = "".join(f"<tr><td>{dt(x['ts'])}</td><td>{x.get('dilatation_cm', '—') if x.get('dilatation_cm') is not None else '—'}</td>"
                   f"<td>{x.get('contractions_10min', '—')}</td><td>{x.get('fhr', '—')}</td><td>{e(rules.LIQUOR.get(x.get('liquor'), '—'))}</td>"
                   f"<td>{e(x.get('moulding') or '—')}</td><td>{e(x['by'] or '')}</td><td><span class='cite'>{x['cite']}</span></td></tr>" for x in entries[::-1])
    form = ""
    if auth.can(user, "enter_obs"):
        liq = "".join(f'<option value="{k}">{e(v)}</option>' for k, v in rules.LIQUOR.items())
        mou = "".join(f"<option>{m}</option>" for m in rules.MOULDING)
        form = f"""<form method="post" action="/patient/{pid}/event" class="subcard"><input type="hidden" name="type" value="labour"><input type="hidden" name="key" value="l-{key}">
<h3>Add labour check <small class="muted">(WHO Labour Care Guide: FHR every 30 min; cervix every 4 h or as indicated)</small></h3>
<div class="row"><div><label>Cervix (cm)</label><input name="dilatation_cm" type="number" step="0.5" min="0" max="10"></div>
<div><label>Descent (fifths palpable)</label><input name="descent_fifths" type="number" min="0" max="5"></div>
<div><label>Contractions /10 min</label><input name="contractions_10min" type="number" min="0" max="10"></div>
<div><label>Duration (s)</label><input name="contraction_secs" type="number" min="0" max="180"></div>
<div><label>Fetal HR</label><input name="fhr" type="number" min="50" max="240"></div></div>
<div class="row"><div><label>Amniotic fluid</label><select name="liquor"><option value="">—</option>{liq}</select></div>
<div><label>Moulding</label><select name="moulding"><option value="">—</option>{mou}</select></div>
<div><label>Oxytocin (if running)</label><input name="oxytocin" placeholder="e.g. 5 IU in 500 ml, 12 dpm"></div>
<div><label>Maternal BP</label><div class="pair"><input name="sbp" type="number" placeholder="sys"><input name="dbp" type="number" placeholder="dia"></div></div>
<div><label>Pulse</label><input name="hr" type="number"></div><div><label>Temp °C</label><input name="temp_c" type="number" step="0.1"></div></div>
<button>Save labour check</button> <small class="muted">Checked instantly against labour red-flag rules.</small></form>"""
    deliv = _delivery_form(p, user, key) if auth.can(user, "record_delivery") else ""
    return f"""<section class="card" id="labour"><h2>Labour monitoring</h2>
<div class="two"><div>{partograph_svg(entries)}<p class="muted small-print">Cervical dilatation (blue) with the classic alert and action lines from 4 cm; fetal heart rate (teal) with the 110–160 band.</p></div>
<div class="table-scroll"><table><tr><th>Time</th><th>Cx</th><th>Contr</th><th>FHR</th><th>Liquor</th><th>Mould</th><th>By</th><th></th></tr>{rows or '<tr><td colspan=8 class=muted>No entries yet.</td></tr>'}</table></div></div>
{form}{deliv}</section>"""


def _delivery_form(p, user, key) -> str:
    opt = lambda d: "".join(f'<option value="{k}">{e(v)}</option>' for k, v in d.items())  # noqa: E731
    return f"""<form method="post" action="/patient/{p['id']}/event" class="subcard pinkish" id="deliver"><input type="hidden" name="type" value="delivery"><input type="hidden" name="key" value="d-{key}">
<h3>Record delivery outcome <small class="muted">(doctor) – creates the baby's record, adds the delivery package to the bill and checks for PPH</small></h3>
<div class="row"><div><label>Mode</label><select name="mode" required><option value="">choose…</option>{opt(events.DELIVERY_MODES)}</select></div>
<div><label>Outcome</label><select name="outcome" required>{opt(events.OUTCOMES)}</select></div>
<div><label>Time of birth</label><input name="delivered_at" type="datetime-local"></div><div><label>Baby no.</label><input name="baby_number" type="number" value="1" min="1" max="4"></div></div>
<div class="row"><div><label>Sex</label><select name="sex"><option value="">—</option><option value="F">Girl</option><option value="M">Boy</option><option value="ambiguous">Ambiguous</option></select></div>
<div><label>Birth weight (g)</label><input name="weight_g" type="number" min="300" max="6500" required></div>
<div><label>APGAR 1 min</label><input name="apgar1" type="number" min="0" max="10"></div><div><label>APGAR 5 min</label><input name="apgar5" type="number" min="0" max="10"></div>
<div><label>Blood loss (ml)</label><input name="ebl_ml" type="number" min="0" max="6000" required></div></div>
<div class="row"><div><label>Placenta</label><select name="placenta">{opt(events.PLACENTA)}</select></div><div><label>Perineum</label><select name="perineum">{opt(events.PERINEUM)}</select></div>
<div style="grid-column:span 2"><label>Indication (required for LSCS)</label><input name="indication" maxlength="200"></div></div>
<div class="row"><div style="grid-column:span 3"><label>Complications / notes</label><input name="complications" maxlength="500"></div>
<div><label><input type="checkbox" name="resuscitation" value="1" style="width:auto"> Baby needed resuscitation</label></div></div>
<button>Save delivery</button></form>"""


def _delivery_summary(p) -> str:
    out = []
    for d in record.delivery(CONN, p["id"]):
        sex = {"M": "Boy", "F": "Girl"}.get(d.get("sex"), d.get("sex") or "—")
        out.append(f"""<div class="kv"><div>Delivered</div><div>{dt(d['ts'])} <span class="cite">{d['cite']}</span> · by {e(d['by'] or '')}</div>
<div>Mode</div><div><b>{e(d['mode_text'])}</b>{(' – ' + e(d['indication'])) if d.get('indication') else ''}</div>
<div>Outcome</div><div>{e(d['outcome_text'])}</div><div>Baby</div><div>{sex}, {d['weight_g']} g{f", APGAR {d.get('apgar1')}/{d.get('apgar5')}" if d.get('apgar5') is not None else ''}</div>
<div>Blood loss</div><div>{d['ebl_ml']} ml</div><div>Placenta / perineum</div><div>{e(events.PLACENTA.get(d.get('placenta'), '—'))} / {e(events.PERINEUM.get(d.get('perineum'), '—'))}</div></div>""")
    babies = "".join(f'<a class="baby-chip big" href="/patient/{b["id"]}">👶 Open {e(b["name"])}’s record →</a>'
                     for b in CONN.execute("SELECT * FROM patients WHERE mother_id=?", (p["id"],)))
    return f'<section class="card" id="delivery"><h2>Delivery outcome</h2>{"<hr>".join(out)}<div style="margin-top:10px">{babies}</div></section>'


# ---------------------------------------------------------------------------- newborn
def _newborn_section(p, user, key) -> str:
    pid = p["id"]
    obs = record.vitals(CONN, pid, 30)
    rows = "".join(f"<tr><td>{dt(o['ts'])}</td><td>{o.get('temp_c', '—')}</td><td>{o.get('hr', '—')}</td><td>{o.get('rr', '—')}</td><td>{o.get('spo2', '—')}</td>"
                   f"<td>{o.get('weight_g', '—')}</td><td>{e(rules.FEEDING.get(o.get('feeding'), '—'))}</td><td>{e(rules.JAUNDICE.get(o.get('jaundice'), '—'))}</td>"
                   f"<td>{e(o['by'] or '')}</td><td><span class='cite'>{o['cite']}</span></td></tr>" for o in obs[::-1])
    form = ""
    if auth.can(user, "enter_obs") and p["status"] != "discharged":
        sel = lambda n, d: f'<select name="{n}"><option value="">—</option>' + "".join(f'<option value="{k}">{e(v)}</option>' for k, v in d.items()) + "</select>"  # noqa: E731
        form = f"""<form method="post" action="/patient/{pid}/event" class="subcard mintish"><input type="hidden" name="type" value="newborn_obs"><input type="hidden" name="key" value="nb-{key}">
<h3>Newborn observations</h3><div class="row"><div><label>Temp °C</label><input name="temp_c" type="number" step="0.1"></div><div><label>Heart rate</label><input name="hr" type="number"></div>
<div><label>Resp. rate</label><input name="rr" type="number"></div><div><label>SpO₂ %</label><input name="spo2" type="number"></div><div><label>Weight (g)</label><input name="weight_g" type="number"></div></div>
<div class="row"><div><label>Feeding</label>{sel('feeding', rules.FEEDING)}</div><div><label>Jaundice</label>{sel('jaundice', rules.JAUNDICE)}</div>
<div><label>Activity</label>{sel('activity', rules.ACTIVITY)}</div><div><label>Umbilicus</label>{sel('cord', rules.CORD)}</div>
<div><label>Urine / stool passed</label><div class="pair"><select name="urine"><option value="">—</option><option>yes</option><option>no</option></select><select name="stool"><option value="">—</option><option>yes</option><option>no</option></select></div></div></div>
<label><input type="checkbox" name="chest_indrawing" value="1" style="width:auto"> Severe chest indrawing</label>
<div style="margin-top:8px"><button>Save newborn observations</button> <small class="muted">Checked instantly against newborn danger-sign rules (WHO/IMNCI).</small></div></form>"""
    return f"""<section class="card" id="newborn"><h2>Newborn care</h2><div class="table-scroll"><table><tr><th>Time</th><th>T °C</th><th>HR</th><th>RR</th><th>SpO₂</th><th>Wt g</th><th>Feeding</th><th>Jaundice</th><th>By</th><th></th></tr>{rows}</table></div>{form}</section>"""


def _vaccine_section(p, user, key) -> str:
    pid = p["id"]
    vac = record.vaccinations(CONN, pid)
    can = auth.can(user, "give_meds") and p["status"] != "discharged"
    stock = {r["item_code"]: r["stock"] for r in CONN.execute("SELECT item_code, stock FROM inventory")}
    rows = []
    for d in vac["birth_doses"]:
        if d["status"] == "given":
            state = badge(f"given {dt(d['given_at'])}", "green")
        else:
            state = badge(d["status"] + f" (within {d['deadline_h']} h of birth)", "red" if d["status"] == "overdue" else "yellow")
            if can:
                state += (f' <form method="post" action="/patient/{pid}/event" class="inline"><input type="hidden" name="type" value="vaccine">'
                          f'<input type="hidden" name="key" value="vac-{d["code"]}-{key}"><input type="hidden" name="code" value="{d["code"]}">'
                          f'<input name="batch" placeholder="batch no." style="width:110px" maxlength="40"> <button class="small">Give now</button></form>'
                          f' <small class="muted">stock {stock.get(d["item_code"], 0)}</small>')
        rows.append(f"<tr><td><b>{e(d['name'])}</b></td><td>{state}</td></tr>")
    sched = "".join(f"<tr><td>{e(v['visit'])}</td><td>{v['due_date']}</td><td>{e(', '.join(x['name'] for x in v['vaccines']))}</td></tr>"
                    for v in vaccines.due_list(p["birth_at"]))
    return f"""<section class="card" id="vaccines"><h2>Vaccines – Indian Academy of Pediatrics schedule</h2>
<div class="two"><div><h3>Birth doses (given in hospital)</h3><table>{''.join(rows)}</table>
<p class="muted small-print">Giving a dose deducts it from vaccine stock and adds it to the bill – once only.</p></div>
<div><h3>Vaccination card – due dates</h3><div class="table-scroll short"><table><tr><th>Visit</th><th>Due</th><th>Vaccines</th></tr>{sched}</table></div>
<p class="muted small-print">{e(vaccines.SOURCE_NOTE)}</p></div></div></section>"""


# ---------------------------------------------------------------------------- bedside
def _bedside_section(p, user, key) -> str:
    pid = p["id"]
    if not auth.can(user, "enter_obs"):
        return ""
    is_baby = p["kind"] == "baby"
    inv = CONN.execute("SELECT * FROM inventory WHERE category<>'vaccine' ORDER BY name").fetchall()
    auto = set(events.DAILY_CHARGES) | set(events.ROOM_CHARGES.values()) | set(events.ADMISSION_CHARGES) | set(events.LIVE_BIRTH_CHARGES) | {c for cs in events.DELIVERY_PACKAGE.values() for c in cs}
    tar = [t for t in CONN.execute("SELECT * FROM tariff ORDER BY name") if t["code"] not in auto]  # auto-billed codes are never entered by hand
    warn_cls = {}
    for i in inv:
        chk = rules.allergy_check(p["allergies"], i["name"], i["drug_class"])
        warn_cls[i["item_code"]] = chk["level"]
    inv_opts = "".join(f'<option value="{e(i["item_code"])}">{"⚠ " if warn_cls[i["item_code"]] != "ok" else ""}{e(i["name"])} (stock {i["stock"]}, {money(i["unit_price"])}/{e(i["unit"])})</option>' for i in inv)
    tar_opts = "".join(f'<option value="{e(t["code"])}">{e(t["name"])} – {money(t["price"])}</option>' for t in tar)
    override = ""
    if auth.can(user, "override_allergy"):
        override = '<div><label>Allergy override reason (doctor, only if warned)</label><input name="override_reason" maxlength="300" placeholder="leave empty unless the system blocks the medicine"></div>'
    obs_form = "" if is_baby else f"""<form method="post" action="/patient/{pid}/event"><input type="hidden" name="type" value="vitals"><input type="hidden" name="key" value="v-{key}">
<h3>Mother's observations</h3><div class="row"><div><label>Systolic BP</label><input name="sbp" type="number"></div><div><label>Diastolic BP</label><input name="dbp" type="number"></div>
<div><label>Pulse /min</label><input name="hr" type="number"></div><div><label>Resp. rate</label><input name="rr" type="number"></div></div>
<div class="row"><div><label>Temp °C</label><input name="temp_c" type="number" step="0.1"></div><div><label>SpO₂ %</label><input name="spo2" type="number"></div>
<div><label>Fetal HR</label><input name="fhr" type="number"></div><div><label>AVPU</label><select name="avpu"><option value="alert">Alert</option><option value="voice">Voice</option><option value="pain">Pain</option><option value="unresponsive">Unresponsive</option></select></div></div>
<button>Save observations</button> <small class="muted">Checked instantly against MEOWS red-flag rules.</small></form>"""
    lab_inputs = "".join(f'<div><label>{e(lab)} <small>{e(unit)}</small></label><input name="lab_{k}" type="number" step="0.1"></div>'
                         for k, (lab, unit) in rules.LAB_FIELDS.items() if (k == "bilirubin") == is_baby or k == "hb")
    up = "" if is_baby else ('<div><label>Urine protein</label><select name="lab_urine_protein"><option value="">—</option>'
                             + "".join(f"<option>{u}</option>" for u in rules.URINE_PROTEIN) + "</select></div>")
    lab_form = f"""<form method="post" action="/patient/{pid}/event"><input type="hidden" name="type" value="investigation"><input type="hidden" name="key" value="i-{key}">
<h3>Lab result</h3><div class="row"><div><label>Test</label><input name="test" required maxlength="80" placeholder="{'Serum bilirubin' if is_baby else 'e.g. CBC, LFT, RFT, urine'}"></div>{lab_inputs}{up}</div>
<div class="row"><div style="grid-column:span 3"><label>Other result / comment</label><input name="result_text" maxlength="500"></div>
<div><label>Bill test</label><select name="code"><option value="">no charge (already billed)</option>{tar_opts}</select></div></div>
<button class="secondary">Save result</button> <small class="muted">Checked against lab red-flag rules.</small></form>"""
    return f"""<section class="card" id="bedside"><h2>Bedside entry <small class="muted">(entered digitally by nurse / doctor)</small></h2>
<div class="two"><div>{obs_form}{lab_form}</div>
<div><form method="post" action="/patient/{pid}/event"><input type="hidden" name="type" value="med_admin"><input type="hidden" name="key" value="m-{key}">
<h3>Medicine given</h3><div class="row"><div style="grid-column:span 2"><label>Medicine (from pharmacy stock){' – ⚠ = allergy warning for this patient' if any(v != 'ok' for v in warn_cls.values()) else ''}</label><select name="item_code">{inv_opts}</select></div></div>
<div class="row"><div><label>Units</label><input name="qty" type="number" value="1" min="1"></div><div><label>Dose</label><input name="dose" placeholder="e.g. 1 g"></div>
<div><label>Route</label><select name="route"><option>oral</option><option>IV</option><option>IM</option><option>SC</option><option>PV</option><option>PR</option></select></div></div>
{override}<button>Record administration</button> <small class="muted">Allergy-checked; deducts stock and bills once.</small></form>
<form method="post" action="/patient/{pid}/event" style="margin-top:14px"><input type="hidden" name="type" value="service"><input type="hidden" name="key" value="s-{key}">
<h3>Service / procedure</h3><div class="row"><div style="grid-column:span 2"><select name="code">{tar_opts}</select></div><div><input name="qty" type="number" value="1" min="1"></div></div>
<button class="secondary">Add service</button></form>
<form method="post" action="/patient/{pid}/event" style="margin-top:14px"><input type="hidden" name="type" value="note"><input type="hidden" name="key" value="n-{key}">
<h3>Clinical note</h3><div class="row"><div><select name="category"><option value="progress">Progress note</option><option value="plan">Plan</option><option value="nursing">Nursing note</option>
<option value="discharge_advice">Discharge medicines &amp; advice</option><option value="follow_up">Follow-up instruction</option></select></div></div>
<textarea name="text" maxlength="4000" placeholder="e.g. Reviewed by consultant: continue labetalol 200 mg BD. Plan discharge day 3."></textarea>
<button class="secondary" style="margin-top:6px">Add note</button></form></div></div></section>"""


# ---------------------------------------------------------------------------- alerts, timeline
def render_alert(a, user) -> str:
    reasons = json.loads(a["reasons"])
    cls = "alert" + (" yellow" if a["level"] == "yellow" else "") + (" ack" if a["status"] != "pending" else "")
    p = CONN.execute("SELECT name, bed FROM patients WHERE id=?", (a["patient_id"],)).fetchone()
    if a["status"] == "pending":
        if auth.can(user, "ack_alert"):
            action = (f'<form method="post" action="/alerts/{a["id"]}/ack" class="inline" style="float:right">'
                      f'<input type="hidden" name="back" value="/patient/{a["patient_id"]}#alerts"><button class="small">Acknowledge</button></form>')
        else:
            action = '<span style="float:right">' + badge("pending consultant review", "red" if a["level"] == "red" else "yellow") + "</span>"
    else:
        action = '<span style="float:right">' + badge(f"acknowledged by {a['acknowledged_by']} · {dt(a['acknowledged_at'])}", "green") + "</span>"
    src = " · " + badge("from AI-read paper – verify", "blue") if a["source"] == "document" else ""
    return f"""<div class="{cls}">{action}<b>{e(a['level'].upper())} · {e((a['category'] or '').upper())} · A{a['id']}</b> · {dt(a['ts'])} · <a href="/patient/{a['patient_id']}">{e(p['name'])}, bed {e(p['bed'])}</a>{src}
<div style="font-size:14px;margin-top:4px">{'<br>'.join(e(r) for r in reasons)}</div><div class="muted small-print">Score {e(a['score'] or '')} · triggered by E{a['event_id']}</div></div>"""


def _alerts_section(p, user) -> str:
    al = CONN.execute("SELECT * FROM alerts WHERE patient_id=? ORDER BY status='pending' DESC, ts DESC, id DESC", (p["id"],)).fetchall()
    return (f'<section class="card" id="alerts"><h2>Red-flag alerts → consultant</h2>'
            + ("".join(render_alert(a, user) for a in al) or "<p class='muted'>No alerts.</p>") + "</section>")


def _timeline_section(p) -> str:
    rows = []
    for r in CONN.execute("SELECT * FROM events WHERE patient_id=? ORDER BY ts DESC, id DESC", (p["id"],)):
        src = badge("from AI-read paper", "blue") if r["source"] == "document" else ""
        rows.append(f"<tr><td class='mono'>E{r['id']}</td><td>{dt(r['ts'])}</td><td>{badge(EVENT_LABEL.get(r['type'], r['type']), 'lilac')}</td>"
                    f"<td>{event_text(r)}</td><td>{e(r['entered_by'] or '')} {src}</td></tr>")
    return (f"<section class='card' id='timeline'><h2>Timeline</h2><div class='table-scroll tall'><table><tr><th>ID</th><th>Time</th><th>Type</th><th>Detail</th>"
            f"<th>By</th></tr>{''.join(rows)}</table></div></section>")


# ---------------------------------------------------------------------------- drafts & discharge
def render_draft(d, user) -> str:
    sections = json.loads(d["content"])
    g = json.loads(d["grounding"])
    app = json.loads(d["appendix"] or "[]")
    title = drafts.KINDS[d["kind"]]["title"]
    ok = badge("grounding check passed", "green") if g["passed"] else badge("grounding check: issues", "red")
    issues = ""
    if g["unsupported_numbers"]:
        issues += f'<div class="danger-box">Numbers not found anywhere in the record (highlighted): {e(", ".join(g["unsupported_numbers"]))}. Check before approving.</div>'
    if g["invalid_citations"]:
        issues += f'<div class="danger-box">Invalid citations: {e(", ".join(g["invalid_citations"]))}</div>'
    if g["sections_without_citations"]:
        issues += f'<div class="warn">Sections without citations: {e(", ".join(g["sections_without_citations"]))}</div>'
    body = "".join(f"<h4>{e(s['heading'])}</h4><div>{cite_html(s['text'], g['unsupported_numbers'])}</div>" for s in sections)
    body += render_appendix(app)
    if d["status"] == "approved":
        state = badge(f"APPROVED by {d['approved_by']} · {dt(d['approved_at'])}", "green") + f' <a class="small-print" href="/print/{d["id"]}" target="_blank">Print ↗</a>'
    elif auth.can(user, "approve_draft"):
        state = (badge("DRAFT – not for use until approved", "yellow") +
                 f""" <form method="post" action="/drafts/{d['id']}/approve" class="inline"><input type="hidden" name="back" value="/patient/{d['patient_id']}#docs">
<button class="small">Approve as {e(user['full_name'])}</button></form>""")
    else:
        state = badge("DRAFT – awaiting doctor approval", "yellow")
    return f"""<div class="draft"><b>{e(title)}</b> · #{d['id']} · {dt(d['created_at'])} · {ok}<div style="margin:6px 0">{state}</div>{issues}{body}</div>"""


def render_appendix(app: list) -> str:
    out = ""
    for blk in app:
        rows = "".join(f"<tr><td>{e(str(a))}</td><td>{e(str(b))}</td></tr>" for a, b in blk["rows"])
        out += (f"<h4>{cite_html(blk['heading'])} <small class='badge b-mint'>from structured record – not AI</small></h4><table class='appx'>{rows}</table>"
                + (f"<p class='muted small-print'>{e(blk['note'])}</p>" if blk.get("note") else ""))
    return out


def _docs_section(p, user) -> str:
    pid = p["id"]
    dr = CONN.execute("SELECT * FROM drafts WHERE patient_id=? ORDER BY id DESC LIMIT 4", (pid,)).fetchall()
    btns = ""
    if auth.can(user, "draft") and p["status"] != "discharged":
        kinds = [k for k, v in drafts.KINDS.items() if p["kind"] in v["for"]]
        btns = "".join(f'<form method="post" action="/patient/{pid}/draft" data-busy class="inline"><input type="hidden" name="kind" value="{k}">'
                       f'<button class="{"secondary" if k == "handover" else ""}">Draft {e(drafts.KINDS[k]["title"].lower())}</button>'
                       f'<span class="spinner">Drafting… ~20 s</span></form> ' for k in kinds)
    disc = _discharge_box(p, user) if p["kind"] == "mother" else (
        f'<p class="muted">The baby is discharged together with the mother from <a href="/patient/{p["mother_id"]}#docs">her record</a>.</p>')
    return f"""<section class="card" id="docs"><h2>Handover &amp; discharge</h2>
<p class="muted">The Summary Writer drafts from the accumulated record; a grounding check verifies every citation and number. Delivery details and vaccination dates are added by code, not AI.
Nothing is final until a doctor approves it.</p>{btns}{''.join(render_draft(d, user) for d in dr)}{disc}</section>"""


def _discharge_box(p, user) -> str:
    pid = p["id"]
    if p["status"] == "discharged":
        fb = CONN.execute("SELECT * FROM final_bills WHERE patient_id=?", (pid,)).fetchone()
        n = CONN.execute("SELECT COUNT(*) FROM reminders r JOIN patients q ON q.id=r.patient_id WHERE q.id=? OR q.mother_id=?", (pid, pid)).fetchone()[0]
        return (f'<div class="subcard mintish"><h3>Discharged</h3>Final family bill {money(fb["total"]) if fb else "—"} · '
                f'<a href="/bill/{pid}" target="_blank">Print bill ↗</a> · {n} email reminder(s) drafted – <a href="/reminders">see reminders</a></div>')
    if not auth.can(user, "discharge"):
        return ""
    bl = discharge.blockers(CONN, pid)
    missing = discharge.missing_birth_doses(CONN, pid)
    fu = CONN.execute("SELECT data FROM events WHERE patient_id=? AND type='note' AND json_extract(data,'$.category')='follow_up' ORDER BY ts DESC LIMIT 1", (pid,)).fetchone()
    fu_text = json.loads(fu["data"])["text"] if fu else ""
    checks = "".join(f"<li class='bad'>✗ {e(b)}</li>" for b in bl) or "<li class='good'>✓ Summaries approved, no pending red alerts</li>"
    checks += "".join(f"<li class='bad'>✗ Birth dose not given: {e(m)}</li>" for m in missing)
    defer = ('<div><label>Reason birth doses are deferred (only if not given)</label><input name="deferral_note" maxlength="300"></div>' if missing else "")
    has_baby = CONN.execute("SELECT 1 FROM patients WHERE mother_id=?", (pid,)).fetchone()
    return f"""<form method="post" action="/patient/{pid}/discharge" class="subcard lilacish" data-confirm="Discharge now? This freezes the family bill and closes the record."><h3>Discharge mother{' and baby' if has_baby else ''}</h3>
<ul class="checks">{checks}</ul>
<div class="row"><div><label>Follow-up visit after (days)</label><input name="follow_up_days" type="number" min="1" max="120" value="7"></div>
<div style="grid-column:span 2"><label>Follow-up reason (goes into the reminder email)</label><input name="follow_up_note" maxlength="200" value="{e(fu_text[:200])}"></div></div>
<div class="row"><div><label>Family email for reminders</label><input name="email" type="email" value="{e(p['email'] or '')}" maxlength="120"></div>{defer}</div>
<button{' disabled' if bl else ''}>Discharge &amp; finalise bill</button> <small class="muted">Freezes the family bill and drafts email reminders for the consultant to authorise.</small></form>"""


def _bill_section(p) -> str:
    b = events.bill(CONN, p["id"])
    lines = "".join(f"<tr><td>{dt(l['ts'])}</td><td>{e(l['description'])}</td><td class='num'>{l['qty']:g}</td><td class='num'>{money(l['unit_price'])}</td>"
                    f"<td class='num'>{money(l['amount'])}</td><td class='mono'>{'E' + str(l['event_id']) if l['event_id'] else 'auto'}</td></tr>" for l in b["lines"])
    fam = ""
    if p["kind"] == "mother" and CONN.execute("SELECT 1 FROM patients WHERE mother_id=?", (p["id"],)).fetchone():
        fb = events.family_bill(CONN, p["id"])
        fam = f'<p><b>Family total (mother + baby): {money(fb["total"])}</b> · <a href="/bill/{p["id"]}" target="_blank">Printable family bill ↗</a></p>'
    note = "Babies room in with the mother: no bed charge." if p["kind"] == "baby" else ""
    return f"""<section class="card" id="bill"><h2>Running bill (INR)</h2><p class="muted">Calculated by fixed rules from recorded services, medicines and vaccines only – a medicine merely mentioned in a note is never billed. {note}</p>
<table><tr><th>Date</th><th>Item</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th><th>Source</th></tr>{lines}
<tr><th colspan="4">Total</th><th class="num">{money(b['total'])}</th><th></th></tr></table>{fam}</section>"""


# ============================================================================ other pages
def view_alerts(user, msg="", err=""):
    pend = CONN.execute("SELECT * FROM alerts WHERE status='pending' ORDER BY CASE level WHEN 'red' THEN 0 ELSE 1 END, ts DESC").fetchall()
    done = CONN.execute("SELECT * FROM alerts WHERE status<>'pending' ORDER BY acknowledged_at DESC LIMIT 20").fetchall()
    hint = "" if auth.can(user, "ack_alert") else '<div class="warn">Only a consultant can acknowledge alerts. Sign in as a consultant to do so.</div>'
    return page("Alerts", f"""<h1>Consultant alerts</h1><p class="sub">Raised automatically by fixed rules (not by AI): maternal MEOWS, labour (WHO LCG), newborn danger signs, labs, delivery and allergy overrides.</p>{hint}
<h2>Pending ({len(pend)})</h2>{''.join(render_alert(a, user) for a in pend) or '<p class="muted">None pending.</p>'}
<h2 style="margin-top:20px">Recently acknowledged</h2>{''.join(render_alert(a, user) for a in done) or '<p class="muted">None.</p>'}""", user, msg, err)


def view_reminders(user, msg="", err=""):
    blocks = []
    for status, title in (("draft", "Awaiting consultant authorisation"), ("authorised", "Authorised – will send on the due date"),
                          ("sent", "Sent"), ("cancelled", "Cancelled")):
        rows = CONN.execute("SELECT r.*, p.name FROM reminders r JOIN patients p ON p.id=r.patient_id WHERE r.status=? ORDER BY r.due_date", (status,)).fetchall()
        cards = []
        for r in rows:
            acts = ""
            if status == "draft":
                if auth.can(user, "authorise_reminders"):
                    acts += (f'<form method="post" action="/reminders/{r["id"]}/authorise" class="inline"><button class="small">Authorise</button></form> ')
                if auth.can(user, "draft_reminders"):
                    acts += (f'<form method="post" action="/reminders/{r["id"]}/email" class="inline"><input name="email" type="email" placeholder="family email" value="{e(r["to_email"] or "")}" style="width:220px">'
                             f' <button class="small secondary">Save email</button></form> ')
            if status == "authorised" and auth.can(user, "authorise_reminders"):
                acts += f'<form method="post" action="/reminders/{r["id"]}/send" class="inline"><button class="small">Send now</button></form> '
            if status in ("draft", "authorised") and auth.can(user, "draft_reminders"):
                acts += f'<form method="post" action="/reminders/{r["id"]}/cancel" class="inline"><button class="small ghost">Cancel</button></form>'
            meta = (f"To {e(r['to_email'] or '— (no email yet)')} · send on {r['due_date']} ({reminders.due_label(r['due_date'])}) · drafted by {e(r['created_by'] or '')}"
                    + (f" · authorised by {e(r['authorised_by'])}" if r["authorised_by"] else "") + (f" · sent {dt(r['sent_at'])} via {e(r['sent_via'])}" if r["sent_at"] else ""))
            cards.append(f"""<div class="remind {status}"><div class="r-head">{badge('Follow-up' if r['kind'] == 'follow_up' else 'Vaccination', 'lilac' if r['kind'] == 'follow_up' else 'mint')}
<b>{e(r['subject'])}</b> · <a href="/patient/{r['patient_id']}">{e(r['name'])}</a></div><div class="muted small-print">{meta}</div>
<details><summary>Preview email (English + Hindi)</summary><pre class="email">{e(r['body'])}</pre></details><div style="margin-top:6px">{acts}</div></div>""")
        blocks.append(f"<div class='card'><h2>{title} ({len(rows)})</h2>{''.join(cards) or '<p class=muted>None.</p>'}</div>")
    smtp = (f"SMTP server {e(config.SMTP_HOST)}" if config.SMTP_HOST else "no SMTP server configured, so authorised emails are written to <span class='mono'>data/outbox/</span> instead of being sent")
    hint = "" if auth.can(user, "authorise_reminders") else '<div class="warn">Only a consultant can authorise reminders.</div>'
    return page("Reminders", f"""<h1>Email reminders</h1><p class="sub">Drafted automatically at discharge from fixed templates – the mother's follow-up visit and the baby's IAP vaccination visits.
<b>Nothing is sent unless a consultant authorises it.</b> Delivery: {smtp}.</p>{hint}{''.join(blocks)}""", user, msg, err)


def view_billing(user, msg="", err=""):
    rows = []
    for m in CONN.execute("SELECT * FROM patients WHERE kind='mother' ORDER BY status='discharged', admitted_at DESC"):
        fb = CONN.execute("SELECT * FROM final_bills WHERE patient_id=?", (m["id"],)).fetchone()
        total = fb["total"] if fb else events.family_bill(CONN, m["id"])["total"]
        if fb and fb["paid_at"]:
            state = badge(f"paid · {fb['payment_mode']} · {dt(fb['paid_at'])}", "green")
        elif fb:
            state = badge("final – awaiting payment", "yellow")
            if auth.can(user, "take_payment"):
                state += (f' <form method="post" action="/billing/{m["id"]}/pay" class="inline"><select name="mode" style="width:auto"><option value="cash">Cash</option>'
                          f'<option value="upi">UPI</option><option value="card">Card</option><option value="insurance">Insurance</option><option value="scheme">Govt scheme</option></select>'
                          f' <button class="small">Record payment</button></form>')
        else:
            state = badge("running (in hospital)", "blue")
        rows.append(f"<tr><td>{e(m['uhid'])}</td><td>{e(m['name'])}</td><td>{e(record.phase(CONN, m))}</td><td class='num'>{money(total)}</td>"
                    f"<td>{state}</td><td><a href='/bill/{m['id']}' target='_blank'>View / print ↗</a></td></tr>")
    return page("Billing", f"""<h1>Billing</h1><p class="sub">Family bills (mother + baby) built by fixed rules from recorded room days, packages, services, medicines and vaccines. Frozen at discharge.</p>
<div class="card"><table><tr><th>UHID</th><th>Mother</th><th>Status</th><th class="num">Family total</th><th>Bill</th><th></th></tr>{''.join(rows)}</table></div>""", user, msg, err)


def view_bill_print(mother_id: int, user):
    m = CONN.execute("SELECT * FROM patients WHERE id=? AND kind='mother'", (mother_id,)).fetchone()
    if not m:
        return None
    fb = CONN.execute("SELECT * FROM final_bills WHERE patient_id=?", (mother_id,)).fetchone()
    fam = json.loads(fb["data"]) if fb else events.family_bill(CONN, mother_id)
    parts = ""
    for part in fam["parts"]:
        lines = "".join(f"<tr><td>{dt(l['ts'])}</td><td>{e(l['description'])}</td><td class='num'>{l['qty']:g}</td><td class='num'>{money(l['unit_price'])}</td>"
                        f"<td class='num'>{money(l['amount'])}</td></tr>" for l in part["lines"])
        parts += f"<h3>{e(part['name'])}</h3><table><tr><th>Date</th><th>Item</th><th class='num'>Qty</th><th class='num'>Rate</th><th class='num'>Amount</th></tr>{lines}<tr><th colspan=4>Subtotal</th><th class='num'>{money(part['total'])}</th></tr></table>"
    status = (f"FINAL bill · finalised {dt(fb['finalized_at'])} by {e(fb['finalized_by'])}" + (f" · PAID ({e(fb['payment_mode'])}) {dt(fb['paid_at'])}" if fb["paid_at"] else " · payment pending")
              if fb else "PROVISIONAL – patient still in hospital")
    body = f"""<div class="print"><div class="print-head"><div><h1>{e(config.HOSPITAL_NAME)}</h1><div class="muted">{e(config.HOSPITAL_PHONE)}</div></div>
<div style="text-align:right"><b>Bill – {e(m['name'])}</b><br>{e(m['uhid'])} · admitted {dt(m['admitted_at'])}<br>{status}</div></div>{parts}
<h2 style="text-align:right">Family total: {money(fam['total'])}</h2><p class="muted small-print">Synthetic demo bill. Generated by CareFlow AI from recorded events (deterministic, no AI).</p>
<button class="noprint print-btn">Print</button></div>"""
    return page("Bill", body, user, bare=True)


def view_draft_print(draft_id: int, user):
    d = CONN.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if not d:
        return None
    p = CONN.execute("SELECT * FROM patients WHERE id=?", (d["patient_id"],)).fetchone()
    sections = json.loads(d["content"])
    body = "".join(f"<h3>{e(s['heading'])}</h3><p>{cite_html(s['text'])}</p>" for s in sections) + render_appendix(json.loads(d["appendix"] or "[]"))
    stamp = (f"Approved by {e(d['approved_by'])} on {dt(d['approved_at'])}" if d["status"] == "approved" else "DRAFT – NOT APPROVED – NOT FOR USE")
    return page("Print", f"""<div class="print"><div class="print-head"><div><h1>{e(config.HOSPITAL_NAME)}</h1><div class="muted">{e(drafts.KINDS[d['kind']]['title'])}</div></div>
<div style="text-align:right"><b>{e(p['name'])}</b><br>{e(p['uhid'])}<br>{stamp}</div></div>{body}
<p class="muted small-print">Codes in boxes (E12, D2.p1) point to the source entry in the electronic record. Synthetic demo patient.</p>
<button class="noprint print-btn">Print</button></div>""", user, bare=True)


def view_inventory(user, msg="", err=""):
    inv = CONN.execute("SELECT * FROM inventory ORDER BY category, name").fetchall()
    rows = "".join(f"<tr><td class='mono'>{e(i['item_code'])}</td><td>{e(i['name'])}</td><td>{badge(i['category'], 'mint' if i['category'] == 'vaccine' else 'lilac')}</td>"
                   f"<td class='num'>{i['stock']} {e(i['unit'])}</td><td class='num'>{i['reorder_level']}</td>"
                   f"<td class='num'>{money(i['unit_price'])}</td><td>{badge('reorder', 'red') if i['stock'] <= i['reorder_level'] else badge('ok', 'green')}</td></tr>" for i in inv)
    led = "".join(f"<tr><td>{dt(r['ts'])}</td><td class='mono'>{e(r['item_code'])}</td><td class='num'>{'+' if r['delta'] > 0 else ''}{r['delta']}</td>"
                  f"<td class='mono'>{'E' + str(r['event_id']) if r['event_id'] else '—'}</td><td>{e(r['reason'])}</td><td>{e(r['by_user'] or '')}</td></tr>"
                  for r in CONN.execute("SELECT * FROM stock_ledger ORDER BY id DESC LIMIT 40"))
    restock = ""
    if auth.can(user, "restock"):
        opts = "".join(f'<option value="{e(i["item_code"])}">{e(i["name"])}</option>' for i in inv)
        restock = f"""<div class="card narrow"><h2>Receive stock</h2><form method="post" action="/inventory/restock"><div class="row">
<div style="grid-column:span 2"><label>Item</label><select name="item_code">{opts}</select></div><div><label>Quantity</label><input name="qty" type="number" min="1" max="10000" required></div></div>
<label>Supplier / batch</label><input name="reason" maxlength="120" required placeholder="e.g. Batch B-2231 from district supplier"><button style="margin-top:10px">Add to stock</button></form></div>"""
    return page("Inventory", f"""<h1>Pharmacy &amp; vaccine stock (simulated)</h1><p class="sub">Stock goes down only when a medicine or vaccine is recorded as given; each event moves stock once (database-enforced).</p>
<div class="card"><table><tr><th>Code</th><th>Item</th><th>Type</th><th class="num">In stock</th><th class="num">Reorder at</th><th class="num">Price</th><th></th></tr>{rows}</table></div>
{restock}<div class="card"><h2>Stock ledger</h2><table><tr><th>Time</th><th>Item</th><th class="num">Change</th><th>Event</th><th>Reason</th><th>By</th></tr>{led}</table></div>""", user, msg, err)


def view_usage(user, msg="", err=""):
    by = CONN.execute("""SELECT feature, COUNT(*) n, SUM(input_tokens) i, SUM(cached_tokens) c, SUM(output_tokens) o, SUM(cost_usd) cost,
        AVG(latency_s) lat, SUM(CASE WHEN note='mock' THEN 1 ELSE 0 END) mocks FROM usage WHERE ok=1 GROUP BY feature ORDER BY feature""").fetchall()
    rows = "".join(f"<tr><td>{e(r['feature'])}</td><td class='num'>{r['n']}</td><td class='num'>{r['i']:,}</td><td class='num'>{r['c']:,}</td><td class='num'>{r['o']:,}</td>"
                   f"<td class='num'>{(r['i'] + r['o']) // r['n']:,}</td><td class='num'>${r['cost']:.5f}</td><td class='num'>{r['lat']:.1f}s</td><td>{badge('mock', 'yellow') if r['mocks'] else ''}</td></tr>" for r in by)
    users = {u["id"]: u["username"] for u in CONN.execute("SELECT id, username FROM users")}
    recent = "".join(f"<tr><td>{dt(r['ts'])}</td><td>{e(r['feature'])}</td><td>{r['patient_id'] or ''}</td><td>{e(users.get(r['user_id'], '') or '')}</td><td class='num'>{r['input_tokens']:,}</td><td class='num'>{r['cached_tokens']:,}</td>"
                     f"<td class='num'>{r['output_tokens']:,}</td><td class='num'>${r['cost_usd']:.5f}</td><td>{badge('ok', 'green') if r['ok'] else badge('failed', 'red')} {e(r['note'] or '')}</td></tr>"
                     for r in CONN.execute("SELECT * FROM usage ORDER BY id DESC LIMIT 40"))
    tot = CONN.execute("SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(cached_tokens),0) c, COALESCE(SUM(output_tokens),0) o, COALESCE(SUM(cost_usd),0) s FROM usage WHERE ok=1").fetchone()
    cache_pct = (tot["c"] / tot["i"] * 100) if tot["i"] else 0
    return page("AI usage", f"""<h1>AI usage &amp; cost</h1><p class="sub">Every model call is logged with real token counts returned by the API. Prices: ${config.PRICE_INPUT}/M input, ${config.PRICE_CACHED_INPUT}/M cached input, ${config.PRICE_OUTPUT}/M output ({config.MODEL}).</p>
<div class="metrics"><div class="metric t-lilac"><div class="v">{llm.tokens_today(CONN):,}</div><div class="l">tokens today (budget {config.DAILY_TOKEN_BUDGET:,})</div></div>
<div class="metric t-mint"><div class="v">{config.USER_DAILY_BUDGET:,}</div><div class="l">daily budget per user</div></div>
<div class="metric t-peach"><div class="v">{config.PER_PATIENT_DAILY_BUDGET:,}</div><div class="l">daily budget per patient</div></div>
<div class="metric t-blue"><div class="v">{cache_pct:.0f}%</div><div class="l">input tokens served from cache</div></div>
<div class="metric t-pink"><div class="v">${tot['s']:.4f}</div><div class="l">total AI cost (all time)</div></div></div>
<div class="card"><h2>By feature</h2><table><tr><th>Feature</th><th class="num">Calls</th><th class="num">Input</th><th class="num">Cached input</th><th class="num">Output</th><th class="num">Avg tokens/call</th><th class="num">Cost</th><th class="num">Avg latency</th><th></th></tr>{rows}</table></div>
<div class="card"><h2>Recent calls</h2><table><tr><th>Time</th><th>Feature</th><th>Patient</th><th>User</th><th class="num">Input</th><th class="num">Cached</th><th class="num">Output</th><th class="num">Cost</th><th>Status</th></tr>{recent}</table></div>""", user, msg, err)


def view_rules(user, msg="", err=""):
    def tbl(title, data, note):
        rows = "".join(f"<tr><td>{e(a)}</td><td>{badge(b, 'red')}</td><td>{badge(c, 'yellow') if c != '—' else '—'}</td></tr>" for a, b, c in data)
        return f"<div class='card'><h2>{title}</h2><table><tr><th>Parameter</th><th>Red</th><th>Yellow</th></tr>{rows}</table><p class='muted small-print'>{note}</p></div>"
    allergy = "".join(f"<tr><td>{e(k)}</td><td>{e(', '.join(sorted(v['block'])))}</td><td>{e(', '.join(sorted(v['caution'])) or '—')}</td></tr>" for k, v in rules.ALLERGY_RULES.items())
    return page("Rules", f"""<h1>Red-flag rules</h1><p class="sub">Deterministic code, not AI. An alert goes to the consultant for <b>any one red</b> finding or <b>two or more yellow</b> findings in one entry.
Demo thresholds – each hospital must set and validate its own.</p><div class="two">
{tbl('Mother – MEOWS', rules.THRESHOLD_TABLE, 'Modified Early Obstetric Warning Score (Singh et al., Anaesthesia 2012); FHR band per FIGO 2015.')}
{tbl('Labour – WHO Labour Care Guide', rules.LABOUR_TABLE, 'WHO Labour Care Guide (2020) alert criteria.')}
{tbl('Newborn – danger signs', rules.NEWBORN_TABLE, 'WHO / IMNCI young-infant danger signs.')}
{tbl('Lab results', rules.LAB_TABLE, 'Severe-feature thresholds for pre-eclampsia/HELLP and anaemia; newborn bilirubin simplified (use an hour-specific nomogram in practice).')}
<div class='card'><h2>Drug–allergy check</h2><table><tr><th>Allergy recorded</th><th>Blocked classes</th><th>Caution classes</th></tr>{allergy}</table>
<p class='muted small-print'>Uses only doctor-confirmed allergies. Blocked or caution medicines need a doctor's written override, which alerts the consultant.</p></div>
<div class='card'><h2>Delivery</h2><table><tr><td>Blood loss</td><td>{badge('≥500 ml vaginal / ≥1000 ml LSCS', 'red')}</td><td>{badge('≥80% of that', 'yellow')}</td></tr>
<tr><td>Placenta incomplete / manual removal, 3rd–4th degree tear, stillbirth</td><td>{badge('red', 'red')}</td><td>—</td></tr></table></div></div>""", user, msg, err)


def view_audit(user, msg="", err=""):
    rows = "".join(f"<tr><td>{dt(r['ts'])}</td><td>{e(r['actor'])}</td><td>{e(r['action'])}</td><td class='mono'>{e(r['detail'][:160])}</td></tr>"
                   for r in CONN.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 150"))
    return page("Audit", f"<h1>Audit log</h1><p class='sub'>Who did what, when – including sign-ins, failed sign-ins and lockouts. Passwords are never logged.</p>"
                f"<div class='card'><table><tr><th>Time</th><th>Actor</th><th>Action</th><th>Detail</th></tr>{rows}</table></div>", user, msg, err)
