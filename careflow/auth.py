"""Staff accounts, passwords and sessions (Python standard library only).

Precautions taken:
- Passwords are never stored: only a salted PBKDF2-HMAC-SHA256 hash (600,000 iterations, 16-byte random salt).
- Password checks use constant-time comparison; unknown usernames still run a hash so timing
  does not reveal which usernames exist, and the error message is the same for both cases.
- 5 failed attempts lock the account for 15 minutes. Every login, failure and lockout is audited.
- Session tokens are 256-bit random values; only their SHA-256 hash is stored, so a copied database
  cannot be used to log in. Sessions end after 30 minutes idle or 12 hours in total, and on logout.
- Every state-changing request must carry the session's CSRF token; cookies are HttpOnly + SameSite=Strict.
- Password policy: at least 10 characters, letters and digits, not containing the username, not a common password.
- Roles are enforced on the server for every action (see PERMISSIONS), not just hidden in the UI.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta

from . import config
from .db import FMT, audit, now, parse_ts

ROLES = {
    "consultant": "Consultant",
    "doctor": "Resident doctor",
    "nurse": "Nurse",
    "billing": "Billing / front office",
    "admin": "Administrator",
}
CLINICAL = {"consultant", "doctor", "nurse"}
DOCTORS = {"consultant", "doctor"}

PERMISSIONS = {
    "view_clinical": CLINICAL,
    "admit": CLINICAL,
    "upload_papers": CLINICAL,
    "verify_papers": DOCTORS,
    "enter_obs": CLINICAL,
    "give_meds": CLINICAL,
    "enter_labs": CLINICAL,
    "record_delivery": DOCTORS,
    "override_allergy": DOCTORS,
    "ack_alert": {"consultant"},
    "draft": DOCTORS,
    "approve_draft": DOCTORS,
    "discharge": DOCTORS,
    "draft_reminders": DOCTORS,
    "authorise_reminders": {"consultant"},
    "ask_ai": CLINICAL,
    "view_bills": {"consultant", "doctor", "billing"},
    "take_payment": {"billing"},
    "restock": {"billing", "nurse", "admin"},
    "view_inventory": CLINICAL | {"billing", "admin"},  # doctors asked to see stock too (user trial)
    "manage_users": {"admin"},
    "view_audit": {"admin", "consultant"},
    "view_usage": {"admin", "consultant", "doctor"},
}

COMMON_PASSWORDS = {"password123", "password1234", "qwerty12345", "1234567890", "admin12345", "welcome123",
                    "india12345", "hospital123", "careflow123", "letmein1234", "iloveyou123", "abc1234567"}

_DUMMY_SALT = secrets.token_bytes(16)


class AuthError(ValueError):
    pass


def can(user: dict | None, permission: str) -> bool:
    return bool(user) and user["role"] in PERMISSIONS.get(permission, set())


def _hash(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


def password_problems(username: str, password: str) -> list[str]:
    probs = []
    if len(password) < config.MIN_PASSWORD_LENGTH:
        probs.append(f"at least {config.MIN_PASSWORD_LENGTH} characters")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        probs.append("both letters and numbers")
    if username and username.lower() in password.lower():
        probs.append("must not contain the username")
    if password.lower() in COMMON_PASSWORDS:
        probs.append("too common")
    return probs


def valid_username(username: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,31}", username or ""))


def create_user(conn: sqlite3.Connection, username: str, full_name: str, role: str, password: str, *,
                actor: str, is_demo: bool = False, must_change: bool = True, check_policy: bool = True) -> int:
    username = (username or "").strip().lower()
    if not valid_username(username):
        raise AuthError("Username: 3–32 characters, lowercase letters, digits, dot, dash or underscore.")
    if role not in ROLES:
        raise AuthError("Unknown role.")
    if not (full_name or "").strip():
        raise AuthError("Full name is required.")
    if check_policy and (p := password_problems(username, password)):
        raise AuthError("Password needs: " + ", ".join(p) + ".")
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise AuthError("That username is taken.")
    salt = secrets.token_bytes(16)
    cur = conn.execute(
        "INSERT INTO users (username, full_name, role, pw_hash, pw_salt, pw_iterations, must_change_pw, is_demo, created_at, pw_changed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (username, full_name.strip()[:80], role, _hash(password, salt, config.PBKDF2_ITERATIONS), salt.hex(),
         config.PBKDF2_ITERATIONS, int(must_change), int(is_demo), now(), now()))
    audit(conn, actor, "user_created", {"username": username, "role": role})
    conn.commit()
    return cur.lastrowid


def set_password(conn: sqlite3.Connection, user_id: int, password: str, *, actor: str, must_change: bool = False) -> None:
    u = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if p := password_problems(u["username"], password):
        raise AuthError("Password needs: " + ", ".join(p) + ".")
    if hmac.compare_digest(_hash(password, bytes.fromhex(u["pw_salt"]), u["pw_iterations"]), u["pw_hash"]):
        raise AuthError("Choose a password different from the current one.")
    salt = secrets.token_bytes(16)
    conn.execute("UPDATE users SET pw_hash=?, pw_salt=?, pw_iterations=?, must_change_pw=?, is_demo=0, pw_changed_at=?, "
                 "failed_attempts=0, locked_until=NULL WHERE id=?",
                 (_hash(password, salt, config.PBKDF2_ITERATIONS), salt.hex(), config.PBKDF2_ITERATIONS, int(must_change), now(), user_id))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))  # sign out everywhere
    audit(conn, actor, "password_changed" if not must_change else "password_reset", {"user_id": user_id})
    conn.commit()


def authenticate(conn: sqlite3.Connection, username: str, password: str, ip: str = "") -> dict:
    """Returns the user row as a dict, or raises AuthError with a deliberately generic message."""
    username = (username or "").strip().lower()
    u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not u:
        _hash(password or "", _DUMMY_SALT, config.PBKDF2_ITERATIONS)  # same work as a real check
        audit(conn, username[:40] or "?", "login_failed", {"reason": "unknown user", "ip": ip})
        conn.commit()
        raise AuthError("Invalid username or password.")
    if u["locked_until"] and parse_ts(u["locked_until"]) > datetime.now():
        audit(conn, username, "login_blocked", {"reason": "locked", "ip": ip})
        conn.commit()
        raise AuthError(f"Account locked after {config.MAX_FAILED_LOGINS} failed attempts. Try again later or ask the administrator.")
    ok = hmac.compare_digest(_hash(password or "", bytes.fromhex(u["pw_salt"]), u["pw_iterations"]), u["pw_hash"])
    if not ok or not u["active"]:
        fails = u["failed_attempts"] + 1
        locked = (datetime.now() + timedelta(minutes=config.LOCKOUT_MINUTES)).strftime(FMT) if fails >= config.MAX_FAILED_LOGINS else None
        conn.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?",
                     (0 if locked else fails, locked, u["id"]))
        audit(conn, username, "account_locked" if locked else "login_failed",
              {"reason": "inactive" if ok else "bad password", "attempt": fails, "ip": ip})
        conn.commit()
        raise AuthError("Invalid username or password." + (" The account is now locked for "
                                                          f"{config.LOCKOUT_MINUTES} minutes." if locked else ""))
    conn.execute("UPDATE users SET failed_attempts=0, locked_until=NULL, last_login=? WHERE id=?", (now(), u["id"]))
    audit(conn, username, "login", {"ip": ip})
    conn.commit()
    return dict(u)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(conn: sqlite3.Connection, user_id: int, ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token_hash, user_id, csrf, created_at, last_seen, ip) VALUES (?,?,?,?,?,?)",
                 (_token_hash(token), user_id, secrets.token_urlsafe(24), now(), now(), ip))
    conn.commit()
    return token


def session_user(conn: sqlite3.Connection, token: str | None) -> dict | None:
    """Look up a session; expire it if idle or too old. Returns user dict + csrf, or None."""
    if not token or len(token) > 100:
        return None
    th = _token_hash(token)
    row = conn.execute("SELECT s.*, u.username, u.full_name, u.role, u.active, u.must_change_pw, u.is_demo "
                       "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (th,)).fetchone()
    if not row:
        return None
    t = datetime.now()
    if (not row["active"] or t - parse_ts(row["last_seen"]) > timedelta(minutes=config.SESSION_IDLE_MINUTES)
            or t - parse_ts(row["created_at"]) > timedelta(hours=config.SESSION_MAX_HOURS)):
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
        conn.commit()
        return None
    conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now(), th))
    conn.commit()
    return {"id": row["user_id"], "username": row["username"], "full_name": row["full_name"], "role": row["role"],
            "csrf": row["csrf"], "must_change_pw": bool(row["must_change_pw"]), "is_demo": bool(row["is_demo"]),
            "label": f"{row['full_name']} · {ROLES[row['role']].lower()}"}


def end_session(conn: sqlite3.Connection, token: str | None, actor: str = "") -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
        audit(conn, actor or "?", "logout", {})
        conn.commit()


def check_csrf(user: dict | None, submitted: str | None) -> bool:
    return bool(user) and bool(submitted) and hmac.compare_digest(user["csrf"], submitted)


def cookie_header(token: str | None) -> str:
    secure = "; Secure" if config.COOKIE_SECURE else ""
    if token is None:
        return f"cf_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{secure}"
    return f"cf_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={config.SESSION_MAX_HOURS * 3600}{secure}"
