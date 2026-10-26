"""CareFlow AI - obstetric & newborn ward assistant (prototype, synthetic data only).

Run:   python app.py        then open http://127.0.0.1:8050 and sign in (demo accounts in README).
Uses only the Python standard library for the web server (no web framework to install).
"""
from __future__ import annotations

import email
import email.policy
import json
import re
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from careflow import (auth, config, db, discharge, drafts, events, extract, intake, llm, qa, reminders, views)

if config.MOCK:
    from careflow import mock
    mock.install()

CONN = db.connect()
db.init(CONN)
views.CONN = CONN
LOCK = threading.RLock()  # one writer at a time (SQLite + small ward)
STATIC = {"/static/style.css": "text/css; charset=utf-8", "/static/app.js": "text/javascript; charset=utf-8"}
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
FAILED_BY_IP: dict[str, list[float]] = {}
VACCINE_ROUTE = {"BCG": ("0.05 ml", "ID"), "OPV0": ("2 drops", "oral"), "HEPB1": ("0.5 ml", "IM"), "VITK": ("1 mg", "IM")}


class Forbidden(Exception):
    pass


def _ip_blocked(ip: str) -> bool:
    now = time.time()
    FAILED_BY_IP[ip] = [t for t in FAILED_BY_IP.get(ip, []) if now - t < 900]
    return len(FAILED_BY_IP[ip]) >= 20


