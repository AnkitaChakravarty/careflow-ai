"""Email reminders to the family: follow-up visit for the mother and IAP vaccination visits for the baby.

- Drafted by code from fixed templates (English + Hindi) at discharge. Dates come from the doctor's
  follow-up instruction and the IAP schedule - never from the AI.
- NOTHING is sent unless a consultant has authorised that specific reminder. `deliver()` re-checks this.
- Authorised reminders go out on their due date (or when the consultant presses "Send now").
- Without SMTP settings the email is written to data/outbox/*.eml instead of being sent.
"""
from __future__ import annotations

import smtplib
import sqlite3
from datetime import date, timedelta
from email.message import EmailMessage

from . import config, vaccines
from .db import audit, now, parse_ts

STATUS = {"draft": "Awaiting consultant authorisation", "authorised": "Authorised – will send on due date",
          "sent": "Sent", "cancelled": "Cancelled"}

DANGER_EN = ("Do not wait for the appointment – come to the hospital at once if there is heavy bleeding, fever, "
             "severe headache, blurred vision, fits, breathlessness or severe abdominal pain; or if the baby is not feeding, "
             "is very sleepy, has fast breathing, fever, or yellow palms/soles.")
DANGER_HI = ("अपॉइंटमेंट का इंतज़ार न करें – तेज़ रक्तस्राव, बुखार, तेज़ सिरदर्द, धुंधला दिखना, दौरे, साँस फूलना या पेट में तेज़ दर्द हो; "
             "या शिशु दूध न पी रहा हो, बहुत सुस्त हो, तेज़ साँस ले रहा हो, बुखार हो या हथेलियाँ/तलवे पीले हों – तो तुरंत अस्पताल आएँ।")


def _first_name(name: str) -> str:
    return name.replace("(synthetic)", "").strip().split(" ")[0]


def _fmt(d: date) -> str:
    return d.strftime("%d %b %Y")


def _insert(conn, pid, kind, due: date, to_email, subject, body, key, actor) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO reminders (patient_id, kind, due_date, to_email, subject, body, status, dedupe_key, "
                       "created_at, created_by) VALUES (?,?,?,?,?,?,'draft',?,?,?)",
                       (pid, kind, due.isoformat(), to_email, subject, body, key, now(), actor))
    return cur.rowcount == 1


def draft_for_discharge(conn: sqlite3.Connection, mother_id: int, follow_up_days: int | None, follow_up_note: str,
                        actor: str) -> int:
    """Create draft reminders for the mother's follow-up and each baby's next 3 vaccination visits."""
    m = conn.execute("SELECT * FROM patients WHERE id=?", (mother_id,)).fetchone()
    today = date.today()
    n = 0
    if follow_up_days:
        visit = today + timedelta(days=follow_up_days)
        note = (follow_up_note or "").strip() or "Postnatal check-up"
        body = (f"Dear {_first_name(m['name'])},\n\nThis is a reminder of your follow-up visit at {config.HOSPITAL_NAME} on "
                f"{_fmt(visit)}.\nReason: {note}\nPlease bring your discharge summary and all medicines.\n\n{DANGER_EN}\n"
                f"Phone: {config.HOSPITAL_PHONE}\n\n"
                f"नमस्ते {_first_name(m['name'])} जी,\n{config.HOSPITAL_NAME} में {_fmt(visit)} को आपकी फॉलो-अप जाँच है।\n"
                f"कारण: {note}\nकृपया डिस्चार्ज पर्ची और सभी दवाइयाँ साथ लाएँ।\n\n{DANGER_HI}\nफ़ोन: {config.HOSPITAL_PHONE}\n")
        n += _insert(conn, mother_id, "follow_up", max(today, visit - timedelta(days=1)), m["email"],
                     f"Follow-up visit on {_fmt(visit)} – {config.HOSPITAL_NAME}", body, f"{mother_id}-follow_up", actor)
    for b in conn.execute("SELECT * FROM patients WHERE mother_id=?", (mother_id,)):
        for v in vaccines.upcoming_visits(b["birth_at"], after_days=1, limit=3):
            due = date.fromisoformat(v["due_date"])
            names = ", ".join(x["name"] for x in v["vaccines"])
            body = (f"Dear {_first_name(m['name'])},\n\nYour baby's {v['visit']} vaccinations are due on {_fmt(due)} at "
                    f"{config.HOSPITAL_NAME}:\n{names}\n(Indian Academy of Pediatrics schedule.) Please bring the vaccination card.\n\n"
                    f"{DANGER_EN}\nPhone: {config.HOSPITAL_PHONE}\n\n"
                    f"नमस्ते {_first_name(m['name'])} जी,\nआपके शिशु के {v['visit']} के टीके {_fmt(due)} को लगने हैं:\n{names}\n"
                    f"कृपया टीकाकरण कार्ड साथ लाएँ।\n\n{DANGER_HI}\nफ़ोन: {config.HOSPITAL_PHONE}\n")
            n += _insert(conn, b["id"], "vaccination", max(today, due - timedelta(days=2)), b["email"] or m["email"],
                         f"Baby's {v['visit']} vaccinations due {_fmt(due)} – {config.HOSPITAL_NAME}", body,
                         f"{b['id']}-vac-{v['visit']}", actor)
    audit(conn, actor, "reminders_drafted", {"mother_id": mother_id, "count": n})
    conn.commit()
    return n