class Handler(BaseHTTPRequestHandler):
    server_version = "CareFlow/2.0"
    sys_version = ""

    def log_message(self, fmt, *args):  # quieter console; never logs form bodies (passwords)
        sys.stderr.write("  %s %s\n" % (self.command, self.path.split("?")[0]))

    # ------------------------------------------------------------------ plumbing
    @property
    def token(self) -> str | None:
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return c["cf_session"].value if "cf_session" in c else None

    def _headers(self, ctype: str, length: int, extra: dict | None = None):
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def send_html(self, body: bytes, status=200, user=None):
        if user:  # every POST form carries the session's CSRF token
            tok = views.e(user["csrf"])
            body = re.sub(rb'(<form[^>]*method="post"[^>]*>)', lambda m: m.group(1) + f'<input type="hidden" name="csrf" value="{tok}">'.encode(), body)
        self.send_response(status)
        self._headers("text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self._headers("application/json", len(body))
        self.wfile.write(body)

    def redirect(self, url, msg="", err="", cookie=None):
        if not url.startswith("/") or url.startswith("//"):
            url = "/"  # no open redirects
        if msg or err:
            base, _, frag = url.partition("#")
            url = base + ("&" if "?" in base else "?") + (f"msg={quote(msg)}" if msg else f"err={quote(err)}") + (f"#{frag}" if frag else "")
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return origin is None
        return urlparse(origin).netloc == self.headers.get("Host", "")

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
        if ctype.startswith("application/json"):
            return json.loads(raw or b"{}"), {}
        return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}, {}

    @staticmethod
    def need(user, perm):
        if not auth.can(user, perm):
            raise Forbidden(f"Your role ({auth.ROLES[user['role']]}) is not allowed to do this.")

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        msg, err = qs.get("msg", [""])[0][:300], qs.get("err", [""])[0][:300]
        if u.path in STATIC:
            body = (config.ROOT / "static" / u.path.rsplit("/", 1)[1]).read_bytes()
            self.send_response(200)
            self._headers(STATIC[u.path], len(body))
            self.wfile.write(body)
            return
        try:
            with LOCK:
                user = auth.session_user(CONN, self.token)
                if u.path == "/login":
                    return self.send_html(views.view_login(err, msg, qs.get("next", ["/"])[0]))
                if not user:
                    return self.redirect("/login?next=" + quote(u.path))
                if user["must_change_pw"] and u.path != "/account":
                    return self.redirect("/account", err="Please choose your own password first.")
                body = self.route_get(u.path, user, msg, err)
            if body is None:
                return self.send_html(views.page("Not found", "<h1>Not found</h1>", user), 404, user)
            self.send_html(body, 200, user)
        except Forbidden as exc:
            self.send_html(views.page("Not allowed", f"<h1>Not allowed</h1><p>{views.e(str(exc))}</p>", user), 403, user)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self.send_html(views.page("Error", "<h1>Something went wrong</h1><p>See the terminal for details.</p>", None), 500)

    def route_get(self, path, user, msg, err):
        simple = {"/": (views.view_dashboard, None), "/assistant": (views.view_assistant, "ask_ai"),
                  "/alerts": (views.view_alerts, "view_clinical"), "/reminders": (views.view_reminders, "draft_reminders"),
                  "/billing": (views.view_billing, "view_bills"), "/inventory": (views.view_inventory, "view_inventory"),
                  "/usage": (views.view_usage, "view_usage"), "/rules": (views.view_rules, None), "/audit": (views.view_audit, "view_audit"),
                  "/account": (views.view_account, None), "/admin/users": (views.view_users, "manage_users"),
                  "/admit": (views.view_admit, "admit")}
        if path in simple:
            fn, perm = simple[path]
            if perm:
                self.need(user, perm)
            return fn(user, msg, err)
        if m := re.fullmatch(r"/patient/(\d+)", path):
            self.need(user, "view_clinical")
            return views.view_patient(int(m.group(1)), user, msg, err)
        if m := re.fullmatch(r"/bill/(\d+)", path):
            self.need(user, "view_bills")
            return views.view_bill_print(int(m.group(1)), user)
        if m := re.fullmatch(r"/print/(\d+)", path):
            self.need(user, "view_clinical")
            return views.view_draft_print(int(m.group(1)), user)
        return None

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        u = urlparse(self.path)
        ip = self.client_address[0]
        if not self.same_origin():
            return self.redirect("/", err="Request blocked: it came from another website.")
        try:
            f, files = self.form()
        except (ValueError, json.JSONDecodeError) as exc:
            return self.redirect("/", err=str(exc))
        if u.path == "/login":
            return self.post_login(f, ip)
        with LOCK:
            user = auth.session_user(CONN, self.token)
        is_api = u.path.startswith("/api/")
        if not user:
            return self.send_json({"error": "Signed out – please sign in again."}, 401) if is_api else self.redirect("/login", err="Please sign in.")
        sent = self.headers.get("X-CSRF-Token") if is_api else f.get("csrf")
        if not auth.check_csrf(user, sent):
            return self.send_json({"error": "Security token missing or expired – reload the page."}, 403) if is_api else \
                self.redirect("/", err="Security token missing or expired – please try again.")
        if u.path == "/logout":
            with LOCK:
                auth.end_session(CONN, self.token, user["username"])
            return self.redirect("/login", msg="Signed out.", cookie=auth.cookie_header(None))
        if user["must_change_pw"] and u.path != "/account/password":
            return self.redirect("/account", err="Please choose your own password first.")
        back = f.get("back") or "/"
        try:
            if is_api:
                return self.post_api(u.path, f, user)
            return self.route_post(u.path, f, files, user)
        except Forbidden as exc:
            return self.redirect(back, err=str(exc))
        except (auth.AuthError, intake.IntakeError, discharge.DischargeError, events.EventError, llm.BudgetExceeded,
                llm.ModelUnavailable, ValueError) as exc:
            target = re.match(r"/patient/\d+", u.path)
            return self.redirect((target.group(0) + "#" + (f.get("anchor") or "")) if target else
                                 (u.path.rsplit("/", 2)[0] if u.path.count("/") > 2 else back), err=str(exc))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self.redirect("/", err=f"Unexpected error: {type(exc).__name__}. See terminal.")

    def post_login(self, f, ip):
        if _ip_blocked(ip):
            return self.redirect("/login", err="Too many failed sign-ins from this computer. Wait 15 minutes.")
        try:
            with LOCK:
                u = auth.authenticate(CONN, f.get("username", ""), f.get("password", ""), ip)
                auth.end_session(CONN, self.token, u["username"]) if self.token else None  # no session fixation
                token = auth.start_session(CONN, u["id"], ip)
        except auth.AuthError as exc:
            FAILED_BY_IP.setdefault(ip, []).append(time.time())
            return self.redirect("/login", err=str(exc))
        nxt = f.get("next") or "/"
        return self.redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/", cookie=auth.cookie_header(token))

    def post_api(self, path, f, user):
        if path != "/api/ask":
            return self.send_json({"error": "Unknown endpoint"}, 404)
        if not auth.can(user, "ask_ai"):
            return self.send_json({"error": "Your role cannot use the assistant."}, 403)
        q = (f.get("q") or "").strip()[:1000]
        if not q:
            return self.send_json({"error": "Type a question."}, 400)
        focus = f.get("focus")
        focus = int(focus) if str(focus or "").isdigit() else None
        with LOCK:
            hist = [{"role": r["role"], "content": r["content"]} for r in
                    CONN.execute("SELECT role, content FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 6", (user["id"],)).fetchall()[::-1]]
            n0 = CONN.execute("SELECT COALESCE(MAX(id),0) FROM usage").fetchone()[0]
            try:
                res = qa.ask(CONN, q, focus_pid=focus, history=hist, user_id=user["id"], can_see_bills=auth.can(user, "view_bills"))
            except (llm.BudgetExceeded, llm.ModelUnavailable) as exc:
                return self.send_json({"error": str(exc)}, 429 if isinstance(exc, llm.BudgetExceeded) else 503)
            tok = CONN.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage WHERE id>?", (n0,)).fetchone()[0]
            meta = {"steps": res["steps"], "tools": [t["tool"] for t in res["trace"]], "invalid": res["check"]["invalid"],
                    "has_citations": res["check"]["has_citations"], "blocked": res.get("blocked", False), "tokens": tok}
            CONN.execute("INSERT INTO chat_messages (user_id, ts, role, content, meta) VALUES (?,?,?,?,?)", (user["id"], db.now(), "user", q, None))
            CONN.execute("INSERT INTO chat_messages (user_id, ts, role, content, meta) VALUES (?,?,?,?,?)",
                         (user["id"], db.now(), "assistant", res["answer"], json.dumps(meta)))
            db.audit(CONN, user["username"], "assistant_question", {"focus": focus, "steps": res["steps"], "blocked": meta["blocked"]})
            CONN.commit()
        return self.send_json({"user_html": views.chat_bubble_html("user", q, {}),
                               "bot_html": views.chat_bubble_html("assistant", res["answer"], meta)})

    def route_post(self, path, f, files, user):
        who = user["label"]
        if path == "/account/password":
            with LOCK:
                row = CONN.execute("SELECT username FROM users WHERE id=?", (user["id"],)).fetchone()
                auth.authenticate(CONN, row["username"], f.get("current", ""), self.client_address[0])
                if f.get("new") != f.get("confirm"):
                    raise auth.AuthError("The two new passwords do not match.")
                auth.set_password(CONN, user["id"], f.get("new", ""), actor=user["username"])
                token = auth.start_session(CONN, user["id"], self.client_address[0])
            return self.redirect("/", msg="Password changed. Other sessions were signed out.", cookie=auth.cookie_header(token))
        if path == "/admin/users":
            self.need(user, "manage_users")
            with LOCK:
                auth.create_user(CONN, f.get("username", ""), f.get("full_name", ""), f.get("role", ""), f.get("password", ""), actor=user["username"])
            return self.redirect("/admin/users", msg="Account created. They must choose their own password at first sign-in.")
        if m := re.fullmatch(r"/admin/users/(\d+)/(toggle|reset|unlock)", path):
            self.need(user, "manage_users")
            uid, act = int(m.group(1)), m.group(2)
            if uid == user["id"]:
                raise Forbidden("Use My account to change your own account.")
            with LOCK:
                if act == "toggle":
                    CONN.execute("UPDATE users SET active=1-active WHERE id=?", (uid,))
                    CONN.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
                elif act == "unlock":
                    CONN.execute("UPDATE users SET locked_until=NULL, failed_attempts=0 WHERE id=?", (uid,))
                else:
                    auth.set_password(CONN, uid, f.get("password", ""), actor=user["username"], must_change=True)
                db.audit(CONN, user["username"], f"user_{act}", {"user_id": uid})
                CONN.commit()
            return self.redirect("/admin/users", msg="Done.")
        if path == "/admit":
            self.need(user, "admit")
            with LOCK:
                pid = intake.new_admission(CONN, f, who)
            return self.redirect(f"/patient/{pid}#admission", msg="Registered. Now attach the handwritten admission papers.")
        if m := re.fullmatch(r"/alerts/(\d+)/ack", path):
            self.need(user, "ack_alert")
            with LOCK:
                events.acknowledge_alert(CONN, int(m.group(1)), who)
            return self.redirect(f.get("back") or "/alerts", msg="Alert acknowledged.")
        if m := re.fullmatch(r"/drafts/(\d+)/approve", path):
            self.need(user, "approve_draft")
            with LOCK:
                drafts.approve(CONN, int(m.group(1)), who)
            return self.redirect(f.get("back") or "/", msg="Approved and signed with your name.")
        if m := re.fullmatch(r"/reminders/(\d+)/(authorise|cancel|send|email)", path):
            rid, act = int(m.group(1)), m.group(2)
            self.need(user, "authorise_reminders" if act in ("authorise", "send") else "draft_reminders")
            with LOCK:
                if act == "authorise":
                    reminders.authorise(CONN, rid, who)
                    note = "Authorised. It will be sent on its due date."
                elif act == "send":
                    note = "Sent via " + reminders.deliver(CONN, rid, who)
                elif act == "cancel":
                    reminders.cancel(CONN, rid, who)
                    note = "Cancelled."
                else:
                    reminders.set_email(CONN, rid, f.get("email", ""), who)
                    note = "Email saved."
            return self.redirect("/reminders", msg=note)
        if m := re.fullmatch(r"/billing/(\d+)/pay", path):
            self.need(user, "take_payment")
            with LOCK:
                discharge.record_payment(CONN, int(m.group(1)), f.get("mode", ""), who)
            return self.redirect("/billing", msg="Payment recorded.")
        if path == "/inventory/restock":
            self.need(user, "restock")
            qty = int(f.get("qty") or 0)
            reason = (f.get("reason") or "").strip()[:120]
            if not 1 <= qty <= 10000 or not reason:
                raise ValueError("Enter a quantity (1–10000) and the supplier / batch.")
            with LOCK:
                if not CONN.execute("SELECT 1 FROM inventory WHERE item_code=?", (f.get("item_code"),)).fetchone():
                    raise ValueError("Unknown item.")
                CONN.execute("UPDATE inventory SET stock=stock+? WHERE item_code=?", (qty, f["item_code"]))
                CONN.execute("INSERT INTO stock_ledger (item_code, delta, event_id, ts, reason, by_user) VALUES (?,?,NULL,?,?,?)",
                             (f["item_code"], qty, db.now(), f"Received: {reason}", who))
                db.audit(CONN, who, "stock_received", {"item": f["item_code"], "qty": qty})
                CONN.commit()
            return self.redirect("/inventory", msg="Stock received.")
        if m := re.fullmatch(r"/patient/(\d+)/(\w+)", path):
            return self.post_patient(int(m.group(1)), m.group(2), f, files, user)
        return self.redirect("/", err="Unknown action.")

    def post_patient(self, pid, action, f, files, user):
        who, back = user["label"], f"/patient/{pid}"
        p = CONN.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if not p:
            return self.redirect("/", err="Unknown patient.")
        if action == "event":
            return self.post_event(p, f, user)
        if action in ("upload", "sample"):
            self.need(user, "upload_papers")
            if action == "sample":
                samples = {x.name: x for x in (config.DATA / "samples").iterdir()}  # whitelist, no path traversal
                path = samples.get(f.get("which") or "")
                if not path:
                    return self.redirect(back + "#admission", err="Unknown sample.")
                content, fname = path.read_bytes(), path.name
                doc_type = "other" if "injection" in fname else "opd_note"
            else:
                fname, content = files.get("file", ("", b""))
                doc_type = f.get("doc_type", "other")
                if not content:
                    return self.redirect(back + "#admission", err="Choose a file first.")
            with LOCK:
                did = extract.add_document(CONN, pid, fname, content, who, doc_type)
            return self.redirect(back + "#admission", msg=f"Uploaded as D{did}. Click “Read with AI”.")
        if action == "extract":
            self.need(user, "upload_papers")
            with LOCK:
                r = extract.run(CONN, pid, int(f["document_id"]), who, user_id=user["id"])
            total, review = extract.review_count(r["data"])
            note = " (identical file read before – reused, no AI cost)" if r["reused"] else ""
            inj = " Instructions inside the paper were detected and ignored." if r["data"].get("embedded_instructions") else ""
            return self.redirect(back + "#admission", msg=f"Read {total} items; please verify the highlighted ones. "
                                                          f"{r['vitals_events']} vital-sign sets checked against red-flag rules.{note}{inj}")
        if action == "review":
            self.need(user, "verify_papers")
            with LOCK:
                intake.review(CONN, pid, int(f.get("extraction_id") or 0), f.get("field_key", ""), f.get("state", ""), f.get("corrected", ""), who)
            return self.redirect(back + "#admission", msg="Saved.")
        if action == "complete_intake":
            self.need(user, "verify_papers")
            with LOCK:
                intake.complete(CONN, pid, f, who)
            return self.redirect(back, msg="Admission complete. Allergies, blood group and history are now doctor-verified.")
        if action == "draft":
            self.need(user, "draft")
            kind = f.get("kind")
            if kind not in drafts.KINDS:
                return self.redirect(back, err="Unknown draft type.")
            with LOCK:
                drafts.generate(CONN, pid, kind, who, user_id=user["id"])
            return self.redirect(back + "#docs", msg="Draft created – review the grounding check before approving.")
        if action == "discharge":
            self.need(user, "discharge")
            days = f.get("follow_up_days")
            with LOCK:
                r = discharge.finalize(CONN, pid, who, follow_up_days=int(days) if str(days or "").isdigit() else None,
                                       follow_up_note=f.get("follow_up_note", ""), deferral_note=f.get("deferral_note", ""),
                                       email=f.get("email", ""))
            return self.redirect(back + "#docs", msg=f"Discharged. Final family bill ₹{r['bill_total']:,.0f}. "
                                                     f"{r['reminders_drafted']} email reminder(s) drafted for consultant authorisation.")
        return self.redirect(back, err="Unknown action.")

    def post_event(self, p, f, user):
        pid, t = p["id"], f.get("type")
        perm = {"vitals": "enter_obs", "labour": "enter_obs", "newborn_obs": "enter_obs", "note": "enter_obs", "service": "enter_obs",
                "med_admin": "give_meds", "vaccine": "give_meds", "investigation": "enter_labs", "delivery": "record_delivery"}.get(t)
        if not perm:
            return self.redirect(f"/patient/{pid}", err="Unknown entry type.")
        self.need(user, perm)
        anchor = {"labour": "labour", "delivery": "labour", "newborn_obs": "newborn", "vaccine": "vaccines"}.get(t, "bedside")
        f["anchor"] = anchor
        ts = None
        if t == "investigation":
            data = {"test": f.get("test"), "result_text": f.get("result_text"), "code": f.get("code") or None,
                    "values": {k[4:]: v for k, v in f.items() if k.startswith("lab_") and v.strip()}}
        elif t == "vaccine":
            dose, route = VACCINE_ROUTE.get(f.get("code"), ("", ""))
            data = {"code": f.get("code"), "batch": (f.get("batch") or "").strip()[:40], "dose": dose, "route": route}
        elif t == "delivery":
            data = dict(f)
            if f.get("delivered_at"):
                when = datetime.strptime(f["delivered_at"][:16], "%Y-%m-%dT%H:%M")
                if when > datetime.now():
                    raise events.EventError("Time of birth cannot be in the future.")
                ts = when.strftime(db.FMT)
        else:
            data = {k: v for k, v in f.items() if k not in ("csrf", "key", "type", "anchor")}
        key = f"form-{user['id']}-{f.get('key')}" if f.get("key") else None
        with LOCK:
            r = events.record_event(CONN, pid, t, data, ts=ts, entered_by=user["label"], key=key, actor_role=user["role"])
        back = f"/patient/{pid}"
        if r["duplicate"]:
            return self.redirect(back + "#timeline", msg="Already saved (duplicate submission ignored).")
        if r.get("baby_id"):
            return self.redirect(f"/patient/{r['baby_id']}", msg=f"Delivery saved (E{r['event_id']}). Baby's record created – please record newborn "
                                                                f"observations and birth doses." + (" Alerts raised for the consultant." if r["alerts"] else ""))
        note = f" {len(r['alerts'])} alert(s) raised – consultant notified." if r["alerts"] else ""
        return self.redirect(back + (f"#alerts" if r["alerts"] else f"#{anchor}"), msg=f"Saved as E{r['event_id']}.{note}")


def reminder_loop():
    while True:
        time.sleep(60)
        try:
            with LOCK:
                reminders.send_due(CONN)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def main():
    port = 8050
    for a in sys.argv[1:]:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    if not config.MOCK and not config.api_key():
        print("WARNING: PORTKEY_API_KEY not found in .env (repo folder or the folder above). AI buttons will show an error.")
    print(f"CareFlow AI running at http://127.0.0.1:{port}   (Ctrl+C to stop)")
    print(f".env files loaded: {', '.join(config.ENV_FILES) or 'none'}")
    threading.Thread(target=reminder_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