def set_email(conn, reminder_id: int, email: str, actor: str) -> None:
    email = (email or "").strip()[:120]
    if "@" not in email or " " in email:
        raise ValueError("Email address looks wrong.")
    conn.execute("UPDATE reminders SET to_email=? WHERE id=? AND status='draft'", (email, reminder_id))
    audit(conn, actor, "reminder_email_set", {"reminder_id": reminder_id})
    conn.commit()


def authorise(conn: sqlite3.Connection, reminder_id: int, consultant: str) -> None:
    r = conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
    if not r or r["status"] != "draft":
        raise ValueError("Only a draft reminder can be authorised.")
    if not r["to_email"]:
        raise ValueError("Add the family's email address before authorising.")
    conn.execute("UPDATE reminders SET status='authorised', authorised_by=?, authorised_at=? WHERE id=?",
                 (consultant, now(), reminder_id))
    audit(conn, consultant, "reminder_authorised", {"reminder_id": reminder_id})
    conn.commit()


def cancel(conn: sqlite3.Connection, reminder_id: int, actor: str) -> None:
    conn.execute("UPDATE reminders SET status='cancelled', cancelled_by=? WHERE id=? AND status IN ('draft','authorised')",
                 (actor, reminder_id))
    audit(conn, actor, "reminder_cancelled", {"reminder_id": reminder_id})
    conn.commit()


def deliver(conn: sqlite3.Connection, reminder_id: int, actor: str = "scheduler") -> str:
    """Send one reminder. Refuses anything a consultant has not authorised."""
    r = conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
    if not r or r["status"] != "authorised" or not r["authorised_by"]:
        raise ValueError("Not sent: a consultant must authorise this reminder first.")
    msg = EmailMessage()
    msg["From"] = f"{config.HOSPITAL_NAME} <{config.HOSPITAL_EMAIL}>"
    msg["To"] = r["to_email"]
    msg["Subject"] = r["subject"]
    msg.set_content(r["body"] + f"\n—\nAuthorised by {r['authorised_by']}. Sent by CareFlow AI.\n")
    if config.SMTP_HOST:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
            s.starttls()
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD or "")
            s.send_message(msg)
        via = f"SMTP {config.SMTP_HOST}"
    else:
        config.OUTBOX.mkdir(parents=True, exist_ok=True)
        path = config.OUTBOX / f"reminder-{r['id']:04d}-{r['kind']}.eml"
        path.write_bytes(bytes(msg))
        via = f"outbox file data/outbox/{path.name} (no SMTP configured)"
    conn.execute("UPDATE reminders SET status='sent', sent_at=?, sent_via=? WHERE id=?", (now(), via, reminder_id))
    audit(conn, actor, "reminder_sent", {"reminder_id": reminder_id, "via": via.split(" ")[0]})
    conn.commit()
    return via


def send_due(conn: sqlite3.Connection) -> int:
    """Called periodically by the app: sends authorised reminders whose due date has arrived."""
    n = 0
    for r in conn.execute("SELECT id FROM reminders WHERE status='authorised' AND due_date<=?", (date.today().isoformat(),)).fetchall():
        try:
            deliver(conn, r["id"])
            n += 1
        except Exception:  # noqa: BLE001 - one bad address must not stop the others
            continue
    return n


def due_label(due_date: str) -> str:
    d = parse_ts(due_date).date()
    delta = (d - date.today()).days
    return "today" if delta == 0 else (f"in {delta} days" if delta > 0 else f"{-delta} days ago")
